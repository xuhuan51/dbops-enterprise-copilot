"""
═══════════════════════════════════════════════════════════════════════════════
📦 模块名称: retrieval_node.py (v2 - 适配值匹配后移)
📝 改动说明: value_mappings 不再从这里产出，留给 column_selector_node
═══════════════════════════════════════════════════════════════════════════════
"""

from typing import Dict, Any, List

from app.core.state import AgentState
from app.modules.retrieval.orchestrator import orchestrator
from app.core.logger import logger


def _format_schema_context_from_dict(retrieved_schema: Dict[str, Any]) -> str:
    """将 retrieved_schema (字典) 格式化为 String"""
    if not retrieved_schema:
        return "No relevant schema found."

    lines = []
    for table_name, table_data in retrieved_schema.items():
        lines.append(f"Table: {table_name}")

        columns = table_data.get("columns", [])
        for col in columns:
            c_name = col.get("column_name")
            desc = col.get("ai_description", "No description")

            samples = col.get("sample_values", [])
            sample_str = ""
            if samples:
                formatted_vals = ", ".join([str(x) for x in samples[:10]])
                sample_str = f" | Values: {formatted_vals}"

            line = f"- {c_name} (Description: {desc}){sample_str}"
            lines.append(line)

        lines.append("")

    return "\n".join(lines).strip()


def _log_retrieval_details(retrieved_schema: Dict[str, Any], rules: List[Any]):
    """
    打印检索结果日志
    🔥 改动: 不再打印 value_mappings（因为此时还没做值匹配）
    """
    log_msg = ["\n" + "=" * 60]

    total_tables = len(retrieved_schema)
    total_cols = sum(len(t.get("columns", [])) for t in retrieved_schema.values())

    log_msg.append(f"🔍 [Retrieval Result] Found {total_cols} columns from {total_tables} tables")
    log_msg.append("=" * 60)

    if not retrieved_schema:
        log_msg.append("   ⚠️ (No relevant schema found!)")
    else:
        log_msg.append("📂 [Retrieved Schema]:")
        for i, (table_name, data) in enumerate(retrieved_schema.items()):
            columns = data.get("columns", [])
            col_names = [c["column_name"] for c in columns]

            col_str = ", ".join(col_names)
            display_cols = f"{col_str[:80]}..." if len(col_str) > 80 else col_str

            log_msg.append(f"   {i + 1}. 📦 Table: {table_name}")
            log_msg.append(f"      └── Columns: {display_cols}")

    # 业务规则
    log_msg.append("-" * 60)
    if not rules:
        log_msg.append("📜 [Business Rules]: None")
    else:
        log_msg.append(f"📜 [Business Rules] ({len(rules)} rules):")
        for r in rules:
            content = r.get("content") or r.get("rule_text")
            log_msg.append(f"   • {content}")

    # 🔥 新增: 提示值匹配将在选列之后进行
    log_msg.append("-" * 60)
    log_msg.append("💡 [Value Mappings]: Deferred to column_selector_node (LCS matching)")

    log_msg.append("=" * 60 + "\n")
    logger.info("\n".join(log_msg))


async def retrieval_node(state: AgentState) -> Dict[str, Any]:
    """
    Retrieval Node v2: 只做 Schema + Knowledge 检索，不做值匹配
    """
    trace_id = state.get("trace_id", "N/A")
    intent_data = state.get("intent_data")

    logger.info(f"🚀 [Retrieval Node] Start processing trace_id={trace_id}")

    # 1. 检查是否需要检索
    if intent_data and not getattr(intent_data, "needs_schema", True):
        logger.info("ℹ️ [Retrieval Node] Skipped (needs_schema=False).")
        return {
            "retrieved_schema": {},
            "schema_str": "",
            "business_rules": [],
            "value_mappings": [],
        }

    try:
        # 2. 调用 Orchestrator
        context = await orchestrator.get_retrieval_context(state)

        # 3. 提取结果
        retrieved_schema = context.get("retrieved_schema", {})
        rules = context.get("business_rules", [])
        join_paths = context.get("join_paths", [])

        # 4. Schema String
        schema_str = _format_schema_context_from_dict(retrieved_schema)

        # 5. 日志（🔥 不再传 value_mappings）
        _log_retrieval_details(retrieved_schema, rules)

        # 6. 返回 State
        return {
            "retrieved_schema": retrieved_schema,
            "schema_str": schema_str,
            "join_paths": join_paths,
            "business_rules": rules,
            "value_mappings": [],  # 🔥 留空，由 column_selector_node 填充
        }

    except Exception as e:
        logger.error(f"❌ [Retrieval Node] Failed: {e}", exc_info=True)
        return {
            "error_message": f"Retrieval failed: {str(e)}",
            "retrieved_schema": {},
            "schema_str": "Error during retrieval.",
        }