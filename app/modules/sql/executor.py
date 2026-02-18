import json
import os
import time
import uuid
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Dict, List, Optional

import aiomysql
from app.core.config import settings
from app.core.logger import logger
from app.modules.sql.guardrail import validate_and_rewrite

# ==========================================
# 📝 日志配置
# ==========================================
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "events.jsonl")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


def append_event(event: dict):
    """
    将事件追加写入到 logs/events.jsonl
    """
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"❌ [Log Error] {e}")


# ==========================================
# 🔌 数据库连接池管理
# ==========================================
# 🔥 修正 2: 变量名改为 _db_pool (之前是 _proxy_pool)
_db_pool: Optional[aiomysql.Pool] = None


async def get_db_pool() -> aiomysql.Pool:
    """
    获取数据库连接池 (直连 MySQL)
    """
    global _db_pool
    if _db_pool is None:
        try:
            logger.info(
                f"🔌 [Executor] Connecting to MySQL -> {settings.DB_HOST}:{settings.DB_PORT} ({settings.DB_NAME})")

            _db_pool = await aiomysql.create_pool(
                host=settings.DB_HOST,
                port=settings.DB_PORT,
                user=settings.DB_USER,
                password=settings.DB_PASSWORD,
                db=settings.DB_NAME,
                cursorclass=aiomysql.DictCursor,
                autocommit=True,
                maxsize=20,
                connect_timeout=10
            )
        except Exception as e:
            logger.error(f"❌ [Executor] Failed to create DB pool: {e}")
            raise e
    return _db_pool


# ==========================================
# 🛠️ 辅助函数
# ==========================================
def _jsonable(v: Any):
    if v is None: return None
    if isinstance(v, (datetime, date)): return v.isoformat()
    if isinstance(v, Decimal): return float(v)
    if isinstance(v, (bytes, bytearray)): return v.decode("utf-8", errors="ignore")
    return v


def _security_precheck(sql: str):
    sql_upper = sql.strip().upper()
    valid = ("SELECT", "WITH", "EXPLAIN", "SHOW", "DESC")
    if not any(sql_upper.startswith(v) for v in valid):
        raise ValueError("Security: Only SELECT/WITH/EXPLAIN/SHOW statements are allowed.")


# ==========================================
# 🚀 业务逻辑 1: 执行数据查询 (带日志)
# ==========================================
async def execute_select_async(user_id: str, sql: str, trace_id: str = None) -> Dict[str, Any]:
    if not trace_id: trace_id = str(uuid.uuid4())
    start = time.time()
    clean_data = []
    truncated = False
    err = None
    final_sql = sql

    try:
        # 1. 调用 Guardrail 进行安检和改写
        check_result = validate_and_rewrite(sql)
        if not check_result.ok:
            raise ValueError(f"Guardrail Blocked: {check_result.reason}")

        # 使用改写后的 SQL
        final_sql = check_result.rewritten_sql

        pool = await get_db_pool()

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 执行最终 SQL
                await cur.execute(final_sql)

                limit_n = getattr(settings, "RESULT_MAX_ROWS", 1000)
                raw_data = await cur.fetchmany(limit_n + 1)

                if len(raw_data) > limit_n:
                    truncated = True
                    raw_data = raw_data[:limit_n]

                for row in raw_data:
                    new_row = {k: _jsonable(v) for k, v in row.items()}
                    clean_data.append(new_row)

    except Exception as e:
        err = str(e)
        logger.error(f"❌ [Select Error] {err}", extra={"trace_id": trace_id})

    latency_ms = int((time.time() - start) * 1000)

    # 写入审计日志
    append_event({
        "trace_id": trace_id, "user_id": user_id, "route": "QUERY",
        "origin_sql": sql, "executed_sql": final_sql,
        "latency_ms": latency_ms, "error": err[:500] if err else None,
        "ts_iso": datetime.utcnow().isoformat(),
    })

    return {
        "trace_id": trace_id, "data": clean_data,
        "truncated": truncated, "latency_ms": latency_ms, "error": err
    }


# ==========================================
# 🚀 业务逻辑 2: 获取表结构
# ==========================================
async def get_tables_columns(table_names: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    if not table_names:
        return {}

    pool = await get_db_pool()
    result = {t: [] for t in table_names}

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for t_name in table_names:
                try:
                    await cur.execute(f"SHOW FULL COLUMNS FROM {t_name}")
                    rows = await cur.fetchall()

                    for row in rows:
                        row_lower = {k.lower(): v for k, v in row.items()}
                        col_info = {
                            "name": row_lower.get('field'),
                            "type": row_lower.get('type'),
                            "comment": (row_lower.get('comment') or "无注释").replace("\n", " ")
                        }
                        result[t_name].append(col_info)
                except Exception as e:
                    logger.warning(f"⚠️ [Executor] Failed to DESC table {t_name}: {e}")
                    continue

    return result


# ==========================================
# 🚀 业务逻辑 3: 字段反查表
# ==========================================
async def search_tables_by_column(column_keyword: str) -> List[str]:
    if not column_keyword: return []
    pool = await get_db_pool()

    # 注意：这里也需要改成 settings.DB_NAME
    sql = "SELECT DISTINCT TABLE_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND COLUMN_NAME LIKE %s LIMIT 5"
    pattern = f"%{column_keyword}%"
    found_tables = []
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (settings.DB_NAME, pattern))
                rows = await cur.fetchall()
                found_tables = [row['TABLE_NAME'] for row in rows]
    except Exception:
        pass
    return found_tables


# ==========================================
# 🚀 业务逻辑 4: 验证 SQL 语法
# ==========================================
async def execute_sql_explain(sql: str, trace_id: str = "N/A"):
    _security_precheck(sql)
    pool = await get_db_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"EXPLAIN {sql}")
                return await cur.fetchall()
    except Exception as e:
        logger.warning(f"⚠️ [Executor] SQL Explain failed: {e}", extra={"trace_id": trace_id})
        raise e