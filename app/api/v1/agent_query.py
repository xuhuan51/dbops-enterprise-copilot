# app/api/v1/agent_query.py
import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.agent_graph import app as agent_app  # 引用我们的大杀器
from app.modules.sql.executor import execute_select

router = APIRouter(tags=["Agent"])


class QueryRequest(BaseModel):
    query: str
    user_id: str = "sys_user"


@router.post("/query")
async def agent_query(req: QueryRequest):
    """
    Agentic Text-to-SQL 入口
    流程: Intent -> Retrieve -> Rerank -> Generate -> Validate -> Repair -> Execution
    """
    trace_id = str(uuid.uuid4())
    print(f"🚀 [API] New Request {trace_id}: {req.query}")

    try:
        # 1. 调用 LangGraph (同步调用，如果耗时久可改为 invoke_async)
        # 输入: {"question": ...}
        # 输出: Final State
        final_state = agent_app.invoke({"question": req.query})

        # 2. 检查结果状态
        intent = final_state.get("intent")

        # A. 非数据查询 / 敏感查询
        if intent != "data_query":
            return {
                "trace_id": trace_id,
                "success": False,
                "type": intent,
                "message": "Guardrail blocked or non-data query."
            }

        # B. SQL 生成失败 (重试耗尽或不可修复)
        error = final_state.get("validation_error")
        if error:
            return {
                "trace_id": trace_id,
                "success": False,
                "error": f"Failed to generate valid SQL: {error}",
                "steps": final_state.get("retry_count", 0)
            }

        # C. 成功生成 SQL -> 执行真实查询
        sql = final_state["generated_sql"]
        print(f"🔍 [API] Executing SQL: {sql}")

        result_data = execute_select(req.user_id, sql)

        # 把 Agent 的思考过程也返回给前端 (可选)
        result_data["agent_meta"] = {
            "confidence": final_state.get("sql_confidence"),
            "retries": final_state.get("retry_count"),
            "tables_used": [t['logical_table'] for t in final_state.get('candidate_tables', [])]
        }

        return result_data

    except Exception as e:
        print(f"❌ [API] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))