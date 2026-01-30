import asyncio
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor

from app.core.rag_store import rag_store
from app.core.logger import logger


class KnowledgeRetriever:
    """
    纯粹的右塔：只负责搜业务知识 (rag_knowledge)
    职责：
    1. 接收 Orchestrator 传来的关键词或查询
    2. 调用底层 rag_store 进行向量检索
    3. 格式化返回结果
    """

    def __init__(self, executor: ThreadPoolExecutor):
        # 接收 Orchestrator 传来的线程池，避免重复创建资源
        self._executor = executor

    async def search_knowledge(
            self,
            knowledge_keywords: List[str],
            knowledge_query: str,  # 这里的 query 通常是用户原问题，用于兜底
            each_top_k: int = 3
    ) -> List[Dict[str, Any]]:

        loop = asyncio.get_running_loop()
        tasks = []

        # ----------------------------------------
        # 策略 A: 关键词精确检索 (High Precision)
        # ----------------------------------------
        # 如果 Router 提取了关键词（比如 "大R", "UV"），优先搜这些
        if knowledge_keywords:
            for kw in knowledge_keywords:
                if not kw.strip():
                    continue
                # 放入线程池执行 Milvus IO
                tasks.append(loop.run_in_executor(
                    self._executor,
                    lambda k=kw: rag_store.search_vectors(
                        collection_name="knowledge",
                        query_text=k,
                        top_k=each_top_k,
                        output_fields=["term", "category", "payload_json"]
                    )
                ))

        # ----------------------------------------
        # 策略 B: 问题全文检索 (Recall) - 可选
        # ----------------------------------------
        # 如果没有关键词，或者为了增加召回，可以把原问题也丢进去搜一次
        if not knowledge_keywords and knowledge_query:
            tasks.append(loop.run_in_executor(
                self._executor,
                lambda: rag_store.search_vectors(
                    collection_name="knowledge",
                    query_text=knowledge_query,
                    top_k=each_top_k,
                    output_fields=["term", "category", "payload_json"]
                )
            ))

        if not tasks:
            return []

        # 等待所有搜索完成
        try:
            results_list = await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"❌ [Knowledge] Search failed: {e}")
            return []

        # ----------------------------------------
        # 结果去重与平铺
        # ----------------------------------------
        final_hits: List[Dict[str, Any]] = []
        seen_terms = set()

        for hits in results_list:
            # rag_store 返回的是 Milvus 的 SearchResult 对象列表
            for hit in hits:
                # 假设 rag_store.search_vectors 返回的是 pymilvus 的 Hit 对象
                # 我们需要安全地提取 entity 属性
                entity = getattr(hit, "entity", {})
                term = entity.get("term")

                # 简单的去重策略：同名术语只留分最高的
                if term and term not in seen_terms:
                    seen_terms.add(term)

                    # 尝试解析 payload_json
                    payload = {}
                    try:
                        import json
                        p_str = entity.get("payload_json")
                        if p_str:
                            payload = json.loads(p_str)
                    except:
                        pass

                    # 融合字段
                    item = {
                        "term": term,
                        "category": entity.get("category"),
                        "score": getattr(hit, "score", 0.0),
                        # 展开 payload 里的详细信息 (definition, synonyms, required_tables)
                        **payload
                    }
                    final_hits.append(item)

        # 按分数降序
        final_hits.sort(key=lambda x: x["score"], reverse=True)

        logger.info(f"📚 [Knowledge] Keywords={knowledge_keywords} -> Found {len(final_hits)} entries")
        return final_hits