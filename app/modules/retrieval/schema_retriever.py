import json
import re
import datetime
import time
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

import sqlglot
from sqlglot import exp
from pymilvus import Collection, connections, utility
from sentence_transformers import SentenceTransformer, CrossEncoder

from app.core.config import settings
from app.core.logger import logger
from app.infrastructure.db.mysql import get_async_pool
from app.modules.sql.executor import append_event, get_proxy_pool

# =========================
# ⚙️ Config & Constants
# =========================
MILVUS_HOST = settings.MILVUS_HOST
MILVUS_PORT = settings.MILVUS_PORT
COLLECTION_NAME = settings.MILVUS_COLLECTION  # Usually "rag_schema"

EMBED_MODEL_NAME = settings.EMBED_MODEL
RERANK_MODEL_NAME = settings.RERANK_MODEL

# Retrieval Defaults
DEFAULT_TOP_K_RECALL = int(getattr(settings, "TOP_K_RECALL", 100))
DEFAULT_TOP_K_RERANK = int(getattr(settings, "TOP_K_RERANK", 20))
DEFAULT_TOP_K_FINAL = int(getattr(settings, "TOP_K_FINAL", 5))
RERANK_THRESHOLD = float(getattr(settings, "RERANK_THRESHOLD", 0.01))

# Security Filter
SENSITIVE_KEYWORDS = ["工资", "薪水", "底薪", "密码", "密钥", "token", "salary", "password"]

# =========================
# 🔒 Singletons & Locks
# =========================
_embed_model: Optional[SentenceTransformer] = None
_rerank_model: Optional[CrossEncoder] = None
_collection_loaded = False

_model_lock = threading.Lock()
_milvus_lock = threading.Lock()

# Dedicated ThreadPool for CPU-intensive tasks (Embedding/Rerank) & Milvus IO
_executor = ThreadPoolExecutor(max_workers=3)


# =========================
# 🛠️ Helper Functions
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
                    logger.warning(f"⚠️ Rerank model load failed: {e}")
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
                Collection(COLLECTION_NAME).load()
                _collection_loaded = True
            except Exception as e:
                logger.error(f"❌ Collection load failed: {e}")
                return False
    return True


def _run_embedding(model, text):
    return model.encode([text], normalize_embeddings=True)[0].tolist()


def _run_rerank(model, pairs):
    return model.predict(pairs, batch_size=32, show_progress_bar=False)


def clean_ddl_with_ast(raw_ddl: str) -> str:
    """
    Use SQLGlot to parse DDL and remove physical attributes (Engine, Charset, etc.)
    that are irrelevant to the LLM, keeping core column definitions, types, and comments.
    """
    try:
        # 1. Parse into AST object
        expression = sqlglot.parse_one(raw_ddl, read="mysql")

        # 2. Only clean properties for CREATE TABLE statements
        if isinstance(expression, exp.Create):
            # Remove table-level properties (e.g., ENGINE=InnoDB, CHARSET=utf8mb4)
            expression.set("properties", None)

        # 3. Regenerate SQL
        clean_sql = expression.sql(dialect="mysql", pretty=True)
        return clean_sql

    except Exception as e:
        logger.warning(f"⚠️ AST Clean failed: {e}, using raw DDL.")
        return raw_ddl


# =======================================================
# 1️⃣ Left Tower Core: Retrieve Tables (retrieve_tables_advanced)
# =======================================================
async def retrieve_tables_advanced(
        query: str,
        top_k_recall: int = DEFAULT_TOP_K_RECALL,
        top_k_rerank: int = DEFAULT_TOP_K_RERANK,
        top_k_final: int = DEFAULT_TOP_K_FINAL,
        trace_id: str = "N/A"
) -> List[Dict[str, Any]]:
    """
    Schema Retrieval Main Logic:
    1. Vector Recall (Recall) -> Get card_json
    2. Parse Metadata (Parse) -> Extract owner, app, etc.
    3. Rerank (Rerank) -> Score using text field
    4. Audit Log (Audit)
    """

    # 1. Hard Rule Filtering
    for kw in SENSITIVE_KEYWORDS:
        if kw in query:
            logger.warning(f"🛑 [Security] Query contains sensitive keyword '{kw}'. Blocked.",
                           extra={"trace_id": trace_id})
            return []

    if not query:
        return []

    # Milvus Connection Check
    if not ensure_milvus_connection():
        return []

    t0 = time.perf_counter()
    loop = asyncio.get_running_loop()

    # -------- Phase 1: Recall (Milvus) --------
    try:
        col = Collection(COLLECTION_NAME)
        model = get_embed_model()

        # Async Embedding
        query_vec = await loop.run_in_executor(_executor, _run_embedding, model, query)

        # 🔥 Critical Fix: Only request fields that actually exist in Milvus Schema
        output_fields = ["db", "logical_table", "domain", "card_json", "full_name"]

        def _search_milvus():
            # Note: rag_store initialized metric_type is COSINE
            return col.search(
                data=[query_vec],
                anns_field="vector",
                param={"metric_type": "COSINE", "params": {"nprobe": 10}},
                limit=top_k_recall,
                output_fields=output_fields,
            )

        res = await loop.run_in_executor(_executor, _search_milvus)

        candidates: List[Dict[str, Any]] = []
        seen = set()

        for hits in res:
            for hit in hits:
                entity = hit.entity

                # Parse JSON payload
                card_data = {}
                try:
                    json_str = entity.get("card_json")
                    if json_str:
                        card_data = json.loads(json_str)
                except Exception:
                    pass

                # Prioritize full_name from card_json, then entity, lastly construct it
                full_name = card_data.get("full_name") or entity.get(
                    "full_name") or f"{entity.get('db')}.{entity.get('logical_table')}"

                if full_name in seen:
                    continue
                seen.add(full_name)

                # Flatten metadata fields
                item = {
                    "score": float(hit.score),
                    "full_name": full_name,
                    "db": entity.get("db"),
                    "logical_table": entity.get("logical_table"),
                    "domain": entity.get("domain"),

                    # Extract important fields for Rerank or Agent
                    "text": card_data.get("text", ""),  # For Rerank
                    "owner": card_data.get("owner", "N/A"),
                    "app": card_data.get("app", "N/A"),
                    "sensitivity": card_data.get("sensitivity", "N/A"),

                    # Keep full raw card as backup
                    "card": card_data
                }
                candidates.append(item)

        if not candidates:
            return []

    except Exception as e:
        logger.error(f"❌ Milvus Search Failed: {e}", exc_info=True, extra={"trace_id": trace_id})
        return []

    # -------- Phase 2: Rerank --------
    reranker = get_rerank_model()
    candidates_final = candidates

    if reranker is not None and candidates:
        try:
            # Slice Top N for Rerank
            rerank_pool = candidates[: max(1, min(top_k_rerank, len(candidates)))]

            # Construct (Query, Doc) pairs
            pairs = [[query[:256], c.get("text", "")[:512]] for c in rerank_pool]

            # Async inference
            scores = await loop.run_in_executor(_executor, _run_rerank, reranker, pairs)

            for i, c in enumerate(rerank_pool):
                c["rerank_score"] = float(scores[i])

            rerank_pool.sort(key=lambda x: x["rerank_score"], reverse=True)

            # Threshold cutoff
            if rerank_pool and rerank_pool[0]["rerank_score"] < RERANK_THRESHOLD:
                logger.info(f"🛑 [Retrieve] Cutoff: top1 {rerank_pool[0]['rerank_score']:.3f} < threshold",
                            extra={"trace_id": trace_id})
                return []

            candidates_final = rerank_pool

        except Exception as e:
            logger.warning(f"⚠️ [Rerank Failed] {e}. Fallback to vector score.", extra={"trace_id": trace_id})

    # -------- Phase 3: Final Output & Audit --------
    final_results = candidates_final[: max(0, min(top_k_final, len(candidates_final)))]

    total_ms = (time.perf_counter() - t0) * 1000.0

    # Log event
    try:
        table_names = [t.get("full_name") for t in final_results]
        append_event({
            "trace_id": trace_id,
            "user_id": "system_retriever",
            "route": "RETRIEVE",
            "sql": query,
            "latency_ms": int(total_ms),
            "result_summary": table_names,
            "ts_iso": datetime.datetime.utcnow().isoformat(),
        })
    except Exception:
        pass

    return final_results


async def fetch_table_metadata(table_names: List[str]) -> List[Dict[str, Any]]:
    """
    Dedicated function for ContextRetriever Rescue.
    Directly query CREATE TABLE statements from DB based on logical table names.
    Supports physical table mapping (t_order_000 -> t_order) and DDL minification.
    """
    if not table_names:
        return []

    results = []

    # ✅ 核心修改：使用 Proxy 连接池 (Port 3307)
    # 之前使用的是 get_async_pool() 直连后端 MySQL (3306)，导致无法识别逻辑表
    pool = await get_proxy_pool()

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for logic_name in table_names:
                try:
                    ddl = ""

                    # Strategy A: 直接查询 Proxy
                    # ShardingSphere Proxy 会拦截这个 SQL，自动找到底层分片并合并 DDL 返回
                    try:
                        await cur.execute(f"SHOW CREATE TABLE `{logic_name}`")
                        row = await cur.fetchone()
                        if row:
                            # 兼容不同驱动返回的 Key (Create Table 或 Create View)
                            ddl = row.get("Create Table") or row.get("Create View")
                    except Exception as e:
                        # 仅当 Proxy 也找不到表时才会报错
                        logger.warning(f"⚠️ [Rescue] Proxy lookup failed for {logic_name}: {e}")

                    # Strategy B: (可选保留) 物理表兜底
                    # 只有在 Proxy 查不到，且你怀疑是纯物理表未配置在 Sharding 规则里时才需要
                    if not ddl:
                        try:
                            # 尝试模糊匹配物理表 (注意：Proxy 环境下 information_schema 行为可能不同)
                            await cur.execute(f"""
                                SELECT table_name 
                                FROM information_schema.tables 
                                WHERE table_name LIKE '{logic_name}_%' 
                                AND table_schema = DATABASE()
                                LIMIT 1
                            """)
                            row = await cur.fetchone()
                            if row:
                                physical_name = row.get("table_name") or row.get("TABLE_NAME")
                                await cur.execute(f"SHOW CREATE TABLE `{physical_name}`")
                                row_ddl = await cur.fetchone()
                                if row_ddl:
                                    raw_ddl = row_ddl.get("Create Table")
                                    # 将物理表名替换回逻辑表名
                                    pattern = re.compile(f"CREATE\\s+TABLE\\s+`?{physical_name}`?", re.IGNORECASE)
                                    ddl = pattern.sub(f"CREATE TABLE `{logic_name}`", raw_ddl, count=1)
                        except Exception:
                            pass

                    if ddl:
                        # 2. 🔥 AST Deep Cleaning (Minification)
                        # 使用 sqlglot 清理掉无关的物理属性 (如 Engine, Charset)
                        clean_ddl = clean_ddl_with_ast(ddl)

                        results.append({
                            "table_name": logic_name,
                            "ddl": clean_ddl,
                            "text": clean_ddl  # 兼容旧格式
                        })
                    else:
                        logger.warning(f"⚠️ [Rescue] DDL not found for table: {logic_name}")

                except Exception as e:
                    logger.error(f"❌ [Rescue] Fetch DDL failed for {logic_name}: {e}")

    return results

# =======================================================
# 3️⃣ Orchestrator Adapter (New Added)
# =======================================================
class SchemaRetriever:
    """
    适配 Orchestrator 的包装类。
    将独立的 retrieve_tables_advanced 函数封装为类方法。
    """
    async def search_tables(self, query: str, top_k_final: int = 5) -> List[Dict[str, Any]]:
        """
        Orchestrator 调用的统一入口
        """
        # 调用本文件上方的 retrieve_tables_advanced 函数
        return await retrieve_tables_advanced(
            query=query,
            top_k_final=top_k_final,
            # trace_id 可以由上层传，这里先给个默认标识，方便看日志
            trace_id="orchestrator_call"
        )