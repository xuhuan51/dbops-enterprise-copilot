# app/graph/nodes/verification_node.py

import json
import logging
from typing import Dict, Any, List
from langchain_core.messages import HumanMessage, SystemMessage
from app.core.state import AgentState
from app.core.llm import get_llm
# 注意：你需要把 SQL_REFLECTION_PROMPT 的定义更新（见下文）
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
    🔥 核心修复：兼容 selected_schema (Dict) 和 retrieved_columns (List)
    """
    schema_lines = []

    # A. 优先尝试从 selected_schema (Dict) 构建
    selected_schema = state.get("selected_schema")
    if selected_schema:
        for table_name, table_data in selected_schema.items():
            cols = table_data.get("columns", [])
            for col in cols:
                # 提取信息
                c_name = col.get("column_name") or col.get("name")
                d_type = col.get("data_type") or col.get("column_type", "UNKNOWN")

                # 提取样本 (兼容多种 key)
                samples = []
                meta = col.get("metadata", col)  # 可能是嵌套的

                if "sample_values" in meta:
                    samples = meta["sample_values"]
                elif "samples" in meta:
                    samples = meta["samples"]
                elif "sample_values" in col:
                    samples = col["sample_values"]

                # 样本格式化
                sample_str = ""
                if samples:
                    # 简单清洗
                    clean_samples = [str(s).strip() for s in samples if s is not None][:5]
                    if clean_samples:
                        sample_str = f" [Samples: {', '.join(clean_samples)}]"

                # 格式: - table.column (type) [Samples: ...]
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


async def verification_node(state: AgentState) -> Dict[str, Any]:
    """
    自省节点 (适配 MySQL & Dict Schema)
    """
    question = state.get("question", "")
    generated_sql = state.get("generated_sql", "")
    current_count = int(state.get("retry_count", 0))
    attempt_number = current_count + 1

    # ---------------------------------------------------------
    # 1) 构造 Schema 上下文 (🔥 已修复)
    # ---------------------------------------------------------
    schema_context = _format_schema_context(state)

    # ---------------------------------------------------------
    # 2) Business Rules (业务规则)
    # ---------------------------------------------------------
    business_rules = state.get("business_rules", [])
    rules_list: List[str] = []
    if business_rules:
        for r in business_rules:
            txt = r.get("content") if isinstance(r, dict) else str(r)
            if txt and txt.strip():
                rules_list.append(f"- {txt.strip()}")
    rules_str = "\n".join(rules_list) if rules_list else "No additional business rules."

    # ---------------------------------------------------------
    # 3) 准备历史记录
    # ---------------------------------------------------------
    history = state.get("feedback_history", []) or []
    history_block = _build_history_block(history, last_k=3)

    # ---------------------------------------------------------
    # 4) 调用 LLM 审查
    # ---------------------------------------------------------
    if not generated_sql or not generated_sql.strip():
        logger.warning(f"⚠️ [Verifier] Empty SQL detected")
        return {
            "verified": False,
            "feedback": "No SQL generated.",
            "retry_count": current_count + 1,
            "feedback_history": [f"[Attempt {attempt_number}] FAIL: Empty SQL"]
        }

    llm = get_llm()

    # 使用新的 Prompt
    prompt = SQL_REFLECTION_PROMPT.format(
        question=question,
        sql=generated_sql,
        schema_context=schema_context,
        business_rules=rules_str,
        history_context=history_block
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
                # Pass 时不增加 retry_count
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