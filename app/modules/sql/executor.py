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

# ❌ 删除或注释掉原来的 mysql_conn，我们不再依赖它，防止混淆
# from app.infrastructure.db.mysql import mysql_conn

# 日志路径配置
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "events.jsonl")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


# ==========================================
# 1. 基础工具函数
# ==========================================

def _jsonable(v: Any):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8", errors="ignore")
        except Exception:
            return str(v)
    return v


def append_event(event: dict):
    """
    写入审计日志 (events.jsonl)
    """
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"❌ [Log Error] Failed to write event log: {e}")


# ==========================================
# 🔥 新增: 安全预检 (Security Pre-check)
# ==========================================
def _security_precheck(sql: str):
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


# ==========================================
# 🔌 核心工具：获取 Proxy 连接
# ==========================================
def get_proxy_connection():
    """
    🔥 关键修改：强制连接到 ShardingSphere Proxy 的逻辑库
    """
    # 确保我们在 .env 或 config.py 里配置了 MYSQL_CONNECT_DB=dbops_proxy
    target_db = getattr(settings, "MYSQL_CONNECT_DB", "dbops_proxy")

    return pymysql.connect(
        host=settings.MYSQL_HOST,
        port=int(settings.MYSQL_PORT),  # 必须是 3307
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=target_db,  # 🚨 必填！否则报 Error 1046
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,  # 让结果返回字典，方便处理
        connect_timeout=10
    )


# ==========================================
# 2. Agent 专用：验证器 (EXPLAIN)
# ==========================================

def execute_sql_explain(sql: str, trace_id: str = "N/A") -> bool:
    # 1. 安全检查
    try:
        _security_precheck(sql)
    except ValueError as e:
        print(f"    ⚠️ [Executor][{trace_id}] Pre-check blocked: {e}")
        raise e

    # 2. 数据库执行
    try:
        # 🔥 使用新的连接函数
        with get_proxy_connection() as conn:
            with conn.cursor() as cur:
                # 超时保护
                try:
                    if hasattr(settings, "SQL_TIMEOUT_MS"):
                        cur.execute(f"SET SESSION MAX_EXECUTION_TIME={settings.SQL_TIMEOUT_MS}")
                except Exception:
                    pass

                cur.execute(f"EXPLAIN {sql}")
                return True

    except Exception as e:
        print(f"    ❌ [Executor][{trace_id}] EXPLAIN Error: {str(e)[:100]}...")
        # 调试用：打印一下到底连的哪
        print(
            f"      -> DEBUG Info: Host={settings.MYSQL_HOST}, Port={settings.MYSQL_PORT}, DB={getattr(settings, 'MYSQL_CONNECT_DB', 'unknown')}")
        raise e


# ==========================================
# 3. API 专用：执行器 (SELECT)
# ==========================================

def execute_select(user_id: str, sql: str, trace_id: str = None) -> Dict[str, Any]:
    if not trace_id:
        trace_id = str(uuid.uuid4())

    start = time.time()
    columns = []
    rows = []
    truncated = False
    err = None

    # 安全检查
    try:
        _security_precheck(sql)
    except ValueError as e:
        return {"trace_id": trace_id, "error": str(e), "rows": [], "latency_ms": 0}

    try:
        # 🔥 使用新的连接函数
        with get_proxy_connection() as conn:
            # 注意：get_proxy_connection 默认用了 DictCursor，
            # 但如果你下游代码依赖 list/tuple 格式，这里可能要改回普通 Cursor。
            # 为了兼容你的旧代码逻辑（rows = [[v for v in r]...]），我们这里临时覆盖回默认 Cursor
            conn.cursorclass = pymysql.cursors.Cursor

            with conn.cursor() as cur:
                try:
                    if hasattr(settings, "SQL_TIMEOUT_MS"):
                        cur.execute(f"SET SESSION MAX_EXECUTION_TIME={settings.SQL_TIMEOUT_MS}")
                except Exception:
                    pass

                cur.execute(sql)

                if cur.description:
                    columns = [d[0] for d in cur.description]

                limit_n = getattr(settings, "RESULT_MAX_ROWS", 1000)
                data = cur.fetchmany(limit_n + 1)

                if len(data) > limit_n:
                    truncated = True
                    data = data[:limit_n]

                rows = []
                for r in data:
                    rows.append([_jsonable(x) for x in r])

    except Exception as e:
        err = str(e)
        print(f"❌ [Select Error] {err}")

    latency_ms = int((time.time() - start) * 1000)

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
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
        "latency_ms": latency_ms,
        "error": err,
    }