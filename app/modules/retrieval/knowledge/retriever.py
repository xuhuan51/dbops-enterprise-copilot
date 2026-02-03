import asyncio
from typing import List, Dict, Any, Union
from concurrent.futures import ThreadPoolExecutor

from app.core.rag_store import rag_store
from app.core.logger import logger


class KnowledgeRetriever:
    """
    [Updated for BIRD]
    负责检索业务规则 (Business Rules / Evidence)。
    数据来源：rag_knowledge_bird 集合 (通过 business_rules.json 加载)
    """

    def __init__(self, executor: ThreadPoolExecutor = None):
        self._executor = executor

    async def search_knowledge(
            self,
            knowledge_keywords: List[str],
            knowledge_query: str,
            db_id: str,
            each_top_k: int = 3
    ) -> List[Dict[str, Any]]:
        """
        检索与 Query 最相关的业务规则
        返回：结构化规则列表 [{"content": "...", "score": 0.85}, ...]
        """
        # 1. 构造查询文本
        query_text = knowledge_query
        # 如果有关键词，优先拼关键词（有时比长句效果好）
        if knowledge_keywords:
            query_text = f"{query_text} {' '.join(knowledge_keywords)}"

        if not query_text.strip():
            return []

        try:
            # 2. 执行向量检索 (异步转同步)
            # 注意：这里假设 rag_store.search_vectors 返回的是 Milvus 的 Hits 对象列表
            hits = await asyncio.to_thread(
                rag_store.search_vectors,
                collection_name="knowledge",
                query_text=query_text,
                top_k=each_top_k,
                db_id=db_id
            )
        except Exception as e:
            logger.error(f"❌ [Knowledge] Search failed: {e}")
            return []

        rules = []
        seen_content = set()

        # 3. 解析结果
        # search_vectors 通常返回 List[List[Hit]] (因为支持批量搜)，我们只取第 0 个
        if hits and isinstance(hits, list) and len(hits) > 0:
            # 兼容性处理：有时返回的是直接的 Hits 列表，有时是嵌套列表
            target_hits = hits[0] if isinstance(hits[0], list) else hits

            for hit in target_hits:
                # 获取 entity (Payload)
                entity = getattr(hit, 'entity', {})
                if not isinstance(entity, dict):
                    # 尝试从对象转 dict (如果有 to_dict 方法)
                    entity = entity.to_dict() if hasattr(entity, 'to_dict') else {}

                # 获取规则文本
                # 假设你的 Milvus schema 里存的是 "content" 或 "rule_text"
                rule_text = entity.get("content") or entity.get("rule_text") or entity.get("evidence")
                score = getattr(hit, 'score', getattr(hit, 'distance', 0.0))

                if rule_text and rule_text not in seen_content:
                    seen_content.add(rule_text)
                    rules.append({
                        "content": rule_text,
                        "score": round(score, 4),
                        "source": "knowledge_base"
                    })

        logger.info(f"📚 [Knowledge] Found {len(rules)} rules for db={db_id}")
        return rules

    # 兼容旧接口
    async def retrieve(self, query: str, db_id: str, top_k: int = 3):
        return await self.search_knowledge([], query, db_id, top_k)