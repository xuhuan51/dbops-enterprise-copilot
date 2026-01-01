from fastapi import APIRouter
from pydantic import BaseModel

from app.modules.sql.guardrail import validate_and_rewrite
from app.modules.sql.executor import execute_select

router = APIRouter(prefix="/api/v1", tags=["analyze"])

class AnalyzeReq(BaseModel):
    user_id: str
    sql: str

@router.post("/analyze")
def analyze(req: AnalyzeReq):
    gr = validate_and_rewrite(req.sql)
    if not gr.ok:
        return {"error": f"GUARDRAIL_REJECT: {gr.reason}"}
    return {"data": execute_select(req.user_id, gr.rewritten_sql)}
