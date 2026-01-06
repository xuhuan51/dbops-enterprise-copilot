import os
import json
from typing import List, Dict, Any, Optional
from pymilvus import Collection, connections, utility
from sentence_transformers import SentenceTransformer, CrossEncoder

# 🔥 1. 统一配置和日志
from app.core.config import settings
from app.core.logger import logger

# 配置
MILVUS_HOST = settings.MILVUS_HOST
MILVUS_PORT = settings.MILVUS_PORT
COLLECTION_NAME = "schema_catalog_v2"

# 模型配置 (建议也在 config.py 中定义，这里暂时保持硬编码或读取 env)
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
RERANK_MODEL_NAME = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")

# 单例模式加载模型
_embed_model = None
_rerank_model = None

# 🔥 全局状态锁：防止重复 Load Collection
_COLLECTION_LOADED = False


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        logger.info(f"🧠 Loading Embedding Model: {EMBED_MODEL_NAME}...")
        _embed_model = SentenceTransformer(EMBED_MODEL_NAME)
    return _embed_model


def get_rerank_model():
    global _rerank_model
    if _rerank_model is None:
        # CrossEncoder 比较大，如果是 CPU 部署要注意内存
        logger.info(f"🧠 Loading Rerank Model: {RERANK_MODEL_NAME}...")
        try:
            _rerank_model = CrossEncoder(RERANK_MODEL_NAME)
        except Exception as e:
            logger.warning(f"⚠️ Rerank model load failed: {e}. Fallback to None.")
    return _rerank_model


def ensure_milvus_connection():
    """
    确保 Milvus 已连接且 Collection 已加载到内存。
    使用全局锁 _COLLECTION_LOADED 避免重复加载。
    """
    global _COLLECTION_LOADED

    # 1. 建立连接 (pymilvus 内部有连接池管理，多次调用 connect 问题不大，但最好也判断一下)
    try:
        connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)
    except Exception as e:
        logger.error(f"❌ Milvus Connect Error: {e}")
        return

    # 2. 加载 Collection (这是重操作，必须加锁)
    if not _COLLECTION_LOADED:
        if utility.has_collection(COLLECTION_NAME):
            logger.info(f"🔄 Loading collection '{COLLECTION_NAME}' into memory...")
            Collection(COLLECTION_NAME).load()
            _COLLECTION_LOADED = True
            logger.info(f"✅ Collection '{COLLECTION_NAME}' loaded.")
        else:
            logger.error(f"❌ Collection '{COLLECTION_NAME}' not found! Please run ETL first.")


# ==========================================
# 核心检索函数 (Recall + Rerank)
# ==========================================

def retrieve_tables(query: str, topk: int = 5) -> List[Dict[str, Any]]:
    """
    简单入口
    """
    return retrieve_tables_advanced(query, top_k_recall=topk * 10, top_k_final=topk)


def retrieve_tables_advanced(query: str, top_k_recall: int = 100, top_k_final: int = 5) -> List[Dict[str, Any]]:
    """
    企业级检索流程：
    1. Milvus 向量召回 Top-100 (Recall)
    2. BGE Cross-Encoder 重排 (Rerank)
    3. 返回 Top-N (Final)
    """
    if not query: return []

    # 确保连接和加载状态
    ensure_milvus_connection()

    # --- 1. Recall (Milvus) ---
    try:
        col = Collection(COLLECTION_NAME)
        # 注意：这里不需要再调用 col.load()，因为 ensure_milvus_connection 已经处理了

        model = get_embed_model()
        query_vec = model.encode([query], normalize_embeddings=True)[0].tolist()

        # 只取 Agent 需要的字段
        search_params = {"metric_type": "IP", "params": {"nprobe": 10}}
        res = col.search(
            data=[query_vec],
            anns_field="embedding",
            param=search_params,
            limit=top_k_recall,  # 广撒网
            output_fields=["db", "logical_table", "text"]
        )

        # 结果转 list
        candidates = []
        seen = set()

        for hits in res:
            for hit in hits:
                entity = hit.entity
                # 逻辑表去重 (可能因为分片表导致重复)
                full_name = f"{entity.get('db')}.{entity.get('logical_table')}"
                if full_name in seen: continue
                seen.add(full_name)

                candidates.append({
                    "score": hit.score,  # 向量相似度
                    "db": entity.get("db"),
                    "logical_table": entity.get("logical_table"),
                    "full_name": full_name,
                    "text": entity.get("text")
                })

    except Exception as e:
        logger.error(f"❌ Milvus Search Failed: {e}", exc_info=True)
        return []

    # --- 2. Rerank (Cross-Encoder) ---
    reranker = get_rerank_model()

    # 🔥 优化点：如果有重排模型，必须加保护
    if reranker and candidates:
        try:
            # A. 硬截断 (Hard Truncation)
            # CrossEncoder 处理长文本极慢且耗内存。
            # Query 截断 256 字符，Document 截断 512 字符
            pairs = [[query[:256], c["text"][:512]] for c in candidates]

            # B. 批处理 (Batching)
            scores = reranker.predict(
                pairs,
                batch_size=32,
                show_progress_bar=False,
                num_workers=0  # 避免多进程开销
            )

            for i, c in enumerate(candidates):
                c["rerank_score"] = float(scores[i])

            # 按 Rerank 分数排序
            candidates.sort(key=lambda x: x["rerank_score"], reverse=True)

        except Exception as e:
            # C. 降级策略 (Fallback)
            # 如果 Rerank 爆显存/超时/报错，不要抛出异常，而是降级回向量分数
            logger.error(f"⚠️ [Rerank Failed] Query: {query} | Error: {e}. Fallback to vector score.")
            candidates.sort(key=lambda x: x["score"], reverse=True)
    else:
        # 无模型或候选集为空时的默认排序
        candidates.sort(key=lambda x: x["score"], reverse=True)

    # --- 3. Cut Off ---
    return candidates[:top_k_final]