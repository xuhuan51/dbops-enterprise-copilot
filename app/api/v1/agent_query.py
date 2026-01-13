import uuid
import asyncio
import re
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# 🔥 引入 LangChain 组件，用于最后的数据总结
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

# 🔥 引入配置和分析师 Prompt
from app.core.config import settings
from app.core.prompts import DATA_SUMMARY_PROMPT

# 引入核心图和组件
import app.core.master_graph as mg
from app.modules.sql.executor import execute_select
from app.core.logger import logger

router = APIRouter(tags=["AI Agent Query"])

# 🔥🔥🔥 实例化一个负责总结的轻量级 LLM (Analyst)
summary_llm = ChatOpenAI(
    model=settings.LLM_MODEL,
    temperature=0.7,  # 稍微有点温度，让回答更自然
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL,
    max_tokens=1024
)


class AgentQueryRequest(BaseModel):
    query: str
    user_id: str = "sys_user"
    session_id: Optional[str] = None


@router.post("/query")
async def agent_query_endpoint(req: AgentQueryRequest):
    """
    AI Agent 接口：
    输入：自然语言 (e.g. "帮我查一下北京的销量")
    输出：执行结果 + 思考步骤 (steps) + AI总结 (message)
    """
    trace_id = str(uuid.uuid4())
    thread_id = req.session_id or str(uuid.uuid4())

    try:
        # LangGraph 配置
        config = {"configurable": {"thread_id": thread_id}}

        # 1. 调用 Master Graph (异步)
        final_state = await mg.master_app.ainvoke(
            {"question": req.query, "trace_id": trace_id},
            config=config
        )

        final_answer = final_state.get("final_answer", "")
        steps = final_state.get("history", [])

        # =================================================
        # 分支 A: SQL 任务 (Agent 决定查库)
        # =================================================
        if final_answer and final_answer.startswith("SQL_RESULT:"):
            sql = final_answer.replace("SQL_RESULT:", "").strip()

            # SQL 安全检查
            forbidden_pattern = re.compile(r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|GRANT|REVOKE)\b",
                                           re.IGNORECASE)
            if forbidden_pattern.search(sql):
                return {
                    "trace_id": trace_id, "success": False,
                    "error": "Security Alert: Dangerous SQL detected.",
                    "intent": "DATA_QUERY", "steps": steps
                }

            # 执行 SQL
            loop = asyncio.get_running_loop()
            try:
                result_data = await loop.run_in_executor(
                    None,
                    lambda: execute_select(req.user_id, sql, trace_id=trace_id)
                )
            except Exception as e:
                return {
                    "trace_id": trace_id, "success": False,
                    "error": f"Execution Failed: {str(e)}",
                    "intent": "DATA_QUERY", "steps": steps
                }

            # 🔥🔥🔥 核心升级: AI 分析师介入 (The Analyst Node) 🔥🔥🔥
            rows = result_data.get("data", [])
            row_count = len(rows) if isinstance(rows, list) else 0

            # 1. 格式化执行过程 (History Formatting)
            # 将 list 类型的 steps 转换为字符串，供 LLM 参考
            process_history_str = ""
            if steps:
                for i, step in enumerate(steps):
                    # 简单转字符串，并截断过长内容防止 Token 溢出
                    step_content = str(step)[:300]
                    process_history_str += f"[Step {i+1}] {step_content}\n"
            else:
                process_history_str = "无详细执行记录"

            # 2. 截取数据预览
            data_preview = str(rows[:10])

            # 3. 构造分析师 Prompt (注入了 process_history)
            summary_prompt = DATA_SUMMARY_PROMPT.format(
                question=req.query,
                process_history=process_history_str, # <--- 新增字段
                sql=sql,
                max_rows=10,
                data_preview=data_preview
            )

            logger.info("🧠 [Analyst] Analyzing process & data...", extra={"trace_id": trace_id})

            summary_text = ""
            try:
                # 异步调用 LLM 生成人话
                ai_response = await summary_llm.ainvoke([HumanMessage(content=summary_prompt)])
                summary_text = ai_response.content
            except Exception as e:
                logger.error(f"Summary Generation Failed: {e}")
                summary_text = f"查询成功，共找到 {row_count} 条数据，详情请见下方列表。"

            # 打印日志
            logger.info(f"🗣️ [Analyst Reply] {summary_text}", extra={"trace_id": trace_id})
            logger.info(f"🔢 [SQL Data] Rows: {row_count} | Preview: {str(rows)[:100]}...", extra={"trace_id": trace_id})

            # 构造最终返回
            result_data["agent_meta"] = {
                "trace_id": trace_id,
                "session_id": thread_id,
                "intent": "DATA_QUERY",
                "tables_used": final_state.get("tables_used", []),
                "generated_sql": sql,
                "steps": steps
            }
            # 🔥 把 AI 生成的总结塞进 message 字段
            result_data["message"] = summary_text
            result_data["session_id"] = thread_id

            return result_data

        # =================================================
        # 分支 B: 纯文本任务 (闲聊 / 知识问答 / 熔断兜底)
        # =================================================
        else:
            final_message = final_state.get("intent", "UNKNOWN")
            reply_content = final_answer

            logger.info(f"💬 [Text Reply] {reply_content}", extra={"trace_id": trace_id})

            return {
                "trace_id": trace_id,
                "session_id": thread_id,
                "success": True,
                "type": "text",
                "intent": final_message,
                "message": reply_content,
                "steps": steps
            }

    except Exception as e:
        logger.error("Agent Internal Error", extra={"trace_id": trace_id}, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Agent Error: {str(e)}")