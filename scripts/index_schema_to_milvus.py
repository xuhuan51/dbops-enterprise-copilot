import os
import json
from dotenv import load_dotenv
from pymilvus import connections, FieldSchema, CollectionSchema, DataType, Collection, utility
from sentence_transformers import SentenceTransformer

# 1. 环境配置
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
load_dotenv(os.path.join(project_root, ".env"))

# 2. 配置
MILVUS_HOST = os.getenv("MILVUS_HOST", "127.0.0.1")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLLECTION_NAME = os.getenv("MILVUS_COLLECTION", "schema_catalog_v2")
SOURCE_FILE = os.path.join(project_root, "data", "table_card_v1.jsonl")

# 模型配置
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
BATCH_SIZE = int(os.getenv("MILVUS_BATCH_SIZE", "64"))
TEXT_MAX_LEN = int(os.getenv("MILVUS_TEXT_MAX_LEN", "8000"))


def init_milvus(dim: int) -> Collection:
    print(f"🔌 Connecting to Milvus {MILVUS_HOST}:{MILVUS_PORT}...")
    connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)

    if utility.has_collection(COLLECTION_NAME):
        print(f"🗑️ Dropping old collection: {COLLECTION_NAME}")
        utility.drop_collection(COLLECTION_NAME)

    print(f"🔨 Creating new collection: {COLLECTION_NAME}")

    # 🔥 瘦身后的 Schema：只留 Agent 核心字段
    fields = [
        # 1. 唯一标识
        FieldSchema(name="full_name", dtype=DataType.VARCHAR, max_length=256, is_primary=True),

        # 2. 向量 (用于检索)
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),

        # 3. 基础信息 (用于 Agent 引用表名)
        FieldSchema(name="db", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="logical_table", dtype=DataType.VARCHAR, max_length=128),

        # 4. 核心上下文 (Agent 的 Prompt 来源)
        # 包含: Summary, Columns(重要!), Samples, Synonyms
        FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=max(8192, TEXT_MAX_LEN + 256)),
    ]

    schema = CollectionSchema(fields, description="Agent-Optimized Schema Catalog")
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
        [x["logical_table"] for x in batch],
        [x["text"] for x in batch],
    ])


def main():
    if not os.path.exists(SOURCE_FILE):
        print(f"❌ File not found: {SOURCE_FILE}")
        return

    print(f"🧠 Loading model: {EMBED_MODEL}...")
    model = SentenceTransformer(EMBED_MODEL)

    # 测维度
    dim = int(model.encode(["test"], normalize_embeddings=True).shape[1])
    col = init_milvus(dim)

    inserted = 0
    batch = []

    print(f"🚀 Indexing data from {SOURCE_FILE}...")
    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                card = json.loads(line)
            except:
                continue

            ident = card.get("identity", {})

            # 主键
            full_name = f"{ident.get('db')}.{ident.get('logical_table')}"

            # 准备 Text
            # 注意：extract_schema_catalog_v2.py 生成的 card["text"]
            # 已经包含了 "Columns: name(type)..." 和 "Samples: ..."
            # 这正是 Agent 写 SQL 必须的素材，所以直接存 text 即可。
            raw_text = card.get("text", "")
            safe_text = raw_text[:TEXT_MAX_LEN]

            entry = {
                "full_name": full_name,
                "db": ident.get("db", ""),
                "logical_table": ident.get("logical_table", ""),
                "text": safe_text,
                "raw_text_for_emb": raw_text
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
    print(f"🎉 Done! {col.num_entities} tables indexed.")


if __name__ == "__main__":
    main()