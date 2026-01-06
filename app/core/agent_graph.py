import os
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

# 导入上面写好的模块
from app.core.prompts import INTENT_PROMPT, GEN_SQL_PROMPT, ERROR_CLASSIFY_PROMPT
from app.core.state import AgentState, IntentOutput, SQLOutput, ErrorOutput


# 导入你之前的检索函数 (确保路径对)
# 如果你没有 retrieve_tables_advanced，就用 retrieve_tables 代替
from app.api.v1.retrieve_tables import retrieve_tables as retrieve_tool
from app.modules.sql.executor import execute_sql_explain

# --- 初始化模型 ---
# 建议使用 DeepSeek-V3 或 GPT-4o
llm = ChatOpenAI(
    model="qwen2.5:14b",  # 或 qwen2.5:14b
    temperature=0,
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL")
)


# ==========================================
# Nodes (节点实现)
# ==========================================

def intent_node(state: AgentState):
    print("\n🚦 [Step 0] Intent Check...")
    prompt = INTENT_PROMPT.format(question=state["question"])
    res = llm.with_structured_output(IntentOutput).invoke(prompt)
    print(f"    -> Intent: {res.intent}")
    return {"intent": res.intent}


def retrieve_node(state: AgentState):
    print("🔍 [Step 1] Retrieving Tables...")
    # 第一次召回，范围大一点
    tables = retrieve_tool(state["question"], topk=10)
    return {
        "candidate_tables": tables,
        "retry_count": 0,
        "validation_error": None
    }


def generate_node(state: AgentState):
    print("✍️ [Step 2] Generating SQL...")

    # 构造 Schema 上下文
    schema_context = "\n".join([
        f"Table: {t['logical_table']}\nInfo: {t.get('text', '')[:150]}..."
        for t in state["candidate_tables"]
    ])

    # 构造错误上下文 (如果有)
    error_context = ""
    if state.get("validation_error"):
        error_context = f"⚠️ [上一次报错]: {state['validation_error']}\n请根据报错修正你的 SQL，如果是缺表导致，请保持 confidence 低分。"

    prompt = GEN_SQL_PROMPT.format(
        schema_context=schema_context,
        question=state["question"],
        error_context=error_context
    )

    res = llm.with_structured_output(SQLOutput).invoke(prompt)
    return {
        "generated_sql": res.sql,
        "sql_confidence": res.confidence
    }


def validate_node(state: AgentState):
    print("⚖️ [Step 3] Validating SQL (EXPLAIN)...")
    sql = state["generated_sql"]

    try:
        execute_sql_explain(sql)
        print("    ✅ Validation Passed.")
        return {"validation_error": None}
    except Exception as e:
        error_msg = str(e)
        print(f"    ❌ Validation Failed: {error_msg}")
        return {"validation_error": error_msg}


def classify_node(state: AgentState):
    print("🧠 [Step 4] Classifying Error...")
    prompt = ERROR_CLASSIFY_PROMPT.format(
        sql=state["generated_sql"],
        error_msg=state["validation_error"]
    )

    res = llm.with_structured_output(ErrorOutput).invoke(prompt)
    print(f"    -> Type: {res.error_type} | Keywords: {res.search_keywords}")

    return {
        "error_type": res.error_type,
        "repair_keywords": res.search_keywords
    }


def repair_node(state: AgentState):
    print(f"🚑 [Repair] Searching supplement tables: {state['repair_keywords']}")

    new_tables = []
    current_ids = {t['logical_table'] for t in state["candidate_tables"]}

    for kw in state["repair_keywords"]:
        # 补搜只取最相关的 Top-3
        found = retrieve_tool(kw, topk=3)
        for t in found:
            if t['logical_table'] not in current_ids:
                new_tables.append(t)
                current_ids.add(t['logical_table'])

    print(f"    -> Added {len(new_tables)} new tables.")
    return {
        "candidate_tables": state["candidate_tables"] + new_tables,
        "retry_count": state["retry_count"] + 1
    }


# ==========================================
# Edges (路由逻辑)
# ==========================================

def route_after_intent(state: AgentState):
    if state["intent"] == "data_query":
        return "retrieve"
    return END  # sensitive / non_data


def route_after_validate(state: AgentState):
    if not state.get("validation_error"):
        return END  # 成功
    return "classify"  # 失败，去分类


def route_after_classify(state: AgentState):
    # 1. 重试次数熔断
    if state["retry_count"] >= 1:  # 生产环境建议设为 2
        print("🛑 Max retries reached.")
        return END

    error_type = state["error_type"]

    # 2. 不可修复 -> 结束
    if error_type == "NON_FIXABLE":
        return END

    # 🔥 3. 新增逻辑：如果是语法错误，直接去生成节点 (Generate) 重写
    if error_type == "SYNTAX_ERROR":
        print("🔄 Syntax Error detected. Retrying generation immediately...")
        return "generate"

    # 4. 其他错误 (缺表/缺列) -> 去补搜 (Repair)
    return "repair"


# ==========================================
# Graph Construction (建图)
# ==========================================

workflow = StateGraph(AgentState)

# Add Nodes
workflow.add_node("intent", intent_node)
workflow.add_node("retrieve", retrieve_node)
workflow.add_node("generate", generate_node)
workflow.add_node("validate", validate_node)
workflow.add_node("classify", classify_node)
workflow.add_node("repair", repair_node)

# Set Entry
workflow.set_entry_point("intent")

# Add Edges
workflow.add_conditional_edges("intent", route_after_intent)
workflow.add_edge("retrieve", "generate")
workflow.add_edge("generate", "validate")
workflow.add_conditional_edges("validate", route_after_validate)
workflow.add_conditional_edges(
    "classify",
    route_after_classify,
    {
        "repair": "repair",
        "generate": "generate",  # 👈 允许从分类节点直接跳回生成节点
        END: END
    }
)
workflow.add_edge("repair", "generate")  # 闭环

# Compile
app = workflow.compile()

# ==========================================
# Run (测试入口)
# ==========================================
if __name__ == "__main__":
    # 测试 Case: 一个需要跨库且容易缺表的查询
    query = "统计北京地区购买小米手机的用户数量"

    print(f"🚀 Starting Agent for: {query}")
    final_state = app.invoke({"question": query})

    print("\n================ RESULT ================")
    if not final_state.get("validation_error"):
        print("🎉 Success SQL:")
        print(final_state["generated_sql"])
    else:
        print("❌ Failed.")
        print(f"Last Error: {final_state['validation_error']}")