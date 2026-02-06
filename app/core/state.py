# app/core/state.py

import operator
from typing import List, Dict, Any, TypedDict, Literal, Optional, Annotated
from enum import Enum
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage


# ==========================================
# 1. 基础枚举与定义
# ==========================================

class IntentType(str, Enum):
    DATA_QUERY = "DATA_QUERY"
    METADATA_QUERY = "METADATA_QUERY"
    OPS_DIAGNOSIS = "OPS_DIAGNOSIS"
    CHAT = "CHAT"
    AMBIGUOUS = "AMBIGUOUS"


# 能力类型定义 (Expand Node 使用)
CapabilityType = Literal[
    "LOOKUP",
    "FILTER",
    "COMPARISON",
    "TIME_RANGE",
    "AGGREGATION",
    "GROUPING",
    "SORT",
    "TOPK_LIMIT",
    "JOIN",
]


# ==========================================
# 2. 结构化关键词与语义线索 (v3.0 核心)
# ==========================================

class KeywordItem(BaseModel):
    """
    检索关键词原子单元
    type="CONCEPT" -> 仅用于 Schema RAG (找列名)
    type="VALUE"   -> 用于 Value Scanning (找行值)
    """
    keyword: str
    type: Literal["CONCEPT", "VALUE"]


class SemanticHints(BaseModel):
    """
    语义线索：只保留“人话层”的语义，不做 schema 推断
    """
    target_hint: Optional[str] = Field(None, description="主要对象（如 学校/学生）")
    metric_hint: Optional[str] = Field(None, description="指标/字段含义（如 eligible free rate）")
    filter_hints: List[str] = Field(default_factory=list, description="过滤原短语")
    group_hint: Optional[str] = Field(None, description="分组线索")
    time_hint: Optional[str] = Field(None, description="时间线索")


class CapabilityExpandOutput(BaseModel):
    """
    Expand Node 的 LLM 原始输出结构 (用于 Pydantic 解析)
    """
    capabilities: List[CapabilityType] = Field(default_factory=list)
    semantic_hints: SemanticHints = Field(default_factory=SemanticHints)
    search_keywords: List[KeywordItem] = Field(default_factory=list)


# ==========================================
# 3. Router 输出 (Intent Data 载体)
# ==========================================

class RouterOutput(BaseModel):
    """
    Intent Data: 在全链路传递的核心意图对象
    由 Router 初始化，由 Expand 填充细节。
    """
    # --- [A] 核心决策 (Router 产出) ---
    intent: IntentType = Field(..., description="用户意图")
    reason: str = Field(..., description="决策理由")

    needs_schema: bool = Field(..., description="是否查表结构")
    needs_knowledge: bool = Field(..., description="是否查知识库")
    needs_clarify: bool = Field(..., description="是否需要追问")

    query_complexity: Literal["simple", "medium", "hard"] = Field(..., description="查询复杂度")
    pruning_budget_cols: int = Field(..., description="列剪枝预算")

    clarify_questions: List[str] = Field(default_factory=list)

    # --- [B] 语义与检索 (Expand 填充) ---
    capabilities: List[CapabilityType] = Field(default_factory=list, description="查询能力需求")
    semantic_hints: SemanticHints = Field(default_factory=SemanticHints, description="语义线索")

    # 🔥 v3.0 核心字段：带类型的关键词列表
    search_keywords: List[KeywordItem] = Field(default_factory=list, description="[关键] 标准化的检索关键词列表")

    # --- [C] 兼容字段 (可选项) ---
    # 有时候只需要简单的一个 string 做日志
    schema_query: Optional[str] = Field(None, description="search_keywords 的字符串拼接版本")


# ==========================================
# 4. Agent State (全局状态)
# ==========================================

class AgentState(TypedDict, total=False):
    # --- 基础信息 ---
    trace_id: str
    question: str
    db_id: str
    history: List[BaseMessage]

    # --- 意图与路由 ---
    intent: IntentType
    intent_data: Optional[RouterOutput]  # 这里存储最核心的意图信息

    # --- 检索上下文 (Retrieval Context) ---
    retrieved_tables: List[str]
    retrieved_columns: List[Any]  # 原始列信息 List[Dict]
    schema_str: str  # 格式化后的 Schema String (给 LLM 看的)
    join_paths: List[str]  # Graph Service 算出的 JOIN 路径
    business_rules: List[str]  # 知识库规则 (RAG 召回)
    value_matches: List[str]  # Value Link 找到的匹配信息 (Format: "Entity 'X' matches...")

    # --- 生成与验证 (Generation & Verification) ---
    generated_sql: str
    verified: bool  # Verifier 是否通过
    feedback: str  # Verifier 给出的修改建议
    retry_count: Annotated[int, operator.add]  # 重试次数 (自动累加)

    # --- 执行结果 (Execution) ---
    sql_result: Optional[List[Dict[str, Any]]]  # 执行结果 (List of Rows)
    error_message: Optional[str]  # 执行报错信息

    # --- 最终输出 ---
    final_answer: Optional[str]  # 对用户的最终自然语言回复
    final_result: Any  # 结构化结果 (可选)