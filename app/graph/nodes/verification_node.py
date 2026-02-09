# app/graph/nodes/verification_node.py

import json
import logging
from typing import Dict, Any, List
from langchain_core.messages import HumanMessage, SystemMessage
from app.core.state import AgentState
from app.core.llm import get_llm
from app.core.prompts import SQL_REFLECTION_PROMPT

logger = logging.getLogger(__name__)


def _truncate(s: str, n: int = 600) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _build_history_block(history: List[str], last_k: int = 3) -> str:
    """
    把最近 K 条历史反馈拼进 prompt。
    注意：传入的 history 列表项必须不包含 SQL 代码，只包含 Critic。
    """
    if not history:
        return "None (First Attempt)"

    # 取最后 K 条
    tail = history[-last_k:]
    # 再次截断单条长度，防止 feedback 废话太多撑爆上下文
    tail = [_truncate(x, 500) for x in tail]

    return "\n".join([f"- {x}" for x in tail])


async def verification_node(state: AgentState) -> Dict[str, Any]:
    """
    自省节点 (纯逻辑版):
    职责：
    1. 接收 SQL 和上下文。
    2. 调用 LLM 进行审查 (Verifier)。
    3. 输出客观的 PASS/FAIL 结果。
    4. 不做路由决策 (不关心 MAX_RETRIES)。
    """
    question = state.get("question", "")
    generated_sql = state.get("generated_sql", "")

    # 获取当前重试次数 (默认为 0)
    # 注意：这里只读取，不判断上限
    current_count = int(state.get("retry_count", 0))

    # 这一次尝试的编号 (用于日志和历史记录)
    attempt_number = current_count + 1

    # ---------------------------------------------------------
    # 1) 构造 Schema 上下文 (包含 Sample Values)
    # ---------------------------------------------------------
    retrieved_columns = state.get("retrieved_columns", [])
    schema_lines: List[str] = []

    if retrieved_columns:
        for col in retrieved_columns:
            # 兼容对象或字典格式
            if isinstance(col, dict):
                t = col.get("table", "UnknownTable")
                c = col.get("column", "UnknownCol")
                dtype = col.get("column_type", "UNKNOWN")
                samples = col.get("samples", []) or []
                # 有些时候 samples 可能是 sample_values
                if not samples:
                    samples = col.get("sample_values", []) or []
            else:
                # 如果是对象 (Pydantic model)
                t = getattr(col, "table", "UnknownTable")
                c = getattr(col, "column", "UnknownCol")
                dtype = getattr(col, "column_type", "UNKNOWN")
                samples = getattr(col, "samples", []) or []

            # --- 样本处理：去重、清洗、取 Top 5 ---
            sample_str = ""
            if samples:
                unique_vals: List[str] = []
                seen = set()
                for val in samples:
                    if val is None: continue
                    s_val = str(val).strip()
                    if not s_val or s_val in seen: continue
                    seen.add(s_val)

                    # 截断过长的样本值
                    display_val = s_val if len(s_val) <= 30 else s_val[:27] + "..."
                    unique_vals.append(f"'{display_val}'")

                if unique_vals:
                    # 限制显示数量，防止 Prompt 爆炸
                    sample_subset = unique_vals[:5]
                    sample_str = f" [Samples: {', '.join(sample_subset)}]"

            line = f"- {t}.{c} ({dtype}){sample_str}"
            schema_lines.append(line)

    if schema_lines:
        schema_context = "\n".join(schema_lines)
    else:
        schema_context = state.get("schema_str", "No schema info available.")[:3000]

    # ---------------------------------------------------------
    # 2) Business Rules (业务规则)
    # ---------------------------------------------------------
    business_rules = state.get("business_rules", [])
    rules_list: List[str] = []
    if business_rules:
        for r in business_rules:
            # 兼容字符串或字典
            txt = r.get("content") if isinstance(r, dict) else str(r)
            if txt and txt.strip():
                rules_list.append(f"- {txt.strip()}")
    rules_str = "\n".join(rules_list) if rules_list else "No additional business rules."

    # ---------------------------------------------------------
    # 3) 准备历史记录 (History Block)
    # ---------------------------------------------------------
    # 这里我们使用纯净历史，不包含上一轮错误的 SQL 代码，只包含 Critic
    history = state.get("feedback_history", []) or []
    history_block = _build_history_block(history, last_k=3)

    # ---------------------------------------------------------
    # 4) 调用 LLM 审查
    # ---------------------------------------------------------

    # 异常防御：如果是空 SQL，直接判负
    if not generated_sql or not generated_sql.strip():
        logger.warning(f"⚠️ [Verifier] Empty SQL detected (Attempt {attempt_number})")
        item = f"[Attempt {attempt_number}] FAIL: Empty SQL generated."
        return {
            "verified": False,
            "feedback": "No SQL generated.",
            "retry_count": current_count + 1,
            "feedback_history": [item]  # 追加历史
        }

    # 获取 LLM 实例
    llm = get_llm()

    # 填充 Prompt
    prompt = SQL_REFLECTION_PROMPT.format(
        question=question,
        sql=generated_sql,
        schema_context=schema_context,
        business_rules=rules_str,
        history_context=history_block
    )

    try:
        messages = [
            SystemMessage(
                content="You are a strict SQL compliance checker. Output raw JSON only with 'status' and 'feedback' keys."
            ),
            HumanMessage(content=prompt),
        ]

        logger.info(f"🧐 [Verifier] Reviewing SQL (Attempt {attempt_number})...")

        # 异步调用
        response = await llm.ainvoke(messages)
        content = (response.content or "").strip()

        # 清理 Markdown 代码块 (以防模型输出 ```json ... ```)
        if content.startswith("```"):
            content = content.replace("```json", "").replace("```", "").strip()

        # 解析 JSON
        result = json.loads(content)
        status = str(result.get("status", "FAIL")).upper()
        feedback = result.get("feedback", "No feedback provided.")
        feedback_short = _truncate(str(feedback), 800)

        # =========================================================
        # ⚖️ 判决时刻
        # =========================================================

        # === 场景 A: 验证通过 (PASS) ===
        if status == "PASS":
            logger.info(f"✅ [Verifier] SQL approved! (Attempt {attempt_number})")

            # 记录成功的历史 (可选)
            item = f"[Attempt {attempt_number}] STATUS: PASS"

            return {
                "verified": True,
                "feedback": "",  # 通过了就不需要 Critic 了
                "feedback_history": [item],
                # 注意：成功时不增加 retry_count，保持原样传递给后续步骤
            }

        # === 场景 B: 验证失败 (FAIL) ===
        logger.warning(f"❌ [Verifier] SQL Rejected: {feedback_short[:100]}...")

        # 记录失败的历史 (只包含 Critic，不包含 SQL，保持 Context 纯净)
        item = f"[Attempt {attempt_number}] FAIL: {feedback_short}"

        return {
            "verified": False,  # 诚实地标记为 False
            "feedback": feedback,  # 将批评意见传回 State，供 Generator 参考
            "retry_count": current_count + 1,  # 🔥 计数器 +1 (这是 Node 的职责)
            "feedback_history": [item],  # 追加历史
        }

    except Exception as e:
        logger.error(f"⚠️ [Verifier] Exception during LLM call: {e}")
        item = f"[Attempt {attempt_number}] EXCEPTION: {str(e)}"

        # 遇到异常通常判负，让 Router 决定是否强制执行或终止
        return {
            "verified": False,
            "feedback": f"Verifier Error: {str(e)}",
            "retry_count": current_count + 1,
            "feedback_history": [item]
        }