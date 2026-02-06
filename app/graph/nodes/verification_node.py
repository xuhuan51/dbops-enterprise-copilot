# app/graph/nodes/verification_node.py

import json
import logging
from typing import Dict, Any
from langchain_core.messages import HumanMessage, SystemMessage
from app.core.state import AgentState
from app.core.llm import get_llm
from app.core.prompts import SQL_REFLECTION_PROMPT

logger = logging.getLogger(__name__)


async def verification_node(state: AgentState) -> Dict[str, Any]:
    """
    自省节点 (合并优化版):
    将 Schema 结构和样本数据合并展示，大幅降低认知负荷。
    """
    question = state.get("question", "")
    generated_sql = state.get("generated_sql", "")

    # ---------------------------------------------------------
    # 🔥 核心修改：构造 "Schema + Evidence" 合体版
    # ---------------------------------------------------------
    retrieved_columns = state.get("retrieved_columns", [])
    schema_lines = []

    if retrieved_columns:
        # 按表名分组一下会更好看，不过直接列出也没问题
        # 这里直接列出，简单粗暴有效
        for col in retrieved_columns:
            # 兼容性提取
            if isinstance(col, dict):
                t = col.get("table", "UnknownTable")
                c = col.get("column", "UnknownCol")
                dtype = col.get("column_type", "UNKNOWN")
                samples = col.get("samples", [])
            else:
                t = getattr(col, "table", "UnknownTable")
                c = getattr(col, "column", "UnknownCol")
                dtype = getattr(col, "column_type", "UNKNOWN")
                samples = getattr(col, "samples", [])

            # 构造样本字符串
            sample_str = ""
            if samples:
                # 只取第一个，清洗一下
                first_val = str(samples[0]).strip()
                if len(first_val) > 50: first_val = first_val[:50] + "..."
                sample_str = f" [Example='{first_val}']"

            # 🔥 拼装：一行搞定所有！
            # 格式：- schools.Magnet (INTEGER) [Example='1']
            line = f"- {t}.{c} ({dtype}){sample_str}"
            schema_lines.append(line)

    # 如果没有检索到列（极少情况），用原来的 schema_str 兜底，或者显示空
    if schema_lines:
        schema_context = "\n".join(schema_lines)
    else:
        # 兜底：如果 RAG 没拿到列信息，就用原本的纯文本 Schema
        schema_context = state.get("schema_str", "No schema info available.")[:3000]

    # ---------------------------------------------------------
    # 2. 准备 Business Rules
    # ---------------------------------------------------------
    business_rules = state.get("business_rules", [])
    rules_list = []
    if business_rules:
        for r in business_rules:
            txt = r.get("content") if isinstance(r, dict) else str(r)
            if txt.strip():
                rules_list.append(f"- {txt.strip()}")

    rules_str = "\n".join(rules_list) if rules_list else "No specific business rules."

    # ---------------------------------------------------------
    # 3. 调用 LLM
    # ---------------------------------------------------------
    retry_count = state.get("retry_count", 0)
    MAX_RETRIES = 3

    logger.info(f"🧐 [Verifier] Reviewing SQL (Attempt {retry_count + 1})...")

    if not generated_sql:
        return {"verified": False, "feedback": "Empty SQL.", "retry_count": retry_count + 1}

    llm = get_llm()

    prompt = SQL_REFLECTION_PROMPT.format(
        question=question,
        sql=generated_sql,
        schema_context=schema_context,  # 👈 只有这一个变量了，清爽！
        business_rules=rules_str
    )

    try:
        messages = [
            SystemMessage(content="You are a SQL compliance checker. Output raw JSON only."),
            HumanMessage(content=prompt)
        ]

        response = await llm.ainvoke(messages)
        content = response.content.strip()

        if content.startswith("```"):
            content = content.replace("```json", "").replace("```", "")

        result = json.loads(content)
        status = result.get("status", "FAIL").upper()
        feedback = result.get("feedback", "")

        if status == "PASS":
            logger.info("✅ [Verifier] SQL approved!")
            return {"verified": True, "feedback": "", "retry_count": retry_count}
        else:
            logger.warning(f"❌ [Verifier] SQL Rejected: {feedback}")
            if retry_count >= MAX_RETRIES:
                logger.error("🛑 [Verifier] Max retries reached. Force passing.")
                return {"verified": True, "feedback": "Max retries reached.", "retry_count": retry_count}
            return {"verified": False, "feedback": feedback, "retry_count": retry_count + 1}

    except Exception as e:
        logger.error(f"⚠️ [Verifier] Error: {e}")
        return {"verified": True, "feedback": ""}