# app/core/llm.py
from __future__ import annotations
from functools import lru_cache
from langchain_openai import ChatOpenAI

from app.core.config import settings

@lru_cache(maxsize=8)
def get_llm(purpose: str = "default") -> ChatOpenAI:
    """
    purpose: 用于区分不同节点的模型/温度/token
    目前先给同配置，后续你想 router 用快模型、generate 用强模型，直接在这里分叉。
    """
    # 你也可以根据 purpose 做不同配置
    temperature = 0.0
    max_tokens = 2048

    return ChatOpenAI(
        model=settings.LLM_MODEL,
        temperature=temperature,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        max_tokens=max_tokens,
    )

def get_router_llm() -> ChatOpenAI:
    return get_llm("router")

def get_generate_llm() -> ChatOpenAI:
    return get_llm("generate")

def get_reflection_llm() -> ChatOpenAI:
    return get_llm("reflection")

def get_misc_llm() -> ChatOpenAI:
    return get_llm("misc")
