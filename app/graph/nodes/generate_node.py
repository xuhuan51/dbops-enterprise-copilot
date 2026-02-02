from typing import Dict, Any, List
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from app.core.config import settings
from app.core.llm import get_llm
from app.core.prompts import GEN_SQL_PROMPT
from app.core.state import AgentState
from app.core.logger import logger
import json


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
            # 尝试从 AI 回复中提取 SQL
            content = msg.content
            if "{" in content and "sql" in content:
                try:
                    data = json.loads(content)
                    sql = data.get("sql", content)
                    context_lines.append(f"AI Generated SQL: {sql}")
                except:
                    context_lines.append(f"AI: {content}")
            else:
                context_lines.append(f"AI: {content}")

    return "\n".join(context_lines)


def _clean_json_output(raw_content: str) -> Dict[str, Any]:
    """
    清洗并解析 JSON 输出
    """
    try:
        txt = raw_content.strip()
        # 去除 markdown 包裹
        if "```json" in txt:
            txt = txt.split("```json")[1].split("```")[0]
        elif "```" in txt:
            txt = txt.split("```")[1].split("```")[0]

        return json.loads(txt.strip())
    except Exception as e:
        logger.error(f"JSON Parse Failed: {e} | Raw: {raw_content}")
        # 兜底策略
        return {
            "sql": raw_content,
            "thought": "Failed to parse JSON, returning raw content.",
            "error": "json_parse_error"
        }


async def generate_node(state: AgentState) -> Dict[str, Any]:
    """
    Generator Node: 注入 History + Schema + Rules + Paths + ValueMatches
    """
    question = state.get("question", "")
    history = state.get("history", [])

    # 1. 获取 Retrieval 产生的素材
    schema_str = state.get("schema_str", "")
    join_paths = state.get("join_paths", [])
    business_rules = state.get("business_rules", [])
    value_matches = state.get("value_matches", [])

    # 2. 格式化各个板块
    history_context = _format_history(history)

    # ----------------------------------------------------
    # 🔥🔥🔥 核心修改：构建强制约束 (Hard Constraints) 🔥🔥🔥
    # ----------------------------------------------------
    constraints = []
    knowledge_lines = []  # 剩下的非强制知识放这里

    # A. 处理值匹配 (Value Binding) - 针对 Case-1/2
    if value_matches:
        for match in value_matches:
            # 极其强势的语气
            constraints.append(
                f"🔴 CONSTRAINT (Entity Binding): {match} -> You MUST use this exact value and column in WHERE clause.")

    # B. 处理业务规则 (Formula Binding) - 针对 Case-0
    if business_rules:
        for r in business_rules:
            txt = str(r)
            if hasattr(r, 'entity'):
                txt = r.entity.get('rule_text') or r.entity.get('doc_text')
            elif isinstance(r, dict):
                txt = r.get('rule_text') or r.get('doc_text')

            # 智能判断：如果是计算公式，放入强制区；否则放入普通知识区
            # 关键词：/, +, -, *, =, formula, calculate
            if any(op in txt for op in ['/', '+', '-', '*', '=', 'formula', 'calculate']):
                constraints.append(
                    f"🔴 CONSTRAINT (Formula Binding): Calculation Rule -> {txt} -> Do NOT use pre-computed columns like 'Rate' or 'Percent' if raw columns exist.")
            else:
                knowledge_lines.append(f"- {txt}")

    # C. 格式化约束字符串
    if constraints:
        constraints_str = "\n".join(constraints)
    else:
        constraints_str = "No specific mandatory constraints."

    # D. 格式化普通知识
    if knowledge_lines:
        knowledge_context = "\n".join(knowledge_lines)
    else:
        knowledge_context = "No additional knowledge."

    # --- 构建 Paths Context ---
    paths_context = "No join paths (Single Table)."
    if join_paths:
        paths_context = "\n".join([f"- {str(p)}" for p in join_paths])

    # 3. 填充 Prompt
    # 注意：这里我们传入了新的 constraints_context
    prompt = GEN_SQL_PROMPT.format(
        history_context=history_context,
        schema_context=schema_str,
        constraints_context=constraints_str,  # 👈 新增变量
        knowledge_context=knowledge_context,
        join_paths_context=paths_context,
        question=question
    )

    logger.info(f"🎨 [Generator] Prompt assembled. History len: {len(history)}")
    if constraints:
        logger.info(f"💡 [Generator] Injected {len(constraints)} MANDATORY constraints.")

    try:
        # 调用 LLM
        llm = get_llm(model_name=settings.LLM_MODEL)

        logger.info(f"🤖 [Generator] Invoking Model: {settings.LLM_MODEL} ...")

        messages = [
            SystemMessage(content="You are a strict JSON-speaking SQL expert."),
            HumanMessage(content=prompt)
        ]

        response = await llm.ainvoke(messages)

        # 5. 解析结果
        result_json = _clean_json_output(response.content)
        final_sql = result_json.get("sql", "")

        # 简单的 SQL 清洗
        if ";" in final_sql:
            final_sql = final_sql.split(";")[0] + ";"

        logger.info(f"📝 [Generator] SQL: {final_sql}")
        logger.info(f"💭 [Generator] Thought: {result_json.get('thought', 'N/A')}")

        return {
            "generated_sql": final_sql,
            "final_answer": final_sql
        }

    except Exception as e:
        logger.error(f"❌ [Generator] Failed: {e}", exc_info=True)
        return {
            "error_message": str(e),
            "generated_sql": ""
        }