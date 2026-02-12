import json
import asyncio
from typing import AsyncGenerator
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.logger import logger
from app.core.state import AgentState
# 引入我们在 graph.py 中编译好的图实例
from app.graph.graph import app as graph_app

router = APIRouter()


# ==========================================
# 1. 请求体定义
# ==========================================
class ChatRequest(BaseModel):
    query: str = Field(..., description="用户的问题")
    db_id: str = Field(..., description="目标数据库ID (e.g., 'california_schools')")
    session_id: str = Field(default="default_session", description="会话ID")
    user_id: str = Field(default="user", description="用户ID")


# ==========================================
# 2. 流式响应生成器 (核心逻辑)
# ==========================================
async def agent_stream(request: ChatRequest) -> AsyncGenerator[str, None]:
    """
    执行 LangGraph 并实时推送 NDJSON (Newline Delimited JSON) 格式的日志
    """

    # 1. 初始化状态
    # 注意：这里对应 app/core/state.py 中的 AgentState 定义
    initial_state = {
        "question": request.query,
        "db_id": request.db_id,
        "trace_id": request.session_id,
        "history": [],  # 暂时为空，后续可对接 memory
        "retry_count": 0,
        "execution_retries": 0
    }

    try:
        # 2. 启动图的流式执行
        # stream_mode="updates" 表示每当一个节点执行完，就返回该节点的输出增量
        async for event in graph_app.astream(initial_state, config={"recursion_limit": 50}):

            # event 格式通常是: {'node_name': {'updated_key': 'value', ...}}
            for node_name, updates in event.items():

                # --- A. 路由节点 (Router) ---
                if node_name == "router_node":
                    intent_data = updates.get("intent_data", {})
                    intent = updates.get("intent", "UNKNOWN")
                    yield json.dumps({
                        "type": "log",
                        "step": "INTENT",
                        "msg": f"识别用户意图: {intent}",
                        "details": f"Reason: {getattr(intent_data, 'reason', 'N/A')}"
                    }, ensure_ascii=False) + "\n"

                # --- B. 关键词扩展 (Expand) ---
                elif node_name == "expand_node":
                    # 虽然前端没怎么展示这个，但可以作为日志输出
                    yield json.dumps({
                        "type": "log",
                        "step": "EXPAND",
                        "msg": "语义关键词提取完成",
                        "details": "Keywords extracted for retrieval."
                    }, ensure_ascii=False) + "\n"

                # --- C. 检索节点 (Retrieval) ---
                elif node_name == "retrieval_node":
                    tables = updates.get("retrieved_tables", [])
                    rules = updates.get("business_rules", [])

                    details_str = f"Found Tables: {tables}\n"
                    if rules:
                        details_str += f"Business Rules: {len(rules)} items loaded."

                    yield json.dumps({
                        "type": "log",
                        "step": "RETRIEVAL",
                        "msg": f"RAG 混合检索完成 (命中 {len(tables)} 表)",
                        "details": details_str
                    }, ensure_ascii=False) + "\n"

                # --- D. 生成节点 (Generate) ---
                elif node_name == "generate_node":
                    sql = updates.get("generated_sql", "")
                    yield json.dumps({
                        "type": "log",
                        "step": "DRAFT",
                        "msg": "生成初始 SQL 逻辑...",
                        "details": f"```sql\n{sql}\n```"
                    }, ensure_ascii=False) + "\n"

                # --- E. 验证节点 (Verification) ---
                elif node_name == "verification_node":
                    verified = updates.get("verified", False)
                    feedback = updates.get("feedback", "")

                    if verified:
                        msg = "✅ SQL 安全审计通过"
                        details = "Syntax Check: PASS\nPermission Check: PASS"
                    else:
                        msg = "❌ SQL 审计未通过 (准备重试)"
                        details = f"Feedback: {feedback}"

                    yield json.dumps({
                        "type": "log",
                        "step": "VERIFY",
                        "msg": msg,
                        "details": details
                    }, ensure_ascii=False) + "\n"

                # --- F. 执行节点 (Execution) ---
                elif node_name == "execution_node":
                    # 注意：Execution 节点可能是最后一步，但也可能触发重试
                    # 如果有 error，说明执行失败
                    error = updates.get("execution_error")
                    result = updates.get("execution_result")

                    if error:
                        yield json.dumps({
                            "type": "log",
                            "step": "EXECUTE",
                            "msg": "❌ SQL 执行报错 (自愈机制启动)",
                            "details": f"Error: {error}"
                        }, ensure_ascii=False) + "\n"

                    if result:
                        yield json.dumps({
                            "type": "log",
                            "step": "EXECUTE",
                            "msg": "🚀 SQL 执行成功",
                            "details": f"Rows fetched: {len(result)}"
                        }, ensure_ascii=False) + "\n"

                    # 无论成功失败，最后的 SQL 都在这里
                    final_sql = updates.get("final_sql")

                    # 如果执行成功，这是整个流的终点，发送最终结果 Result
                    if updates.get("is_executable"):
                        yield json.dumps({
                            "type": "result",
                            "status": "success",
                            "msg": "查询已完成。",
                            "data": result,
                            "sql": final_sql
                        }, ensure_ascii=False) + "\n"

    except Exception as e:
        logger.error(f"Agent Stream Error: {e}", exc_info=True)
        yield json.dumps({
            "type": "error",
            "msg": f"Internal Server Error: {str(e)}"
        }, ensure_ascii=False) + "\n"


# ==========================================
# 3. 路由定义
# ==========================================
@router.post("/query")
async def chat_endpoint(request: ChatRequest):
    """
    前端调用的主入口
    """
    logger.info(f"📨 Received query: {request.query} [db={request.db_id}]")

    return StreamingResponse(
        agent_stream(request),
        media_type="application/x-ndjson"
    )