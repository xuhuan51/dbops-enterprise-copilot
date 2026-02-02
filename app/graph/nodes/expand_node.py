# app/graph/nodes/expand_node.py

import json
from typing import Dict, Any

from app.core.state import AgentState, CapabilityExpandOutput
from app.core.prompts import CAPABILITY_EXPAND_PROMPT
from app.core.llm import get_llm
from app.core.logger import logger

llm = get_llm()

ALLOWED_CAPS = {
    "LOOKUP",
    "FILTER",
    "COMPARISON",
    "TIME_RANGE",
    "AGGREGATION",
    "GROUPING",
    "SORT",
    "TOPK_LIMIT",
    "JOIN",
}


def _clean_and_parse_json(text: str) -> Dict[str, Any]:
    """
    鲁棒 JSON 提取：
    1) 去 ```json``` 包裹
    2) 截取最外层 { ... }
    3) json.loads
    """
    if text is None:
        raise ValueError("LLM returned None content")

    text = text.strip()

    if "```" in text:
        if "```json" in text:
            try:
                text = text.split("```json", 1)[1].split("```", 1)[0].strip()
            except Exception:
                text = text.split("```", 1)[1].split("```", 1)[0].strip()
        else:
            try:
                text = text.split("```", 1)[1].split("```", 1)[0].strip()
            except Exception:
                pass

    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        text = text[s : e + 1]

    return json.loads(text)


def _render_prompt_safe(template: str, question: str) -> str:
    # 避免 question 里出现 { } 破坏模板替换
    safe_question = (question or "").replace("{", "(").replace("}", ")")
    return template.replace("{question}", safe_question)


def _post_validate(out: CapabilityExpandOutput) -> CapabilityExpandOutput:
    # 1. 过滤非法 capability (保持不变)
    caps = [c for c in (out.capabilities or []) if c in ALLOWED_CAPS]
    seen = set()
    dedup = []
    for c in caps:
        if c not in seen:
            seen.add(c)
            dedup.append(c)
    out.capabilities = dedup

    # 2. semantic_hints 兜底 (保持不变)
    if out.semantic_hints is None:
        out.semantic_hints = CapabilityExpandOutput().semantic_hints
    if out.semantic_hints.filter_hints is None:
        out.semantic_hints.filter_hints = []

    # 3. 🔥🔥🔥 【新增】: search_keywords 清洗 (转小写 + 去重)
    if out.search_keywords:
        # 过滤空字符串，转小写
        cleaned_kws = [k.strip().lower() for k in out.search_keywords if k and k.strip()]
        # 去重但保持顺序
        seen_kw = set()
        dedup_kw = []
        for k in cleaned_kws:
            if k not in seen_kw:
                seen_kw.add(k)
                dedup_kw.append(k)
        out.search_keywords = dedup_kw
    else:
        out.search_keywords = []

    return out


async def expand_node(state: AgentState):
    trace_id = state.get("trace_id", "N/A")
    question = state.get("question", "") or ""
    intent_data = state.get("intent_data")

    print(f"\n{'=' * 30} [💥 透视: EXPAND (Capability+Hints)] {'=' * 30}")

    try:
        prompt = _render_prompt_safe(CAPABILITY_EXPAND_PROMPT, question)
        resp = await llm.ainvoke(prompt)
        content = getattr(resp, "content", None)

        parsed = _clean_and_parse_json(content)
        out = CapabilityExpandOutput(**parsed)
        out = _post_validate(out)

        print("🧠 [理解结果]:")
        print(f"   - 🧩 Capabilities: {out.capabilities}")
        print(f"   - 🎯 Hints: {out.semantic_hints}")
        # 🔥 打印关键词，方便调试
        print(f"   - 🔑 Keywords: {out.search_keywords}")

    except Exception as e:
        logger.error(f"[Expand] Failed: {e}", extra={"trace_id": trace_id})
        print(f"❌ Expand 失败，启用降级策略: {e}")
        out = CapabilityExpandOutput()

    # =======================================================
    # 更新 State（Option A：不生成 schema_query）
    # =======================================================
    if intent_data is not None:
        intent_data.capabilities = out.capabilities
        intent_data.semantic_hints = out.semantic_hints

        # 🔥🔥🔥 【新增】: 将关键词存入 RouterOutput，传递给下游 Retriever
        intent_data.search_keywords = out.search_keywords

        # (可选) 如果你的老代码还在用 schema_query，可以顺便生成一下兼容
        intent_data.schema_query = " ".join(out.search_keywords)

    print(f"{'=' * 80}\n")
    return {"intent_data": intent_data}
