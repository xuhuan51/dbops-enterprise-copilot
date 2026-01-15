import datetime
import warnings
import re
from typing import Literal
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core._api import LangChainBetaWarning
from langchain_core.output_parsers import JsonOutputParser

warnings.filterwarnings("ignore", category=LangChainBetaWarning)

from app.core.config import settings
from app.core.logger import logger

# Import Prompts
from app.core.prompts import (
    INTENT_CHECK_PROMPT,
    GEN_SQL_PROMPT,
    ERROR_CLASSIFY_PROMPT,
    REFLECTION_PROMPT,
    QUERY_REWRITE_PROMPT
)
from app.core.state import AgentState, IntentOutput, SQLOutput, ErrorOutput, ReflectionOutput

# Import Tools
from app.api.v1.retrieve_tables import retrieve_tables_advanced
from app.modules.sql.executor import execute_sql_explain, append_event, get_tables_columns

# ==========================================
# LLM Initialization
# ==========================================
llm = ChatOpenAI(
    model=settings.LLM_MODEL,
    temperature=0,
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL,
    max_tokens=2048
)


# ==========================================
# 🛠️ Utility Functions
# ==========================================

def _extract_columns_from_ddl(text: str) -> list[str]:
    """
    Fallback mechanism: Extract column names from DDL text using Regex
    when live database metadata fetching fails.
    """
    columns = []
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if not line: continue
        if line.upper().startswith(
                ("CREATE", "TABLE", ")", "PRIMARY", "KEY", "CONSTRAINT", "UNIQUE", "--", "INTO", "ENGINE")):
            continue
        match = re.match(r"^[`']?([a-zA-Z0-9_]+)[`']?", line)
        if match:
            col = match.group(1)
            if col.upper() not in ["AND", "OR", "ON", "IN", "NOT", "NULL", "DEFAULT", "COMMENT", "INSERT", "VALUES"]:
                columns.append(col)
    return columns


def _lint_sql_columns(sql: str, table_columns: dict) -> str | None:
    """
    Defense Line 2: Static SQL Linting.
    Checks if alias.column references exist in the whitelist.
    Returns the first hallucinated column name if found.
    """
    if not table_columns or not sql:
        return None

    sql_lower = sql.lower()
    table_columns_lower = {
        t_name: {c.lower() for c in cols}
        for t_name, cols in table_columns.items()
    }

    alias_map = {}
    table_pattern = r"(?:from|join)\s+(?:[`']?[\w]+[`']?\.)?[`']?([a-zA-Z0-9_]+)[`']?(?:\s+(?:as\s+)?)?([`']?[a-zA-Z0-9_]+[`']?)?"

    matches = re.finditer(table_pattern, sql_lower)
    for m in matches:
        t_name = m.group(1).strip('`\'"')
        alias = m.group(2).strip('`\'"') if m.group(2) else t_name
        alias_map[alias] = t_name

    col_pattern = r"([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)"
    col_matches = re.finditer(col_pattern, sql_lower)

    for m in col_matches:
        alias = m.group(1).strip('`\'"')
        col = m.group(2).strip('`\'"')

        if alias.isdigit() or col.isdigit() or col == "*":
            continue

        real_table = alias_map.get(alias)

        if real_table and real_table in table_columns_lower:
            whitelist = table_columns_lower[real_table]
            if col not in whitelist:
                logger.warning(f"🛡️ [Lint] Detected Hallucination: {real_table}.{col} does not exist!")
                return col

    return None


# ==========================================
# Nodes
# ==========================================

async def intent_node(state: AgentState):
    """Step 0: User Intent Recognition (健壮兼容版)"""
    trace_id = state.get("trace_id", "N/A")
    question = state["question"]

    # 1. 提取历史记录 (修复：兼容 String 和 LangChain Message 对象)
    history_objs = state.get("history", []) or []
    history_text = "无历史记录 (首轮对话)"

    if history_objs:
        recent_msgs = history_objs[-4:]
        formatted_list = []
        for msg in recent_msgs:
            # 🔥 核心修复：先判断类型，防止 AttributeError
            if isinstance(msg, str):
                formatted_list.append(f"History: {msg}")
            elif hasattr(msg, 'content'):
                role = "User" if getattr(msg, 'type', '') == "human" else "AI"
                formatted_list.append(f"{role}: {msg.content}")
            else:
                formatted_list.append(str(msg))

        history_text = "\n".join(formatted_list)

    logger.info(f"[Step 0] Intent Check | Context: {len(history_objs)} messages", extra={"trace_id": trace_id})

    # 2. 记录事件
    try:
        append_event({
            "trace_id": trace_id, "user_id": "real_user", "route": "USER_INPUT",
            "sql": question, "ts_iso": datetime.datetime.utcnow().isoformat(),
        })
    except:
        pass

    # 3. 调用 LLM
    try:
        parser = JsonOutputParser(pydantic_object=IntentOutput)
        format_instructions = parser.get_format_instructions()

        full_prompt = (
            f"{INTENT_CHECK_PROMPT.format(history=history_text, question=question)}\n\n"
            f"【重要输出要求】\n{format_instructions}\n"
            f"请直接输出纯 JSON，不要包含 Markdown 格式（如 ```json ... ```）。"
        )

        response = await llm.ainvoke(full_prompt)
        parsed_res = parser.parse(response.content)

        if isinstance(parsed_res, dict):
            intent = parsed_res.get("intent", "DATA_QUERY")
        else:
            intent = parsed_res.intent

        intent = intent.upper()

    except Exception as e:
        logger.warning(
            f"⚠️ Intent check failed: {repr(e)}. Content was: {response.content if 'response' in locals() else 'N/A'}")
        intent = "DATA_QUERY"

    logger.info(f"✅ Intent detected: {intent}", extra={"trace_id": trace_id})
    return {"intent": intent}


async def rewrite_node(state: AgentState):
    """Step 0.5: Query Rewriting & Expansion"""
    trace_id = state.get("trace_id", "N/A")
    question = state["question"]

    logger.info("[Step 0.5] Query Rewriting", extra={"trace_id": trace_id})

    prompt = QUERY_REWRITE_PROMPT.format(question=question)
    response = await llm.ainvoke(prompt)
    rewritten_query = response.content.strip()

    logger.info(f"🔄 [Rewriter] Origin: {question} -> New: {rewritten_query}", extra={"trace_id": trace_id})
    return {"search_query": rewritten_query}


async def retrieve_node(state: AgentState):
    """Step 1: Retrieve Tables & Metadata"""
    trace_id = state.get("trace_id", "N/A")
    current_retry = state.get("retry_count", 0)

    query_text = state.get("search_query") or state["question"]

    logger.info(f"[Step 1] Retrieving Tables for: '{query_text}'", extra={"trace_id": trace_id})

    try:
        candidate_tables = await retrieve_tables_advanced(query_text)
    except Exception as e:
        logger.error(f"Retrieval failed: {e}")
        candidate_tables = []

    table_names = [t.get('logical_table', t.get('table_name')) for t in candidate_tables]

    try:
        table_columns_dict = get_tables_columns(table_names)
    except Exception as e:
        logger.error(f"Metadata fetch failed: {e}")
        table_columns_dict = {}

    return {
        "candidate_tables": candidate_tables,
        "table_columns": table_columns_dict,
        "retry_count": current_retry,
        "validation_error": None,
        "sentinel_blocked": False
    }


async def generate_node(state: AgentState):
    """Step 2: Generate SQL (健壮版)"""
    trace_id = state.get("trace_id", "N/A")
    retry_count = state.get("retry_count", 0)
    logger.info(f"[Step 2] Generating SQL (Attempt {retry_count + 1})", extra={"trace_id": trace_id})

    candidate_tables = state.get("candidate_tables", [])
    table_columns = state.get("table_columns", {})

    # ============================================================
    # 🛡️ 防线 1: 零召回强制熔断 (Zero-Recall Circuit Breaker)
    # ============================================================
    if not candidate_tables:
        logger.warning(f"🛑 [Fail-Closed] No tables retrieved. Blocking LLM generation.", extra={"trace_id": trace_id})
        err_sql = "SELECT 'ERR::NO_RELEVANT_TABLE' AS error;"
        return {
            "generated_sql": err_sql,
            "final_answer": f"SQL_RESULT:{err_sql}",
            "retry_count": retry_count,
            "validation_error": None,
            "sentinel_blocked": False,
            "reflection_passed": True,
            "reflection_feedback": "System Logic: No tables found, correctly triggered Fail-Closed."
        }

    # ============================================================
    # 🛡️ 防线 1.5: 元数据兜底
    # ============================================================
    if candidate_tables and not table_columns:
        logger.warning("⚠️ [Meta Warning] Live DB metadata missing. Switch to RAG Text Parsing.")
        for t in candidate_tables:
            t_name = t['logical_table']
            text = t.get('text', '')
            parsed_cols = _extract_columns_from_ddl(text)
            if parsed_cols:
                table_columns[t_name] = parsed_cols

    if candidate_tables and not table_columns:
        err_msg = "Critical: Failed to retrieve metadata from both DB and RAG Text."
        logger.error(f"🛑 [Fail-Closed] {err_msg}")
        fake_sql = "SELECT 'NEED_SCHEMA_FIELD: System Metadata Error' AS error;"
        return {
            "generated_sql": fake_sql,
            "final_answer": f"SQL_RESULT:{fake_sql}",
            "retry_count": retry_count + 1,
            "validation_error": "Metadata Error",
            "sentinel_blocked": True
        }

    # 1. 准备 Schema 上下文
    schema_lines = []
    for t in candidate_tables:
        table_name = t['logical_table']
        full_text = t.get('text', '')[:2000]
        schema_lines.append(f"Table: {table_name}\nInfo: {full_text}")
    schema_context = "\n".join(schema_lines)

    whitelist_lines = [f"- {k}: [{', '.join(v)}]" for k, v in table_columns.items()]
    whitelist_context = "\n".join(whitelist_lines)

    # 🔥🔥 核心修复：历史记录处理兼容 String 和 Object 🔥🔥
    history_list = state.get("history", [])
    history_context = "None"
    if history_list:
        formatted_msgs = []
        for m in history_list[-5:]:
            if isinstance(m, str):
                formatted_msgs.append(f"History: {m}")
            elif hasattr(m, 'content'):
                role = "User" if getattr(m, 'type', '') == 'human' else "AI"
                formatted_msgs.append(f"{role}: {m.content}")
            else:
                formatted_msgs.append(str(m))
        history_context = "\n".join(formatted_msgs)

    # 2. 动态错误上下文
    error_context = "None"
    if state.get("reflection_passed") is False:
        error_context = f"⚠️ Previous logic rejected: {state.get('reflection_feedback')}"
    elif state.get("validation_error"):
        error_msg = state['validation_error']
        if "Unknown column" in error_msg:
            col_match = re.search(r"['`](\w+)['`]", error_msg)
            if col_match:
                missing_col = col_match.group(1)
                error_context = (
                    f"⚠️ Critical Error: Column '{missing_col}' does not exist.\n"
                    f"🛑 Status: Not in whitelist.\n"
                    f"👉 Action: MUST output error: SELECT 'ERR::NEED_SCHEMA_FIELD::{missing_col}' AS error;"
                )
        else:
            error_context = f"⚠️ Execution Error: {error_msg}"

    # 3. LLM 生成
    prompt = GEN_SQL_PROMPT.format(
        schema_context=schema_context,
        column_whitelist_context=whitelist_context,
        history_context=history_context,
        question=state["question"],
        error_context=error_context
    )

    try:
        res = await llm.with_structured_output(SQLOutput).ainvoke(prompt)
        generated_sql = res.sql
    except Exception as e:
        logger.error(f"Generate LLM failed: {e}")
        generated_sql = "SELECT 'Generation Failed' AS error;"
        res = SQLOutput(sql=generated_sql, assumptions="LLM Error")

    logger.info(f"🤖 [Generated SQL] {generated_sql}", extra={"trace_id": trace_id})

    # 4. 静态哨兵检查
    is_blocked = False
    reflection_result = {}

    if table_columns:
        missing_col = _lint_sql_columns(generated_sql, table_columns)
        if missing_col:
            is_blocked = True
            intent_keywords = state.get("search_query") or state["question"]
            logger.warning(f"🛑 [Sentinel] Lint failed for column: {missing_col}.")

            error_code = f"ERR::NEED_SCHEMA_FIELD::{missing_col}"
            error_message = f"{error_code} | Intent: {intent_keywords}"
            generated_sql = f"SELECT '{error_message}' AS error;"

            reflection_result = {
                "reflection_passed": False,
                "reflection_feedback": f"Sentinel Static Check Failed: Column '{missing_col}' does not exist.",
                "reflection_count": state.get("reflection_count", 0)
            }

            try:
                append_event({
                    "trace_id": trace_id, "user_id": "system_sentinel", "route": "LINT_BLOCK",
                    "sql": res.sql, "blocked_col": missing_col,
                    "feedback": reflection_result["reflection_feedback"],
                    "ts_iso": datetime.datetime.utcnow().isoformat(),
                })
            except:
                pass

    try:
        append_event({
            "trace_id": trace_id, "user_id": "ai_agent", "route": "GENERATE",
            "sql": generated_sql, "assumptions": res.assumptions, "ts_iso": datetime.datetime.utcnow().isoformat(),
        })
    except:
        pass

    return {
        "generated_sql": generated_sql,
        "final_answer": f"SQL_RESULT:{generated_sql}",
        "retry_count": retry_count + 1,
        "validation_error": None,
        "sentinel_blocked": is_blocked,
        **reflection_result
    }


async def reflection_node(state: AgentState):
    """Step 2.5: Self-Reflection"""
    trace_id = state.get("trace_id", "N/A")
    logger.info("[Step 2.5] Reflection", extra={"trace_id": trace_id})
    current_count = state.get("reflection_count", 0) + 1

    schema_summary = "\n".join([f"Table: {t['logical_table']}" for t in state["candidate_tables"]])

    prompt = REFLECTION_PROMPT.format(
        question=state["question"],
        schema_summary=schema_summary,
        sql=state["generated_sql"]
    )

    res = await llm.with_structured_output(ReflectionOutput).ainvoke(prompt)

    if res.is_valid:
        logger.info("✅ Reflection Passed.", extra={"trace_id": trace_id})
        return {"reflection_passed": True, "reflection_count": current_count}
    else:
        logger.warning(f"❌ Reflection Failed: {res.reason}", extra={"trace_id": trace_id})
        return {
            "reflection_passed": False,
            "reflection_feedback": res.missing_info,
            "suggested_search_keywords": res.suggested_search_keywords,
            "reflection_count": current_count
        }


async def validate_node(state: AgentState):
    """Step 3: Syntax Validation"""
    trace_id = state.get("trace_id", "N/A")
    logger.info("[Step 3] Validating SQL", extra={"trace_id": trace_id})
    try:
        execute_sql_explain(state["generated_sql"], trace_id=trace_id)
        return {"validation_error": None}
    except Exception as e:
        logger.warning(f"Validation Failed: {e}", extra={"trace_id": trace_id})
        return {"validation_error": str(e)}


async def classify_node(state: AgentState):
    """Step 4: Error Classification"""
    trace_id = state.get("trace_id", "N/A")
    prompt = ERROR_CLASSIFY_PROMPT.format(sql=state["generated_sql"], error_msg=state["validation_error"])
    res = await llm.with_structured_output(ErrorOutput).ainvoke(prompt)
    return {"error_type": res.error_type}


async def repair_node(state: AgentState):
    """Step 3: Robust Repair"""
    trace_id = state.get("trace_id", "N/A")
    retry_count = state.get("retry_count", 0)
    question = state.get("question", "")

    suggested_keywords = state.get("suggested_search_keywords")
    feedback = state.get("reflection_feedback")
    error_context = f"{state.get('error', '')} {state.get('validation_error', '')}"

    logger.info(f"🔧 [Repair] Analyzing failure... (Attempt {retry_count + 1})", extra={"trace_id": trace_id})

    repair_query = ""
    strategy = "UNKNOWN"

    if suggested_keywords and len(str(suggested_keywords).strip()) > 2:
        repair_query = suggested_keywords
        strategy = "REFLECTION_SUGGESTION"
    elif feedback and "缺少" in str(feedback):
        repair_query = f"{feedback} schema definition"
        strategy = "REFLECTION_FEEDBACK"
    elif error_context:
        if "ERR::NEED_SCHEMA_FIELD" in error_context:
            match = re.search(r"ERR::NEED_SCHEMA_FIELD::(\w+)", error_context)
            if match:
                missing_col = match.group(1)
                intent = state.get("search_query") or question
                repair_query = f"table containing column {missing_col} for {intent}"
                strategy = "SENTINEL_LINT"
        elif "Unknown column" in error_context:
            match = re.search(r"Unknown column ['`]([\w\.]+)['`]", error_context)
            if match:
                full_col = match.group(1)
                bad_col = full_col.split(".")[-1] if "." in full_col else full_col
                repair_query = f"definition of column {bad_col}"
                strategy = "MYSQL_ERROR"
        elif "doesn't exist" in error_context:
            repair_query = f"correct table name for {question}"
            strategy = "TABLE_NOT_FOUND"

    if not repair_query:
        repair_query = f"relevant tables for: {question}"
        strategy = "FALLBACK_GENERIC"

    logger.info(f"🔧 [Repair] Strategy: {strategy} | Search: '{repair_query}'", extra={"trace_id": trace_id})

    new_tables_added = []
    new_table_cols = {}
    try:
        found_tables = await retrieve_tables_advanced(repair_query)
        current_tables = state.get("candidate_tables", [])
        current_names = {t.get('logical_table', t.get('table_name')) for t in current_tables}

        for t in found_tables:
            t_name = t.get('logical_table', t.get('table_name'))
            if t_name not in current_names:
                new_tables_added.append(t)
                current_names.add(t_name)

        if new_tables_added:
            new_names = [t.get('logical_table', t.get('table_name')) for t in new_tables_added]
            new_table_cols = get_tables_columns(new_names)
    except Exception as e:
        logger.error(f"Repair retrieval failed: {e}")

    logger.info(f"🔧 [Repair] Added {len(new_tables_added)} tables.", extra={"trace_id": trace_id})

    return {
        "retry_count": retry_count + 1,
        "candidate_tables": state.get("candidate_tables", []) + new_tables_added,
        "table_columns": {**state.get("table_columns", {}), **new_table_cols},
        "final_answer": None,
        "generated_sql": None,
        "error": None,
        "validation_error": None,
        "last_repair_query": repair_query
    }


async def fallback_node(state: AgentState):
    """Step 5: Fallback & Give Up"""
    trace_id = state.get("trace_id", "N/A")
    logger.info("🛑 [Fallback] Triggered.", extra={"trace_id": trace_id})
    feedback = state.get("reflection_feedback", "无法生成有效的查询")

    friendly_msg = (
        f"🤔 抱歉，经过多次尝试，我仍无法生成准确的查询。\n"
        f"原因: {feedback}\n"
        f"建议简化问题或补充更多细节。"
    )

    return {
        "final_answer": friendly_msg,
        "generated_sql": None,
        "sql": None,
        "intent": "STOP_FALLBACK",
        "retry_count": state.get("retry_count", 0)
    }


# ==========================================
# Edges & Routing
# ==========================================

def route_after_generate(state: AgentState):
    if state.get("sentinel_blocked"):
        logger.warning("🛑 [Routing] Sentinel blocked execution. Short-circuiting to END.")
        return END
    return "reflection"


def route_after_reflection(state: AgentState):
    if state.get("reflection_passed"): return "validate"
    if state.get("reflection_count", 0) >= 3: return "fallback"
    return "repair"


def route_after_classify(state: AgentState):
    if state["retry_count"] >= 3: return "fallback"
    if state["error_type"] == "NON_FIXABLE": return "fallback"
    if state["error_type"] in ["SYNTAX_ERROR"]: return "generate"
    return "repair"


# ==========================================
# Workflow Graph
# ==========================================
workflow = StateGraph(AgentState)

workflow.add_node("intent", intent_node)
workflow.add_node("rewrite", rewrite_node)
workflow.add_node("retrieve", retrieve_node)
workflow.add_node("generate", generate_node)
workflow.add_node("reflection", reflection_node)
workflow.add_node("validate", validate_node)
workflow.add_node("classify", classify_node)
workflow.add_node("repair", repair_node)
workflow.add_node("fallback", fallback_node)

workflow.set_entry_point("intent")

workflow.add_conditional_edges(
    "intent",
    lambda x: "rewrite" if x.get("intent") == "DATA_QUERY" else END
)

workflow.add_edge("rewrite", "retrieve")
workflow.add_edge("retrieve", "generate")

workflow.add_conditional_edges(
    "generate",
    route_after_generate,
    {"reflection": "reflection", END: END}
)

workflow.add_conditional_edges(
    "reflection",
    route_after_reflection,
    {"validate": "validate", "repair": "repair", "fallback": "fallback"}
)

workflow.add_conditional_edges(
    "validate",
    lambda x: "classify" if x.get("validation_error") else END
)

workflow.add_conditional_edges(
    "classify",
    route_after_classify,
    {"repair": "repair", "generate": "generate", "fallback": "fallback"}
)

workflow.add_edge("repair", "generate")
workflow.add_edge("fallback", END)

app = workflow.compile()