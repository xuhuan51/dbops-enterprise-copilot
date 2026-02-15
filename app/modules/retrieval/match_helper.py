"""
═══════════════════════════════════════════════════════════════════════════════
📦 模块名称: match_helper.py
📝 模块功能: Text-to-SQL 系统的关键词匹配助手
🎯 核心目标: 解决 "用户说的词" 和 "数据库存的值" 不一致的问题

═══════════════════════════════════════════════════════════════════════════════
🔍 问题场景举例:

  用户问: "北京已发货的订单有哪些?"
  数据库: region 列存的是 "北京市朝阳区"
           status 列存的是 "SHIPPED"

  ❌ 直接生成 SQL: WHERE region = '北京' AND status = '已发货'  → 查不到！
  ✅ 使用本模块:   WHERE region LIKE '%北京%' AND status = 'SHIPPED' → 正确！

═══════════════════════════════════════════════════════════════════════════════
🛠️ 核心流程:

  ┌──────────────┐
  │ 1. 提取关键词 │ → 从 JSON 的 values.terms 里拿到 ["北京", "已发货"]
  └──────┬───────┘
         ↓
  ┌──────────────┐
  │ 2. 扫描数据库 │ → 在各个表的文本列里搜索，找到 ["北京市", "SHIPPED"]
  └──────┬───────┘
         ↓
  ┌──────────────┐
  │ 3. 打分匹配   │ → 计算相似度: "北京" vs "北京市" = 95分
  └──────┬───────┘
         ↓
  ┌──────────────┐
  │ 4. 筛选最优   │ → 只保留高分匹配（>70分），去重
  └──────┬───────┘
         ↓
  ┌──────────────┐
  │ 5. 生成 JSON  │ → 输出简洁的映射关系给 LLM
  └──────────────┘

═══════════════════════════════════════════════════════════════════════════════
"""

import re
from typing import List, Dict, Any, Set, Tuple
from dataclasses import dataclass, asdict


# ═════════════════════════════════════════════════════════════════════════════
# 📊 数据结构定义
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class MatchCandidate:
    """
    💡 匹配候选对象 - 存储一次匹配的完整信息

    【作用】
    记录 "用户说的词" 和 "数据库真实值" 之间的匹配关系

    【字段说明】
    - keyword:  用户问题里的原始词 (如 "北京")
    - db_val:   数据库里实际存储的值 (如 "北京市朝阳区")
    - table:    所在表名 (如 "orders")
    - column:   所在列名 (如 "region")
    - score:    匹配分数 0-100 (越高越相似)
    - strength: 匹配强度 "hard"(强制约束) 或 "soft"(提示)
    - reason:   匹配原因 (exact_match/substring_match/fuzzy_match)

    【示例】
    MatchCandidate(
        keyword="北京",
        db_val="北京市朝阳区",
        table="orders",
        column="region",
        score=95.0,
        strength="hard",
        reason="substring_match"
    )
    """
    keyword: str  # 用户输入的关键词
    db_val: str  # 数据库实际值
    table: str  # 表名
    column: str  # 列名
    score: float = 0.0  # 匹配分数
    strength: str = "soft"  # hard(强约束) / soft(弱提示)
    reason: str = ""  # 匹配类型说明

    def to_simple_dict(self) -> Dict[str, str]:
        """
        🎨 生成简洁的 JSON 格式，给 LLM 看

        【返回格式】
        {
            "user_input": "北京",
            "db_value": "北京市朝阳区",
            "table": "orders",
            "column": "region"
        }

        【为什么这样设计？】
        - 只保留 LLM 需要的核心信息（4个字段）
        - 去掉内部评分细节（score, reason, strength）
        - 键名用英文，便于 LLM 理解
        - 格式清晰，易于解析

        Returns:
            简化后的字典对象
        """
        return {
            "user_input": self.keyword,
            "db_value": self.db_val,
            "table": self.table,
            "column": self.column
        }


# ═════════════════════════════════════════════════════════════════════════════
# 🧰 核心工具类
# ═════════════════════════════════════════════════════════════════════════════

class MatchHelper:
    """
    🔧 关键词匹配助手 - 负责关键词提取、匹配、打分、筛选

    【核心方法】
    1. prepare_scan_phrases()     - 从 JSON 提取需要扫描的词
    2. calculate_match_score()    - 计算两个词的相似度分数
    3. select_best_matches()      - 从多个匹配中筛选最佳结果
    4. enhance_with_value_groups() - 利用分组信息增强匹配
    5. format_matches_to_json()   - 转换为 LLM 友好的 JSON 格式
    """

    # ─────────────────────────────────────────────────────────────────────────
    # 方法1: 提取扫描词
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def prepare_scan_phrases(search_keywords: Dict[str, Any]) -> List[str]:
        """
        🎯 从新版 JSON 结构中提取需要去数据库扫描的关键词

        【输入示例】
        {
          "values": [
            {"group": "城市值", "terms": ["北京", "Beijing"]},
            {"group": "状态值", "terms": ["已发货", "shipped"]}
          ]
        }

        【输出】
        ["北京", "Beijing", "已发货", "shipped"]

        【为什么只从 values 提取？】
        - concepts 是表名/列名，不需要扫数据
        - values 才是用户要查询的具体值

        【过滤规则】
        - 长度 < 2 的词会被过滤 (防止 "A", "1" 这种噪音)

        Args:
            search_keywords: 包含 concepts 和 values 的字典

        Returns:
            去重后的扫描词列表
        """
        phrases = set()  # 用 set 自动去重

        # 🔥 核心：只从 values 里提取（这是用户要查的具体值）
        if "values" in search_keywords:
            for value_group in search_keywords["values"]:
                # 遍历每个分组的 terms
                for term in value_group.get("terms", []):
                    term = term.strip()  # 去除首尾空格

                    # 过滤太短的词（防止 "A", "1" 被当作值）
                    if len(term) >= 2:
                        phrases.add(term)

        return list(phrases)

    # ─────────────────────────────────────────────────────────────────────────
    # 方法2: 计算匹配分数（核心算法）
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def calculate_match_score(keyword: str, db_value: str) -> Tuple[float, str]:
        """
        🧮 计算两个字符串的匹配分数（处理大小写、模糊匹配）

        【匹配策略（按优先级）】

        1️⃣ 精确匹配 (100分)
           - "iphone" vs "iPhone"  → 忽略大小写后完全相同
           - "北京" vs "北京"      → 完全一致

        2️⃣ 子串包含 (85-95分)
           - "iphone" 包含在 "iPhone 15 Pro" 里
           - 分数 = 85 + (关键词长度/数据库值长度) * 10
           - 越接近说明匹配越精确

        3️⃣ 反向包含 (80-90分)
           - "iPhone 15" 被包含在用户输入的 "iPhone" 里
           - 用于处理用户输入更长的情况

        4️⃣ 模糊匹配 (70-85分)
           - 使用编辑距离算法 (Levenshtein Distance)
           - "Bejing" vs "Beijing" → 只差1个字符 → 高分
           - "iPhone" vs "Samsung" → 差很多字符 → 低分

        【返回】
        (分数, 原因)
        - (100.0, "exact_match")
        - (92.5, "substring_match")
        - (75.3, "fuzzy_match")
        - (0.0, "no_match")

        Args:
            keyword: 用户输入的词
            db_value: 数据库里的值

        Returns:
            (score, reason) - 分数和匹配原因
        """
        # 统一转小写，忽略大小写差异
        kw_lower = keyword.lower()
        db_lower = db_value.lower()

        # ─────────────────────────────────────────────────────────────────────
        # 1️⃣ 精确匹配（忽略大小写）
        # ─────────────────────────────────────────────────────────────────────
        if kw_lower == db_lower:
            return (100.0, "exact_match")

        # ─────────────────────────────────────────────────────────────────────
        # 2️⃣ 子串包含（关键词是数据库值的一部分）
        # ─────────────────────────────────────────────────────────────────────
        # 例如: "iphone" in "iphone 15 pro"
        if kw_lower in db_lower:
            # 计算长度比例，越接近说明匹配越好
            ratio = len(kw_lower) / len(db_lower)
            score = 85.0 + (ratio * 10)  # 分数范围: 85-95
            return (score, "substring_match")

        # ─────────────────────────────────────────────────────────────────────
        # 3️⃣ 反向包含（数据库值是关键词的一部分）
        # ─────────────────────────────────────────────────────────────────────
        # 例如: "iphone" in "iphone 15"
        if db_lower in kw_lower:
            ratio = len(db_lower) / len(kw_lower)
            score = 80.0 + (ratio * 10)  # 分数范围: 80-90
            return (score, "reverse_substring")

        # ─────────────────────────────────────────────────────────────────────
        # 4️⃣ 模糊匹配（基于编辑距离）
        # ─────────────────────────────────────────────────────────────────────
        # 计算需要多少次编辑（增删改）才能从一个词变成另一个词
        distance = MatchHelper._levenshtein_distance(kw_lower, db_lower)
        max_len = max(len(kw_lower), len(db_lower))

        # 相似度 = 1 - (编辑距离 / 最大长度)
        # 例如: "Bejing" vs "Beijing", 距离=1, 长度=7
        #       相似度 = (1 - 1/7) * 100 = 85.7%
        similarity = (1 - distance / max_len) * 100

        # 只接受相似度 >= 70% 的匹配
        if similarity >= 70:
            return (similarity, "fuzzy_match")

        # 完全不匹配
        return (0.0, "no_match")

    # ─────────────────────────────────────────────────────────────────────────
    # 辅助方法: 计算编辑距离（Levenshtein Distance）
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _levenshtein_distance(s1: str, s2: str) -> int:
        """
        📏 计算编辑距离 - 衡量两个字符串的相似度

        【算法原理】
        计算把 s1 变成 s2 需要的最少操作次数（增删改）

        【示例】
        - "cat" -> "hat": 1次操作（替换 c->h）
        - "Saturday" -> "Sunday": 3次操作

        【应用场景】
        - 纠正拼写错误: "Bejing" -> "Beijing"
        - 处理简写: "北京" -> "北京市"

        【算法复杂度】
        时间: O(m*n), 空间: O(n)
        m, n 分别是两个字符串的长度

        Args:
            s1: 字符串1
            s2: 字符串2

        Returns:
            编辑距离（整数）
        """
        # 优化：确保 s1 是较长的那个（减少空间复杂度）
        if len(s1) < len(s2):
            return MatchHelper._levenshtein_distance(s2, s1)

        # 边界情况：如果 s2 是空字符串，距离就是 s1 的长度
        if len(s2) == 0:
            return len(s1)

        # 动态规划算法
        # previous_row 存储上一行的计算结果
        previous_row = range(len(s2) + 1)

        for i, c1 in enumerate(s1):
            current_row = [i + 1]  # 当前行的第一个值

            for j, c2 in enumerate(s2):
                # 三种操作的代价：
                insertions = previous_row[j + 1] + 1  # 插入
                deletions = current_row[j] + 1  # 删除
                substitutions = previous_row[j] + (c1 != c2)  # 替换（相同则0）

                # 选择代价最小的操作
                current_row.append(min(insertions, deletions, substitutions))

            previous_row = current_row

        return previous_row[-1]

    # ─────────────────────────────────────────────────────────────────────────
    # 方法3: 筛选最佳匹配
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def select_best_matches(
            matches: List[MatchCandidate],
            min_score: float = 70.0
    ) -> List[MatchCandidate]:
        """
        🎯 从多个匹配中筛选出最佳结果

        【筛选策略】

        1. 按关键词分组
           - "北京" 可能匹配到多个列: orders.region, users.city
           - 每个关键词只保留最高分的那个

        2. 分数过滤
           - 只保留 score >= min_score 的匹配
           - 默认阈值 70 分（可调）

        3. 去重
           - 同一个 table.column 不重复提示
           - 例如: orders.region 已经匹配了"北京"，就不再匹配"Beijing"

        【示例】
        输入 10 个匹配 →
          - 按关键词分组成 3 组
          - 每组取 Top 1
          - 去重后剩 2 个
        → 输出 2 个最优匹配

        Args:
            matches: 所有候选匹配列表
            min_score: 最低分数阈值（默认70分）

        Returns:
            筛选后的最佳匹配列表
        """
        if not matches:
            return []

        # ─────────────────────────────────────────────────────────────────────
        # 步骤1: 按关键词分组
        # ─────────────────────────────────────────────────────────────────────
        # 结构: {"北京": [match1, match2], "已发货": [match3]}
        grouped = {}
        for m in matches:
            grouped.setdefault(m.keyword, []).append(m)

        # ─────────────────────────────────────────────────────────────────────
        # 步骤2: 每组取 Top 1（分数最高的）
        # ─────────────────────────────────────────────────────────────────────
        final_list = []
        for kw, group in grouped.items():
            # 组内按分数降序排序
            group.sort(key=lambda x: x.score, reverse=True)

            # 取第一名，且分数要及格
            top = group[0]
            if top.score >= min_score:
                final_list.append(top)

        # ─────────────────────────────────────────────────────────────────────
        # 步骤3: 去重（同一个 table.column 只保留最高分）
        # ─────────────────────────────────────────────────────────────────────
        unique_map = {}
        # 先按分数排序，确保高分的先被加入 map
        for m in sorted(final_list, key=lambda x: x.score, reverse=True):
            key = f"{m.table}.{m.column}"
            if key not in unique_map:
                unique_map[key] = m

        return list(unique_map.values())

    # ─────────────────────────────────────────────────────────────────────────
    # 方法4: 增强匹配（可选）
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def enhance_with_value_groups(
            matches: List[MatchCandidate],
            search_keywords: Dict[str, Any]
    ) -> List[MatchCandidate]:
        """
        🌟 利用 value groups 信息增强匹配结果

        【作用】
        给匹配结果添加语义标签，帮助 LLM 理解上下文

        【示例】
        输入匹配:
          keyword="北京", reason="exact_match"

        增强后:
          reason="exact_match [城市值]"  ← 多了个分组标签

        【为什么有用？】
        当同一个词在多个上下文中出现时，标签能帮助消歧:
        - "上海" 可能是城市，也可能是公司名
        - 有了 [城市值] 标签，LLM 就知道去 region 列找

        Args:
            matches: 匹配结果列表
            search_keywords: 包含分组信息的 JSON

        Returns:
            增强后的匹配列表（原地修改）
        """
        if "values" not in search_keywords:
            return matches

        # ─────────────────────────────────────────────────────────────────────
        # 构建词 -> 分组的映射表
        # ─────────────────────────────────────────────────────────────────────
        # 结构: {"北京": "城市值", "已发货": "状态值"}
        term_to_group = {}
        for value_group in search_keywords["values"]:
            group_name = value_group.get("group", "unknown")
            for term in value_group.get("terms", []):
                term_to_group[term.lower()] = group_name

        # ─────────────────────────────────────────────────────────────────────
        # 为每个匹配添加分组信息
        # ─────────────────────────────────────────────────────────────────────
        for match in matches:
            kw_lower = match.keyword.lower()
            if kw_lower in term_to_group:
                # 在原因后面追加分组标签
                match.reason += f" [{term_to_group[kw_lower]}]"

        return matches

    # ─────────────────────────────────────────────────────────────────────────
    # 方法5: 格式化为 JSON（新增）
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def format_matches_to_json(matches: List[MatchCandidate]) -> Dict[str, Any]:
        """
        📤 将匹配结果转换为 LLM 友好的 JSON 格式

        【输出格式】
        {
            "value_mappings": [
                {
                    "user_input": "北京",
                    "db_value": "北京市朝阳区",
                    "table": "orders",
                    "column": "region"
                },
                {
                    "user_input": "已发货",
                    "db_value": "SHIPPED",
                    "table": "orders",
                    "column": "status"
                }
            ]
        }

        【设计理念】
        1. 扁平结构 - 不需要嵌套，直接数组
        2. 语义清晰 - user_input vs db_value 一目了然
        3. 完整信息 - 包含表名和列名，方便 LLM 定位
        4. 去除噪音 - 不包含内部评分、原因等调试信息

        【LLM 使用方式】
        在 prompt 里这样写:
        "根据以下值映射关系生成 SQL:
         {json_output}

         例如: 用户说'北京'，实际应该查 orders.region = '北京市朝阳区'"

        Args:
            matches: 筛选后的最佳匹配列表

        Returns:
            包含 value_mappings 的 JSON 对象
        """
        return {
            "value_mappings": [m.to_simple_dict() for m in matches]
        }


# ═════════════════════════════════════════════════════════════════════════════
# 🚀 全局实例（单例模式）
# ═════════════════════════════════════════════════════════════════════════════

# 创建全局实例，方便直接调用
match_helper = MatchHelper()

# ═════════════════════════════════════════════════════════════════════════════
# 📖 使用示例
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    完整的使用流程演示
    """
    import json

    # ─────────────────────────────────────────────────────────────────────────
    # 步骤1: 准备输入数据（从你的 JSON 解析器获取）
    # ─────────────────────────────────────────────────────────────────────────
    search_keywords = {
        "values": [
            {"group": "城市值", "terms": ["北京", "Beijing"]},
            {"group": "状态值", "terms": ["已发货", "shipped"]}
        ]
    }

    # ─────────────────────────────────────────────────────────────────────────
    # 步骤2: 提取扫描词
    # ─────────────────────────────────────────────────────────────────────────
    scan_phrases = match_helper.prepare_scan_phrases(search_keywords)
    print(f"📝 需要扫描的词: {scan_phrases}")
    # 输出: ["北京", "Beijing", "已发货", "shipped"]

    # ─────────────────────────────────────────────────────────────────────────
    # 步骤3: 模拟数据库扫描结果
    # ─────────────────────────────────────────────────────────────────────────
    # 实际使用时，这里应该是调用向量数据库或全文搜索
    raw_matches = [
        MatchCandidate("北京", "北京市朝阳区", "orders", "region"),
        MatchCandidate("北京", "Beijing City", "users", "city"),
        MatchCandidate("已发货", "SHIPPED", "orders", "status"),
        MatchCandidate("shipped", "DELIVERED", "orders", "status"),  # 噪音数据
    ]

    # ─────────────────────────────────────────────────────────────────────────
    # 步骤4: 计算匹配分数
    # ─────────────────────────────────────────────────────────────────────────
    for match in raw_matches:
        score, reason = match_helper.calculate_match_score(
            match.keyword,
            match.db_val
        )
        match.score = score
        match.reason = reason
        print(f"🔍 {match.keyword} vs {match.db_val}: {score:.1f}分 ({reason})")

    # ─────────────────────────────────────────────────────────────────────────
    # 步骤5: 筛选最佳匹配
    # ─────────────────────────────────────────────────────────────────────────
    best_matches = match_helper.select_best_matches(raw_matches, min_score=75)

    # ─────────────────────────────────────────────────────────────────────────
    # 步骤6: 生成 JSON 输出（传给 LLM）
    # ─────────────────────────────────────────────────────────────────────────
    output_json = match_helper.format_matches_to_json(best_matches)

    print("\n" + "=" * 80)
    print("🎯 最终输出（传给 LLM 的 JSON）:")
    print("=" * 80)
    print(json.dumps(output_json, ensure_ascii=False, indent=2))

    """
    预期输出:
    ================================================================================
    🎯 最终输出（传给 LLM 的 JSON）:
    ================================================================================
    {
      "value_mappings": [
        {
          "user_input": "北京",
          "db_value": "北京市朝阳区",
          "table": "orders",
          "column": "region"
        },
        {
          "user_input": "已发货",
          "db_value": "SHIPPED",
          "table": "orders",
          "column": "status"
        }
      ]
    }
    """