"""
═══════════════════════════════════════════════════════════════════════════════
📦 模块名称: agent.py (v2 - 丰富流式事件)
📝 改动说明:
   1. 每个节点推送 "开始" 和 "结束" 两个事件，前端不再出现空档
   2. column_selector_node 推送精选的表、列、Join、规则详情
   3. generate_node 区分首次生成和修复模式
   4. verification_node 推送审计中/通过/驳回三个状态
   5. 统一使用 step 字段标识阶段
═══════════════════════════════════════════════════════════════════════════════
"""

import json
import logging
from typing import AsyncGenerator
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.state import AgentState
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
# 2. 辅助：格式化精选上下文
# ==========================================
def _format_selected_context(selected_schema: dict, join_paths: list, business_rules: list, value_mappings: list) -> dict:
    """
    将 column_selector_node 的输出打包成前端能直接展示的结构
    """
    tables = {}
    if selected_schema:
        for table_name, table_data in selected_schema.items():
            cols = []
            for col in table_data.get("columns", []):
                col_name = col.get("column_name") or col.get("name", "")
                if col_name:
                    cols.append(col_name)
            tables[table_name] = cols

    joins = []
    if join_paths:
        for jp in join_paths:
            if isinstance(jp, str):
                joins.append(jp)
            elif isinstance(jp, dict):
                joins.append(str(jp))

    rules = []
    if business_rules:
        for r in business_rules:
            if isinstance(r, dict):
                content = r.get("content", "")
                if content:
                    rules.append(content)
            elif isinstance(r, str) and r.strip():
                rules.append(r.strip())

    values = []
    if value_mappings:
        for vm in value_mappings:
            if isinstance(vm, dict):
                values.append({
                    "user_input": vm.get("user_input", ""),
                    "db_value": vm.get("db_value", ""),
                    "table": vm.get("table", ""),
                    "column": vm.get("column", ""),
                })

    return {
        "tables": tables,
        "joins": joins,
        "rules": rules,
        "values": values,
    }


# ==========================================
# 3. 流式响应生成器 (核心逻辑)
# ==========================================
async def agent_stream(request: ChatRequest) -> AsyncGenerator[str, None]:
    """
    执行 LangGraph 并实时推送 NDJSON 事件

    事件格式:
    {"type": "log"|"sql"|"data"|"answer"|"chart"|"context"|"error",
     "step": "ROUTER"|"EXPAND"|"RETRIEVAL"|"SELECTOR"|"GENERATE"|"VERIFY"|"EXECUTE"|"ANALYSIS",
     "msg": "...",
     "payload": ...}
    """

    initial_state = AgentState(
        question=request.query,
        db_id=request.db_id,
        user_id=request.user_id,
        trace_id=request.session_id,
        history=[],
        retry_count=0,
        execution_retries=0
    )

    # 追踪状态
    is_repairing = False
    repair_count = 0
    last_sql = ""

    try:
        async for event in graph_app.astream(initial_state, config={"recursion_limit": 50}):

            for node_name, updates in event.items():

                # ─────────────────────────────────────────
                # A. 意图路由 (Router)
                # ─────────────────────────────────────────
                if node_name == "router_node":
                    intent_data = updates.get("intent_data", {})
                    intent = getattr(intent_data, "intent", "UNKNOWN") if hasattr(intent_data, "intent") else intent_data.get("intent", "UNKNOWN")
                    yield _evt("log", "ROUTER", f"识别意图: {intent}")
                    # 推送"正在检索"提示
                    yield _evt("log", "RETRIEVAL_START", "正在检索相关列和业务知识...")

                # ─────────────────────────────────────────
                # A2. 扩展节点 (Expand) - 静默，不推前端
                # ─────────────────────────────────────────
                elif node_name == "expand_node":
                    pass  # 扩展节点是内部处理，不需要给用户展示

                # ─────────────────────────────────────────
                # B. 检索完成 (Retrieval)
                # ─────────────────────────────────────────
                elif node_name == "retrieval_node":
                    retrieved_schema = updates.get("retrieved_schema", {})
                    rules = updates.get("business_rules", [])
                    total_tables = len(retrieved_schema)
                    total_cols = sum(len(t.get("columns", [])) for t in retrieved_schema.values())
                    yield _evt("log", "RETRIEVAL_DONE", f"检索完成：扫描了 {total_tables} 张表、{total_cols} 个候选列")
                    # 推送"正在精选"提示
                    yield _evt("log", "SELECTOR_START", "AI 正在精选相关表、列和业务规则...")

                # ─────────────────────────────────────────
                # C. 选列完成 (Column Selector)
                # ─────────────────────────────────────────
                elif node_name == "column_selector_node":
                    selected_schema = updates.get("selected_schema", {})
                    join_paths = updates.get("join_paths", [])
                    business_rules = updates.get("business_rules", [])
                    value_mappings = updates.get("value_mappings", [])

                    # 打包精选上下文详情
                    context_detail = _format_selected_context(
                        selected_schema, join_paths, business_rules, value_mappings
                    )
                    table_names = list(selected_schema.keys()) if selected_schema else []
                    total_cols = sum(len(v) for v in context_detail["tables"].values())

                    yield _evt("context", "SELECTOR_DONE",
                               f"精选完成：{len(table_names)} 张表、{total_cols} 个列",
                               payload=context_detail)

                    # 推送"正在生成"提示
                    yield _evt("log", "GENERATE_START", "正在生成 SQL...")

                # ─────────────────────────────────────────
                # D. 生成 SQL (Generate)
                # ─────────────────────────────────────────
                elif node_name == "generate_node":
                    sql = updates.get("generated_sql", "")
                    if sql:
                        last_sql = sql
                        if is_repairing:
                            yield _evt("sql", "GENERATE_REPAIRED",
                                       f"SQL 修正完成（第 {repair_count} 次）",
                                       payload=sql)
                        else:
                            yield _evt("sql", "GENERATE_DONE", "SQL 生成完毕",
                                       payload=sql)
                    # 推送"审计中"提示
                    yield _evt("log", "VERIFY_START", "SQL 审计中...")

                # ─────────────────────────────────────────
                # E. 验证 (Verify)
                # ─────────────────────────────────────────
                elif node_name == "verification_node":
                    verified = updates.get("verified", False)
                    if verified:
                        is_repairing = False
                        yield _evt("log", "VERIFY_PASS", "SQL 审计通过")
                        yield _evt("log", "EXECUTE_START", "正在执行查询...")
                    else:
                        is_repairing = True
                        repair_count += 1
                        feedback = updates.get("feedback", "")
                        yield _evt("log", "VERIFY_REJECT",
                                   f"审计驳回: {feedback}",
                                   payload={"feedback": feedback, "attempt": repair_count})
                        yield _evt("log", "GENERATE_REPAIR_START",
                                   f"正在修正 SQL（第 {repair_count} 次）...")

                # ─────────────────────────────────────────
                # F. 执行 (Execution)
                # ─────────────────────────────────────────
                elif node_name == "execution_node":
                    error = updates.get("execution_error")
                    result = updates.get("execution_result")

                    if error:
                        yield _evt("log", "EXECUTE_ERROR", f"SQL 执行报错: {error}")
                    else:
                        row_count = len(result) if result else 0
                        yield _evt("data", "EXECUTE_DONE",
                                   f"查询成功，获取 {row_count} 行数据",
                                   payload=result)

                # ─────────────────────────────────────────
                # G. 分析 (Analysis) - 最终环节
                # ─────────────────────────────────────────
                elif node_name == "analysis_node":
                    final_answer = updates.get("final_answer", "")
                    viz_config = updates.get("visualization_config")

                    yield _evt("answer", "ANALYSIS", "分析完成",
                               payload=final_answer)

                    if viz_config:
                        yield _evt("chart", "ANALYSIS", "生成可视化图表",
                                   payload=viz_config)

    except Exception as e:
        logger.error(f"Stream Error: {e}", exc_info=True)
        yield _evt("error", "SYSTEM", f"系统内部错误: {str(e)}")


def _evt(type_str: str, step: str, msg: str, payload=None) -> str:
    """格式化 NDJSON 事件行"""
    from decimal import Decimal

    data = {
        "type": type_str,
        "step": step,
        "msg": msg,
    }
    if payload is not None:
        data["payload"] = payload
    return json.dumps(data, ensure_ascii=False, default=_json_default) + "\n"


def _json_default(obj):
    """JSON 序列化兜底：处理 Decimal、datetime 等非标准类型"""
    from decimal import Decimal
    from datetime import datetime, date
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


# ==========================================
# 4. 注册路由
# ==========================================
@router.post("/query")
async def chat_endpoint(request: ChatRequest):
    logger.info(f"📨 Query: {request.query}")
    return StreamingResponse(
        agent_stream(request),
        media_type="application/x-ndjson"
    )