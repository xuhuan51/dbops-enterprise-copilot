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
        text = text[s: e + 1]

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

    # 3. 🔥🔥🔥 【修改】: 适配结构化关键词 (List[KeywordItem])
    if out.search_keywords:
        dedup_kw = []
        seen_kw = set()

        for item in out.search_keywords:
            # 兼容性防御：如果 LLM 偶尔发疯返回了字符串，尝试容错（虽然 Prompt 强约束了）
            if isinstance(item, str):
                continue

                # Pydantic 对象访问属性
            raw_kw = getattr(item, "keyword", "").strip()
            raw_type = getattr(item, "type", "CONCEPT").upper()

            if not raw_kw:
                continue

            # 逻辑：VALUE 类型保留原大小写（可能对精确匹配重要），CONCEPT 类型转小写
            # 但为了去重 key，统一用小写判断
            dedup_key = (raw_kw.lower(), raw_type)

            if dedup_key not in seen_kw:
                seen_kw.add(dedup_key)

                # 规范化数据回写
                item.keyword = raw_kw
                item.type = raw_type
                dedup_kw.append(item)

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

        # 🔥 打印关键词（展示 Type，方便调试）
        print("   - 🔑 Keywords:")
        for k in out.search_keywords:
            # k 是 KeywordItem 对象
            print(f"      • [{k.type}] {k.keyword}")

    except Exception as e:
        logger.error(f"[Expand] Failed: {e}", extra={"trace_id": trace_id})
        print(f"❌ Expand 失败，启用降级策略: {e}")
        out = CapabilityExpandOutput()

    # =======================================================
    # 更新 State
    # =======================================================
    if intent_data is not None:
        intent_data.capabilities = out.capabilities
        intent_data.semantic_hints = out.semantic_hints

        # ✅ 传递结构化对象列表 (List[KeywordItem])
        intent_data.search_keywords = out.search_keywords

        # ✅ 兼容旧版 schema_query (只提取 keyword 拼接成字符串)
        # 避免 join 对象导致报错
        kw_str_list = [k.keyword for k in out.search_keywords]
        intent_data.schema_query = " ".join(kw_str_list)

    print(f"{'=' * 80}\n")
    return {"intent_data": intent_data}