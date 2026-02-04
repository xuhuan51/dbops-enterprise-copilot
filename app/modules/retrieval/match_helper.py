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
        phrases = set()  # 用 set 直接去重

        # 1. Value Keywords (优先信任，但加一道防线)
        for v in value_kw:
            s = v.strip()
            if len(s) >= 2:
                # 防御：如果是纯数字，且长度小于 6 (比如年份 2023, 分数 1500)，大概率是逻辑值，不扫
                # 除非你确定你的 ID 都是短数字，否则这里建议保守
                if s.isdigit() and len(s) < 6:
                    continue
                phrases.add(s)

        # 2. Filter Hints (谨慎使用)
        if hints and hints.filter_hints:
            for h in hints.filter_hints:
                # 过滤掉明显的非实体 Hint (如 "score > 100", "K-12")
                if h and not RE_GRADE_HINT.match(h) and not RE_AGE_RANGE.match(h):
                    # 如果 Hint 包含比较符号，千万别当作值去搜！
                    if any(op in h for op in [">", "<", "=", "over", "under", "above", "below"]):
                        continue
                    phrases.add(h.strip())

        # 3. Fallback from all keywords (补漏，但必须严格)
        for w in all_kw:
            wl = (w or "").strip()
            if not wl: continue

            # 过滤停用词、学段等
            if wl.lower() in SCHEMA_STOP_WORDS or wl.lower() in CONCEPT_STOP_WORDS: continue
            if RE_GRADE_HINT.match(wl) or RE_AGE_RANGE.match(wl): continue

            # 🔥 修正后的逻辑：严禁纯数字！
            # 1500 -> isdigit=True -> 拒绝
            # 2023 -> isdigit=True -> 拒绝
            # CDS-123 -> isdigit=False -> 允许
            # Alameda -> isdigit=False -> 允许
            if wl.isdigit():
                continue

                # 之前的启发式保留，但前提是它不是纯数字
            # 允许: 长度>=3 且 (有大写 或 有数字但不是纯数字)
            # 例子: "iPhone" (有大写), "District9" (有数字)
            if len(wl) >= 3 and (any(c.isupper() for c in wl) or any(c.isdigit() for c in wl)):
                phrases.add(wl)

        # 4. County variants (处理 Alameda County 这种)
        final_list = list(phrases)
        for p in list(phrases):  # copy current set to list to iterate
            m = re.match(r"^\s*(.+?)\s+county\s*$", p, flags=re.IGNORECASE)
            if m and len(m.group(1)) >= 3:
                final_list.append(m.group(1))

        return list(set(final_list))

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