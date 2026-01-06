import os
import sys
import json
from dotenv import load_dotenv
from pymilvus import connections, FieldSchema, CollectionSchema, DataType, Collection, utility
from sentence_transformers import SentenceTransformer

# 1. 环境配置
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
load_dotenv(os.path.join(project_root, ".env"))

# 2. Milvus 配置
MILVUS_HOST = os.getenv("MILVUS_HOST", "127.0.0.1")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLLECTION_NAME = os.getenv("MILVUS_COLLECTION", "schema_catalog_v2")

# 🔥 变更点 1: 输入文件路径改为 V2 产物
SOURCE_FILE = os.path.join(project_root, "data", "table_card_v1.jsonl")

EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
BATCH_SIZE = int(os.getenv("MILVUS_BATCH_SIZE", "64"))  # BGE-M3 比较大，Batch 调小点稳妥
TEXT_MAX_LEN = int(os.getenv("MILVUS_TEXT_MAX_LEN", "8000"))


def init_milvus(dim: int) -> Collection:
    print(f"🔌 Connecting to Milvus {MILVUS_HOST}:{MILVUS_PORT}...")
    connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)

    if utility.has_collection(COLLECTION_NAME):
        print(f"🗑️ Dropping existing collection: {COLLECTION_NAME}")
        utility.drop_collection(COLLECTION_NAME)

    print(f"🔨 Creating collection: {COLLECTION_NAME}")

    # 🔥 变更点 2: Schema 适配 TableCard 结构
    fields = [
        # 主键
        FieldSchema(name="full_name", dtype=DataType.VARCHAR, max_length=256, is_primary=True),
        # 向量
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),

        # 基础元数据 (来自 identity)
        FieldSchema(name="db", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="logical_table", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="domain", dtype=DataType.VARCHAR, max_length=64),

        # 治理元数据 (来自 llm) -> 用于过滤
        FieldSchema(name="risk_level", dtype=DataType.VARCHAR, max_length=32),  # normal/sensitive
        FieldSchema(name="table_type", dtype=DataType.VARCHAR, max_length=32),  # fact/dim

        # 核心特征 (来自 features) -> 存为 JSON 字符串，Gate 取出来转 dict 用
        # 这样比存 feat_join_keys, feat_time_cols 多个字段更灵活，以后加特征不用改表结构
        FieldSchema(name="features_json", dtype=DataType.VARCHAR, max_length=4096),

        # 文本内容 (来自 text)
        FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=max(8192, TEXT_MAX_LEN + 256)),
    ]

    schema = CollectionSchema(fields, description="TableCard V2: Governance Asset Catalog")
    col = Collection(COLLECTION_NAME, schema)

    # 索引
    index_params = {
        "index_type": "HNSW",
        "metric_type": "IP",  # 内积 (适用于归一化后的 Cosine 相似度)
        "params": {"M": 16, "efConstruction": 200},
    }
    col.create_index(field_name="embedding", index_params=index_params)
    return col


def insert_batch(col: Collection, model: SentenceTransformer, batch: list[dict]):
    texts = [x["raw_text_for_emb"] for x in batch]
    # 归一化向量，使得 IP 等价于 Cosine
    embeddings = model.encode(texts, normalize_embeddings=True)

    col.insert([
        [x["full_name"] for x in batch],
        embeddings.tolist(),
        [x["db"] for x in batch],
        [x["logical_table"] for x in batch],
        [x["domain"] for x in batch],
        [x["risk_level"] for x in batch],
        [x["table_type"] for x in batch],
        [x["features_json"] for x in batch],
        [x["text"] for x in batch],
    ])


def main():
    if not os.path.exists(SOURCE_FILE):
        print(f"❌ File not found: {SOURCE_FILE}. Please run extract_schema_catalog_v2.py first.")
        return

    print(f"🧠 Loading embedding model: {EMBED_MODEL}...")
    try:
        model = SentenceTransformer(EMBED_MODEL)
    except Exception as e:
        print(f"❌ Model load failed: {e}")
        print("Try: pip install sentence-transformers")
        return

    # 测算维度
    test_emb = model.encode(["test"], normalize_embeddings=True)
    dim = int(test_emb.shape[1])
    print(f"📏 Vector dimension: {dim}")

    col = init_milvus(dim)

    inserted = 0
    batch = []

    print(f"🚀 Processing data from {SOURCE_FILE}...")
    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue

            try:
                card = json.loads(line)
            except:
                continue

            # 🔥 变更点 3: 解析嵌套结构 (TableCard)
            ident = card.get("identity", {})
            llm = card.get("llm", {})
            features = card.get("features", {})

            # 构造主键
            full_name = f"{ident.get('db')}.{ident.get('logical_table')}"

            # 截断文本防止超长
            raw_text = card.get("text", "")
            safe_text = raw_text[:TEXT_MAX_LEN]

            entry = {
                "full_name": full_name,
                "db": ident.get("db", ""),
                "logical_table": ident.get("logical_table", ""),
                "domain": ident.get("domain", "unknown"),

                # 新字段
                "risk_level": llm.get("risk_level", "normal"),
                "table_type": llm.get("table_type", "unknown"),
                "features_json": json.dumps(features, ensure_ascii=False),  # 存整个特征包

                "text": safe_text,
                "raw_text_for_emb": raw_text  # 向量计算用全量
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
    # col.load() # 写入完不需要立即 load，等查询时再 load

    print(f"🎉 All Done! Total {col.num_entities} entities indexed in '{COLLECTION_NAME}'.")


if __name__ == "__main__":
    main()