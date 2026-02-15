# app/graph/nodes/expand_node.py

import json
import re
from typing import Dict, Any

from app.core.state import AgentState, ExpandOutput
from app.core.prompts import CAPABILITY_EXPAND_PROMPT
from app.core.llm import get_llm
from app.core.logger import logger

llm = get_llm()

ALLOWED_CAPS = {
    "LOOKUP", "FILTER", "COMPARISON", "TIME_RANGE",
    "AGGREGATION", "GROUPING", "SORT", "TOPK_LIMIT", "JOIN",
}


def _clean_and_parse_json(text: str) -> Dict[str, Any]:
    """鲁棒 JSON 提取"""
    if not text:
        raise ValueError("Empty LLM response")

    # 去除 Markdown 代码块
    text = re.sub(r"```json", "", text, flags=re.IGNORECASE).replace("```", "").strip()

    # 提取 JSON 对象
    match = re.search(r"(\{.*\})", text, re.DOTALL)
    if match:
        text = match.group(1)

    # 修复常见 JSON 错误
    text = text.replace("，", ",").replace(""", '"').replace(""", '"')

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON Parse Error: {text[:100]}...\nError: {e}")


def _post_validate(out: ExpandOutput) -> ExpandOutput:
    """后处理：清洗数据"""

    # 1. Capabilities 清洗
    caps = [c for c in (out.capabilities or []) if c in ALLOWED_CAPS]
    out.capabilities = list(set(caps))

    # 2. 清洗 concepts
    if out.search_keywords.concepts:
        clean_concepts = []
        for group in out.search_keywords.concepts:
            # 去重 + 去空
            terms = list(dict.fromkeys([t.strip() for t in group.terms if t.strip()]))
            if terms:
                group.terms = terms
                clean_concepts.append(group)
        out.search_keywords.concepts = clean_concepts

    # 3. 清洗 values
    if out.search_keywords.values:
        clean_values = []
        for group in out.search_keywords.values:
            terms = list(dict.fromkeys([t.strip() for t in group.terms if t.strip()]))
            if terms:
                group.terms = terms
                clean_values.append(group)
        out.search_keywords.values = clean_values

    return out


async def expand_node(state: AgentState) -> dict:
    """
    🧠 能力理解与扩写节点
    """
    trace_id = state.get("trace_id", "unknown")
    question = state.get("question", "")

    try:
        # 构建 Prompt
        prompt = CAPABILITY_EXPAND_PROMPT.replace("{question}", question)

        # 调用 LLM
        resp = await llm.ainvoke(prompt)
        content = getattr(resp, "content", "")

        # 解析与校验
        parsed = _clean_and_parse_json(content)

        # 打印解析后的 JSON
        print("\n📄 [解析后的 JSON]:")
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
        print()

        out = ExpandOutput(**parsed)
        out = _post_validate(out)

    except Exception as e:
        logger.error(f"[Expand] Failed: {e}", extra={"trace_id": trace_id})
        print(f"❌ Expand 失败: {e}")
        out = ExpandOutput()

    return {"expand_data": out}


# ==========================================
# ⚡️ 独立测试入口
# ==========================================
if __name__ == "__main__":
    import asyncio
    import sys
    import os

    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))
    sys.path.append(project_root)


    async def expand():
        test_cases = [
            "帮我找一下价格在 500 到 1000 元之间的运动鞋，按价格从低到高排序",
            "查一下北京地区已发货的订单",
            "上个月 iPhone 15 的销量和销售额是多少",
        ]

        for question in test_cases:
            print(f"\n{'🧪' * 40}")
            print(f"测试问题: {question}")
            print(f"{'🧪' * 40}")

            mock_state = AgentState({"question": question})
            result = await expand_node(mock_state)


    asyncio.run(expand())