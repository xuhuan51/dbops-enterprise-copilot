from typing import List, Dict, Any, TypedDict, Literal, Optional
from enum import Enum
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage


# ==========================================
# 0. Execution Trace (可选：执行记录)
# ==========================================

class ExecutionStep(TypedDict):
    node: str               # 节点名称 (e.g., "Router")
    purpose: str            # 为什么要调用这个节点 (e.g., "分析意图并分流")
    llm_raw_output: Any     # 大模型生成的原始数据 (JSON对象或SQL)
    thought_process: str    # 大模型的推理过程 (Reasoning)
    status: str             # 执行状态 (Success/Failed/Blocked)
    timestamp: float        # 时间戳


# ==========================================
# 1. Router Models (唯一版本 ✅)
# ==========================================

class IntentType(str, Enum):
    DATA_QUERY = "DATA_QUERY"
    METADATA_QUERY = "METADATA_QUERY"
    OPS_DIAGNOSIS = "OPS_DIAGNOSIS"
    CHAT = "CHAT"
    AMBIGUOUS = "AMBIGUOUS"


class RouterOutput(BaseModel):
    """
    核心调度器的输出结构：
    包含意图、开关、搜索词、追问建议等所有指令
    """
    reason: str = Field(..., description="解释为什么选择该意图")
    intent: IntentType = Field(..., description="主要意图分类")

    # --- 资源开关 (Switches) ---
    needs_schema: bool = Field(..., description="是否需要检索数据库表结构(左塔)")
    needs_knowledge: bool = Field(..., description="是否需要检索业务知识/文档(右塔)")
    needs_clarify: bool = Field(..., description="是否需要反问用户")

    # --- 搜索增强 (Search Terms) ---
    schema_query: Optional[str] = Field(None, description="用于左塔检索的改写语句(去噪后)")
    knowledge_keywords: List[str] = Field(default_factory=list, description="用于右塔检索的关键词列表")

    # --- 追问 (Clarification) ---
    clarify_questions: List[str] = Field(default_factory=list, description="如果不清楚，生成的追问建议")


# ==========================================
# 2. AgentState (图状态定义)
# ==========================================

class AgentState(TypedDict, total=False):
    """
    total=False: 允许不同节点逐步补齐字段，避免 LangGraph 过程中 KeyError。
    如果你想更严格，可以改回 total=True + 在每个节点都填满字段。
    """

    # --- 基础字段 ---
    trace_id: str
    question: str
    history: List[BaseMessage]

    # --- Router ---
    intent: IntentType
    intent_data: Optional[RouterOutput]

    # --- 召回层 (Retrieval Context) ---
    candidate_tables: List[Dict[str, Any]]
    rag_contexts: Dict[str, str]             # keys: schema, knowledge
    table_columns: Dict[str, List[str]]      # 物理表列名缓存

    # --- 生成层 (Generation Output) ---
    generated_sql: str
    sql_confidence: float
    tables_used: List[str]
    assumptions: List[str]
    search_query: Optional[str]              # 兼容字段：可保留

    # --- 错误处理与修复 ---
    validation_error: Optional[str]
    error_type: Optional[str]
    suggested_search_keywords: List[str]
    retry_count: int
    reflection_count: int

    # --- 反思与哨兵 ---
    reflection_passed: Optional[bool]
    reflection_feedback: Optional[str]
    sentinel_blocked: Optional[bool]

    # --- 结果层 ---
    final_answer: Optional[str]
    final_result: Any


# ==========================================
# 3. LLM 输出结构 (Generate/Reflect/Classify)
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


class ReflectionOutput(BaseModel):
    is_valid: bool = Field(description="true=通过, false=不通过")

    severity: Literal["MUST_FAIL", "SHOULD_WARN", "PASS"] = Field(
        default="PASS",
        description="MUST_FAIL=必须修复; SHOULD_WARN=建议提醒但不阻断; PASS=通过"
    )

    reason: str = Field(default="", description="简短判断理由")

    error_type: Literal["COLUMN_NOT_FOUND", "TABLE_NOT_FOUND", "LOGIC_ERROR", "NONE"] = Field(
        default="NONE"
    )

    missing_items: List[str] = Field(default_factory=list, description="Schema中找不到的表/字段")
    suggested_search_keywords: List[str] = Field(default_factory=list, description="修复检索关键词")
    suggested_improvements: List[str] = Field(default_factory=list, description="可选改进建议(不触发repair)")
