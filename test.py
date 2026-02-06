import asyncio
import logging
from dotenv import load_dotenv
from app.core.state import AgentState
# 🔥 核心：引入编译好的图 app
from app.graph.graph import app

# 配置日志，让你能看到 Verification 的内部过程
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)

# 🔥🔥🔥 新增：强力屏蔽噪音 🔥🔥🔥
# 屏蔽 HTTP 请求日志
logging.getLogger("httpx").setLevel(logging.WARNING)
# 屏蔽 ChromaDB/SentenceTransformer 的进度条日志 (通常是 tqdm)
logging.getLogger("chromadb").setLevel(logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)

BIRD_DATASET = [
    {
        "question_id": 6,
        "db_id": "california_schools",
        "question": "Among the schools with the SAT test takers of over 500, please list the schools that are magnet schools or offer a magnet program.",
        "SQL": "SELECT T2.School FROM satscores AS T1 INNER JOIN schools AS T2 ON T1.cds = T2.CDSCode WHERE T2.Magnet = 1 AND T1.NumTstTakr > 500",
    },
    {
        "question_id": 7,
        "db_id": "california_schools",
        "question": "What is the phone number of the school that has the highest number of test takers with an SAT score of over 1500?",
        "SQL": "SELECT T2.Phone FROM satscores AS T1 INNER JOIN schools AS T2 ON T1.cds = T2.CDSCode ORDER BY T1.NumGE1500 DESC LIMIT 1",
    },
    {
        "question_id": 8,
        "db_id": "california_schools",
        "question": "What is the number of SAT test takers of the schools with the highest FRPM count for K-12 students?",
        "SQL": "SELECT NumTstTakr FROM satscores WHERE cds = ( SELECT CDSCode FROM frpm ORDER BY `FRPM Count (K-12)` DESC LIMIT 1 )",
    }
]


async def run_graph_test():
    print(f"\n{'=' * 80}")
    print(f"🚀 [LangGraph] BIRD 数据集全链路测试 (自动循环版)")
    print(f"{'=' * 80}\n")

    for case in BIRD_DATASET:
        q_id = case.get("question_id")
        question = case.get("question")
        db_id = case.get("db_id")
        gold_sql = case.get("SQL")

        print(f"🧪 Case ID: {q_id}")
        print(f"❓ Question: {question}")

        inputs = {
            "trace_id": f"test-bird-{q_id}",
            "question": question,
            "db_id": db_id,
            "history": []
        }

        try:
            print("   ⏳ Graph is running... (请观察下方日志流)")

            # 🔥 这一句是关键！它启动了自动驾驶模式
            final_state = await app.ainvoke(inputs)

            print("✅ Done!")

            # 结果分析
            gen_sql = final_state.get("generated_sql", "")
            retry_count = final_state.get("retry_count", 0)
            verified = final_state.get("verified", False)
            feedback = final_state.get("feedback", "")

            print(f"\n   📝 [执行报告]")
            print(f"      🔄 重试次数: {retry_count}")
            print(f"      🛡️ 最终验证: {'PASS' if verified else 'FAIL'}")
            if not verified:
                print(f"      👀 失败反馈: {feedback}")
            print(f"      🤖 最终 SQL: \033[92m{gen_sql}\033[0m")
            print(f"      🔑 标准 SQL: \033[93m{gold_sql}\033[0m")

            norm_gen = " ".join(gen_sql.lower().split())
            norm_gold = " ".join(gold_sql.lower().split())

            if norm_gen == norm_gold:
                print("      🎉 [Perfect Match]")
            else:
                print("      🤔 [Check Logic]")

        except Exception as e:
            print(f"\n❌ Graph Execution Failed: {e}")
            import traceback
            traceback.print_exc()

        print(f"\n{'-' * 80}\n")


if __name__ == "__main__":
    asyncio.run(run_graph_test())