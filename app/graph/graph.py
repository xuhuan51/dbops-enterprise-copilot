from langgraph.graph import StateGraph, END
from app.core.state import AgentState

# 引入所有节点
from app.graph.nodes.router_node import router_node
from app.graph.nodes.expand_node import expand_node
from app.graph.nodes.retrieval_node import retrieval_node
from app.graph.nodes.generate_node import generate_node
from app.graph.nodes.verification_node import verification_node
from app.graph.nodes.execution_node import execution_node  # ✅ 1. 取消注释，正式引入


# ==========================================
# 1. 定义判断逻辑 (路标)
# ==========================================

def route_after_verification(state: AgentState):
    """
    决定验证后的去向：
    - 如果 verified=True -> 去执行 (Execution)
    - 如果 verified=False -> 回炉重造 (Generate)
    """
    # 这里可以加一个最大重试次数的逻辑，防止无限死循环
    # 比如: if not state.get("verified") and state.get("retry_count", 0) > 3: return "give_up"

    if state.get("verified"):
        print("🚦 [Graph] Verification Passed -> Moving to Execution")
        return "execute"  # 指向 execution_node
    else:
        print("🚦 [Graph] Verification Failed -> Retrying Generation")
        return "retry"  # 指向 generate_node (循环!)

# 定义新的判断逻辑
def route_after_execution(state: AgentState):
    """
    执行后的路由：
    - 成功 -> END
    - 失败 -> 回到 Generate (带着报错信息去重修)
    """
    # 1. 防止死循环：如果重试超过 3 次，直接躺平
    if state.get("retry_count", 0) > 3:
        print("🛑 [Graph] Max retries reached. Giving up.")
        return END

    # 2. 检查执行结果
    if state.get("is_executable"):
        print("✅ [Graph] Execution Success -> Finishing")
        return END
    else:
        print(f"❌ [Graph] Execution Failed: {state.get('execution_error')} -> Retrying")
        return "retry_generation"


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
workflow.add_node("execution_node", execution_node)  # ✅ 2. 注册执行节点

# --- 连线 (Edge) ---

# 1. 线性流程: Router -> Expand -> Retrieval -> Generate -> Verification
workflow.set_entry_point("router_node")
workflow.add_edge("router_node", "expand_node")
workflow.add_edge("expand_node", "retrieval_node")
workflow.add_edge("retrieval_node", "generate_node")
workflow.add_edge("generate_node", "verification_node")

# 2. 🔥 关键循环与分流 🔥
workflow.add_conditional_edges(
    "verification_node",  # 起点
    route_after_verification,  # 路由逻辑函数
    {
        "execute": "execution_node",  # ✅ 3. 验证通过 -> 进入执行节点
        "retry": "generate_node"  # 🔄 验证失败 -> 回到生成节点 (Reflect & Regenerate)
    }
)


# 修改 workflow 添加 conditional_edges
workflow.add_conditional_edges(
    "execution_node",           # 从执行节点出来
    route_after_execution,      # 走这个判断函数
    {
        END: END,               # 成功了就结束
        "retry_generation": "generate_node"  # 失败了回炉重造
    }
)

# 3. 结束流程
workflow.add_edge("execution_node", END)  # ✅ 4. 执行完毕 -> 结束

# 编译图
app = workflow.compile()