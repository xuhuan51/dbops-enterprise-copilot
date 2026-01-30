import time
from fastapi import APIRouter, HTTPException, BackgroundTasks
from app.services.agent_service import AgentService
from app.schemas.response import StandardResponse  # 假设你定义在这里
from app.core.logger import logger

router = APIRouter()
agent_service = AgentService()


@router.post("/query", response_model=StandardResponse)
async def query_agent(payload: dict):
    start_ts = time.time()
    user_id = payload.get("user_id", "anonymous")
    query = payload.get("query", "")
    session_id = payload.get("session_id")

    # 结果容器
    response_payload = {
        "success": False,
        "message": "",
        "data": [],
        "meta": {}
    }

    try:
        # 1. 调用业务逻辑
        # Service 层返回的通常是 dict: {"message": "...", "data": [...], "sql": "...", "trace_id": "..."}
        result = await agent_service.process_query(query, user_id, session_id)

        # 2. 映射字段 (Mapping)
        # 无论 Service 返回什么，这里负责转换成标准格式
        response_payload["success"] = result.get("success", True)  # Service 可能显式返回 False
        response_payload["message"] = result.get("message", "")  # Analyst 的话
        response_payload["data"] = result.get("data", [])  # 表格数据

        # 3. 组装元数据 (Meta) - 给前端 Debug 或展示 SQL 用
        response_payload["meta"] = {
            "trace_id": result.get("trace_id"),
            "intent": result.get("intent", "UNKNOWN"),
            "sql": result.get("sql"),  # 前端可能想展示生成的 SQL
            "steps": result.get("steps", []),  # 如果前端要画流程图
            "duration": round(time.time() - start_ts, 2)
        }

        # 特殊处理：如果 Service 返回了 error 字段，视为业务失败
        if result.get("error"):
            response_payload["success"] = False
            # 如果 message 是空的，把 error 填进去
            if not response_payload["message"]:
                response_payload["message"] = f"查询处理异常: {result.get('error')}"

    except Exception as e:
        # 4. 兜底异常捕获 (Catch-All)
        # 就算 Service 炸了，接口也不能炸，要优雅地返回 JSON
        logger.error(f"API Endpoint Error: {str(e)}", exc_info=True)
        response_payload["success"] = False
        response_payload["message"] = f"系统内部错误: {str(e)}"
        response_payload["meta"]["duration"] = round(time.time() - start_ts, 2)
        # 这里可以选择是否返回 500 状态码，或者保持 200 但 success=False
        # 通常建议保持 200，让前端根据 success 字段判断

    return response_payload