from fastapi import APIRouter
from pydantic import BaseModel

from app.modules.sql.guardrail import validate_and_rewrite
from app.modules.sql.executor import execute_select

router = APIRouter(tags=["Raw SQL Executor"])


class RawSqlRequest(BaseModel):
    user_id: str
    sql: str


# 路径明确改为 /execute_sql，避免和 /query 冲突
@router.post("/execute_sql")
def execute_raw_sql_endpoint(req: RawSqlRequest):
    """
    调试专用接口：直接执行 SQL 语句 (带安全检查)
    """
    # 1. 安全检查
    gr = validate_and_rewrite(req.sql)
    if not gr.ok:
        return {
            "trace_id": None,
            "columns": [],
            "rows": [],
            "truncated": False,
            "latency_ms": 0,
            "error": f"GUARDRAIL_REJECT: {gr.reason}",
        }

    # 2. 直接执行 (FastAPI 会自动把同步函数放到线程池跑，不会卡死)
    return execute_select(req.user_id, gr.rewritten_sql)