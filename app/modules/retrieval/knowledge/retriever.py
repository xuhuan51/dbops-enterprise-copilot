import asyncio
from typing import List, Dict, Any, Union, Optional
from concurrent.futures import ThreadPoolExecutor

from app.core.rag_store import rag_store
from app.core.logger import logger


class KnowledgeRetriever:
    """
    业务规则检索器 (Knowledge Retriever)

    职责：从知识库中检索与查询相关的业务规则、约束条件、领域知识等
    数据来源：Milvus collection "knowledge"
    """

    def __init__(self, executor: Optional[ThreadPoolExecutor] = None):
        self._executor = executor

    async def search_knowledge(
            self,
            knowledge_keywords: List[str],
            knowledge_query: str,
            db_id: str,
            each_top_k: int = 3
    ) -> List[Dict[str, Any]]:
        """
        检索与查询最相关的业务规则

        Args:
            knowledge_keywords: 关键词列表（从 search_keywords 提取）
            knowledge_query: 完整问题文本
            db_id: 数据库标识
            each_top_k: 每次检索返回的最大规则数

        Returns:
            规则列表，格式: [{"content": "...", "score": 0.85, "source": "knowledge_base"}, ...]
        """
        # 1. 构造查询文本
        # 策略：关键词 + 完整问题（关键词在前，权重更高）
        query_parts = []
        if knowledge_keywords:
            # 去重并过滤空字符串
            unique_keywords = list(set(k.strip() for k in knowledge_keywords if k.strip()))
            if unique_keywords:
                query_parts.append(" ".join(unique_keywords))

        if knowledge_query and knowledge_query.strip():
            query_parts.append(knowledge_query.strip())

        query_text = " ".join(query_parts)

        if not query_text:
            logger.warning("⚠️ [Knowledge] Empty query, skipping search")
            return []

        try:
            # 2. 执行向量检索
            logger.info(f"🔍 [Knowledge] Searching with query: '{query_text[:100]}...'")

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

        # 3. 解析结果
        rules = []
        seen_content = set()

        # 兼容不同的返回格式
        target_hits = hits
        if isinstance(hits, list) and len(hits) > 0:
            if isinstance(hits[0], list):
                # 嵌套列表：[[Hit, Hit, ...]]
                target_hits = hits[0]
            # 否则就是直接的 [Hit, Hit, ...]

        for hit in target_hits:
            # 提取 entity
            entity = getattr(hit, 'entity', None)
            if entity is None:
                entity = hit.get('entity', {}) if isinstance(hit, dict) else {}

            if not isinstance(entity, dict):
                # 尝试转 dict
                entity = entity.to_dict() if hasattr(entity, 'to_dict') else {}

            # 提取规则文本（多字段兼容）
            rule_text = (
                    entity.get("content") or
                    entity.get("rule_text") or
                    entity.get("evidence") or
                    entity.get("doc_text") or
                    ""
            )

            # 提取分数
            score = getattr(hit, 'score', None)
            if score is None:
                score = getattr(hit, 'distance', 0.0)

            # 去重并添加
            if rule_text and rule_text.strip() and rule_text not in seen_content:
                seen_content.add(rule_text)
                rules.append({
                    "content": rule_text.strip(),
                    "score": round(float(score), 4),
                    "source": "knowledge_base",
                    "db_id": db_id
                })

        logger.info(f"📚 [Knowledge] Found {len(rules)} unique rules for db={db_id}")

        # 按分数降序排列
        rules.sort(key=lambda x: x['score'], reverse=True)
        return rules

    # 兼容旧接口
    async def retrieve(self, query: str, db_id: str, top_k: int = 3):
        return await self.search_knowledge([], query, db_id, top_k)