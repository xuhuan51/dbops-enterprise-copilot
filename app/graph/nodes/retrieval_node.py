from typing import Dict, Any, List
from collections import defaultdict

from app.core.state import AgentState
from app.modules.retrieval.orchestrator import orchestrator
from app.core.logger import logger


def _format_schema_context(retrieved_columns: List[Dict[str, Any]]) -> str:
    """
    将检索到的零散列信息，格式化为 LLM 易读的 Schema 字符串。

    格式示例:
    Table: schools
      - school_name (Description: Name of the school)
      - zip_code (Description: Zip code of the school location)

    Table: frpm
      - ...
    """
    if not retrieved_columns:
        return "No relevant schema found."

    # 1. 按表分组
    tables = defaultdict(list)
    for col in retrieved_columns:
        t_name = col.get("table")
        c_name = col.get("column")
        desc = col.get("desc", "No description")
        source = col.get("source", "unknown")  # 可选：把来源也打出来方便调试

        # 格式：column_name (Description...)
        col_str = f"- {c_name} (Description: {desc})"
        tables[t_name].append(col_str)

    # 2. 拼接字符串
    lines = []
    for t_name, col_strs in tables.items():
        lines.append(f"Table: {t_name}")
        lines.extend(col_strs)
        lines.append("")  # 空行分隔

    return "\n".join(lines).strip()


async def retrieval_node(state: AgentState) -> Dict[str, Any]:
    """
    Retrieval Node:
    连接 AgentState 和 RAGOrchestrator 的桥梁。
    """
    trace_id = state.get("trace_id", "N/A")
    question = state.get("question", "")
    intent_data = state.get("intent_data")

    logger.info(f"🚀 [Retrieval Node] Start processing trace_id={trace_id}")

    # 1. 检查是否需要检索
    # (虽然图路由通常已经保证了这点，但双重检查更安全)
    if intent_data and not intent_data.needs_schema:
        logger.info("ℹ️ [Retrieval Node] Skipped (needs_schema=False).")
        return {
            "retrieved_tables": [],
            "retrieved_columns": [],
            "schema_str": "",
            "join_paths": [],
            "business_rules": []
        }

    try:
        # 2. 调用 Orchestrator (核心分治检索逻辑在这里执行)
        # 注意：这里直接传 state，因为我们在 Orchestrator 里改成了接收 state
        context = await orchestrator.get_retrieval_context(state)

        # 3. 提取结果
        retrieved_cols = context.get("retrieved_columns", [])
        join_paths = context.get("join_paths", [])
        rules = context.get("business_rules", [])
        retrieved_tables = context.get("retrieved_tables", [])

        # 4. 格式化 Schema 字符串 (关键步骤！)
        # Generator 写 SQL 时，是看不到原始 list 的，只能看到这个 string
        schema_str = _format_schema_context(retrieved_cols)

        logger.info(f"✅ [Retrieval Node] Done. Found {len(retrieved_tables)} tables, {len(retrieved_cols)} cols.")

        # 5. 更新 State
        return {
            "retrieved_tables": retrieved_tables,  # 表名列表
            "retrieved_columns": retrieved_cols,  # 详细列信息(List[Dict])
            "schema_str": schema_str,  # 给 LLM 看的格式化文本
            "join_paths": join_paths,  # 图谱路径
            "business_rules": rules  # 知识库规则
        }

    except Exception as e:
        logger.error(f"❌ [Retrieval Node] Failed: {e}", exc_info=True)
        # 降级策略：返回空，让 Generator 尝试根据常识或报错
        return {
            "error_message": f"Retrieval failed: {str(e)}",
            "schema_str": "Error during retrieval."
        }