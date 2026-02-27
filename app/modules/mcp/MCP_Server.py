"""
═══════════════════════════════════════════════════════════════════════════════
📦 MCP Server: Text-to-SQL Agent
📝 说明:
   将原有的 LangGraph Text-to-SQL Agent 暴露为 MCP Server，
   任何支持 MCP 的客户端（Claude Desktop, Cursor, VS Code 等）都可以调用。

   提供的 Tools:
   1. query_database   — 自然语言查询，走完整 Agent 流程
   2. explain_sql      — 验证 SQL 语法
   3. get_table_schema — 获取表结构
   4. search_columns   — 按字段名反查表

   启动方式:
   - stdio 模式 (本地):  python mcp_server.py
   - SSE 模式 (远程):    python mcp_server.py --transport sse --port 8888
═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
)

# ── 你自己的模块 ──
from app.core.state import AgentState
from app.graph.graph import app as graph_app
from app.modules.sql.executor import (
    execute_select_async,
    execute_sql_explain,
    get_tables_columns,
    search_tables_by_column,
)

logger = logging.getLogger(__name__)

# ==========================================
# 1. 创建 MCP Server 实例
# ==========================================
server = Server("text2sql-agent")


# ==========================================
# 2. 注册 Tools 列表
# ==========================================
@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="query_database",
            description=(
                "用自然语言查询数据库。内部会自动完成：意图识别 → 表/列检索 → "
                "SQL 生成 → SQL 审计 → 执行 → 结果分析。"
                "返回生成的 SQL、查询结果和自然语言分析。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "用户的自然语言问题，例如：'上个月销售额最高的前10个商品是什么？'",
                    },
                    "db_id": {
                        "type": "string",
                        "description": "目标数据库 ID",
                        "default": "ecommerce",
                    },
                },
                "required": ["question"],
            },
        ),
        Tool(
            name="explain_sql",
            description="验证 SQL 语句的语法是否正确（执行 EXPLAIN），返回执行计划或错误信息。",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "要验证的 SQL 语句",
                    },
                },
                "required": ["sql"],
            },
        ),
        Tool(
            name="get_table_schema",
            description="获取指定表的结构信息（字段名、类型、注释）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要查询的表名列表，例如 ['orders', 'products']",
                    },
                },
                "required": ["table_names"],
            },
        ),
        Tool(
            name="search_columns",
            description="根据字段名关键字反查哪些表包含该字段。",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "字段名关键字，例如 'order_id'",
                    },
                },
                "required": ["keyword"],
            },
        ),
    ]


# ==========================================
# 3. 实现 Tool 调用逻辑
# ==========================================
@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """
    MCP 客户端调用 tool 时的入口
    """
    try:
        if name == "query_database":
            return await _handle_query(arguments)
        elif name == "explain_sql":
            return await _handle_explain(arguments)
        elif name == "get_table_schema":
            return await _handle_schema(arguments)
        elif name == "search_columns":
            return await _handle_search_columns(arguments)
        else:
            return [TextContent(type="text", text=f"未知工具: {name}")]
    except Exception as e:
        logger.error(f"Tool '{name}' error: {e}", exc_info=True)
        return [TextContent(type="text", text=f"❌ 执行出错: {str(e)}")]


# ─────────────────────────────────────────
# 3a. 核心：自然语言查询（跑完整 LangGraph）
# ─────────────────────────────────────────
async def _handle_query(arguments: dict) -> list[TextContent]:
    question = arguments["question"]
    db_id = arguments.get("db_id", "ecommerce")

    initial_state = AgentState(
        question=question,
        db_id=db_id,
        user_id="mcp_user",
        trace_id="mcp_session",
        history=[],
        retry_count=0,
        execution_retries=0,
    )

    # 收集各阶段的结果
    result_parts = []
    final_sql = ""
    final_answer = ""
    execution_result = None
    viz_config = None

    async for event in graph_app.astream(
        initial_state, config={"recursion_limit": 50}
    ):
        for node_name, updates in event.items():

            if node_name == "generate_node":
                sql = updates.get("generated_sql", "")
                if sql:
                    final_sql = sql

            elif node_name == "execution_node":
                error = updates.get("execution_error")
                if error:
                    result_parts.append(f"⚠️ SQL 执行报错: {error}")
                else:
                    execution_result = updates.get("execution_result")

            elif node_name == "analysis_node":
                final_answer = updates.get("final_answer", "")
                viz_config = updates.get("visualization_config")

    # ── 组装返回结果 ──
    contents = []

    # 1) SQL
    if final_sql:
        contents.append(TextContent(
            type="text",
            text=f"## 生成的 SQL\n```sql\n{final_sql}\n```",
        ))

    # 2) 查询结果（截取前 20 行避免太长）
    if execution_result:
        preview = execution_result[:20]
        contents.append(TextContent(
            type="text",
            text=f"## 查询结果（共 {len(execution_result)} 行，预览前 {len(preview)} 行）\n"
                 f"```json\n{json.dumps(preview, ensure_ascii=False, indent=2, default=str)}\n```",
        ))

    # 3) 自然语言分析
    if final_answer:
        contents.append(TextContent(
            type="text",
            text=f"## 分析\n{final_answer}",
        ))

    # 4) 可视化配置（如有）
    if viz_config:
        contents.append(TextContent(
            type="text",
            text=f"## 可视化配置\n```json\n{json.dumps(viz_config, ensure_ascii=False, indent=2, default=str)}\n```",
        ))

    # 5) 错误信息
    if result_parts:
        contents.append(TextContent(
            type="text",
            text="\n".join(result_parts),
        ))

    if not contents:
        contents.append(TextContent(type="text", text="未能生成结果，请换个问法试试。"))

    return contents


# ─────────────────────────────────────────
# 3b. SQL 语法验证
# ─────────────────────────────────────────
async def _handle_explain(arguments: dict) -> list[TextContent]:
    sql = arguments["sql"]
    try:
        plan = await execute_sql_explain(sql, trace_id="mcp_explain")
        return [TextContent(
            type="text",
            text=f"✅ SQL 语法正确\n\n执行计划:\n```json\n{json.dumps(plan, ensure_ascii=False, indent=2, default=str)}\n```",
        )]
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ SQL 语法错误: {str(e)}",
        )]


# ─────────────────────────────────────────
# 3c. 获取表结构
# ─────────────────────────────────────────
async def _handle_schema(arguments: dict) -> list[TextContent]:
    table_names = arguments["table_names"]
    schema = await get_tables_columns(table_names)

    lines = []
    for table, cols in schema.items():
        lines.append(f"### {table}")
        if cols:
            lines.append("| 字段 | 类型 | 注释 |")
            lines.append("|------|------|------|")
            for c in cols:
                lines.append(f"| {c['name']} | {c['type']} | {c.get('comment', '')} |")
        else:
            lines.append("（无数据或表不存在）")
        lines.append("")

    return [TextContent(type="text", text="\n".join(lines))]


# ─────────────────────────────────────────
# 3d. 字段反查表
# ─────────────────────────────────────────
async def _handle_search_columns(arguments: dict) -> list[TextContent]:
    keyword = arguments["keyword"]
    tables = await search_tables_by_column(keyword)

    if tables:
        text = f"包含 `{keyword}` 字段的表:\n" + "\n".join(f"- {t}" for t in tables)
    else:
        text = f"未找到包含 `{keyword}` 字段的表"

    return [TextContent(type="text", text=text)]


# ==========================================
# 4. 启动入口
# ==========================================
async def main():
    import sys

    # 支持 --transport sse --port 8888 参数
    if "--transport" in sys.argv and "sse" in sys.argv:
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.routing import Route
        import uvicorn

        port = 8888
        if "--port" in sys.argv:
            idx = sys.argv.index("--port")
            port = int(sys.argv[idx + 1])

        sse = SseServerTransport("/messages")

        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await server.run(
                    streams[0], streams[1], server.create_initialization_options()
                )

        starlette_app = Starlette(
            routes=[
                Route("/sse", endpoint=handle_sse),
                Route("/messages", endpoint=sse.handle_post_message, methods=["POST"]),
            ]
        )

        print(f"🚀 MCP Server (SSE) running on http://0.0.0.0:{port}")
        uvicorn.run(starlette_app, host="0.0.0.0", port=port)

    else:
        # 默认 stdio 模式
        print("🚀 MCP Server (stdio) starting...", file=__import__("sys").stderr)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )


if __name__ == "__main__":
    asyncio.run(main())