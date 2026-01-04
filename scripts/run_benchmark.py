import sys
import os
import requests
import time
from colorama import init, Fore, Style

# 引入测试数据
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from benchmark_data import BENCHMARK_CASES

init(autoreset=True)

API_URL = "http://localhost:8000/api/v1/retrieve_tables_gate"


def check_hit(retrieved_tables, expected_keywords):
    """
    判定逻辑升级版：支持通配符模糊匹配
    """
    # 1. 熔断测试：如果期望是空，那么结果必须也是空才算对
    if not expected_keywords:
        return len(retrieved_tables) == 0

    hit_count = 0
    for exp in expected_keywords:
        is_found = False
        for ret in retrieved_tables:
            # 逻辑 A: 完全包含 (旧逻辑)
            # 比如 ret="t_order", exp="t_order" -> 中
            if exp in ret:
                is_found = True

            # 逻辑 B: 通配符前缀匹配 (新加的逻辑 ✨)
            # 比如 ret="t_order_*", exp="t_order"
            if ret.endswith("*"):
                # 去掉末尾的 _* (例如 t_order_* -> t_order)
                prefix = ret[:-2]
                # 如果期望值是以这个前缀开头的 (或者期望值就是前缀)
                if exp.startswith(prefix) or prefix.startswith(exp):
                    is_found = True

            if is_found:
                break

        if is_found:
            hit_count += 1

    # 全部命中才算 Pass
    return hit_count == len(expected_keywords)


def run_benchmark():
    total = len(BENCHMARK_CASES)
    passed = 0
    results_by_type = {}

    print(f"{Fore.CYAN}🚀 开始执行检索准确率评估 (共 {total} 个用例)...")
    print("=" * 60)

    for idx, case in enumerate(BENCHMARK_CASES):
        query = case["q"]
        expected = case["expected"]
        case_type = case["type"]

        if case_type not in results_by_type:
            results_by_type[case_type] = {"total": 0, "pass": 0}
        results_by_type[case_type]["total"] += 1

        print(f"Test [{idx + 1}/{total}] {case_type}: {query[:30]}...", end="", flush=True)

        try:
            start_time = time.time()

            # 🟢 修正点：加上了 user_id 字段
            payload = {
                "user_id": "benchmark_bot",
                "query": query,
                "topk": 10
            }

            resp = requests.post(API_URL, json=payload)
            cost_time = time.time() - start_time

            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("retrieval", {}).get("candidates", [])
                retrieved_names = [c["table"] for c in candidates]

                is_success = check_hit(retrieved_names, expected)

                if is_success:
                    print(f"{Fore.GREEN} [PASS] {Style.RESET_ALL} (Matches: {expected})")
                    passed += 1
                    results_by_type[case_type]["pass"] += 1
                else:
                    print(f"{Fore.RED} [FAIL] {Style.RESET_ALL}")
                    print(f"    ❌ Expected: {expected}")
                    print(f"    🔍 Actual:   {retrieved_names[:3]}...")
            else:
                print(f"{Fore.RED} [ERROR] HTTP {resp.status_code}")
                # 打印详细报错，方便调试
                print(f"    Server says: {resp.text}")

        except Exception as e:
            print(f"{Fore.RED} [EXCEPTION] {e}")

    # 打印最终报告
    accuracy = (passed / total) * 100
    print("\n" + "=" * 60)
    print(f"{Fore.YELLOW}🏆 测试报告 (Benchmark Report)")
    print("=" * 60)
    print(f"Total Cases:  {total}")
    print(f"Passed:       {passed}")
    print(f"Failed:       {total - passed}")
    print(f"Overall Acc:  {Fore.GREEN}{accuracy:.2f}%")
    print("-" * 60)
    print("详细分类表现：")
    for c_type, stats in results_by_type.items():
        if stats["total"] > 0:
            type_acc = (stats["pass"] / stats["total"]) * 100
            print(f"  - {c_type:<10}: {stats['pass']}/{stats['total']} ({type_acc:.1f}%)")
    print("=" * 60)


if __name__ == "__main__":
    run_benchmark()