import logging
import pymysql
from typing import List, Dict, Tuple
from thefuzz import process, fuzz  # pip install thefuzz
from app.core.config import settings
from app.modules.retrieval.match_helper import MatchCandidate

logger = logging.getLogger(__name__)

# 不需要扫描的列名关键词 (黑名单)
BAD_COLNAME_TOKENS = {
    "street", "addr", "address", "mail", "phone", "fax", "email", "url", "web",
    "lat", "lon", "zip", "zipcode", "id", "code", "date", "time", "created", "updated"
}


class ValueScanner:
    """
    [值扫描器]
    负责：在数据库中搜索用户提到的具体值 (Value)。
    例如用户问 "Show me orders for iPhone 15"，
    我们需要确定 "iPhone 15" 是在 `products.product_name` 列里。
    """

    def scan_tiered(
            self,
            db_id: str,
            columns: List[Dict],
            keywords: List[str],
            use_rescue: bool = False  # 暂时保留接口，但不实现复杂逻辑
    ) -> Tuple[List[MatchCandidate], List[Dict]]:
        """
        执行扫描。

        Args:
            db_id: 数据库ID (ecommerce)
            columns: 待扫描的列 (RAG 召回的 Top-K)
            keywords: 待查找的关键词 (用户 Query 中的 Value)

        Returns:
            (Matches, NewColumns)
        """
        if not columns or not keywords:
            return [], []

        # 1. 预处理关键词 (去重、去空)
        clean_kws = list(set([k.strip() for k in keywords if len(k.strip()) >= 2]))

        logger.info(f"🔍 [Scanner] Scanning {len(columns)} columns for keywords: {clean_kws}")

        # 2. 执行扫描
        matches = self._scan_mysql(columns, clean_kws)

        return matches, []  # NewColumns 留空

    def _scan_mysql(self, columns: List[Dict], keywords: List[str]) -> List[MatchCandidate]:
        matches = []
        conn = None
        try:
            # 连接 MySQL
            conn = pymysql.connect(
                host=settings.DB_HOST,
                port=settings.DB_PORT,
                user=settings.DB_USER,
                password=settings.DB_PASSWORD,
                database=settings.DB_NAME,
                charset='utf8mb4'
            )
            cursor = conn.cursor()

            for col_info in columns:
                tbl, col = col_info['table'], col_info['column']

                # 性能优化：跳过明显无关的列 (ID, Code, URL 等)
                col_lower = col.lower()
                if any(bad in col_lower for bad in BAD_COLNAME_TOKENS):
                    continue

                for kw in keywords:
                    # --- A. 尝试 Exact SQL 匹配 ---
                    # SELECT distinct val FROM tbl WHERE col = kw LIMIT 1
                    try:
                        sql_exact = f"SELECT DISTINCT `{col}` FROM `{tbl}` WHERE `{col}` = %s LIMIT 1"
                        cursor.execute(sql_exact, (kw,))
                        row = cursor.fetchone()

                        if row:
                            # 💯 精确匹配成功
                            matches.append(MatchCandidate(
                                keyword=kw, db_val=str(row[0]),
                                table=tbl, column=col,
                                score=100, strength="hard", reason="exact_match"
                            ))
                            continue  # 找到精确的就不用做模糊了

                        # --- B. 尝试 Like 模糊匹配 ---
                        # 只有关键词够长才做 (>=3)
                        if len(kw) >= 3:
                            sql_like = f"SELECT DISTINCT `{col}` FROM `{tbl}` WHERE `{col}` LIKE %s LIMIT 3"
                            cursor.execute(sql_like, (f"%{kw}%",))
                            rows = cursor.fetchall()

                            for r in rows:
                                val = str(r[0])
                                # 简单的长度惩罚：如果匹配出来的值太长，分数降低
                                # e.g. kw="Apple", val="Apple iPhone 15 Pro Max 512G..." -> 分数低一点
                                score = 95 if len(val) <= len(kw) + 5 else 80

                                matches.append(MatchCandidate(
                                    keyword=kw, db_val=val,
                                    table=tbl, column=col,
                                    score=score, strength="hard", reason="like_match"
                                ))
                    except Exception as e:
                        # 可能会遇到类型错误 (int vs str)，忽略
                        pass

        except Exception as e:
            logger.error(f"❌ [ValueScanner] MySQL Error: {e}")
        finally:
            if conn: conn.close()

        return matches


# 单例模式
value_scanner = ValueScanner()