import time
import json
import os
import uuid
import re
import pymysql
from decimal import Decimal
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from app.core.config import settings

# ==========================================
# 📝 日志路径配置
# ==========================================
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "events.jsonl")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


# ==========================================
# 🛠️ 基础工具函数
# ==========================================
def _jsonable(v: Any):
    """
    🔥 核心清洗函数：处理 JSON 不支持的类型
    """
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)  # 🔥 关键：Decimal -> float，防止 json dump 报错
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8", errors="ignore")
        except Exception:
            return str(v)
    return v


def append_event(event: dict):
    """写入审计日志"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"❌ [Log Error] Failed to write event log: {e}")


def _security_precheck(sql: str):
    """
    🔥 安全预检：拦截非查询语句
    """
    sql_upper = sql.strip().upper()
    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
        raise ValueError("Security: Only SELECT/WITH statements are allowed.")

    if ";" in sql:
        parts = sql.split(";")
        if len(parts) > 1 and any(p.strip() for p in parts[1:]):
            raise ValueError("Security: Multiple statements detected.")

    forbidden_patterns = [
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|REVOKE)\b",
        r"\bINTO\s+(OUTFILE|DUMPFILE)\b",
        r"\bLOAD_FILE\b",
    ]
    for pattern in forbidden_patterns:
        if re.search(pattern, sql_upper):
            raise ValueError(f"Security: Forbidden keyword detected by pattern: {pattern}")


def get_proxy_connection():
    try:
        return pymysql.connect(
            host=settings.PROXY_HOST,
            port=settings.PROXY_PORT,
            user=settings.PROXY_USER,
            password=settings.PROXY_PASSWORD,
            database=settings.PROXY_LOGIC_DB,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            autocommit=True
        )
    except Exception as e:
        print(f"❌ [Critical] Proxy Connection Failed: {e}")
        raise e


# =========================================================
# 🔥 核心修复：获取列名白名单 (安全版)
# =========================================================
def get_tables_columns(table_names: List[str]) -> Dict[str, List[str]]:
    if not table_names:
        return {}

    table_columns = {}

    try:
        conn = get_proxy_connection()
        with conn.cursor() as cur:
            for t_name in table_names:
                try:
                    # 使用最原始的 SQL
                    sql = f"SHOW COLUMNS FROM `{t_name}`"
                    cur.execute(sql)
                    columns_data = cur.fetchall()

                    col_list = [row['Field'] for row in columns_data]

                    if col_list:
                        table_columns[t_name] = col_list

                except Exception as inner_e:
                    print(f"   ❌ [Meta Warning] Failed to fetch columns for '{t_name}': {inner_e}")

        conn.close()
    except Exception as e:
        print(f"❌ [Meta Error] Global failure in get_tables_columns: {e}")
        return {}

    return table_columns

# ==========================================
# 2. Agent 专用：验证器 (EXPLAIN)
# ==========================================
def execute_sql_explain(sql: str, trace_id: str = "N/A") -> bool:
    start = time.time()
    err = None
    status = "SUCCESS"

    try:
        _security_precheck(sql)
        with get_proxy_connection() as conn:
            with conn.cursor() as cur:
                conn.ping(reconnect=True)
                if hasattr(settings, "SQL_TIMEOUT_MS"):
                    cur.execute(f"SET SESSION MAX_EXECUTION_TIME={settings.SQL_TIMEOUT_MS}")

                cur.execute(f"EXPLAIN {sql}")
                return True

    except Exception as e:
        err = str(e)
        status = "ERROR"
        # 仅打印简略错误供调试
        print(f"    ❌ [Executor][{trace_id}] EXPLAIN Error: {err[:200]}...")
        if "DEBUG" in os.environ:
            print(f"      -> DEBUG: DB={getattr(settings, 'PROXY_LOGIC_DB', 'N/A')}")
        raise e

    finally:
        latency_ms = int((time.time() - start) * 1000)
        event = {
            "trace_id": trace_id,
            "user_id": "system_validator",
            "route": "EXPLAIN",
            "sql": sql,
            "latency_ms": latency_ms,
            "truncated": False,
            "error": err[:500] if err else None,
            "status": status,
            "ts_iso": datetime.utcnow().isoformat(),
        }
        append_event(event)


# ==========================================
# 3. API 专用：执行器 (SELECT)
# ==========================================
def execute_select(user_id: str, sql: str, trace_id: str = None) -> Dict[str, Any]:
    if not trace_id:
        trace_id = str(uuid.uuid4())

    start = time.time()
    clean_data = []  # 🔥 存储清洗后的数据
    truncated = False
    err = None

    try:
        _security_precheck(sql)
    except ValueError as e:
        return {"trace_id": trace_id, "error": str(e), "data": [], "latency_ms": 0}

    try:
        with get_proxy_connection() as conn:
            # 🔥 保持 DictCursor，不覆盖 cursorclass
            with conn.cursor() as cur:
                if hasattr(settings, "SQL_TIMEOUT_MS"):
                    cur.execute(f"SET SESSION MAX_EXECUTION_TIME={settings.SQL_TIMEOUT_MS}")

                cur.execute(sql)

                # 获取数据
                limit_n = getattr(settings, "RESULT_MAX_ROWS", 1000)
                raw_data = cur.fetchmany(limit_n + 1)

                if len(raw_data) > limit_n:
                    truncated = True
                    raw_data = raw_data[:limit_n]

                # 🔥 数据清洗循环：Dict -> Dict (处理 Decimal 和 Datetime)
                for row in raw_data:
                    # row 是 {'total_amount': Decimal('800.00'), ...}
                    new_row = {}
                    for key, val in row.items():
                        new_row[key] = _jsonable(val)
                    clean_data.append(new_row)

    except Exception as e:
        err = str(e)
        print(f"❌ [Select Error] {err}")
        # 这里不抛出异常，返回空数据和错误信息，保证前端不崩

    latency_ms = int((time.time() - start) * 1000)

    # 记录审计日志
    event = {
        "trace_id": trace_id,
        "user_id": user_id,
        "route": "QUERY",
        "sql": sql,
        "latency_ms": latency_ms,
        "truncated": truncated,
        "error": err[:500] if err else None,
        "ts_iso": datetime.utcnow().isoformat(),
    }
    append_event(event)

    return {
        "trace_id": trace_id,
        "data": clean_data,  # 🔥 改名为 data，对应 List[Dict]
        "truncated": truncated,
        "latency_ms": latency_ms,
        "error": err,
    }