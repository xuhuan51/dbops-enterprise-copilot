import pymysql
import asyncio
from typing import List, Dict, Any
from app.core.config import settings
from app.core.logger import logger


class SchemaHelper:
    """
    [Schema 助手]
    负责：
    1. 补全 Join Key (外键)：如果选了表A和表B，自动把它们关联的 ID 列加进来。
    2. 注入样本值 (Sample Values)：去数据库查询真实的样本，填入 context。
    3. 格式化 (Format)：生成 LLM 易读的 Schema 字符串。
    """

    def augment_with_join_keys(self, db_id: str, retrieved_columns: List[Dict]) -> List[Dict]:
        """
        [图增强] 利用外键规则补全连接键。
        如果 retrieved_columns 里有 `orders` 表，但没有 `user_id`，这会导致无法 join `users`。
        本函数会自动补全这些 FK 列。
        """
        if not retrieved_columns:
            return []

        # 1. 提取当前已有的 "table.column"
        existing_keys = {f"{r['table']}.{r['column']}" for r in retrieved_columns}

        # 2. 提取涉及的所有表
        seen_tables = list({r['table'] for r in retrieved_columns})

        # 3. 简单的补全策略：强制把 ID 列加进来
        # (更高级的做法是查 information_schema.KEY_COLUMN_USAGE，这里用简单规则)
        new_columns = []

        # 规则：只要涉及某个表，就自动把它的 PK 和常见的 FK (xxx_id) 加进来
        # 这样 LLM 才有东西写 JOIN ON
        for tbl in seen_tables:
            # 假设 ID 列名规则：table_id 或 id
            possible_pks = [f"{tbl[:-1]}_id", f"{tbl}_id", "id"]  # users -> user_id

            for pk in possible_pks:
                key = f"{tbl}.{pk}"
                # 如果这个 ID 列不在已召回列表中，且数据库里大概率有这个列（这里没法验证，只能盲猜或查元数据）
                # 稳妥起见，我们只添加那些包含 "id" 的列作为 candidate，
                # 实际生产中这里应该查一下 information_schema 确认列存在。
                if key not in existing_keys:
                    new_columns.append({
                        "table": tbl,
                        "column": pk,
                        "column_type": "BIGINT",
                        "sample_values": [],
                        "column_comment": "🗝️ Potential Join Key",
                        "is_structural": True  # 标记为结构性列
                    })
                    existing_keys.add(key)

        if new_columns:
            logger.info(f"🔗 [Schema] Augmented {len(new_columns)} potential join keys")

        return retrieved_columns + new_columns

    @staticmethod
    async def inject_sample_values(db_id: str, columns: List[Dict], limit_per_col: int = 3) -> List[Dict]:
        """
        [样本注入]
        对于没有样本的列，去 MySQL 查 3 个非空值。
        这对于 "status" (0,1,2) 这种枚举列非常重要。
        """
        # 筛选出需要注入样本的列 (没有 sample_values 的)
        target_cols = [c for c in columns if not c.get("sample_values")]
        if not target_cols: return columns

        def _sync_query():
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

                for col in target_cols:
                    tbl, cn = col.get("table"), col.get("column")
                    try:
                        # 查 3 个非空 distinct 值
                        sql = f"SELECT DISTINCT `{cn}` FROM `{tbl}` WHERE `{cn}` IS NOT NULL LIMIT {limit_per_col}"
                        cursor.execute(sql)
                        rows = cursor.fetchall()
                        vals = [str(r[0]) for r in rows]
                        if vals:
                            col["sample_values"] = vals
                    except Exception as e:
                        # 某些列可能无法查询 (如 blob)，跳过
                        logger.warning(f"⚠️ Failed to sample {tbl}.{cn}: {e}")
                        pass
            except Exception as e:
                logger.error(f"❌ Sample injection failed: {e}")
            finally:
                if conn: conn.close()

        # 异步执行 IO 操作
        await asyncio.to_thread(_sync_query)
        return columns

    @staticmethod
    def format_schema_str(columns: List[Dict]) -> str:
        """
        [格式化] 将列列表转换为 Prompt 友好的字符串。
        """
        lines = []
        tables = {}
        # 按表分组
        for col in columns:
            t = col.get("table")
            if t not in tables: tables[t] = []
            tables[t].append(col)

        for t, cols in tables.items():
            lines.append(f"Table: {t}")
            for c in cols:
                c_name = c.get("column")
                comment = c.get("column_comment") or c.get("desc", "")
                samples = c.get("sample_values") or c.get("samples", [])

                # 构造样本字符串
                s_str = f" (Values: {', '.join([str(x) for x in samples[:5]])})" if samples else ""

                # 构造注释字符串
                c_str = f" | {comment}" if comment else ""

                lines.append(f"  - {c_name}{c_str}{s_str}")
            lines.append("")  # 空行分隔表

        return "\n".join(lines)


schema_helper = SchemaHelper()