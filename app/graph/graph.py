from langgraph.graph import StateGraph, END
from app.core.state import AgentState

# 引入所有节点
from app.graph.nodes.router_node import router_node
from app.graph.nodes.expand_node import expand_node
from app.graph.nodes.retrieval_node import retrieval_node
from app.graph.nodes.generate_node import generate_node
from app.graph.nodes.verification_node import verification_node
from app.graph.nodes.execution_node import execution_node


MAX_EXECUTION_RETRIES = 1


# ==========================================
# 1. 定义判断逻辑 (路标)
# ==========================================

MAX_RETRIES = 3  # 全局最大重试次数

# 定义最大重试次数 (Verifier 专用)
MAX_VERIFY_RETRIES = 2


def route_after_verification(state: AgentState):
    """
    路标 1：验证后的去向 (Router)
    负责调度逻辑：通过、重试、还是强制执行。
    """
    # 1. 优先检查：如果已经验证通过
    if state.get("verified"):
        print("🟢 [Router] Verification Passed -> Moving to Execution")
        return "execute"

    # 2. 检查重试次数
    # 注意：verification_node 刚刚已经把 retry_count +1 了
    current_count = state.get("retry_count", 0)

    # 3. 判断是否超过上限
    if current_count > MAX_VERIFY_RETRIES:
        # 💀 次数耗尽：这里是决策点
        # 选择 A: 强制执行 (Force Pass) - "虽然写得烂，但死马当活马医跑跑看"
        print(f"🛑 [Router] Max retries ({MAX_VERIFY_RETRIES}) reached. Force Passing to Execution.")
        return "execute"

        # 选择 B: 直接结束 (Give Up) - "写得太烂了，不跑了"
        # print(f"🛑 [Router] Max retries reached. Giving up.")
        # return END

    # 4. 还有机会 -> 回去重写
    print(f"🔄 [Router] Verification Failed (Count: {current_count}). Retrying Generation...")
    return "retry"

def route_after_execution(state: AgentState):
    """
    路标 2：执行后的去向
    """
    # 1. 优先检查是否成功
    # 如果执行成功 (is_executable=True)，直接结束，皆大欢喜
    if state.get("is_executable"):
        print("✅ [Graph] Execution Success -> Finishing")
        return END

    # 2. 检查 "执行专用" 的重试计数器
    # ⚠️ 注意：这里读取的是 execution_retries，而不是全局的 retry_count
    current_exec_retries = state.get("execution_retries", 0)

    # 3. 判断是否还有机会
    # 逻辑：如果当前已经是第 2 次尝试 (retries > 1)，说明修过一次还是挂了 -> 停止
    if current_exec_retries > MAX_EXECUTION_RETRIES:
        print(f"🛑 [Graph] Max execution retries ({MAX_EXECUTION_RETRIES}) reached. Stopping.")
        return END

    # 4. 还有机会 -> 回炉重造
    # 此时 state 里已经包含了 execution_error，Generator 会看到它
    print(
        f"❌ [Graph] Execution Failed: {state.get('execution_error')[:50]}... -> Retrying (Attempt {current_exec_retries})")

    # 返回 "retry_generation" (对应你 add_conditional_edges 里的 key)
    # 确保这个 key 指向 "generate_node"
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
workflow.add_node("execution_node", execution_node)

# --- 连线 (Edge) ---

# 1. 线性流程
workflow.set_entry_point("router_node")
workflow.add_edge("router_node", "expand_node")
workflow.add_edge("expand_node", "retrieval_node")
workflow.add_edge("retrieval_node", "generate_node")
workflow.add_edge("generate_node", "verification_node")

# 2. 条件分支 A: 验证节点 -> (执行 或 重试)
workflow.add_conditional_edges(
    "verification_node",
    route_after_verification,
    {
        "execute": "execution_node",
        "retry": "generate_node",
        END: END # 对应 Max Retries 的情况
    }
)

# 3. 条件分支 B: 执行节点 -> (结束 或 重试)
# 🔥 这里包含了成功结束的逻辑，所以不需要额外的 add_edge(..., END)
workflow.add_conditional_edges(
    "execution_node",
    route_after_execution,
    {
        END: END,                          # 成功 -> 结束
        "retry_generation": "generate_node" # 失败 -> 回炉重造 (进入 ICU 模式)
    }
)

# 编译图
app = workflow.compile()