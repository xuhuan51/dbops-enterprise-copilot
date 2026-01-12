import uuid
import asyncio
import re  # 🔥 必须放在最外层，防止命名空间冲突
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# 🔥 核心修改: 导入整个模块，确保获取最新的 master_app 对象
import app.core.master_graph as mg

# 引入 SQL 执行器
from app.modules.sql.executor import execute_select
from app.core.logger import logger

router = APIRouter(tags=["AI Agent Query"])


class AgentQueryRequest(BaseModel):
    query: str
    user_id: str = "sys_user"
    session_id: Optional[str] = None


@router.post("/query")
async def agent_query_endpoint(req: AgentQueryRequest):
    """
    AI Agent 接口：
    输入：自然语言 (e.g. "帮我查一下北京的销量")
    输出：执行结果 + 思考步骤 (steps)
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
        # 注意：这里的 history 取决于你在 Graph 中如何记录步骤，如果 state 没有 history 字段，则为空
        steps = final_state.get("history", [])

        # =================================================
        # 分支 A: SQL 任务 (Agent 决定查库)
        # =================================================
        # 只有当 final_answer 明确以 SQL_RESULT: 开头时才执行
        if final_answer and final_answer.startswith("SQL_RESULT:"):
            sql = final_answer.replace("SQL_RESULT:", "").strip()

            # 🔥 Fix: SQL 安全卫士 (使用顶部的 re 模块)
            # 严禁执行非查询语句，防止 Prompt 注入攻击
            forbidden_pattern = re.compile(r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|GRANT|REVOKE)\b",
                                           re.IGNORECASE)

            if forbidden_pattern.search(sql):
                logger.warning(f"🛑 Blocked dangerous SQL: {sql}", extra={"trace_id": trace_id})
                return {
                    "trace_id": trace_id,
                    "success": False,
                    "error": "Security Alert: Dangerous SQL detected and blocked.",
                    "intent": "DATA_QUERY",
                    "steps": steps
                }

            # 在线程池中执行同步的 SQL executor
            loop = asyncio.get_running_loop()
            try:
                result_data = await loop.run_in_executor(
                    None,
                    lambda: execute_select(req.user_id, sql, trace_id=trace_id)
                )
            except Exception as e:
                # 捕获执行错误，优雅返回
                return {
                    "trace_id": trace_id,
                    "success": False,
                    "error": f"Execution Failed: {str(e)}",
                    "intent": "DATA_QUERY",
                    "steps": steps
                }

            # 注入元数据
            result_data["agent_meta"] = {
                "trace_id": trace_id,
                "session_id": thread_id,
                "intent": "DATA_QUERY",
                "tables_used": final_state.get("tables_used", []),
                "generated_sql": sql,
                "steps": steps
            }
            result_data["session_id"] = thread_id
            data_preview = str(result_data.get("data", []))[:200]
            row_count = len(result_data.get("data", [])) if isinstance(result_data.get("data"), list) else 0

            logger.info(f"🔢 [SQL Data] Rows: {row_count} | Preview: {data_preview}...", extra={"trace_id": trace_id})

            return result_data

        # =================================================
        # 分支 B: 纯文本任务 (闲聊 / 知识问答 / 熔断兜底)
        # =================================================
        else:

            final_message = final_state.get("intent", "UNKNOWN")
            reply_content = final_answer  # 这里就是那个 "抱歉..." 或者闲聊回复

            # 🔥 新增: 显式打印回复内容，方便调试
            logger.info(f"💬 [Text Reply] {reply_content}", extra={"trace_id": trace_id})
            # 如果是 Fallback Node 返回的，final_answer 就是那段“抱歉...”的文本
            # 直接透传给前端
            return {
                "trace_id": trace_id,
                "session_id": thread_id,
                "success": True,
                "type": "text",
                "intent": final_state.get("intent", "UNKNOWN"),
                "message": final_answer,  # 这里包含 Fallback 的友好提示
                "steps": steps
            }

    except Exception as e:
        logger.error("Agent Internal Error", extra={"trace_id": trace_id}, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Agent Error: {str(e)}")