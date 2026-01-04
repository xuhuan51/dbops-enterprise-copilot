import os
import logging
from typing import List, Dict, Any, Optional

import numpy as np
from pymilvus import connections, Collection, utility
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

MILVUS_HOST = os.getenv("MILVUS_HOST", "127.0.0.1")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "schema_catalog")

EMBED_MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "BAAI/bge-m3")

EVIDENCE_MAX_CHARS = int(os.getenv("EVIDENCE_MAX_CHARS", "800"))

_model: Optional[SentenceTransformer] = None
_col: Optional[Collection] = None
_connected = False


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info("Loading embedding model: %s", EMBED_MODEL_NAME)
        _model = SentenceTransformer(EMBED_MODEL_NAME)
        logger.info("Embedding model loaded.")
    return _model


def _connect():
    global _connected
    if _connected:
        return
    connections.connect(host=MILVUS_HOST, port=MILVUS_PORT)
    _connected = True


def _get_collection() -> Collection:
    global _col
    _connect()
    if _col is None:
        if not utility.has_collection(MILVUS_COLLECTION):
            raise RuntimeError(f"Milvus collection not found: {MILVUS_COLLECTION}")
        _col = Collection(MILVUS_COLLECTION)
        _col.load()
        logger.info("Milvus collection loaded: %s", MILVUS_COLLECTION)
    return _col


def retrieve_tables(query: str, topk: int = 10) -> List[Dict[str, Any]]:
    """
    与 Attu schema 100% 对齐：schema_catalog
    fields:
      id, db, table, full_name, domain, owner, app, perm_tag, sensitivity,
      join_keys, time_cols, metric_cols, text, vector(FloatVector1024)
    index:
      HNSW (IP)
    """
    if not query or not query.strip():
        return []

    model = _get_model()
    col = _get_collection()

    # normalize_embeddings=True + IP => cosine 相似度效果
    vec = model.encode([query], normalize_embeddings=True)
    vec = np.asarray(vec, dtype=np.float32)[0].tolist()

    output_fields = [
        "db", "table", "full_name", "domain",
        "owner", "app", "perm_tag", "sensitivity",
        "join_keys", "time_cols", "metric_cols",
        "text"
    ]

    current_ef = max(128, int(topk * 1.5))

    search_params = {
        "metric_type": "IP",
        "params": {"ef": current_ef}  # <--- 这里用动态变量
    }

    res = col.search(
        data=[vec],
        anns_field="vector",
        param=search_params,
        limit=topk,
        output_fields=output_fields
    )

    hits = res[0] if res else []
    items: List[Dict[str, Any]] = []
    for h in hits:
        ent = h.entity

        full_name = ent.get("full_name") or f"{ent.get('db')}.{ent.get('table')}".strip(".")
        raw_text = ent.get("text") or ""
        if len(raw_text) > EVIDENCE_MAX_CHARS:
            raw_text = raw_text[:EVIDENCE_MAX_CHARS] + "..."

        items.append({
            "db": ent.get("db"),
            "table": ent.get("table"),
            "full_name": full_name,
            "domain": ent.get("domain"),
            "owner": ent.get("owner"),
            "app": ent.get("app"),
            "perm_tag": ent.get("perm_tag"),
            "sensitivity": ent.get("sensitivity"),
            "join_keys": ent.get("join_keys"),
            "time_cols": ent.get("time_cols"),
            "metric_cols": ent.get("metric_cols"),
            "text": raw_text,
            "score": float(h.score),
        })

    return items
