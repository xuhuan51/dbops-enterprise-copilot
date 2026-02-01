import json
import os
from collections import defaultdict


def build_business_rule_base():
    INPUT_FILE = '../data/bird/questions/dev.json'
    OUTPUT_FILE = '../data/bird/metadata/business_rules.json'  # 这是你的新“真理之书”

    if not os.path.exists(INPUT_FILE):
        print(f"❌ 找不到文件: {INPUT_FILE}")
        return

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)

    # 1. 使用字典按 db_id 分组，使用 set 自动去重
    rule_registry = defaultdict(set)

    print("🧹 正在从 Evidence 中萃取业务规则...")

    for item in raw_data:
        db_id = item['db_id']
        evidence = item.get('evidence', '').strip()

        # 过滤无效数据
        if not evidence or evidence.lower() == 'null':
            continue

        # 核心逻辑：这里只存规则，不存问题！
        # 比如：只存 "High value customers are those with balance > 5000"
        rule_registry[db_id].add(evidence)

    # 2. 转换为列表以便 JSON 序列化
    # 这一步我们做一个小的优化：把规则变成对象，方便后续扩展
    structured_rules = []

    for db_id, rules in rule_registry.items():
        # 将该数据库下的所有去重规则整理出来
        unique_rules = sorted(list(rules))

        for rule in unique_rules:
            structured_rules.append({
                "db_id": db_id,
                "rule_text": rule,
                # 生成一个用于向量检索的纯净文本
                "doc_text": f"Business Rule for {db_id}: {rule}"
            })

    # 3. 保存
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(structured_rules, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 指标定义库构建完成！")
    print(f"📍 输出路径: {OUTPUT_FILE}")
    print(f"📊 提取出的独立规则总数: {len(structured_rules)}")
    print(f"   (原数据有 {len(raw_data)} 条，去重效果显著)")


if __name__ == "__main__":
    build_business_rule_base()