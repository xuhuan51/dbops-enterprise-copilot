# app/graph/nodes/analysis_node.py
"""
═══════════════════════════════════════════════════════════════════════════════
📦 模块名称: analysis_node.py (v3 - 数据与决策分离)
📝 核心改动:
   1. 修正模板参数: total_rows / preview_count
   2. LLM 只返回 chart_type + x_field + y_field，不再填 x_axis_data / series_data
   3. 代码从全量 execution_result 提取图表数据，保持 SQL 原始排序
   4. Prompt 仍然放在 app/core/prompts.py，这里只 import
═══════════════════════════════════════════════════════════════════════════════
"""

import json
import logging
from typing import Dict, Any, List, Optional

from langchain_core.messages import SystemMessage, HumanMessage

from app.core.prompts import ANALYSIS_SYSTEM_PROMPT, ANALYSIS_USER_TEMPLATE
from app.core.state import AgentState
from app.core.llm import get_llm

logger = logging.getLogger(__name__)


# ==========================================
# 1. 从全量数据构建图表（保持 SQL 排序）
# ==========================================

def _build_viz_from_real_data(
        analysis_data: dict,
        full_results: List[dict],
        max_chart_items: int = 50
) -> Optional[dict]:
    """
    LLM 决定图表类型和字段映射，代码从全量数据填充。

    支持两种 LLM 返回格式：
    A) 新版 (推荐): chart_config.x_field / y_field
    B) 旧版 (兼容): chart_data.x_axis_data / series_data
    """
    if not analysis_data.get("show_chart") or not full_results:
        logger.info(f"📊 [Chart] Skipped. show_chart={analysis_data.get('show_chart')}, results={len(full_results)}")
        return None

    chart_type = analysis_data.get("chart_type", "bar")

    # ── 策略A: LLM 返回了 x_field / y_field（字段映射模式） ──
    config = analysis_data.get("chart_config") or analysis_data.get("chart_data", {})
    x_field = config.get("x_field", "")
    y_field = config.get("y_field", "")
    title = config.get("title", "")
    series_name = config.get("series_name", "")

    sample_row = full_results[0]
    available_keys = list(sample_row.keys())

    logger.info(f"📊 [Chart] LLM returned: x_field='{x_field}', y_field='{y_field}', chart_type='{chart_type}'")
    logger.info(f"📊 [Chart] Available keys in result: {available_keys}")

    if x_field and y_field and x_field in sample_row and y_field in sample_row:
        logger.info(f"📊 [Chart] Strategy A matched: using x='{x_field}', y='{y_field}'")
        return _extract_chart_data(chart_type, title, series_name or y_field,
                                   x_field, y_field, full_results, max_chart_items)

    if x_field or y_field:
        logger.warning(f"📊 [Chart] Strategy A failed: x_field='{x_field}' in_keys={x_field in sample_row}, "
                       f"y_field='{y_field}' in_keys={y_field in sample_row}")

    # ── 策略B: LLM 返回了 x_axis_data / series_data（旧版兼容） ──
    chart_data = analysis_data.get("chart_data", {})
    if chart_data.get("x_axis_data") and chart_data.get("series_data"):
        x_col, y_col = _auto_detect_fields(sample_row)
        logger.info(f"📊 [Chart] Strategy B: auto_detect x='{x_col}', y='{y_col}'")
        if x_col and y_col:
            return _extract_chart_data(chart_type,
                                       chart_data.get("title", title),
                                       chart_data.get("series_name", series_name or y_col),
                                       x_col, y_col, full_results, max_chart_items)
        else:
            return {"type": chart_type, "data": chart_data}

    # ── 策略C: 完全自动推断 ──
    x_col, y_col = _auto_detect_fields(sample_row)
    logger.info(f"📊 [Chart] Strategy C: auto_detect x='{x_col}', y='{y_col}'")
    if x_col and y_col:
        return _extract_chart_data(chart_type, title, series_name or y_col,
                                   x_col, y_col, full_results, max_chart_items)

    logger.warning("📊 [Chart] All strategies failed, no chart generated")
    return None


def _extract_chart_data(chart_type, title, series_name, x_field, y_field,
                        full_results, max_items):
    """从全量数据提取图表数据，保持原始排序"""
    from decimal import Decimal
    chart_rows = full_results[:max_items]
    x_axis_data = []
    series_data = []
    for row in chart_rows:
        x_axis_data.append(str(row.get(x_field, "")))
        val = row.get(y_field, 0)
        try:
            if isinstance(val, Decimal):
                series_data.append(float(val))
            elif val is not None:
                series_data.append(float(val))
            else:
                series_data.append(0)
        except (ValueError, TypeError):
            series_data.append(0)

    return {
        "type": chart_type,
        "data": {
            "title": title,
            "x_axis_data": x_axis_data,
            "series_name": series_name,
            "series_data": series_data,
        }
    }


def _auto_detect_fields(sample_row: dict):
    """从一行数据中自动推断 x(标签列) / y(数值列)"""
    from decimal import Decimal
    keys = list(sample_row.keys())
    x_col = None
    y_col = None
    for k in keys:
        val = sample_row[k]
        # 兼容 Decimal、int、float
        is_numeric = isinstance(val, (int, float, Decimal))
        if not is_numeric and val is not None:
            try:
                float(val)
                is_numeric = True
            except (ValueError, TypeError):
                pass
        if y_col is None and is_numeric:
            y_col = k
        elif x_col is None and not is_numeric:
            x_col = k
    if x_col is None and len(keys) >= 1:
        x_col = keys[0]
    if y_col is None and len(keys) >= 2:
        y_col = keys[1]
    return x_col, y_col


# ==========================================
# 2. 主节点
# ==========================================

def analysis_node(state: AgentState) -> Dict[str, Any]:
    """数据分析节点 v3"""
    logger.info("🧠 [Analysis Node] Start analyzing...")

    question = state.get("question", "")
    sql = state.get("generated_sql", "")
    results = state.get("execution_result", []) or []

    if not results:
        return {
            "final_answer": "抱歉，根据您的查询条件，没有找到相关数据。",
            "visualization_config": None
        }

    total_rows = len(results)
    preview_count = min(20, total_rows)
    data_preview = results[:preview_count]

    llm = get_llm(temperature=0.1)

    # 🔥 关键修正：传入正确的模板参数
    messages = [
        SystemMessage(content=ANALYSIS_SYSTEM_PROMPT),
        HumanMessage(content=ANALYSIS_USER_TEMPLATE.format(
            question=question,
            sql=sql,
            total_rows=total_rows,
            preview_count=preview_count,
            data_preview=json.dumps(data_preview, ensure_ascii=False, default=str)
        ))
    ]

    try:
        response = llm.invoke(messages)
        content = response.content.strip()

        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        analysis_data = json.loads(content)

        final_answer = analysis_data.get("summary", "数据查询成功。")

        # 🔥 用全量数据构建图表，保持 SQL 排序
        viz_config = _build_viz_from_real_data(analysis_data, results)

        logger.info(
            f"🧠 [Analysis Node] Done. "
            f"Chart: {viz_config.get('type') if viz_config else 'None'}, "
            f"Items: {len(viz_config['data']['x_axis_data']) if viz_config else 0}"
        )

        return {
            "final_answer": final_answer,
            "visualization_config": viz_config
        }

    except Exception as e:
        logger.error(f"⚠️ [Analysis Node] Error: {e}", exc_info=True)

        # 降级：自动图表
        fallback_viz = None
        if total_rows > 1 and len(results[0]) >= 2:
            x_col, y_col = _auto_detect_fields(results[0])
            if x_col and y_col:
                chart_rows = results[:50]
                fallback_viz = {
                    "type": "bar",
                    "data": {
                        "title": question[:30],
                        "x_axis_data": [str(r.get(x_col, "")) for r in chart_rows],
                        "series_name": y_col,
                        "series_data": [float(r.get(y_col, 0) or 0) for r in chart_rows],
                    }
                }

        return {
            "final_answer": f"数据已查询，结果包含 {total_rows} 条记录。",
            "visualization_config": fallback_viz
        }