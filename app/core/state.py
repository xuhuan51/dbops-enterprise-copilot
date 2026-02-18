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


CapabilityType = Literal[
    "LOOKUP", "FILTER", "COMPARISON", "TIME_RANGE",
    "AGGREGATION", "GROUPING", "SORT", "TOPK_LIMIT", "JOIN",
]


# ==========================================
# 1.  Router 输出 (只负责分流)
# ==========================================
class RouterOutput(BaseModel):
    """Router 节点的产物：意图与资源开关"""
    intent: IntentType = Field(..., description="用户的核心意图")
    reason: str = Field(..., description="判断意图的理由")

    needs_clarify: bool = Field(default=False, description="是否需要进一步澄清")
    clarify_questions: List[str] = Field(default_factory=list, description="追问话术")

    # 资源开关 (默认开启)
    needs_schema: bool = Field(default=True, description="是否检索表结构")
    needs_knowledge: bool = Field(default=True, description="是否检索业务知识库")



# ==========================================
# 2. 结构化关键词与语义线索
# ==========================================
class KeywordTermGroup(BaseModel):
    """单个关键词组（包含多个同义词）"""
    group: str = Field(..., description="组的语义主题，如 '订单表', '状态字段', '城市值'")
    terms: List[str] = Field(default_factory=list, description="该组的关键词列表")


class SearchKeywords(BaseModel):
    """检索关键词（分 concepts 和 values）"""
    concepts: List[KeywordTermGroup] = Field(default_factory=list, description="概念关键词组（用于 Schema 检索）")
    values: List[KeywordTermGroup] = Field(default_factory=list, description="值关键词组（用于值检索）")


class SemanticHints(BaseModel):
    """语义线索"""
    target_hint: Optional[str] = Field(None, description="查询主体")
    metric_hint: Optional[str] = Field(None, description="查询指标")
    filter_hints: List[str] = Field(default_factory=list, description="筛选条件")
    group_hint: Optional[str] = Field(None, description="分组维度")
    time_hint: Optional[str] = Field(None, description="时间范围")


class ExpandOutput(BaseModel):
    """Expand 节点输出"""
    capabilities: List[str] = Field(default_factory=list, description="能力标签")
    semantic_hints: SemanticHints = Field(default_factory=SemanticHints, description="语义线索")
    search_keywords: SearchKeywords = Field(default_factory=SearchKeywords, description="检索关键词")



# ==========================================
# 3. Agent State (双插槽)
# ==========================================
class AgentState(TypedDict, total=False):
    # --- 基础信息 ---
    trace_id: str
    question: str
    db_id: str
    history: List[BaseMessage]

    # --- 意图与路由 (Slot 1) ---
    intent_data: Optional[RouterOutput]  # 👈 Router 填这里

    # --- 扩展搜索信息 (Slot 2) ---
    expand_data: Optional[ExpandOutput]  # 👈 Expand 填这里 (新加的)

    # --- 检索上下文 ---
    retrieved_schema: Dict[str, Any]  # 存真实的表结构字典
    value_mappings: List[Any]  # 存真实的 "北京"->"北京市" 映射
    join_paths: List[str]
    business_rules: List[str]
    value_matches: List[str]

    # --- 选列阶段(Selection - 精选)---
    selected_schema: Dict[str, Any]  # 精选后的 Schema (Generator 用这个)
    selected_tables_list: List[str]  # 选中的表名列表
    join_paths: List[str]  # 计算出的 JOIN 路径
    column_selection_reasoning: str  # 选列理由

    # --- 生成与验证 ---
    generated_sql: str
    final_sql: Optional[str]
    verified: bool
    feedback: str
    feedback_history: Annotated[List[str], operator.add]

    # 重试
    retry_count: int
    execution_retries: int

    # --- 执行结果 ---
    execution_result: Optional[List[Dict[str, Any]]]
    execution_error: Optional[str]
    is_executable: bool

    # --- 最终输出 ---
    final_answer: Optional[str]
    final_result: Any