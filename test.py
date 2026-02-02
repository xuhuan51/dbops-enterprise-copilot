import asyncio
import os
from dotenv import load_dotenv

# 载入环境变量
load_dotenv()

from app.core.state import AgentState
from app.graph.nodes.router_node import router_node
from app.graph.nodes.expand_node import expand_node
from app.graph.nodes.retrieval_node import retrieval_node

# ==========================================
# 🧪 测试用例 (BIRD California Schools)
# ==========================================
TEST_CASES = [
    {
        "id": "Case-0 (Complex)",
        "question": "What is the highest eligible free rate for K-12 students in the schools in Alameda County?",
        "db_id": "california_schools",
        "focus": "能否找出 eligible_free_rate 和 county"
    },
    {
        "id": "Case-1 (Filter+Sort)",
        "question": "Please list the lowest three eligible free rates for students aged 5-17 in continuation schools.",
        "db_id": "california_schools",
        "focus": "能否找出 continuation 过滤条件"
    },
    {
        "id": "Case-2 (Simple Lookup)",
        "question": "Please list the zip code of all the charter schools in Fresno County Office of Education.",
        "db_id": "california_schools",
        "focus": "能否通过 Metric Search 找出 Zip Code"
    }
]


def _safe_get(obj, attr, default=None):
    return getattr(obj, attr, default) if obj is not None else default


async def run_integration_test():
    print(f"\n{'=' * 70}")
    print(f"🚀 BIRD 集成测试: Router -> Expand(v3.0) -> Retrieval(Split&Conquer)")
    print(f"{'=' * 70}\n")

    for case in TEST_CASES:
        print(f"🧪 [{case['id']}] Question: \"{case['question']}\"")
        print(f"   🎯 关注点: {case['focus']}")

        # 1. 初始化 State
        state = AgentState(
            trace_id="test-integration",
            question=case["question"],
            db_id=case["db_id"],
            history=[]
        )

        # ------------------------------------------------------
        # Step 1: Router Node
        # ------------------------------------------------------
        print(f"   [1/3] Running Router...", end=" ", flush=True)
        try:
            r_res = await router_node(state)
            state.update(r_res)
            print("✅ Done")
        except Exception as e:
            print(f"❌ Error: {e}")
            continue

        # ------------------------------------------------------
        # Step 2: Expand Node (v3.0)
        # ------------------------------------------------------
        print(f"   [2/3] Running Expand...", end=" ", flush=True)
        try:
            e_res = await expand_node(state)
            state.update(e_res)
            print("✅ Done")
        except Exception as e:
            print(f"❌ Error: {e}")
            continue

        # 打印 Expand 结果，验证关键词生成
        intent = state.get("intent_data")
        hints = getattr(intent, "semantic_hints", None)
        keywords = getattr(intent, "search_keywords", [])

        print(
            f"         > Hints: Target='{_safe_get(hints, 'target_hint')}' | Metric='{_safe_get(hints, 'metric_hint')}'")
        print(f"         > Keywords: {keywords}")

        # ------------------------------------------------------
        # Step 3: Retrieval Node (Split & Conquer)
        # ------------------------------------------------------
        print(f"   [3/3] Running Retrieval...", end=" ", flush=True)
        try:
            # 这里会触发 RAGOrchestrator 的并发分治检索
            ret_res = await retrieval_node(state)
            state.update(ret_res)
            print("✅ Done")
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()
            continue

        # ------------------------------------------------------
        # 📊 结果透视 (验证分治策略是否生效)
        # ------------------------------------------------------
        cols = state.get("retrieved_columns", [])
        tables = state.get("retrieved_tables", [])

        print(f"\n   🧐 [Retrieval Report]")
        print(f"      - 涉及表 ({len(tables)}): {tables}")
        print(f"      - 召回列 Top-10 (共 {len(cols)} 个):")

        # 打印前10个列，重点看 Source 字段
        for i, col in enumerate(cols[:10]):
            t_name = col.get("table")
            c_name = col.get("column")
            score = col.get("score", 0.0)
            source = col.get("source", "Unknown")  # 关键：看看是谁捞回来的

            # 高亮显示高分列
            mark = "⭐" if i < 3 else "  "
            print(f"        {mark} {t_name}.{c_name:<20} | Score: {score:.4f} | Src: {source}")

        print(f"\n{'-' * 70}\n")


if __name__ == "__main__":
    asyncio.run(run_integration_test())