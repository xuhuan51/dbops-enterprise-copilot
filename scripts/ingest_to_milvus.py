import json
import os
import sys
import time
from tqdm import tqdm
from pymilvus import utility

# ==========================================
# 1. 环境准备
# ==========================================
# 确保能导入 app 模块
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

try:
    from app.core.rag_store import rag_store, encoder

    print("✅ 成功导入 rag_store")
except ImportError as e:
    print(f"❌ 导入失败: {e}")
    sys.exit(1)

# 定义数据路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 假设你的数据在 data/bird/metadata 下，请根据实际情况调整 ../
METADATA_DIR = os.path.join(BASE_DIR, "../data/bird/metadata")

SCHEMA_FILE = os.path.join(METADATA_DIR, "schema_catalog.json")
RULES_FILE = os.path.join(METADATA_DIR, "business_rules.json")
BATCH_SIZE = 200


# ==========================================
# 2. 核心：暴力重置函数
# ==========================================
def reset_milvus_collections():
    print("=" * 50)
    print("🧨 正在执行 Milvus 暴力重置 (Drop All)...")
    print("=" * 50)

    # 1. 删除 Schema 集合
    if utility.has_collection("rag_schema_bird"):
        utility.drop_collection("rag_schema_bird")
        print("   🗑️  已删除旧集合: rag_schema_bird")
    else:
        print("   ⚪ 集合 rag_schema_bird 不存在，无需删除")

    # 2. 删除 Knowledge 集合
    if utility.has_collection("rag_knowledge_bird"):
        utility.drop_collection("rag_knowledge_bird")
        print("   🗑️  已删除旧集合: rag_knowledge_bird")
    else:
        print("   ⚪ 集合 rag_knowledge_bird 不存在，无需删除")

    # 3. 关键一步：重新初始化 rag_store 对象
    # 这会强制执行 _init_schema_collection，按新代码创建新表
    print("   🆕 正在根据最新的代码重建集合...")
    rag_store.__init__()
    print("   ✅ 重置完成！现在的 Milvus 结构是最新的。")


# ==========================================
# 3. 入库逻辑 (严格匹配你的字段顺序)
# ==========================================
def ingest_schema():
    if not os.path.exists(SCHEMA_FILE):
        print(f"❌ 找不到文件: {SCHEMA_FILE}")
        return

    print(f"\n🚀 开始导入 Schema: {SCHEMA_FILE}")
    with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 分批处理
    for i in tqdm(range(0, len(data), BATCH_SIZE)):
        batch = data[i: i + BATCH_SIZE]

        # 容器准备 (8个字段，对应你代码中的 fields 定义)
        vectors = []
        db_ids = []
        table_names = []
        column_names = []
        is_pks = []
        is_fks = []
        doc_texts = []
        metadatas = []

        # 批量 Embedding
        texts = [item["doc_text"] for item in batch]
        try:
            vecs = encoder.encode(texts, normalize_embeddings=True).tolist()
        except Exception as e:
            print(f"Embedding error: {e}")
            continue

        for idx, item in enumerate(batch):
            # 1. vector
            vectors.append(vecs[idx])
            # 2. db_id
            db_ids.append(item.get("db_id", ""))
            # 3. table_name
            table_names.append(item.get("table", ""))
            # 4. column_name
            column_names.append(item.get("column", ""))
            # 5. is_pk (注意转换 boolean)
            is_pks.append(bool(item.get("is_pk", False)))
            # 6. is_fk (注意转换 boolean)
            is_fks.append(bool(item.get("is_fk", False)))
            # 7. doc_text (这是之前报错缺少的)
            doc_texts.append(item.get("doc_text", ""))
            # 8. metadata_json
            meta = {
                "column_type": item.get("column_type"),
                "fk_to": item.get("fk_to", []),
                "samples": item.get("samples", [])
            }
            metadatas.append(json.dumps(meta, ensure_ascii=False))

        # 执行插入
        rag_store.schema_col.insert([
            vectors, db_ids, table_names, column_names,
            is_pks, is_fks, doc_texts, metadatas
        ])

    # 刷盘 & 建索引
    rag_store.schema_col.flush()
    idx_params = {"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 200}}
    rag_store.schema_col.create_index("vector", idx_params)
    print(f"✅ Schema 入库完毕。总数: {rag_store.schema_col.num_entities}")


def ingest_knowledge():
    if not os.path.exists(RULES_FILE):
        print(f"⚠️ 跳过知识库: 找不到 {RULES_FILE}")
        return

    print(f"\n🚀 开始导入 Knowledge: {RULES_FILE}")
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    for i in tqdm(range(0, len(data), BATCH_SIZE)):
        batch = data[i: i + BATCH_SIZE]

        texts = [item["doc_text"] for item in batch]
        vecs = encoder.encode(texts, normalize_embeddings=True).tolist()

        vectors = []
        db_ids = []
        doc_texts = []
        rule_texts = []

        for idx, item in enumerate(batch):
            vectors.append(vecs[idx])
            db_ids.append(item.get("db_id", ""))
            doc_texts.append(item.get("doc_text", ""))
            rule_texts.append(item.get("rule_text", ""))

        rag_store.knowledge_col.insert([vectors, db_ids, doc_texts, rule_texts])

    rag_store.knowledge_col.flush()
    idx_params = {"metric_type": "COSINE", "index_type": "IVF_FLAT", "params": {"nlist": 128}}
    rag_store.knowledge_col.create_index("vector", idx_params)
    print(f"✅ Knowledge 入库完毕。总数: {rag_store.knowledge_col.num_entities}")


# ==========================================
# 主入口
# ==========================================
if __name__ == "__main__":
    # 1. 先删后建
    reset_milvus_collections()

    # 2. 重新入库
    ingest_schema()
    ingest_knowledge()

    print("\n✨ 全部搞定！现在请运行 diagnose_system.py 进行测试。")