import os
import json
from typing import List, Dict, Any, Optional
from pymilvus import Collection, connections
from sentence_transformers import SentenceTransformer, CrossEncoder

# 1. 配置
MILVUS_HOST = os.getenv("MILVUS_HOST", "127.0.0.1")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLLECTION_NAME = os.getenv("MILVUS_COLLECTION", "schema_catalog_v2")

# 模型路径
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
RERANK_MODEL_NAME = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")

# 单例模式加载模型 (防止每次请求都加载)
_embed_model = None
_rerank_model = None


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        print(f"🧠 Loading Embedding Model: {EMBED_MODEL_NAME}...")
        _embed_model = SentenceTransformer(EMBED_MODEL_NAME)
    return _embed_model


def get_rerank_model():
    global _rerank_model
    if _rerank_model is None:
        # CrossEncoder 比较大，如果是 CPU 部署要注意内存
        print(f"🧠 Loading Rerank Model: {RERANK_MODEL_NAME}...")
        try:
            _rerank_model = CrossEncoder(RERANK_MODEL_NAME)
        except Exception as e:
            print(f"⚠️ Rerank model load failed: {e}. Fallback to None.")
    return _rerank_model


# 建立 Milvus 连接
try:
    connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)
except Exception as e:
    print(f"❌ Milvus Connect Error: {e}")


# ==========================================
# 核心检索函数 (Recall + Rerank)
# ==========================================

def retrieve_tables(query: str, topk: int = 5) -> List[Dict[str, Any]]:
    """
    为了兼容旧代码的简单的入口
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

    # --- 1. Recall (Milvus) ---
    try:
        col = Collection(COLLECTION_NAME)
        col.load()  # 确保加载到内存

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
        print(f"❌ Milvus Search Failed: {e}")
        return []

    # --- 2. Rerank (Cross-Encoder) ---
    reranker = get_rerank_model()
    if reranker and candidates:
        # 构造 Pair: [[query, doc1], [query, doc2]...]
        pairs = [[query, c["text"]] for c in candidates]
        scores = reranker.predict(pairs)

        # 把重排分数写回去
        for i, c in enumerate(candidates):
            c["rerank_score"] = float(scores[i])

        # 按重排分数排序
        candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
    else:
        # 降级：如果没有重排模型，就按向量分数排
        candidates.sort(key=lambda x: x["score"], reverse=True)

    # --- 3. Cut Off ---
    return candidates[:top_k_final]