"""
═══════════════════════════════════════════════════════════════════════════════
📦 模块名称: generate_node.py (v2 - 适配 value_linker)
📝 改动说明:
   1. 读取 state["value_mappings"]（不再读 value_matches）
   2. 将值映射格式化为明确的 SQL 提示，告诉 LLM 用数据库实际值
   3. 塞进 GEN_SQL_PROMPT 和 SQL_REPAIR_PROMPT
═══════════════════════════════════════════════════════════════════════════════
"""

import re
import json
from typing import Dict, Any, List
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from app.core.config import settings
from app.core.llm import get_llm
from app.core.state import AgentState
from app.core.logger import logger
from app.core.prompts import GEN_SQL_PROMPT, SQL_REPAIR_PROMPT


# ==============================================================================
# 🛠️ 辅助函数
# ==============================================================================

def _build_rich_schema_json(retrieved_columns: List[Dict]) -> str:
    """将检索到的列信息组装成精炼的 JSON 格式"""
    if not retrieved_columns:
        return "No schema information found."

    tables_structure = {}

    for col in retrieved_columns:
        table_name = col.get("table") or col.get("table_name")
        col_name = col.get("column") or col.get("column_name")

        meta = col.get("metadata", col)

        col_info = {
            "type": meta.get("data_type") or meta.get("column_type", "UNKNOWN"),
            "comment": meta.get("column_comment") or col.get("desc", ""),
        }

        biz_meaning = meta.get("business_meaning")
        ai_desc = meta.get("ai_description")
        if biz_meaning:
            col_info["meaning"] = biz_meaning
        elif ai_desc:
            col_info["description"] = ai_desc

        dist = meta.get("value_distribution")
        if dist:
            top_dist = dict(sorted(dist.items(), key=lambda x: x[1], reverse=True)[:5])
            col_info["distribution_top5"] = top_dist

        numeric = meta.get("numeric_stats")
        if numeric:
            col_info["stats"] = numeric

        samples = meta.get("sample_values") or col.get("sample_values")
        if samples and not dist:
            col_info["samples"] = samples[:3]

        if table_name not in tables_structure:
            tables_structure[table_name] = []

        tables_structure[table_name].append({col_name: col_info})

    return json.dumps(tables_structure, ensure_ascii=False, indent=2)


def _format_history(history: List[Any]) -> str:
    if not history:
        return "无 (这是第一轮对话)"
    context_lines = []
    for msg in history[-6:]:
        if isinstance(msg, HumanMessage):
            context_lines.append(f"User: {msg.content}")
        elif isinstance(msg, AIMessage):
            content = str(msg.content)[:200].replace("\n", " ")
            context_lines.append(f"AI: {content}...")
    return "\n".join(context_lines)


# ==============================================================================
# 🔥 新增: 值映射格式化函数
# ==============================================================================

def _format_value_mappings_for_sql(value_mappings: List[Dict]) -> str:
    """
    将 value_mappings 格式化为 SQL 生成器能直接使用的提示

    支持两种操作符：
    - "=" : 精确匹配，直接用数据库值
    - "LIKE" : 模糊匹配，用 LIKE 模式

    输出示例:
    ⚠️ IMPORTANT: Use the exact database values below in your WHERE clauses:
    - User said "北京" → USE: WHERE `user_addresses`.`province` = '北京市'
    - User said "小米 14 PRO", multiple DB matches found:
      ['小米14 Pro 旗舰版', '小米14 Pro版', '小米14 Pro 新款']
      → USE: WHERE `order_items`.`product_name` LIKE '%小米14 Pro%'
    """
    if not value_mappings:
        return "No value mappings. Use values as mentioned in the question."

    lines = [
        "⚠️ IMPORTANT: The user's wording may differ from actual database values.",
        "Use the EXACT database values or patterns below in your WHERE clauses:\n"
    ]

    for m in value_mappings:
        user_input = m.get("user_input", "")
        db_value = m.get("db_value", "")
        table = m.get("table", "")
        column = m.get("column", "")
        operator = m.get("suggest_operator", "=")
        all_values = m.get("all_db_values", [])

        if operator == "LIKE":
            # 模糊匹配模式
            lines.append(
                f'- User said "{user_input}", multiple DB matches found in `{table}`.`{column}`:'
            )
            if all_values:
                lines.append(f'  Matched values: {all_values}')
            lines.append(
                f"  → USE: WHERE `{table}`.`{column}` LIKE '{db_value}'"
            )
        else:
            # 精确匹配模式
            if user_input == db_value:
                lines.append(
                    f'- "{user_input}" → `{table}`.`{column}` = \'{db_value}\''
                )
            else:
                lines.append(
                    f'- User said "{user_input}", but actual DB value in `{table}`.`{column}` is \'{db_value}\''
                )
                lines.append(
                    f'  → USE: WHERE `{table}`.`{column}` = \'{db_value}\''
                )

    return "\n".join(lines)


# ==============================================================================
# 🚀 核心生成节点
# ==============================================================================

async def generate_node(state: AgentState) -> Dict[str, Any]:
    """
    Generator Node v2
    - 适配 value_linker 的输出
    - 🔥 支持动态 Schema 切换：首次生成用精简列，修复报错用检索全列
    """
    question = state.get("question", "")
    history = state.get("history", [])

    # 状态标志
    execution_error = state.get("execution_error")
    generated_sql = state.get("generated_sql")
    feedback = state.get("feedback", "")
    verified = state.get("verified", True)
    retry_count = state.get("retry_count", 0)

    # ============================================================
    # 🔥 1. 动态 Schema 切换 (核心改动)
    # ============================================================
    # 判断当前是否处于修复/重试模式
    current_error = None
    if execution_error:
        current_error = f"Execution Error: {execution_error}"
    elif not verified and feedback:
        current_error = f"Feedback: {feedback}"

    is_repairing = bool(current_error and generated_sql)

    # 获取两个层级的 Schema (注意：这里统一使用 state.py 中的命名)
    selected_schema = state.get("selected_schema", {})
    retrieved_schema = state.get("retrieved_schema", {})

    rich_schema_structure = {}

    # 根据状态决定用哪个 Schema
    if is_repairing and retrieved_schema:
        logger.info(
            f"🔧 [Generator] 修复模式 (Attempt {retry_count}): 启用完整的 retrieved_schema ({len(retrieved_schema)}张表) 以防字段丢失...")
        schema_to_use = retrieved_schema
    elif selected_schema:
        logger.info(f"🎨 [Generator] 首次生成: 使用精简的 selected_schema ({len(selected_schema)}张表)...")
        schema_to_use = selected_schema
    else:
        logger.warning(f"⚠️ [Generator] 缺省状态: 退回使用 retrieved_schema...")
        schema_to_use = retrieved_schema

    # 统一格式化为 Rich JSON
    for table_name, table_data in schema_to_use.items():
        rich_schema_structure[table_name] = []
        for col in table_data.get("columns", []):
            col_name = col.get("column_name") or col.get("name")
            meta = col.get("metadata", col)

            col_info = {
                "type": meta.get("data_type") or meta.get("column_type", "UNKNOWN"),
                "comment": meta.get("column_comment") or col.get("desc", ""),
            }

            biz = meta.get("business_meaning")
            ai_desc = meta.get("ai_description")
            if biz:
                col_info["meaning"] = biz
            elif ai_desc:
                col_info["description"] = ai_desc

            dist = meta.get("value_distribution")
            if dist:
                col_info["distribution_top5"] = dict(
                    sorted(dist.items(), key=lambda x: x[1], reverse=True)[:5]
                )

            rich_schema_structure[table_name].append({col_name: col_info})

    rich_schema_json = json.dumps(rich_schema_structure, ensure_ascii=False, indent=2)

    # ============================================================
    # 2. 值映射处理 (保持不变)
    # ============================================================
    value_mappings = state.get("value_mappings", [])
    constraints_str = _format_value_mappings_for_sql(value_mappings)

    # ============================================================
    # 3. 其他上下文处理 (保持不变)
    # ============================================================
    business_rules = state.get("business_rules", [])
    few_shot_examples = state.get("few_shot_examples", [])
    join_paths = state.get("join_paths", [])

    history_context = _format_history(history) if history else ""

    general_rules = []
    if business_rules:
        for r in business_rules:
            txt = r.get("content") if isinstance(r, dict) else str(r)
            if txt.strip():
                general_rules.append(f"- {txt.strip()}")
    rules_str = "\n".join(general_rules) if general_rules else "No specific business rules."

    paths_str = "\n".join([str(p) for p in join_paths[:3]]) if join_paths else "Auto-detect based on schema FKs."

    few_shot_str = "No examples."
    if few_shot_examples:
        ex_lines = [
            f"Example {i + 1}:\nQ: {ex.get('question')}\nSQL: {ex.get('sql')}"
            for i, ex in enumerate(few_shot_examples)
        ]
        few_shot_str = "\n\n".join(ex_lines)

    # ============================================================
    # 4. Prompt 构造
    # ============================================================
    llm = get_llm(model_name=settings.LLM_MODEL)
    final_prompt = ""

    if is_repairing:
        # 这里注入的是扩展后的 rich_schema_json
        final_prompt = SQL_REPAIR_PROMPT.format(
            question=question,
            schema_context=rich_schema_json,
            rules_context=rules_str,
            previous_sql=generated_sql,
            error_msg=current_error
        )
    else:
        # 这里注入的是精简的 rich_schema_json
        final_prompt = GEN_SQL_PROMPT.format(
            history_context=history_context,
            schema_context=rich_schema_json,
            constraints_context=constraints_str,
            rules_context=rules_str,
            join_paths_context=paths_str,
            few_shot_context=few_shot_str,
            question=question
        )


    # ============================================================
    # LLM 调用
    # ============================================================
    try:
        messages = [
            SystemMessage(
                content="You are a MySQL expert. The schema is provided in JSON format with rich semantic info."
            ),
            HumanMessage(content=final_prompt),
        ]

        response = await llm.ainvoke(messages)
        content = response.content

        sql = ""
        thought = ""

        sql_match = re.search(r"```sql\n(.*?)\n```", content, re.DOTALL)
        if sql_match:
            sql = sql_match.group(1).strip()
        else:
            clean = content.replace("```sql", "").replace("```", "").strip()
            if "SELECT" in clean.upper():
                sql = clean

        audit_match = re.search(r"```audit\n(.*?)\n```", content, re.DOTALL)
        if audit_match:
            thought = audit_match.group(1).strip()

        if sql and not sql.endswith(";"):
            sql += ";"

        logger.info(f"📝 [Generator] SQL Generated")

        return {
            "generated_sql": sql,
            "generated_thought": thought,
            "final_answer": sql,
            "execution_error": None,
            "is_executable": None,
        }

    except Exception as e:
        logger.error(f"❌ Generation Failed: {e}")
        return {"generated_sql": "", "error_message": str(e)}