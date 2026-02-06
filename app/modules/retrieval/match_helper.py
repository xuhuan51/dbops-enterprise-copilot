import re
from typing import List, Dict, Any
from dataclasses import dataclass

# 定义在外部的常量，方便管理
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

RE_GRADE_HINT = re.compile(r"^\s*k\s*-\s*12\s*$", re.IGNORECASE)
RE_AGE_RANGE = re.compile(r"^\s*\d+\s*-\s*\d+\s*$")


class MatchHelper:
    """负责关键词处理、匹配筛选和结果提取"""

    @staticmethod
    def prepare_scan_phrases(value_kw: List[str], all_kw: List[str], hints: Any) -> List[str]:
        """
        修正版：严格隔离 CONCEPT。
        只扫描被标记为 VALUE 的词。
        ❌ 删除了所有针对 all_kw (CONCEPT) 的 Fallback 逻辑。
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
        # 只有当 hints 里不包含比较级操作符时，才认为是实体
        if hints and hints.filter_hints:
            for h in hints.filter_hints:
                if not h: continue
                # 过滤掉 "K-12", "Ages 5-17"
                if RE_GRADE_HINT.match(h) or RE_AGE_RANGE.match(h): continue

                # 🔥 关键防御：如果包含 > < = over under，说明是逻辑条件，不是值！
                if any(op in h for op in [">", "<", "=", "over", "under", "above", "below"]):
                    continue

                phrases.add(h.strip())

        # 3. ❌ 原来的 Fallback 逻辑被彻底删除了 ❌
        # 我们不再遍历 all_kw。
        # 因为 all_kw 里的 "SAT", "Count", "School" 是用来找 Schema 的，
        # 绝对不能进入这里的 phrases 列表！

        return list(phrases)

    @staticmethod
    def select_best_matches(matches: List[Any], question: str, hints: Any) -> List[Any]:
        if not matches: return []
        ql = question.lower()
        hint_str = " ".join((hints.filter_hints or []) + [hints.metric_hint or ""]).lower() if hints else ""
        ctx = f"{ql} {hint_str}"

        want_county = "county" in ctx
        want_district = "district" in ctx

        grouped = {}
        for m in matches:
            grouped.setdefault(m.keyword, []).append(m)

        final_list = []
        for kw, group in grouped.items():
            for m in group:
                if want_county and "county" in m.column.lower(): m.score += 20
                if want_district and "district" in m.column.lower(): m.score += 20
            group.sort(key=lambda x: x.score, reverse=True)
            if group[0].score >= 70:
                final_list.append(group[0])

        unique_map = {}
        for m in sorted(final_list, key=lambda x: x.score, reverse=True):
            k = f"{m.table}.{m.column}"
            if k not in unique_map: unique_map[k] = m
        return list(unique_map.values())

    @staticmethod
    def recursive_extract_hits(obj: Any) -> List[Any]:
        extracted = []
        if isinstance(obj, list) or (hasattr(obj, '__iter__') and not isinstance(obj, (dict, str))):
            for item in obj: extracted.extend(MatchHelper.recursive_extract_hits(item))
        elif hasattr(obj, 'entity') or (isinstance(obj, dict) and 'entity' in obj):
            extracted.append(obj)
        return extracted


match_helper = MatchHelper()