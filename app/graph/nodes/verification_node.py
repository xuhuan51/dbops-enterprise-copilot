"""
═══════════════════════════════════════════════════════════════════════════════
📦 模块名称: verification_node.py (v2 - 适配 value_linker)
📝 改动说明:
   1. 读取 state["value_mappings"]，格式化后传给 verifier prompt
   2. 审查标准新增：值映射过的 WHERE 条件不算错
═══════════════════════════════════════════════════════════════════════════════
"""

import json
import logging
from typing import Dict, Any, List
from langchain_core.messages import HumanMessage, SystemMessage
from app.core.state import AgentState
from app.core.llm import get_llm
from app.core.prompts import SQL_REFLECTION_PROMPT

logger = logging.getLogger(__name__)


def _truncate(s: str, n: int = 600) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _build_history_block(history: List[str], last_k: int = 3) -> str:
    if not history:
        return "None (First Attempt)"
    tail = history[-last_k:]
    tail = [_truncate(x, 500) for x in tail]
    return "\n".join([f"- {x}" for x in tail])


def _format_schema_context(state: AgentState) -> str:
    """
    兼容 selected_schema (Dict) 和 retrieved_columns (List)
    """
    schema_lines = []

    # A. 优先尝试从 selected_schema (Dict) 构建
    selected_schema = state.get("selected_schema")
    if selected_schema:
        for table_name, table_data in selected_schema.items():
            cols = table_data.get("columns", [])
            for col in cols:
                c_name = col.get("column_name") or col.get("name")
                d_type = col.get("data_type") or col.get("column_type", "UNKNOWN")

                samples = []
                meta = col.get("metadata", col)

                if "sample_values" in meta:
                    samples = meta["sample_values"]
                elif "samples" in meta:
                    samples = meta["samples"]
                elif "sample_values" in col:
                    samples = col["sample_values"]

                sample_str = ""
                if samples:
                    clean_samples = [str(s).strip() for s in samples if s is not None][:5]
                    if clean_samples:
                        sample_str = f" [Samples: {', '.join(clean_samples)}]"

                schema_lines.append(f"- {table_name}.{c_name} ({d_type}){sample_str}")

        if schema_lines:
            return "\n".join(schema_lines)

    # B. 兜底：使用 retrieved_columns (List)
    retrieved_columns = state.get("retrieved_columns", [])
    if retrieved_columns:
        for col in retrieved_columns:
            t = col.get("table_name") or col.get("table") or "UnknownTable"
            c = col.get("column_name") or col.get("column") or "UnknownCol"
            dtype = col.get("data_type") or "UNKNOWN"
            samples = col.get("sample_values", []) or []

            sample_str = ""
            if samples:
                clean_samples = [str(s).strip() for s in samples if s is not None][:5]
                if clean_samples:
                    sample_str = f" [Samples: {', '.join(clean_samples)}]"

            schema_lines.append(f"- {t}.{c} ({dtype}){sample_str}")

    if schema_lines:
        return "\n".join(schema_lines)

    return "No schema info available."


# ==============================================================================
# 🔥 新增：格式化值映射供 verifier 使用
# ==============================================================================

def _format_value_mappings_for_verifier(value_mappings: List[Dict]) -> str:
    """
    将 value_mappings 格式化为 verifier 能理解的上下文

    输出示例:
    以下值映射已经过数据库验证，SQL 中使用映射后的值是正确的行为：
    - 用户说 "小米 14 PRO" → SQL 应使用 LIKE '%小米14 Pro%' (order_items.product_name)
      匹配到的数据库值: ['小米14 Pro 旗舰版', '小米14 Pro版', '小米14 Pro 新款']
    - 用户说 "北京" → SQL 应使用 = '北京市' (user_addresses.province)
    """
    if not value_mappings:
        return "None (no value mappings applied)"

    lines = [
        "以下值映射已经过数据库验证，SQL 中使用映射后的值是**正确的行为**，不要因此判 FAIL："
    ]

    for m in value_mappings:
        user_input = m.get("user_input", "")
        db_value = m.get("db_value", "")
        table = m.get("table", "")
        column = m.get("column", "")
        operator = m.get("suggest_operator", "=")
        all_values = m.get("all_db_values", [])

        if operator == "LIKE":
            lines.append(
                f'- 用户说 "{user_input}" → SQL 应使用 LIKE \'{db_value}\' ({table}.{column})'
            )
            if all_values:
                lines.append(f'  匹配到的数据库值: {all_values}')
        else:
            if user_input != db_value:
                lines.append(
                    f'- 用户说 "{user_input}" → SQL 应使用 = \'{db_value}\' ({table}.{column})'
                )
            else:
                lines.append(
                    f'- "{user_input}" → = \'{db_value}\' ({table}.{column})'
                )

    return "\n".join(lines)


# ==============================================================================
# 🚀 验证节点
# ==============================================================================

async def verification_node(state: AgentState) -> Dict[str, Any]:
    """
    自省节点 (适配 MySQL & Dict Schema & value_mappings)
    """
    question = state.get("question", "")
    generated_sql = state.get("generated_sql", "")
    current_count = int(state.get("retry_count", 0))
    attempt_number = current_count + 1

    # 1) Schema 上下文
    schema_context = _format_schema_context(state)

    # 2) Business Rules
    business_rules = state.get("business_rules", [])
    rules_list: List[str] = []
    if business_rules:
        for r in business_rules:
            txt = r.get("content") if isinstance(r, dict) else str(r)
            if txt and txt.strip():
                rules_list.append(f"- {txt.strip()}")
    rules_str = "\n".join(rules_list) if rules_list else "No additional business rules."

    # 3) 历史反馈
    history = state.get("feedback_history", []) or []
    history_block = _build_history_block(history, last_k=3)

    # 🔥 4) 值映射上下文（新增）
    value_mappings = state.get("value_mappings", [])
    value_mappings_context = _format_value_mappings_for_verifier(value_mappings)

    # 5) 空 SQL 检查
    if not generated_sql or not generated_sql.strip():
        logger.warning(f"⚠️ [Verifier] Empty SQL detected")
        return {
            "verified": False,
            "feedback": "No SQL generated.",
            "retry_count": current_count + 1,
            "feedback_history": [f"[Attempt {attempt_number}] FAIL: Empty SQL"]
        }

    # 6) 调用 LLM 审查
    llm = get_llm()

    prompt = SQL_REFLECTION_PROMPT.format(
        question=question,
        sql=generated_sql,
        schema_context=schema_context,
        business_rules=rules_str,
        history_context=history_block,
        value_mappings_context=value_mappings_context,  # 🔥 新增参数
    )

    try:
        messages = [
            SystemMessage(
                content="You are a strict MySQL Code Reviewer. Output raw JSON only."
            ),
            HumanMessage(content=prompt),
        ]

        logger.info(f"🧐 [Verifier] Reviewing SQL (Attempt {attempt_number})...")

        response = await llm.ainvoke(messages)
        content = (response.content or "").strip()

        if content.startswith("```"):
            content = content.replace("```json", "").replace("```", "").strip()

        result = json.loads(content)
        status = str(result.get("status", "FAIL")).upper()
        feedback = result.get("feedback", "No feedback.")

        if status == "PASS":
            logger.info(f"✅ [Verifier] SQL approved!")
            return {
                "verified": True,
                "feedback": "",
                "feedback_history": [f"[Attempt {attempt_number}] STATUS: PASS"],
            }

        logger.warning(f"❌ [Verifier] SQL Rejected: {feedback[:100]}...")
        return {
            "verified": False,
            "feedback": feedback,
            "retry_count": current_count + 1,
            "feedback_history": [f"[Attempt {attempt_number}] FAIL: {feedback}"]
        }

    except Exception as e:
        logger.error(f"⚠️ [Verifier] Exception: {e}")
        return {
            "verified": False,
            "feedback": f"Verifier Error: {str(e)}",
            "retry_count": current_count + 1,
            "feedback_history": [f"[Attempt {attempt_number}] EXCEPTION: {str(e)}"]
        }