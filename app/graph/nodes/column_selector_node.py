"""
═══════════════════════════════════════════════════════════════════════════════
📦 模块名称: column_selector_node.py (v2 - 集成值匹配)
📝 改动说明:
   1. LLM 输出新增 entity_columns 字段（定位实体值在哪个列）
   2. 选列完成后，调用 value_linker 做 LCS 列内匹配
   3. 匹配结果写入 state.value_mappings，供 SQL 生成使用
═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import json
import sys
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage

from app.core.logger import logger
from app.core.state import AgentState
from app.graph.nodes.expand_node import llm
from app.modules.retrieval.graph.service import graph_service
from app.modules.retrieval.schema.value_linker import match_values_from_samples


# =============================================================================
# 1. 数据模型 (LLM 输出结构) —— 新增 entity_columns
# =============================================================================

class EntityColumnMapping(BaseModel):
    """单个实体值的列定位"""
    value: str = Field(..., description="用户问题中的实体值，如 '北京'、'华为 Mate 60'")
    candidate_columns: List[Dict[str, str]] = Field(
        ...,
        description='候选列列表，格式: [{"table": "xxx", "column": "yyy"}]'
    )


class ColumnSelectorOutput(BaseModel):
    """列选择器输出 —— 新增 entity_columns"""
    selected_columns: Dict[str, List[str]] = Field(
        ...,
        description="选中的列，格式: {表名: [列名1, 列名2, ...]}"
    )
    entity_columns: List[EntityColumnMapping] = Field(
        default_factory=list,
        description="实体值到列的映射（用于后续值匹配）"
    )
    reasoning: str = Field(..., description="简述选列理由")




# =============================================================================
# 2. 辅助格式化函数
# =============================================================================

def _format_schema_for_llm(retrieved_schema: dict) -> str:
    """瘦身版格式化"""
    if not retrieved_schema:
        return "（未检索到 Schema）"

    lines = []
    for table_name, table_data in retrieved_schema.items():
        columns = table_data.get("columns", [])
        lines.append(f"\n### 表: {table_name}")

        for col in columns:
            c_name = col.get("column_name", "unknown")
            d_type = col.get("data_type", "unknown")
            desc = col.get("ai_description", "无描述")
            samples = col.get("sample_values", [])
            sample_str = ""
            if samples:
                s_preview = str(samples[:3]).replace("\n", " ").replace("{", "").replace("}", "")
                sample_str = f" | 样本: {s_preview}"
            lines.append(f"  - {c_name} ({d_type}): {desc}{sample_str}")

    return "\n".join(lines)


def _format_expand_requirements(expand_data: Any) -> str:
    """格式化语义需求"""
    if not expand_data:
        return "（无额外语义提示）"

    lines = []

    def get_attr(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    caps = get_attr(expand_data, "capabilities", [])
    if caps:
        lines.append(f"- **操作类型**: {', '.join(caps)}")

    hints = get_attr(expand_data, "semantic_hints")
    if hints:
        target = get_attr(hints, "target_hint")
        metric = get_attr(hints, "metric_hint")
        filters = get_attr(hints, "filter_hints") or []
        group = get_attr(hints, "group_hint")
        time_hint = get_attr(hints, "time_hint")

        if target: lines.append(f"- **查询目标**: {target}")
        if metric: lines.append(f"- **核心指标**: {metric}")
        if filters: lines.append(f"- **过滤条件**: {'; '.join(filters)}")
        if group: lines.append(f"- **分组维度**: {group}")
        if time_hint: lines.append(f"- **时间范围**: {time_hint}")

    return "\n".join(lines) if lines else "（无额外语义提示）"


def _format_business_rules(rules: list) -> str:
    if not rules:
        return "（无业务规则）"
    lines = []
    for r in rules:
        content = r.get("content") or r.get("rule_text")
        if content:
            lines.append(f"- {content}")
    return "\n".join(lines)


# =============================================================================
# 4. 日志打印函数
# =============================================================================

def _log_selection_result(
    selected_schema: Dict[str, Any],
    join_paths: List[str],
    reasoning: str,
    value_mappings: List[Dict],
    entity_columns: List[Dict],
):
    """打印选列结果 + JOIN 路径 + 值匹配结果"""
    print("\n" + "=" * 60)

    total_tables = len(selected_schema)
    total_cols = sum(len(t.get("columns", [])) for t in selected_schema.values())

    print(f"🎯 [Column Selector] Final Context (Tables: {total_tables}, Cols: {total_cols})")
    print("=" * 60)
    print(f"🤔 Reasoning:\n{reasoning.strip()}")
    print("-" * 60)

    for i, (table_name, data) in enumerate(selected_schema.items()):
        columns = data.get("columns", [])
        col_names = [c["column_name"] for c in columns]
        col_str = ", ".join(col_names)
        if len(col_str) > 100:
            col_str = col_str[:100] + "..."
        print(f"   {i + 1}. 📦 {table_name}")
        print(f"      └── Cols: {col_str}")

    print("-" * 60)

    # JOIN 路径
    if join_paths:
        print(f"🔗 Calculated Join Paths ({len(join_paths)}):")
        for path in join_paths:
            print(f"   👉 {path}")
    else:
        print("🔗 No Joins needed (Single table or no path found)")

    print("-" * 60)

    # 🔥 新增: 打印实体定位
    if entity_columns:
        print(f"🏷️ Entity Column Mapping ({len(entity_columns)} entities):")
        for ec in entity_columns:
            val = ec.get("value", "?")
            cols = ec.get("candidate_columns", [])
            col_strs = [f"{c['table']}.{c['column']}" for c in cols]
            print(f"   🔍 \"{val}\" → {', '.join(col_strs)}")
    else:
        print("🏷️ No entity values detected")

    print("-" * 60)

    # 🔥 新增: 打印 LCS 值匹配结果
    if value_mappings:
        print(f"💡 Value Mappings ({len(value_mappings)} matches via LCS):")
        for m in value_mappings:
            u = m.get("user_input", "?")
            d = m.get("db_value", "?")
            t = m.get("table", "?")
            c = m.get("column", "?")
            print(f"   👉 \"{u}\" → \"{d}\" (at {t}.{c})")
    else:
        print("💡 Value Mappings: None")

    print("=" * 60 + "\n")
    sys.stdout.flush()


# =============================================================================
# 5. 核心节点函数
# =============================================================================

async def column_selector_node(state: AgentState) -> AgentState:
    logger.info("🎯 [ColumnSelector] Starting selection & entity locating...")

    question = state.get("question", "")
    retrieved_schema = state.get("retrieved_schema", {})
    business_rules = state.get("business_rules", [])
    expand_data = state.get("expand_data", {})

    if not retrieved_schema:
        logger.warning("⚠️ No schema input.")
        return state

    # ─────────────────────────────────────────────────────────────────────
    # Step 1: LLM 选列 + 定位实体列（一次调用完成）
    # ─────────────────────────────────────────────────────────────────────
    formatted_schema = _format_schema_for_llm(retrieved_schema)
    formatted_requirements = _format_expand_requirements(expand_data)

    final_prompt = COLUMN_SELECTOR_PROMPT_V2.format(
        question=question,
        expand_requirements=formatted_requirements,
        retrieved_schema=formatted_schema,
        business_rules=_format_business_rules(business_rules),
    )

    try:
        structured_llm = llm.with_structured_output(ColumnSelectorOutput)
        messages = [
            SystemMessage(content="你是一个精通数据库架构的专家。"),
            HumanMessage(content=final_prompt),
        ]
        response = await structured_llm.ainvoke(messages)

    except Exception as e:
        logger.error(f"❌ LLM failed: {e}")
        response = ColumnSelectorOutput(
            selected_columns={
                t: [c["column_name"] for c in d["columns"]]
                for t, d in retrieved_schema.items()
            },
            entity_columns=[],
            reasoning=f"Fallback due to error: {str(e)}",
        )

    # ─────────────────────────────────────────────────────────────────────
    # Step 2: 信息还原 (Rehydration)
    # ─────────────────────────────────────────────────────────────────────
    selected_schema_full = {}
    selected_tables_list = []

    if response and response.selected_columns:
        for table_name, selected_cols_list in response.selected_columns.items():
            if table_name not in retrieved_schema:
                continue

            original_cols_objs = retrieved_schema[table_name].get("columns", [])
            col_map = {c["column_name"]: c for c in original_cols_objs}

            valid_col_objs = []
            for c_name in selected_cols_list:
                if c_name in col_map:
                    valid_col_objs.append(col_map[c_name])

            if valid_col_objs:
                selected_schema_full[table_name] = {"columns": valid_col_objs}
                selected_tables_list.append(table_name)

    # ─────────────────────────────────────────────────────────────────────
    # Step 3: JOIN 路径补全
    # ─────────────────────────────────────────────────────────────────────
    join_paths = []
    if len(selected_tables_list) >= 2:
        try:
            join_paths = graph_service.find_join_path(selected_tables_list)
            logger.info(f"🔗 [PathFinder] Found {len(join_paths)} joins for tables: {selected_tables_list}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to find join paths: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # 🔥 Step 4: LCS 值匹配（新增！）
    # ─────────────────────────────────────────────────────────────────────
    value_mappings = []
    entity_columns_raw = []

    if response and response.entity_columns:
        # 将 Pydantic 对象转为 dict 列表
        entity_columns_raw = [ec.model_dump() for ec in response.entity_columns]

        # 用 sample_values 做 LCS 匹配（不额外查数据库）
        matches = match_values_from_samples(
            entity_columns=entity_columns_raw,
            selected_schema=selected_schema_full,
            min_score=70.0,
            top_k=5,
        )

        if matches:
            value_mappings = [m.to_dict() for m in matches]
            logger.info(f"🏆 [ValueLinker] LCS matched {len(value_mappings)} values")
        else:
            logger.info("ℹ️ [ValueLinker] No LCS matches found (values may already be exact)")

    # ─────────────────────────────────────────────────────────────────────
    # Step 5: 日志 & 返回
    # ─────────────────────────────────────────────────────────────────────
    _log_selection_result(
        selected_schema_full,
        join_paths,
        response.reasoning,
        value_mappings,
        entity_columns_raw,
    )

    return {
        "selected_schema": selected_schema_full,
        "selected_tables_list": selected_tables_list,
        "join_paths": join_paths,
        "column_selection_reasoning": response.reasoning,
        "value_mappings": value_mappings,  # 🔥 值匹配结果现在从这里产出
    }