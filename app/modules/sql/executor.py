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
from app.infrastructure.db.mysql import mysql_conn

# 日志路径配置
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "events.jsonl")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


# ==========================================
# 1. 基础工具函数
# ==========================================

def _jsonable(v: Any):
    # ... (保持不变) ...
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
    写入审计日志 (events.jsonl) - 公共方法，供 API 层记录 Agent 思考过程
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
    """
    轻量级静态检查，拦截危险 SQL，避免浪费 DB 连接。
    """
    sql_upper = sql.strip().upper()

    # 1. 必须是 SELECT 开头
    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
        raise ValueError("Security: Only SELECT/WITH statements are allowed.")

    # 2. 禁止多语句 (防止 SQL 注入: "SELECT 1; DROP TABLE users;")
    # 简单检查分号：如果分号后还有非空字符，视为多语句
    # (注：这只是简单防御，无法处理字符串内含分号的情况，但对 Agent 生成的规范 SQL 够用了)
    if ";" in sql:
        parts = sql.split(";")
        if len(parts) > 1 and any(p.strip() for p in parts[1:]):
            raise ValueError("Security: Multiple statements detected.")

    # 3. 禁止高危关键词 (正则匹配单词边界)
    # 拦截: DML/DDL, 文件操作, 系统表操作
    forbidden_patterns = [
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|REVOKE)\b",  # 修改数据
        r"\bINTO\s+(OUTFILE|DUMPFILE)\b",  # 导出文件
        r"\bLOAD_FILE\b",  # 读取文件
        # r"\bINFORMATION_SCHEMA\b",                                    # 可选：禁止查系统表
    ]

    for pattern in forbidden_patterns:
        if re.search(pattern, sql_upper):
            raise ValueError(f"Security: Forbidden keyword detected by pattern: {pattern}")


# ==========================================
# 2. Agent 专用：验证器 (EXPLAIN)
# ==========================================

def execute_sql_explain(sql: str, trace_id: str = "N/A") -> bool:
    """
    【给 LangGraph Agent 使用】
    1. Python 正则预检 (无 IO 消耗)
    2. MySQL EXPLAIN (低 IO 消耗 + 超时保护)
    """
    # 🔥 1. 先跑轻量级预检，拦住大半恶意或错误的 SQL
    try:
        _security_precheck(sql)
    except ValueError as e:
        print(f"    ⚠️ [Executor][{trace_id}] Pre-check blocked: {e}")
        raise e  # 直接抛出，不连数据库

    # 🔥 2. 数据库连接层
    try:
        with mysql_conn() as conn:
            cur = conn.cursor()

            # 🛡️ 设置超时 (复用配置)，防止 EXPLAIN 卡死
            # 有些复杂的 VIEW 或海量 JOIN，EXPLAIN 也会很慢
            try:
                if hasattr(settings, "SQL_TIMEOUT_MS"):
                    cur.execute(f"SET SESSION MAX_EXECUTION_TIME={settings.SQL_TIMEOUT_MS}")
            except Exception:
                pass

            cur.execute(f"EXPLAIN {sql}")
            return True

    except Exception as e:
        print(f"    ❌ [Executor][{trace_id}] EXPLAIN Error: {str(e)[:100]}...")
        raise e


# ==========================================
# 3. API 专用：执行器 (SELECT)
# ==========================================

def execute_select(user_id: str, sql: str, trace_id: str = None) -> Dict[str, Any]:
    # ... (这部分保持上一步修改后的状态，记得带上 trace_id 和超时逻辑) ...
    if not trace_id:
        trace_id = str(uuid.uuid4())

    start = time.time()
    columns = []
    rows = []
    truncated = False
    err = None

    # 🔥 建议：正式执行前也跑一次预检，双重保险
    try:
        _security_precheck(sql)
    except ValueError as e:
        return {
            "trace_id": trace_id,
            "error": str(e),
            "rows": [],
            "latency_ms": 0
        }

    try:
        with mysql_conn() as conn:
            cur = conn.cursor()

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