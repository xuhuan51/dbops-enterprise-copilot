import asyncio
from typing import List, Dict, Any
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
        # 如果没传 executor，就用 None，让 asyncio.to_thread 自动处理
        self._executor = executor

    async def search_knowledge(
            self,
            knowledge_keywords: List[str],  # 兼容接口，但主要用 query
            knowledge_query: str,
            db_id: str,  # 🔥 BIRD 核心：必须带 db_id 防止串库
            each_top_k: int = 5
    ) -> List[str]:
        """
        检索与 Query 最相关的业务规则
        返回：规则文本列表 (List[str])
        """
        # 兜底逻辑：如果有关键词就用关键词搜，否则用原问题搜
        # 在 BIRD 场景下，直接用原问题搜规则通常效果更好
        query_text = knowledge_query
        if knowledge_keywords and len(knowledge_keywords) > 0:
            query_text = " ".join(knowledge_keywords)

        if not query_text:
            return []

        loop = asyncio.get_running_loop()

        try:
            # 在线程池中执行同步的 Milvus 查询
            hits = await loop.run_in_executor(
                self._executor,
                lambda: rag_store.search_vectors(
                    collection_name="knowledge",
                    query_text=query_text,
                    top_k=each_top_k,
                    db_id=db_id  # 🔥 关键：只搜当前库的规则
                )
            )
        except Exception as e:
            logger.error(f"❌ [Knowledge] Search failed: {e}")
            return []

        rules = []
        seen_rules = set()

        # 处理结果
        # rag_store.search_vectors 返回的是 [[Hit, Hit...]] (批量搜索结果)
        if hits and len(hits) > 0:
            for hit in hits[0]:  # 我们只搜了一条 query，所以取 hits[0]
                entity = hit.entity

                # 获取规则文本 (对应 rag_store 里的 schema)
                rule_text = entity.get("rule_text")

                # 简单去重
                if rule_text and rule_text not in seen_rules:
                    seen_rules.add(rule_text)
                    rules.append(rule_text)

        logger.info(f"📚 [Knowledge] Found {len(rules)} rules for db={db_id}")
        return rules

    # 兼容旧接口的别名方法 (如果 Orchestrator 还在调用 retrieve)
    async def retrieve(self, query: str, db_id: str, top_k: int = 5):
        return await self.search_knowledge([], query, db_id, top_k)