import re
import json
from typing import Dict, Any, List
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from app.core.config import settings
from app.core.llm import get_llm
from app.core.state import AgentState
from app.core.logger import logger

# 🔥🔥🔥 重点：直接从你的 prompt 文件导入，不再重复定义
from app.core.prompts import GEN_SQL_PROMPT, SQL_RETRY_FEEDBACK_TEMPLATE


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
    Generator Node (Final Version)
    1. Logic Flattening: 平铺所有业务规则，不强制区分公式。
    2. Feedback Injection: 如果 verified=False，利用 Template 注入 Reviewer 的反馈。
    """
    # --- 1. 获取 State ---
    question = state.get("question", "")
    history = state.get("history", [])

    # 反馈相关
    feedback = state.get("feedback", "")
    verified = state.get("verified", True)  # 默认为 True，只有被 Verifier 打回才为 False
    retry_count = state.get("retry_count", 0)

    # 上下文相关
    schema_str = state.get("schema_str", "")
    join_paths = state.get("join_paths", [])
    business_rules = state.get("business_rules", [])
    value_matches = state.get("value_matches", [])
    few_shot_examples = state.get("few_shot_examples", [])

    # 处理历史记录 (如果有的话)
    history_context = ""
    # if history: history_context = _format_history(history)

    # ----------------------------------------------------
    # A. 约束处理 (Logic Flattening)
    # ----------------------------------------------------

    # 1. 强制过滤器 (Mandatory Filters - 来自 Value Scanner)
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

    # 2. 业务规则 (Business Logic - 来自 RAG)
    general_rules: List[str] = []
    if business_rules:
        for r in business_rules:
            # 兼容 dict 或 str
            txt = r.get("content") if isinstance(r, dict) else str(r)
            txt = txt.strip()
            if not txt: continue
            general_rules.append(txt)

    # ----------------------------------------------------
    # B. 组装 Context (对应 Prompt 结构)
    # ----------------------------------------------------

    # 1. Constraints String
    constraints_str = "No specific mandatory filters."
    if hard_filters:
        constraints_str = "\n".join([f"🔴 FILTER: {c}" for c in hard_filters])

    # 2. Rules String
    rules_blocks = []
    if general_rules:
        rules_blocks.extend([f"- 💡 {r}" for r in general_rules])

    if soft_hints:
        deduped_hints = _dedup_keep_order(soft_hints, limit=5)
        if deduped_hints:
            rules_blocks.append("\n**Matched Value Hints:**")
            rules_blocks.extend([f"- {h}" for h in deduped_hints])

    rules_str = "\n".join(rules_blocks) if rules_blocks else "No additional business rules."

    # 3. Join Paths String
    paths_context = "No specific join paths provided."
    if join_paths:
        paths = [str(p) for p in join_paths]
        paths_context = "\n".join(paths[:5])

    # 4. Few Shot String
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
    # 🔥 C. 反馈注入 (Feedback Injection)
    # ----------------------------------------------------
    final_question_prompt = question

    # 只有当 "验证未通过" 且 "存在反馈" 时，才启用重试模板
    if not verified and feedback:
        logger.info(f"🔄 [Generator] Retrying with feedback (Attempt {retry_count})...")

        # 使用 prompts.py 里定义的模板进行填空
        final_question_prompt = SQL_RETRY_FEEDBACK_TEMPLATE.format(
            question=question,
            feedback=feedback
        )

    # ----------------------------------------------------
    # D. 调用模型
    # ----------------------------------------------------
    try:
        # 填充主 Prompt
        # 注意：这里我们不需要修改 GEN_SQL_PROMPT，只要把处理好的 final_question_prompt 传进去即可
        prompt = GEN_SQL_PROMPT.format(
            history_context=history_context,
            schema_context=schema_str,
            constraints_context=constraints_str,
            rules_context=rules_str,
            join_paths_context=paths_context,
            few_shot_context=few_shot_str,
            question=final_question_prompt  # 👈 关键点：传入的是（可能带骂声的）问题
        )

        logger.info(f"🎨 [Generator] Prompt assembled. Rules Count: {len(general_rules)}")

        llm = get_llm(model_name=settings.LLM_MODEL)

        messages = [
            SystemMessage(content="You are a SQL expert. Output ONLY valid Markdown with 'audit' and 'sql' blocks."),
            HumanMessage(content=prompt)
        ]

        response = await llm.ainvoke(messages)
        content = response.content

        # ----------------------------------------------------
        # E. 结果解析
        # ----------------------------------------------------
        sql = ""
        thought = ""

        # 1. 提取 SQL
        sql_match = re.search(r"```sql\n(.*?)\n```", content, re.DOTALL)
        if sql_match:
            sql = sql_match.group(1).strip()
        else:
            # 兜底：尝试去掉所有 markdown 标记
            clean_text = content.replace("```sql", "").replace("```", "").strip()
            # 简单判断是否像 SQL
            if "SELECT" in clean_text.upper():
                sql = clean_text

        # 2. 提取 Audit/Thought
        audit_match = re.search(r"```audit\n(.*?)\n```", content, re.DOTALL)
        if audit_match:
            thought = audit_match.group(1).strip()

        # 3. 规范化 SQL
        final_sql = sql
        if final_sql and not final_sql.endswith(";"):
            final_sql += ";"

        logger.info(f"📝 [Generator] SQL: {final_sql}")
        if thought:
            logger.info(f"💭 [Generator] Audit: {thought}")

        return {
            "generated_sql": final_sql,
            "generated_thought": thought,
            # final_answer 暂时先给 SQL，等 Execution 节点跑完后，会更新为数据结果
            "final_answer": final_sql
        }

    except Exception as e:
        logger.error(f"❌ [Generator] Failed: {e}", exc_info=True)
        return {
            "generated_sql": "",
            "error_message": str(e)
        }