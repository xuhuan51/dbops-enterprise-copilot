import threading
import time
from typing import List, Dict, Any, Optional

from pymilvus import Collection, connections, utility
from sentence_transformers import SentenceTransformer, CrossEncoder

from app.core.config import settings
from app.core.logger import logger

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

# ✅ Rerank cutoff threshold (match your observed scale ~0.xx)
# - Normal queries: top1 often 0.05~0.25+
# - Irrelevant queries: top1 often near 0.0 (or lower depending on model)
RERANK_THRESHOLD = float(getattr(settings, "RERANK_THRESHOLD", 0.01))
SENSITIVE_KEYWORDS = ["工资", "薪水", "底薪", "密码", "密钥", "token", "salary", "password"]
# ✅ Gap is for confidence logging / downstream decision only (NOT hard cutoff)
RERANK_WARN_GAP = float(getattr(settings, "RERANK_WARN_GAP", 0.03))

# =========================
# Singletons + Locks
# =========================
_embed_model: Optional[SentenceTransformer] = None
_rerank_model: Optional[CrossEncoder] = None
_collection_loaded = False

_model_lock = threading.Lock()
_milvus_lock = threading.Lock()


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
    """
    Ensure Milvus connected and collection loaded once.
    Return False on failure so caller can fail-fast.
    """
    global _collection_loaded

    with _milvus_lock:
        try:
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


# =========================
# Public API
# =========================
def retrieve_tables(query: str, topk: int = 5) -> List[Dict[str, Any]]:
    """
    Simple entry: recall 10x, rerank DEFAULT_TOP_K_RERANK, final = topk
    """
    # 1. 硬规则过滤 (Circuit Breaker)
    for kw in SENSITIVE_KEYWORDS:
        if kw in query:
            logger.warning(f"🛑 [Security] Query contains sensitive keyword '{kw}'. Blocked.")
            return []

    return retrieve_tables_advanced(
        query=query,
        top_k_recall=max(topk * 10, 30),
        top_k_rerank=DEFAULT_TOP_K_RERANK,
        top_k_final=topk,
    )


def retrieve_tables_advanced(
    query: str,
    top_k_recall: int = DEFAULT_TOP_K_RECALL,
    top_k_rerank: int = DEFAULT_TOP_K_RERANK,
    top_k_final: int = DEFAULT_TOP_K_FINAL,
) -> List[Dict[str, Any]]:
    """
    Retrieval pipeline:
      1) Milvus recall (IP on normalized embeddings)
      2) Rerank only top_k_rerank by vector score using CrossEncoder
      3) Cutoff by rerank threshold (top1 only)  ✅
      4) Return top_k_final

    Notes:
      - gap is NOT used for hard cutoff anymore (to avoid false negatives)
      - gap is logged for confidence and can be used by downstream (LLM topk selection)
    """
    if not query:
        return []

    if not ensure_milvus_connection():
        return []

    t0 = time.perf_counter()
    logger.info(f"🔍 [Retrieve] Start searching for: '{query}'")

    # -------- 1) Recall (Milvus) --------
    try:
        col = Collection(COLLECTION_NAME)

        embed_t0 = time.perf_counter()
        model = get_embed_model()
        query_vec = model.encode([query], normalize_embeddings=True)[0].tolist()
        embed_ms = (time.perf_counter() - embed_t0) * 1000.0

        search_params = {"metric_type": "IP", "params": {"nprobe": 10}}
        milvus_t0 = time.perf_counter()
        res = col.search(
            data=[query_vec],
            anns_field="embedding",
            param=search_params,
            limit=top_k_recall,
            output_fields=["db", "logical_table", "text"],
        )
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
                        "score": float(hit.score),  # IP similarity on normalized vectors
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

        recall_preview = [
            f"{c['full_name']} vec={c['score']:.4f}"
            for c in candidates[: min(10, len(candidates))]
        ]
        logger.info(
            f"🧾 [Retrieve] RecallTop-{min(10, len(candidates))}: {recall_preview} | "
            f"embed_ms={embed_ms:.1f} milvus_ms={milvus_ms:.1f}"
        )

    except Exception as e:
        logger.error(f"❌ Milvus Search Failed: {e}", exc_info=True)
        return []

    # -------- 2) Rerank (only top_k_rerank) --------
    reranker = get_rerank_model()
    if reranker is not None:
        rerank_pool = candidates[: max(1, min(top_k_rerank, len(candidates)))]
        try:
            rerank_t0 = time.perf_counter()
            pairs = [[query[:256], c["text"][:512]] for c in rerank_pool]
            scores = reranker.predict(pairs, batch_size=32, show_progress_bar=False)
            rerank_ms = (time.perf_counter() - rerank_t0) * 1000.0

            for i, c in enumerate(rerank_pool):
                c["rerank_score"] = float(scores[i])

            rerank_pool.sort(key=lambda x: x["rerank_score"], reverse=True)

            rerank_preview = [
                f"{c['full_name']} rerank={c['rerank_score']:.3f} vec={c['score']:.4f}"
                for c in rerank_pool[: min(10, len(rerank_pool))]
            ]
            logger.info(
                f"🧾 [Retrieve] RerankTop-{min(10, len(rerank_pool))}: {rerank_preview} | rerank_ms={rerank_ms:.1f}"
            )

            # -------- 3) Cutoff (top1 only) ✅ --------
            top1 = rerank_pool[0].get("rerank_score", -999.0)
            top2 = rerank_pool[1].get("rerank_score", -999.0) if len(rerank_pool) > 1 else -999.0
            gap = top1 - top2

            if top1 < RERANK_THRESHOLD:
                logger.info(
                    f"🛑 [Retrieve] Cutoff: top1 rerank={top1:.3f} < threshold={RERANK_THRESHOLD:.3f}. Return []."
                )
                return []

            # gap only as confidence signal (no hard cutoff)
            if gap < RERANK_WARN_GAP:
                logger.info(
                    f"⚠️ [Retrieve] Low confidence gap={gap:.3f} (<{RERANK_WARN_GAP:.3f}). "
                    f"Suggest downstream use more tables (e.g., top_k_llm=5)."
                )

            candidates_final = rerank_pool

        except Exception as e:
            logger.error(f"⚠️ [Rerank Failed] {e}. Fallback to vector score.", exc_info=True)
            candidates_final = candidates
    else:
        candidates_final = candidates

    # -------- 4) Final output --------
    final_results = candidates_final[: max(0, min(top_k_final, len(candidates_final)))]

    total_ms = (time.perf_counter() - t0) * 1000.0
    if final_results:
        debug_hits = [
            f"{t['full_name']} vec={t['score']:.4f} rerank={t.get('rerank_score', None)}"
            for t in final_results
        ]
        logger.info(f"✅ [Retrieve] Final Top-{len(final_results)}: {debug_hits} | total_ms={total_ms:.1f}")
    else:
        logger.info(f"✅ [Retrieve] No relevant tables found. | total_ms={total_ms:.1f}")

    return final_results
