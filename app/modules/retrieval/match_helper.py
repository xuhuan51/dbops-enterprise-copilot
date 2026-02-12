import re
from typing import List, Dict, Any, Set
from dataclasses import dataclass

# =============================================================================
# 🛑 Stop Words (停用词表)
# 用于过滤掉 sql 中的关键字或无意义的高频词，防止它们被当做 value 去扫描数据库
# =============================================================================
CONCEPT_STOP_WORDS = {
    "public", "private", "alternative", "elementary", "middle", "high",
    "free", "eligible", "reduced", "rate", "percent", "percentage",
    "average", "total", "count", "sum", "max", "min",
    "highest", "lowest", "top", "bottom"
}

SCHEMA_STOP_WORDS = {
    "what", "which", "where", "show", "list", "please", "many", "much",
    "ordered", "group", "order", "by", "asc", "desc", "limit", "of",
    "in", "for", "and", "or", "the", "a", "an", "to"
}

# 移除了 BIRD 特定的 K-12 正则，保留通用的数字范围正则 (如 "10-20")
RE_AGE_RANGE = re.compile(r"^\s*\d+\s*-\s*\d+\s*$")


@dataclass
class MatchCandidate:
    """
    扫描到的候选匹配对象
    """
    keyword: str  # 用户问题里的词 (如 "iPhone")
    db_val: str  # 数据库里的真实值 (如 "iPhone 15 Pro")
    table: str  # 所在的表
    column: str  # 所在的列
    score: float = 0.0  # 匹配分数 (0-100)
    strength: str = "soft"  # hard (强匹配) / soft (弱匹配)
    reason: str = ""  # 匹配原因 (exact / fuzzy)

    def format_constraint(self) -> str:
        """格式化为自然语言提示，给 LLM 看"""
        prefix = "🔴 CONSTRAINT" if self.strength == "hard" else "🟡 HINT"
        return (
            f"{prefix}: Entity '{self.keyword}' -> matches value '{self.db_val}' "
            f"in `{self.table}`.`{self.column}`"
        )


class MatchHelper:
    """
    [关键词匹配助手]
    负责：
    1. 清洗和准备需要去数据库扫描的关键词 (Scan Phrases)。
    2. 对扫描结果进行打分和筛选 (Select Best Matches)。
    3. 辅助提取 Milvus 返回的嵌套对象。
    """

    @staticmethod
    def prepare_scan_phrases(value_kw: List[str], all_kw: List[str], hints: Any) -> List[str]:
        """
        准备扫描词 (Scan Phrases)。
        策略：严格隔离，只扫描被 LLM 明确标记为 VALUE 的词，防止全库扫描爆炸。
        """
        phrases = set()

        # 1. 核心来源：只信任 value_kw (由 LLM 标记为 VALUE 的词)
        for v in value_kw:
            s = v.strip()
            # 忽略太短的词 (防止把 'A', '1' 这种当做值去全库扫)
            if len(s) >= 2:
                # 再次防御：如果是纯数字且太短，也不扫 (除非是 ID，这需要结合 hint，这里先保守)
                if s.isdigit() and len(s) < 4:
                    continue
                phrases.add(s)

        # 2. 辅助来源：Filter Hints (慎用)
        # 只有当 hints 里不包含比较级操作符时，才认为是实体 (Entity)
        # 例如: "age > 18" -> 丢弃; "city = Beijing" -> 提取 "Beijing"
        if hints and hints.filter_hints:
            for h in hints.filter_hints:
                if not h: continue

                # 过滤掉数字范围 (10-20)
                if RE_AGE_RANGE.match(h): continue

                # 🔥 关键防御：如果包含 > < = over under，说明是逻辑条件，不是值！
                if any(op in h for op in [">", "<", "=", "over", "under", "above", "below"]):
                    continue

                phrases.add(h.strip())

        return list(phrases)

    @staticmethod
    def select_best_matches(matches: List[MatchCandidate], question: str, hints: Any) -> List[MatchCandidate]:
        """
        从扫描结果中筛选出最佳匹配 (Best Matches)。
        策略：
        1. 分组：按关键词分组。
        2. 排序：组内按分数排序。
        3. 阈值：只保留高分结果。
        """
        if not matches: return []

        # 简单的上下文感知 (可选优化)
        # 比如问题里问了 "province"，那么 province 列的匹配分数应该加成
        ql = question.lower()

        grouped = {}
        for m in matches:
            grouped.setdefault(m.keyword, []).append(m)

        final_list = []
        for kw, group in grouped.items():
            # 组内排序
            group.sort(key=lambda x: x.score, reverse=True)

            # 取 Top 1，且分数要及格
            top_match = group[0]
            if top_match.score >= 70:
                final_list.append(top_match)

        # 去重：同一个 table.column 不需要重复提示
        unique_map = {}
        for m in sorted(final_list, key=lambda x: x.score, reverse=True):
            k = f"{m.table}.{m.column}"
            if k not in unique_map:
                unique_map[k] = m

        return list(unique_map.values())

    @staticmethod
    def recursive_extract_hits(obj: Any) -> List[Any]:
        """
        递归提取 Milvus 返回结果中的 entity 对象。
        因为 Milvus 的返回结构有时是 list of list，有时是 list of hits。
        """
        extracted = []
        if isinstance(obj, list) or (hasattr(obj, '__iter__') and not isinstance(obj, (dict, str))):
            for item in obj:
                extracted.extend(MatchHelper.recursive_extract_hits(item))
        elif hasattr(obj, 'entity') or (isinstance(obj, dict) and 'entity' in obj):
            extracted.append(obj)
        return extracted


match_helper = MatchHelper()