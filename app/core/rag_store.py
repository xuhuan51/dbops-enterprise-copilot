import json
import sys
from typing import List, Dict, Any, Optional
from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType, utility
from sentence_transformers import SentenceTransformer
from app.core.config import settings
from app.core.logger import logger

# ==========================================
# 1. 加载 Embedding 模型 (基础能力)
# ==========================================
logger.info(f"⏳ [Init] Loading Embedding Model: {settings.EMBED_MODEL} ...")
try:
    encoder = SentenceTransformer(settings.EMBED_MODEL)
    # 简单测试一下维度
    _test_vec = encoder.encode("test", normalize_embeddings=True)
    DIMENSION = len(_test_vec)
    logger.info(f"📏 [Init] Model loaded, Dimension: {DIMENSION}")
except Exception as e:
    logger.error(f"❌ [Init] Model load failed: {e}")
    sys.exit(1)


class MilvusDAO:
    """
    基础设施层：Milvus 数据访问对象
    职责：
    1. 管理数据库连接
    2. 定义和初始化 Collection 结构
    3. 提供基础的 CRUD 接口 (Add, Base Vector Search)
    4. 不包含任何业务逻辑 (如重排、混合检索策略)
    """

    def __init__(self):
        self._connect_milvus()

        # 初始化集合对象 (Lazy Load)
        self.schema_col = self._init_schema_collection()
        self.knowledge_col = self._init_knowledge_collection()

    def _connect_milvus(self):
        try:
            connections.connect(alias="default", host=settings.MILVUS_HOST, port=settings.MILVUS_PORT)
        except Exception as e:
            logger.error(f"❌ Milvus Connect Error: {e}")
            sys.exit(1)

    # ==========================================
    # 📦 Collection 初始化 (Schema Definition)
    # ==========================================

    def _init_schema_collection(self):
        """
        左塔 Schema 集合定义
        """
        name = "rag_schema"
        if utility.has_collection(name):
            col = Collection(name)
            col.load()
            return col

        fields = [
            FieldSchema(name="full_name", dtype=DataType.VARCHAR, max_length=256, is_primary=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=DIMENSION),
            # 元数据字段
            FieldSchema(name="db", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="logical_table", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="domain", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="card_json", dtype=DataType.VARCHAR, max_length=65535)
        ]
        col = Collection(name, CollectionSchema(fields, description="Database Schema Store"))
        col.create_index("vector",
                         {"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 200}})
        col.load()
        return col

    def _init_knowledge_collection(self):
        """
        右塔 Knowledge 集合定义
        """
        name = "rag_knowledge"
        if utility.has_collection(name):
            col = Collection(name)
            col.load()
            return col

        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=DIMENSION),
            FieldSchema(name="term", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="category", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="payload_json", dtype=DataType.VARCHAR, max_length=65535)
        ]
        col = Collection(name, CollectionSchema(fields, description="Business Knowledge Store"))
        col.create_index("vector", {"metric_type": "COSINE", "index_type": "IVF_FLAT", "params": {"nlist": 128}})
        col.load()
        return col

    # ==========================================
    # 📥 数据写入 (Ingestion)
    # ==========================================

    def add_schema_card(self, card_dict: Dict):
        """Schema 入库"""
        ident = card_dict.get("identity", {})
        full_name = f"{ident.get('db')}.{ident.get('logical_table')}"

        text = card_dict.get("text", "") or full_name
        vec = encoder.encode([text], normalize_embeddings=True)[0].tolist()

        self.schema_col.upsert([
            [full_name],
            [vec],
            [ident.get("db")],
            [ident.get("logical_table")],
            [ident.get("domain", "default")],
            [json.dumps(card_dict, ensure_ascii=False)]
        ])

    def add_knowledge(self, knowledge_data: Dict):
        """Knowledge 入库"""
        term = knowledge_data.get("term", "unknown")
        category = knowledge_data.get("category", "GENERAL")
        definition = knowledge_data.get("definition", "") or ""
        synonyms = knowledge_data.get("synonyms", [])

        # 构建语义文本 (Term + Synonyms + Definition)
        text_parts = [term]
        if synonyms:
            text_parts.extend(synonyms)
        if definition:
            text_parts.append(definition)

        text = " ".join([str(x) for x in text_parts if x])
        vec = encoder.encode([text], normalize_embeddings=True)[0].tolist()

        self.knowledge_col.insert([
            [vec],
            [term],
            [category],
            [json.dumps(knowledge_data, ensure_ascii=False)]
        ])

    # ==========================================
    # 🔎 基础向量检索 (Base Vector Search)
    # ==========================================

    def search_vectors(self, collection_name: str, query_text: str, top_k: int, output_fields: List[str]):
        """
        通用的向量检索接口。

        :param collection_name: "schema" 或 "knowledge"
        :param query_text: 用户查询文本
        :param top_k: 返回数量 (Recall 数量)
        :param output_fields: 需要返回的字段列表
        :return: Milvus SearchResult 对象 (包含 hits)
        """
        # 1. 确定集合
        if collection_name == "schema":
            collection = self.schema_col
        elif collection_name == "knowledge":
            collection = self.knowledge_col
        else:
            logger.error(f"Unknown collection: {collection_name}")
            return []

        # 2. 生成向量
        try:
            vec = encoder.encode([query_text], normalize_embeddings=True)[0].tolist()
        except Exception as e:
            logger.error(f"Embedding failed: {e}")
            return []

        # 3. 执行搜索 (纯向量 IP/COSINE)
        try:
            res = collection.search(
                data=[vec],
                anns_field="vector",
                param={"metric_type": "COSINE", "params": {"nprobe": 10, "ef": 64}},
                limit=top_k,
                output_fields=output_fields
            )
            return res
        except Exception as e:
            logger.error(f"Milvus search failed: {e}")
            return []


# 全局单例
rag_store = MilvusDAO()