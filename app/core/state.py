from typing import List, Dict, Any, TypedDict, Literal, Optional
from pydantic import BaseModel, Field


# --- Graph 状态 ---
class AgentState(TypedDict):
    question: str
    intent: str  # 意图
    candidate_tables: List[Dict]  # 候选表池

    generated_sql: str  # 生成的 SQL
    sql_confidence: float  # 信心分

    validation_error: Optional[str]  # 验证报错信息
    error_type: Optional[str]  # 错误类型
    repair_keywords: List[str]  # 补搜词

    retry_count: int  # 重试计数
    final_result: Any  # 最终 SQL 或 结果


# --- LLM 输出结构 (Structured Output) ---

class IntentOutput(BaseModel):
    intent: Literal["data_query", "sensitive", "non_data"]
    reason: str


class SQLOutput(BaseModel):
    sql: str = Field(description="生成的 SQL 语句")
    assumptions: List[str] = Field(description="假设条件")
    tables_used: List[str] = Field(description="使用到的表名")
    confidence: float = Field(description="信心分数 0.0-1.0")


class ErrorOutput(BaseModel):
    error_type: Literal["MISSING_COLUMN", "MISSING_TABLE", "WRONG_TABLE", "NON_FIXABLE"]
    analysis: str
    search_keywords: List[str] = Field(description="用于补搜的关键词列表")