import json
from typing import List, Any, Optional
from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType, utility

from app.core.config import settings
from app.core.logger import logger
from app.core.embedding import embedder


class MilvusDAO:
    def __init__(self):
        self._connect_milvus()

        # 1. 动态获取维度
        self.dimension = embedder.dimension
        logger.info(f"📏 Milvus Schema Dimension initialized to: {self.dimension}")

        # 2. 初始化所有集合
        self.schema_col = self._init_schema_collection()
        self.knowledge_col = self._init_knowledge_collection()
        self.few_shot_col = self._init_few_shot_collection()  # 🔥 新增

    def _connect_milvus(self):
        try:
            connections.connect(
                alias="default",
                host=settings.MILVUS_HOST,
                port=settings.MILVUS_PORT
            )
        except Exception as e:
            logger.error(f"❌ Milvus Connection Failed: {e}")

    # ==========================================
    # 1. Schema 集合 (列结构)
    # ==========================================
    def _init_schema_collection(self):
        name = "rag_schema_bird"
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.dimension),
            FieldSchema(name="db_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="table_name", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="column_name", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="is_pk", dtype=DataType.BOOL),
            FieldSchema(name="is_fk", dtype=DataType.BOOL),
            FieldSchema(name="doc_text", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name="metadata_json", dtype=DataType.VARCHAR, max_length=65535)
        ]
        return self._get_or_create_collection(name, fields, "BIRD Database Schema")

    # ==========================================
    # 2. Knowledge 集合 (业务规则)
    # ==========================================
    def _init_knowledge_collection(self):
        name = "rag_knowledge_bird"
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.dimension),
            FieldSchema(name="db_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="doc_text", dtype=DataType.VARCHAR, max_length=4000),
            FieldSchema(name="rule_text", dtype=DataType.VARCHAR, max_length=4000)
        ]
        return self._get_or_create_collection(name, fields, "BIRD Business Rules")

    # ==========================================
    # 3. Few-Shot 集合 (SQL 案例) 🔥
    # ==========================================
    def _init_few_shot_collection(self):
        name = "few_shot"  # 对应 Orchestrator 里的名字
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=128, is_primary=True, auto_id=False),
            # ID由外部控制 fs_001
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.dimension),
            FieldSchema(name="db_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="question", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="sql", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="evidence", dtype=DataType.VARCHAR, max_length=2048)
        ]
        return self._get_or_create_collection(name, fields, "BIRD SQL Few-Shot Examples")

    # ==========================================
    # 通用辅助：创建集合
    # ==========================================
    def _get_or_create_collection(self, name, fields, desc):
        schema = CollectionSchema(fields, description=desc)
        if utility.has_collection(name):
            col = Collection(name)
            col.load()
            return col

        col = Collection(name, schema)
        # 根据集合类型选择索引参数
        if name == "rag_schema_bird":
            idx_params = {"M": 16, "efConstruction": 200}
            idx_type = "HNSW"
        else:
            idx_params = {"nlist": 128}
            idx_type = "IVF_FLAT"

        col.create_index("vector", {
            "metric_type": "COSINE",
            "index_type": idx_type,
            "params": idx_params
        })
        col.load()
        return col

    # ==========================================
    # 4. 存入数据 (通用)
    # ==========================================
    def add_documents(self, collection_name: str, documents: List[str], metadatas: List[dict], ids: List[Any] = None):
        if collection_name == "schema":
            col = self.schema_col
        elif collection_name == "knowledge":
            col = self.knowledge_col
        elif collection_name == "few_shot":
            col = self.few_shot_col
        else:
            return

        if not documents: return

        # Embedding
        vecs = embedder.encode(documents, normalize_embeddings=True)

        # 组装插入数据
        data = []

        # 1. IDs (如果是 auto_id=True 的集合，不需要传 ID，除了 few_shot)
        if collection_name == "few_shot":
            if not ids: raise ValueError("Few-Shot collection requires explicit IDs")
            data.append(ids)  # id column

        # 2. Vectors
        data.append(vecs.tolist())  # vector column

        # 3. Metadatas
        # 解析 Metadata 里的字段，按 Schema 顺序放入
        if collection_name == "schema":
            data.append([m.get('db_id', '') for m in metadatas])
            data.append([m.get('table_name', '') for m in metadatas])
            data.append([m.get('column_name', '') for m in metadatas])
            data.append([m.get('is_pk', False) for m in metadatas])
            data.append([m.get('is_fk', False) for m in metadatas])
            data.append(documents)  # doc_text
            data.append([json.dumps(m) for m in metadatas])  # metadata_json

        elif collection_name == "knowledge":
            data.append([m.get('db_id', '') for m in metadatas])
            data.append(documents)  # doc_text
            data.append([m.get('rule_text', '') for m in metadatas])

        elif collection_name == "few_shot":
            data.append([m.get('db_id', '') for m in metadatas])
            data.append([m.get('question', '') for m in metadatas])
            data.append([m.get('sql', '') for m in metadatas])
            data.append([m.get('evidence', '') for m in metadatas])

        try:
            col.insert(data)
            col.flush()  # 确保写入
            logger.info(f"✅ Inserted {len(documents)} docs into {collection_name}")
        except Exception as e:
            logger.error(f"❌ Insert failed: {e}")

    # ==========================================
    # 5. 通用检索 (参数顺序优化版)
    # ==========================================
    def search_vectors(self, collection_name: str, query_text: str, db_id: str = None, top_k: int = 5):
        """
        :param collection_name: schema | knowledge | few_shot
        :param query_text: 用户问题
        :param db_id: [重要] 数据库过滤，默认为 None
        :param top_k: 返回数量，默认为 5
        """
        if not query_text: return []

        if collection_name == "schema":
            col = self.schema_col
            output_fields = ["table_name", "column_name", "doc_text", "metadata_json", "is_pk"]
        elif collection_name == "knowledge":
            col = self.knowledge_col
            output_fields = ["rule_text", "doc_text"]
        elif collection_name == "few_shot":
            col = self.few_shot_col
            output_fields = ["question", "sql", "evidence", "db_id"]
        else:
            logger.warning(f"Unknown collection: {collection_name}")
            return []

        # 1. Embedding
        try:
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


rag_store = MilvusDAO()