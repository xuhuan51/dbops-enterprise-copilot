import json
from typing import List, Dict, Any
from pymilvus import (
    connections, Collection, CollectionSchema, FieldSchema,
    DataType, utility
)

from app.core.embedding import embedder
from app.core.logger import logger
from app.core.config import settings


class MilvusDAO:
    def __init__(self):
        self._connect_milvus()
        self.dimension = embedder.dimension
        logger.info(f"📏 Milvus Schema Dimension initialized to: {self.dimension}")

        self.schema_col = self._init_schema_collection()
        self.knowledge_col = self._init_knowledge_collection()
        self.few_shot_col = self._init_few_shot_collection()

    def _connect_milvus(self):
        try:
            connections.connect(
                alias="default",
                host=settings.MILVUS_HOST,
                port=settings.MILVUS_PORT
            )
        except Exception as e:
            logger.error(f"❌ Milvus Connection Failed: {e}")

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

    def _init_few_shot_collection(self):
        name = "few_shot"
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=128, is_primary=True, auto_id=False),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.dimension),
            FieldSchema(name="db_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="question", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="sql", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="evidence", dtype=DataType.VARCHAR, max_length=2048)
        ]
        return self._get_or_create_collection(name, fields, "BIRD SQL Few-Shot Examples")

    def drop_collection(self, collection_name: str):
        """清空指定的集合数据"""
        name_map = {
            "schema": "rag_schema_bird",
            "knowledge": "rag_knowledge_bird",
            "few_shot": "few_shot"
        }
        target_name = name_map.get(collection_name)
        if target_name and utility.has_collection(target_name):
            utility.drop_collection(target_name)
            logger.info(f"🗑️ 已清空 Milvus 集合: {target_name}")
            if collection_name == "schema":
                self.schema_col = self._init_schema_collection()
            elif collection_name == "knowledge":
                self.knowledge_col = self._init_knowledge_collection()
            elif collection_name == "few_shot":
                self.few_shot_col = self._init_few_shot_collection()

    def _get_or_create_collection(self, name, fields, desc):
        schema = CollectionSchema(fields, description=desc)
        if utility.has_collection(name):
            col = Collection(name)
            col.load()
            return col

        col = Collection(name, schema)
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

        vecs = embedder.encode(documents, normalize_embeddings=True)
        data = []

        if collection_name == "few_shot":
            if not ids: raise ValueError("Few-Shot collection requires explicit IDs")
            data.append(ids)

        data.append(vecs.tolist())

        if collection_name == "schema":
            data.append([m.get('db_id', '') for m in metadatas])
            data.append([m.get('table_name', '') for m in metadatas])
            data.append([m.get('column_name', '') for m in metadatas])
            data.append([m.get('is_pk', False) for m in metadatas])
            data.append([m.get('is_fk', False) for m in metadatas])
            data.append(documents)
            data.append([json.dumps(m) for m in metadatas])

        elif collection_name == "knowledge":
            data.append([m.get('db_id', '') for m in metadatas])
            data.append(documents)
            data.append([m.get('rule_text', '') for m in metadatas])

        elif collection_name == "few_shot":
            data.append([m.get('db_id', '') for m in metadatas])
            data.append([m.get('question', '') for m in metadatas])
            data.append([m.get('sql', '') for m in metadatas])
            data.append([m.get('evidence', '') for m in metadatas])

        try:
            col.insert(data)
            col.flush()
            logger.info(f"✅ Inserted {len(documents)} docs into {collection_name}")
        except Exception as e:
            logger.error(f"❌ Insert failed: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # 🔥 修改后的 search_vectors（返回处理好的数据）
    # ═══════════════════════════════════════════════════════════════════════
    def search_vectors(self, collection_name: str, query_text: str, db_id: str = None, top_k: int = 5):
        """
        统一的向量检索接口

        Returns:
            处理好的列表，格式根据 collection_name 不同而不同：
            - schema: [{"table_name": ..., "column_name": ..., "data_type": ..., ...}, ...]
            - knowledge: [{"content": ..., "score": ..., ...}, ...]
            - few_shot: [{"question": ..., "sql": ..., ...}, ...]
        """
        if not query_text:
            return []

        if collection_name == "schema":
            col = self.schema_col
            output_fields = ["table_name", "column_name", "doc_text", "metadata_json", "is_pk"]
        elif collection_name == "knowledge":
            col = self.knowledge_col
            output_fields = ["rule_text", "doc_text", "db_id"]
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
            raw_result = col.search(
                data=[vec],
                anns_field="vector",
                param={"metric_type": "COSINE", "params": {"nprobe": 10}},
                limit=top_k,
                expr=expr,
                output_fields=output_fields
            )
        except Exception as e:
            logger.error(f"Milvus search error: {e}")
            return []

        # 🔥 4. 处理返回结果
        return self._process_search_result(raw_result, collection_name)

    def _process_search_result(self, raw_result, collection_name: str) -> List[Dict]:
        """
        将 Milvus 的 SearchResult 转换为干净的列表
        """
        results = []

        # SearchResult 是二维结构: [[hit1, hit2, ...]]
        for batch in raw_result:
            for hit in batch:
                entity = hit.entity

                if collection_name == "schema":
                    # 解析 metadata_json
                    metadata = {}
                    metadata_json = entity.get('metadata_json', '{}')
                    if metadata_json:
                        try:
                            metadata = json.loads(metadata_json)
                        except:
                            logger.warning(f"⚠️ Failed to parse metadata_json")

                    # 构建统一的列信息
                    col_info = {
                        "table_name": metadata.get('table_name') or entity.get('table_name'),
                        "column_name": metadata.get('column_name') or entity.get('column_name'),
                        "data_type": metadata.get('data_type'),
                        "is_nullable": metadata.get('is_nullable'),
                        "sample_values": metadata.get('sample_values', []),
                        "distinct_count": metadata.get('distinct_count'),
                        "null_count": metadata.get('null_count'),
                        "numeric_stats": metadata.get('numeric_stats'),
                        "ai_description": metadata.get('ai_description', ''),
                        "score": hit.distance
                    }
                    results.append(col_info)

                elif collection_name == "knowledge":
                    knowledge_info = {
                        "content": entity.get('rule_text') or entity.get('doc_text', ''),
                        "score": hit.distance,
                        "source": "knowledge_base",
                        "db_id": entity.get('db_id', '')
                    }
                    results.append(knowledge_info)

                elif collection_name == "few_shot":
                    few_shot_info = {
                        "question": entity.get('question', ''),
                        "sql": entity.get('sql', ''),
                        "evidence": entity.get('evidence', ''),
                        "score": hit.distance,
                        "db_id": entity.get('db_id', '')
                    }
                    results.append(few_shot_info)

        return results


rag_store = MilvusDAO()