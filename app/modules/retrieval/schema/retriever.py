import json
import sys
import os
from typing import List, Dict, Any

# ==========================================
# 1. 环境准备
# ==========================================
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))
sys.path.append(project_root)
sys.path.append(current_dir)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from app.core.rag_store import rag_store
from app.core.logger import logger


# ==========================================
# 2. SchemaRetriever (底层召回)
# ==========================================
class SchemaRetriever:
    """
    Schema 向量召回器
    职责：根据查询关键词从 Milvus 中召回相关的列信息
    """

    async def retrieve(
            self,
            query: str,
            db_id: str,
            top_k: int = 8,
            source_group: str = "GENERAL"
    ) -> List[Dict[str, Any]]:
        """
        召回相关列信息

        Args:
            query: 查询关键词（如 "商品 product"）
            db_id: 数据库标识（如 "ecommerce"）
            top_k: 召回数量
            source_group: 分组标签（用于调试）

        Returns:
            包含完整列信息的字典列表
        """
        try:
            hits = rag_store.search_vectors(
                collection_name="schema",
                query_text=query,
                top_k=top_k,
                db_id=db_id
            )
        except Exception as e:
            logger.error(f"❌ [Schema] Search error for '{source_group}': {e}")
            return []

        retrieved_items = []

        # 处理不同格式的返回结果
        iterable_hits = hits
        if isinstance(hits, list) and len(hits) > 0 and isinstance(hits[0], list):
            iterable_hits = hits[0]

        for item in iterable_hits:
            # 提取 entity 对象
            entity = getattr(item, 'entity', None) or item.get('entity')
            if not entity:
                continue

            # 解析 metadata（可能是 dict 或 JSON 字符串）
            meta = {}
            raw_meta = entity.get("metadata") or entity.get("metadata_json")
            if isinstance(raw_meta, dict):
                meta = raw_meta
            elif isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except:
                    pass

            # 提取表名和列名
            table_name = meta.get("table_name") or entity.get("table_name")
            column_name = meta.get("column_name") or entity.get("column_name")

            if not table_name or not column_name:
                continue

            # 构建完整列信息（保留所有字段供后续使用）
            col_info = {
                # 基础标识
                "table_name": table_name,
                "column_name": column_name,

                # 核心元数据
                "data_type": meta.get("data_type") or entity.get("data_type"),
                "is_nullable": meta.get("is_nullable") or entity.get("is_nullable"),

                # 统计信息
                "sample_values": meta.get("sample_values") or entity.get("sample_values", []),
                "distinct_count": meta.get("distinct_count") or entity.get("distinct_count"),
                "null_count": meta.get("null_count") or entity.get("null_count"),
                "numeric_stats": meta.get("numeric_stats") or entity.get("numeric_stats"),

                # AI 语义描述
                "ai_description": (
                        meta.get("ai_description") or
                        entity.get("ai_description") or
                        entity.get("doc_text") or
                        ""
                ),

                # 召回相关信息
                "score": getattr(item, 'score', 0.0),
                "source_group": source_group
            }
            retrieved_items.append(col_info)

        # 按相关性分数排序
        retrieved_items.sort(key=lambda x: x['score'], reverse=True)
        return retrieved_items


# ==========================================
# 3. 简单测试
# ==========================================
if __name__ == "__main__":
    import asyncio


    async def retriever():
        retriever = SchemaRetriever()

        # 测试查询
        results = await retriever.retrieve(
            query="商品 product",
            db_id="ecommerce",
            top_k=5,
            source_group="商品表"
        )

        print(f"📦 召回了 {len(results)} 列:")
        for item in results:
            print(f"  - {item['table_name']}.{item['column_name']} "
                  f"(score: {item['score']:.4f}, type: {item['data_type']})")
            print(f"    描述: {item['ai_description'][:80]}...")


    asyncio.run(retriever())