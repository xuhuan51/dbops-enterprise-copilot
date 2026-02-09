import asyncio
import logging
import sqlite3
import os
import pandas as pd
from typing import List, Dict, Any, Set
from dotenv import load_dotenv
from app.core.state import AgentState
from app.core.config import settings  # 需要读取 BIRD_DB_ROOT
from app.graph.graph import app  # 引入编译好的图

# ==========================================
# 1. 日志配置 (保持清爽)
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("chromadb").setLevel(logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("execution_node").setLevel(logging.INFO)  # 让我们看到执行节点的日志

# ==========================================
# 2. 测试数据集 (BIRD Dev Set Samples)
# ==========================================
BIRD_DATASET = [
    {
        "question_id": 10,
        "db_id": "california_schools",
        "question": "For the school with the highest average score in Reading in the SAT test, what is its FRPM count for students aged 5-17?",
        "evidence": "",
        "SQL": "SELECT T2.`FRPM Count (Ages 5-17)` FROM satscores AS T1 INNER JOIN frpm AS T2 ON T1.cds = T2.CDSCode ORDER BY T1.AvgScrRead DESC LIMIT 1",
        "difficulty": "simple"
    },

    {
        "question_id": 9,
        "db_id": "california_schools",
        "question": "Among the schools with the average score in Math over 560 in the SAT test, how many schools are directly charter-funded?",
        "evidence": "",
        "SQL": "SELECT COUNT(T2.`School Code`) FROM satscores AS T1 INNER JOIN frpm AS T2 ON T1.cds = T2.CDSCode WHERE T1.AvgScrMath > 560 AND T2.`Charter Funding Type` = 'Directly funded'",
        "difficulty": "simple"
    },

    {
        "question_id": 11,
        "db_id": "california_schools",
        "question": "Please list the codes of the schools with a total enrollment of over 500.",
        "evidence": "Total enrollment can be represented by `Enrollment (K-12)` + `Enrollment (Ages 5-17)`",
        "SQL": "SELECT T2.CDSCode FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.`Enrollment (K-12)` + T2.`Enrollment (Ages 5-17)` > 500",
        "difficulty": "simple"
    }
]


# ==========================================
# 3. 辅助工具：执行标准 SQL (Ground Truth)
# ==========================================
def execute_gold_sql(db_id: str, sql: str) -> List[Dict[str, Any]]:
    """
    手动执行标准答案 SQL，用于获取正确结果
    """
    db_path = os.path.join(settings.BIRD_DB_ROOT, db_id, f"{db_id}.sqlite")

    if not os.path.exists(db_path):
        print(f"❌ [Test Error] Gold DB not found: {db_path}")
        return []

    try:
        # 只读模式连接
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute(sql)

        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()

        conn.close()
        # 转为 List[Dict]
        return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        print(f"❌ [Gold SQL Error]: {e}")
        return []


def normalize_result(result: List[Dict]) -> Set[tuple]:
    """
    结果标准化：忽略列名，只提取值的元组集合。
    原因：生成的 SQL 列名可能叫 "Phone"，标准答案可能叫 "T2.Phone"，
    但我们要比对的是【内容】是否一致。
    """
    if not result:
        return set()

    # 将字典的值提取出来，转为元组，再放入集合 (自动去重且忽略顺序)
    # 注意：对于 ORDER BY LIMIT 1 的问题，集合比对也是有效的（因为只有一个元素）
    # 对于严格排序问题，应该用 List 比对，但 Set 比对能覆盖 95% 场景
    normalized = []
    for row in result:
        # 将所有值转为字符串处理，防止 int vs float 的细微差异
        values = tuple(str(v).strip() for v in row.values())
        normalized.append(values)

    return set(normalized)


# ==========================================
# 4. 主测试逻辑
# ==========================================
async def run_graph_test():
    print(f"\n{'=' * 80}")
    print(f"🚀 [LangGraph] BIRD 数据集执行精度测试 (Execution Accuracy)")
    print(f"{'=' * 80}\n")

    pass_count = 0
    total_count = len(BIRD_DATASET)

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
            print("   ⏳ Agent Running... ", end="", flush=True)

            # 1. 运行 Agent
            final_state = await app.ainvoke(inputs)
            print("Done.")

            # 2. 提取 Agent 结果
            gen_sql = final_state.get("generated_sql", "N/A")
            pred_result = final_state.get("execution_result")  # List[Dict]
            exec_error = final_state.get("execution_error")

            # 3. 运行 Gold SQL (获取真值)
            gold_result = execute_gold_sql(db_id, gold_sql)

            # 4. 核心比对逻辑
            # 我们比对的是【结果集的内容】，忽略列名差异
            pred_set = normalize_result(pred_result)
            gold_set = normalize_result(gold_result)

            is_correct = (pred_set == gold_set) and (pred_result is not None)

            # 5. 输出报告
            print(f"\n   📝 [对比报告]")

            # SQL 对比
            print(f"      🤖 Gen SQL: \033[90m{gen_sql}\033[0m")  # 灰色显示
            print(f"      🔑 Gold SQL: \033[90m{gold_sql}\033[0m")

            # 结果对比
            if exec_error:
                print(f"      ❌ 执行报错: \033[91m{exec_error}\033[0m")
            else:
                # 只显示前 3 行结果，防止刷屏
                print(f"      📊 Gen Result ({len(pred_set)} rows): {list(pred_set)[:3]}...")
                print(f"      📊 Gold Result ({len(gold_set)} rows): {list(gold_set)[:3]}...")

            # 最终判定
            if is_correct:
                print(f"      🎉 \033[92m[EXECUTION MATCH] 结果一致！\033[0m")
                pass_count += 1
            else:
                print(f"      🚫 \033[91m[MISMATCH] 结果不一致\033[0m")
                # 如果不一样，可以打印差异
                diff = gold_set.symmetric_difference(pred_set)
                if diff:
                    print(f"         差异样本: {list(diff)[:3]}...")

        except Exception as e:
            print(f"\n❌ System Failed: {e}")
            import traceback
            traceback.print_exc()

        print(f"\n{'-' * 80}\n")

    print(f"🏆 测试结束: 准确率 {pass_count}/{total_count} ({(pass_count / total_count) * 100:.1f}%)")


if __name__ == "__main__":
    asyncio.run(run_graph_test())