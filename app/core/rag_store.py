import json
from sentence_transformers import SentenceTransformer
from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType, utility

from app.core.config import settings
from app.core.logger import logger

# ==========================================
# 1. 动态加载 BGE-M3 (保持不变)
# ==========================================
encoder = SentenceTransformer(settings.EMBED_MODEL)
_test_vec = encoder.encode("check dimension")
DIMENSION = len(_test_vec)
logger.info(f"📏 Model loaded: {settings.EMBED_MODEL}, Dimension: {DIMENSION}")


class MilvusDAO:
    def __init__(self):
        self._connect_milvus()
        self.schema_col = self._init_schema_collection()
        self.knowledge_col = self._init_knowledge_collection()

    def _connect_milvus(self):
        try:
            # 生产环境建议从 settings 读取 host/port
            connections.connect(alias="default", host="localhost", port="19530")
        except Exception as e:
            logger.error(f"❌ Milvus Connection Failed: {e}")

    # ==========================================
    # 2. Schema 集合 (适配 build_bird_catalog)
    # ==========================================
    def _init_schema_collection(self):
        name = "rag_schema_bird"

        # 定义字段结构
        fields = [
            # 唯一主键
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            # 核心向量
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=DIMENSION),

            # --- 过滤字段 (用于 expr 过滤) ---
            FieldSchema(name="db_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="table_name", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="column_name", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="is_pk", dtype=DataType.BOOL),  # 新增：方便只搜主键
            FieldSchema(name="is_fk", dtype=DataType.BOOL),  # 新增：方便只搜外键

            # --- 内容字段 ---
            # 你的 doc_text 很长，包含了 dataset | table | column | samples 等
            FieldSchema(name="doc_text", dtype=DataType.VARCHAR, max_length=8192),

            # 原始 JSON (存 num_profile, samples 列表, fk_to 详情等)
            FieldSchema(name="metadata_json", dtype=DataType.VARCHAR, max_length=65535)
        ]

        schema = CollectionSchema(fields, description="BIRD Database Schema Knowledge")

        # 初始化或加载
        if utility.has_collection(name):
            col = Collection(name)
            # 如果schema变了，这里可能需要 drop 再 create，开发阶段手动 drop 即可
            col.load()
            return col

        col = Collection(name, schema)
        # HNSW 索引：平衡速度和精度
        col.create_index("vector", {
            "metric_type": "COSINE",
            "index_type": "HNSW",
            "params": {"M": 16, "efConstruction": 200}
        })
        col.load()
        return col

    # ==========================================
    # 3. Knowledge 集合 (适配 build_business_rule_base)
    # ==========================================
    def _init_knowledge_collection(self):
        name = "rag_knowledge_bird"

        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=DIMENSION),

            # --- 过滤字段 ---
            FieldSchema(name="db_id", dtype=DataType.VARCHAR, max_length=128),

            # --- 内容字段 ---
            # 对应你的 "doc_text": f"Business Rule for {db_id}: {rule}"
            FieldSchema(name="doc_text", dtype=DataType.VARCHAR, max_length=4000),
            # 对应你的 "rule_text" (纯净规则)
            FieldSchema(name="rule_text", dtype=DataType.VARCHAR, max_length=4000)
        ]

        schema = CollectionSchema(fields, description="BIRD Business Rules")

        if utility.has_collection(name):
            col = Collection(name)
            col.load()
            return col

        col = Collection(name, schema)
        col.create_index("vector", {
            "metric_type": "COSINE",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 128}
        })
        col.load()
        return col

    # ==========================================
    # 4. 通用检索方法 (增强版)
    # ==========================================
    def search_vectors(self, collection_name: str, query_text: str, top_k: int = 5, db_id: str = None):
        """
        :param db_id: 如果传入，则只在该数据库范围内搜索 (Partition/Filter)
        """
        if collection_name == "schema":
            col = self.schema_col
            # 默认返回字段
            output_fields = ["table_name", "column_name", "doc_text", "metadata_json", "is_pk"]
        elif collection_name == "knowledge":
            col = self.knowledge_col
            output_fields = ["rule_text", "doc_text"]
        else:
            return []

        # 1. Embedding
        try:
            vec = encoder.encode([query_text], normalize_embeddings=True)[0].tolist()
        except Exception as e:
            logger.error(f"Embedding error: {e}")
            return []

        # 2. 构建过滤表达式 (Expr)
        # BIRD 是多库数据集，一定要防止串库！
        expr = None
        if db_id:
            expr = f"db_id == '{db_id}'"

        # 3. Search
        try:
            res = col.search(
                data=[vec],
                anns_field="vector",
                param={"metric_type": "COSINE", "params": {"nprobe": 10}},
                limit=top_k,
                expr=expr,  # <--- 关键：加上了 filtering
                output_fields=output_fields
            )
            return res
        except Exception as e:
            logger.error(f"Milvus search error: {e}")
            return []


# 实例化单例
rag_store = MilvusDAO()