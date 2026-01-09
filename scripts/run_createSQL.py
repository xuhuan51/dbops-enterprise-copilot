import requests
import json
import time
from tabulate import tabulate  # pip install tabulate

# 配置你的 API 地址
API_URL = "http://127.0.0.1:8000/api/v1/query"


def run_tests():
    # 1. 加载测试集
    with open("test_cases.json", "r", encoding="utf-8") as f:
        cases = json.load(f)

    results = []
    print(f"🚀 开始执行 {len(cases)} 个测试用例...\n")

    for case in cases:
        print(f"Testing [{case['id']}] {case['category']} ...", end="", flush=True)

        # 2. 构造请求
        payload = {
            "query": case["query"],
            "session_id": case["session_id"],
            "user_id": "tester"
        }

        start_time = time.time()
        try:
            # 发送请求
            resp = requests.post(API_URL, json=payload, timeout=30)
            data = resp.json()
            duration = round(time.time() - start_time, 2)

            # 3. 验证结果
            # A. 检查 HTTP 状态
            if resp.status_code != 200:
                print(f"\n❌ [CRITICAL] 服务端报错 (Code {resp.status_code}):")
                # 尝试打印 JSON，如果不是 JSON 就打印纯文本
                try:
                    print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
                except:
                    print(resp.text)

                status = f"❌ Error {resp.status_code}"
                detail = "Server Error (Check logs above)"
            # 🔥🔥🔥 修改结束 🔥🔥🔥
            else:
                # B. 检查意图分类是否正确
                # 注意：实际返回结构可能在 agent_meta 或 intent 字段，根据你的 agent_query.py 调整
                actual_intent = data.get("intent") or data.get("agent_meta", {}).get("intent", "UNKNOWN")

                if actual_intent != case["expected_intent"]:
                    status = "❌ Intent Mismatch"
                    detail = f"Exp: {case['expected_intent']}, Got: {actual_intent}"

                # C. 如果是 Data Query，检查 SQL 关键词
                elif case["expected_intent"] == "DATA_QUERY":
                    # 从返回中提取 SQL (你的 API 可能在 logs 或 agent_meta 中返回 SQL，或者你得把 SQL 透传出来)
                    # 这里假设 API 返回结果里不直接带 SQL，我们验证是否有数据返回
                    if "result" in data or "data" in data or isinstance(data, list):
                        status = "✅ Pass"
                        detail = "Data Returned"

                        # 如果你的 API 在 response 里透传了生成的 SQL，可以在这里做关键词检查
                        # sql = data.get("agent_meta", {}).get("generated_sql", "")
                        # missing = [kw for kw in case["expected_sql_keywords"] if kw.lower() not in sql.lower()]
                        # if missing:
                        #     status = "⚠️ SQL Logic?"
                        #     detail = f"Missing: {missing}"
                    else:
                        status = "❌ No Data"
                        detail = str(data)[:50]
                else:
                    # 闲聊/搜索类，只要有 message 就算过
                    if "message" in data:
                        status = "✅ Pass"
                        detail = "Response OK"
                    else:
                        status = "❌ Empty"
                        detail = "No message"

        except Exception as e:
            duration = 0
            status = "❌ Exception"
            detail = str(e)[:50]

        print(f" {status}")

        results.append([
            case["id"],
            case["category"],
            case["query"][:20] + "...",
            status,
            f"{duration}s",
            detail
        ])

        # 稍微歇一下，别把 LLM QPS 刷爆了
        time.sleep(1)

    # 4. 打印报告
    print("\n" + "=" * 50)
    print("📊 测试报告 (Test Report)")
    print("=" * 50)
    print(tabulate(results, headers=["ID", "Category", "Query", "Status", "Time", "Detail"], tablefmt="grid"))


if __name__ == "__main__":
    run_tests()