from langgraph.graph import StateGraph, END
from app.core.state import AgentState

# 引入所有节点
from app.graph.nodes.router_node import router_node
from app.graph.nodes.expand_node import expand_node
from app.graph.nodes.retrieval_node import retrieval_node
from app.graph.nodes.column_selector_node import column_selector_node  # 👈 新增
from app.graph.nodes.generate_node import generate_node
from app.graph.nodes.verification_node import verification_node
from app.graph.nodes.execution_node import execution_node

# ==========================================
# 1. 定义判断逻辑 (路标)
# ==========================================

MAX_VERIFY_RETRIES = 2
MAX_EXECUTION_RETRIES = 1

def route_after_verification(state: AgentState):
    """验证后的去向：通过、重写或达到上限强制通过"""
    if state.get("verified"):
        print("🟢 [Graph] Verification Passed -> Execution")
        return "execute"

    current_count = state.get("retry_count", 0)
    if current_count > MAX_VERIFY_RETRIES:
        print(f"🛑 [Graph] Max verify retries ({MAX_VERIFY_RETRIES}) reached. Force Passing.")
        return "execute"

    return "retry"

def route_after_execution(state: AgentState):
    """执行后的去向：成功结束或回炉重造"""
    if state.get("is_executable"):
        print("✅ [Graph] Execution Success -> Finishing")
        return END

    # 这里的 key 需与 State 中的定义保持一致 (假设为 retry_count 或专门的 execution_retries)
    current_exec_retries = state.get("execution_retries", 0)

    if current_exec_retries > MAX_EXECUTION_RETRIES:
        print(f"🛑 [Graph] Max execution retries ({MAX_EXECUTION_RETRIES}) reached.")
        return END

    print(f"❌ [Graph] Execution Failed -> Retrying Generation (Attempt {current_exec_retries})")
    return "retry_generation"

# ==========================================
# 2. 构建图 (Workflow)
# ==========================================

workflow = StateGraph(AgentState)

# --- 添加节点 ---
workflow.add_node("router_node", router_node)
workflow.add_node("expand_node", expand_node)
workflow.add_node("retrieval_node", retrieval_node)
workflow.add_node("column_selector_node", column_selector_node) # 👈 新增
workflow.add_node("generate_node", generate_node)
workflow.add_node("verification_node", verification_node)
workflow.add_node("execution_node", execution_node)

# --- 连线 (Edge) ---

# 1. 核心链路 (线性部分)
workflow.set_entry_point("router_node")
workflow.add_edge("router_node", "expand_node")
workflow.add_edge("expand_node", "retrieval_node")
workflow.add_edge("retrieval_node", "column_selector_node") # 👈 路由到精选列
workflow.add_edge("column_selector_node", "generate_node") # 👈 精选后再生成
workflow.add_edge("generate_node", "verification_node")

# 2. 条件分支 A: 验证反馈循环
workflow.add_conditional_edges(
    "verification_node",
    route_after_verification,
    {
        "execute": "execution_node",
        "retry": "generate_node",
    }
)

# 3. 条件分支 B: 执行失败重试 (ICU 模式)
workflow.add_conditional_edges(
    "execution_node",
    route_after_execution,
    {
        END: END,
        "retry_generation": "generate_node"
    }
)

# 编译图
app = workflow.compile()