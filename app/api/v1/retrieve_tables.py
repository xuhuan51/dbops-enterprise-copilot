import time
import uuid
import json
from typing import Optional, List, Dict, Any

from fastapi import APIRouter
from pydantic import BaseModel

# 引入刚才定义的 LLM 工具
from app.core.llm import chat_completion
# 引入你之前的 Milvus 检索函数 (假设在 app.modules.retrieval)
from app.modules.retrieval.schema_retriever import retrieve_tables

router = APIRouter(prefix="/api/v1", tags=["retrieve"])


# --- 1. Pydantic 数据模型 ---
class Filters(BaseModel):
    allowed_dbs: Optional[List[str]] = None
    domain: Optional[str] = None


class RetrieveReq(BaseModel):
    user_id: str
    query: str
    topk: int = 5  # Agent 模式下不需要太多，5个足够决策
    filters: Optional[Filters] = None


# --- 2. 辅助工具函数 ---
def _loads_list(s: Any) -> List[str]:
    """安全解析存储在数据库里的 JSON 字符串列表"""
    if s is None: return []
    if isinstance(s, list): return s
    if isinstance(s, str):
        try:
            x = json.loads(s)
            return x if isinstance(x, list) else []
        except:
            return []
    return []


# --- 3. 核心 Agent：LLM 澄清决策器 ---
def clarify_with_llm(query: str, candidates: List[Dict]) -> Dict[str, Any]:
    """
    利用 LLM 判断检索结果是否具有歧义，并生成专业的反问话术
    """
    # 1. 判空处理
    if not candidates:
        return {
            "need_clarify": True,
            "clarify_question": "抱歉，未找到相关业务表，请补充更多业务细节（如业务域、关键字段）。",
            "reason": "No candidates found"
        }

    # 2. 构造 Prompt，将 Top-3 的核心元数据发给 LLM 评估
    # 只取前3个，减少Token消耗，因为通常干扰项就在前几名
    top_n = candidates[:3]
    candidate_info = ""
    for idx, c in enumerate(top_n):
        metrics = c['features'].get('metric_cols', [])
        comment = c.get('evidence', '') or c.get('full_name', '')
        candidate_info += f"候选表{idx + 1}: {c['full_name']} (注释/匹配证据: {comment}) | 包含指标: {metrics}\n"

    prompt = f"""
    【角色任务】
    你是一个数据分析专家。用户想查询："{query}"。
    系统检索到了以下最相关的数据库表（按相关性排序）：
    {candidate_info}

    【判断逻辑】
    1. **直接通过**：如果"候选表1"明显优于其他表，且完美覆盖用户需求，不需要澄清。
    2. **需要澄清**：如果前两名表业务含义极其相近（例如：只有状态不同、只有统计口径不同），且用户问题模糊，必须反问。
    3. **无结果**：如果所有候选表都毫不相关，请告知用户无法回答。

    【输出要求】
    请直接返回合法的 JSON 字符串，不要包含 Markdown 格式：
    {{
        "need_clarify": true/false,
        "clarify_question": "如果为true，请在此生成一句简短专业的反问，引导用户区分这两个表；如果为false，留空",
        "reason": "简述判断理由"
    }}
    """

    # 3. 调用 LLM 并解析结果
    try:
        raw_res = chat_completion(prompt)
        # 清理可能的 markdown 标记
        raw_res = raw_res.replace("```json", "").replace("```", "")
        decision = json.loads(raw_res)
        return decision
    except Exception as e:
        print(f"Agent 决策解析失败: {e}, Raw: {raw_res}")
        # 兜底策略：如果 LLM 挂了，回退到规则（Gap 策略）
        gap = candidates[0]['score'] - candidates[1]['score'] if len(candidates) > 1 else 1.0
        return {
            "need_clarify": gap < 0.05,
            "clarify_question": "查询结果存在多个相似表，请提供更详细的描述。" if gap < 0.05 else "",
            "reason": "Fallback due to LLM error"
        }


# --- 4. API 路由逻辑 ---
@router.post("/retrieve_tables")
def retrieve(req: RetrieveReq):
    trace_id = str(uuid.uuid4())
    t0 = time.time()

    # A. 执行向量检索
    raw_items = retrieve_tables(req.query, req.topk)

    # B. 应用过滤器 (Filters)
    items = raw_items
    filters_applied = []
    if req.filters:
        if req.filters.allowed_dbs:
            allow = set(req.filters.allowed_dbs)
            items = [x for x in items if x.get("db") in allow]
            filters_applied.append(f"allowed_dbs: {req.filters.allowed_dbs}")
        if req.filters.domain:
            items = [x for x in items if x.get("domain") == req.filters.domain]
            filters_applied.append(f"domain: {req.filters.domain}")

    # C. 格式化候选结果 (Standardization)
    candidates = []
    for i, x in enumerate(items[:req.topk], 1):
        candidates.append({
            "rank": i,
            "full_name": x.get("full_name"),
            "db": x.get("db"),
            "table": x.get("table"),
            "score": float(x.get("score", 0.0)),
            # evidence 是你向量库里存的那段长文本
            "evidence": x.get("text", "")[:200] + "...",
            "features": {
                "join_keys": _loads_list(x.get("join_keys")),
                "time_cols": _loads_list(x.get("time_cols")),
                "metric_cols": _loads_list(x.get("metric_cols")),
            },
            "governance": {
                "owner": x.get("owner", ""),
                "app": x.get("app", "")
            }
        })

    # D. Agent 介入决策 (The "Brain")
    # 只有当确实检索到了东西，才让 LLM 去判断是否需要澄清
    if candidates:
        decision = clarify_with_llm(req.query, candidates)
    else:
        decision = {
            "need_clarify": True,
            "clarify_question": "未检索到相关表，请检查关键词或业务域。",
            "reason": "No candidates"
        }

    latency_ms = int((time.time() - t0) * 1000)

    return {
        "trace_id": trace_id,
        "success": True,
        "data": {
            "candidates": candidates,
            "agent_decision": decision,  # 前端根据这个字段弹窗反问用户
            "meta": {
                "latency_ms": latency_ms,
                "filters_applied": filters_applied
            }
        }
    }