# app/graph/nodes/execution_node.py

import sqlite3
import re
import os
import logging
from typing import Dict, Any

from app.core.config import settings
from app.core.state import AgentState

# 设置日志
logger = logging.getLogger("execution_node")


def is_read_only_sql(sql: str) -> bool:
    """
    🛡️ 安全检查 (Soft Guard):
    通过正则确保 SQL 主要是 SELECT/WITH 查询，且不包含 DML/DDL 关键字。
    """
    if not sql:
        return False

    # 1. 移除注释 (防止 -- DELETE 绕过)
    # 移除 -- 注释 和 /* */ 注释
    sql_clean = re.sub(r'(--[^\n]*)|(/\*.*?\*/)', '', sql, flags=re.DOTALL).strip()

    # 2. 必须以安全关键字开头
    # BIRD 数据集主要是 SELECT 或 WITH (CTE)
    if not re.match(r'^(SELECT|WITH|PRAGMA|VALUES)', sql_clean, re.IGNORECASE):
        return False

    # 3. 黑名单关键字检查
    # 使用单词边界 \b 防止误杀列名 (例如 select_update_time 是合法的)
    forbidden_patterns = [
        r'\bINSERT\b', r'\bUPDATE\b', r'\bDELETE\b', r'\bDROP\b',
        r'\bALTER\b', r'\bCREATE\b', r'\bTRUNCATE\b', r'\bGRANT\b',
        r'\bREPLACE\b', r'\bATTACH\b'
    ]

    for pattern in forbidden_patterns:
        if re.search(pattern, sql_clean, re.IGNORECASE):
            logger.warning(f"🚫 SQL blocked by keyword check: {pattern}")
            return False

    return True


def execution_node(state: AgentState) -> Dict[str, Any]:
    """
    执行节点：连接 BIRD SQLite 数据库并执行 SQL。
    包含双重安全防御：正则检查 + 数据库只读模式。
    """
    logger.info("🚀 [Execution Node] Start...")

    db_id = state.get("db_id")
    raw_sql = state.get("generated_sql", "")

    # 1. SQL 清洗 (去除 Markdown 标记)
    sql = raw_sql.replace('```sql', '').replace('```', '').strip().rstrip(';')

    # -------------------------------------------------------
    # 🛡️ 防御层 1: 语法白名单检查
    # -------------------------------------------------------
    if not is_read_only_sql(sql):
        error_msg = "🚫 [Security Block] SQL blocked because it implies data modification or unsafe operations."
        logger.warning(f"{error_msg} SQL: {sql}")
        return {
            "execution_result": None,
            "execution_error": error_msg,
            "is_executable": False,
            "final_sql": sql
        }

    # 2. 路径构建
    # BIRD 结构: settings.BIRD_DB_ROOT / {db_id} / {db_id}.sqlite
    if not db_id:
        return {"execution_error": "Missing db_id in state", "is_executable": False}

    db_path = os.path.join(settings.BIRD_DB_ROOT, db_id, f"{db_id}.sqlite")

    if not os.path.exists(db_path):
        error_msg = f"❌ Database file not found at: {db_path}"
        logger.error(error_msg)
        return {
            "execution_result": None,
            "execution_error": error_msg,
            "is_executable": False,
            "final_sql": sql
        }

    conn = None
    result_data = None
    execution_error = None

    try:
        # -------------------------------------------------------
        # 🛡️ 防御层 2: 驱动级只读模式 (Hard Guard)
        # -------------------------------------------------------
        # 使用 URI 模式 ?mode=ro 强制只读
        # check_same_thread=False 允许 LangGraph 在不同线程中运行
        db_uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True, check_same_thread=False)

        cursor = conn.cursor()

        # 执行 SQL
        cursor.execute(sql)

        # 获取列名
        columns = [description[0] for description in cursor.description]

        # 获取数据 (限制行数，防止内存爆炸)
        rows = cursor.fetchmany(settings.RESULT_MAX_ROWS)

        # 转换为 List[Dict] 格式
        result_data = [dict(zip(columns, row)) for row in rows]

        row_count = len(result_data)
        logger.info(f"✅ [Execution] Success. Retrieved {row_count} rows.")

    except sqlite3.OperationalError as e:
        # 专门捕获只读模式拦截的写操作
        if "readonly" in str(e).lower():
            execution_error = "🔒 [Security Block] Database rejected write operation (ReadOnly Mode)."
        else:
            execution_error = f"SQLite Operational Error: {str(e)}"
        logger.error(f"❌ Execution Failed: {execution_error}")

    except Exception as e:
        execution_error = f"Execution Error: {str(e)}"
        logger.error(f"❌ Execution Failed: {execution_error}")

    finally:
        if conn:
            conn.close()

    # 3. 返回更新后的状态
    return {
        "execution_result": result_data,
        "execution_error": execution_error,
        "is_executable": execution_error is None,  # 没有报错才算 True
        "final_sql": sql  # 记录实际执行的 SQL (去除了 markdown 的)
    }