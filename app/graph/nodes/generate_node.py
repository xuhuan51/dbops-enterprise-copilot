import re
import json
from typing import Dict, Any, List
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from app.core.config import settings
from app.core.llm import get_llm
from app.core.state import AgentState
from app.core.logger import logger

# 🔥 核心：导入两个 Prompt
# GEN_SQL_PROMPT: 用于第一次从零生成 (重型)
# SQL_REPAIR_PROMPT: 用于根据报错或反馈进行修复 (轻型)
from app.core.prompts import GEN_SQL_PROMPT, SQL_REPAIR_PROMPT


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


def _dedup_keep_order(items: List[str], limit: int | None = None) -> List[str]:
    seen = set()
    out = []
    for x in items:
        x = (x or "").strip()
        if not x or x in seen: continue
        seen.add(x)
        out.append(x)
    return out[:limit] if limit else out


# ==============================================================================
# 🚀 核心生成节点 (Final Ultimate Version)
# ==============================================================================

async def generate_node(state: AgentState) -> Dict[str, Any]:
    """
    Generator Node
    策略逻辑：
    1. 判断是否存在错误来源 (Execution Error 或 Verifier Feedback)。
    2. 如果有错且有旧代码 -> 进入 [Repair Mode] (使用 SQL_REPAIR_PROMPT)。
    3. 否则 -> 进入 [Normal Mode] (使用 GEN_SQL_PROMPT)。
    """
    # --- 1. 获取 State ---
    question = state.get("question", "")
    history = state.get("history", [])

    # 状态标志
    execution_error = state.get("execution_error")  # SQLite 报错
    generated_sql = state.get("generated_sql")  # 旧 SQL
    feedback = state.get("feedback", "")  # Verifier 反馈
    verified = state.get("verified", True)  # 验证状态

    retry_count = state.get("retry_count", 0)

    # 上下文相关
    schema_str = state.get("schema_str", "")
    join_paths = state.get("join_paths", [])
    business_rules = state.get("business_rules", [])
    value_matches = state.get("value_matches", [])
    few_shot_examples = state.get("few_shot_examples", [])

    # 历史记录
    history_context = _format_history(history) if history else ""

    # ----------------------------------------------------
    # A. 上下文组装 (Logic Flattening)
    # ----------------------------------------------------
    # 即使是修复模式，也需要 Rules 和 Schema，所以先组装好

    # 1. 处理 Value Matches (Hard Filters & Hints)
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

    # 2. 处理 Business Rules
    general_rules: List[str] = []
    if business_rules:
        for r in business_rules:
            txt = r.get("content") if isinstance(r, dict) else str(r)
            if txt.strip(): general_rules.append(txt.strip())

    # 构建 rules_str (包含业务规则和值匹配提示)
    # 这对修复模式至关重要，防止修好了语法但丢了业务逻辑
    rules_blocks = []
    if general_rules:
        rules_blocks.extend([f"- 💡 {r}" for r in general_rules])
    if soft_hints:
        deduped_hints = _dedup_keep_order(soft_hints, limit=5)
        if deduped_hints:
            rules_blocks.append("\n**Matched Value Hints:**")
            rules_blocks.extend([f"- {h}" for h in deduped_hints])
    rules_str = "\n".join(rules_blocks) if rules_blocks else "No additional business rules."

    # 构建 constraints_str (仅用于 Normal Mode)
    constraints_str = "No specific mandatory filters."
    if hard_filters:
        constraints_str = "\n".join([f"🔴 FILTER: {c}" for c in hard_filters])

    # 构建 join_paths_str (仅用于 Normal Mode)
    paths_context = "No specific join paths provided."
    if join_paths:
        paths = [str(p) for p in join_paths]
        paths_context = "\n".join(paths[:5])

    # 构建 few_shot_str (仅用于 Normal Mode)
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
    # 🔥 B. 策略分流 (核心逻辑)
    # ----------------------------------------------------

    llm = get_llm(model_name=settings.LLM_MODEL)
    final_prompt = ""
    mode_log = ""

    # 确定当前的“错误信息” (Execution Error 优先级高于 Feedback)
    current_error = None
    if execution_error:
        current_error = f"Execution Failed: {execution_error}"
    elif not verified and feedback:
        current_error = f"Verifier Critique: {feedback}"

    # 🚦 分支判断

    # 【模式 A：统一修复模式 (Repair Mode)】
    # 条件：(有报错 OR 有反馈) AND 有旧代码
    if current_error and generated_sql:

        # 使用轻量级的 SQL_REPAIR_PROMPT
        # 它可以让 LLM 聚焦于 Schema、错误信息和旧代码，避免被 Few-Shot 干扰
        final_prompt = SQL_REPAIR_PROMPT.format(
            question=question,
            schema_context=schema_str,
            rules_context=rules_str,  # 带上规则，确保逻辑正确
            previous_sql=generated_sql,
            error_msg=current_error  # 无论是报错还是骂声，都填这里
        )

    # 【模式 B：从零生成模式 (Creation Mode)】
    # 条件：第一次生成 OR 之前没有旧代码
    else:
        mode_log = "🎨 [Normal Mode] Generating from scratch..."
        logger.info(mode_log)

        # 使用重型的 GEN_SQL_PROMPT
        final_prompt = GEN_SQL_PROMPT.format(
            history_context=history_context,
            schema_context=schema_str,
            constraints_context=constraints_str,
            rules_context=rules_str,
            join_paths_context=paths_context,
            few_shot_context=few_shot_str,
            question=question
        )

    # ----------------------------------------------------
    # C. 调用模型与解析
    # ----------------------------------------------------
    try:
        messages = [
            SystemMessage(content="You are a SQL expert. Output ONLY valid Markdown with 'audit' and 'sql' blocks."),
            HumanMessage(content=final_prompt)
        ]

        response = await llm.ainvoke(messages)
        content = response.content

        # 解析逻辑 (统一处理)
        sql = ""
        thought = ""

        # 1. 提取 SQL
        sql_match = re.search(r"```sql\n(.*?)\n```", content, re.DOTALL)
        if sql_match:
            sql = sql_match.group(1).strip()
        else:
            # 兜底
            clean_text = content.replace("```sql", "").replace("```", "").strip()
            if "SELECT" in clean_text.upper():
                sql = clean_text

        # 2. 提取 Audit
        audit_match = re.search(r"```audit\n(.*?)\n```", content, re.DOTALL)
        if audit_match:
            thought = audit_match.group(1).strip()

        # 3. 补全分号
        final_sql = sql
        if final_sql and not final_sql.endswith(";"):
            final_sql += ";"

        logger.info(f"📝 [Generator] SQL Generated: {final_sql}")

        # ----------------------------------------------------
        # D. 更新 State
        # ----------------------------------------------------
        return {
            "generated_sql": final_sql,
            "generated_thought": thought,
            "final_answer": final_sql,
            # 🔥 关键：清空之前的报错信息，重置状态
            # 如果这轮生成的 SQL 还有错，Verifier 或 Executor 会再次把这两个字段填上
            "execution_error": None,
            "is_executable": None,
            # 注意：verified 状态会在 verification_node 更新，这里不需要强制设为 True，
            # 但通常生成新 SQL 后，我们假设它需要重新验证。
        }

    except Exception as e:
        logger.error(f"❌ [Generator] LLM Call Failed: {e}", exc_info=True)
        return {
            "generated_sql": "",
            "error_message": str(e)
        }