import os
import json
from dotenv import load_dotenv
from pymilvus import connections, FieldSchema, CollectionSchema, DataType, Collection, utility
from sentence_transformers import SentenceTransformer

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
load_dotenv(os.path.join(project_root, ".env"))

MILVUS_HOST = os.getenv("MILVUS_HOST", "127.0.0.1")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")

COLLECTION_NAME = os.getenv("MILVUS_COLLECTION", "schema_catalog_v2")
SOURCE_FILE = os.path.join(project_root, "data", "schema_catalog_enriched.jsonl")
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")

BATCH_SIZE = int(os.getenv("MILVUS_BATCH_SIZE", "128"))
TEXT_MAX_LEN = int(os.getenv("MILVUS_TEXT_MAX_LEN", "8000"))

def init_milvus(dim: int) -> Collection:
    print(f"🔌 Connecting to Milvus {MILVUS_HOST}:{MILVUS_PORT}...")
    connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)

    if utility.has_collection(COLLECTION_NAME):
        print(f"🗑️ Dropping existing collection: {COLLECTION_NAME}")
        utility.drop_collection(COLLECTION_NAME)

    print(f"🔨 Creating collection: {COLLECTION_NAME}")
    fields = [
        FieldSchema(name="full_name", dtype=DataType.VARCHAR, max_length=256, is_primary=True),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),

        FieldSchema(name="db", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="table", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="logical_table", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="domain", dtype=DataType.VARCHAR, max_length=64),

        FieldSchema(name="feat_join_keys", dtype=DataType.VARCHAR, max_length=2048),
        FieldSchema(name="feat_time_cols", dtype=DataType.VARCHAR, max_length=2048),
        FieldSchema(name="feat_metric_cols", dtype=DataType.VARCHAR, max_length=2048),

        # VARCHAR 长度有时按字符/字节实现不一，给大一点更稳；不行就用 env 降
        FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=max(8192, TEXT_MAX_LEN + 256)),
    ]

    schema = CollectionSchema(fields, description="Schema Catalog with Table Capability Card")
    col = Collection(COLLECTION_NAME, schema)

    index_params = {
        "index_type": "HNSW",
        "metric_type": "IP",
        "params": {"M": 16, "efConstruction": 200},
    }
    col.create_index(field_name="embedding", index_params=index_params)
    return col

def insert_batch(col: Collection, model: SentenceTransformer, batch: list[dict]):
    texts = [x["raw_text_for_emb"] for x in batch]
    embeddings = model.encode(texts, normalize_embeddings=True)
    col.insert([
        [x["full_name"] for x in batch],
        embeddings.tolist(),
        [x["db"] for x in batch],
        [x["table"] for x in batch],
        [x["logical_table"] for x in batch],
        [x["domain"] for x in batch],
        [x["feat_join_keys"] for x in batch],
        [x["feat_time_cols"] for x in batch],
        [x["feat_metric_cols"] for x in batch],
        [x["text"] for x in batch],
    ])

def main():
    if not os.path.exists(SOURCE_FILE):
        print(f"❌ File not found: {SOURCE_FILE}. Run extract script first.")
        return

    print(f"🧠 Loading embedding model: {EMBED_MODEL}...")
    model = SentenceTransformer(EMBED_MODEL)

    test_emb = model.encode(["test"], normalize_embeddings=True)
    dim = int(test_emb.shape[1])

    col = init_milvus(dim)

    inserted = 0
    batch = []

    print("🚀 Processing data...")
    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)

            # 主键：db.logical_table
            full_name = f"{item['db']}.{item['logical_table']}"

            feats = item.get("features", {}) or {}

            raw_text = item.get("text", "") or ""
            safe_text = raw_text[:TEXT_MAX_LEN]

            entry = {
                "full_name": full_name,
                "db": item["db"],
                "table": item["table"],
                "logical_table": item["logical_table"],
                "domain": item.get("domain", "other") or "other",
                "feat_join_keys": json.dumps(feats.get("join_keys", []), ensure_ascii=False),
                "feat_time_cols": json.dumps(feats.get("time_cols", []), ensure_ascii=False),
                "feat_metric_cols": json.dumps(feats.get("metric_cols", []), ensure_ascii=False),
                "text": safe_text,
                "raw_text_for_emb": raw_text,  # embedding 用完整版
            }
            batch.append(entry)

            if len(batch) >= BATCH_SIZE:
                insert_batch(col, model, batch)
                inserted += len(batch)
                print(f"  ✅ Inserted: {inserted}")
                batch = []

    if batch:
        insert_batch(col, model, batch)
        inserted += len(batch)
        print(f"  ✅ Inserted: {inserted}")

    col.flush()
    col.load()
    print(f"✅ Done! Total {col.num_entities} entities in {COLLECTION_NAME}.")

if __name__ == "__main__":
    main()
