import uuid
import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

# 🔥 核心修改: 导入整个模块，而不是 import master_app
# 这样能确保我们用到的是 main.py 初始化后的最新对象
import app.core.master_graph as mg

from app.modules.sql.executor import execute_select
from app.core.logger import logger

router = APIRouter(tags=["Agent"])

class QueryRequest(BaseModel):
    query: str
    user_id: str = "sys_user"
    session_id: Optional[str] = None



@router.post("/query")
async def agent_query(req: QueryRequest):
    trace_id = str(uuid.uuid4())
    thread_id = req.session_id or str(uuid.uuid4())

    # ... (日志代码不变)

    try:
        config = {"configurable": {"thread_id": thread_id}}

        # 调用 Master Graph
        final_state = await mg.master_app.ainvoke(
            {"question": req.query, "trace_id": trace_id},
            config=config
        )

        final_answer = final_state.get("final_answer", "")
        # 🔥 获取思考步骤 (History)
        steps = final_state.get("history", [])

        # =================================================
        # 分支 A: SQL 任务
        # =================================================
        if final_answer.startswith("SQL_RESULT:"):
            sql = final_answer.replace("SQL_RESULT:", "").strip()

            # 执行 SQL
            loop = asyncio.get_running_loop()
            result_data = await loop.run_in_executor(
                None,
                lambda: execute_select(req.user_id, sql, trace_id=trace_id)
            )

            result_data["agent_meta"] = {
                "trace_id": trace_id,
                "session_id": thread_id,
                "intent": "DATA_QUERY",
                "tables_used": final_state.get("tables_used", []),
                "steps": steps  # 🔥🔥🔥 核心修改：把步骤返回给客户端
            }
            result_data["session_id"] = thread_id
            return result_data

        # =================================================
        # 分支 B: 文本任务
        # =================================================
        else:
            return {
                "trace_id": trace_id,
                "session_id": thread_id,
                "success": True,
                "type": "text",
                "intent": final_state.get("intent", "UNKNOWN"),
                "message": final_answer,
                "steps": steps  # 🔥🔥🔥 核心修改：把步骤返回给客户端
            }

    except Exception as e:
        logger.error("Internal Error", extra={"trace_id": trace_id}, exc_info=True)
        # 🔥 为了调试方便，把报错详情直接返回
        raise HTTPException(status_code=500, detail=str(e))
