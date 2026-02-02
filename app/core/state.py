from typing import List, Dict, Any, TypedDict, Literal, Optional, Annotated
from enum import Enum
import operator
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



# ==========================================
# 新增：规划线索 (Plan Hints) - 剥离排序/Limit
# ==========================================
class PlanHints(BaseModel):
    limit: Optional[int] = Field(None, description="LIMIT 数量 (e.g. 3)")
    order_direction: Optional[Literal["ASC", "DESC"]] = Field(None, description="排序方向")
    agg_method: Optional[Literal["MAX", "MIN", "SUM", "AVG", "COUNT"]] = Field(None, description="聚合方式")

# ==========================================
# 修改：语义桶 (Semantic Buckets)
# ==========================================
class SemanticBuckets(BaseModel):
    """思维链中间层：需求桶"""
    entity: List[str] = Field(default_factory=list, description="业务对象 (表级)")
    metric: List[str] = Field(default_factory=list, description="数值指标 (列级，不含排序词)")
    filter: List[str] = Field(default_factory=list, description="过滤条件 (保留原短语)")
    plan_hints: Optional[PlanHints] = Field(None, description="排序与截断信息")
    target_hint: Optional[str] = Field(None, description="主要对象")
    metric_hint: Optional[str] = Field(None, description="指标/字段含义")
    filter_hints: List[str] = Field(default_factory=list, description="过滤原短语")
    group_hint: Optional[str] = Field(None, description="分组线索")
    time_hint: Optional[str] = Field(None, description="时间线索")



# ==========================================
# 修改：Expand 输出
# ==========================================
class ExpandOutput(BaseModel):
    """Expand Node 输出"""
    semantic_buckets: SemanticBuckets = Field(..., description="结构化拆解")
    schema_keywords: List[str] = Field(default_factory=list, description="物理字段关键词列表") # ✅ 改为 List
    knowledge_keywords: List[str] = Field(default_factory=list, description="严格受控的业务术语")

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

class SemanticHints(BaseModel):
    """
    Option A: 只保留“人话层”的语义线索，不做 schema 推断
    """
    target_hint: Optional[str] = Field(None, description="主要对象（如 学校/学生/订单/用户）")
    metric_hint: Optional[str] = Field(None, description="指标/字段含义（如 eligible free rate / zip code）")
    filter_hints: List[str] = Field(default_factory=list, description="过滤原短语（允许包含具体值）")
    group_hint: Optional[str] = Field(None, description="分组线索（如 by city / 每个县）")
    time_hint: Optional[str] = Field(None, description="时间线索（如 last 30 days / 2023年）")


class CapabilityExpandOutput(BaseModel):
    """
    Expand Node 新输出（Option A）
    """
    capabilities: List[CapabilityType] = Field(default_factory=list)
    semantic_hints: SemanticHints = Field(default_factory=SemanticHints)
    search_keywords: List[str] = Field(default_factory=list, description="标准化的英文检索关键词")

# ==========================================
# 3. Router 输出 (决策包)
# ==========================================
class RouterOutput(BaseModel):
    """
    Router 节点的输出对象，后续会被 Expand 节点填充更多细节。
    """
    # --- 1. 核心决策 (Router 负责) ---
    intent: IntentType = Field(..., description="用户意图")
    reason: str = Field(..., description="决策理由")

    # --- 2. 资源开关 (Router 负责) ---
    needs_schema: bool = Field(..., description="是否查表结构")
    needs_knowledge: bool = Field(..., description="是否查知识库")
    needs_clarify: bool = Field(..., description="是否需要追问")

    # --- 3. 预算控制 (Router 负责) ---
    query_complexity: Literal["simple", "medium", "hard"] = Field(..., description="查询复杂度")
    pruning_budget_cols: int = Field(..., description="列剪枝预算")

    # --- 4. 交互追问 (Router 负责) ---
    clarify_questions: List[str] = Field(default_factory=list)

    # --- 5. 知识库专用词 (Router 负责) ---
    # Router 可能会专门提取一些业务术语（如 "ROI", "大R"）用于查文档
    knowledge_keywords: List[str] = Field(default_factory=list, description="业务术语/知识库关键词")

    # =================================================================
    # 🔥 下面是 Expand Node 填充的字段 (v3.0 架构)
    # =================================================================

    # [A] 语义理解：给 Generator (写SQL) 看的
    capabilities: List[CapabilityType] = Field(default_factory=list, description="查询能力需求 (Filter, Sort, etc.)")
    semantic_hints: SemanticHints = Field(default_factory=SemanticHints, description="自然语言层面的语义线索")

    # [B] 物理检索：给 Retriever (找列) 看的
    search_keywords: List[str] = Field(default_factory=list, description="[关键] 标准化的英文检索关键词列表")

    # [C] 兼容/日志字段
    schema_query: Optional[str] = Field(None, description="search_keywords 的字符串拼接版本，用于日志或简单检索")


# ==========================================
# 4. Phase 2: Planner 结构 (CoT)
# ==========================================

class SQLPlan(BaseModel):
    """Structured Reasoning: 强制模型先做需求映射，再生成计划"""
    thought_process: str = Field(...,
                                 description="Step-by-step reasoning: 1.Analyze Requirements 2.Map to Schema 3.Select Join Path")
    tables_involved: List[str] = Field(..., description="Final list of table names to use")
    join_paths: List[str] = Field(..., description="Exact JOIN conditions from Graph Hints")
    columns_selected: List[str] = Field(..., description="Physical columns for SELECT")
    filter_conditions: List[str] = Field(..., description="Physical conditions for WHERE")
    is_impossible: bool = Field(False, description="Set true if required columns are missing")


# ==========================================
# 5. Phase 4: Diagnosis 结构
# ==========================================

class DiagnosisResult(BaseModel):
    status: Literal["legit_empty", "join_issue", "filter_issue", "error"]
    reason: str
    suggested_fix: str


# ==========================================
# 6. LLM 输出结构 (Generate/Reflect/Classify)
# ==========================================

class SQLOutput(BaseModel):
    sql: str = Field(description="生成的 SQL 语句")
    assumptions: List[str] = Field(default_factory=list, description="假设条件")
    tables_used: List[str] = Field(default_factory=list, description="使用到的表名")
    confidence: float = Field(0.0, description="信心分数 0.0-1.0")


class ErrorOutput(BaseModel):
    error_type: Literal["MISSING_COLUMN", "MISSING_TABLE", "WRONG_TABLE", "SYNTAX_ERROR", "NON_FIXABLE"]
    analysis: str
    search_keywords: List[str] = Field(default_factory=list, description="用于补搜的关键词")


# ==========================================
# 7. Agent State (全局状态)
# ==========================================

class AgentState(TypedDict, total=False):
    # Base
    trace_id: str
    question: str
    db_id: str
    history: List[BaseMessage]

    # Router & Intent
    intent: IntentType
    intent_data: Optional[RouterOutput]

    # Retrieval Context
    retrieved_tables: List[str]
    retrieved_columns: List[Any]  # 原始列信息
    schema_str: str  # 格式化后的 Schema
    graph_hints: List[str]  # Graph Service 算出的路径
    business_rules: List[str]  # 知识库规则

    # Planning
    plan: Optional[SQLPlan]

    # Generation & Execution
    generated_sql: str
    sql_result: Optional[List[Dict[str, Any]]]
    error_message: Optional[str]

    # Diagnosis
    diagnosis: Optional[DiagnosisResult]

    # Counters & Feedback
    retry_count: Annotated[int, operator.add]
    reflection_feedback: Optional[str]

    # Final Output
    final_answer: Optional[str]
    final_result: Any

