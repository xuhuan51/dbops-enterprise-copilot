import re
import json
from typing import Dict, Any, List
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from app.core.config import settings
from app.core.llm import get_llm
from app.core.state import AgentState
from app.core.logger import logger

# 🔥 导入两个 Prompt：一个是正常生成，一个是修 Bug
from app.core.prompts import (
    GEN_SQL_PROMPT,
    SQL_RETRY_FEEDBACK_TEMPLATE,
    FIX_SQL_PROMPT  # <--- ✅ 新增这个导入
)


# ==============================================================================
# 🛠️ 辅助函数 (保持不变)
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
# 🚀 核心生成节点
# ==============================================================================

async def generate_node(state: AgentState) -> Dict[str, Any]:
    """
    Generator Node (Ultimate Version)
    策略优先级:
    1. 🚑 ICU 模式: 如果有 execution_error，优先使用 FIX_SQL_PROMPT 进行修复。
    2. 🔄 反馈模式: 如果 verified=False，使用带 Feedback 的 Prompt 重试。
    3. 🆕 正常模式: 初次生成。
    """
    # --- 1. 获取 State ---
    question = state.get("question", "")
    history = state.get("history", [])

    # 状态标志
    execution_error = state.get("execution_error")  # SQLite 报错信息
    generated_sql = state.get("generated_sql")  # 上一次生成的 SQL (用于修复)

    feedback = state.get("feedback", "")  # Verifier 的反馈
    verified = state.get("verified", True)

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
    # A. 约束处理 (Logic Flattening) - 无论是生成还是修复都需要
    # ----------------------------------------------------

    # ... (这部分逻辑保持不变，构建 constraints_str 和 rules_str) ...
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

    general_rules: List[str] = []
    if business_rules:
        for r in business_rules:
            txt = r.get("content") if isinstance(r, dict) else str(r)
            if txt.strip(): general_rules.append(txt.strip())

    # Constraints String
    constraints_str = "No specific mandatory filters."
    if hard_filters:
        constraints_str = "\n".join([f"🔴 FILTER: {c}" for c in hard_filters])

    # Rules String
    rules_blocks = []
    if general_rules:
        rules_blocks.extend([f"- 💡 {r}" for r in general_rules])
    if soft_hints:
        deduped_hints = _dedup_keep_order(soft_hints, limit=5)
        if deduped_hints:
            rules_blocks.append("\n**Matched Value Hints:**")
            rules_blocks.extend([f"- {h}" for h in deduped_hints])
    rules_str = "\n".join(rules_blocks) if rules_blocks else "No additional business rules."

    # Join Paths String
    paths_context = "No specific join paths provided."
    if join_paths:
        paths = [str(p) for p in join_paths]
        paths_context = "\n".join(paths[:5])

    # Few Shot String
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
    # 🔥 B. 策略分流 (核心修改)
    # ----------------------------------------------------

    llm = get_llm(model_name=settings.LLM_MODEL)
    final_prompt = ""
    mode_log = ""

    # 🛑 优先级 1: 执行报错修复 (ICU Mode)
    # 条件: 存在报错信息 + 存在旧 SQL
    if execution_error and generated_sql:
        mode_log = "🚑 [ICU Mode] Fixing Execution Error"
        logger.warning(f"{mode_log}: {execution_error[:100]}...")

        # 使用 FIX_SQL_PROMPT
        # 注意: 这里我们要把 rules_str 也传进去，辅助诊断
        final_prompt = FIX_SQL_PROMPT.format(
            question=question,
            schema_context=schema_str,
            rules_context=rules_str,  # 关键：带上规则，防止修好了语法丢了业务逻辑
            previous_sql=generated_sql,
            error_msg=execution_error
        )

    # 🔄 优先级 2: 验证反馈重试 (Feedback Mode)
    # 条件: verified=False + 存在反馈建议
    elif not verified and feedback:
        mode_log = "🔄 [Feedback Mode] Retrying with Verifier Feedback"
        logger.info(f"{mode_log}: {feedback[:100]}...")

        # 构造带反馈的问题
        final_question_prompt = SQL_RETRY_FEEDBACK_TEMPLATE.format(
            question=question,
            feedback=feedback
        )

        # 使用通用 Prompt，但 Question 变了
        final_prompt = GEN_SQL_PROMPT.format(
            history_context=history_context,
            schema_context=schema_str,
            constraints_context=constraints_str,
            rules_context=rules_str,
            join_paths_context=paths_context,
            few_shot_context=few_shot_str,
            question=final_question_prompt
        )

    # 🆕 优先级 3: 正常生成 (Normal Mode)
    else:
        mode_log = "🎨 [Normal Mode] First Generation"
        logger.info(mode_log)

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

        # 解析 (复用相同的解析逻辑，因为我们统一了 Prompt 的输出格式要求)
        sql = ""
        thought = ""

        # 1. 提取 SQL
        sql_match = re.search(r"```sql\n(.*?)\n```", content, re.DOTALL)
        if sql_match:
            sql = sql_match.group(1).strip()
        else:
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

        logger.info(f"📝 [Generator] SQL Generated: {final_sql[:50]}...")

        # ----------------------------------------------------
        # D. 更新 State
        # ----------------------------------------------------
        return {
            "generated_sql": final_sql,
            "generated_thought": thought,
            "final_answer": final_sql,

            # 🔥 关键：增加重试计数
            "retry_count": retry_count + 1,

            # 🔥 关键：清空之前的报错信息！
            # 否则 Router 会一直以为处于报错状态，导致死循环
            "execution_error": None,
            "is_executable": None
        }

    except Exception as e:
        logger.error(f"❌ [Generator] LLM Call Failed: {e}", exc_info=True)
        return {
            "generated_sql": "",
            "error_message": str(e)
        }