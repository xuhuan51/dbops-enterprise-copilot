import json
import re
import time
import uuid
from typing import List, Dict, Any, Optional, Set

from fastapi import APIRouter
from pydantic import BaseModel, Field

# 引入核心组件
from app.core.llm import chat_completion
from app.core.prompts import RETRIEVAL_JUDGE_TEMPLATE
from app.core.domain_config import get_relevant_rules, ECOMMERCE_EXAMPLES
from app.modules.retrieval.schema_retriever import retrieve_tables

router = APIRouter(prefix="/api/v1", tags=["retrieve"])

# =========================
# 0. 配置参数
# =========================
PREFETCH_K = 500  # 向量检索召回数量
GATE_CANDIDATE_K = 20  # 进入门禁检查的候选表数量
FINAL_TOP_K = 5  # 最终返回数量

MIN_SCORE_THRESHOLD = 0.45
MAX_HOPS = 1  # 最大重试次数


# =========================
# 1. 核心数据结构 (Pydantic)
# =========================

class Filters(BaseModel):
    allowed_dbs: Optional[List[str]] = None
    domain: Optional[str] = None


class RetrieveReq(BaseModel):
    user_id: str
    query: str
    topk: int = FINAL_TOP_K
    filters: Optional[Filters] = None


# ✅ 新增：LLM 提取的需求结构
class QueryNeeds(BaseModel):
    intent: str = Field(..., description="data_query | non_data | sensitive")
    must_have: Dict[str, List[str]] = Field(...,
                                            description="必须具备的能力，如 {'entity': ['user'], 'dimension': ['time']}")
    search_keywords: List[str] = Field(default=[], description="用于重搜的关键词")


# =========================
# 2. 核心组件：Needs Extraction (需求提取)
# =========================
def extract_query_needs(query: str) -> QueryNeeds:
    """
    让 LLM 分析用户Query，提取硬性需求 (Must Have)。
    不涉及具体表名，只涉及业务能力。
    """
    prompt = f"""
你是一个数据分析师。请分析用户问题，提取查询所需的【核心数据能力】。

User Query: "{query}"

请输出 JSON，包含：
1. intent: "data_query" (正常查询) | "non_data" (闲聊/写诗) | "sensitive" (查工资/密码)
2. must_have: 必须具备的字段能力，从以下类别中选：
   - "entity": 需要的主体 (user, order, sku, supplier, activity...)
   - "dimension": 需要的过滤/分组维度 (time, region, channel, status...)
   - "metric": 需要的统计指标 (amount, qty, count, duration...)
   - "join": 需要跨表关联 (join)
3. search_keywords: 如果当前检索失败，你建议用什么关键词去重搜？(提供3-5个同义词/业务词)

示例：
Query: "统计上个月北京用户的注册量"
JSON:
{{
    "intent": "data_query",
    "must_have": {{
        "entity": ["user"],
        "dimension": ["time", "region"],
        "metric": ["count"]
    }},
    "search_keywords": ["用户基础信息", "注册时间", "create_time", "地区"]
}}
"""
    try:
        raw = chat_completion(prompt)
        # 简单的 JSON 提取
        json_str = re.search(r"\{[\s\S]*\}", raw).group(0)
        data = json.loads(json_str)
        return QueryNeeds(**data)
    except Exception as e:
        print(f"⚠️ Needs Extraction Failed: {e}")
        # 兜底：假设是普通查询，无强制约束
        return QueryNeeds(intent="data_query", must_have={}, search_keywords=[])


# =========================
# 3. 核心组件：Capability Gate (硬门禁)
# =========================
def check_capabilities(needs: QueryNeeds, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    代码逻辑门禁：检查候选表是否覆盖了 must_have 的能力。
    返回: {"pass": bool, "missing": str}
    """
    # 1. 熔断检查
    if needs.intent in ["non_data", "sensitive"]:
        return {"pass": False, "action": "ASK_USER", "reason": f"Intent is {needs.intent}"}

    if not candidates:
        return {"pass": False, "action": "REWRITE", "reason": "No candidates found"}

    # 2. 收集所有候选表的能力并集
    # 这里的 features 是从 Milvus 读出来的 feat_xxx_cols JSON 字符串解析后的列表
    all_caps = {
        "entity": set(),  # 从 domain 推断，或 features 里有 uid/oid
        "dimension": set(),
        "metric": set()
    }

    for c in candidates:
        # 解析 features (假设已转为 dict/list)
        feats = c.get("features", {})

        # Time Dimension
        if feats.get("time_cols"):
            all_caps["dimension"].add("time")

        # Region/Status 等其他维度 (可以从 columns 里简单的正则判断，或离线已打标)
        # 这里简化：如果有 domain=user，默认有 user entity
        domain = c.get("domain", "")
        if domain == "user": all_caps["entity"].add("user")
        if domain == "trade": all_caps["entity"].add("order")
        if domain == "scm": all_caps["entity"].add("sku")

        # Metrics
        if feats.get("metric_cols"):
            all_caps["metric"].add("metric")  # 只要有指标列就算有 metric 能力
            # 也可以更细：if "amount" in feats['metric_cols']: ...

    # 3. 对照检查
    missing = []

    # 检查维度 (Time)
    if "time" in needs.must_have.get("dimension", []) and "time" not in all_caps["dimension"]:
        missing.append("缺少[时间]维度字段")

    # 检查实体 (User) - 这是一个强校验示例
    if "user" in needs.must_have.get("entity", []) and "user" not in all_caps["entity"]:
        missing.append("缺少[用户]相关表")

    # 4. 判定
    if missing:
        return {
            "pass": False,
            "action": "REWRITE",
            "reason": f"Gate拦截: {','.join(missing)}",
            "missing_caps": missing
        }

    return {"pass": True, "action": "PASS"}


# =========================
# 4. 辅助函数 (聚合 & 搜索)
# =========================
def _safe_json_load(s):
    if isinstance(s, list): return s
    try:
        return json.loads(s)
    except:
        return []


def aggregate_shards_and_parse(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    1. 分表聚合 (逻辑同前)
    2. 解析 Milvus 存的 JSON 字符串 (feat_time_cols) 为列表
    """
    # ... (分表聚合逻辑与之前相同，略微简化展示) ...
    # 假设 items 已经是 retrieve_tables 返回的 raw data

    # 这里重点是解析 features
    for it in items:
        # Milvus 里的 feat_time_cols 是字符串，转回 list
        it["features"] = {
            "time_cols": _safe_json_load(it.get("feat_time_cols", "[]")),
            "metric_cols": _safe_json_load(it.get("feat_metric_cols", "[]")),
            "join_keys": _safe_json_load(it.get("feat_join_keys", "[]"))
        }
    return items  # 这里应保留 aggregate_shards 的去重逻辑


def search_by_keywords(keywords: List[str]) -> List[Dict[str, Any]]:
    # 调用底层的 retrieve_tables
    # 实际应包含去重逻辑
    results = []
    for kw in keywords:
        res = retrieve_tables(kw, topk=50)  # 扩大搜索
        if res: results.extend(res)
    return aggregate_shards_and_parse(results)


# =========================
# 5. 原有的 Judge (用于 Gate 通过后的精选)
# =========================
def llm_judge_final(query: str, candidates: List[Dict[str, Any]]) -> Dict:
    # ... (代码与之前一致：动态 prompt + 规则) ...
    # 略写，直接调用之前的逻辑
    return {"status": "PASS", "selected_tables": [c['logical_table'] for c in candidates[:3]]}


# =========================
# 6. 主 API 入口
# =========================
@router.post("/retrieve_tables_gate")
def retrieve_tables_with_gate(req: RetrieveReq):
    trace_id = str(uuid.uuid4())
    t0 = time.time()

    # --- Step 1: 初始检索 (Vector Recall) ---
    raw_1 = retrieve_tables(req.query, topk=PREFETCH_K) or []
    # 聚合分表 & 解析 features JSON
    candidates_pool = aggregate_shards_and_parse(raw_1)

    # 截取 Top K 进入门禁
    candidates_gate = candidates_pool[:GATE_CANDIDATE_K]

    # --- Step 2: 需求提取 (LLM) ---
    needs = extract_query_needs(req.query)

    # --- Step 3: Capability Gate (Python Logic) ---
    gate_result = check_capabilities(needs, candidates_gate)

    gate_action = gate_result["action"]
    final_pool = candidates_gate

    # --- Step 4: 处理 Gate 结果 ---
    if gate_action == "ASK_USER":
        return {
            "success": True,
            "agent_decision": {"need_clarify": True, "reason": gate_result["reason"]}
        }

    elif gate_action == "REWRITE":
        # 🔴 触发重搜！
        print(f"🔄 Gate blocked: {gate_result['reason']}. Rewriting...")

        # 使用 LLM 生成的 keywords 重搜
        new_kws = needs.search_keywords
        if new_kws:
            raw_2 = search_by_keywords(new_kws)
            # 合并结果 (去重)
            seen = {c.get("full_name") for c in candidates_pool}
            for r in raw_2:
                if r.get("full_name") not in seen:
                    candidates_pool.append(r)
                    seen.add(r.get("full_name"))

            # 重新排序 (简单按原有分数或置顶新结果)
            final_pool = candidates_pool[:GATE_CANDIDATE_K]  # 再次截取
        else:
            # 没生成关键词，无奈 Pass
            pass

    # --- Step 5: Final Judge (LLM Selection) ---
    # 现在 final_pool 里应该包含了补搜回来的表
    # 这里调用之前的 judge 逻辑做最后的清洗
    # judge_res = llm_judge(req.query, final_pool) ...

    # (为了演示，直接返回 final_pool)
    return {
        "trace_id": trace_id,
        "success": True,
        "retrieval": {
            "latency_ms": int((time.time() - t0) * 1000),
            "gate_result": gate_result,
            "needs": needs.dict(),
            "candidates": final_pool[:req.topk]
        }
    }