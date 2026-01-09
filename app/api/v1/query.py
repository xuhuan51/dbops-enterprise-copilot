from fastapi import APIRouter
from pydantic import BaseModel

from app.modules.sql.guardrail import validate_and_rewrite
from app.modules.sql.executor import execute_select

# 🔥 修改 1: 去掉 prefix，防止路径叠加混乱
# (我们会在 main.py 里统一加 /api/v1)
router = APIRouter(tags=["Raw SQL Executor"])

class QueryReq(BaseModel):
    user_id: str
    sql: str

# 🔥 修改 2: 核心解决！把路径从 /query 改成 /execute_sql
# 这样它就变成了 http://localhost:8000/api/v1/execute_sql
# 彻底把 /api/v1/query 让给 AI Agent 用
@router.post("/execute_sql")
def execute_sql_endpoint(req: QueryReq):
    """
    直接执行 SQL 语句 (仅供调试或后台使用)
    """
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
    return execute_select(req.user_id, gr.rewritten_sql)