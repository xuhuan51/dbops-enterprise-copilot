from langgraph.graph import StateGraph, END
from app.core.state import AgentState

# 引入所有节点
from app.graph.nodes.router_node import router_node
from app.graph.nodes.expand_node import expand_node
from app.graph.nodes.retrieval_node import retrieval_node
from app.graph.nodes.generate_node import generate_node
from app.graph.nodes.verification_node import verification_node
# from app.graph.nodes.execution_node import execution_node # 下一步我们要写的

# ==========================================
# 1. 定义判断逻辑 (路标)
# ==========================================

def route_after_verification(state: AgentState):
    """
    决定验证后的去向：
    - 如果 verified=True -> 去执行 (Execution)
    - 如果 verified=False -> 回炉重造 (Generate)
    """
    if state.get("verified"):
        print("🚦 [Graph] Verification Passed -> Moving to Execution")
        return "execute"  # 指向 execution_node
    else:
        print("🚦 [Graph] Verification Failed -> Retrying Generation")
        return "retry"    # 指向 generate_node (循环!)

# ==========================================
# 2. 构建图 (Workflow)
# ==========================================

workflow = StateGraph(AgentState)

# --- 添加节点 ---
workflow.add_node("router_node", router_node)
workflow.add_node("expand_node", expand_node)
workflow.add_node("retrieval_node", retrieval_node)
workflow.add_node("generate_node", generate_node)
workflow.add_node("verification_node", verification_node)
# workflow.add_node("execution_node", execution_node) # 暂时注释，等会写

# --- 连线 (Edge) ---

# 1. 线性流程
workflow.set_entry_point("router_node")
workflow.add_edge("router_node", "expand_node")
workflow.add_edge("expand_node", "retrieval_node")
workflow.add_edge("retrieval_node", "generate_node")

# 2. 生成 -> 验证 (必须经过验证)
workflow.add_edge("generate_node", "verification_node")

# 3. 🔥 关键循环：条件边 🔥
workflow.add_conditional_edges(
    "verification_node",          # 从哪里出发
    route_after_verification,     # 谁来指挥 (上面的函数)
    {
        "execute": END,           # 暂时指向 END，写好执行节点后改成 "execution_node"
        "retry": "generate_node"  # 👈 这就是循环！回指 Generator
    }
)

# 编译图
app = workflow.compile()