import uuid
import asyncio
import re
import json
from typing import Dict, Any, Optional

# LangChain 组件
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

# 配置与 Prompt
from app.core.config import settings
from app.core.prompts import DATA_SUMMARY_PROMPT

# 核心图与组件
import app.core.master_graph as mg
from app.modules.sql.executor import execute_select
from app.core.logger import logger


class AgentService:
    def __init__(self):
        # 初始化分析师 LLM (专门用于解释数据)
        self.summary_llm = ChatOpenAI(
            model=settings.LLM_MODEL,
            temperature=0.7,  # 分析师可以稍微有点温度
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            max_tokens=1024
        )

    async def process_query(self, query: str, user_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        处理 Agent 查询的核心业务逻辑 (已修复 Fallback 短路逻辑)
        """
        trace_id = str(uuid.uuid4())
        thread_id = session_id or str(uuid.uuid4())

        # 结果容器
        final_result = {
            "trace_id": trace_id,
            "session_id": thread_id,
            "query": query,
            "success": False,
            "message": "",
            "data": [],
            "sql": None,
            "intent": "UNKNOWN",
            "steps": []
        }

        try:
            # LangGraph 配置
            config = {"configurable": {"thread_id": thread_id}}

            # =================================================
            # 1. 调用 Master Graph (推理核心)
            # =================================================
            logger.info(f"🚀 [Agent] Starting graph execution for: {query}", extra={"trace_id": trace_id})

            final_state = await mg.master_app.ainvoke(
                {"question": query, "trace_id": trace_id},
                config=config
            )

            final_answer = final_state.get("final_answer", "")
            steps = final_state.get("history", [])
            # 🔥 关键：优先获取 intent，用于后续的短路判断
            intent = final_state.get("intent", "UNKNOWN")

            final_result["steps"] = steps
            final_result["intent"] = intent

            # =================================================
            # 🚦 核心修复：分支判断逻辑 (短路 Fallback)
            # =================================================
            # 只有满足以下所有条件，才被视为 SQL 任务：
            # 1. final_answer 有内容
            # 2. 以 SQL_RESULT: 开头
            # 3. 🔥 intent 不是 'non_data' (这是 Fallback/Refusal 的标志)
            is_sql_task = (
                    final_answer
                    and final_answer.startswith("SQL_RESULT:")
                    and intent != "non_data"
            )

            # =================================================
            # 分支 A: SQL 任务 (Agent 决定查库)
            # =================================================
            if is_sql_task:
                final_result["intent"] = "DATA_QUERY"

                # 1. 提取 SQL
                sql = final_answer.replace("SQL_RESULT:", "").strip()
                final_result["sql"] = sql

                # 2. SQL 安全检查
                forbidden_pattern = re.compile(r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|GRANT|REVOKE)\b",
                                               re.IGNORECASE)
                if forbidden_pattern.search(sql):
                    logger.error("🛑 Security Alert: Dangerous SQL detected.")
                    final_result["error"] = "Security Alert: Dangerous SQL detected."
                    return final_result

                # 3. 执行 SQL (Executor 层强制 LIMIT 1000 兜底)
                loop = asyncio.get_running_loop()
                try:
                    db_res = await loop.run_in_executor(
                        None,
                        lambda: execute_select(user_id, sql, trace_id=trace_id)
                    )
                except Exception as e:
                    logger.error(f"Execution Failed: {e}")
                    final_result["error"] = f"Database Error: {str(e)}"
                    return final_result

                raw_data = db_res.get("data", [])
                error_msg = db_res.get("error")

                if error_msg:
                    final_result["error"] = error_msg
                    final_result["message"] = f"查询执行出错: {error_msg}"
                    return final_result

                # =========================================================
                # 核心功能：展示层截断 (Display Truncation)
                # =========================================================
                DISPLAY_LIMIT = 5
                total_count = len(raw_data)

                if total_count > DISPLAY_LIMIT:
                    preview_data = raw_data[:DISPLAY_LIMIT]
                    data_context_msg = (
                        f"【注意】底层数据共找到 {total_count} 条，"
                        f"为优化展示，**仅向您提供前 {DISPLAY_LIMIT} 条**作为样本。\n"
                        f"数据预览：\n{json.dumps(preview_data, ensure_ascii=False, default=str)}"
                    )
                else:
                    preview_data = raw_data
                    data_context_msg = f"数据结果（共 {total_count} 条）：\n{json.dumps(preview_data, ensure_ascii=False, default=str)}"

                final_result["data"] = preview_data
                final_result["success"] = True

                # 4. 召唤 Analyst
                process_summary = "\n".join([str(s)[:200] for s in steps]) if steps else "执行过程已省略"

                summary_prompt = DATA_SUMMARY_PROMPT.format(
                    question=query,
                    process_history=process_summary,
                    sql=sql,
                    data_context=data_context_msg
                )

                logger.info("🧠 [Analyst] Analyzing data...", extra={"trace_id": trace_id})
                try:
                    ai_response = await self.summary_llm.ainvoke([HumanMessage(content=summary_prompt)])
                    summary_text = ai_response.content
                except Exception as e:
                    logger.error(f"Summary Generation Failed: {e}")
                    summary_text = f"查询成功，共找到 {total_count} 条数据，详情请见下方表格。"

                logger.info(f"🗣️ [Analyst Reply] {summary_text}", extra={"trace_id": trace_id})
                final_result["message"] = summary_text

                return final_result

            # =================================================
            # 分支 B: 纯文本任务 (闲聊 / 拒绝 / Fallback / 知识问答)
            # =================================================
            else:
                # 即使 intent 是 non_data，final_answer 可能还是带了 SQL_RESULT 前缀（脏数据），这里清洗一下
                clean_reply = final_answer.replace("SQL_RESULT:", "").strip() if final_answer else ""

                # 如果是 Fallback 触发的 non_data，回复通常已经是道歉文案了
                final_result["success"] = True
                final_result["message"] = clean_reply
                final_result["data"] = []  # 确保数据为空

                logger.info(f"💬 [Text Reply] {clean_reply[:100]}...", extra={"trace_id": trace_id})

                return final_result

        except Exception as e:
            logger.error("Agent Service Internal Error", extra={"trace_id": trace_id}, exc_info=True)
            final_result["success"] = False
            final_result["error"] = str(e)
            return final_result