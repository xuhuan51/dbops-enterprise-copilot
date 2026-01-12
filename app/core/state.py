from typing import List, Dict, Any, TypedDict, Literal, Optional
from pydantic import BaseModel, Field


class AgentState(TypedDict):
    # 基础字段
    trace_id: str
    question: str
    intent: str
    # 聊天记录
    chat_history: List[str]

    # 召回层 (Retrieval Context)
    candidate_tables: List[Dict]

    # 生成层 (Generation Output)
    generated_sql: str
    sql_confidence: float
    # 记录模型真实的引用情况
    tables_used: List[str]
    assumptions: List[str]
    search_query: Optional[str]

    # 错误处理层
    validation_error: Optional[str]
    error_type: Optional[str]
    repair_keywords: List[str]

    retry_count: int
    reflection_count: int

    # 🔥 新增: 最终回答 (可能是 "SQL_RESULT:..." 或 "抱歉，无法回答...")
    final_answer: Optional[str]

    final_result: Any
    reflection_passed: Optional[bool]
    reflection_feedback: Optional[str]


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
    intent: Literal["data_query", "sensitive", "non_data"] = Field(
        description="用户意图分类: data_query(查数据), sensitive(敏感信息), non_data(闲聊)"
    )

class ReflectionOutput(BaseModel):
    is_valid: bool = Field(description="SQL是否在语义上真正回答了用户的问题，且使用了正确的表")
    reason: str = Field(description="判断理由")
    missing_info: str = Field(description="如果无效，指出缺少的表或信息")
    suggested_search_keywords: List[str] = Field(description="如果无效，提供一组新的搜索关键词用于修补")