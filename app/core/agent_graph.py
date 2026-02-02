import warnings
import re
from typing import Any, Dict, List, Optional, Set

import sqlglot
from sqlglot import exp
from langgraph.graph import StateGraph, END
from langchain_core._api import LangChainBetaWarning
from langchain_core.messages import BaseMessage

from app.core.logger import logger
from app.core.prompts import (
    GEN_SQL_PROMPT,
    ERROR_CLASSIFY_PROMPT,
    ONE_PASS_ROUTER_PROMPT,
    CLARIFY_PROMPT,
)
from app.core.state import (
    AgentState,
    IntentType,
    RouterOutput,
    SQLOutput,
    ErrorOutput,
)
from app.core.llm import (
    get_router_llm,
    get_generate_llm,
    get_reflection_llm,
    get_misc_llm,
)

from app.modules.retrieval.orchestrator import retriever
from app.modules.retrieval.schema.retriever import fetch_table_metadata
from app.modules.sql.executor import execute_sql_explain, get_tables_columns, search_tables_by_column
from app.modules.sql.guardrail import validate_and_rewrite, validate_schema_columns

warnings.filterwarnings("ignore", category=LangChainBetaWarning)

# =========================
# LLM Instances
# =========================
router_llm = get_router_llm()
generate_llm = get_generate_llm()
reflection_llm = get_reflection_llm()
misc_llm = get_misc_llm()


# =========================
# Helpers
# =========================
def _format_history(history: List[BaseMessage]) -> str:
    if not history:
        return "无"
    lines: List[str] = []
    for m in history[-3:]:
        role = getattr(m, "type", m.__class__.__name__)
        content = getattr(m, "content", str(m))
        lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "无"


def _get_table_name(t: Dict[str, Any]) -> Optional[str]:
    if not t:
        return None
    return t.get("logical_table") or t.get("table_name")


def _allowed_tables(candidate_tables: List[Dict[str, Any]]) -> Set[str]:
    names = set()
    for t in candidate_tables or []:
        n = _get_table_name(t)
        if n:
            names.add(n)
    return names


# =========================
# Nodes
# =========================
async def router_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    question = state.get("question", "")
    history: List[BaseMessage] = state.get("history", [])

    print(f"\n{'=' * 30} [🚦 透视: ROUTER (指挥与预算)] {'=' * 30}")
    print(f"❓ 问题: {question}")

    history_text = _format_history(history)

    try:
        # 调用 LLM 进行结构化输出
        prompt = ONE_PASS_ROUTER_PROMPT.format(history=history_text, question=question)
        router_output: RouterOutput = await router_llm.with_structured_output(RouterOutput).ainvoke(prompt)

    except Exception as e:
        logger.error(f"[Router] LLM failed: {e}", extra={"trace_id": trace_id})
        # 🛡️ Fallback 兜底策略：
        # 如果 Router 挂了，默认假设是一个“困难的查数据任务”，拉满预算，防止后续流程断掉
        router_output = RouterOutput(
            intent=IntentType.DATA_QUERY,
            reason="Router LLM Error (Fallback)",
            needs_schema=True,
            needs_knowledge=True,  # 默认开启
            needs_clarify=False,
            query_complexity="hard",  # 默认困难
            pruning_budget_cols=60,  # 默认满预算
            clarify_questions=[]
        )

    # =======================================================
    # 🔥 硬规则干预 (Hard Rules) - 治愈 LLM 的“过度自信”
    # =======================================================

    if router_output.intent == IntentType.DATA_QUERY:
        # 规则 1: 只要查数据，强制开启知识库检索（宁可搜空，不可不搜）
        if not router_output.needs_knowledge:
            print(f"🛡️ [Router] 强制开启 needs_knowledge (保底策略)")
            router_output.needs_knowledge = True

        # 规则 2: 如果是 Hard 模式，强制拉满预算（防止 LLM 抠门）
        if router_output.query_complexity == "hard" and router_output.pruning_budget_cols < 60:
            router_output.pruning_budget_cols = 60

    print(f"✅ 意图: {router_output.intent} ({router_output.query_complexity})")
    print(f"💰 预算: Top-{router_output.pruning_budget_cols} Cols")
    print(f"📋 开关: Schema={router_output.needs_schema} | Knowledge={router_output.needs_knowledge}")
    print(f"{'=' * 80}\n")

    return {
        "intent": router_output.intent,
        "intent_data": router_output,
        # 注意：这里不需要返回 keywords，下一步 Expand Node 会负责生成
    }


async def clarify_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    question = state.get("question", "")
    intent_data: Optional[RouterOutput] = state.get("intent_data")

    suggestions_text = "暂无具体建议"
    if intent_data and intent_data.clarify_questions:
        suggestions_text = "\n".join([f"- {q}" for q in intent_data.clarify_questions])

    try:
        prompt = CLARIFY_PROMPT.format(question=question, suggestions=suggestions_text)
        response = await misc_llm.ainvoke(prompt)
        final_answer = (response.content or "").strip()
    except Exception as e:
        logger.error(f"[Clarify] failed: {e}", extra={"trace_id": trace_id})
        final_answer = "这个问题有点宽泛，您能补充一下具体要查的对象/指标/时间范围吗？"

    print(f"\n{'=' * 30} [🛡️ 透视: CLARIFY] {'=' * 30}")
    print(f"🗣️ 追问内容: {final_answer}")
    print(f"{'=' * 80}\n")

    return {
        "final_answer": final_answer,
        "generated_sql": "",
        "final_result": None,
    }




async def retrieve_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    question = (state.get("question") or "").strip()
    intent_data: Optional[RouterOutput] = state.get("intent_data")

    # 1) 准备检索参数
    if intent_data is None:
        schema_q = question
        knowledge_keywords = []
    else:
        schema_q = (intent_data.schema_query or question).strip()
        knowledge_keywords = (intent_data.knowledge_keywords or [])[:5]

    print(f"\n{'=' * 30} [👀 透视: RETRIEVE (实时模式)] {'=' * 30}")
    print(f"🔍 正在寻找相关表名: {schema_q}")

    # 2) 先用 Milvus 找“候选表名”
    candidate_tables = []
    retrieval_result = {}
    try:
        retrieval_result = await retriever.retrieve_all(
            schema_query=schema_q,
            needs_schema=True,
            needs_knowledge=False,
            schema_top_k=5,
        )
        candidate_tables = retrieval_result.get("candidate_tables", []) or []
    except Exception as e:
        logger.error(f"[Retrieve] Vector search failed: {e}", extra={"trace_id": trace_id})

    # 提取表名（去重）
    found_table_names = []
    try:
        names = []
        for t in candidate_tables:
            n = _get_table_name(t)
            if n:
                names.append(n)
        found_table_names = list(dict.fromkeys(names))  # 保序去重
    except Exception:
        found_table_names = []

    print(f"📚 Milvus 推荐表名: {found_table_names}")

    # 3) 实时拉 DDL + 列注释（主数据源）
    real_time_schema_text = ""
    table_columns_dict: Dict[str, List[str]] = {}
    final_candidates: List[Dict[str, Any]] = []

    if found_table_names:
        try:
            print(f"⚡ 正在连接数据库拉取最新 Schema: {found_table_names}...")

            # A) DDL
            live_ddl_info = await fetch_table_metadata(found_table_names)   # List[{table_name, ddl, text}]
            ddl_map = {x.get("table_name"): (x.get("ddl") or x.get("text") or "") for x in (live_ddl_info or [])}

            # B) 列信息+注释（你 get_tables_columns 返回 List[Dict{name, comment}]）
            live_col_info = await get_tables_columns(found_table_names)     # Dict[str, List[Dict{name, comment}]]

            schema_segments: List[str] = []

            for t_name in found_table_names:
                ddl = (ddl_map.get(t_name) or "").strip()
                cols_with_comment = live_col_info.get(t_name) or []

                # 纯列名给 Guardrail 用
                if cols_with_comment:
                    table_columns_dict[t_name] = [c.get("name") for c in cols_with_comment if c.get("name")]

                # candidate_tables：统一带 ddl/text
                # （后续节点有人读 ddl，有人读 text，你两边都给）
                final_candidates.append({
                    "table_name": t_name,
                    "ddl": ddl,
                    "text": ddl,
                })

                # 给 LLM 的 schema segment：优先 DDL，其次列信息兜底
                if ddl:
                    seg = [f"Table: {t_name}", ddl]
                else:
                    seg = [f"Table: {t_name}", "(DDL unavailable, using verified columns only)"]

                # 字段注释块（有就加）
                if cols_with_comment:
                    comment_lines = []
                    for c in cols_with_comment:
                        n = c.get("name")
                        cm = (c.get("comment") or "").replace("\n", " ").strip()
                        if not n:
                            continue
                        if cm:
                            comment_lines.append(f"- {n}: {cm}")
                        else:
                            comment_lines.append(f"- {n}")

                    if comment_lines:
                        seg.append("\n[Verified Columns]: " + ", ".join(table_columns_dict.get(t_name, [])))
                        seg.append("\n[字段含义速查]:\n" + "\n".join(comment_lines))
                else:
                    # 没列也没 ddl，明确标记
                    if not ddl:
                        seg.append("\n(Schema missing)")

                schema_segments.append("\n".join(seg))

            real_time_schema_text = "\n\n".join([s for s in schema_segments if s and s.strip()]).strip()

            # 最终兜底：避免 schema_context 变空字符串
            if not real_time_schema_text:
                # 至少输出 Table 列表
                real_time_schema_text = "\n".join([f"Table: {t}" for t in found_table_names])

            print(f"✅ 成功加载 {len(final_candidates)} 张表的实时定义！")

        except Exception as e:
            logger.error(f"❌ [Retrieve] Real-time fetch failed: {e}", extra={"trace_id": trace_id})
            # 回退到 Milvus 的候选（但仍要兜底 schema_context）
            final_candidates = candidate_tables or []
            real_time_schema_text = "\n\n".join(
                [str(t.get("ddl") or t.get("text") or "").strip() for t in (candidate_tables or []) if str(t.get("ddl") or t.get("text") or "").strip()]
            ).strip()
            if not real_time_schema_text and found_table_names:
                real_time_schema_text = "\n".join([f"Table: {t}" for t in found_table_names])

    else:
        print("⚠️ 未找到任何相关表名")
        real_time_schema_text = "(无相关表结构)"
        final_candidates = []

    print(f"{'=' * 80}\n")

    # 4) 写入 state：✅ 注意不要把 retry/reflection 强行清零
    rag_contexts = state.get("rag_contexts", {}) or {}
    rag_contexts["schema"] = real_time_schema_text or "(无相关表结构)"
    rag_contexts["knowledge"] = rag_contexts.get("knowledge", "") or ""

    return {
        "candidate_tables": final_candidates,
        "table_columns": table_columns_dict,
        "rag_contexts": rag_contexts,
        # 这些不要在 retrieve 里硬重置，否则 repair/reflect 会被你抹掉
        # "retry_count": 0,
        # "reflection_count": 0,
        "validation_error": None,
        "sentinel_blocked": False,
    }



async def knowledge_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    intent: IntentType = state.get("intent", IntentType.CHAT)
    question = state.get("question", "")
    rag_contexts = state.get("rag_contexts", {}) or {}

    context_str = ""
    if intent != IntentType.CHAT:
        if rag_contexts.get("schema") or rag_contexts.get("knowledge"):
            context_str = f"【参考资料】:\n{rag_contexts.get('schema', '')}\n{rag_contexts.get('knowledge', '')}"

    if intent == IntentType.METADATA_QUERY:
        system_prompt = "你是元数据专家。请根据【参考资料】解释表结构或字段含义。禁止生成 SQL。"
    elif intent == IntentType.OPS_DIAGNOSIS:
        system_prompt = "你是运维技术专家。请根据【参考资料】分析报错原因、技术原理或给出建议。"
    elif intent == IntentType.CHAT:
        system_prompt = "你是一个友好的助手。请进行简短回复。"
    else:
        system_prompt = "你是数据智能助手。"

    full_prompt = f"{system_prompt}\n\n{context_str}\n\n用户问题: {question}"

    try:
        response = await misc_llm.ainvoke(full_prompt)
        answer = (response.content or "").strip()
    except Exception as e:
        logger.error(f"[Knowledge] failed: {e}", extra={"trace_id": trace_id})
        answer = "抱歉，我暂时无法回答该问题。"

    print(f"\n{'=' * 30} [🧠 透视: KNOWLEDGE] {'=' * 30}")
    print(f"💬 回复: {answer[:100]}...")
    print(f"{'=' * 80}\n")

    return {
        "final_answer": answer,
        "generated_sql": "",
        "final_result": None,
    }


async def generate_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    retry_count = int(state.get("retry_count", 0) or 0)

    rag_contexts = state.get("rag_contexts", {}) or {}
    schema_context = (rag_contexts.get("schema") or "").strip()
    knowledge_context = (rag_contexts.get("knowledge") or "").strip()

    candidate_tables = state.get("candidate_tables", []) or []
    table_columns = state.get("table_columns", {}) or {}

    # ✅ 更合理的 STOP：schema_context 为空 且 candidate_tables 也为空 才 stop
    if not schema_context and not candidate_tables:
        print(f"\n{'=' * 30} [🛑 透视: GENERATE STOP] {'=' * 30}")
        print("原因: 没有 Schema 上下文，无法生成 SQL")
        print(f"{'=' * 80}\n")
        err_sql = "SELECT 'ERR::NO_RELEVANT_TABLE' AS error;"
        return {
            "generated_sql": err_sql,
            "final_answer": f"SQL_RESULT:{err_sql}",
            "retry_count": retry_count + 1,
            "sentinel_blocked": True,
        }

    # # ✅ 关键：真实打印 schema 内容（你之前注释掉导致你以为是空）
    # print(f"\n{'=' * 30} [🧠 透视: LLM 正在阅读的 Schema] {'=' * 30}")
    # print(f"schema_len={len(schema_context)} | candidate_tables={len(candidate_tables)} | tables_with_cols={len(table_columns)}")
    # print(schema_context[:800] + ("\n...(truncated)..." if len(schema_context) > 800 else ""))
    # print(f"{'=' * 80}\n")

    # 错误上下文：用于让模型在 repair 后更收敛
    error_context = "None"
    if state.get("validation_error"):
        error_context = f"⚠️ Previous Execution Error: {state.get('validation_error')}"
    elif state.get("reflection_passed") is False:
        error_context = f"⚠️ Feedback from Expert: {state.get('reflection_feedback', '')}"

    prompt = GEN_SQL_PROMPT.format(
        knowledge_context=knowledge_context or "No specific terms.",
        schema_context=schema_context or "(无相关表结构)",
        history_context=state.get("history", [])[-3:] if state.get("history") else "None",
        question=state.get("question", ""),
        error_context=error_context,
        golden_sql_context="None"
    )

    # 生成
    try:
        res: SQLOutput = await generate_llm.with_structured_output(SQLOutput).ainvoke(prompt)
        generated_sql = (res.sql or "").strip()
        if not generated_sql:
            generated_sql = "SELECT 'ERR::EMPTY_SQL' AS error;"
    except Exception as e:
        logger.error(f"[Generate] LLM failed: {e}", extra={"trace_id": trace_id})
        generated_sql = "SELECT 'ERR::GENERATION_FAILED' AS error;"

    print(f"\n{'=' * 30} [📝 透视: GENERATE SQL] {'=' * 30}")
    print(f"📄 原始 SQL:\n{generated_sql}")

    # 0) 安全护栏 + LIMIT 重写
    gr = validate_and_rewrite(generated_sql)
    if not gr.ok:
        print(f"🛑 护栏拦截: {gr.reason}")
        err_sql = f"SELECT 'ERR::{gr.reason}' AS error;"
        return {
            "generated_sql": err_sql,
            "final_answer": f"SQL_RESULT:{err_sql}",
            "retry_count": retry_count + 1,
            "sentinel_blocked": True,
            "reflection_passed": False,
        }

    generated_sql = (gr.rewritten_sql or generated_sql).strip()

    # 1) AST Parse & Schema Check（注意：你 validate_schema_columns 目前“只警告不拦截”，先不强依赖它）
    try:
        statement = sqlglot.parse_one(generated_sql, read="mysql")
        allowed = _allowed_tables(candidate_tables)
        schema_gr = validate_schema_columns(statement, table_columns, allowed)

        if not schema_gr.ok:
            print(f"🛑 Schema 校验失败: {schema_gr.reason}")
            err_sql = f"SELECT 'ERR::{schema_gr.reason}' AS error;"
            return {
                "generated_sql": err_sql,
                "final_answer": f"SQL_RESULT:{err_sql}",
                "retry_count": retry_count + 1,
                "sentinel_blocked": True,
                "reflection_passed": False,
            }

    except Exception as e:
        print(f"🛑 SQL 解析/校验异常: {e}")
        err_sql = "SELECT 'ERR::AST_FAIL' AS error;"
        return {
            "generated_sql": err_sql,
            "final_answer": f"SQL_RESULT:{err_sql}",
            "retry_count": retry_count + 1,
            "sentinel_blocked": True,
            "reflection_passed": False,
        }

    print("✅ SQL 检查通过")
    print(f"{'=' * 80}\n")

    return {
        "generated_sql": generated_sql,
        "final_answer": f"SQL_RESULT:{generated_sql}",
        "retry_count": retry_count + 1,
        "sentinel_blocked": False,
        "validation_error": None,
    }



async def reflection_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    sql = (state.get("generated_sql", "") or "").strip()

    # 熔断 SQL 直接放行
    if "ERR::" in sql:
        return {"reflection_passed": True}

    # ✅ 你已有的实时列信息：retrieve_node 写入 state 的 table_columns
    # 结构：{ "t_order": ["oid","uid","amount",...], ... }
    table_columns = state.get("table_columns", {}) or {}

    print(f"\n{'=' * 30} [🤔 透视: REFLECTION] {'=' * 30}")

    # 1) SQL 必须能解析
    try:
        stmt = sqlglot.parse_one(sql, read="mysql")
    except Exception as e:
        msg = f"LOGIC_ERROR: SQL parse failed: {e}"
        print(f"❌ 结果: False | 理由: {msg}")
        return {
            "reflection_passed": False,
            "reflection_feedback": msg,
            "suggested_search_keywords": ["SQL", "syntax"],
            "reflection_count": (state.get("reflection_count", 0) or 0) + 1,
            "reflection_severity": "MUST_FAIL",
        }

    # 2) 抽取 SQL 用到的表
    used_tables = set()
    for t in stmt.find_all(exp.Table):
        if t.name:
            used_tables.add(t.name)

    known_tables = set(table_columns.keys())

    # 2.1 表不存在 -> FAIL
    missing_tables = [t for t in used_tables if t not in known_tables]
    if missing_tables:
        msg = f"TABLE_NOT_FOUND: {missing_tables[0]}"
        print(f"❌ 结果: False | 理由: {msg}")
        return {
            "reflection_passed": False,
            "reflection_feedback": msg,
            "suggested_search_keywords": missing_tables,
            "reflection_count": (state.get("reflection_count", 0) or 0) + 1,
            "reflection_severity": "MUST_FAIL",
        }

    # 3) 抽取 SQL 用到的列
    # 只对 “带表前缀的列” 做硬判，避免 SUM(amount) 这种无前缀列误杀
    for c in stmt.find_all(exp.Column):
        col = c.name
        tbl = c.table  # 可能为空
        if not col or col == "*":
            continue
        if tbl:
            cols = table_columns.get(tbl, [])
            if col not in cols:
                msg = f"COLUMN_NOT_FOUND: {tbl}.{col}"
                print(f"❌ 结果: False | 理由: {msg}")
                return {
                    "reflection_passed": False,
                    "reflection_feedback": msg,
                    "suggested_search_keywords": [col, tbl],
                    "reflection_count": (state.get("reflection_count", 0) or 0) + 1,
                    "reflection_severity": "MUST_FAIL",
                }

    # ✅ 到这里：放宽通过（不再检查“是否缺少限制条件/口径”）
    print("✅ 结果: True | 理由: PASS (relaxed reflection)")
    return {
        "reflection_passed": True,
        "reflection_feedback": "PASS (relaxed reflection)",
        "suggested_search_keywords": [],
        "reflection_count": (state.get("reflection_count", 0) or 0) + 1,
        "reflection_severity": "PASS",
    }



async def validate_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    sql = state.get("generated_sql", "") or ""
    print(f"\n{'=' * 30} [🛡️ 透视: VALIDATE] {'=' * 30}")

    if "ERR::" in sql:
        print(f"❌ SQL 包含已知错误: {sql}")
        return {"validation_error": sql}

    try:
        execute_sql_explain(sql, trace_id=trace_id)
        print(f"✅ SQL 执行计划 (EXPLAIN) 成功")
        print(f"{'=' * 80}\n")
        return {"validation_error": None}
    except Exception as e:
        print(f"❌ SQL 执行/Explain 报错: {e}")
        print(f"{'=' * 80}\n")
        return {"validation_error": str(e)}


async def classify_node(state: AgentState):
    prompt = ERROR_CLASSIFY_PROMPT.format(
        sql=state.get("generated_sql", ""),
        error_msg=state.get("validation_error", ""),
    )
    try:
        res: ErrorOutput = await reflection_llm.with_structured_output(ErrorOutput).ainvoke(prompt)
        return {"error_type": res.error_type}
    except:
        return {"error_type": "NON_FIXABLE"}


async def repair_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    retry_count = int(state.get("retry_count", 0) or 0)

    question = state.get("question", "")
    feedback = state.get("reflection_feedback", "") or ""
    suggested_keywords = state.get("suggested_search_keywords", []) or []

    current_rag_contexts = state.get("rag_contexts", {}) or {}
    current_schema_context = current_rag_contexts.get("schema", "")
    current_tables = state.get("candidate_tables", [])
    current_columns_dict = state.get("table_columns", {}) or {}

    print(f"\n{'=' * 30} [🔧 透视: REPAIR (Retry {retry_count + 1})] {'=' * 30}")

    # =======================================================
    # 1. 目标锁定：找出所有需要“强制刷新”的表名
    # =======================================================
    target_tables = set()

    # 策略 A: 从反馈中“正则提取”被点名的表
    potential_tables = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b", feedback)
    for t in potential_tables:
        if t.lower() not in ["schema", "table", "column", "field", "check", "select", "where", "database", "sql",
                             "query", "need", "info"]:
            target_tables.add(t)
            print(f"   -> 🎯 Reflector 要求检查: {t}")

    # 策略 B: 从建议关键词反查
    target_columns = suggested_keywords
    if target_columns:
        print(f"   -> 正在反查字段归属: {target_columns}")
        for col in target_columns:
            found_tbls = await search_tables_by_column(col)
            for t in found_tbls:
                target_tables.add(t)
                print(f"   -> 🎯 字段 '{col}' 命中表: {t}")

    # 🔥 策略 C (兜底): 如果反馈暗示了 Schema 问题但没提表名，强制刷新当前所有表
    if not target_tables:
        feedback_lower = feedback.lower()
        triggers = ["missing", "column", "field", "join", "schema", "definition", "缺少", "字段", "列", "连接",
                    "表结构"]
        if any(t in feedback_lower for t in triggers):
            print(f"   -> ⚠️ 反馈暗示 Schema 信息缺失，决定刷新所有当前表！")
            for t in current_tables:
                t_name = _get_table_name(t)
                if t_name: target_tables.add(t_name)

    # 策略 D: 向量检索
    if not target_tables:
        repair_query = f"{question} {feedback}"
        print(f"   -> ⚠️ 未锁定具体表，执行宽泛检索: '{repair_query}'")
        try:
            retrieval_res = await retriever.retrieve_all(
                schema_query=repair_query,
                needs_schema=True,
                needs_knowledge=False,
                schema_top_k=3
            )
            for t in retrieval_res.get("candidate_tables", []):
                t_name = _get_table_name(t)
                if t_name: target_tables.add(t_name)
        except Exception as e:
            logger.error(f"[Repair] Retrieval failed: {e}")

    # =======================================================
    # 2. 强制刷新
    # =======================================================
    if target_tables:
        tables_to_refresh = list(target_tables)
        print(f"📦 正在强制刷新表元数据 (Breaking the loop): {tables_to_refresh}")

        try:
            # 1. 拉取 DDL
            added_metadata = await fetch_table_metadata(tables_to_refresh)

            # 2. 拉取 列信息
            added_columns = await get_tables_columns(tables_to_refresh)

            # 更新 candidate_tables (把新的放前面)
            new_candidates_objs = [{"table_name": m["table_name"], "ddl": m["ddl"]} for m in added_metadata]
            final_candidates = new_candidates_objs + current_tables

            # 更新 schema_context
            ddl_segments = []
            for meta in added_metadata:
                if meta.get("ddl"):
                    ddl_segments.append(f"[Refreshed Detail Schema]: {meta['table_name']}\n{meta['ddl']}")

            new_schema_context = current_schema_context
            if ddl_segments:
                new_schema_context += "\n\n" + "\n\n".join(ddl_segments)

            # 更新 table_columns
            for tb, cols_info in added_columns.items():
                current_columns_dict[tb] = [c["name"] for c in cols_info]

            updated_rag_contexts = {
                "schema": new_schema_context,
                "knowledge": current_rag_contexts.get("knowledge", "")
            }

            print(f"✅ 刷新完成，DDL 已注入。下一轮 Reflector 应该能看见了。")
            return {
                "candidate_tables": final_candidates,
                "table_columns": current_columns_dict,
                "rag_contexts": updated_rag_contexts,
                "retry_count": retry_count + 1,
                "reflection_passed": None,
                "validation_error": None
            }

        except Exception as e:
            logger.error(f"[Repair] Fetch Metadata failed: {e}")

    print("⚠️ 修复尝试未产生有效更新，继续重试...")
    return {
        "retry_count": retry_count + 1,
        "reflection_passed": None
    }


async def fallback_node(state: AgentState):
    print(f"\n{'=' * 30} [🏳️ 透视: FALLBACK] {'=' * 30}")
    print("❌ 最终放弃，输出兜底话术。")
    print(f"{'=' * 80}\n")
    return {
        "final_answer": "🤔 抱歉，经过多次尝试，我仍无法生成准确的 SQL 查询。",
        "generated_sql": "",
        "final_result": None,
    }


# =========================
# Routing Logic
# =========================
def route_after_router(state: AgentState):
    intent: IntentType = state.get("intent", IntentType.CHAT)
    intent_data: Optional[RouterOutput] = state.get("intent_data")

    if intent == IntentType.AMBIGUOUS or (intent_data and intent_data.needs_clarify):
        return "clarify"
    if intent in (IntentType.DATA_QUERY, IntentType.METADATA_QUERY, IntentType.OPS_DIAGNOSIS):
        return "retrieve"
    return "knowledge"


def route_after_retrieve(state: AgentState):
    intent: IntentType = state.get("intent", IntentType.CHAT)
    if intent == IntentType.DATA_QUERY:
        return "generate"
    return "knowledge"


def route_after_generate(state: AgentState):
    return "reflection"


def route_after_reflection(state: AgentState):
    if state.get("reflection_passed"):
        return "validate"
    if (state.get("reflection_count", 0) or 0) >= 3:
        return "fallback"
    return "repair"


def route_after_validate(state: AgentState):
    if state.get("validation_error"):
        return "classify"
    return END


def route_after_classify(state: AgentState):
    if (state.get("retry_count", 0) or 0) >= 3:
        return "fallback"
    return "repair"


def route_after_repair(state: AgentState):
    if (state.get("retry_count", 0) or 0) >= 3:
        return "fallback"
    return "generate"


# =========================
# Graph Definition
# =========================
workflow = StateGraph(AgentState)

workflow.add_node("router", router_node)
workflow.add_node("clarify", clarify_node)
workflow.add_node("retrieve", retrieve_node)
workflow.add_node("knowledge", knowledge_node)
workflow.add_node("generate", generate_node)
workflow.add_node("reflection", reflection_node)
workflow.add_node("validate", validate_node)
workflow.add_node("classify", classify_node)
workflow.add_node("repair", repair_node)
workflow.add_node("fallback", fallback_node)

workflow.set_entry_point("router")

workflow.add_conditional_edges("router", route_after_router,
                               {"clarify": "clarify", "retrieve": "retrieve", "knowledge": "knowledge"})
workflow.add_edge("clarify", END)
workflow.add_conditional_edges("retrieve", route_after_retrieve, {"generate": "generate", "knowledge": "knowledge"})
workflow.add_edge("knowledge", END)
workflow.add_conditional_edges("generate", route_after_generate, {"reflection": "reflection"})
workflow.add_conditional_edges("reflection", route_after_reflection,
                               {"validate": "validate", "repair": "repair", "fallback": "fallback"})
workflow.add_conditional_edges("validate", route_after_validate, {"classify": "classify", END: END})
workflow.add_conditional_edges("classify", route_after_classify, {"repair": "repair", "fallback": "fallback"})
workflow.add_conditional_edges("repair", route_after_repair, {"generate": "generate", "fallback": "fallback"})
workflow.add_edge("fallback", END)

app = workflow.compile()