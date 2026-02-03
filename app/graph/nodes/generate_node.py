# app/graph/nodes/generate_node.py
import re
from typing import Dict, Any, List
import json
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from app.core.config import settings
from app.core.llm import get_llm
from app.core.prompts import GEN_SQL_PROMPT
from app.core.state import AgentState
from app.core.logger import logger


def _format_history(history: List[Any]) -> str:
    """
    将聊天记录格式化为清晰的上下文文本
    """
    if not history:
        return "无 (这是第一轮对话)"

    context_lines = []
    # 只取最近的 6 轮对话，避免 Prompt 过长
    for msg in history[-6:]:
        if isinstance(msg, HumanMessage):
            context_lines.append(f"User: {msg.content}")
        elif isinstance(msg, AIMessage):
            content = msg.content
            if "{" in content and "sql" in content:
                try:
                    if "```json" in content:
                        clean_content = content.split("```json")[1].split("```")[0].strip()
                        data = json.loads(clean_content)
                    else:
                        data = json.loads(content)

                    sql = data.get("sql", "")
                    if sql:
                        context_lines.append(f"AI Generated SQL: {sql}")
                    else:
                        context_lines.append(f"AI: {content[:100]}...")
                except:
                    context_lines.append(f"AI: {content[:100]}...")
            else:
                context_lines.append(f"AI: {content[:100]}...")

    return "\n".join(context_lines)


def _clean_json_output(raw_content: str) -> Dict[str, Any]:
    """
    清洗并解析 JSON 输出 (v5.2: 鲁棒性增强版)
    针对模型输出的非法转义符、换行符以及 JSON 结构损坏进行自动修复。
    """
    if not raw_content:
        return {"sql": "", "thought": "Empty input."}

    # 1. 提取 Markdown 中的 JSON 代码块
    txt = raw_content.strip()
    if "```json" in txt:
        txt = txt.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in txt:
        txt = txt.split("```", 1)[1].split("```", 1)[0]

    txt = txt.strip()

    try:
        # 2. 尝试初步清理并解析
        # 替换掉模型最爱乱写的 \' (JSON 规范只允许 \")
        # 处理掉可能存在的单反斜杠
        processed_txt = txt.replace("\\'", "'")
        return json.loads(processed_txt)

    except Exception as e:
        logger.warning(f"⚠️ [JSON Fixer] Standard parse failed, trying advanced recovery: {e}")

        try:
            # 3. 进阶清理：处理 JSON 字符串中的非法换行符
            # 逻辑：在 JSON 的 key-value 结构中，如果双引号之间有换行，会导致解析失败
            # 这里用正则尝试清理掉 thought 字段里的非法换行
            fixed_txt = re.sub(r'("thought":\s*")(.*?)("\s*,\s*"sql")',
                               lambda m: m.group(1) + m.group(2).replace('\n', '\\n') + m.group(3),
                               processed_txt, flags=re.DOTALL)
            return json.loads(fixed_txt)
        except:
            # 4. 🛡️ 终极兜底：正则暴力提取 SQL (不管 JSON 是否完整)
            # 哪怕整个 JSON 格式全乱了，只要里面有 "sql": "SELECT..." 就能救回来
            logger.error("🚨 [JSON Fixer] Advanced recovery failed. Falling back to Regex extraction.")

            # 提取 SQL (支持跨行)
            sql_match = re.search(r'"sql":\s*"(SELECT.*?)"', txt, re.DOTALL | re.IGNORECASE)
            # 提取 Thought (尽量拿)
            thought_match = re.search(r'"thought":\s*"(.*?)"', txt, re.DOTALL | re.IGNORECASE)

            if sql_match:
                # 把里面的转义引号还原回来
                sql = sql_match.group(1).replace('\\"', '"').strip()
                thought = thought_match.group(1) if thought_match else "Recovered via regex."
                return {
                    "sql": sql,
                    "thought": f"[Regex Recovered] {thought}",
                    "used_tables": []  # 兜底补齐字段
                }

    # 5. 彻底完蛋
    return {
        "sql": "",
        "thought": "Failed to recover SQL from raw output.",
        "error": "unrecoverable_json_error"
    }


def _is_hard_constraint(line: str) -> bool:
    """
    只有 🔴 CONSTRAINT 开头才算强约束。
    其它（🟡 HINT / HINT: / 任何不确定）一律作为软提示。
    """
    if not line:
        return False
    s = str(line).strip()
    return s.startswith("🔴 CONSTRAINT")


def _dedup_keep_order(items: List[str], limit: int | None = None) -> List[str]:
    seen = set()
    out = []
    for x in items:
        x = (x or "").strip()
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
        if limit is not None and len(out) >= limit:
            break
    return out


async def generate_node(state: AgentState) -> Dict[str, Any]:
    """
    Generator Node: 注入 History + Schema + Rules + Paths + ValueMatches
    """
    question = state.get("question", "")
    history = state.get("history", [])

    schema_str = state.get("schema_str", "")
    join_paths = state.get("join_paths", [])
    business_rules = state.get("business_rules", [])
    value_matches = state.get("value_matches", [])  # List[str]，里面可能混入 🔴/🟡

    history_context = _format_history(history)

    # ----------------------------------------------------
    # ✅ 核心修复：硬约束/软提示分流
    # ----------------------------------------------------
    hard_constraints: List[str] = []
    soft_hints: List[str] = []
    knowledge_lines: List[str] = []

    # A) 处理 value_matches：🔴 才进 constraints，🟡 全部进 knowledge/hints
    if value_matches:
        for m in value_matches:
            s = str(m).strip()
            if not s:
                continue
            if _is_hard_constraint(s):
                hard_constraints.append(s)
            else:
                # 统一归入软提示
                soft_hints.append(s)

    # B) 处理业务规则：公式类进硬约束，其它进知识
    if business_rules:
        for r in business_rules:
            # 1. 精准提取：只拿 rule_text
            txt = ""
            if isinstance(r, dict):
                txt = r.get("rule_text") or r.get("doc_text") or ""
            else:
                txt = str(r)

            txt = txt.strip()
            if not txt: continue

            # 2. 识别公式特征
            is_formula = any(op in txt for op in ["/", "+", "-", "*", "=", "sum(", "count("])

            if is_formula:
                # 3. 语气强化：不仅给公式，还要下“死命令”
                hard_constraints.append(
                    f"🔴 MANDATORY CALCULATION: {txt}. "
                    "YOU MUST use this specific formula. "
                    "DO NOT use pre-computed average/rate columns from the schema."
                )
            else:
                # 普通业务逻辑（如 Virtual = F）也建议作为硬约束，因为它决定了 WHERE 条件
                hard_constraints.append(f"🔴 CONSTRAINT: {txt}")

    # 4. 排序：把公式类的约束排在最前面
    hard_constraints.sort(key=lambda x: "FORMULA" in x, reverse=True)

    # D) 软提示也去重 & 上限（否则 prompt 也会膨胀）
    soft_hints = _dedup_keep_order(soft_hints, limit=8)

    # ----------------------------------------------------
    # 组装 constraints_context / knowledge_context
    # ----------------------------------------------------
    constraints_str = "\n".join(hard_constraints) if hard_constraints else "No specific mandatory constraints."

    # knowledge_context 里同时放：软提示 + 非公式业务规则
    knowledge_blocks = []
    if soft_hints:
        knowledge_blocks.append("Soft Hints (optional, do NOT treat as hard filters):")
        knowledge_blocks.extend([f"- {h}" for h in soft_hints])

    if knowledge_lines:
        knowledge_blocks.append("Business Knowledge (optional):")
        knowledge_blocks.extend(knowledge_lines)

    knowledge_context = "\n".join(knowledge_blocks) if knowledge_blocks else "No additional knowledge."

    # --- Paths Context ---
    paths_context = "No join paths (Single Table)."
    if join_paths:
        paths_context = "\n".join([f"- {str(p)}" for p in join_paths])

    # 3) 填充 Prompt
    prompt = GEN_SQL_PROMPT.format(
        history_context=history_context,
        schema_context=schema_str,
        constraints_context=constraints_str,
        knowledge_context=knowledge_context,
        join_paths_context=paths_context,
        question=question
    )

    logger.info(f"🎨 [Generator] Prompt assembled. History len: {len(history)}")
    if hard_constraints:
        logger.info(f"💡 [Generator] Injected {len(hard_constraints)} HARD constraints.")
        for c in hard_constraints[:3]:
            logger.info(f"   -> {c}")
    if soft_hints:
        logger.info(f"🟡 [Generator] Injected {len(soft_hints)} SOFT hints (non-mandatory).")
        for h in soft_hints[:3]:
            logger.info(f"   -> {h}")

    try:
        llm = get_llm(model_name=settings.LLM_MODEL)
        logger.info(f"🤖 [Generator] Invoking Model: {settings.LLM_MODEL} ...")

        messages = [
            SystemMessage(content="You are a strict JSON-speaking SQL expert."),
            HumanMessage(content=prompt)
        ]

        response = await llm.ainvoke(messages)

        result_json = _clean_json_output(response.content)
        final_sql = result_json.get("sql", "")
        thought = result_json.get("thought", "No thought provided.")

        if final_sql:
            final_sql = final_sql.replace("```sql", "").replace("```", "").strip()
            if not final_sql.endswith(";"):
                final_sql += ";"

        logger.info(f"📝 [Generator] SQL: {final_sql}")
        logger.info(f"💭 [Generator] Thought: {thought}")

        return {
            "generated_sql": final_sql,
            "final_answer": final_sql,
            "thought": thought
        }

    except Exception as e:
        logger.error(f"❌ [Generator] Failed: {e}", exc_info=True)
        return {
            "error_message": str(e),
            "generated_sql": ""
        }
