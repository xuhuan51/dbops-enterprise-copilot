"""
═══════════════════════════════════════════════════════════════════════════════
📦 模块名称: value_linker.py
📝 模块功能: 基于 LCS（最长公共子串）的值匹配器
🎯 核心思路: LLM 先定位列 → 在目标列内用 LCS 精准匹配数据库实际值
═══════════════════════════════════════════════════════════════════════════════

灵感来源: MAG-SQL (BIRD benchmark)
- LLM 负责语义判断（这个实体值该在哪个列）
- LCS 负责字符串匹配（这个列里实际存了什么值）
"""

import asyncio
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from app.core.logger import logger


# ============================================================================
# 1. 数据结构
# ============================================================================

@dataclass
class ValueMatch:
    """一条值匹配结果"""
    keyword: str        # 用户说的词 (e.g., "北京")
    db_value: str       # 数据库实际值 (e.g., "北京市")
    table: str          # 表名
    column: str         # 列名
    score: float        # 匹配分数 0~100
    match_type: str     # exact / lcs / substring

    def to_dict(self) -> Dict[str, str]:
        """供下游 prompt 使用的简洁格式"""
        return {
            "user_input": self.keyword,
            "db_value": self.db_value,
            "table": self.table,
            "column": self.column,
        }


# ============================================================================
# 2. LCS 核心算法
# ============================================================================

def lcs_length(s1: str, s2: str) -> int:
    """
    计算两个字符串的最长公共子串长度
    时间复杂度: O(m*n)，对于短字符串完全够用
    """
    m, n = len(s1), len(s2)
    if m == 0 or n == 0:
        return 0

    # 空间优化：只用两行
    prev = [0] * (n + 1)
    curr = [0] * (n + 1)
    max_len = 0

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if s1[i - 1] == s2[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > max_len:
                    max_len = curr[j]
            else:
                curr[j] = 0
        prev, curr = curr, [0] * (n + 1)

    return max_len


def calculate_lcs_score(keyword: str, db_value: str) -> Tuple[float, str]:
    """
    计算匹配分数，返回 (score, match_type)

    策略（按优先级）：
    1. 精确匹配 → 100 分
    2. 子串包含（关键词在数据库值里）→ 85~95 分
    3. LCS 匹配 → 按比例打分
    """
    kw = keyword.lower().strip()
    db = db_value.lower().strip()

    # 1. 精确匹配
    if kw == db:
        return (100.0, "exact")

    # 2. 关键词是数据库值的子串 (e.g., "北京" in "北京市")
    if kw in db:
        ratio = len(kw) / len(db)
        return (85.0 + ratio * 10, "substring")

    # 3. 数据库值是关键词的子串 —— 要非常谨慎！
    #    防止 "M" 匹配到 "华为 Mate 60" 这种情况
    if db in kw:
        # 数据库值太短（< 3字符）或占比太低，直接跳过
        if len(db) < 3 or len(db) / len(kw) < 0.5:
            return (0.0, "no_match")
        ratio = len(db) / len(kw)
        return (80.0 + ratio * 10, "substring")

    # 4. LCS 匹配
    lcs_len = lcs_length(kw, db)
    max_len = max(len(kw), len(db))
    score = (lcs_len / max_len) * 100

    if score >= 60:
        return (score, "lcs")

    return (0.0, "no_match")


# ============================================================================
# 3. 列内匹配（核心函数）
# ============================================================================

async def match_values_in_columns(
    entity_columns: List[Dict[str, Any]],
    db_conn_func,
    min_score: float = 70.0,
    top_k: int = 5,
) -> List[ValueMatch]:
    """
    在 LLM 指定的列内进行 LCS 值匹配

    参数:
        entity_columns: LLM 输出的实体-列映射列表，格式:
            [
                {
                    "value": "北京",
                    "candidate_columns": [
                        {"table": "user_addresses", "column": "province"},
                        {"table": "user_addresses", "column": "city"}
                    ]
                },
                {
                    "value": "华为 Mate 60",
                    "candidate_columns": [
                        {"table": "order_items", "column": "product_name"}
                    ]
                }
            ]
        db_conn_func: 异步函数，签名 async (table, column, keyword) -> List[str]
                      返回该列中与 keyword 相关的 distinct 值
        min_score: 最低匹配分数
        top_k: 最多返回几条匹配

    返回:
        ValueMatch 列表
    """
    all_matches: List[ValueMatch] = []

    for entity in entity_columns:
        keyword = entity.get("value", "").strip()
        if not keyword or len(keyword) < 2:
            continue

        candidates = entity.get("candidate_columns", [])

        for col_info in candidates:
            table = col_info.get("table", "")
            column = col_info.get("column", "")
            if not table or not column:
                continue

            try:
                # 从数据库获取该列的候选值
                db_values = await db_conn_func(table, column, keyword)

                for db_val in db_values:
                    db_val_str = str(db_val).strip()
                    if not db_val_str:
                        continue

                    score, match_type = calculate_lcs_score(keyword, db_val_str)

                    if score >= min_score:
                        all_matches.append(ValueMatch(
                            keyword=keyword,
                            db_value=db_val_str,
                            table=table,
                            column=column,
                            score=score,
                            match_type=match_type,
                        ))
            except Exception as e:
                logger.warning(f"⚠️ [ValueLinker] Failed to query {table}.{column}: {e}")
                continue

    # 去重 & 排序：每个 keyword 只保留最高分的匹配
    best_per_keyword = _select_best_per_keyword(all_matches)
    return best_per_keyword[:top_k]


def match_values_from_samples(
    entity_columns: List[Dict[str, Any]],
    selected_schema: Dict[str, Any],
    min_score: float = 70.0,
    top_k: int = 5,
) -> List[ValueMatch]:
    """
    不查数据库，直接用 retrieved_schema 里的 sample_values 做匹配
    适用于不想额外查库的场景（你当前的架构就是这种）

    参数:
        entity_columns: LLM 输出的实体-列映射
        selected_schema: 选列后的 schema（带 sample_values）
        min_score: 最低分数
        top_k: 最多返回几条
    """
    all_matches: List[ValueMatch] = []

    for entity in entity_columns:
        keyword = entity.get("value", "").strip()
        if not keyword or len(keyword) < 2:
            continue

        candidates = entity.get("candidate_columns", [])

        for col_info in candidates:
            table = col_info.get("table", "")
            column = col_info.get("column", "")
            if not table or not column:
                continue

            # 从 selected_schema 里找 sample_values
            table_data = selected_schema.get(table, {})
            columns = table_data.get("columns", [])
            sample_values = []
            for c in columns:
                if c.get("column_name") == column:
                    sample_values = c.get("sample_values", [])
                    break

            # 对每个样本值做 LCS 匹配
            for sv in sample_values:
                sv_str = str(sv).strip()
                if not sv_str:
                    continue

                score, match_type = calculate_lcs_score(keyword, sv_str)

                if score >= min_score:
                    all_matches.append(ValueMatch(
                        keyword=keyword,
                        db_value=sv_str,
                        table=table,
                        column=column,
                        score=score,
                        match_type=match_type,
                    ))

    best = _select_best_per_keyword(all_matches)
    return best[:top_k]


# ============================================================================
# 4. 辅助函数
# ============================================================================

def _select_best_per_keyword(matches: List[ValueMatch]) -> List[ValueMatch]:
    """每个 keyword 只保留分数最高的一条匹配"""
    if not matches:
        return []

    best_map: Dict[str, ValueMatch] = {}
    for m in matches:
        existing = best_map.get(m.keyword)
        if existing is None or m.score > existing.score:
            best_map[m.keyword] = m

    # 按分数降序
    result = sorted(best_map.values(), key=lambda x: x.score, reverse=True)
    return result


def format_value_mappings_for_prompt(matches: List[ValueMatch]) -> str:
    """格式化匹配结果，供 SQL 生成 prompt 使用"""
    if not matches:
        return "（无值映射）"

    lines = []
    for m in matches:
        lines.append(
            f'- "{m.keyword}" → "{m.db_value}" '
            f'(in {m.table}.{m.column}, score={m.score:.0f}, type={m.match_type})'
        )
    return "\n".join(lines)