import json
import uuid
import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.agent_graph import app as agent_app
from app.modules.sql.executor import execute_select
from app.core.logger import logger  # 🔥 引入统一 Logger

router = APIRouter(tags=["Agent"])


class QueryRequest(BaseModel):
    query: str
    user_id: str = "sys_user"


@router.post("/query")
async def agent_query(req: QueryRequest):
    # 1. 生成全链路唯一 ID
    trace_id = str(uuid.uuid4())

    # 📝 结构化日志
    logger.info("Request received", extra={
        "trace_id": trace_id,
        "event": "request_start",
        "query": req.query,
        "user_id": req.user_id
    })

    try:
        # 🔥 核心修复 1: 改用 ainvoke (异步调用)，防止 LangGraph 内部同步操作阻塞主线程
        final_state = await agent_app.ainvoke({
            "question": req.query,
            "trace_id": trace_id,
            "retry_count": 0
        })

        intent = final_state.get("intent")

        if intent != "data_query":
            logger.info("Query blocked or non-data intent", extra={"trace_id": trace_id, "intent": intent})
            return {
                "trace_id": trace_id,
                "success": False,
                "type": intent,
                "message": "Guardrail blocked or non-data query."
            }

        error = final_state.get("validation_error")
        if error:
            logger.warning("Agent failed to generate valid SQL", extra={"trace_id": trace_id, "error": error})
            return {
                "trace_id": trace_id,
                "success": False,
                "error": f"Failed to generate valid SQL: {error}",
                "steps": final_state.get("retry_count", 0)
            }

        sql = final_state["generated_sql"]
        logger.info(f"Executing SQL: {sql}", extra={"trace_id": trace_id})

        # 🔥 核心修复 2: 将同步的 SQL 执行扔到线程池
        # 避免 execute_select (pymysql) 卡死 Event Loop
        loop = asyncio.get_running_loop()
        result_data = await loop.run_in_executor(
            None,
            lambda: execute_select(req.user_id, sql, trace_id=trace_id)
        )

        # 构造返回
        result_data["agent_meta"] = {
            "trace_id": trace_id,
            "confidence": final_state.get("sql_confidence"),
            "retries": final_state.get("retry_count"),
            "retrieved_context": [t['logical_table'] for t in final_state.get('candidate_tables', [])],
            "tables_used": final_state.get("tables_used", []),
            "assumptions": final_state.get("assumptions", [])
        }

        logger.info("Request finished successfully", extra={"trace_id": trace_id})
        return result_data

    except Exception as e:
        logger.error("Internal Error", extra={"trace_id": trace_id}, exc_info=True)
        raise HTTPException(status_code=500, detail=f"[{trace_id}] Internal Error: {str(e)}")