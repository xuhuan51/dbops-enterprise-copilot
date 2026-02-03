import logging
import re
from typing import List, Dict, Set, Tuple, Any
from dataclasses import dataclass
from sqlalchemy import create_engine, text
from thefuzz import process, fuzz  # ✨ 引入模糊匹配

logger = logging.getLogger(__name__)

# ==========================================
# 常量定义 (从原 Orchestrator 移过来)
# ==========================================
BAD_COLNAME_TOKENS = {
    "street", "addr", "address", "mail", "phone", "fax", "email", "url", "web",
    "lat", "lon", "zip", "zipcode", "id", "code", "date", "time", "created", "updated"
}


@dataclass
class MatchCandidate:
    keyword: str
    db_val: str
    table: str
    column: str
    score: float = 0.0
    strength: str = "soft"  # hard / soft
    reason: str = ""

    def format_constraint(self) -> str:
        prefix = "🔴 CONSTRAINT" if self.strength == "hard" else "🟡 HINT"
        return (
            f"{prefix}: Entity '{self.keyword}' -> matches value '{self.db_val}' "
            f"in `{self.table}`.`{self.column}`"
        )


class ValueScanner:
    """
    特种兵：负责数据库值的扫描、模糊匹配和全表救援。
    """

    # ======================================================
    # 🚀 核心入口：阶梯式扫描 (Tiered Scan)
    # ======================================================
    def scan_tiered(
            self,
            db_path: str,
            columns: List[Dict],
            keywords: List[str],
            use_rescue: bool = False
    ) -> Tuple[List[MatchCandidate], List[Dict]]:
        """
        执行两阶段扫描策略：
        1. Tier 1: 在 RAG 召回的列中，进行 Exact SQL + Fuzzy 匹配。
        2. Tier 2: (如果仍有未匹配关键词) 在嫌疑表中进行全量 Rescue 扫描。

        返回: (匹配结果列表, 新发现的 Rescue 列列表)
        """
        if not columns or not keywords:
            return [], []

        # 预处理关键词 (去重、去空)
        clean_kws = list(set([k.strip() for k in keywords if len(k.strip()) >= 2]))

        # --- 🟢 Tier 1: 扫描 Top-20 列 (精准+模糊) ---
        logger.info(f"🔍 [Tier 1] Scanning {len(columns)} retrieved columns for {len(clean_kws)} keywords...")
        matches_tier1 = self._scan_columns_hybrid(db_path, columns, clean_kws)

        # 统计哪些关键词已经搞定了
        matched_kws = {m.keyword.lower() for m in matches_tier1}
        missing_kws = [k for k in clean_kws if k.lower() not in matched_kws]

        new_rescue_columns = []
        final_matches = list(matches_tier1)

        # --- 🚑 Tier 2: 救援模式 (Rescue Scan) ---
        # 触发条件：还有没找到的关键词 + 开启了 Rescue
        if use_rescue and missing_kws:
            logger.info(f"🚑 [Tier 2] Rescue triggered for missing keywords: {missing_kws}")

            # 1. 锁定嫌疑表 (只取出现频率最高的前 3 个表，避免性能爆炸)
            # 这里简单地取 unique table names，理想情况应该按 RAG 分数排序，这里简化处理取前 3 个
            seen_tables = list({c['table'] for c in columns})[:3]

            if seen_tables:
                # 2. 拉取这些表的所有“隐形列” (当前 Context 里没有的文本列)
                existing_keys = {f"{c['table']}.{c['column']}" for c in columns}
                rescue_cols_info = self._fetch_all_text_columns(db_path, seen_tables, existing_keys)

                if rescue_cols_info:
                    logger.info(f"   -> Scanning {len(rescue_cols_info)} hidden columns in tables: {seen_tables}...")

                    # 3. 对隐形列进行扫描 (Tier 1 逻辑复用)
                    matches_rescue = self._scan_columns_hybrid(db_path, rescue_cols_info, missing_kws)

                    if matches_rescue:
                        logger.info(f"   -> ✅ Rescue Success! Found {len(matches_rescue)} new matches.")
                        final_matches.extend(matches_rescue)

                        # 4. 记录新发现的列，以便加回 Context 给 LLM 看
                        matched_keys = {f"{m.table}.{m.column}" for m in matches_rescue}
                        new_rescue_columns = [c for c in rescue_cols_info if
                                              f"{c['table']}.{c['column']}" in matched_keys]
                    else:
                        logger.info("   -> Rescue scan found nothing.")
                else:
                    logger.info("   -> No extra text columns found to rescue.")

        return final_matches, new_rescue_columns

    # ======================================================
    # ⚙️ 底层逻辑：混合匹配 (Exact SQL + Fuzzy)
    # ======================================================
    def _scan_columns_hybrid(self, db_path: str, columns: List[Dict], keywords: List[str]) -> List[MatchCandidate]:
        matches = []
        try:
            engine = create_engine(f"sqlite:///{db_path}", connect_args={'timeout': 3})
            with engine.connect() as conn:
                for col_info in columns:
                    tbl, col = col_info['table'], col_info['column']

                    # 性能优化：跳过明显无关的列 (ID, Code, URL 等)
                    col_lower = col.lower()
                    if any(bad in col_lower for bad in BAD_COLNAME_TOKENS):
                        continue

                    for kw in keywords:
                        kw_matches = []

                        # --- A. 尝试 SQL 快速匹配 (Exact & Like) ---
                        # 数据库直接查，速度最快，不做模糊
                        try:
                            # 优先 Exact
                            sql_exact = text(
                                f'SELECT DISTINCT "{col}" FROM "{tbl}" WHERE "{col}" = :v COLLATE NOCASE LIMIT 1')
                            row = conn.execute(sql_exact, {"v": kw}).fetchone()
                            if row:
                                kw_matches.append((str(row[0]), 100))  # 100分

                            # 其次 Like (只在关键词够长时触发，防止 "a" 匹配所有)
                            elif len(kw) >= 3:
                                sql_like = text(f'SELECT DISTINCT "{col}" FROM "{tbl}" WHERE "{col}" LIKE :pat LIMIT 3')
                                rows = conn.execute(sql_like, {"pat": f"%{kw}%"}).fetchall()
                                for r in rows:
                                    # 简单的惩罚：如果匹配结果太长，分数降低
                                    val = str(r[0])
                                    score = 90 if len(val) < len(kw) * 3 else 70
                                    kw_matches.append((val, score))
                        except Exception:
                            continue  # 列可能不存在或类型不对，跳过

                        # --- B. 尝试 Fuzzy Match (仅当 SQL 没查到时) ---
                        # 只有当 SQL 失败，且这一列看起来像枚举 (Name/Type/City) 时才做
                        if not kw_matches:
                            try:
                                # 拉取前 50 个不重复值到内存做模糊匹配
                                # 注意：这是内存操作，不要拉太多
                                sql_distinct = text(
                                    f'SELECT DISTINCT "{col}" FROM "{tbl}" WHERE "{col}" IS NOT NULL LIMIT 50')
                                all_rows = conn.execute(sql_distinct).fetchall()
                                candidates = [str(r[0]) for r in all_rows if r[0]]

                                if candidates:
                                    # extractOne 返回 (match, score)
                                    best = process.extractOne(kw, candidates, scorer=fuzz.token_sort_ratio)
                                    if best and best[1] >= 80:  # 相似度阈值 80
                                        # 标记这是一个 Fuzzy 匹配
                                        # logger.debug(f"Fuzzy Match: {kw} ~ {best[0]} ({best[1]})")
                                        kw_matches.append((best[0], best[1]))
                            except Exception:
                                pass

                        # --- C. 生成结果对象 ---
                        for val, score in kw_matches:
                            m = MatchCandidate(keyword=kw, db_val=val, table=tbl, column=col, score=score)

                            # 判定 Hard/Soft
                            if score >= 90:
                                m.strength = "hard"
                                m.reason = "exact_or_high_fuzzy"
                            else:
                                m.strength = "soft"
                                m.reason = "partial_or_fuzzy"

                            matches.append(m)

        except Exception as e:
            logger.error(f"❌ [ValueScanner] Error: {e}")

        return matches

    # ======================================================
    # 🛠️ 辅助：拉取所有文本列 (Rescue 用)
    # ======================================================
    def _fetch_all_text_columns(self, db_path: str, tables: List[str], exclude_keys: Set[str]) -> List[Dict]:
        """从数据库元数据中拉取指定表的所有 TEXT 类型列"""
        new_cols = []
        try:
            engine = create_engine(f"sqlite:///{db_path}")
            with engine.connect() as conn:
                for tbl in tables:
                    try:
                        # SQLite PRAGMA table_info returns: (cid, name, type, notnull, dflt_value, pk)
                        rows = conn.execute(text(f"PRAGMA table_info(\"{tbl}\")")).fetchall()
                        for r in rows:
                            c_name = r[1]
                            c_type = str(r[2]).upper()

                            # 只关心 TEXT 类列 (包括 VARCHAR, CHAR, TEXT, CLOB)
                            if "TEXT" in c_type or "CHAR" in c_type or "CLOB" in c_type or c_type == "":
                                key = f"{tbl}.{c_name}"
                                # 排除已经在 Context 里的列
                                if key not in exclude_keys:
                                    # 再次过滤掉 BAD 列
                                    if any(b in c_name.lower() for b in BAD_COLNAME_TOKENS):
                                        continue

                                    new_cols.append({
                                        "table": tbl,
                                        "column": c_name,
                                        "desc": "Rescue Scan Column",  # 标记来源
                                        "sample_values": []  # 待填充
                                    })
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Error fetching schema info: {e}")
        return new_cols


# 单例模式
value_scanner = ValueScanner()