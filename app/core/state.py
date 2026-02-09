# app/core/state.py
import operator
from typing import List, Dict, Any, TypedDict, Literal, Optional, Annotated
from enum import Enum
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage


# ==========================================
# 1. 基础枚举与定义 (保持不变)
# ==========================================

class IntentType(str, Enum):
    DATA_QUERY = "DATA_QUERY"
    METADATA_QUERY = "METADATA_QUERY"
    OPS_DIAGNOSIS = "OPS_DIAGNOSIS"
    CHAT = "CHAT"
    AMBIGUOUS = "AMBIGUOUS"


CapabilityType = Literal[
    "LOOKUP", "FILTER", "COMPARISON", "TIME_RANGE",
    "AGGREGATION", "GROUPING", "SORT", "TOPK_LIMIT", "JOIN",
]


# ==========================================
# 2. 结构化关键词与语义线索 (保持不变)
# ==========================================

class KeywordItem(BaseModel):
    keyword: str
    type: Literal["CONCEPT", "VALUE"]


class SemanticHints(BaseModel):
    target_hint: Optional[str] = Field(None, description="主要对象")
    metric_hint: Optional[str] = Field(None, description="指标/字段含义")
    filter_hints: List[str] = Field(default_factory=list, description="过滤原短语")
    group_hint: Optional[str] = Field(None, description="分组线索")
    time_hint: Optional[str] = Field(None, description="时间线索")


class CapabilityExpandOutput(BaseModel):
    capabilities: List[CapabilityType] = Field(default_factory=list)
    semantic_hints: SemanticHints = Field(default_factory=SemanticHints)
    search_keywords: List[KeywordItem] = Field(default_factory=list)


# ==========================================
# 3. Router 输出 (保持不变)
# ==========================================

class RouterOutput(BaseModel):
    intent: IntentType = Field(...)
    reason: str = Field(...)
    needs_schema: bool = Field(...)
    needs_knowledge: bool = Field(...)
    needs_clarify: bool = Field(...)
    query_complexity: Literal["simple", "medium", "hard"] = Field(...)
    pruning_budget_cols: int = Field(...)
    clarify_questions: List[str] = Field(default_factory=list)
    capabilities: List[CapabilityType] = Field(default_factory=list)
    semantic_hints: SemanticHints = Field(default_factory=SemanticHints)
    search_keywords: List[KeywordItem] = Field(default_factory=list)
    schema_query: Optional[str] = Field(None)


# ==========================================
# 4. Agent State (全局状态 - 已清理)
# ==========================================

class AgentState(TypedDict, total=False):
    # --- 基础信息 ---
    trace_id: str
    question: str
    db_id: str
    history: List[BaseMessage]

    # --- 意图与路由 ---
    intent: IntentType
    intent_data: Optional[RouterOutput]

    # --- 检索上下文 ---
    retrieved_tables: List[str]
    retrieved_columns: List[Any]
    schema_str: str
    join_paths: List[str]
    business_rules: List[str]
    value_matches: List[str]

    # 补充上下文 (Schema RAG 检索到的 Context)
    schema_context: str
    rules_context: str
    constraints_context: str
    join_paths_context: str

    # --- 生成与验证 ---
    generated_sql: str  # LLM 生成的原始 SQL
    final_sql: Optional[str]  # 清洗后实际执行的 SQL
    verified: bool  # Verifier 结果
    feedback: str  # Verifier 建议
    feedback_history: Annotated[List[str], operator.add]  # 使用 operator.add 方便自动追加

    # 重试计数 (使用普通 int，由节点手动 state["retry_count"] += 1 控制)
    retry_count: int

    # --- 执行结果 (Execution - 统一命名) ---
    execution_result: Optional[List[Dict[str, Any]]]  # 成功时的结果行
    execution_error: Optional[str]  # 失败时的报错信息
    is_executable: bool  # 是否执行成功

    # --- 最终输出 ---
    final_answer: Optional[str]
    final_result: Any