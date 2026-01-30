from pydantic import BaseModel
from typing import Any, Optional, Dict, List

class StandardResponse(BaseModel):
    success: bool
    message: str            # 这里的 message 对应 Analyst 的回复 或 错误信息
    data: Any = []          # 数据 payload (Rows)
    meta: Dict[str, Any] = {} # 元数据：trace_id, sql, execution_time, intent 等