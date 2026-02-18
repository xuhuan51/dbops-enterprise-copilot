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
from typing import List, Dict, Any, Optional, Tuple, Set
from dataclasses import dataclass, field
from app.core.logger import logger

# 白名单：只允许查这些表.列，防止 SQL 注入
# 🔧 根据你的实际业务表按需扩充
ALLOWED_QUERY_COLUMNS: Set[Tuple[str, str]] = {
    ("products", "product_name"),
    ("order_items", "product_name"),
    ("order_items", "sku_name"),
    ("brands", "brand_name"),
    ("categories", "category_name"),
    ("user_addresses", "province"),
    ("user_addresses", "city"),
    ("user_addresses", "district"),
    ("orders", "order_status"),
    ("users", "gender"),
    ("users", "user_level"),
    ("users", "username"),
    ("product_skus", "sku_name"),
}


# ============================================================================
# 1. 数据结构
# ============================================================================

@dataclass
class ValueMatch:
    """一条值匹配结果"""
    keyword: str        # 用户说的词 (e.g., "小米 14 PRO")
    db_value: str       # 数据库最佳匹配值 (e.g., "小米14 Pro版")
    table: str          # 表名
    column: str         # 列名
    score: float        # 匹配分数 0~100
    match_type: str     # exact / lcs / substring / db_substring ...

    # 🔥 新增：当有多个候选值时
    all_db_values: List[str] = field(default_factory=list)  # 所有命中的值
    suggest_operator: str = "="  # "=" 精确匹配 / "LIKE" 模糊匹配

    def to_dict(self) -> Dict[str, Any]:
        """供下游 prompt 使用的格式"""
        return {
            "user_input": self.keyword,
            "db_value": self.db_value,
            "table": self.table,
            "column": self.column,
            "suggest_operator": self.suggest_operator,
            "all_db_values": self.all_db_values,
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


def _normalize(s: str) -> str:
    """
    归一化字符串：统一小写 + 去除所有空格
    解决 "小米 14" vs "小米14" 这种空格差异导致子串匹配失败的问题
    """
    return s.lower().strip().replace(" ", "").replace("\u3000", "")


def calculate_lcs_score(keyword: str, db_value: str) -> Tuple[float, str]:
    """
    计算匹配分数，返回 (score, match_type)

    策略（按优先级）：
    1. 精确匹配 → 100 分
    2. 子串包含（关键词在数据库值里）→ 85~95 分
    3. LCS 匹配 → 按比例打分

    关键改进：
    - 先用原始字符串做精确匹配
    - 再用归一化字符串（去空格）做子串和 LCS 匹配
      解决 "小米 14" vs "小米14 Pro 新款" 因空格不同而匹配失败的问题
    """
    kw_raw = keyword.lower().strip()
    db_raw = db_value.lower().strip()

    # 1. 精确匹配（原始字符串）
    if kw_raw == db_raw:
        return (100.0, "exact")

    # 归一化（去空格）后再比较
    kw = _normalize(keyword)
    db = _normalize(db_value)

    # 1b. 归一化后精确匹配
    if kw == db:
        return (99.0, "exact_normalized")

    # 2. 关键词是数据库值的子串
    #    e.g., "小米14" in "小米14pro新款" ✅
    #    e.g., "北京" in "北京市" ✅
    if kw in db:
        ratio = len(kw) / len(db)
        return (85.0 + ratio * 10, "substring")

    # 3. 数据库值是关键词的子串 —— 要非常谨慎！
    #    防止 "M" 匹配到 "华为mate60" 这种情况
    if db in kw:
        if len(db) < 3 or len(db) / len(kw) < 0.5:
            return (0.0, "no_match")
        ratio = len(db) / len(kw)
        return (80.0 + ratio * 10, "substring")

    # 4. LCS 匹配（用归一化字符串）
    lcs_len = lcs_length(kw, db)
    max_len = max(len(kw), len(db))
    score = (lcs_len / max_len) * 100

    if score >= 60:
        return (score, "lcs")

    return (0.0, "no_match")


# ============================================================================
# 3. DB 实查辅助函数
# ============================================================================

async def _query_distinct_values_like(
    table: str,
    column: str,
    keyword: str,
    limit: int = 20,
) -> List[str]:
    """
    通过 LIKE 从数据库中查询候选值
    使用你已有的 aiomysql 连接池

    安全措施：
    1. 白名单校验表名和列名（防注入）
    2. keyword 通过参数化查询传入（防注入）
    3. 只允许 SELECT DISTINCT（只读）
    """
    # 白名单校验
    if (table, column) not in ALLOWED_QUERY_COLUMNS:
        logger.warning(f"⚠️ [ValueLinker] Blocked query: {table}.{column} not in whitelist")
        return []

    try:
        # 延迟导入，避免循环依赖
        from app.modules.sql.executor import get_db_pool

        pool = await get_db_pool()

        # ── 构造多种 LIKE 模式，尽可能宽地召回 ──
        kw_clean = keyword.strip()
        kw_no_space = kw_clean.replace(" ", "")  # "小米 14 PRO" → "小米14PRO"

        # 模式1: 原始关键词整串匹配
        #   "小米 14 PRO" → LIKE '%小米 14 PRO%'
        # 模式2: 去空格整串匹配
        #   "小米14PRO" → LIKE '%小米14PRO%'
        # 模式3: 按空格拆词，每个词单独 LIKE，用 AND 连接（核心修复！）
        #   "小米 14 PRO" → (col LIKE '%小米%' AND col LIKE '%14%' AND col LIKE '%PRO%')
        #   这样 "小米14 Pro 旗舰版" 就能命中了

        params = []
        or_clauses = []

        # 模式1: 原始整串
        or_clauses.append(f"`{column}` LIKE %s")
        params.append(f"%{kw_clean}%")

        # 模式2: 去空格整串（如果和原始不同）
        if kw_no_space != kw_clean:
            or_clauses.append(f"`{column}` LIKE %s")
            params.append(f"%{kw_no_space}%")

        # 模式3: 拆词 AND 匹配（关键！）
        tokens = [t.strip() for t in kw_clean.split() if len(t.strip()) >= 1]
        if len(tokens) >= 2:
            and_parts = []
            for token in tokens:
                and_parts.append(f"`{column}` LIKE %s")
                params.append(f"%{token}%")
            or_clauses.append(f"({' AND '.join(and_parts)})")

        where = " OR ".join(or_clauses)
        sql = f"SELECT DISTINCT `{column}` FROM `{table}` WHERE ({where}) LIMIT {limit}"

        logger.info(f"   🔎 [ValueLinker] DB query: {sql} params={params}")

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, tuple(params))
                rows = await cur.fetchall()

                results = []
                for row in rows:
                    val = row.get(column)
                    if val is not None:
                        results.append(str(val))
                return results

    except Exception as e:
        logger.warning(f"⚠️ [ValueLinker] DB query failed for {table}.{column}: {e}")
        return []


# ============================================================================
# 4. 核心匹配函数（分层：sample_values → DB LIKE → LCS 精排）
# ============================================================================

async def match_values_with_fallback(
    entity_columns: List[Dict[str, Any]],
    selected_schema: Dict[str, Any],
    min_score: float = 70.0,
    top_k: int = 5,
    enable_db_fallback: bool = True,
) -> List[ValueMatch]:
    """
    分层值匹配（核心入口函数）

    Layer 1: 先在 sample_values 里找（快，零成本）
    Layer 2: 没命中的关键词，fallback 到数据库 LIKE 查询 + LCS 精排

    参数:
        entity_columns: LLM 输出的实体-列映射
        selected_schema: 选列后的 schema（带 sample_values）
        min_score: 最低匹配分数
        top_k: 最多返回几条
        enable_db_fallback: 是否启用数据库实查兜底
    """
    all_matches: List[ValueMatch] = []
    unmatched_entities: List[Dict[str, Any]] = []  # sample 没命中的，待 DB 兜底

    # ── Layer 1: Sample Values 快速通道 ──────────────────────────────
    for entity in entity_columns:
        keyword = entity.get("value", "").strip()
        if not keyword or len(keyword) < 2:
            continue

        candidates = entity.get("candidate_columns", [])
        entity_matched = False

        for col_info in candidates:
            table = col_info.get("table", "")
            column = col_info.get("column", "")
            if not table or not column:
                continue

            # 从 selected_schema 里找 sample_values
            table_data = selected_schema.get(table, {})
            columns_list = table_data.get("columns", [])
            sample_values = []
            for c in columns_list:
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
                    entity_matched = True

        # 如果 sample_values 没命中，记录下来待 DB 兜底
        if not entity_matched:
            unmatched_entities.append(entity)

    # ── Layer 2: DB LIKE 兜底 ───────────────────────────────────────
    if enable_db_fallback and unmatched_entities:
        logger.info(
            f"🔄 [ValueLinker] {len(unmatched_entities)} entities not found in samples, "
            f"falling back to DB LIKE query..."
        )

        for entity in unmatched_entities:
            keyword = entity.get("value", "").strip()
            candidates = entity.get("candidate_columns", [])

            for col_info in candidates:
                table = col_info.get("table", "")
                column = col_info.get("column", "")
                if not table or not column:
                    continue

                # 去数据库查
                try:
                    db_values = await _query_distinct_values_like(
                        table, column, keyword, limit=20
                    )

                    if not db_values:
                        logger.info(f"   ❌ No DB results for \"{keyword}\" in {table}.{column}")
                        continue

                    logger.info(
                        f"   🔍 DB returned {len(db_values)} candidates for "
                        f"\"{keyword}\" in {table}.{column}"
                    )

                    # 对 DB 返回的值做 LCS 精排
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
                                match_type=f"db_{match_type}",  # 标记来源
                            ))
                except Exception as e:
                    logger.warning(f"⚠️ [ValueLinker] DB fallback failed for {table}.{column}: {e}")
                    continue

    # ── 去重 & 返回 ─────────────────────────────────────────────────
    best = _aggregate_per_keyword(all_matches)

    # 日志
    for m in best:
        src = "sample" if not m.match_type.startswith("db_") else "DB"
        logger.info(f"   ✅ [{src}] \"{m.keyword}\" → \"{m.db_value}\" ({m.table}.{m.column}, {m.score:.0f})")

    return best[:top_k]


# 保留同步版本作为向后兼容（不查数据库，纯 sample_values）
def match_values_from_samples(
    entity_columns: List[Dict[str, Any]],
    selected_schema: Dict[str, Any],
    min_score: float = 70.0,
    top_k: int = 5,
) -> List[ValueMatch]:
    """
    同步版本：只在 sample_values 里匹配（不查数据库）
    保留向后兼容，但推荐使用 match_values_with_fallback
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

            table_data = selected_schema.get(table, {})
            columns_list = table_data.get("columns", [])
            sample_values = []
            for c in columns_list:
                if c.get("column_name") == column:
                    sample_values = c.get("sample_values", [])
                    break

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

    best = _aggregate_per_keyword(all_matches)
    return best[:top_k]


# ============================================================================
# 5. 辅助函数
# ============================================================================

def _aggregate_per_keyword(matches: List[ValueMatch]) -> List[ValueMatch]:
    """
    按 keyword 分组聚合，智能决定用 = 还是 LIKE

    规则：
    - 只有 1 个候选值 且 精确匹配(score>=99) → suggest "="
    - 只有 1 个候选值 但不是精确匹配       → suggest "=" (用最佳匹配值)
    - 有多个候选值                         → suggest "LIKE" (用归一化后的公共模式)
    """
    if not matches:
        return []

    # 按 (keyword, table, column) 分组
    from collections import defaultdict
    groups: Dict[str, List[ValueMatch]] = defaultdict(list)
    for m in matches:
        key = f"{m.keyword}||{m.table}||{m.column}"
        groups[key].append(m)

    results: List[ValueMatch] = []

    for group_key, group_matches in groups.items():
        # 按分数降序
        group_matches.sort(key=lambda x: x.score, reverse=True)
        best = group_matches[0]
        all_values = list(set(m.db_value for m in group_matches))

        if len(all_values) == 1 and best.score >= 99:
            # 精确匹配：只有一个值且分数极高 → 用 =
            best.suggest_operator = "="
            best.all_db_values = all_values
        elif len(all_values) == 1:
            # 只有一个候选但不是精确匹配 → 也用 =，但用数据库实际值
            best.suggest_operator = "="
            best.all_db_values = all_values
        else:
            # 多个候选值 → 用 LIKE
            # 提取公共前缀作为 LIKE 模式
            like_pattern = _extract_like_pattern(best.keyword, all_values)
            best.suggest_operator = "LIKE"
            best.db_value = like_pattern  # 覆盖为 LIKE 模式
            best.all_db_values = all_values

        results.append(best)

    # 按分数降序
    results.sort(key=lambda x: x.score, reverse=True)
    return results


def _extract_like_pattern(keyword: str, db_values: List[str]) -> str:
    """
    从关键词和多个数据库值中提取最佳 LIKE 模式

    例如：
    keyword: "小米 14 PRO"
    db_values: ["小米14 Pro 旗舰版", "小米14 Pro版", "小米14 Pro 新款"]
    → 提取公共部分: "小米14 Pro" → LIKE 模式: "%小米14 Pro%"

    简化实现：找所有值的最长公共前缀
    """
    if not db_values:
        return f"%{keyword}%"

    if len(db_values) == 1:
        return db_values[0]

    # 找最长公共前缀
    prefix = db_values[0]
    for val in db_values[1:]:
        i = 0
        while i < len(prefix) and i < len(val) and prefix[i] == val[i]:
            i += 1
        prefix = prefix[:i]

    # 去掉末尾空格
    prefix = prefix.rstrip()

    # 公共前缀太短的话，用关键词的归一化版本
    if len(prefix) < 2:
        # 用拆词方式: "小米 14 PRO" → "%小米%14%Pro%"
        tokens = keyword.strip().split()
        if len(tokens) >= 2:
            pattern = "%" + "%".join(tokens) + "%"
        else:
            pattern = f"%{keyword.strip()}%"
        return pattern

    return f"%{prefix}%"


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