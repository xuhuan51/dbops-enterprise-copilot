import os
from typing import TypedDict, Literal, List
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.core.config import settings
from app.core.prompts import ROUTER_PROMPT
from app.core.mysql_saver import AsyncMySQLSaver
from app.core.agent_graph import app as query_agent_app

# ==========================================
# 🔥 补回丢失的 DB 配置 (main.py 需要用到)
# ==========================================
DB_CONFIG = {
    "host": settings.MYSQL_HOST,
    "port": 3306,  # 强制走物理端口，避开 Proxy
    "user": settings.MYSQL_USER,
    "password": settings.MYSQL_PASSWORD,
    "db": "dbops_memory",
    "autocommit": True
}

# --- 初始化 LLM ---
llm = ChatOpenAI(
    model=settings.LLM_MODEL,
    temperature=0,
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL
)


# --- 定义 Master 状态 ---
class MasterState(TypedDict):
    question: str
    intent: str
    final_answer: str
    trace_id: str
    history: List[str]  # 主图这里存字符串列表没问题


# --- 定义路由输出 ---
class RouterOutput(BaseModel):
    intent: Literal["DATA_QUERY", "KNOWLEDGE_SEARCH", "CHAT"]


# ==========================================
# Nodes
# ==========================================
def router_node(state: MasterState):
    print(f"🚦 [Master] Routing query: {state['question']}")
    current_history = state.get("history", [])

    # 构造 Prompt
    prompt = ROUTER_PROMPT.format(question=state["question"])

    # 调用 LLM 决策
    try:
        res = llm.with_structured_output(RouterOutput).invoke(prompt)
        intent = res.intent
    except Exception as e:
        print(f"⚠️ Router LLM failed: {e}, fallback to CHAT")
        intent = "CHAT"

    print(f"    -> Route to: {intent}")
    return {"intent": intent, "history": current_history}


async def search_agent_node(state: MasterState):
    print("🌐 [Search Agent] Searching knowledge...")
    # 简单模拟搜索
    res = await llm.ainvoke(f"请简要回答这个技术问题: {state['question']}")
    # 更新历史：统一转为字符串格式
    new_history = state.get("history", []) + [f"User: {state['question']}", f"AI: {res.content}"]
    return {"final_answer": res.content, "history": new_history}


async def chat_node(state: MasterState):
    # 简单闲聊
    res = await llm.ainvoke(f"请用亲切的语气回复用户: {state['question']}")
    new_history = state.get("history", []) + [f"User: {state['question']}", f"AI: {res.content}"]
    return {"final_answer": res.content, "history": new_history}


async def call_query_agent(state: MasterState):
    print("📊 [Query Agent] Activated.")
    global_history = state.get("history", [])
    recent_history = global_history[-6:]

    inputs = {
        "question": state["question"],
        "trace_id": state.get("trace_id"),
        # 🔥🔥🔥 核心修复：key 必须是 "history"，对应 AgentState 定义 🔥🔥🔥
        # 原来写的是 "chat_history"，导致子 Agent 拿不到历史
        "history": recent_history
    }

    # 调用子图
    result_state = await query_agent_app.ainvoke(inputs)

    # 解析结果
    final_ans = ""
    if result_state.get("generated_sql"):
        # 优先展示 SQL
        final_ans = f"SQL_RESULT:{result_state['generated_sql']}"
        ai_msg = f"Generated SQL: {result_state['generated_sql']}"
    elif result_state.get("final_answer"):
        # 如果有兜底回复 (比如 Fallback)
        final_ans = result_state["final_answer"]
        ai_msg = final_ans
    else:
        final_ans = "抱歉，无法生成有效的查询语句。"
        ai_msg = "Failed to generate SQL"

    # 更新主图历史
    new_history = global_history + [f"User: {state['question']}", f"AI: {ai_msg}"]
    return {"final_answer": final_ans, "history": new_history}


# ==========================================
# Graph Definition
# ==========================================
workflow = StateGraph(MasterState)
workflow.add_node("router", router_node)
workflow.add_node("search_agent", search_agent_node)
workflow.add_node("chat_agent", chat_node)
workflow.add_node("data_query_agent", call_query_agent)

workflow.set_entry_point("router")


def route_logic(state):
    return state["intent"]


workflow.add_conditional_edges(
    "router",
    route_logic,
    {
        "DATA_QUERY": "data_query_agent",
        "KNOWLEDGE_SEARCH": "search_agent",
        "CHAT": "chat_agent"
    }
)
workflow.add_edge("search_agent", END)
workflow.add_edge("chat_agent", END)
workflow.add_edge("data_query_agent", END)

# 🔥 核心修改 1: 全局变量初始为 None (Lazy Init)
master_app = None


# 🔥 核心修改 2: 真正的初始化逻辑放在函数里
def init_master_app(pool):
    """
    由 main.py 调用，注入数据库连接池，启用持久化记忆
    """
    global master_app
    print("🧠 [Master] Injecting MySQL Memory Saver (Lazy Init)...")

    # 1. 实例化 Saver
    checkpointer = AsyncMySQLSaver(pool)

    # 2. 编译 Graph
    master_app = workflow.compile(checkpointer=checkpointer)
    return master_app