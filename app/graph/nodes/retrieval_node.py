from typing import Dict, Any, List
from collections import defaultdict

from app.core.state import AgentState
from app.modules.retrieval.orchestrator import orchestrator
from app.core.logger import logger


def _format_schema_context(retrieved_columns: List[Dict[str, Any]]) -> str:
    """
    将检索到的零散列信息，格式化为 LLM 易读的 Schema 字符串。
    (已修复：增加样本值 Sample Values 的显示)
    """
    if not retrieved_columns:
        return "No relevant schema found."

    # 1. 按表分组
    tables = defaultdict(list)
    for col in retrieved_columns:
        t_name = col.get("table")
        c_name = col.get("column")
        desc = col.get("desc", "No description")

        # 尝试获取样本值，优先取 'sample_values'，没有则取 'samples'
        samples = col.get("sample_values") or col.get("samples", [])

        sample_str = ""
        if samples:
            # 限制一下数量（只显示前 15 个给 LLM），防止 Token 爆炸
            formatted_vals = ", ".join([repr(x) for x in samples[:15]])
            sample_str = f" | Values: {formatted_vals}"

        # 格式：column_name (Description...) | Values: ...
        col_str = f"- {c_name} (Description: {desc}){sample_str}"
        tables[t_name].append(col_str)

    # 2. 拼接字符串
    lines = []
    for t_name, col_strs in tables.items():
        lines.append(f"Table: {t_name}")
        lines.extend(col_strs)
        lines.append("")  # 空行分隔

    return "\n".join(lines).strip()


# 🔥🔥🔥 修改点 1：增加 rules 参数 🔥🔥🔥
def _log_retrieval_details(cols: List[Dict], matches: List[str], schema_len: int, rules: List[Any] = None):
    """
    专门负责打印漂亮的调试日志 (高颜值版 ✨ + 知识库)
    """
    # 1. 标题
    log_msg = ["\n" + "=" * 60]
    log_msg.append(f"🔍 [Retrieval Debug Info] (Found {len(cols)} columns)")
    log_msg.append("=" * 60)

    # 2. 打印列信息 (带缩进和空行)
    if not cols:
        log_msg.append("   (No columns retrieved)")
    else:
        for i, col in enumerate(cols):
            # 同样保留全量打印
            if i >= 100:
                log_msg.append(f"\n   ... (and {len(cols) - 100} more columns truncated)")
                break

            t_name = col.get('table')
            c_name = col.get('column')

            # 获取样本
            full_samples = col.get("sample_values") or col.get("samples", [])

            # 截取前 5 个用于展示
            samples_preview = full_samples[:5]
            count_info = f"(Total: {len(full_samples)})" if len(full_samples) > 5 else ""

            # 🎨 格式化
            log_msg.append(f"\n   {i + 1:02d}. 🏷️  {t_name}.{c_name}")
            log_msg.append(f"       └── 🧪 Samples: {samples_preview} {count_info}")

    # 3. 打印 Schema 长度
    log_msg.append("\n" + "-" * 60)
    log_msg.append(f"📝 Schema Context Length: {schema_len} chars (Ready for LLM)")

    # 4. 打印值匹配
    log_msg.append("-" * 60)
    log_msg.append(f"🎯 Value Matches ({len(matches)}):")

    if matches:
        for v in matches:
            # 🔥🔥🔥 修复点：检查对象是否有 format_constraint 方法 🔥🔥🔥
            if hasattr(v, "format_constraint"):
                # 如果是 MatchCandidate 对象，调用它的格式化方法
                log_msg.append(f"   ✨ {v.format_constraint()}")
            else:
                # 如果是普通字符串 (兼容旧逻辑)，直接打印
                log_msg.append(f"   ✨ {str(v)}")
    else:
        log_msg.append("   (No specific value constraints found)")

    # 🔥🔥🔥 修改点 2：新增打印知识库规则 🔥🔥🔥
    log_msg.append("-" * 60)
    rules = rules or []
    log_msg.append(f"📚 Knowledge Rules ({len(rules)}):")
    if rules:
        for r in rules:
            # 兼容字典或字符串格式 (防止结构不同报错)
            if isinstance(r, dict):
                # 优先取 content，没有取 rule_text，再没有转 string
                content = r.get('content') or r.get('rule_text') or str(r)
            else:
                content = str(r)

            # 如果内容太长，截断一下方便查看
            preview = content[:100] + "..." if len(content) > 100 else content
            log_msg.append(f"   💡 {preview}")
    else:
        log_msg.append("   (No external rules found)")

    log_msg.append("=" * 60 + "\n")

    # 一次性输出
    print("\n".join(log_msg))


async def retrieval_node(state: AgentState) -> Dict[str, Any]:
    """
    Retrieval Node: 连接 AgentState 和 RAGOrchestrator 的桥梁。
    """
    trace_id = state.get("trace_id", "N/A")
    intent_data = state.get("intent_data")

    logger.info(f"🚀 [Retrieval Node] Start processing trace_id={trace_id}")

    # 1. 检查是否需要检索
    if intent_data and not intent_data.needs_schema:
        logger.info("ℹ️ [Retrieval Node] Skipped (needs_schema=False).")
        return {
            "retrieved_tables": [],
            "retrieved_columns": [],
            "schema_str": "",
            "join_paths": [],
            "business_rules": [],
            "value_matches": []
        }

    try:
        # 2. 调用 Orchestrator
        context = await orchestrator.get_retrieval_context(state)

        # 3. 提取结果
        retrieved_cols = context.get("retrieved_columns", [])
        join_paths = context.get("join_paths", [])
        rules = context.get("business_rules", [])
        retrieved_tables = context.get("retrieved_tables", [])
        value_matches = context.get("value_matches", [])

        # 4. 准备 Schema String
        schema_str = context.get("schema_str")
        if not schema_str:
            schema_str = _format_schema_context(retrieved_cols)

        # 🔥🔥🔥 修改点 3：把 rules 传给日志函数 🔥🔥🔥
        _log_retrieval_details(retrieved_cols, value_matches, len(schema_str), rules)

        logger.info(f"✅ [Retrieval Node] Done. Found {len(retrieved_tables)} tables.")

        # 5. 更新 State
        return {
            "retrieved_tables": retrieved_tables,
            "retrieved_columns": retrieved_cols,
            "schema_str": schema_str,
            "join_paths": join_paths,
            "business_rules": rules,
            "value_matches": value_matches
        }

    except Exception as e:
        logger.error(f"❌ [Retrieval Node] Failed: {e}", exc_info=True)
        return {
            "error_message": f"Retrieval failed: {str(e)}",
            "schema_str": "Error during retrieval.",
            "value_matches": []
        }