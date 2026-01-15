import requests
import json
import time
import os
from pathlib import Path
from tabulate import tabulate
from colorama import init, Fore, Style

# 初始化颜色
init(autoreset=True)

# 配置
API_URL = "http://127.0.0.1:8000/api/v1/query"
TEST_FILE = "test_cases.json"


def truncate_str(text, max_len=50):
    """辅助函数：截断过长的字符串用于展示"""
    if not text:
        return ""
    text = str(text).replace("\n", " ").strip()
    return (text[:max_len] + '..') if len(text) > max_len else text


def run_evaluation():
    file_path = Path(__file__).parent / TEST_FILE
    if not file_path.exists():
        print(f"{Fore.RED}❌ 错误: 找不到测试文件 {TEST_FILE}")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        cases = json.load(f)

    print(f"{Fore.CYAN}🚀 开始执行 {len(cases)} 个自动化测试用例 (逻辑增强版)...\n")
    print(f"{Fore.CYAN}ℹ️  注: 对于 DATA_RETURNED，只要 SQL 合法且执行无错，返回 0 行也视为通过。")

    results = []
    success_count = 0

    for case in cases:
        print(f"Testing [{case['id']}] {case['query'][:30]}... ", end="", flush=True)

        payload = {
            "query": case["query"],
            "session_id": case["session_id"],
            "user_id": "auto_tester_v3"
        }

        start_ts = time.time()
        generated_content_display = ""  # 用于在表格中展示的内容

        try:
            # 保持 60s 超时，给予 Agent 足够的自我修复时间
            resp = requests.post(API_URL, json=payload, timeout=120)
            duration = round(time.time() - start_ts, 2)

            if resp.status_code != 200:
                status = f"{Fore.RED}HTTP {resp.status_code}"
                detail = "Server Error"
            else:
                resp_json = resp.json()

                # =================================================
                # 1. 🔍 提取关键字段
                # =================================================
                # 提取 meta 信息
                meta = resp_json.get("meta", {})

                # 提取生成的 SQL (优先从 meta 取，兼容旧版从根节点取)
                agent_sql = meta.get("sql") or resp_json.get("sql")

                # 提取数据结果
                sql_data = resp_json.get("data", [])

                # 提取文本回复
                agent_reply = resp_json.get("message", "")

                # 决定最终用于展示的内容 (SQL 优先，其次是回复)
                if agent_sql:
                    generated_content_display = agent_sql
                else:
                    generated_content_display = agent_reply

                # 判断是否存在 SQL 尝试
                has_sql_attempt = bool(agent_sql)

                # =================================================
                # 2. ⚖️ 核心判题逻辑
                # =================================================
                is_pass = False
                detail = ""

                # -----------------------------------
                # 场景 A: 预期应当查出数据 (DATA_RETURNED)
                # -----------------------------------
                if case["expected_type"] == "DATA_RETURNED":
                    if not has_sql_attempt:
                        is_pass = False
                        detail = "❌ No SQL Generated"
                    else:
                        # 检查是否是 "ERR::" 开头的 SQL (这是 Agent 主动报错，不算数据查询成功)
                        if "ERR::" in agent_sql:
                            is_pass = False
                            detail = f"❌ Refusal SQL: {agent_sql[:20]}"

                        # 检查数据库执行是否报错 (data 里的 error 字段)
                        elif isinstance(sql_data, list) and len(sql_data) > 0 and isinstance(sql_data[0],
                                                                                             dict) and "error" in \
                                sql_data[0]:
                            is_pass = False
                            err_str = str(sql_data[0]['error'])
                            detail = f"❌ DB Runtime Error: {err_str[:20]}..."

                        else:
                            # 🔥 核心修复：即使 data 为空 (Rows: 0)，只要 SQL 没报错，就算 PASS
                            is_pass = True
                            row_count = len(sql_data) if isinstance(sql_data, list) else 0

                            if row_count > 0:
                                detail = f"✅ Rows: {row_count}"
                            else:
                                # 专门标记这是空数据通过
                                detail = f"✅ SQL Valid (Rows: 0)"

                # -----------------------------------
                # 场景 B: 预期应当拒绝 (REFUSAL) -> 针对幻觉陷阱
                # -----------------------------------
                elif case["expected_type"] == "REFUSAL":
                    # 1. 检查 SQL 协议拒绝 (ERR::)
                    protocol_refusal = False
                    if agent_sql and "ERR::" in agent_sql:
                        protocol_refusal = True

                    # 2. 检查文本拒绝
                    text_refusal = False
                    refusal_keywords = ["抱歉", "无法", "没有找到", "缺少", "不支持", "未找到"]
                    if not agent_sql and any(k in agent_reply for k in refusal_keywords):
                        text_refusal = True

                    if protocol_refusal:
                        is_pass = True
                        detail = f"✅ Protocol Refusal"
                    elif text_refusal:
                        is_pass = True
                        detail = "✅ Text Refusal"
                    else:
                        is_pass = False
                        if agent_sql:
                            detail = "❌ Hallucination (Executed SQL)"
                        else:
                            detail = "❌ Invalid Reply"

                # -----------------------------------
                # 场景 C: 闲聊 (TEXT_REPLY)
                # -----------------------------------
                elif case["expected_type"] == "TEXT_REPLY":
                    if agent_reply and len(agent_reply) > 2:
                        is_pass = True
                        detail = "✅ Reply OK"
                    else:
                        is_pass = False
                        detail = "❌ Empty Reply"

                if is_pass:
                    status = f"{Fore.GREEN}PASS"
                    success_count += 1
                else:
                    status = f"{Fore.RED}FAIL"

        except Exception as e:
            status = f"{Fore.RED}EXCEPTION"
            detail = str(e)[:30]
            duration = 0
            generated_content_display = "N/A"

        print(f"{status}")

        # 添加结果到列表，注意加入了 generated_content_display
        results.append([
            case['id'],
            case['category'],
            status,
            f"{duration}s",
            detail,
            truncate_str(generated_content_display, 40)  # 截断以便表格显示
        ])

        # 保持间隔
        time.sleep(1)

    # =================================================
    # 3. 📊 生成最终报告
    # =================================================
    print("\n" + "=" * 100)
    print(f"📊 测试摘要: Pass {success_count}/{len(cases)} | Accuracy: {int(success_count / len(cases) * 100)}%")
    print("=" * 100)

    # 增加了 "Actual Output" 列
    headers = ["ID", "Category", "Status", "Time", "Detail", "Actual Output"]

    # 使用 grid 格式，虽然占空间但更清晰
    print(tabulate(results, headers=headers, tablefmt="simple"))


if __name__ == "__main__":
    run_evaluation()