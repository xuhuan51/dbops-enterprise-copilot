from langgraph.graph import StateGraph, END
from app.core.state import AgentState

# ==========================================
# 0. 引入所有节点
# ==========================================
from app.graph.nodes.router_node import router_node
from app.graph.nodes.expand_node import expand_node
from app.graph.nodes.retrieval_node import retrieval_node
from app.graph.nodes.column_selector_node import column_selector_node
from app.graph.nodes.generate_node import generate_node
from app.graph.nodes.verification_node import verification_node
from app.graph.nodes.execution_node import execution_node
# 👇 新增：引入分析师节点
from app.graph.nodes.analysis_node import analysis_node

# ==========================================
# 1. 定义判断逻辑 (路标)
# ==========================================

MAX_VERIFY_RETRIES = 2
MAX_EXECUTION_RETRIES = 1


def route_after_verification(state: AgentState):
    """
    验证后的去向：
    1. 验证通过 -> 去执行
    2. 验证失败但次数耗尽 -> 强制去执行 (赌一把)
    3. 验证失败且有次数 -> 回去重写 SQL
    """
    if state.get("verified"):
        print("🟢 [Graph] Verification Passed -> Execution")
        return "execute"

    current_count = state.get("retry_count", 0)
    if current_count > MAX_VERIFY_RETRIES:
        print(f"🛑 [Graph] Max verify retries ({MAX_VERIFY_RETRIES}) reached. Force Passing.")
        return "execute"

    print(f"🔄 [Graph] Verification Failed -> Retrying Generation (Attempt {current_count})")
    return "retry"


def route_after_execution(state: AgentState):
    """
    执行后的去向 (关键修改点)：
    1. 执行成功 -> 去分析 (Analysis) 🧠
    2. 执行失败且次数耗尽 -> 结束 (END)
    3. 执行失败且有次数 -> 回去重写 SQL (Retry)
    """
    if state.get("is_executable"):
        print("✅ [Graph] Execution Success -> Analysis")
        return "analyze"  # 👈 成功后，交给分析师处理

    current_exec_retries = state.get("execution_retries", 0)

    if current_exec_retries > MAX_EXECUTION_RETRIES:
        print(f"🛑 [Graph] Max execution retries ({MAX_EXECUTION_RETRIES}) reached. Terminating.")
        return END

    print(f"❌ [Graph] Execution Failed -> Retrying Generation (Attempt {current_exec_retries})")
    return "retry_generation"


# ==========================================
# 2. 构建图 (Workflow)
# ==========================================

workflow = StateGraph(AgentState)

# --- A. 添加节点 ---
workflow.add_node("router_node", router_node)
workflow.add_node("expand_node", expand_node)
workflow.add_node("retrieval_node", retrieval_node)
workflow.add_node("column_selector_node", column_selector_node)
workflow.add_node("generate_node", generate_node)
workflow.add_node("verification_node", verification_node)
workflow.add_node("execution_node", execution_node)
workflow.add_node("analysis_node", analysis_node)  # 👈 注册新节点

# --- B. 连线 (Edge) ---

# 1. 核心链路 (线性部分: 理解 -> 检索 -> 选列 -> 生成 -> 验证)
workflow.set_entry_point("router_node")
workflow.add_edge("router_node", "expand_node")
workflow.add_edge("expand_node", "retrieval_node")
workflow.add_edge("retrieval_node", "column_selector_node")
workflow.add_edge("column_selector_node", "generate_node")
workflow.add_edge("generate_node", "verification_node")

# 2. 条件分支 A: 验证反馈循环 (Generate <-> Verify)
workflow.add_conditional_edges(
    "verification_node",
    route_after_verification,
    {
        "execute": "execution_node",  # 通过 -> 执行
        "retry": "generate_node",  # 失败 -> 重写
    }
)

# 3. 条件分支 B: 执行后处理 (Execution -> Analysis or Retry)
workflow.add_conditional_edges(
    "execution_node",
    route_after_execution,
    {
        "analyze": "analysis_node",  # 👈 成功 -> 分析
        "retry_generation": "generate_node",  # 失败 -> 重写 (ICU模式)
        END: END  # 彻底失败 -> 结束
    }
)

# 4. 最终链路: 分析 -> 结束
workflow.add_edge("analysis_node", END)

# 编译图
app = workflow.compile()