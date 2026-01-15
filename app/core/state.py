from typing import List, Dict, Any, TypedDict, Literal, Optional
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage  # 🔥 新增引入


class AgentState(TypedDict):
    # --- 基础字段 ---
    trace_id: str
    question: str
    intent: str

    # 🔥 修复 1: 名称改为 history (匹配 agent_graph.py)
    # 🔥 修复 2: 类型改为 List[BaseMessage] (匹配 msg.content/msg.type 用法)
    history: List[BaseMessage]

    # --- 召回层 (Retrieval Context) ---
    candidate_tables: List[Dict]

    # --- 生成层 (Generation Output) ---
    generated_sql: str
    sql_confidence: float
    tables_used: List[str]
    assumptions: List[str]
    search_query: Optional[str]

    # --- 错误处理层 ---
    validation_error: Optional[str]
    error_type: Optional[str]
    repair_keywords: List[str]

    retry_count: int
    reflection_count: int

    # --- 结果层 ---
    final_answer: Optional[str]
    table_columns: Dict[str, List[str]]
    final_result: Any

    # --- 反思与哨兵 ---
    reflection_passed: Optional[bool]
    reflection_feedback: Optional[str]
    sentinel_blocked: Optional[bool]


# --- LLM 输出结构 (保持不变) ---
class SQLOutput(BaseModel):
    sql: str = Field(description="生成的 SQL 语句")
    assumptions: List[str] = Field(description="假设条件")
    tables_used: List[str] = Field(description="使用到的表名")
    confidence: float = Field(description="信心分数 0.0-1.0")


class ErrorOutput(BaseModel):
    error_type: Literal["MISSING_COLUMN", "MISSING_TABLE", "WRONG_TABLE", "SYNTAX_ERROR", "NON_FIXABLE"]
    analysis: str
    search_keywords: List[str] = Field(description="用于补搜的关键词")


class IntentOutput(BaseModel):
    # 🔥 修复：
    # 1. 选项必须大写，与 INTENT_CHECK_PROMPT 里的要求一致
    # 2. 选项必须包含 UNKNOWN，防止 LLM 遇到无法回答的问题时报错
    intent: Literal["DATA_QUERY", "CHAT", "UNKNOWN"] = Field(
        description="用户意图分类: DATA_QUERY(数据查询), CHAT(闲聊), UNKNOWN(无法识别)"
    )


class ReflectionOutput(BaseModel):
    is_valid: bool = Field(description="SQL是否在语义上真正回答了用户的问题，且使用了正确的表")
    reason: str = Field(description="判断理由")
    missing_info: str = Field(description="如果无效，指出缺少的表或信息")
    suggested_search_keywords: List[str] = Field(description="如果无效，提供一组新的搜索关键词用于修补")