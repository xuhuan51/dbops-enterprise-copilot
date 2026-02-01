import json
import os
import time
from tqdm import tqdm  # 建议安装：pip install tqdm
from sentence_transformers import SentenceTransformer

from app.core.rag_store import rag_store
from app.core.rag_store import encoder

# =================配置路径==================
METADATA_DIR = "../data/bird/metadata"
SCHEMA_FILE = os.path.join(METADATA_DIR, "schema_catalog.json")
RULES_FILE = os.path.join(METADATA_DIR, "business_rules.json")
BATCH_SIZE = 500  # 每一批次处理的数量


# ==========================================

def ingest_schema_catalog():
    """入库 Schema 目录"""
    if not os.path.exists(SCHEMA_FILE):
        print(f"⚠️ 跳过 Schema: 找不到文件 {SCHEMA_FILE}")
        return

    with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
        catalog_data = json.load(f)

    print(f"🚀 开始同步 Schema Catalog ({len(catalog_data)} 条记录)...")

    # 分批处理
    for i in tqdm(range(0, len(catalog_data), BATCH_SIZE)):
        batch = catalog_data[i: i + BATCH_SIZE]

        # 准备 Milvus 批量数据格式
        vectors = []
        db_ids = []
        tables = []
        columns = []
        metadatas = []

        # 提取 doc_text 进行批量 Embedding
        texts_to_embed = [item["doc_text"] for item in batch]
        batch_vectors = encoder.encode(texts_to_embed, normalize_embeddings=True).tolist()

        for idx, item in enumerate(batch):
            vectors.append(batch_vectors[idx])
            db_ids.append(item["db_id"])
            tables.append(item["table"])
            columns.append(item["column"])

            # 聚合元数据，减少检索后的二次查询
            meta = {
                "table_comment": item.get("table_comment"),
                "column_comment": item.get("column_comment"),
                "column_type": item.get("column_type"),
                "is_pk": item.get("is_pk"),
                "is_fk": item.get("is_fk"),
                "fk_to": item.get("fk_to"),
                "samples": item.get("samples"),
                "num_profile": item.get("num_profile")
            }
            metadatas.append(json.dumps(meta, ensure_ascii=False))

        # 调用 DAO 写入 (注意：这里假设你之前的 DAO.schema_col.insert 已准备好)
        rag_store.schema_col.insert([
            vectors, db_ids, tables, columns, metadatas
        ])

    rag_store.schema_col.flush()
    print(f"✅ Schema 入库完成。当前总量: {rag_store.schema_col.num_entities}")


def ingest_business_rules():
    """入库 业务规则/知识库"""
    if not os.path.exists(RULES_FILE):
        print(f"⚠️ 跳过 Rules: 找不到文件 {RULES_FILE}")
        return

    with open(RULES_FILE, "r", encoding="utf-8") as f:
        rules_data = json.load(f)

    print(f"🚀 开始同步 Business Rules ({len(rules_data)} 条记录)...")

    for i in tqdm(range(0, len(rules_data), BATCH_SIZE)):
        batch = rules_data[i: i + BATCH_SIZE]

        texts_to_embed = [item["doc_text"] for item in batch]
        batch_vectors = encoder.encode(texts_to_embed, normalize_embeddings=True).tolist()

        vectors = []
        db_ids = []
        rule_texts = []
        doc_texts = []

        for idx, item in enumerate(batch):
            vectors.append(batch_vectors[idx])
            db_ids.append(item["db_id"])
            rule_texts.append(item["rule_text"])
            doc_texts.append(item["doc_text"])

        rag_store.knowledge_col.insert([
            vectors, db_ids, rule_texts, doc_texts
        ])

    rag_store.knowledge_col.flush()
    print(f"✅ Business Rules 入库完成。当前总量: {rag_store.knowledge_col.num_entities}")


if __name__ == "__main__":
    start_time = time.time()

    # 1. 确保集合已存在 (调用一次 DAO 的初始化)
    print("Initializing Milvus Collections...")

    # 2. 执行入库
    ingest_schema_catalog()
    print("-" * 30)
    ingest_business_rules()

    end_time = time.time()
    print(f"\n✨ 所有数据同步完成！总耗时: {end_time - start_time:.2f}s")