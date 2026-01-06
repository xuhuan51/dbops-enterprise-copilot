import os
import sys
import json
from dotenv import load_dotenv
from pymilvus import connections, FieldSchema, CollectionSchema, DataType, Collection, utility
from sentence_transformers import SentenceTransformer

# 添加项目根目录到 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.core.logger import logger

# 配置
MILVUS_HOST = settings.MILVUS_HOST
MILVUS_PORT = settings.MILVUS_PORT
COLLECTION_NAME = settings.MILVUS_COLLECTION

# 输入文件 (ETL 产物)
SOURCE_FILE = settings.OUT_PATH  # e.g., data/schema_catalog.jsonl

# 模型配置
EMBED_MODEL = settings.EMBED_MODEL
BATCH_SIZE = 64
TEXT_MAX_LEN = 8192  # 允许更长的 Rich Text


def init_milvus(dim: int) -> Collection:
    logger.info(f"🔌 Connecting to Milvus {MILVUS_HOST}:{MILVUS_PORT}...")
    connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)

    # 🔥 每次全量更新时，先删除旧集合，防止 Schema 冲突
    if utility.has_collection(COLLECTION_NAME):
        logger.warning(f"🗑️ Dropping existing collection: {COLLECTION_NAME}")
        utility.drop_collection(COLLECTION_NAME)

    logger.info(f"🔨 Creating collection: {COLLECTION_NAME}")

    fields = [
        # 1. 主键 (Primary Key)
        FieldSchema(name="full_name", dtype=DataType.VARCHAR, max_length=256, is_primary=True),

        # 2. 向量 (Embedding) - 核心检索依据
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),

        # 3. 基础定位信息
        FieldSchema(name="db", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="logical_table", dtype=DataType.VARCHAR, max_length=128),

        # 4. 治理元数据 (用于过滤/Gate)
        FieldSchema(name="domain", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="risk_level", dtype=DataType.VARCHAR, max_length=32),
        FieldSchema(name="table_type", dtype=DataType.VARCHAR, max_length=32),

        # 5. 核心语义文本 (Rich Text: 锚点+总结+Schema+样本)
        # 注意：Milvus VARCHAR 最大支持 65535，这里设 8192 足够了
        FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=TEXT_MAX_LEN),
    ]

    schema = CollectionSchema(fields, description="TableCard V2: Semantic Catalog")
    col = Collection(COLLECTION_NAME, schema)

    # 创建索引 (HNSW 性能最好)
    index_params = {
        "index_type": "HNSW",
        "metric_type": "IP",  # Inner Product (配合归一化 Embedding 等价于 Cosine)
        "params": {"M": 16, "efConstruction": 200},
    }
    col.create_index(field_name="embedding", index_params=index_params)
    logger.info("✅ Collection & Index created.")
    return col


def insert_batch(col: Collection, model: SentenceTransformer, batch: list[dict]):
    # 提取用于 Embedding 的文本
    texts = [x["raw_text_for_emb"] for x in batch]

    # 计算向量 (Normalize=True 很重要，便于后续用 IP 算分)
    embeddings = model.encode(texts, normalize_embeddings=True)

    # 插入数据 (注意顺序必须和 Schema definition 一致)
    data = [
        [x["full_name"] for x in batch],  # full_name
        embeddings.tolist(),  # embedding
        [x["db"] for x in batch],  # db
        [x["logical_table"] for x in batch],  # logical_table
        [x["domain"] for x in batch],  # domain
        [x["risk_level"] for x in batch],  # risk_level
        [x["table_type"] for x in batch],  # table_type
        [x["text"] for x in batch],  # text
    ]

    col.insert(data)


def main():
    if not os.path.exists(SOURCE_FILE):
        logger.error(f"❌ File not found: {SOURCE_FILE}. Please run extract_schema_to_jsonl.py first.")
        return

    logger.info(f"🧠 Loading embedding model: {EMBED_MODEL}...")
    try:
        model = SentenceTransformer(EMBED_MODEL)
    except Exception as e:
        logger.error(f"❌ Model load failed: {e}")
        return

    # 测算维度
    test_emb = model.encode(["test"], normalize_embeddings=True)
    dim = int(test_emb.shape[1])
    logger.info(f"📏 Vector dimension: {dim}")

    # 初始化 Milvus
    col = init_milvus(dim)

    inserted = 0
    batch = []

    logger.info(f"🚀 Processing data from {SOURCE_FILE}...")
    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue

            try:
                card = json.loads(line)
            except json.JSONDecodeError:
                continue

            ident = card.get("identity", {})
            llm_info = card.get("llm", {})

            # 这里的 text 已经是 ETL 生成好的 Rich Text (带锚点和清洗过的同义词)
            raw_text = card.get("text", "")
            safe_text = raw_text[:TEXT_MAX_LEN]

            # 构造 full_name (主键)
            # 注意：这里的 logical_table 已经是归一化后的 (如 t_order)
            full_name = f"{ident.get('db')}.{ident.get('logical_table')}"

            entry = {
                "full_name": full_name,
                "db": ident.get("db", ""),
                "logical_table": ident.get("logical_table", ""),
                "domain": ident.get("domain", "unknown"),

                "risk_level": llm_info.get("risk_level", "normal"),
                "table_type": llm_info.get("table_type", "unknown"),

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

    # 刷盘并加载到内存，准备查询
    col.flush()
    # col.load() # 暂时不 Load，留给 retrieve_tables.py 懒加载

    logger.info(f"🎉 All Done! Total {col.num_entities} entities indexed in '{COLLECTION_NAME}'.")


if __name__ == "__main__":
    main()