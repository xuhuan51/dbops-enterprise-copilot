import uuid
import asyncio
import json
import time
from typing import Dict, Any, Optional

# SQL 解析库
import sqlglot
from sqlglot import exp

# LangChain 组件
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

# 配置与 Prompt
from app.core.config import settings
from app.core.prompts import DATA_SUMMARY_PROMPT
from app.core.logger import logger

# 🔥 核心修改 1: 引入刚才确认过的 Executor
from app.modules.sql.executor import execute_select_async

# 🔥 核心修改 2: 引入 Graph (这里假设 agent_graph.py 在 app/graphs/ 目录下)
# 如果你的路径不同，请修改这个 import。例如可能是 from app.agent_graph ...
try:
    from app.core.agent_graph import app as master_app
except ImportError:
    # 兼容另一种常见路径
    from app.core.agent_graph import app as master_app


class AgentService:
    def __init__(self):
        # 初始化分析师 LLM (用于最后的数据总结)
        self.summary_llm = ChatOpenAI(
            model=settings.LLM_MODEL,
            temperature=0.7,
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            max_tokens=1024
        )

    def _validate_sql_safe(self, sql: str) -> bool:
        """
        使用 AST 解析验证 SQL 安全性：
        1. 必须能被解析
        2. 只能包含一条语句
        3. 根节点必须是 SELECT 或 UNION
        """
        try:
            # 指定 dialect="mysql"
            parsed_statements = sqlglot.parse(sql, read="mysql")

            if not parsed_statements or len(parsed_statements) > 1:
                logger.warning(f"🛑 [Security] Multi-statement or empty SQL detected: {sql[:50]}...")
                return False

            statement = parsed_statements[0]

            # 白名单检查：只允许 SELECT 或 UNION
            if not isinstance(statement, (exp.Select, exp.Union)):
                logger.warning(f"🛑 [Security] Forbidden SQL type detected ({type(statement)}): {sql[:50]}...")
                return False

            return True

        except Exception as e:
            logger.error(f"🛑 [Security] SQL Parsing failed: {str(e)}")
            return False

    async def process_query(self, query: str, user_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        处理 Agent 查询的核心业务逻辑
        """
        trace_id = str(uuid.uuid4())
        thread_id = session_id or str(uuid.uuid4())

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
            # LangGraph 的配置
            config = {"configurable": {"thread_id": thread_id}}
            logger.info(f"🚀 [Agent] Starting graph execution for: {query}", extra={"trace_id": trace_id})

            # 🔥 核心修改 3: 直接调用 master_app，不再通过 mg.master_app
            final_state = await master_app.ainvoke(
                {"question": query, "trace_id": trace_id},
                config=config
            )

            # 解析图执行结果
            final_answer = final_state.get("final_answer", "")
            # 注意：LangGraph 的 history 有时是 messages 列表，这里简单处理
            steps = final_state.get("history", [])
            intent = final_state.get("intent", "UNKNOWN")

            final_result["steps"] = steps if isinstance(steps, list) else []
            final_result["intent"] = intent

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

                # 2. AST 安全检查
                if not self._validate_sql_safe(sql):
                    error_msg = "Security Alert: SQL validation failed (Only SELECT allowed)."
                    logger.error(f"🛑 {error_msg} SQL: {sql}")
                    final_result["error"] = error_msg
                    return final_result

                # 3. 执行 SQL
                try:
                    # 调用刚才确认过的 executor
                    db_res = await execute_select_async(user_id, sql, trace_id=trace_id)
                except Exception as e:
                    logger.error(f"Execution Failed: {e}")
                    final_result["error"] = f"Database Error: {str(e)}"
                    return final_result

                # 4. 处理结果
                raw_data = db_res.get("data", [])
                error_msg = db_res.get("error")

                if error_msg:
                    final_result["error"] = error_msg
                    final_result["message"] = f"查询执行出错: {error_msg}"
                    return final_result

                # 5. 展示层截断逻辑 (给 LLM 看的数据不能太多)
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

                # 6. 生成总结 (Analyst)
                process_summary = "执行过程正常"  # 简化日志

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
            # 分支 B: 纯文本任务
            # =================================================
            else:
                clean_reply = final_answer.replace("SQL_RESULT:", "").strip() if final_answer else ""
                final_result["success"] = True
                final_result["message"] = clean_reply
                final_result["data"] = []

                logger.info(f"💬 [Text Reply] {clean_reply[:100]}...", extra={"trace_id": trace_id})

                return final_result

        except Exception as e:
            logger.error("Agent Service Internal Error", extra={"trace_id": trace_id}, exc_info=True)
            final_result["success"] = False
            final_result["error"] = str(e)
            return final_result