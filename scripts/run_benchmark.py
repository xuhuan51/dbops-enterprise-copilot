import sys
import os
import time
from colorama import init, Fore, Style

# 🔥 确保能导入 app 模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 引入测试数据
from scripts.benchmark_data import BENCHMARK_CASES
# 🔥 直接引入核心函数 (根据你实际文件位置调整 import)
from app.api.v1.retrieve_tables import retrieve_tables

init(autoreset=True)


def check_hit(retrieved_tables, expected_keywords):
    """
    判定逻辑升级版：
    1. 列表中的 expected 如果是 ["A", "B"]，表示必须同时命中 A 和 B。
    2. 如果想表达 "A 或 B"，可以在 expected 里写成 "A|B" (这是新逻辑)。
    """
    if not expected_keywords:
        return len(retrieved_tables) == 0

    hit_count = 0
    for exp in expected_keywords:
        # 🔥 新增逻辑：支持 "A|B" 写法，表示命中其一即可
        # 例如: "u_user_base|user_dim"
        sub_choices = exp.split("|")

        is_sub_hit = False
        for sub in sub_choices:
            # 只要有一个 sub 命中了 retrieved，这个 exp 就算 pass
            for ret in retrieved_tables:
                # 1. 精确匹配
                if sub == ret:
                    is_sub_hit = True
                # 2. 前缀匹配 (t_order 命中 t_order_001)
                elif ret.startswith(sub + "_") or ret.startswith(sub + "."):
                    is_sub_hit = True

                if is_sub_hit: break
            if is_sub_hit: break

        if is_sub_hit:
            hit_count += 1

    return hit_count == len(expected_keywords)


def run_benchmark():
    total = len(BENCHMARK_CASES)
    passed = 0
    results_by_type = {}

    print(f"{Fore.CYAN}🚀 开始执行检索准确率评估 (Direct Function Call)...")
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

            # 🔥 直接调用函数，而不是 requests.post
            # 注意：retrieve_tables 返回的是 List[Dict]
            candidates_list = retrieve_tables(query, topk=10)

            cost_time = (time.time() - start_time) * 1000  # ms

            # 提取表名 (logical_table 或 full_name)
            retrieved_names = [c.get("logical_table") for c in candidates_list]

            is_success = check_hit(retrieved_names, expected)

            if is_success:
                print(f"{Fore.GREEN} [PASS] {Style.RESET_ALL} ({cost_time:.1f}ms)")
                passed += 1
                results_by_type[case_type]["pass"] += 1
            else:
                print(f"{Fore.RED} [FAIL] {Style.RESET_ALL} ({cost_time:.1f}ms)")
                print(f"    ❌ Expected: {expected}")
                print(f"    🔍 Actual:   {retrieved_names[:5]}...")  # 只打印前5个

        except Exception as e:
            print(f"{Fore.RED} [EXCEPTION] {e}")

    # 打印报告
    accuracy = (passed / total) * 100 if total > 0 else 0
    print("\n" + "=" * 60)
    print(f"{Fore.YELLOW}🏆 测试报告 (Benchmark Report)")
    print("=" * 60)
    print(f"Overall Acc:  {Fore.GREEN}{accuracy:.2f}% ({passed}/{total})")
    print("-" * 60)
    for c_type, stats in results_by_type.items():
        if stats["total"] > 0:
            type_acc = (stats["pass"] / stats["total"]) * 100
            print(f"  - {c_type:<10}: {type_acc:.1f}% ({stats['pass']}/{stats['total']})")
    print("=" * 60)


if __name__ == "__main__":
    run_benchmark()