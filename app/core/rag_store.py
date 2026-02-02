import json
from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType, utility

from app.core.config import settings
from app.core.logger import logger
from app.core.embedding import embedder  # ✅ 1. 引入 Embedding 单例

class MilvusDAO:
    def __init__(self):
        self._connect_milvus()

        # ✅ 2. 动态获取维度 (这会触发模型懒加载)
        # BGE-M3 默认为 1024，BGE-Large 为 1024，Base 为 768
        # 这样无论你换什么模型，这里都会自动适配，不用手动改代码
        self.dimension = embedder.dimension
        logger.info(f"📏 Milvus Schema Dimension initialized to: {self.dimension}")

        self.schema_col = self._init_schema_collection()
        self.knowledge_col = self._init_knowledge_collection()

    def _connect_milvus(self):
        try:
            # 建议使用 settings 里的配置，而不是写死 localhost
            connections.connect(
                alias="default",
                host=settings.MILVUS_HOST,
                port=settings.MILVUS_PORT
            )
        except Exception as e:
            logger.error(f"❌ Milvus Connection Failed: {e}")

    # ==========================================
    # 2. Schema 集合
    # ==========================================
    def _init_schema_collection(self):
        name = "rag_schema_bird"

        # 定义字段结构
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            # ✅ 3. 使用 self.dimension
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.dimension),

            FieldSchema(name="db_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="table_name", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="column_name", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="is_pk", dtype=DataType.BOOL),
            FieldSchema(name="is_fk", dtype=DataType.BOOL),
            FieldSchema(name="doc_text", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name="metadata_json", dtype=DataType.VARCHAR, max_length=65535)
        ]

        schema = CollectionSchema(fields, description="BIRD Database Schema Knowledge")

        if utility.has_collection(name):
            col = Collection(name)
            col.load()
            return col

        col = Collection(name, schema)
        col.create_index("vector", {
            "metric_type": "COSINE",
            "index_type": "HNSW",
            "params": {"M": 16, "efConstruction": 200}
        })
        col.load()
        return col

    # ==========================================
    # 3. Knowledge 集合
    # ==========================================
    def _init_knowledge_collection(self):
        name = "rag_knowledge_bird"

        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            # ✅ 3. 使用 self.dimension
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.dimension),

            FieldSchema(name="db_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="doc_text", dtype=DataType.VARCHAR, max_length=4000),
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
    # 4. 通用检索方法
    # ==========================================
    def search_vectors(self, collection_name: str, query_text: str, top_k: int = 5, db_id: str = None):
        """
        :param db_id: 数据库 ID 过滤
        """
        if collection_name == "schema":
            col = self.schema_col
            output_fields = ["table_name", "column_name", "doc_text", "metadata_json", "is_pk"]
        elif collection_name == "knowledge":
            col = self.knowledge_col
            output_fields = ["rule_text", "doc_text"]
        else:
            return []

        # 1. Embedding
        try:
            # ✅ 4. 使用 embedder 单例进行编码
            # 注意：embedder.encode 返回的是 numpy array，需要转 list 才能给 Milvus
            vec_result = embedder.encode([query_text], normalize_embeddings=True)
            vec = vec_result[0].tolist()
        except Exception as e:
            logger.error(f"Embedding error: {e}")
            return []

        # 2. 过滤表达式
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
                expr=expr,
                output_fields=output_fields
            )
            return res
        except Exception as e:
            logger.error(f"Milvus search error: {e}")
            return []

# 实例化单例
rag_store = MilvusDAO()