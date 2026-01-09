import datetime
import threading
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel
from pymilvus import Collection, connections, utility
from sentence_transformers import SentenceTransformer, CrossEncoder

from app.core.config import settings
from app.core.logger import logger
from app.modules.sql.executor import append_event  # 记得引入日志记录

router = APIRouter(tags=["RAG"])

# =========================
# Config
# =========================
MILVUS_HOST = settings.MILVUS_HOST
MILVUS_PORT = settings.MILVUS_PORT
COLLECTION_NAME = settings.MILVUS_COLLECTION

EMBED_MODEL_NAME = settings.EMBED_MODEL
RERANK_MODEL_NAME = settings.RERANK_MODEL

# Recall / Rerank / Final defaults
DEFAULT_TOP_K_RECALL = int(getattr(settings, "TOP_K_RECALL", 100))
DEFAULT_TOP_K_RERANK = int(getattr(settings, "TOP_K_RERANK", 20))
DEFAULT_TOP_K_FINAL = int(getattr(settings, "TOP_K_FINAL", 5))

RERANK_THRESHOLD = float(getattr(settings, "RERANK_THRESHOLD", 0.01))
SENSITIVE_KEYWORDS = ["工资", "薪水", "底薪", "密码", "密钥", "token", "salary", "password"]

# =========================
# Singletons + Locks
# =========================
_embed_model: Optional[SentenceTransformer] = None
_rerank_model: Optional[CrossEncoder] = None
_collection_loaded = False

_model_lock = threading.Lock()
_milvus_lock = threading.Lock()

# 专门用于跑模型推理的线程池
_executor = ThreadPoolExecutor(max_workers=3)


# =========================
# Core Logic Functions
# =========================

def get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        with _model_lock:
            if _embed_model is None:
                logger.info(f"🧠 Loading Embedding Model: {EMBED_MODEL_NAME}...")
                _embed_model = SentenceTransformer(EMBED_MODEL_NAME)
    return _embed_model


def get_rerank_model() -> Optional[CrossEncoder]:
    global _rerank_model
    if _rerank_model is None:
        with _model_lock:
            if _rerank_model is None:
                logger.info(f"🧠 Loading Rerank Model: {RERANK_MODEL_NAME}...")
                try:
                    _rerank_model = CrossEncoder(RERANK_MODEL_NAME)
                except Exception as e:
                    logger.warning(f"⚠️ Rerank model load failed: {e}. Fallback to None.")
                    _rerank_model = None
    return _rerank_model


def ensure_milvus_connection() -> bool:
    global _collection_loaded
    with _milvus_lock:
        try:
            if not connections.has_connection("default"):
                connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)
        except Exception as e:
            logger.error(f"❌ Milvus Connect Error: {e}")
            return False

        if not _collection_loaded:
            try:
                if not utility.has_collection(COLLECTION_NAME):
                    logger.error(f"❌ Collection '{COLLECTION_NAME}' not found! Please run ETL first.")
                    return False
                logger.info(f"🔄 Loading collection '{COLLECTION_NAME}' into memory...")
                Collection(COLLECTION_NAME).load()
                _collection_loaded = True
                logger.info(f"✅ Collection '{COLLECTION_NAME}' loaded.")
            except Exception as e:
                logger.error(f"❌ Collection load failed: {e}", exc_info=True)
                return False
    return True


# 辅助函数：在线程池中运行 Embedding (CPU密集)
def _run_embedding(model, text):
    return model.encode([text], normalize_embeddings=True)[0].tolist()


# 辅助函数：在线程池中运行 Rerank (CPU密集)
def _run_rerank(model, pairs):
    return model.predict(pairs, batch_size=32, show_progress_bar=False)


# 🔥 改为 async def
async def retrieve_tables(query: str, topk: int = 5, trace_id: str = "N/A") -> List[Dict[str, Any]]:
    # 1. 硬规则过滤
    for kw in SENSITIVE_KEYWORDS:
        if kw in query:
            logger.warning(f"🛑 [Security] Query contains sensitive keyword '{kw}'. Blocked.")
            return []

    # 调用异步的高级检索
    return await retrieve_tables_advanced(
        query=query,
        top_k_recall=max(topk * 10, 50),
        top_k_rerank=DEFAULT_TOP_K_RERANK,
        top_k_final=topk,
        trace_id=trace_id # 🔥 记得把 trace_id 传给下面
    )


# 🔥 改为 async def
async def retrieve_tables_advanced(
        query: str,
        top_k_recall: int = DEFAULT_TOP_K_RECALL,
        top_k_rerank: int = DEFAULT_TOP_K_RERANK,
        top_k_final: int = DEFAULT_TOP_K_FINAL,
        trace_id: str = "N/A"  # 建议加上 trace_id 参数
) -> List[Dict[str, Any]]:
    if not query:
        return []

    # Milvus 连接检查 (这一步很快，可以同步)
    if not ensure_milvus_connection():
        return []

    t0 = time.perf_counter()
    logger.info(f"🔍 [Retrieve] Start searching for: '{query}'")

    # -------- 1) Recall (Milvus) --------
    try:
        loop = asyncio.get_running_loop()
        col = Collection(COLLECTION_NAME)
        model = get_embed_model()

        # 🔥 异步执行 Embedding (防止阻塞主线程)
        embed_t0 = time.perf_counter()
        query_vec = await loop.run_in_executor(_executor, _run_embedding, model, query)
        embed_ms = (time.perf_counter() - embed_t0) * 1000.0

        # Milvus 搜索 (IO操作，目前 pymilvus 只有同步版，暂且这样跑，或者也放 executor)
        search_params = {"metric_type": "IP", "params": {"nprobe": 10}}
        milvus_t0 = time.perf_counter()

        # 将 Milvus 搜索放入线程池
        def _search_milvus():
            return col.search(
                data=[query_vec],
                anns_field="embedding",
                param=search_params,
                limit=top_k_recall,
                output_fields=["db", "logical_table", "text"],
            )

        res = await loop.run_in_executor(_executor, _search_milvus)
        milvus_ms = (time.perf_counter() - milvus_t0) * 1000.0

        candidates: List[Dict[str, Any]] = []
        seen = set()

        for hits in res:
            for hit in hits:
                entity = hit.entity
                full_name = f"{entity.get('db')}.{entity.get('logical_table')}"
                if full_name in seen:
                    continue
                seen.add(full_name)
                candidates.append(
                    {
                        "score": float(hit.score),
                        "db": entity.get("db"),
                        "logical_table": entity.get("logical_table"),
                        "full_name": full_name,
                        "text": entity.get("text") or "",
                    }
                )

        if not candidates:
            logger.info("✅ [Retrieve] No candidates from Milvus.")
            return []

        candidates.sort(key=lambda x: x["score"], reverse=True)

    except Exception as e:
        logger.error(f"❌ Milvus Search Failed: {e}", exc_info=True)
        return []

    # -------- 2) Rerank --------
    reranker = get_rerank_model()
    candidates_final = candidates

    if reranker is not None:
        rerank_pool = candidates[: max(1, min(top_k_rerank, len(candidates)))]
        try:
            rerank_t0 = time.perf_counter()
            pairs = [[query[:256], c["text"][:512]] for c in rerank_pool]

            # 🔥 异步执行 Rerank 推理 (CPU密集)
            scores = await loop.run_in_executor(_executor, _run_rerank, reranker, pairs)
            rerank_ms = (time.perf_counter() - rerank_t0) * 1000.0

            for i, c in enumerate(rerank_pool):
                c["rerank_score"] = float(scores[i])

            rerank_pool.sort(key=lambda x: x["rerank_score"], reverse=True)

            # Cutoff
            top1 = rerank_pool[0].get("rerank_score", -999.0)
            if top1 < RERANK_THRESHOLD:
                logger.info(f"🛑 [Retrieve] Cutoff: top1 {top1:.3f} < threshold {RERANK_THRESHOLD}. Return [].")
                # 这里也可以记一条 cutoff 日志
                return []

            candidates_final = rerank_pool

        except Exception as e:
            logger.error(f"⚠️ [Rerank Failed] {e}. Fallback to vector score.", exc_info=True)

    # -------- 4) Final output --------
    final_results = candidates_final[: max(0, min(top_k_final, len(candidates_final)))]
    total_ms = (time.perf_counter() - t0) * 1000.0

    # 1. 提取表名列表 (方便查看)
    table_names = [t["logical_table"] for t in final_results]

    # 🔥 修改点：直接把表名打印在控制台！
    logger.info(f"✅ [Retrieve] Found {len(final_results)} tables: {table_names} | ms={total_ms:.0f}")

    # 2. 写入审计日志 (events.jsonl)
    try:
        append_event({
            "trace_id": trace_id,
            "user_id": "system_retriever",
            "route": "RETRIEVE",
            "sql": query,
            "latency_ms": int(total_ms),
            "truncated": False,
            "error": None,
            "result_summary": table_names,  # 这里也会记录
            "ts_iso": datetime.utcnow().isoformat(),
        })
    except Exception:
        pass

    return final_results


# =========================
# API Endpoints
# =========================

class RetrieveRequest(BaseModel):
    query: str
    top_k: int = 5


# 🔥 路由函数也要改成 async def
@router.post("/retrieve")
async def api_retrieve_tables(req: RetrieveRequest):
    results = await retrieve_tables(req.query, topk=req.top_k)
    return {
        "query": req.query,
        "count": len(results),
        "results": results
    }