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
# 🛠️ 辅助函数：结构化 Schema 组装器
# ==============================================================================

def _build_rich_schema_json(retrieved_columns: List[Dict]) -> str:
    """
    将检索到的列信息组装成精炼的 JSON 格式，重点突出业务含义和数据分布。
    """
    if not retrieved_columns:
        return "No schema information found."

    tables_structure = {}

    for col in retrieved_columns:
        table_name = col.get("table") or col.get("table_name")
        col_name = col.get("column") or col.get("column_name")

        # 1. 获取元数据 (兼容不同来源的 key)
        # 如果是 profile_schema_enhanced 生成的，通常在 metadata 字段里
        meta = col.get("metadata", col)

        # 2. 提取高价值信息
        col_info = {
            "type": meta.get("data_type") or meta.get("column_type", "UNKNOWN"),
            "comment": meta.get("column_comment") or col.get("desc", ""),
        }

        # [A] 业务语义 (优先用 business_meaning，没有则用 ai_description)
        biz_meaning = meta.get("business_meaning")
        ai_desc = meta.get("ai_description")
        if biz_meaning:
            col_info["meaning"] = biz_meaning
        elif ai_desc:
            col_info["description"] = ai_desc

        # [B] 数据分布 (非常重要，用于 WHERE 条件)
        dist = meta.get("value_distribution")
        if dist:
            # 只取 Top 5 分布，节省 Token
            top_dist = dict(sorted(dist.items(), key=lambda x: x[1], reverse=True)[:5])
            col_info["distribution_top5"] = top_dist

        # [C] 数值范围 (用于 > < 比较)
        numeric = meta.get("numeric_stats")
        if numeric:
            col_info["stats"] = numeric

        # [D] 样本值 (兜底)
        samples = meta.get("sample_values") or col.get("sample_values")
        if samples and not dist:  # 如果有分布就不展示样本了，省空间
            col_info["samples"] = samples[:3]

        # 3. 按表分组
        if table_name not in tables_structure:
            tables_structure[table_name] = []

        # 把列名作为 key 或者包含在对象里均可，这里作为 key 更直观
        tables_structure[table_name].append({col_name: col_info})

    # 序列化为 JSON 字符串
    return json.dumps(tables_structure, ensure_ascii=False, indent=2)


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


# ==============================================================================
# 🚀 核心生成节点
# ==============================================================================

async def generate_node(state: AgentState) -> Dict[str, Any]:
    """
    Generator Node (JSON Schema Version)
    """
    question = state.get("question", "")
    history = state.get("history", [])

    # 状态标志
    execution_error = state.get("execution_error")
    generated_sql = state.get("generated_sql")
    feedback = state.get("feedback", "")
    verified = state.get("verified", True)

    # 获取原始检索结果 (List[Dict])
    retrieved_columns = state.get("retrieved_columns", [])

    # 🔥 核心修改：将 Schema 转换为 Rich JSON 字符串
    rich_schema_json = _build_rich_schema_json(retrieved_columns)

    # 其他上下文
    business_rules = state.get("business_rules", [])
    value_matches = state.get("value_matches", [])
    few_shot_examples = state.get("few_shot_examples", [])
    join_paths = state.get("join_paths", [])

    # 历史记录
    history_context = _format_history(history) if history else ""

    # ----------------------------------------------------
    # A. 规则与约束组装
    # ----------------------------------------------------
    # 1. 业务规则
    general_rules = []
    if business_rules:
        for r in business_rules:
            txt = r.get("content") if isinstance(r, dict) else str(r)
            if txt.strip(): general_rules.append(f"- {txt.strip()}")
    rules_str = "\n".join(general_rules) if general_rules else "No specific business rules."

    # 2. 值匹配 (Constraints)
    constraints = []
    if value_matches:
        for m in value_matches:
            # 过滤一下格式
            s = str(m).strip()
            if "CONSTRAINT" in s:
                constraints.append(f"🔴 {s}")
            else:
                constraints.append(f"🟡 {s}")
    constraints_str = "\n".join(constraints) if constraints else "No mandatory value filters."

    # 3. Few-Shot
    few_shot_str = "No examples."
    if few_shot_examples:
        ex_lines = []
        for i, ex in enumerate(few_shot_examples):
            ex_lines.append(f"Example {i + 1}:\nQ: {ex.get('question')}\nSQL: {ex.get('sql')}")
        few_shot_str = "\n\n".join(ex_lines)

    # 4. Join Paths
    paths_str = "\n".join([str(p) for p in join_paths[:3]]) if join_paths else "Auto-detect based on schema FKs."

    # ----------------------------------------------------
    # B. 提示词构造
    # ----------------------------------------------------
    llm = get_llm(model_name=settings.LLM_MODEL)
    final_prompt = ""

    # 确定错误上下文
    current_error = None
    if execution_error:
        current_error = f"Execution Error: {execution_error}"
    elif not verified and feedback:
        current_error = f"Feedback: {feedback}"

    # 🟢 修复模式
    if current_error and generated_sql:
        logger.info(f"🔧 [Generator] Entering Repair Mode...")
        # 注意：这里我们把 rich_schema_json 传给 schema_context
        # 你的 prompts.py 里的 SQL_REPAIR_PROMPT 需要能接受 json 格式的 schema
        # 通常大模型对 JSON 自适应很好，不需要改 prompt template 里的文字
        final_prompt = SQL_REPAIR_PROMPT.format(
            question=question,
            schema_context=rich_schema_json,  # 🔥 传入 JSON
            rules_context=rules_str,
            previous_sql=generated_sql,
            error_msg=current_error
        )

    # 🔵 生成模式
    else:
        logger.info(f"🎨 [Generator] Generating from scratch...")
        final_prompt = GEN_SQL_PROMPT.format(
            history_context=history_context,
            schema_context=rich_schema_json,  # 🔥 传入 JSON
            constraints_context=constraints_str,
            rules_context=rules_str,
            join_paths_context=paths_str,
            few_shot_context=few_shot_str,
            question=question
        )

    # ----------------------------------------------------
    # C. LLM 调用
    # ----------------------------------------------------
    try:
        messages = [
            SystemMessage(
                content="You are a SQL expert. The schema is provided in JSON format with rich semantic info. Use it to write accurate SQL."),
            HumanMessage(content=final_prompt)
        ]

        response = await llm.ainvoke(messages)
        content = response.content

        # 解析 SQL (保持原有逻辑)
        sql = ""
        thought = ""

        sql_match = re.search(r"```sql\n(.*?)\n```", content, re.DOTALL)
        if sql_match:
            sql = sql_match.group(1).strip()
        else:
            clean = content.replace("```sql", "").replace("```", "").strip()
            if "SELECT" in clean.upper(): sql = clean

        audit_match = re.search(r"```audit\n(.*?)\n```", content, re.DOTALL)
        if audit_match: thought = audit_match.group(1).strip()

        if sql and not sql.endswith(";"): sql += ";"

        logger.info(f"📝 [Generator] SQL Generated")

        return {
            "generated_sql": sql,
            "generated_thought": thought,
            "final_answer": sql,
            "execution_error": None,
            "is_executable": None
        }

    except Exception as e:
        logger.error(f"❌ Generation Failed: {e}")
        return {"generated_sql": "", "error_message": str(e)}