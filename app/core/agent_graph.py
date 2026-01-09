import datetime  # 🔥 新增
import asyncio  # 🔥 新增
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI

# 🔥 1. 引入统一配置和 Logger
from app.core.config import settings
from app.core.logger import logger

from app.core.prompts import INTENT_PROMPT, GEN_SQL_PROMPT, ERROR_CLASSIFY_PROMPT
from app.core.state import AgentState, IntentOutput, SQLOutput, ErrorOutput
# 引入检索工具 (现在是 async 的了)
from app.api.v1.retrieve_tables import retrieve_tables as retrieve_tool
# 🔥 引入 execute_sql_explain 和 append_event (用于写日志)
from app.modules.sql.executor import execute_sql_explain, append_event

# --- 初始化模型 ---
llm = ChatOpenAI(
    model=settings.LLM_MODEL,
    temperature=0,
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL,
    max_tokens=2048
)


# ==========================================
# Nodes (全部升级为 async def)
# ==========================================

async def intent_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    question = state["question"]  # 获取用户提问

    logger.info("[Step 0] Intent Check", extra={"trace_id": trace_id})

    # 🔥🔥🔥【核心修改】把用户提问写入审计日志 events.jsonl 🔥🔥🔥
    try:
        append_event({
            "trace_id": trace_id,
            "user_id": "real_user",  # 标记这是真实用户
            "route": "USER_INPUT",  # 标记这是用户输入的环节
            "sql": question,  # 把“自然语言问题”存在 sql 字段里（或者你也可以加个 text 字段，但复用 sql 字段比较省事）
            "latency_ms": 0,
            "truncated": False,
            "error": None,
            "ts_iso": datetime.datetime.utcnow().isoformat(),
        })
    except Exception as e:
        logger.warning(f"Failed to log user input: {e}")

    # --- 下面是原有的 LLM 逻辑 ---
    prompt = INTENT_PROMPT.format(question=question)

    # 异步调用 LLM
    res = await llm.with_structured_output(IntentOutput).ainvoke(prompt)

    return {"intent": res.intent}


async def retrieve_node(state: AgentState):
    logger.info("[Step 1] Retrieving Tables", extra={"trace_id": state.get("trace_id")})

    # 🔥 改为 await 调用 (因为 retrieve_tables 现在是 async 函数)
    # 注意：这里不需要手动 append_event，因为 retrieve_tables 内部已经加了日志记录
    tables = await retrieve_tool(state["question"], topk=5, trace_id=state.get("trace_id", "N/A"))

    return {
        "candidate_tables": tables,
        "retry_count": 0,
        "validation_error": None
    }


async def generate_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    retry_count = state.get("retry_count", 0)
    logger.info(f"[Step 2] Generating SQL (Attempt {retry_count + 1})", extra={"trace_id": trace_id})

    # 🔥🔥🔥【核心修改点】Schema 注入逻辑优化 🔥🔥🔥
    # 1. 强行加上 db 前缀 (默认 dbops_proxy)
    # 2. 缩短 text 长度，防止物理表名干扰
    schema_lines = []
    for t in state["candidate_tables"]:
        # 1. 尝试从检索结果获取 db，如果没有，才回退到 unknown (或者你可以回退到 dbops_proxy 作为保底)
        db_name = t.get('db')
        table_name = t['logical_table']

        # 2. 动态拼接：如果有库名就拼库名，没库名就裸奔
        full_table_name = f"{db_name}.{table_name}" if db_name else table_name

        # 3. 截断 text，防止物理表名干扰
        safe_info = t.get('text', '')[:500]

        schema_lines.append(f"Table: {full_table_name}\nInfo: {safe_info}")

    schema_context = "\n".join(schema_lines)

    # 历史对话上下文 (保持不变)
    history_list = state.get("chat_history", [])
    history_context = "\n".join(history_list[-6:]) if history_list else "无"

    # 错误上下文 (保持不变)
    error_context = "无"
    if state.get("validation_error"):
        error_context = (
            f"⚠️ 上一次生成的 SQL 执行失败！\n"
            f"错误信息: {state['validation_error']}\n"
            f"请根据错误信息修正 SQL。"
        )

    prompt = GEN_SQL_PROMPT.format(
        schema_context=schema_context,
        history_context=history_context,
        question=state["question"],
        error_context=error_context
    )

    # 异步调用 LLM
    res = await llm.with_structured_output(SQLOutput).ainvoke(prompt)

    logger.info(f"🤖 [Generated SQL] {res.sql}", extra={"trace_id": trace_id})

    # 记录日志
    try:
        append_event({
            "trace_id": trace_id,
            "user_id": "ai_agent",
            "route": "GENERATE",
            "sql": res.sql,
            "latency_ms": 0,
            "truncated": False,
            "error": None,
            "assumptions": res.assumptions,
            "ts_iso": datetime.datetime.utcnow().isoformat(),
        })
    except Exception as e:
        logger.warning(f"Failed to log generate event: {e}")

    return {
        "generated_sql": res.sql,
        "sql_confidence": res.confidence,
        "tables_used": res.tables_used,
        "assumptions": res.assumptions,
        "retry_count": retry_count + 1,
    }

async def validate_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    logger.info("[Step 3] Validating SQL (EXPLAIN)", extra={"trace_id": trace_id})

    sql = state["generated_sql"]

    try:
        # execute_sql_explain 内部是同步的 pymysql，但可以直接在 async 函数里调用
        # 它内部已经集成了 append_event，所以这里不需要再写日志
        execute_sql_explain(sql, trace_id=trace_id)
        return {"validation_error": None}
    except Exception as e:
        error_msg = str(e)
        logger.warning(f"Validation Failed: {error_msg}", extra={"trace_id": trace_id})
        return {"validation_error": error_msg}


async def classify_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    logger.info("[Step 4] Classifying Error", extra={"trace_id": trace_id})

    prompt = ERROR_CLASSIFY_PROMPT.format(
        sql=state["generated_sql"],
        error_msg=state["validation_error"]
    )

    # 异步调用 LLM
    res = await llm.with_structured_output(ErrorOutput).ainvoke(prompt)
    logger.info(f"Error Type: {res.error_type}", extra={"trace_id": trace_id})

    # 🔥🔥🔥【新增】记录错误分类决策 🔥🔥🔥
    try:
        append_event({
            "trace_id": trace_id,
            "user_id": "system_classifier",
            "route": "CLASSIFY_ERROR",   # 标记动作
            "sql": state["generated_sql"], # 记录出错的 SQL
            "error": state["validation_error"], # 记录报错信息
            "result_summary": f"Type: {res.error_type}, Keywords: {res.search_keywords}", # 记录分类结果
            "latency_ms": 0,
            "truncated": False,
            "ts_iso": datetime.datetime.utcnow().isoformat(),
        })
    except Exception:
        pass

    return {
        "error_type": res.error_type,
        "repair_keywords": res.search_keywords
    }

    # 🔥 改为异步调用 ainvoke
    res = await llm.with_structured_output(ErrorOutput).ainvoke(prompt)
    logger.info(f"Error Type: {res.error_type}", extra={"trace_id": trace_id})

    return {
        "error_type": res.error_type,
        "repair_keywords": res.search_keywords
    }


async def repair_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    keywords = state['repair_keywords']
    logger.info(f"[Repair] Searching supplement: {keywords}", extra={"trace_id": trace_id})

    new_tables = []
    current_full_names = {t.get('full_name') for t in state["candidate_tables"]}

    for kw in keywords:
        repair_query = f"{state['question']} {kw}"
        # 这里调用的 retrieve_tool 内部会记一条 RETRIEVE 日志
        found = await retrieve_tool(repair_query, topk=2, trace_id=trace_id)

        for t in found:
            t_full_name = t.get('full_name')
            if t_full_name and t_full_name not in current_full_names:
                new_tables.append(t)
                current_full_names.add(t_full_name)

    logger.info(f"[Repair] Added {len(new_tables)} new tables.", extra={"trace_id": trace_id})

    # 🔥🔥🔥【新增】记录修复摘要 🔥🔥🔥
    try:
        append_event({
            "trace_id": trace_id,
            "user_id": "system_repair",
            "route": "REPAIR_ACTION", # 标记动作
            "sql": f"Repair Keywords: {keywords}", # 记录用了什么词修补
            "result_summary": f"Added {len(new_tables)} tables to context", # 记录结果
            "latency_ms": 0,
            "truncated": False,
            "error": None,
            "ts_iso": datetime.datetime.utcnow().isoformat(),
        })
    except Exception:
        pass

    return {
        "candidate_tables": state["candidate_tables"] + new_tables,
        "retry_count": state["retry_count"]
    }


# ==========================================
# Edges & Graph (这部分逻辑不变)
# ==========================================

def route_after_intent(state: AgentState):
    if state["intent"] == "data_query":
        return "retrieve"
    return END


def route_after_validate(state: AgentState):
    if not state.get("validation_error"):
        return END
    return "classify"


def route_after_classify(state: AgentState):
    if state["retry_count"] >= 3:
        logger.warning("❌ Max retries reached. Giving up.", extra={"trace_id": state.get("trace_id")})
        return END

    error_type = state["error_type"]
    if error_type == "NON_FIXABLE":
        return END
    if error_type == "SYNTAX_ERROR" or error_type == "MISSING_COLUMN":
        return "generate"
    return "repair"


workflow = StateGraph(AgentState)
# 添加节点 (现在它们都是 async 的了)
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