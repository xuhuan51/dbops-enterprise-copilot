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
from app.modules.retrieval.schema.value_linker import match_values_with_fallback


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
# 2. Prompt 模板 —— 新增 entity_columns 输出要求
# =============================================================================

COLUMN_SELECTOR_PROMPT_V2 = """
你是一个拥有 20 年经验的**数据库架构师**。
你的任务是：基于用户的自然语言问题，从数据库 Schema 中，**精准锁定**生成 SQL 所需的最小化表和列集合。
同时，你需要**识别问题中的实体值**，并判断它们可能对应数据库中的哪个列。

---
### 🧠 核心推理思维链

#### 1. 实体-表 归属原则
不要只看关键词匹配，要分析**业务实体**的归属：
- **用户属性**（性别、年龄、等级） → `users` 表
- **商品属性**（名称、品牌、规格） → `products` 或 `order_items` 表
- **交易属性**（金额、时间、状态） → `orders` 表
- **收货信息**（省份、城市、地址） → `user_addresses` 表

#### 2. SQL 子句全覆盖原则
选出的列必须能支撑完整的 SQL 语句：
- **SELECT**: 用户想看什么？
- **WHERE**: 用户限制了什么？
- **GROUP BY**: 用户想怎么统计？
- **ORDER BY**: 用户想怎么排？

#### 3. 事实表与维度表
如果查询涉及**具体的交易细节**（如"买了某商品"），**必须**选中交易明细表（如 `order_items`）。

#### 4. 实体值定位（关键！）
识别问题中的**具体值**（地名、人名、产品名、状态值等），判断它们最可能在哪个表的哪个列。
- "北京" → 大概率在 `user_addresses.province` 或 `user_addresses.city`
- "华为 Mate 60" → 大概率在 `order_items.product_name` 或 `products.product_name`
- "已发货" → 大概率在 `orders.order_status`
- **不要把产品名映射到 gender、status 等无关列！**

---
### 📝 输入上下文

**1. 用户问题**:
{question}

**2. 语义分析**:
{expand_requirements}

**3. 检索到的 Schema**:
{retrieved_schema}

**4. 业务规则**:
{business_rules}

---
### 📤 输出要求

请输出一个纯 JSON 对象，格式如下：
```json
{{
  "reasoning": "简短说明为什么选这些表和列",
  "selected_columns": {{
    "table_name_1": ["col1", "col2"],
    "table_name_2": ["col3", "col4"]
  }},
  "entity_columns": [
    {{
      "value": "北京",
      "candidate_columns": [
        {{"table": "user_addresses", "column": "province"}},
        {{"table": "user_addresses", "column": "city"}}
      ]
    }},
    {{
      "value": "华为 Mate 60",
      "candidate_columns": [
        {{"table": "order_items", "column": "product_name"}}
      ]
    }}
  ]
}}
```

### entity_columns 规则：
1. 只提取**具体的值**（地名、产品名、状态值等），不要提取通用概念（如"订单"、"用户"）
2. 每个值给出 1~2 个最可能的候选列
3. 候选列**必须**出现在 selected_columns 中
4. 如果问题中没有具体实体值，entity_columns 返回空列表 `[]`

### 注意事项
1. **宁多勿少**：不确定时就选上
2. **不选连接键**：user_id, order_id 这种外键不用选（图谱会自动补）
3. **按表分组输出**

现在开始！
"""


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

        # 🔥 分层匹配：sample_values 快速通道 + DB LIKE 兜底
        matches = await match_values_with_fallback(
            entity_columns=entity_columns_raw,
            selected_schema=selected_schema_full,
            min_score=70.0,
            top_k=5,
            enable_db_fallback=True,  # 开启数据库兜底
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