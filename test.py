import asyncio
import json

from app.core.agent_graph import retrieve_node, generate_node
from app.core.state import AgentState, IntentType, RouterOutput



async def debug_system():
    # 1. 模拟一个初始状态
    state: AgentState = {
        "question": "统计所有订单总销售额",
        "intent": IntentType.DATA_QUERY,
        "intent_data": RouterOutput(
            reason="test", intent=IntentType.DATA_QUERY,
            needs_schema=True, needs_knowledge=False, needs_clarify=False,
            schema_query="订单销售额", knowledge_keywords=[], clarify_questions=[]
        ),
        "history": [],
        "retry_count": 0
    }

    print("\n--- [Step 1: 运行 Retrieve 节点] ---")
    ret_result = await retrieve_node(state)
    state.update(ret_result)  # 更新状态

    schema_content = state.get("rag_contexts", {}).get("schema", "")
    print(f"DEBUG: 检索到的 Schema 长度: {len(schema_content)}")
    if not schema_content:
        print("❌ 错误: Retrieve 节点没拿到任何 Schema！请检查 fetch_table_metadata 的连接。")
    else:
        print(f"✅ Schema 内容预览: {schema_content[:200]}...")

    print("\n--- [Step 2: 模拟 Reflection 失败后的 Repair 逻辑] ---")
    # 模拟一个错误的反馈
    state["reflection_passed"] = False
    state["reflection_feedback"] = "你应该使用 t_order_item 表计算，不要只看 t_order。"

    print("\n--- [Step 3: 运行 Generate 节点] ---")
    # 我们看 Generate 节点生成的 Prompt
    gen_result = await generate_node(state)
    print(f"LLM 生成的 SQL: {gen_result.get('generated_sql')}")


if __name__ == "__main__":
    asyncio.run(debug_system())