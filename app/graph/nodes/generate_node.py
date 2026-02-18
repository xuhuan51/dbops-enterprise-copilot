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

    输入 (来自 column_selector_node / value_linker):
    [
        {"user_input": "北京", "db_value": "北京市", "table": "user_addresses", "column": "province"},
        {"user_input": "华为 Mate 60", "db_value": "华为 Mate 60", "table": "order_items", "column": "product_name"}
    ]

    输出:
    ⚠️ IMPORTANT: Use the exact database values below in your WHERE clauses:
    - User said "北京", but the actual value in `user_addresses`.`province` is '北京市'
      → Use: WHERE `user_addresses`.`province` = '北京市'
    - User said "华为 Mate 60", the actual value in `order_items`.`product_name` is '华为 Mate 60'
      → Use: WHERE `order_items`.`product_name` = '华为 Mate 60'
    """
    if not value_mappings:
        return "No value mappings. Use values as mentioned in the question."

    lines = [
        "⚠️ IMPORTANT: The user's wording may differ from actual database values.",
        "Use the EXACT database values below in your WHERE clauses:\n"
    ]

    for m in value_mappings:
        user_input = m.get("user_input", "")
        db_value = m.get("db_value", "")
        table = m.get("table", "")
        column = m.get("column", "")

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
    - 值映射信息明确注入 prompt
    """
    question = state.get("question", "")
    history = state.get("history", [])

    # 状态标志
    execution_error = state.get("execution_error")
    generated_sql = state.get("generated_sql")
    feedback = state.get("feedback", "")
    verified = state.get("verified", True)

    # ============================================================
    # Schema 处理（不变）
    # ============================================================

    selected_schema = state.get("selected_schema")
    retrieved_columns = state.get("retrieved_columns", [])

    rich_schema_structure = {}

    if selected_schema:
        logger.info(f"🎨 [Generator] Using refined schema (Dict) directly...")

        for table_name, table_data in selected_schema.items():
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
    else:
        logger.warning(f"⚠️ [Generator] No selected schema, falling back to raw list...")
        temp_json = _build_rich_schema_json(retrieved_columns)
        rich_schema_structure = json.loads(temp_json)

    rich_schema_json = json.dumps(rich_schema_structure, ensure_ascii=False, indent=2)

    # ============================================================
    # 🔥 值映射处理（核心改动）
    # ============================================================

    # 读取 value_mappings（来自 column_selector_node → value_linker）
    value_mappings = state.get("value_mappings", [])

    # 格式化为 SQL 生成器能理解的提示
    value_mappings_str = _format_value_mappings_for_sql(value_mappings)

    # ============================================================
    # 其他上下文（保持不变）
    # ============================================================

    business_rules = state.get("business_rules", [])
    few_shot_examples = state.get("few_shot_examples", [])
    join_paths = state.get("join_paths", [])

    history_context = _format_history(history) if history else ""

    # 1. 业务规则
    general_rules = []
    if business_rules:
        for r in business_rules:
            txt = r.get("content") if isinstance(r, dict) else str(r)
            if txt.strip():
                general_rules.append(f"- {txt.strip()}")
    rules_str = "\n".join(general_rules) if general_rules else "No specific business rules."

    # 2. 值映射约束
    # 🔥 改动: 不再用 value_matches，直接用格式化好的 value_mappings_str
    constraints_str = value_mappings_str

    # 3. Join Paths
    paths_str = "\n".join([str(p) for p in join_paths[:3]]) if join_paths else "Auto-detect based on schema FKs."

    # 4. Few Shot
    few_shot_str = "No examples."
    if few_shot_examples:
        ex_lines = [
            f"Example {i + 1}:\nQ: {ex.get('question')}\nSQL: {ex.get('sql')}"
            for i, ex in enumerate(few_shot_examples)
        ]
        few_shot_str = "\n\n".join(ex_lines)

    # ============================================================
    # Prompt 构造
    # ============================================================
    llm = get_llm(model_name=settings.LLM_MODEL)
    final_prompt = ""

    current_error = None
    if execution_error:
        current_error = f"Execution Error: {execution_error}"
    elif not verified and feedback:
        current_error = f"Feedback: {feedback}"

    if current_error and generated_sql:
        logger.info(f"🔧 [Generator] Entering Repair Mode...")
        final_prompt = SQL_REPAIR_PROMPT.format(
            question=question,
            schema_context=rich_schema_json,
            rules_context=rules_str,
            previous_sql=generated_sql,
            error_msg=current_error
        )
    else:
        logger.info(f"🎨 [Generator] Generating from scratch...")
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