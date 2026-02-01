import json
import asyncio
from typing import List, Dict, Any
from collections import defaultdict

from app.core.rag_store import rag_store
from app.core.logger import logger


class SchemaRetriever:
    """
    负责从 Milvus 中检索相关的 Table 和 Column 信息。
    """

    async def retrieve(self, query: str, db_id: str, top_k: int = 15) -> List[Dict[str, Any]]:
        """
        检索 Schema 信息
        :param query: 用户的问题
        :param db_id: 目标数据库 ID (必须提供，用于隔离)
        :param top_k: 召回的列数
        """
        try:
            # 1. Milvus 向量检索
            # 注意：传入 db_id 让 rag_store 内部构建 expr="db_id == '...'"
            hits = rag_store.search_vectors(
                collection_name="schema",
                query_text=query,
                top_k=top_k,
                db_id=db_id  # 🔥 关键：物理隔离
            )
        except Exception as e:
            logger.error(f"❌ [Schema] Search failed: {e}")
            return []

        # 2. 聚合结果 (按 Table 分组)
        candidates = defaultdict(list)

        for hit in hits:
            # pymilvus 结果拆包
            for item in hit:
                entity = item.entity
                table_name = entity.get("table_name")

                # 解析元数据
                meta = {}
                try:
                    meta_json = entity.get("metadata_json")
                    if meta_json:
                        meta = json.loads(meta_json)
                except Exception:
                    pass

                col_info = {
                    "name": entity.get("column_name"),
                    "doc_text": entity.get("doc_text"),  # 召回原文
                    "type": meta.get("column_type", "UNKNOWN"),
                    "is_pk": entity.get("is_pk", False),
                    "is_fk": entity.get("is_fk", False),
                    "samples": meta.get("samples", []),  # 🔥 这里的样本是去重过的
                    "score": item.score
                }
                candidates[table_name].append(col_info)

        # 3. 格式化输出
        final_schema = []
        for table, cols in candidates.items():
            # 按相似度分数对列排序，取前 N 个最相关的列
            # 避免把无关的列塞进 Prompt 浪费 token
            cols.sort(key=lambda x: x['score'], reverse=True)

            final_schema.append({
                "table_name": table,
                "columns": cols  # 这里包含该表下最相关的列
            })

        logger.info(f"🗂️ [Schema] Retrieved {len(final_schema)} tables for db={db_id}")
        return final_schema