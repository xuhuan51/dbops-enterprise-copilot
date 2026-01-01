import time
import json
import os
import uuid
from decimal import Decimal
from datetime import datetime, date
from typing import Any

from app.core.config import settings
from app.infrastructure.db.mysql import mysql_conn

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

def _append_event(event: dict):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def execute_select(user_id: str, sql: str) -> dict:
    """
    返回：
    {
      trace_id, columns, rows, truncated, latency_ms, error
    }
    """
    trace_id = str(uuid.uuid4())
    start = time.time()
    columns = []
    rows = []
    truncated = False
    err = None

    try:
        with mysql_conn() as conn:
            cur = conn.cursor()

            # 尝试设置 MySQL 语句超时（部分版本支持）
            try:
                cur.execute(f"SET SESSION MAX_EXECUTION_TIME={settings.SQL_TIMEOUT_MS}")
            except Exception:
                pass

            cur.execute(sql)
            columns = [d[0] for d in (cur.description or [])]

            # 只取最多 RESULT_MAX_ROWS 行
            fetch_n = settings.RESULT_MAX_ROWS + 1
            data = cur.fetchmany(fetch_n)

            if len(data) > settings.RESULT_MAX_ROWS:
                truncated = True
                data = data[: settings.RESULT_MAX_ROWS]

            rows = [[_jsonable(x) for x in r] for r in data]

    except Exception as e:
        err = str(e)[:300]

    latency_ms = int((time.time() - start) * 1000)

    event = {
        "trace_id": trace_id,
        "user_id": user_id,
        "route": "QUERY",
        "sql": sql,
        "latency_ms": latency_ms,
        "truncated": truncated,
        "error": err,
        "ts_iso": datetime.utcnow().isoformat(),
    }
    _append_event(event)

    return {
        "trace_id": trace_id,
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
        "latency_ms": latency_ms,
        "error": err,
    }
