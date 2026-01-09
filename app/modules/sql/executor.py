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

# 日志路径配置
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "events.jsonl")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


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
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"❌ [Log Error] Failed to write event log: {e}")


# ==========================================
# 🔥 安全预检 (Security Pre-check)
# ==========================================
def _security_precheck(sql: str):
    sql_upper = sql.strip().upper()
    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
        raise ValueError("Security: Only SELECT/WITH statements are allowed.")

    # 你的担忧是对的：如果用 USE 语句补救，可能会被这里拦住，或者导致连接状态混乱
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
    🔥 修正版：通过 connect 参数直连 dbops_proxy，不使用 USE 语句
    """
    # 优先使用 .env 里的 MYSQL_CONNECT_DB，如果没有则兜底 dbops_proxy
    target_db = getattr(settings, "MYSQL_CONNECT_DB", "dbops_proxy")
    proxy_port = int(getattr(settings, "MYSQL_PORT", 3307))

    return pymysql.connect(
        host=settings.MYSQL_HOST,
        port=proxy_port,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        # ✅ 关键修正：直接在握手阶段指定 Schema
        database=target_db,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        autocommit=True
    )


# ==========================================
# 2. Agent 专用：验证器 (EXPLAIN) - 企业级增强版
# ==========================================
def execute_sql_explain(sql: str, trace_id: str = "N/A") -> bool:
    start = time.time()
    err = None
    status = "SUCCESS"

    try:
        # 1. 安全预检
        _security_precheck(sql)

        # 2. 数据库 EXPLAIN
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
        # 这里的 print 可以保留用于控制台快速调试
        print(f"    ❌ [Executor][{trace_id}] EXPLAIN Error: {err[:200]}...")
        if "DEBUG" in os.environ:
            print(f"      -> DEBUG: DB={getattr(settings, 'MYSQL_CONNECT_DB', 'N/A')}")
        raise e

    finally:
        # 🔥【关键修改】无论成功还是失败，都写入审计日志
        latency_ms = int((time.time() - start) * 1000)
        event = {
            "trace_id": trace_id,
            "user_id": "system_validator",  # 标记这是验证器产生的日志
            "route": "EXPLAIN",  # 明确这是 SQL 验证操作
            "sql": sql,
            "latency_ms": latency_ms,
            "truncated": False,
            "error": err[:500] if err else None,  # 记录报错信息
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
    columns = []
    rows = []
    truncated = False
    err = None

    try:
        _security_precheck(sql)
    except ValueError as e:
        return {"trace_id": trace_id, "error": str(e), "rows": [], "latency_ms": 0}

    try:
        with get_proxy_connection() as conn:
            # 兼容旧代码，切换回普通 Cursor 返回 list
            conn.cursorclass = pymysql.cursors.Cursor
            with conn.cursor() as cur:
                if hasattr(settings, "SQL_TIMEOUT_MS"):
                    cur.execute(f"SET SESSION MAX_EXECUTION_TIME={settings.SQL_TIMEOUT_MS}")

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