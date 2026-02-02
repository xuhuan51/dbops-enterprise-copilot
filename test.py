import asyncio
import os
import json
from dotenv import load_dotenv

# 载入环境变量
load_dotenv()

from app.core.state import AgentState
from app.graph.nodes.router_node import router_node
from app.graph.nodes.expand_node import expand_node
from app.graph.nodes.retrieval_node import retrieval_node
from app.graph.nodes.generate_node import generate_node

# ==========================================
# 📝 测试数据集 (带标准答案)
# ==========================================
TEST_CASES = [
    {
        "id": "Case-0 (Rate Calculation)",
        "question": "What is the highest eligible free rate for K-12 students in the schools in Alameda County?",
        "db_id": "california_schools",
        "gold_sql": "SELECT `Free Meal Count (K-12)` / `Enrollment (K-12)` FROM frpm WHERE `County Name` = 'Alameda' ORDER BY 1 DESC LIMIT 1"
    },
    {
        "id": "Case-1 (Filter & Sort)",
        "question": "Please list the lowest three eligible free rates for students aged 5-17 in continuation schools.",
        "db_id": "california_schools",
        "gold_sql": "SELECT ... FROM frpm WHERE `Educational Option Type` = 'Continuation School' ... ORDER BY ... ASC LIMIT 3"
    },
    {
        "id": "Case-2 (Join Query)",
        "question": "Please list the zip code of all the charter schools in Fresno County Office of Education.",
        "db_id": "california_schools",
        "gold_sql": "SELECT T2.Zip FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE ..."
    }
]


async def run_end_to_end_test():
    print(f"\n{'=' * 80}")
    print(f"🚀 [End-to-End] 全链路生成测试 (Router -> Expand -> Retrieval -> Generator)")
    print(f"{'=' * 80}\n")

    for case in TEST_CASES:
        print(f"🧪 测试用例: {case['id']}")
        print(f"❓ 问题: {case['question']}")

        # 1. 初始化 State
        state = AgentState(
            trace_id="test-final",
            question=case["question"],
            db_id=case["db_id"],
            history=[]
        )

        try:
            # Step 1: Router
            print("   Running Router...", end=" ", flush=True)
            state.update(await router_node(state))
            print("✅")

            # Step 2: Expand
            print("   Running Expand...", end=" ", flush=True)
            state.update(await expand_node(state))
            print("✅")

            # Step 3: Retrieval (Columns + Rules + Paths)
            print("   Running Retrieval...", end=" ", flush=True)
            # 执行节点
            retrieval_res = await retrieval_node(state)
            state.update(retrieval_res)
            print("✅")

            # =========================================================
            # 🕵️‍♂️ [DEBUG] 检索侦探：在这里检查你的“子弹”有没有上膛
            # =========================================================
            print(f"\n   🔍 [Retrieval Debug Info]")

            # 1. 检查 Schema Linking (是否找对了列?)
            # Case-0 就要看这里有没有 'Enrollment' 和 'Free Meal Count'
            # Case-1 就要看这里有没有 'Educational Option Type' (而不是 School Type)
            cols = state.get("retrieved_columns", [])
            print(f"      📑 Retrieved Columns ({len(cols)} found):")
            for i, col in enumerate(cols):
                if i >= 5:  # 防止列太多刷屏，只看前5个核心的
                    print(f"         ... (and {len(cols) - 5} more)")
                    break
                # 打印列名 + 采样值 (采样值能帮你确认数据长啥样)
                samples = col.get("sample_values", [])[:3]
                print(f"         - {col.get('table')}.{col.get('column')} | Samples: {samples}")

            # 2. 检查 Value Matches (是否解决了 Case-1 的值幻觉?)
            # Case-1 必须看到: 'Continuation' -> 'Continuation School'
            vals = state.get("value_matches", [])
            print(f"      🎯 Value Matches (关键词命中):")
            if vals:
                for v in vals:
                    # 高亮打印，一眼看到有没有命中
                    print(f"         ✨ \033[96m{v}\033[0m")
            else:
                print(f"         ❌ No specific values matched (Warning for filtering questions!)")

                # 3. 检查知识库
                rules = state.get("business_rules", [])
                print(f"      📚 Knowledge Rules:")
                if rules:
                    for r in rules:
                        # 🛠️ 修复逻辑：如果是字典就取 content，如果是字符串直接用
                        if isinstance(r, dict):
                            content = r.get('content', str(r))
                        else:
                            content = str(r)

                        print(f"         💡 {content[:80]}...")  # 只打印前80个字符
                else:
                    print(f"         (No external rules found)")
            # =========================================================

            # Step 4: Generator (LLM Writing SQL)
            print("   Running Generator...", end=" ", flush=True)
            res = await generate_node(state)
            state.update(res)
            print("✅")

            # ------------------------------------------------
            # 📊 阅卷时刻
            # ------------------------------------------------
            gen_sql = state.get("generated_sql", "Generating Failed")

            print(f"\n   📝 [结果对比]")
            print(f"   🤖 生成 SQL: \033[92m{gen_sql}\033[0m")
            print(f"   🔑 标准 SQL: \033[93m{case['gold_sql']}\033[0m")

            # 简单的关键词检查
            if "JOIN" in case['gold_sql'] and "JOIN" in gen_sql:
                print("      👉 JOIN 结构检测: ✅ 成功检测到多表连接")

            # 简单的值检查 (针对 Case-1)
            if "Continuation School" in case['gold_sql'] and "Continuation School" not in gen_sql:
                print("      ⚠️ [值匹配警告] 标准答案用了 'Continuation School'，但你没生成。请检查上面的 Value Matches!")

        except Exception as e:
            print(f"\n❌ 流程中断: {e}")
            import traceback
            traceback.print_exc()

        print(f"\n{'-' * 80}\n")

if __name__ == "__main__":
    asyncio.run(run_end_to_end_test())