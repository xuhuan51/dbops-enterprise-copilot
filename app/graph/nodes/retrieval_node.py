from typing import Dict, Any, List
from collections import defaultdict

from app.core.state import AgentState
from app.modules.retrieval.orchestrator import orchestrator
from app.core.logger import logger


def _format_schema_context_from_dict(retrieved_schema: Dict[str, Any]) -> str:
    """
    将 retrieved_schema (字典) 格式化为 String
    适配 Orchestrator v10 的输出结构
    """
    if not retrieved_schema:
        return "No relevant schema found."

    lines = []
    for table_name, table_data in retrieved_schema.items():
        lines.append(f"Table: {table_name}")

        columns = table_data.get("columns", [])
        for col in columns:
            c_name = col.get("column_name")
            desc = col.get("ai_description", "No description")

            # 样本值
            samples = col.get("sample_values", [])
            sample_str = ""
            if samples:
                # 简单截取前10个
                formatted_vals = ", ".join([str(x) for x in samples[:10]])
                sample_str = f" | Values: {formatted_vals}"

            # 标记 (PK/FK 信息如果 orchestrator 没传，这里暂时留空，或者需要在 orchestrator 补充)
            # 假设 orchestrator v10 的 column 字典里目前主要是基础信息
            # 如果需要 PK/FK，需要在 orchestrator._group_columns_by_table 里透传

            line = f"- {c_name} (Description: {desc}){sample_str}"
            lines.append(line)

        lines.append("")  # 空行

    return "\n".join(lines).strip()


def _flatten_schema_for_logging(retrieved_schema: Dict[str, Any]) -> List[Dict]:
    """
    辅助函数：将字典结构的 schema 拍平为列表，
    以便复用原本漂亮的 _log_retrieval_details 函数
    """
    flat_cols = []
    for table_name, data in retrieved_schema.items():
        for col in data.get("columns", []):
            flat_cols.append({
                "table": table_name,
                "column": col.get("column_name"),
                "sample_values": col.get("sample_values", []),
                # 可以添加其他日志需要的字段
            })
    return flat_cols


def _log_retrieval_details(retrieved_schema: Dict[str, Any], value_mappings: List[Any], rules: List[Any]):
    """
    专门打印检索结果的漂亮日志 (包含表、列、值映射、业务规则)
    """
    log_msg = ["\n" + "=" * 60]

    # 1. 统计信息
    total_tables = len(retrieved_schema)
    total_cols = sum(len(t.get("columns", [])) for t in retrieved_schema.values())

    log_msg.append(f"🔍 [Retrieval Result] Found {total_cols} columns from {total_tables} tables")
    log_msg.append("=" * 60)

    # 2. 打印检索到的表和列 (知识点：Schema)
    if not retrieved_schema:
        log_msg.append("   ⚠️ (No relevant schema found!)")
    else:
        log_msg.append("📂 [Retrieved Schema]:")
        for i, (table_name, data) in enumerate(retrieved_schema.items()):
            columns = data.get("columns", [])
            col_names = [c["column_name"] for c in columns]

            # 格式化列名，防止太长换行
            col_str = ", ".join(col_names)
            display_cols = f"{col_str[:80]}..." if len(col_str) > 80 else col_str

            log_msg.append(f"   {i + 1}. 📦 Table: {table_name}")
            log_msg.append(f"      └── Columns: {display_cols}")

    # 3. 打印值映射 (知识点：Value Mapping)
    log_msg.append("-" * 60)
    if not value_mappings:
        log_msg.append("💡 [Value Mappings]: None")
    else:
        log_msg.append(f"💡 [Value Mappings] ({len(value_mappings)} matches):")
        for m in value_mappings:
            # 兼容不同版本的字段名
            u_input = m.get("keyword") or m.get("user_input")
            db_val = m.get("db_val") or m.get("db_value")
            tbl = m.get("table")
            col = m.get("column")
            log_msg.append(f"   👉 \"{u_input}\" -> \"{db_val}\" (at {tbl}.{col})")

    # 4. 打印业务规则 (知识点：Business Rules)
    log_msg.append("-" * 60)
    if not rules:
        log_msg.append("📜 [Business Rules]: None")
    else:
        log_msg.append(f"📜 [Business Rules] ({len(rules)} rules):")
        for r in rules:
            content = r.get("content") or r.get("rule_text")
            log_msg.append(f"   • {content}")

    log_msg.append("=" * 60 + "\n")

    # 一次性通过 logger 输出
    logger.info("\n".join(log_msg))

async def retrieval_node(state: AgentState) -> Dict[str, Any]:
    """
    Retrieval Node: 适配 Orchestrator v10
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
            "value_mappings": []
        }

    try:
        # 2. 调用 Orchestrator
        context = await orchestrator.get_retrieval_context(state)

        # 3. 提取结果
        retrieved_schema = context.get("retrieved_schema", {})
        value_mappings = context.get("value_mappings", [])
        rules = context.get("business_rules", [])
        join_paths = context.get("join_paths", [])

        # 4. 准备 Schema String
        schema_str = _format_schema_context_from_dict(retrieved_schema)

        # 5. 打印日志 (关键修改点：直接传参，解决 TypeError)
        _log_retrieval_details(retrieved_schema, value_mappings, rules)

        # 6. 返回 State
        return {
            "retrieved_schema": retrieved_schema,
            "schema_str": schema_str,
            "join_paths": join_paths,
            "business_rules": rules,
            "value_mappings": value_mappings
        }

    except Exception as e:
        logger.error(f"❌ [Retrieval Node] Failed: {e}", exc_info=True)
        return {
            "error_message": f"Retrieval failed: {str(e)}",
            "retrieved_schema": {},
            "schema_str": "Error during retrieval."
        }