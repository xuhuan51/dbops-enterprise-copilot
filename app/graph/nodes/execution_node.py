import logging
from typing import Dict, Any

from app.core.state import AgentState
from app.modules.sql.executor import execute_select_async

logger = logging.getLogger(__name__)

MAX_EXECUTION_RETRIES = 1


async def execution_node(state: AgentState) -> Dict[str, Any]:
    """
    执行节点 (Async MySQL + Guardrail 版)
    """
    logger.info("🚀 [Execution Node] Start...")

    # 获取 SQL
    sql = state.get("generated_sql", "").strip().rstrip(';')
    current_retries = state.get("execution_retries", 0)

    # 这里的 user_id 假设在 config 或 state 里，如果没有就给个默认值
    user_id = state.get("user_id", "system_user")
    trace_id = state.get("trace_id", "no-trace")

    # -------------------------------------------------------
    # 1. 调用异步工具执行 (自带连接池 + 风控)
    # -------------------------------------------------------
    # execute_select_async 内部已经处理了 try-catch 并返回 dict
    result_package = await execute_select_async(user_id, sql, trace_id)

    data = result_package.get("data")  # List[Dict]
    error = result_package.get("error")  # str | None

    # -------------------------------------------------------
    # 2. 成功路径
    # -------------------------------------------------------
    if not error:
        row_count = len(data) if data else 0
        logger.info(f"✅ [Execution] Success. Rows: {row_count}")

        preview = str(data)[:200] + "..." if row_count > 0 else "[]"
        logger.info(f"📄 Result Preview: {preview}")

        return {
            "execution_result": data,
            "execution_error": None,
            "is_executable": True,
        }

    # -------------------------------------------------------
    # 3. 失败路径 (Guardrail 拦截 或 SQL 报错)
    # -------------------------------------------------------
    next_retries = current_retries + 1

    if next_retries > MAX_EXECUTION_RETRIES:
        logger.error(f"🛑 [Execution] Max retries reached. Error: {error}")
    else:
        logger.warning(f"🔄 [Execution] Failed: {error}. Retrying...")

    return {
        "execution_result": None,
        "execution_error": error,
        "is_executable": False,
        "execution_retries": next_retries
    }