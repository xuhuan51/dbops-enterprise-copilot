import re
import json
from typing import Dict, Any, List
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from app.core.config import settings
from app.core.llm import get_llm
from app.core.state import AgentState
from app.core.logger import logger

# 🔥🔥🔥 重点：直接从你的 prompt 文件导入，不再重复定义
from app.core.prompts import GEN_SQL_PROMPT


# ==============================================================================
# 🛠️ 辅助函数
# ==============================================================================

def _format_history(history: List[Any]) -> str:
    if not history: return "无 (这是第一轮对话)"
    context_lines = []
    for msg in history[-6:]:
        if isinstance(msg, HumanMessage):
            context_lines.append(f"User: {msg.content}")
        elif isinstance(msg, AIMessage):
            content = str(msg.content)[:200].replace("\n", " ")
            context_lines.append(f"AI: {content}...")
    return "\n".join(context_lines)


def _parse_markdown_output(raw_content: str) -> Dict[str, Any]:
    """
    🔥 解析 Markdown 分块输出 (比 JSON 更稳健)
    """
    if not raw_content:
        return {"sql": "", "thought": "Empty input."}

    txt = raw_content.strip()

    # 1. 提取 Thought
    thought = ""
    # 匹配 ```thought ... ``` 或者在 ```sql 之前的内容
    thought_match = re.search(r"```thought\s*(.*?)\s*```", txt, re.DOTALL | re.IGNORECASE)
    if thought_match:
        thought = thought_match.group(1).strip()
    else:
        # 兜底：如果没写 thought block，就把 SQL 之前的所有内容当做 thought
        sql_start = txt.find("```sql")
        if sql_start > 0:
            thought = txt[:sql_start].strip()

    # 2. 提取 SQL
    sql = ""
    sql_match = re.search(r"```sql\s*(.*?)\s*```", txt, re.DOTALL | re.IGNORECASE)
    if sql_match:
        sql = sql_match.group(1).strip()
    else:
        # 兜底：找 SELECT ... ;
        raw_sql_match = re.search(r"(SELECT .*?;)", txt, re.DOTALL | re.IGNORECASE)
        if raw_sql_match:
            sql = raw_sql_match.group(1).strip()

    # 清洗 SQL (去掉可能存在的 markdown 标记残留)
    if sql:
        sql = sql.replace("```", "").strip()

    if not sql:
        return {"sql": "", "thought": thought, "error": "No SQL found in output"}

    return {"sql": sql, "thought": thought}


def _dedup_keep_order(items: List[str], limit: int | None = None) -> List[str]:
    seen = set()
    out = []
    for x in items:
        x = (x or "").strip()
        if not x or x in seen: continue
        seen.add(x)
        out.append(x)
    return out[:limit] if limit else out


async def generate_node(state: AgentState) -> Dict[str, Any]:
    """
    Generator Node (Revised: Logic Flattening Strategy)
    不强制区分硬约束/软约束，将所有规则平铺给 LLM，依赖模型自身推理能力。
    """
    question = state.get("question", "")
    history = state.get("history", [])

    schema_str = state.get("schema_str", "")
    join_paths = state.get("join_paths", [])
    business_rules = state.get("business_rules", [])
    value_matches = state.get("value_matches", [])
    few_shot_examples = state.get("few_shot_examples", [])

    history_context = _format_history(history)

    # ----------------------------------------------------
    # A. 约束处理 (修正版：逻辑降级)
    # ----------------------------------------------------

    # 1. 强制过滤器 (Mandatory Filters)
    # 这些通常是 Value Scanner 找出的具体值 (如 "Year = 2023")，依然作为硬约束
    hard_filters: List[str] = []
    soft_hints: List[str] = []

    if value_matches:
        for m in value_matches:
            s = str(m).strip()
            if not s: continue
            if "CONSTRAINT" in s or "hard" in s.lower():
                hard_filters.append(s)
            else:
                soft_hints.append(s)

    # 2. 业务规则 (Business Logic)
    # 🔥 核心修改：不再检测公式，不再强制升级。
    # 所有的 RAG 规则都视为同等重要的业务逻辑，放入 general_rules。
    general_rules: List[str] = []

    if business_rules:
        for r in business_rules:
            txt = r.get("content") if isinstance(r, dict) else str(r)
            txt = txt.strip()
            if not txt: continue

            # 直接添加，不做公式/非公式的二元分类
            # 让 Prompt 里的 "CRITICAL RULES" 区块去统一管理这些规则
            general_rules.append(txt)

    # ----------------------------------------------------
    # B. 组装 Context (对应 Prompt 结构)
    # ----------------------------------------------------

    # 1. Mandatory Filters (对应 constraints_context)
    constraints_str = "No specific mandatory filters."
    if hard_filters:
        constraints_str = "\n".join([f"🔴 FILTER: {c}" for c in hard_filters])

    # 2. Business Logic (对应 rules_context)
    rules_blocks = []
    if general_rules:
        # 这里不加 "MANDATORY" 前缀，而是用 emoji 标记，保持中立
        rules_blocks.extend([f"- 💡 {r}" for r in general_rules])

    if soft_hints:
        deduped_hints = _dedup_keep_order(soft_hints, limit=5)
        if deduped_hints:
            rules_blocks.append("\n**Matched Value Hints:**")
            rules_blocks.extend([f"- {h}" for h in deduped_hints])

    rules_str = "\n".join(rules_blocks) if rules_blocks else "No additional business rules."

    # 3. Join Paths (对应 join_paths_context)
    paths_context = "No specific join paths provided."
    if join_paths:
        paths = [str(p) for p in join_paths]
        # 只要前 5 条最强的路径
        paths_context = "\n".join(paths[:5])

    # 4. Few Shot (对应 few_shot_context)
    few_shot_str = "No few-shot examples available."
    if few_shot_examples:
        ex_lines = []
        for i, ex in enumerate(few_shot_examples):
            ex_lines.append(f"--- Example {i + 1} ---")
            ex_lines.append(f"Q: {ex.get('question', '')}")
            if ex.get('evidence'): ex_lines.append(f"Note: {ex.get('evidence')}")
            ex_lines.append(f"SQL: {ex.get('sql', '')}")
        few_shot_str = "\n".join(ex_lines)

    # ----------------------------------------------------
    # C. 调用模型
    # ----------------------------------------------------
    try:
        # 填充 Prompt
        # ⚠️ 请确保 GEN_SQL_PROMPT 里的占位符名称与这里一致
        prompt = GEN_SQL_PROMPT.format(
            history_context=history_context,
            schema_context=schema_str,
            constraints_context=constraints_str,  # 对应 B. Mandatory Filters
            rules_context=rules_str,  # 对应 A. Business Logic
            join_paths_context=paths_context,  # 对应 C. Join Paths
            few_shot_context=few_shot_str,
            question=question
        )

        logger.info(f"🎨 [Generator] Prompt assembled. Rules Count: {len(general_rules)}")

        # 调试：打印一下喂给模型的 Rules，确认没有 "MANDATORY FORMULA" 这种字眼
        # print(f"DEBUG RULES:\n{rules_str}")

        llm = get_llm(model_name=settings.LLM_MODEL)

        messages = [
            SystemMessage(content="You are a SQL expert. Output ONLY valid Markdown with 'audit' and 'sql' blocks."),
            HumanMessage(content=prompt)
        ]

        response = await llm.ainvoke(messages)

        # 解析
        result = _parse_markdown_output(response.content)

        final_sql = result.get("sql", "")
        thought = result.get("thought", "")

        if final_sql and not final_sql.endswith(";"): final_sql += ";"

        logger.info(f"📝 [Generator] SQL: {final_sql}")
        if thought:
            logger.info(f"💭 [Generator] Audit: {thought}")

        return {
            "generated_sql": final_sql,
            "final_answer": final_sql,
            "generator_thought": thought
        }

    except Exception as e:
        logger.error(f"❌ [Generator] Failed: {e}", exc_info=True)
        return {"generated_sql": "", "error": str(e)}