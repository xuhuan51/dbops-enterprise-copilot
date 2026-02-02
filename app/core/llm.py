# app/core/llm.py
from __future__ import annotations
from functools import lru_cache
from typing import Optional
from langchain_openai import ChatOpenAI
from app.core.config import settings


@lru_cache(maxsize=10)
def get_llm(model_name: Optional[str] = None, temperature: float = 0.0) -> ChatOpenAI:
    """
    获取 LLM 实例的核心工厂方法 (带缓存)

    :param model_name: 指定模型名称。如果不传(None)，则自动使用 settings.LLM_MODEL
    :param temperature: 温度，默认 0
    """

    # 1. 确定最终使用的模型名
    # 如果调用方传了 model_name，就用传进来的；否则用配置文件里的默认值
    target_model = model_name if model_name else settings.LLM_MODEL

    # 2. 创建并返回实例
    return ChatOpenAI(
        model=target_model,
        temperature=temperature,
        api_key=settings.LLM_API_KEY,  # 比如 "ollama"
        base_url=settings.LLM_BASE_URL,  # 比如 "http://127.0.0.1:11434/v1"
        max_tokens=4096,  # 设大一点，防止生成 SQL 被截断
        streaming=False
    )
