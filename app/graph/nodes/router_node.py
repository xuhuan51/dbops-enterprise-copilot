import json
from typing import List
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage
from app.core.llm import get_llm
from app.core.state import AgentState, RouterOutput, IntentType
from app.core.prompts import ONE_PASS_ROUTER_PROMPT
from app.core.logger import logger

router_llm = get_llm()


# ========================================================
# 🔧 内部专用模型：LLM 只做选择题
# ========================================================
class RawRouterPrediction(BaseModel):
    """仅用于接收 LLM 的核心判断"""
    intent: IntentType = Field(..., description="用户的核心意图")
    reason: str = Field(..., description="判断意图的理由")
    needs_clarify: bool = Field(default=False, description="是否需要进一步澄清")
    clarify_questions: List[str] = Field(default_factory=list, description="如果需要澄清，列出追问问题")


def _format_history(history: List[BaseMessage]) -> str:
    if not history:
        return "无"
    lines = []
    for m in history[-3:]:
        role = getattr(m, "type", "unknown")
        content = getattr(m, "content", str(m))
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def router_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    question = state.get("question", "")
    history = state.get("history", [])

    print(f"\n{'=' * 30} [🚦 透视: ROUTER (意图分流)] {'=' * 30}")
    print(f"❓ 问题: {question}")

    history_text = _format_history(history)

    try:
        # 1. 调用 LLM
        prompt = ONE_PASS_ROUTER_PROMPT.format(history=history_text, question=question)
        prediction: RawRouterPrediction = await router_llm.with_structured_output(RawRouterPrediction).ainvoke(prompt)

        # 2. 组装 RouterOutput
        # 注意：needs_schema 和 needs_knowledge 在 State 定义里默认已经是 True 了
        # 我们这里只需要把 Intent 填进去即可

        router_output = RouterOutput(
            intent=prediction.intent,
            reason=prediction.reason,
            needs_clarify=prediction.needs_clarify,
            clarify_questions=prediction.clarify_questions,

            # 强制硬编码 (保险起见，显式写出来)
            needs_schema=True,
            needs_knowledge=True
        )

        # 特殊处理：如果是纯闲聊，虽然 State 默认是 True，
        # 但为了逻辑合理性，通常闲聊不查库。
        # 如果你希望严格遵守"开关都为True"，可以把下面这三行注释掉。
        if prediction.intent == IntentType.CHAT:
            router_output.needs_schema = False
            router_output.needs_knowledge = False

    except Exception as e:
        logger.error(f"[Router] LLM failed: {e}", extra={"trace_id": trace_id})
        print(f"⚠️ [Router] 触发兜底策略: {e}")
        # 兜底对象
        router_output = RouterOutput(
            intent=IntentType.DATA_QUERY,
            reason="Fallback Error",
            needs_schema=True,
            needs_knowledge=True,
            needs_clarify=False
        )

    # 打印结果
    print(f"✅ 意图: {router_output.intent.value}")
    print(f"📋 开关: Schema={router_output.needs_schema} | Knowledge={router_output.needs_knowledge}")

    if router_output.needs_clarify:
        print(f"🗣️ 追问: {router_output.clarify_questions}")
    print(f"{'=' * 80}\n")

    return {
        "intent": router_output.intent,
        "intent_data": router_output,
    }


# ==========================================
# ⚡️ 独立测试入口
# ==========================================
if __name__ == "__main__":
    import asyncio
    import sys
    import os

    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))
    sys.path.append(project_root)


    async def router():
        print("🚀 开始测试 Router Node (No Budget Version)...")

        mock_state = AgentState({
            "trace_id": "test-uuid-001",
            "question": "帮我查一下上个月销售额最高的产品",
            "history": []
        })

        result = await router_node(mock_state)
        intent_data = result["intent_data"]

        print("\n👇👇👇 [Final Raw JSON Output] 👇👇👇")
        if hasattr(intent_data, "model_dump_json"):
            print(intent_data.model_dump_json(indent=4))
        else:
            print(json.dumps(intent_data.dict(), indent=4, ensure_ascii=False))


    asyncio.run(router())