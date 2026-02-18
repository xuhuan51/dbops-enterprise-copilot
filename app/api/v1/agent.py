import json
import logging
from typing import AsyncGenerator
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.state import AgentState
# 确保这里引入的是最新的 graph 实例
from app.graph.graph import app as graph_app

logger = logging.getLogger(__name__)

router = APIRouter()


# ==========================================
# 1. 请求体定义
# ==========================================
class ChatRequest(BaseModel):
    query: str = Field(..., description="用户的问题")
    db_id: str = Field(default="ecommerce", description="目标数据库ID")
    session_id: str = Field(default="default_session", description="会话ID")
    user_id: str = Field(default="test_user", description="用户ID")


# ==========================================
# 2. 流式响应生成器 (核心逻辑)
# ==========================================
async def agent_stream(request: ChatRequest) -> AsyncGenerator[str, None]:
    """
    执行 LangGraph 并实时推送 NDJSON 日志
    格式: {"type": "log"|"sql"|"data"|"answer"|"chart", "step": "...", "payload": ...}
    """

    # 1. 初始化状态
    initial_state = AgentState(
        question=request.query,
        db_id=request.db_id,
        user_id=request.user_id,
        trace_id=request.session_id,
        history=[],
        retry_count=0,
        execution_retries=0
    )

    try:
        # 2. 启动图的流式执行
        # stream_mode="updates" 表示每当一个节点执行完，就返回该节点的输出增量
        async for event in graph_app.astream(initial_state, config={"recursion_limit": 50}):

            for node_name, updates in event.items():

                # --- A. 意图路由 (Router) ---
                if node_name == "router_node":
                    intent_data = updates.get("intent_data", {})
                    # 兼容 Pydantic v1/v2 或 dict
                    intent = getattr(intent_data, "intent", "UNKNOWN") if hasattr(intent_data,
                                                                                  "intent") else intent_data.get(
                        "intent")

                    yield _format_event("log", "ROUTER", f"识别意图: {intent}")

                # --- B. 检索 (Retrieval) ---
                elif node_name == "retrieval_node":
                    # 注意：retrieved_columns 具体结构取决于你的实现，这里做个兜底
                    cols = updates.get("retrieved_columns", [])
                    count = len(cols) if cols else 0
                    yield _format_event("log", "RETRIEVAL", f"已检索 Schema 信息，命中 {count} 个字段")

                # --- C. 选列 (Column Selector) ---
                elif node_name == "column_selector_node":
                    selected = updates.get("selected_schema", {})
                    table_names = list(selected.keys())
                    yield _format_event("log", "SELECTOR", f"AI 精选表范围: {table_names}")

                # --- D. 生成 SQL (Generate) ---
                elif node_name == "generate_node":
                    sql = updates.get("generated_sql", "")
                    # 专门推一个 type="sql" 给前端渲染代码块
                    yield _format_event("sql", "GENERATE", "SQL 生成完毕", payload=sql)

                # --- E. 验证 (Verify) ---
                elif node_name == "verification_node":
                    verified = updates.get("verified", False)
                    if verified:
                        yield _format_event("log", "VERIFY", "✅ SQL 安全审计通过")
                    else:
                        feedback = updates.get("feedback", "")
                        yield _format_event("log", "VERIFY", f"❌ 审计驳回: {feedback} (正在重试...)")

                # --- F. 执行 (Execution) ---
                elif node_name == "execution_node":
                    error = updates.get("execution_error")
                    result = updates.get("execution_result")

                    if error:
                        yield _format_event("log", "EXECUTE", f"⚠️ SQL 执行报错: {error}")
                    else:
                        row_count = len(result) if result else 0
                        # 这里可以推一个 "data" 事件给前端渲染表格
                        yield _format_event("data", "EXECUTE", f"查询成功，获取 {row_count} 行数据", payload=result)

                # --- G. 分析 (Analysis) - 🏁 最终环节 ---
                elif node_name == "analysis_node":
                    final_answer = updates.get("final_answer", "")
                    viz_config = updates.get("visualization_config")

                    # 1. 推送自然语言回答
                    yield _format_event("answer", "ANALYSIS", "分析完成", payload=final_answer)

                    # 2. 如果有图表，推送图表配置
                    if viz_config:
                        yield _format_event("chart", "ANALYSIS", "生成可视化图表", payload=viz_config)

    except Exception as e:
        logger.error(f"Stream Error: {e}", exc_info=True)
        yield _format_event("error", "SYSTEM", f"系统内部错误: {str(e)}")


def _format_event(type_str: str, step: str, msg: str, payload: any = None) -> str:
    """辅助函数：格式化 NDJSON 行"""
    data = {
        "type": type_str,  # 事件类型: log, sql, data, answer, chart, error
        "step": step,  # 当前步骤
        "msg": msg,  # 简短描述
        "payload": payload  # 详细数据 (SQL代码, JSON数据, 图表配置等)
    }
    return json.dumps(data, ensure_ascii=False) + "\n"


# ==========================================
# 3. 注册路由
# ==========================================
@router.post("/query")
async def chat_endpoint(request: ChatRequest):
    logger.info(f"📨 Query: {request.query}")
    return StreamingResponse(
        agent_stream(request),
        media_type="application/x-ndjson"
    )