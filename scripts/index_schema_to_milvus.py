import os, json
from typing import List, Dict, Tuple
from dotenv import load_dotenv

from pymilvus import connections, FieldSchema, CollectionSchema, DataType, Collection, utility
from sentence_transformers import SentenceTransformer
import numpy as np

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(project_root, ".env"))

MILVUS_HOST = os.getenv("MILVUS_HOST", "127.0.0.1")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLLECTION_NAME = os.getenv("MILVUS_SCHEMA_COLLECTION", "schema_catalog")

CATALOG_PATH = os.path.join(project_root, "data", "schema_catalog.jsonl")

EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
BATCH_SIZE = int(os.getenv("MILVUS_INSERT_BATCH", "256"))
RECREATE = os.getenv("MILVUS_RECREATE", "0") == "1"

# --------- 规则：从字段名推断 join/time/metric ----------
JOIN_KEY_CAND = {
    "uid", "user_id", "buyer_id", "seller_id",
    "oid", "order_id",
    "sku_id", "spu_id",
    "shop_id", "store_id",
    "app_id", "tenant_id",
    "dept_id", "org_id",
}
TIME_KEYWORDS = ["time", "date", "dt", "created", "create", "updated", "update", "ts", "timestamp"]
METRIC_KEYWORDS = ["amount", "amt", "price", "cost", "fee", "gmv", "revenue", "qty", "count", "cnt", "num", "total", "score"]

def safe_json(x) -> str:
    return json.dumps(x, ensure_ascii=False)

def infer_domain(db: str, table: str) -> str:
    s = f"{db}.{table}".lower()
    if "trade" in s or "order" in s or "pay" in s:
        return "trade"
    if "user" in s or s.startswith("corp_user"):
        return "user"
    if "mkt" in s or "marketing" in s or "coupon" in s or "activity" in s:
        return "marketing"
    if "scm" in s or "erp" in s or "supplier" in s or "purchase" in s or "stock" in s or "wh_" in s:
        return "scm"
    if "log" in s or "data_log" in s or "access" in s or "err" in s:
        return "log"
    return "unknown"

def infer_features(columns: List[Dict]) -> Tuple[List[str], List[str], List[str]]:
    join_keys, time_cols, metric_cols = [], [], []
    for c in columns:
        name = (c.get("name") or "").lower()

        if name in JOIN_KEY_CAND or name.endswith("_id"):
            join_keys.append(c.get("name"))

        if any(k in name for k in TIME_KEYWORDS):
            time_cols.append(c.get("name"))

        if any(k in name for k in METRIC_KEYWORDS):
            metric_cols.append(c.get("name"))

    # 去重保持顺序
    def dedup(lst):
        seen = set()
        out = []
        for x in lst:
            if x and x not in seen:
                out.append(x)
                seen.add(x)
        return out

    return dedup(join_keys), dedup(time_cols), dedup(metric_cols)

def ensure_collection(dim: int) -> Collection:
    if RECREATE and utility.has_collection(COLLECTION_NAME):
        print(f"⚠️ drop collection: {COLLECTION_NAME}")
        utility.drop_collection(COLLECTION_NAME)

    if utility.has_collection(COLLECTION_NAME):
        col = Collection(COLLECTION_NAME)
        return col

    fields = [
        FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),

        FieldSchema(name="db", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="table", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="full_name", dtype=DataType.VARCHAR, max_length=512),

        FieldSchema(name="domain", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="owner", dtype=DataType.VARCHAR, max_length=128),     # 先空，后续接 CMDB
        FieldSchema(name="app", dtype=DataType.VARCHAR, max_length=128),       # 先空，后续接 CMDB

        FieldSchema(name="perm_tag", dtype=DataType.VARCHAR, max_length=256),  # 先默认
        FieldSchema(name="sensitivity", dtype=DataType.INT64),                 # 先默认 0

        FieldSchema(name="join_keys", dtype=DataType.VARCHAR, max_length=2048),    # JSON string
        FieldSchema(name="time_cols", dtype=DataType.VARCHAR, max_length=2048),    # JSON string
        FieldSchema(name="metric_cols", dtype=DataType.VARCHAR, max_length=2048),  # JSON string

        FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=8192),
        FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dim),
    ]

    schema = CollectionSchema(fields, description="schema catalog for table retrieval")
    col = Collection(COLLECTION_NAME, schema=schema)

    # 用 IP + normalize_embeddings=True 等价 cosine
    index_params = {
        "index_type": "HNSW",
        "metric_type": "IP",
        "params": {"M": 16, "efConstruction": 200},
    }
    col.create_index(field_name="vector", index_params=index_params)
    col.load()
    return col

def read_catalog(path: str) -> List[Dict]:
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items

def batched(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def truncate_text(s: str, max_len: int = 8000) -> str:
    if s is None:
        return ""
    if len(s) <= max_len:
        return s
    return s[:max_len]

def main():
    if not os.path.exists(CATALOG_PATH):
        raise FileNotFoundError(f"catalog not found: {CATALOG_PATH}，先跑 scripts/extract_schema_catalog.py")

    print(f"🔌 connect milvus {MILVUS_HOST}:{MILVUS_PORT}")
    connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)

    print(f"🧠 load embedding model: {EMBED_MODEL}")
    model = SentenceTransformer(EMBED_MODEL)

    items = read_catalog(CATALOG_PATH)
    print(f"📄 catalog items: {len(items)}")

    # 推断 dim
    sample_vec = model.encode([items[0]["text"]], normalize_embeddings=True)
    dim = int(sample_vec.shape[1])

    col = ensure_collection(dim=dim)
    print(f"📦 collection: {COLLECTION_NAME}, dim={dim}")

    inserted = 0
    for chunk in batched(items, BATCH_SIZE):
        texts = [truncate_text(x.get("text", ""), 8000) for x in chunk]
        vecs = model.encode(texts, normalize_embeddings=True).astype(np.float32)

        dbs = []
        tables = []
        full_names = []
        domains = []
        owners = []
        apps = []
        perm_tags = []
        sensitivities = []
        join_keys_list = []
        time_cols_list = []
        metric_cols_list = []
        final_texts = []

        for x, t in zip(chunk, texts):
            db = x.get("db", "")
            table = x.get("table", "")
            full_name = f"{db}.{table}"
            domain = infer_domain(db, table)

            columns = x.get("columns", []) or []
            join_keys, time_cols, metric_cols = infer_features(columns)

            # 暂时拿不到的先给默认值
            owner = ""               # TODO: 接 CMDB
            app = ""                 # TODO: 接 CMDB
            perm_tag = "default"     # TODO: 接权限系统/角色
            sensitivity = 0          # TODO: 分类分级

            dbs.append(db)
            tables.append(table)
            full_names.append(full_name)
            domains.append(domain)
            owners.append(owner)
            apps.append(app)
            perm_tags.append(perm_tag)
            sensitivities.append(sensitivity)

            join_keys_list.append(safe_json(join_keys))
            time_cols_list.append(safe_json(time_cols))
            metric_cols_list.append(safe_json(metric_cols))
            final_texts.append(t)

        col.insert([
            dbs, tables, full_names,
            domains, owners, apps,
            perm_tags, sensitivities,
            join_keys_list, time_cols_list, metric_cols_list,
            final_texts,
            vecs.tolist()
        ])

        inserted += len(chunk)
        if inserted % (BATCH_SIZE * 5) == 0:
            print(f"  inserted: {inserted}")

    col.flush()
    col.load()
    print(f"✅ done. total inserted: {inserted}")
    print("👉 下一步：做 /api/v1/retrieve_tables 检索接口")

if __name__ == "__main__":
    main()
