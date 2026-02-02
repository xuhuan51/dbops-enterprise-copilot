import asyncio
from dotenv import load_dotenv

# 载入环境变量
load_dotenv()

from app.core.state import AgentState
from app.graph.nodes.router_node import router_node
from app.graph.nodes.expand_node import expand_node

# ==========================================
# 测试用例 (BIRD California Schools)
# ==========================================
TEST_CASES = [
    {
        "question_id": 0,
        "db_id": "california_schools",
        "question": "What is the highest eligible free rate for K-12 students in the schools in Alameda County?",
    },
    {
        "question_id": 1,
        "db_id": "california_schools",
        "question": "Please list the lowest three eligible free rates for students aged 5-17 in continuation schools.",
    },
    {
        "question_id": 2,
        "db_id": "california_schools",
        "question": "Please list the zip code of all the charter schools in Fresno County Office of Education.",
    }
]


def _safe_get(obj, attr, default=None):
    """安全获取属性，防止 NoneType Error"""
    return getattr(obj, attr, default) if obj is not None else default


async def run_pipeline_test():
    print(f"\n{'=' * 60}")
    print(f"🚀 BIRD Pipeline 测试 (Router -> Expand v3.0)")
    print(f"{'=' * 60}\n")

    for i, case in enumerate(TEST_CASES):
        question = case["question"]
        db_id = case["db_id"]

        print(f"🧪 [Case {i}] Question: \"{question}\"")

        # 1) 构造初始 State
        state = AgentState(
            trace_id=f"test-{i}",
            question=question,
            db_id=db_id,
            history=[]
        )

        # ==========================================
        # Step 1: Router Node
        # ==========================================
        print(f"   Running Router Node...", end=" ")
        try:
            router_result = await router_node(state)
            state.update(router_result)
            print("✅ Done")
        except Exception as e:
            print(f"❌ Failed: {e}")
            print(f"{'-' * 50}\n")
            continue

        intent_data = state.get("intent_data")

        # ==========================================
        # Step 2: Expand Node (v3.0)
        # ==========================================
        print(f"   Running Expand Node...", end=" ")
        try:
            expand_result = await expand_node(state)
            state.update(expand_result)
            print("✅ Done")
        except Exception as e:
            print(f"❌ Failed: {e}")
            print(f"{'-' * 50}\n")
            continue

        final_intent_data = state.get("intent_data")

        # ==========================================
        # 结果透视 (v3.0 标准输出)
        # ==========================================
        caps = _safe_get(final_intent_data, "capabilities", [])
        hints = _safe_get(final_intent_data, "semantic_hints", None)
        keywords = _safe_get(final_intent_data, "search_keywords", [])
        schema_query_str = _safe_get(final_intent_data, "schema_query", "")

        print(f"\n   🧐 [透视结果 - v3.0 Final]")

        # 1. 核心动作
        print(f"   1️⃣  核心动作 (Capabilities): {caps}")

        # 2. 语义线索
        if hints:
            print(f"   2️⃣  语义线索 (Semantic Hints) -> 给 Generator 写 SQL 用:")
            print(f"      - 🎯 Target: {_safe_get(hints, 'target_hint')}")
            print(f"      - 📊 Metric: {_safe_get(hints, 'metric_hint')}")
            print(f"      - 🌪️ Filters: {_safe_get(hints, 'filter_hints', [])}")
            print(f"      - 🧱 Group:  {_safe_get(hints, 'group_hint')}")
            print(f"      - ⏱️ Time:   {_safe_get(hints, 'time_hint')}")
        else:
            print(f"   2️⃣  语义线索: ❌ 未生成")

        # 3. 检索关键词 (这是新架构的核心差异)
        print(f"   3️⃣  检索关键词 (Search Keywords) -> 给 Retriever 找表用:")
        if keywords:
            print(f"      🔑 List:   {keywords}")
            print(f"      📄 String: \"{schema_query_str}\"")
        else:
            print(f"      ⚠️ Warning: 关键词列表为空 (Retriever 将无法工作)")

        print(f"\n{'-' * 60}\n")


if __name__ == "__main__":
    asyncio.run(run_pipeline_test())