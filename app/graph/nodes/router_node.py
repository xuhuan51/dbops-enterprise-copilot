# app/graph/nodes/router_node.py

from typing import List
from langchain_core.messages import BaseMessage

from app.core.llm import get_llm
# ✅ 引入核心定义
from app.core.state import AgentState, RouterOutput, IntentType
from app.core.prompts import ONE_PASS_ROUTER_PROMPT
from app.core.logger import logger


router_llm = get_llm()


def _format_history(history: List[BaseMessage]) -> str:
    """
    辅助函数：将聊天记录格式化为字符串，只取最近 3 条以节省 Token
    """
    if not history:
        return "无"

    # 简单的格式化：Role: Content
    lines = []
    for m in history[-3:]:
        role = getattr(m, "type", "unknown")
        content = getattr(m, "content", str(m))
        lines.append(f"{role}: {content}")

    return "\n".join(lines)


async def router_node(state: AgentState):
    """
    节点功能：
    1. 意图识别 (Intent Classification)
    2. 资源开关控制 (Resource Switching)
    3. 预算与复杂度预估 (Budgeting)
    """
    trace_id = state.get("trace_id", "N/A")
    question = state.get("question", "")
    history: List[BaseMessage] = state.get("history", [])

    print(f"\n{'=' * 30} [🚦 透视: ROUTER (指挥与预算)] {'=' * 30}")
    print(f"❓ 问题: {question}")

    history_text = _format_history(history)

    try:
        # 1. 调用 LLM 进行结构化输出
        # prompt 里的 {history} 和 {question} 会在这里被填充
        prompt = ONE_PASS_ROUTER_PROMPT.format(history=history_text, question=question)

        # with_structured_output 强制 LLM 输出符合 RouterOutput 定义的 JSON
        router_output: RouterOutput = await router_llm.with_structured_output(RouterOutput).ainvoke(prompt)

    except Exception as e:
        logger.error(f"[Router] LLM failed: {e}", extra={"trace_id": trace_id})

        # 🛡️ Fallback 兜底策略：
        # 如果 Router LLM 挂了（超时或解析失败），为了不让系统崩盘，
        # 我们默认假设用户是在查数据（最常见场景），并拉满配置。
        print(f"⚠️ [Router] 触发兜底策略: {e}")
        router_output = RouterOutput(
            intent=IntentType.DATA_QUERY,
            reason="Router LLM Error (Fallback)",
            needs_schema=True,
            needs_knowledge=True,  # 默认开启知识库
            needs_clarify=False,
            query_complexity="hard",  # 默认按最难处理
            pruning_budget_cols=60,  # 预算拉满
            clarify_questions=[]
        )

    # =======================================================
    # 🔥 硬规则干预 (Hard Rules) - 治愈 LLM 的“盲目自信”或“过度节约”
    # =======================================================

    if router_output.intent == IntentType.DATA_QUERY:
        # 规则 1: 只要查数据，强制开启知识库检索
        # 原因：Orchestrator 跑一下知识库开销很小，但漏搜代价很大。
        if not router_output.needs_knowledge:
            print(f"🛡️ [Router] 硬规则: 强制开启 needs_knowledge")
            router_output.needs_knowledge = True

        # 规则 2: 如果 LLM 判定为 Hard 模式，强制预算拉满
        # 原因：防止 LLM 虽然识别出很难，但因为 Prompt 指令微调问题给了个低预算 (40)。
        if router_output.query_complexity == "hard" and router_output.pruning_budget_cols < 60:
            print(f"🛡️ [Router] 硬规则: Hard 模式强制修正预算至 60")
            router_output.pruning_budget_cols = 60

    # 打印决策结果供调试
    print(f"✅ 意图: {router_output.intent.value} ({router_output.query_complexity})")
    print(f"💰 预算: Top-{router_output.pruning_budget_cols} Cols")
    print(f"📋 开关: Schema={router_output.needs_schema} | Knowledge={router_output.needs_knowledge}")

    if router_output.needs_clarify:
        print(f"🗣️ 建议追问: {router_output.clarify_questions}")

    print(f"{'=' * 80}\n")

    return {
        # 更新全局 State 的 intent
        "intent": router_output.intent,

        # 将详细的决策包存入 intent_data，供后续节点（如 Expand）读取
        "intent_data": router_output,
    }