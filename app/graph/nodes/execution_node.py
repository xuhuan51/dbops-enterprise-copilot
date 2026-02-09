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

# 🔥 定义执行节点的最大重试次数 (与反思节点的重试区分开)
MAX_EXECUTION_RETRIES = 1


def is_read_only_sql(sql: str) -> bool:
    """
    🛡️ 安全检查 (Soft Guard):
    通过正则确保 SQL 主要是 SELECT/WITH 查询，且不包含 DML/DDL 关键字。
    """
    if not sql:
        return False

    # 1. 移除注释
    sql_clean = re.sub(r'(--[^\n]*)|(/\*.*?\*/)', '', sql, flags=re.DOTALL).strip()

    # 2. 必须以安全关键字开头
    if not re.match(r'^(SELECT|WITH|PRAGMA|VALUES)', sql_clean, re.IGNORECASE):
        return False

    # 3. 黑名单关键字检查
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
    包含双重安全防御 + 独立的重试计数器控制。
    """
    logger.info("🚀 [Execution Node] Start...")

    db_id = state.get("db_id")
    raw_sql = state.get("generated_sql", "")

    # 🔥 获取当前的执行重试次数 (默认为 0)
    current_retries = state.get("execution_retries", 0)

    # 1. SQL 清洗
    sql = raw_sql.replace('```sql', '').replace('```', '').strip().rstrip(';')

    # 定义统一的返回结构 helper
    def build_response(result=None, error=None, executable=False, retries=0):
        return {
            "execution_result": result,
            "execution_error": error,
            "is_executable": executable,
            "final_sql": sql,
            "execution_retries": retries  # ✅ 更新状态中的计数器
        }

    # -------------------------------------------------------
    # 🛡️ 防御层 1: 语法白名单检查
    # -------------------------------------------------------
    if not is_read_only_sql(sql):
        error_msg = "🚫 [Security Block] SQL blocked because it implies data modification or unsafe operations."
        logger.warning(f"{error_msg} SQL: {sql}")
        # 安全拦截视为不可恢复错误，不建议重试，或者由 Router 决定
        return build_response(error=error_msg, executable=False, retries=current_retries)

    # 2. 路径构建
    if not db_id:
        return build_response(error="Missing db_id in state", executable=False, retries=current_retries)

    db_path = os.path.join(settings.BIRD_DB_ROOT, db_id, f"{db_id}.sqlite")

    if not os.path.exists(db_path):
        error_msg = f"❌ Database file not found at: {db_path}"
        logger.error(error_msg)
        return build_response(error=error_msg, executable=False, retries=current_retries)

    conn = None
    execution_error = None
    result_data = None

    try:
        # -------------------------------------------------------
        # 🛡️ 防御层 2: 驱动级只读模式 (Hard Guard)
        # -------------------------------------------------------
        db_uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True, check_same_thread=False)
        cursor = conn.cursor()

        # 执行 SQL
        cursor.execute(sql)

        # 获取列名和数据
        if cursor.description:
            columns = [description[0] for description in cursor.description]
            rows = cursor.fetchmany(settings.RESULT_MAX_ROWS)
            result_data = [dict(zip(columns, row)) for row in rows]
        else:
            result_data = []  # 处理非查询语句（虽然被过滤了，防万一）

        row_count = len(result_data)
        logger.info(f"✅ [Execution] Success. Retrieved {row_count} rows.")

        # ✅ 成功：重置重试计数器为 0
        return build_response(result=result_data, executable=True, retries=0)

    except sqlite3.OperationalError as e:
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

    # =======================================================
    # 🔥 失败处理逻辑：判断是否允许重试
    # =======================================================

    # 计数器 +1
    next_retries = current_retries + 1

    if next_retries > MAX_EXECUTION_RETRIES:
        logger.error(f"🛑 [Execution] Max retries ({MAX_EXECUTION_RETRIES}) reached. No more repairs.")
        # 这里你可以选择给 error 加个前缀，让 Router 识别这是终极失败
        # 或者 Router 只需要检查 state['execution_retries'] > 1 即可决定是否结束
    else:
        logger.warning(f"🔄 [Execution] Failed. Attempt {next_retries}/{MAX_EXECUTION_RETRIES}. Triggering Repair...")

    return build_response(error=execution_error, executable=False, retries=next_retries)