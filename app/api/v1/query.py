from fastapi import APIRouter
from pydantic import BaseModel

from app.modules.sql.guardrail import validate_and_rewrite
from app.modules.sql.executor import execute_select

router = APIRouter(prefix="/api/v1", tags=["query"])

class QueryReq(BaseModel):
    user_id: str
    sql: str

@router.post("/query")
def query(req: QueryReq):
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
