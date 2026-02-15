"""
═══════════════════════════════════════════════════════════════════════════════
📦 模块名称: match_helper.py
📝 模块功能: Text-to-SQL 系统的关键词匹配助手
🎯 核心目标: 解决 "用户说的词" 和 "数据库存的值" 不一致的问题
═══════════════════════════════════════════════════════════════════════════════
"""

import re
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass


@dataclass
class MatchCandidate:
    """匹配候选对象 - 存储一次匹配的完整信息"""
    keyword: str       # 用户输入的关键词
    db_val: str        # 数据库实际值
    table: str         # 表名
    column: str        # 列名
    score: float = 0.0 # 匹配分数
    strength: str = "soft"  # hard(强约束) / soft(弱提示)
    reason: str = ""   # 匹配类型说明

    def to_simple_dict(self) -> Dict[str, str]:
        """生成 LLM 友好的 JSON 格式"""
        return {
            "user_input": self.keyword,
            "db_value": self.db_val,
            "table": self.table,
            "column": self.column
        }


class MatchHelper:
    """关键词匹配助手 - 负责关键词提取、匹配、打分、筛选"""

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
        """
        phrases = set()

        # 🔥 核心：只从 values 里提取
        if "values" in search_keywords:
            for value_group in search_keywords["values"]:
                for term in value_group.get("terms", []):
                    term = term.strip()
                    if len(term) >= 2:  # 过滤太短的词
                        phrases.add(term)

        return list(phrases)

    @staticmethod
    def calculate_match_score(keyword: str, db_value: str) -> Tuple[float, str]:
        """
        🧮 计算两个字符串的匹配分数（处理大小写、模糊匹配）

        返回: (分数, 原因)
        """
        kw_lower = keyword.lower()
        db_lower = db_value.lower()

        # 1️⃣ 精确匹配
        if kw_lower == db_lower:
            return (100.0, "exact_match")

        # 2️⃣ 子串包含
        if kw_lower in db_lower:
            ratio = len(kw_lower) / len(db_lower)
            score = 85.0 + (ratio * 10)
            return (score, "substring_match")

        # 3️⃣ 反向包含
        if db_lower in kw_lower:
            ratio = len(db_lower) / len(kw_lower)
            score = 80.0 + (ratio * 10)
            return (score, "reverse_substring")

        # 4️⃣ 模糊匹配（编辑距离）
        distance = MatchHelper._levenshtein_distance(kw_lower, db_lower)
        max_len = max(len(kw_lower), len(db_lower))
        similarity = (1 - distance / max_len) * 100

        if similarity >= 70:
            return (similarity, "fuzzy_match")

        return (0.0, "no_match")

    @staticmethod
    def _levenshtein_distance(s1: str, s2: str) -> int:
        """计算编辑距离"""
        if len(s1) < len(s2):
            return MatchHelper._levenshtein_distance(s2, s1)

        if len(s2) == 0:
            return len(s1)

        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row

        return previous_row[-1]

    @staticmethod
    def select_best_matches(
        matches: List[MatchCandidate],
        min_score: float = 70.0
    ) -> List[MatchCandidate]:
        """从多个匹配中筛选出最佳结果"""
        if not matches:
            return []

        # 按关键词分组
        grouped = {}
        for m in matches:
            grouped.setdefault(m.keyword, []).append(m)

        # 每组取 Top 1
        final_list = []
        for kw, group in grouped.items():
            group.sort(key=lambda x: x.score, reverse=True)
            top = group[0]
            if top.score >= min_score:
                final_list.append(top)

        # 去重：同一个 table.column 只保留最高分
        unique_map = {}
        for m in sorted(final_list, key=lambda x: x.score, reverse=True):
            key = f"{m.table}.{m.column}"
            if key not in unique_map:
                unique_map[key] = m

        return list(unique_map.values())

    @staticmethod
    def format_matches_to_json(matches: List[MatchCandidate]) -> Dict[str, Any]:
        """将匹配结果转换为 LLM 友好的 JSON 格式"""
        return {
            "value_mappings": [m.to_simple_dict() for m in matches]
        }

    @staticmethod
    def recursive_extract_hits(obj: Any) -> List[Any]:
        """递归提取 Milvus 返回结果中的 entity 对象"""
        extracted = []
        if isinstance(obj, list) or (hasattr(obj, '__iter__') and not isinstance(obj, (dict, str))):
            for item in obj:
                extracted.extend(MatchHelper.recursive_extract_hits(item))
        elif hasattr(obj, 'entity') or (isinstance(obj, dict) and 'entity' in obj):
            extracted.append(obj)
        return extracted


# 全局实例
match_helper = MatchHelper()
