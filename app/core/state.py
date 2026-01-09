from typing import List, Dict, Any, TypedDict, Literal, Optional
from pydantic import BaseModel, Field


class AgentState(TypedDict):
    # 基础字段
    trace_id: str
    question: str
    intent: str
    # 🔥 新增: 聊天记录 (格式: ["User: 北京销量", "AI: 100", "User: 那上海呢"])
    chat_history: List[str]

    # 召回层 (Retrieval Context)
    candidate_tables: List[Dict]

    # 生成层 (Generation Output)
    generated_sql: str
    sql_confidence: float
    # 🔥 新增字段: 记录模型真实的引用情况
    tables_used: List[str]  # 模型声称用到的表名
    assumptions: List[str]  # 模型做的业务假设 (如: "假设 status=1 是有效订单")

    # 错误处理层
    validation_error: Optional[str]
    error_type: Optional[str]
    repair_keywords: List[str]

    retry_count: int
    final_result: Any


# --- LLM 输出结构 (保持不变) ---
class SQLOutput(BaseModel):
    sql: str = Field(description="生成的 SQL 语句")
    assumptions: List[str] = Field(description="假设条件")
    tables_used: List[str] = Field(description="使用到的表名")
    confidence: float = Field(description="信心分数 0.0-1.0")


class ErrorOutput(BaseModel):
    # 🔥 1. 增加 "SYNTAX_ERROR" 选项
    error_type: Literal["MISSING_COLUMN", "MISSING_TABLE", "WRONG_TABLE", "SYNTAX_ERROR", "NON_FIXABLE"]
    analysis: str
    search_keywords: List[str] = Field(description="用于补搜的关键词")

# 🔥 新增: 意图识别的输出结构
class IntentOutput(BaseModel):
    intent: Literal["data_query", "sensitive", "non_data"] = Field(
        description="用户意图分类: data_query(查数据), sensitive(敏感信息), non_data(闲聊)"
    )

