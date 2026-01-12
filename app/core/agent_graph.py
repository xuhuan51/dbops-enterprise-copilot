import datetime
import warnings
import re
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core._api import LangChainBetaWarning

warnings.filterwarnings("ignore", category=LangChainBetaWarning)

from app.core.config import settings
from app.core.logger import logger

# 引入 Prompt
from app.core.prompts import (
    INTENT_PROMPT,
    GEN_SQL_PROMPT,
    ERROR_CLASSIFY_PROMPT,
    REFLECTION_PROMPT,
    QUERY_REWRITE_PROMPT
)
from app.core.state import AgentState, IntentOutput, SQLOutput, ErrorOutput, ReflectionOutput

from app.api.v1.retrieve_tables import retrieve_tables as retrieve_tool
from app.modules.sql.executor import execute_sql_explain, append_event

llm = ChatOpenAI(
    model=settings.LLM_MODEL,
    temperature=0,
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL,
    max_tokens=2048
)


# ==========================================
# Nodes (节点定义)
# ==========================================

async def intent_node(state: AgentState):
    """Step 0: 识别用户意图"""
    trace_id = state.get("trace_id", "N/A")
    question = state["question"]
    logger.info("[Step 0] Intent Check", extra={"trace_id": trace_id})

    # 记录 Event
    try:
        append_event({
            "trace_id": trace_id, "user_id": "real_user", "route": "USER_INPUT",
            "sql": question, "ts_iso": datetime.datetime.utcnow().isoformat(),
        })
    except:
        pass

    prompt = INTENT_PROMPT.format(question=question)
    res = await llm.with_structured_output(IntentOutput).ainvoke(prompt)
    return {"intent": res.intent}


async def rewrite_node(state: AgentState):
    """Step 0.5: 问题改写 (翻译官)"""
    trace_id = state.get("trace_id", "N/A")
    question = state["question"]

    logger.info("[Step 0.5] Query Rewriting", extra={"trace_id": trace_id})

    # 调用 LLM 进行发散联想
    prompt = QUERY_REWRITE_PROMPT.format(question=question)
    response = await llm.ainvoke(prompt)
    rewritten_query = response.content.strip()

    logger.info(f"🔄 [Rewriter] Origin: {question} -> New: {rewritten_query}", extra={"trace_id": trace_id})

    # 更新 State
    return {"search_query": rewritten_query}


async def retrieve_node(state: AgentState):
    """Step 1: 检索表结构 (RAG)"""
    trace_id = state.get("trace_id", "N/A")
    logger.info("[Step 1] Retrieving Tables", extra={"trace_id": trace_id})

    # 优先使用改写后的 Query
    query_text = state.get("search_query") or state["question"]

    tables = await retrieve_tool(query_text, topk=5, trace_id=trace_id)
    return {"candidate_tables": tables, "retry_count": 0, "validation_error": None}


async def generate_node(state: AgentState):
    """Step 2: 生成 SQL"""
    trace_id = state.get("trace_id", "N/A")
    retry_count = state.get("retry_count", 0)
    logger.info(f"[Step 2] Generating SQL (Attempt {retry_count + 1})", extra={"trace_id": trace_id})

    # --- 智能 Schema 拼接 ---
    schema_lines = []
    for t in state["candidate_tables"]:
        table_name = t['logical_table']
        full_text = t.get('text', '')

        MAX_LEN = 2000
        if len(full_text) > MAX_LEN:
            field_start = full_text.find("字段结构:")
            if field_start != -1:
                header = full_text[:field_start]
                body = full_text[field_start:field_start + 1500]
                safe_info = header + body + "\n...(Samples Truncated)"
            else:
                safe_info = full_text[:MAX_LEN]
        else:
            safe_info = full_text

        schema_lines.append(f"Table: {table_name}\nInfo: {safe_info}")

    schema_context = "\n".join(schema_lines)

    # --- 注入多轮对话历史 ---
    history_list = state.get("chat_history", [])
    if history_list:
        history_context = "\n".join(history_list[-5:])
    else:
        history_context = "无 (这是第一轮对话)"

    # 上下文处理 (Error Context)
    error_context = "无"
    if state.get("reflection_passed") is False:
        error_context = f"⚠️ 之前的逻辑被反思驳回：{state.get('reflection_feedback')}"
    elif state.get("validation_error"):
        error_msg = state['validation_error']
        if "Unknown column" in error_msg or "MISSING_COLUMN" in str(state.get("error_type", "")):
            col_match = re.search(r"['`](\w+)['`]", error_msg)
            if col_match:
                missing_col = col_match.group(1)
                error_context = f"⚠️ 字段 '{missing_col}' 不存在。请检查 Schema，如果确实没有，输出: SELECT 'NEED_SCHEMA_FIELD: {missing_col}' AS error;"
            else:
                error_context = f"⚠️ 字段错误：{error_msg}"
        else:
            error_context = f"⚠️ 执行报错：{error_msg}"

    prompt = GEN_SQL_PROMPT.format(
        schema_context=schema_context,
        history_context=history_context,
        question=state["question"],
        error_context=error_context
    )

    res = await llm.with_structured_output(SQLOutput).ainvoke(prompt)
    logger.info(f"🤖 [Generated SQL] {res.sql}", extra={"trace_id": trace_id})

    try:
        append_event({
            "trace_id": trace_id, "user_id": "ai_agent", "route": "GENERATE",
            "sql": res.sql, "assumptions": res.assumptions, "ts_iso": datetime.datetime.utcnow().isoformat(),
        })
    except:
        pass

    return {
        "generated_sql": res.sql,
        # 🔥 修改: 将结果同步到 final_answer，格式必须与 api/agent_query.py 对齐
        "final_answer": f"SQL_RESULT:{res.sql}",
        "retry_count": retry_count + 1,
        "validation_error": None,
        "reflection_passed": None
    }


async def reflection_node(state: AgentState):
    """Step 2.5: 自我反思"""
    trace_id = state.get("trace_id", "N/A")
    logger.info("[Step 2.5] Reflection (Self-Correction)", extra={"trace_id": trace_id})

    # 计数
    current_count = state.get("reflection_count", 0) + 1

    schema_summary = "\n".join([
        f"Table: {t['logical_table']}\nSchema: {t.get('text', '')[:800]}"
        for t in state["candidate_tables"]
    ])

    prompt = REFLECTION_PROMPT.format(
        question=state["question"],
        schema_summary=schema_summary,
        sql=state["generated_sql"]
    )

    res = await llm.with_structured_output(ReflectionOutput).ainvoke(prompt)

    try:
        append_event({
            "trace_id": trace_id, "user_id": "system_reflection", "route": "REFLECTION",
            "sql": state["generated_sql"], "result_summary": f"Valid: {res.is_valid}, Reason: {res.reason}",
            "ts_iso": datetime.datetime.utcnow().isoformat(),
        })
    except:
        pass

    if res.is_valid:
        logger.info("✅ Reflection Passed.", extra={"trace_id": trace_id})
        return {
            "reflection_passed": True,
            "reflection_feedback": None,
            "reflection_count": current_count
        }
    else:
        logger.warning(f"❌ Reflection Failed: {res.reason}", extra={"trace_id": trace_id})
        return {
            "reflection_passed": False,
            "reflection_feedback": res.missing_info,
            "repair_keywords": res.suggested_search_keywords,
            "reflection_count": current_count
        }


async def validate_node(state: AgentState):
    """Step 3: 语法验证 (Explain)"""
    trace_id = state.get("trace_id", "N/A")
    logger.info("[Step 3] Validating SQL (EXPLAIN)", extra={"trace_id": trace_id})
    try:
        execute_sql_explain(state["generated_sql"], trace_id=trace_id)
        return {"validation_error": None}
    except Exception as e:
        logger.warning(f"Validation Failed: {e}", extra={"trace_id": trace_id})
        return {"validation_error": str(e)}


async def classify_node(state: AgentState):
    """Step 4: 错误分类"""
    trace_id = state.get("trace_id", "N/A")
    logger.info("[Step 4] Classifying Error", extra={"trace_id": trace_id})
    prompt = ERROR_CLASSIFY_PROMPT.format(sql=state["generated_sql"], error_msg=state["validation_error"])
    res = await llm.with_structured_output(ErrorOutput).ainvoke(prompt)
    logger.info(f"Error Type: {res.error_type}", extra={"trace_id": trace_id})
    return {"error_type": res.error_type, "repair_keywords": res.search_keywords}


async def repair_node(state: AgentState):
    """Repair: 补充检索"""
    trace_id = state.get("trace_id", "N/A")
    keywords = state.get('repair_keywords', [])

    logger.info(f"[Repair] Searching supplement: {keywords}", extra={"trace_id": trace_id})

    new_tables = []
    current_full_names = {t.get('full_name') for t in state["candidate_tables"]}

    for kw in keywords:
        repair_query = f"{kw} table schema"
        found = await retrieve_tool(repair_query, topk=2, trace_id=trace_id)
        for t in found:
            t_full_name = t.get('full_name')
            if t_full_name and t_full_name not in current_full_names:
                new_tables.append(t)
                current_full_names.add(t_full_name)

    logger.info(f"[Repair] Added {len(new_tables)} new tables.", extra={"trace_id": trace_id})
    return {"candidate_tables": state["candidate_tables"] + new_tables}


async def fallback_node(state: AgentState):
    """🔥 Step 5: 最终兜底 (当尝试多次仍失败时，生成友好回复)"""
    trace_id = state.get("trace_id", "N/A")
    logger.info("[Step 5] Fallback (Give Up)", extra={"trace_id": trace_id})

    # 获取最后一次的反思反馈
    feedback = state.get("reflection_feedback", "无法生成有效的 SQL 查询")

    # 构造友好的回复
    friendly_msg = (
        f"🤔 抱歉，我尝试查询了数据，但发现缺少支持该问题的字段或表信息。\n"
        f"原因分析: {feedback}\n\n"
        f"💡 建议：您可以尝试询问现有数据（如：订单金额、用户注册时间、商品名称等），或者联系管理员补充相关数据源。"
    )

    # 返回非数据意图，防止 API 解析 SQL
    return {
        "final_answer": friendly_msg,
        "intent": "non_data"
    }


# ==========================================
# Edges & Routing (工作流定义)
# ==========================================

def route_after_intent(state: AgentState):
    if state["intent"] == "data_query":
        return "rewrite"
    return END


def route_after_reflection(state: AgentState):
    # 1. 如果反思通过，正常走下一步
    if state.get("reflection_passed"):
        return "validate"

    # 2. 🔥 熔断 -> 去兜底节点 (而不是直接 END)
    if state.get("reflection_count", 0) >= 3:
        logger.error("🛑 Reflection Loop Limit Reached. Routing to Fallback.")
        return "fallback"

    # 3. 如果没通过且没超限，去修补
    return "repair"


def route_after_validate(state: AgentState):
    if not state.get("validation_error"): return END
    return "classify"


def route_after_classify(state: AgentState):
    if state["retry_count"] >= 3: return END
    if state["error_type"] == "NON_FIXABLE": return END
    if state["error_type"] in ["SYNTAX_ERROR", "MISSING_COLUMN"]: return "generate"
    return "repair"


# 构建图
workflow = StateGraph(AgentState)

# 添加节点
workflow.add_node("intent", intent_node)
workflow.add_node("rewrite", rewrite_node)
workflow.add_node("retrieve", retrieve_node)
workflow.add_node("generate", generate_node)
workflow.add_node("reflection", reflection_node)
workflow.add_node("validate", validate_node)
workflow.add_node("classify", classify_node)
workflow.add_node("repair", repair_node)
# 🔥 注册 Fallback 节点
workflow.add_node("fallback", fallback_node)

# 设置连线
workflow.set_entry_point("intent")

workflow.add_conditional_edges("intent", route_after_intent, {"rewrite": "rewrite", END: END})
workflow.add_edge("rewrite", "retrieve")
workflow.add_edge("retrieve", "generate")

workflow.add_edge("generate", "reflection")

# 🔥 更新路由表: 加入 fallback
workflow.add_conditional_edges("reflection", route_after_reflection,
                               {"validate": "validate", "repair": "repair", "fallback": "fallback"})

workflow.add_conditional_edges("validate", route_after_validate)
workflow.add_conditional_edges("classify", route_after_classify, {"repair": "repair", "generate": "generate", END: END})

workflow.add_edge("repair", "generate")

# 🔥 Fallback 结束后终止
workflow.add_edge("fallback", END)

app = workflow.compile()