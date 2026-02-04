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

# ==============================================================================
# 📝 BIRD 数据集原生格式 (直接粘贴 JSON 对象到这里)
# ==============================================================================
BIRD_DATASET = [
    {
        "question_id": 6,
        "db_id": "california_schools",
        "question": "Among the schools with the SAT test takers of over 500, please list the schools that are magnet schools or offer a magnet program.",
        "evidence": "Magnet schools or offer a magnet program means that Magnet = 1",
        "SQL": "SELECT T2.School FROM satscores AS T1 INNER JOIN schools AS T2 ON T1.cds = T2.CDSCode WHERE T2.Magnet = 1 AND T1.NumTstTakr > 500",
        "difficulty": "simple"
    },
    {
        "question_id": 7,
        "db_id": "california_schools",
        "question": "What is the phone number of the school that has the highest number of test takers with an SAT score of over 1500?",
        "evidence": "",
        "SQL": "SELECT T2.Phone FROM satscores AS T1 INNER JOIN schools AS T2 ON T1.cds = T2.CDSCode ORDER BY T1.NumGE1500 DESC LIMIT 1",
        "difficulty": "simple"
    },
    {
        "question_id": 8,
        "db_id": "california_schools",
        "question": "What is the number of SAT test takers of the schools with the highest FRPM count for K-12 students?",
        "evidence": "",
        "SQL": "SELECT NumTstTakr FROM satscores WHERE cds = ( SELECT CDSCode FROM frpm ORDER BY `FRPM Count (K-12)` DESC LIMIT 1 )",
        "difficulty": "simple"
    }
]


async def run_end_to_end_test():
    print(f"\n{'=' * 80}")
    print(f"🚀 [End-to-End] BIRD 数据集全链路测试")
    print(f"{'=' * 80}\n")

    for case in BIRD_DATASET:
        # 1. 提取 BIRD 格式字段
        q_id = case.get("question_id", "Unknown ID")
        question = case.get("question", "")
        db_id = case.get("db_id", "")
        gold_sql = case.get("SQL", "")  # BIRD 里的标准答案键名是 "SQL"
        evidence = case.get("evidence", "")

        print(f"🧪 测试用例 ID: {q_id}")
        print(f"❓ 问题: {question}")
        if evidence:
            print(f"🕵️ 线索 (Evidence): {evidence}")

        # 2. 初始化 State (把 evidence 放入 intent_data 可能会更好，但这里先不做硬塞，让 Agent 自己去悟)
        # 如果你想把 evidence 作为 hint 传进去，可以在这里处理，
        # 但标准的 Text-to-SQL 测试通常不直接给 evidence，除非作为 Knowledge 检索的一部分。
        state = AgentState(
            trace_id=f"test-bird-{q_id}",
            question=question,
            db_id=db_id,
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

            # Step 3: Retrieval (Columns + Rules + Values)
            # Retrieval Node 内部会自动打印详细的 Debug Info (Tier 1 / Tier 2 / Rescue)
            print("   Running Retrieval...", end=" ", flush=True)
            state.update(await retrieval_node(state))
            print("✅")

            # Step 4: Generator
            print("   Running Generator...", end=" ", flush=True)
            state.update(await generate_node(state))
            print("✅")

            # ------------------------------------------------
            # 📊 阅卷时刻
            # ------------------------------------------------
            gen_sql = state.get("generated_sql", "Generating Failed")

            print(f"\n   📝 [结果对比]")
            print(f"   🤖 生成 SQL: \033[92m{gen_sql}\033[0m")
            print(f"   🔑 标准 SQL: \033[93m{gold_sql}\033[0m")

            # 简单的完全匹配检查 (注意：实际评测通常用 Execution Accuracy，这里仅供参考)
            # 标准化 SQL 字符串 (去掉多余空格、转小写) 来做简单对比
            norm_gen = " ".join(gen_sql.lower().split())
            norm_gold = " ".join(gold_sql.lower().split())

            if norm_gen == norm_gold:
                print("      🎉 [Perfect Match] SQL 字面完全一致！")
            else:
                # 简单的逻辑检查提示
                if "join" in norm_gold and "join" not in norm_gen:
                    print("      ⚠️ [Structure Warning] 标准答案用了 JOIN，但你没用。")
                if "limit" in norm_gold and "limit" not in norm_gen:
                    print("      ⚠️ [Logic Warning] 标准答案用了 LIMIT，但你没用。")

        except Exception as e:
            print(f"\n❌ 流程中断: {e}")
            import traceback
            traceback.print_exc()

        print(f"\n{'-' * 80}\n")


if __name__ == "__main__":
    asyncio.run(run_end_to_end_test())