import time
import json
import os
import uuid
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
    """处理无法直接 JSON 序列化的类型"""
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
    """写入审计日志 (events.jsonl)"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"❌ [Log Error] Failed to write event log: {e}")


# ==========================================
# 2. Agent 专用：验证器 (EXPLAIN)
# ==========================================

def execute_sql_explain(sql: str) -> bool:
    """
    【给 LangGraph Agent 使用】
    仅执行 EXPLAIN 验证 SQL 语法、表名、列名是否存在。
    不返回数据，不记录业务日志。
    如果 SQL 有错，直接抛出异常。
    """
    # 安全拦截：防止 Agent 生成非 SELECT 语句修改数据
    if not sql.strip().upper().startswith("SELECT"):
        raise ValueError("Safe mode: Only SELECT statements are allowed for verification.")

    try:
        with mysql_conn() as conn:
            cur = conn.cursor()
            # 执行 EXPLAIN，MySQL 会检查语法和元数据
            cur.execute(f"EXPLAIN {sql}")
            return True
    except Exception as e:
        # 将数据库原始报错抛出，供 Agent 进行“错误分类”和“反思”
        raise e


# ==========================================
# 3. API 专用：执行器 (SELECT)
# ==========================================

def execute_select(user_id: str, sql: str) -> Dict[str, Any]:
    """
    【给前端 API 使用】
    执行真实的 SELECT 查询，返回数据行，包含超时控制、截断和日志记录。
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

            # 1. 设置会话级超时 (防止慢 SQL 卡死 DB)
            # 注意: MAX_EXECUTION_TIME 单位是毫秒
            try:
                if hasattr(settings, "SQL_TIMEOUT_MS"):
                    cur.execute(f"SET SESSION MAX_EXECUTION_TIME={settings.SQL_TIMEOUT_MS}")
            except Exception:
                pass  # 部分 MySQL 版本可能不支持，忽略

            # 2. 执行查询
            cur.execute(sql)

            # 3. 获取列头
            if cur.description:
                columns = [d[0] for d in cur.description]

            # 4. 获取数据 (带截断保护)
            # 多取 1 行，用于判断是否超过最大行数限制
            limit_n = getattr(settings, "RESULT_MAX_ROWS", 1000)
            data = cur.fetchmany(limit_n + 1)

            if len(data) > limit_n:
                truncated = True
                data = data[:limit_n]

            # 5. 类型转换 (Decimal -> float, Date -> str)
            rows = []
            for r in data:
                # 这里假设 cursor 返回的是 tuple/list，如果 cursorclass 是 DictCursor，逻辑需微调
                # 为了通用性，这里处理 tuple 并结合 columns 转 dict (如果需要)
                # 你的原代码看似是处理 tuple，这里保持一致
                rows.append([_jsonable(x) for x in r])

    except Exception as e:
        err = str(e)[:300]  # 截断错误信息防止日志爆炸

    latency_ms = int((time.time() - start) * 1000)

    # 6. 记录审计日志
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