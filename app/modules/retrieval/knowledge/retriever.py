import asyncio
from typing import List, Dict, Any, Union, Optional
from concurrent.futures import ThreadPoolExecutor

from app.core.rag_store import rag_store
from app.core.logger import logger

import asyncio
from typing import List, Dict, Any, Union, Optional
from concurrent.futures import ThreadPoolExecutor

from app.core.rag_store import rag_store
from app.core.logger import logger


class KnowledgeRetriever:
    """
    业务规则检索器 (Knowledge Retriever) - 修复版
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

        # 1. 构造查询文本
        query_parts = []
        if knowledge_keywords:
            unique_keywords = list(set(k.strip() for k in knowledge_keywords if k.strip()))
            if unique_keywords:
                query_parts.append(" ".join(unique_keywords))

        if knowledge_query and knowledge_query.strip():
            query_parts.append(knowledge_query.strip())

        query_text = " ".join(query_parts)

        if not query_text:
            return []

        try:
            # 2. 执行向量检索
            # 注意：rag_store 可能会返回 [[hit1, hit2]] (二维) 或 [hit1, hit2] (一维)
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

        # 3. 解析结果 (关键修复 🔥)
        rules = []
        seen_content = set()

        # 展平结果列表
        target_hits = []
        if isinstance(hits, list):
            for item in hits:
                if isinstance(item, list):
                    target_hits.extend(item)
                else:
                    target_hits.append(item)

        for hit in target_hits:
            rule_text = ""
            score = 0.0

            # --- 分支 A: 处理已经封装好的字典 (MilvusDAO 默认返回格式) ---
            if isinstance(hit, dict) and ("content" in hit or "rule_text" in hit):
                rule_text = hit.get("content") or hit.get("rule_text")
                score = hit.get("score", 0.0)

            # --- 分支 B: 处理原始 Milvus Hit 对象 (防御性编程) ---
            else:
                entity = getattr(hit, 'entity', None)
                if entity is None and isinstance(hit, dict):
                    entity = hit.get('entity', {})

                # 如果是对象，转 dict
                if hasattr(entity, 'to_dict'):
                    entity = entity.to_dict()

                if isinstance(entity, dict):
                    rule_text = (
                            entity.get("content") or
                            entity.get("rule_text") or
                            entity.get("evidence") or
                            entity.get("doc_text")
                    )

                # 获取分数
                if hasattr(hit, 'score'):
                    score = hit.score
                elif hasattr(hit, 'distance'):
                    score = hit.distance
                elif isinstance(hit, dict):
                    score = hit.get('score', hit.get('distance', 0.0))

            # --- 统一添加逻辑 ---
            if rule_text and rule_text.strip() and rule_text not in seen_content:
                seen_content.add(rule_text)
                rules.append({
                    "content": rule_text.strip(),
                    "score": round(float(score), 4),
                    "source": "knowledge_base",
                    "db_id": db_id
                })

        logger.info(f"📚 [Knowledge] Found {len(rules)} unique rules for db={db_id}")

        rules.sort(key=lambda x: x['score'], reverse=True)
        return rules

    async def retrieve(self, query: str, db_id: str, top_k: int = 3):
        return await self.search_knowledge([], query, db_id, top_k)