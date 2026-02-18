"""
═══════════════════════════════════════════════════════════════════════════════
📦 模块名称: column_selector_node.py (v4 - 集成值匹配 + 规则筛选 + 规则补列)
📝 改动说明:
   1. LLM 输出新增 entity_columns 字段（定位实体值在哪个列）
   2. 选列完成后，调用 value_linker 做 LCS 列内匹配
   3. 匹配结果写入 state.value_mappings，供 SQL 生成使用
   4. LLM 输出 selected_rules 字段，筛选与当前查询相关的业务规则
      筛选后的规则写入 state.business_rules（直接覆盖），供 Generator/Verifier 使用
   5. 🔥 新增: 扫描 required 规则文本，提取其中提到的 table.column，
      如果在 retrieved_schema 中缺失，则通过 Milvus 补检索并注入 selected_schema
═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import json
import re
import sys
from typing import Dict, Any, List, Optional, Set, Tuple
from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage

from app.core.logger import logger
from app.core.rag_store import rag_store
from app.core.state import AgentState
from app.graph.nodes.expand_node import llm
from app.modules.retrieval.graph.service import graph_service
from app.modules.retrieval.schema.value_linker import match_values_with_fallback


# =============================================================================
# 1. 数据模型 (LLM 输出结构) —— 新增 selected_rules
# =============================================================================

class EntityColumnMapping(BaseModel):
    """单个实体值的列定位"""
    value: str = Field(..., description="用户问题中的实体值，如 '北京'、'华为 Mate 60'")
    candidate_columns: List[Dict[str, str]] = Field(
        ...,
        description='候选列列表，格式: [{"table": "xxx", "column": "yyy"}]'
    )


class RuleSelection(BaseModel):
    """单条规则的筛选结果"""
    rule_index: int = Field(..., description="规则在输入列表中的索引（从0开始）")
    relevance: str = Field(
        ...,
        description="相关性判断: required=必须遵守, optional=可参考但不强制, irrelevant=与本次查询无关"
    )
    reason: str = Field(..., description="简述为什么该规则相关/不相关")


class ColumnSelectorOutput(BaseModel):
    """列选择器输出 —— v4: 选列 + 实体定位 + 规则筛选"""
    selected_columns: Dict[str, List[str]] = Field(
        ...,
        description="选中的列，格式: {表名: [列名1, 列名2, ...]}"
    )
    entity_columns: List[EntityColumnMapping] = Field(
        default_factory=list,
        description="实体值到列的映射（用于后续值匹配）"
    )
    selected_rules: List[RuleSelection] = Field(
        default_factory=list,
        description="对每条业务规则的相关性判断"
    )
    reasoning: str = Field(..., description="简述选列理由")



# =============================================================================
# 3. 辅助格式化函数
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


def _format_business_rules_indexed(rules: list) -> str:
    """🔥 带索引的规则格式化，方便 LLM 引用"""
    if not rules:
        return "（无业务规则）"
    lines = []
    for i, r in enumerate(rules):
        content = r.get("content") or r.get("rule_text") if isinstance(r, dict) else str(r)
        if content:
            lines.append(f"  [{i}] {content}")
    return "\n".join(lines)


# =============================================================================
# 4. 规则筛选辅助函数
# =============================================================================

def _filter_rules_by_selection(
    business_rules: list,
    selected_rules: List[RuleSelection],
) -> tuple[list, list]:
    """
    根据 LLM 的 selected_rules 输出，将业务规则分为：
    - required_rules: 必须遵守的规则（传给 Generator + Verifier）
    - optional_rules: 可参考的规则（仅传给 Generator 作为提示）

    Returns:
        (required_rules, optional_rules)
    """
    required_rules = []
    optional_rules = []

    # 建立索引映射
    rule_decisions = {}
    for sr in selected_rules:
        rule_decisions[sr.rule_index] = sr.relevance

    for i, rule in enumerate(business_rules):
        content = rule.get("content") or rule.get("rule_text") if isinstance(rule, dict) else str(rule)
        if not content:
            continue

        relevance = rule_decisions.get(i, "required")  # 默认保守：未标注的视为 required

        if relevance == "required":
            required_rules.append(content)
        elif relevance == "optional":
            optional_rules.append(content)
        # irrelevant -> 丢弃

    return required_rules, optional_rules


# =============================================================================
# 🔥 5. 规则补列：从 required 规则中提取列名，补检索缺失列
# =============================================================================

def _extract_column_refs_from_rules(
    required_rules: list,
    retrieved_schema: dict,
) -> List[Tuple[str, str]]:
    """
    从 required 规则文本中提取 table.column 引用，
    找出在 retrieved_schema 中已有该表但缺失该列的情况。

    支持的模式：
      - "orders 表中的 actual_amount 字段"
      - "order_status 不能为"
      - "table.column" 格式
      - "table_name.column_name" 英文引用

    Returns:
        [(table_name, column_name), ...] 需要补检索的列
    """
    # 收集 retrieved_schema 中已有的表名和列名
    existing_tables = set(retrieved_schema.keys())
    existing_cols: Dict[str, Set[str]] = {}
    for table_name, table_data in retrieved_schema.items():
        existing_cols[table_name] = {
            c["column_name"] for c in table_data.get("columns", [])
        }

    missing_cols: List[Tuple[str, str]] = []
    seen = set()

    for rule_text in required_rules:
        # ── 模式1: "xxx 表中的 yyy 字段" / "xxx 表的 yyy"
        pattern1 = re.findall(r'(\w+)\s*表[中的]*\s*(\w+)\s*字段', rule_text)
        for table, col in pattern1:
            if table in existing_tables and col not in existing_cols.get(table, set()):
                key = (table, col)
                if key not in seen:
                    missing_cols.append(key)
                    seen.add(key)

        # ── 模式2: "table.column" 格式（如 orders.order_status, product_skus.sales_count）
        pattern2 = re.findall(r'(\w+)\.(\w+)', rule_text)
        for table, col in pattern2:
            if table in existing_tables and col not in existing_cols.get(table, set()):
                key = (table, col)
                if key not in seen:
                    missing_cols.append(key)
                    seen.add(key)

        # ── 模式3: 单独出现的已知列名（如 "order_status 不能为 'cancelled'"）
        # 扫描规则中的每个单词，看是否匹配已知表中可能的列名
        # 这个用 Milvus 搜更靠谱，见下面的 _enrich_schema_from_rules

    return missing_cols


def _enrich_schema_from_rules(
    required_rules: list,
    retrieved_schema: dict,
    selected_schema: dict,
    selected_tables_list: list,
    db_id: str,
) -> Tuple[dict, list, int]:
    """
    🔥 核心补列逻辑：
    1. 从 required 规则中提取 table.column 引用
    2. 对于缺失的列，去 Milvus 按 "table_name column_name" 精确检索
    3. 将检索到的列信息注入 selected_schema

    Args:
        required_rules: 必须遵守的规则文本列表
        retrieved_schema: 原始检索到的完整 schema（用于确认表名存在）
        selected_schema: LLM 选列后的精选 schema（要往这里注入）
        selected_tables_list: 选中的表名列表
        db_id: 数据库标识

    Returns:
        (updated_selected_schema, updated_tables_list, injected_count)
    """
    missing_cols = _extract_column_refs_from_rules(required_rules, retrieved_schema)

    if not missing_cols:
        return selected_schema, selected_tables_list, 0

    injected_count = 0

    for table_name, col_name in missing_cols:
        # 用 "table_name col_name" 作为查询去 Milvus 精确检索
        query = f"{table_name} {col_name}"
        try:
            hits = rag_store.search_vectors(
                collection_name="schema",
                query_text=query,
                db_id=db_id,
                top_k=3,
            )
        except Exception as e:
            logger.warning(f"⚠️ [RuleEnrich] Milvus search failed for {query}: {e}")
            continue

        # 在检索结果中找到精确匹配的列
        matched_col = None
        for hit in hits:
            if hit.get("table_name") == table_name and hit.get("column_name") == col_name:
                matched_col = hit
                break

        if not matched_col:
            logger.warning(f"⚠️ [RuleEnrich] Column {table_name}.{col_name} not found in Milvus")
            continue

        # 注入到 selected_schema
        if table_name not in selected_schema:
            selected_schema[table_name] = {"columns": []}
            if table_name not in selected_tables_list:
                selected_tables_list.append(table_name)

        # 检查是否已存在
        existing_col_names = {c["column_name"] for c in selected_schema[table_name]["columns"]}
        if col_name not in existing_col_names:
            # 构建标准列信息（与 retriever 格式一致）
            col_info = {
                "table_name": table_name,
                "column_name": col_name,
                "data_type": matched_col.get("data_type"),
                "is_nullable": matched_col.get("is_nullable"),
                "sample_values": matched_col.get("sample_values", []),
                "distinct_count": matched_col.get("distinct_count"),
                "null_count": matched_col.get("null_count"),
                "numeric_stats": matched_col.get("numeric_stats"),
                "ai_description": matched_col.get("ai_description", ""),
            }
            selected_schema[table_name]["columns"].append(col_info)
            injected_count += 1
            logger.info(f"🔧 [RuleEnrich] Injected {table_name}.{col_name} from Milvus (required by business rule)")

    return selected_schema, selected_tables_list, injected_count


# =============================================================================
# 6. 日志打印函数
# =============================================================================

def _log_selection_result(
    selected_schema: Dict[str, Any],
    join_paths: List[str],
    reasoning: str,
    value_mappings: List[Dict],
    entity_columns: List[Dict],
    selected_rules: List[RuleSelection],
    business_rules: list,
    required_rules: list,
    optional_rules: list,
    injected_count: int = 0,
):
    """打印选列结果 + JOIN 路径 + 值匹配结果 + 规则筛选结果"""
    print("\n" + "=" * 60)

    total_tables = len(selected_schema)
    total_cols = sum(len(t.get("columns", [])) for t in selected_schema.values())

    inject_tag = f" (+{injected_count} from rules)" if injected_count > 0 else ""
    print(f"🎯 [Column Selector] Final Context (Tables: {total_tables}, Cols: {total_cols}{inject_tag})")
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

    # 实体定位
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

    # LCS 值匹配结果
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

    print("-" * 60)

    # 规则筛选结果
    total_rules = len(business_rules)
    print(f"📋 Rule Filtering ({len(required_rules)} required, {len(optional_rules)} optional, "
          f"{total_rules - len(required_rules) - len(optional_rules)} irrelevant "
          f"out of {total_rules} total):")
    for sr in selected_rules:
        icon = {"required": "✅", "optional": "⚡", "irrelevant": "❌"}.get(sr.relevance, "❓")
        content_preview = ""
        if sr.rule_index < len(business_rules):
            rule = business_rules[sr.rule_index]
            content_preview = (rule.get("content") or rule.get("rule_text") or str(rule))[:60]
            if len(content_preview) >= 60:
                content_preview += "..."
        print(f"   {icon} [{sr.rule_index}] {sr.relevance}: {sr.reason}")
        if content_preview:
            print(f"      └── \"{content_preview}\"")

    # 🔥 补列结果
    if injected_count > 0:
        print(f"   🔧 Rule-based enrichment: {injected_count} columns injected from Milvus")

    print("=" * 60 + "\n")
    sys.stdout.flush()


# =============================================================================
# 7. 核心节点函数
# =============================================================================

async def column_selector_node(state: AgentState) -> AgentState:
    logger.info("🎯 [ColumnSelector] Starting selection & entity locating & rule filtering...")

    question = state.get("question", "")
    retrieved_schema = state.get("retrieved_schema", {})
    business_rules = state.get("business_rules", [])
    expand_data = state.get("expand_data", {})
    db_id = state.get("db_id", "")

    if not retrieved_schema:
        logger.warning("⚠️ No schema input.")
        return state

    # ─────────────────────────────────────────────────────────────────────
    # Step 1: LLM 选列 + 定位实体列 + 筛选规则（一次调用完成）
    # ─────────────────────────────────────────────────────────────────────
    formatted_schema = _format_schema_for_llm(retrieved_schema)
    formatted_requirements = _format_expand_requirements(expand_data)

    from app.core.prompts import COLUMN_SELECTOR_PROMPT
    final_prompt = COLUMN_SELECTOR_PROMPT.format(
        question=question,
        expand_requirements=formatted_requirements,
        retrieved_schema=formatted_schema,
        business_rules=_format_business_rules_indexed(business_rules),
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
            selected_rules=[],
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
    # Step 4: 规则筛选
    # ─────────────────────────────────────────────────────────────────────
    required_rules = []
    optional_rules = []
    selected_rules_raw = []

    if response and response.selected_rules:
        selected_rules_raw = response.selected_rules
        required_rules, optional_rules = _filter_rules_by_selection(
            business_rules, response.selected_rules
        )
        logger.info(
            f"📋 [RuleFilter] {len(required_rules)} required, "
            f"{len(optional_rules)} optional, "
            f"{len(business_rules) - len(required_rules) - len(optional_rules)} irrelevant"
        )
    else:
        for rule in business_rules:
            content = rule.get("content") or rule.get("rule_text") if isinstance(rule, dict) else str(rule)
            if content:
                required_rules.append(content)
        logger.info("📋 [RuleFilter] No selection output, keeping all rules as required (conservative)")

    # 合并为下游可用的格式
    selected_business_rules = []
    for r in required_rules:
        selected_business_rules.append(r)
    for r in optional_rules:
        selected_business_rules.append(f"[可选参考] {r}")

    # ─────────────────────────────────────────────────────────────────────
    # 🔥 Step 5: 规则补列 —— 从 required 规则中提取缺失列，Milvus 补检索
    # ─────────────────────────────────────────────────────────────────────
    injected_count = 0
    if required_rules and db_id:
        selected_schema_full, selected_tables_list, injected_count = _enrich_schema_from_rules(
            required_rules=required_rules,
            retrieved_schema=retrieved_schema,
            selected_schema=selected_schema_full,
            selected_tables_list=selected_tables_list,
            db_id=db_id,
        )
        if injected_count > 0:
            # 补列后可能新增了表，重新计算 JOIN 路径
            if len(selected_tables_list) >= 2:
                try:
                    join_paths = graph_service.find_join_path(selected_tables_list)
                    logger.info(f"🔗 [PathFinder] Recalculated {len(join_paths)} joins after enrichment")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to recalculate join paths: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # Step 6: LCS 值匹配
    # ─────────────────────────────────────────────────────────────────────
    value_mappings = []
    entity_columns_raw = []

    if response and response.entity_columns:
        entity_columns_raw = [ec.model_dump() for ec in response.entity_columns]

        matches = await match_values_with_fallback(
            entity_columns=entity_columns_raw,
            selected_schema=selected_schema_full,
            min_score=70.0,
            top_k=5,
            enable_db_fallback=True,
        )

        if matches:
            value_mappings = [m.to_dict() for m in matches]
            logger.info(f"🏆 [ValueLinker] LCS matched {len(value_mappings)} values")
        else:
            logger.info("ℹ️ [ValueLinker] No LCS matches found (values may already be exact)")

    # ─────────────────────────────────────────────────────────────────────
    # Step 7: 日志 & 返回
    # ─────────────────────────────────────────────────────────────────────
    _log_selection_result(
        selected_schema_full,
        join_paths,
        response.reasoning,
        value_mappings,
        entity_columns_raw,
        selected_rules_raw,
        business_rules,
        required_rules,
        optional_rules,
        injected_count,
    )

    return {
        "selected_schema": selected_schema_full,
        "selected_tables_list": selected_tables_list,
        "join_paths": join_paths,
        "column_selection_reasoning": response.reasoning,
        "value_mappings": value_mappings,
        "business_rules": selected_business_rules,  # 直接覆盖，下游无感
    }