import os
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI

# 🔥 1. 引入统一配置和 Logger
from app.core.config import settings
from app.core.logger import logger

from app.core.prompts import INTENT_PROMPT, GEN_SQL_PROMPT, ERROR_CLASSIFY_PROMPT
from app.core.state import AgentState, IntentOutput, SQLOutput, ErrorOutput
from app.api.v1.retrieve_tables import retrieve_tables as retrieve_tool
from app.modules.sql.executor import execute_sql_explain

# --- 初始化模型 ---
llm = ChatOpenAI(
    model=settings.LLM_MODEL,
    temperature=0,
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL,
    max_tokens=2048
)


# ==========================================
# Nodes
# ==========================================

def intent_node(state: AgentState):
    logger.info("[Step 0] Intent Check", extra={"trace_id": state.get("trace_id")})
    prompt = INTENT_PROMPT.format(question=state["question"])
    res = llm.with_structured_output(IntentOutput).invoke(prompt)
    return {"intent": res.intent}


def retrieve_node(state: AgentState):
    logger.info("[Step 1] Retrieving Tables", extra={"trace_id": state.get("trace_id")})
    tables = retrieve_tool(state["question"], topk=5)
    return {
        "candidate_tables": tables,
        "retry_count": 0,
        "validation_error": None
    }


def generate_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    retry_count = state.get("retry_count", 0)
    logger.info(f"[Step 2] Generating SQL (Attempt {retry_count + 1})", extra={"trace_id": trace_id})

    # 🔥🔥🔥 核心修改：Schema 注入时带上 db 名字 🔥🔥🔥
    schema_context = "\n".join([
        f"Table: {t.get('db', 'unknown_db')}.{t['logical_table']}\nInfo: {t.get('text', '')[:2000]}..."
        for t in state["candidate_tables"]
    ])

    # 2. 🔥 构造历史对话上下文
    history_list = state.get("chat_history", [])
    # 只取最近 6 轮，防止 Prompt 爆炸
    history_context = "\n".join(history_list[-6:]) if history_list else "无"

    # 3. 构造错误上下文
    error_context = "无"
    if state.get("validation_error"):
        error_context = (
            f"⚠️ 上一次生成的 SQL 执行失败！\n"
            f"错误信息: {state['validation_error']}\n"
            f"请根据错误信息修正 SQL。"
        )

    prompt = GEN_SQL_PROMPT.format(
        schema_context=schema_context,
        history_context=history_context, # 🔥 注入历史
        question=state["question"],
        error_context=error_context
    )

    res = llm.with_structured_output(SQLOutput).invoke(prompt)

    return {
        "generated_sql": res.sql,
        "sql_confidence": res.confidence,
        "tables_used": res.tables_used,
        "assumptions": res.assumptions,
        "retry_count": retry_count + 1,
    }


def validate_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    logger.info("[Step 3] Validating SQL (EXPLAIN)", extra={"trace_id": trace_id})

    sql = state["generated_sql"]

    try:
        execute_sql_explain(sql, trace_id=trace_id)
        return {"validation_error": None}
    except Exception as e:
        error_msg = str(e)
        logger.warning(f"Validation Failed: {error_msg}", extra={"trace_id": trace_id})
        return {"validation_error": error_msg}


def classify_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    logger.info("[Step 4] Classifying Error", extra={"trace_id": trace_id})

    prompt = ERROR_CLASSIFY_PROMPT.format(
        sql=state["generated_sql"],
        error_msg=state["validation_error"]
    )

    res = llm.with_structured_output(ErrorOutput).invoke(prompt)
    logger.info(f"Error Type: {res.error_type}", extra={"trace_id": trace_id})

    return {
        "error_type": res.error_type,
        "repair_keywords": res.search_keywords
    }


def repair_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    keywords = state['repair_keywords']
    logger.info(f"[Repair] Searching supplement: {keywords}", extra={"trace_id": trace_id})

    new_tables = []
    current_full_names = {t.get('full_name') for t in state["candidate_tables"]}

    for kw in keywords:
        repair_query = f"{state['question']} {kw}"
        found = retrieve_tool(repair_query, topk=2)

        for t in found:
            t_full_name = t.get('full_name')
            if t_full_name and t_full_name not in current_full_names:
                new_tables.append(t)
                current_full_names.add(t_full_name)

    logger.info(f"[Repair] Added {len(new_tables)} new tables.", extra={"trace_id": trace_id})

    return {
        "candidate_tables": state["candidate_tables"] + new_tables,
        "retry_count": state["retry_count"]
    }


# ==========================================
# Edges & Graph
# ==========================================
# ... (Edges 代码逻辑正确，无需变动，保持原样即可) ...
def route_after_intent(state: AgentState):
    if state["intent"] == "data_query":
        return "retrieve"
    return END


def route_after_validate(state: AgentState):
    if not state.get("validation_error"):
        return END
    return "classify"


def route_after_classify(state: AgentState):
    # 🔥 核心修改：允许重试 3 次 (0, 1, 2)
    if state["retry_count"] >= 3:
        logger.warning("❌ Max retries reached. Giving up.", extra={"trace_id": state.get("trace_id")})
        return END

    error_type = state["error_type"]

    if error_type == "NON_FIXABLE":
        return END

    # 如果是语法错误，不需要补搜，直接带着报错信息回 Generate 重写
    if error_type == "SYNTAX_ERROR" or error_type == "MISSING_COLUMN":
        # 手动增加一次重试计数 (因为没有经过 repair_node)
        return "generate"

        # 如果是缺表，去 Repair 节点补搜
    return "repair"


workflow = StateGraph(AgentState)
workflow.add_node("intent", intent_node)
workflow.add_node("retrieve", retrieve_node)
workflow.add_node("generate", generate_node)
workflow.add_node("validate", validate_node)
workflow.add_node("classify", classify_node)
workflow.add_node("repair", repair_node)

workflow.set_entry_point("intent")
workflow.add_conditional_edges("intent", route_after_intent)
workflow.add_edge("retrieve", "generate")
workflow.add_edge("generate", "validate")
workflow.add_conditional_edges("validate", route_after_validate)
workflow.add_conditional_edges("classify", route_after_classify, {"repair": "repair", "generate": "generate", END: END})
workflow.add_edge("repair", "generate")

app = workflow.compile()