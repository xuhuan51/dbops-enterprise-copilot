import asyncio
from typing import Dict, Any, List, Set

from app.core.rag_store import rag_store
# 🔥 新增：引入重排序单例
from app.core.reranker import reranker
from app.modules.retrieval.graph.service import graph_service
from app.modules.retrieval.knowledge.retriever import KnowledgeRetriever
from app.core.logger import logger


class RAGOrchestrator:
    def __init__(self):
        self.knowledge_retriever = KnowledgeRetriever()

    async def get_retrieval_context(self, query: str, db_id: str) -> Dict[str, Any]:
        """
        全链路检索入口 (Rerank 增强版)：
        1. 粗排：Schema 向量检索 (Top-15) + Knowledge 检索
        2. 精排：使用 Cross-Encoder 对 Schema 结果进行二次打分，选出 Top-25
        3. 寻路：根据精排后的表，去 Graph 里找连接路径
        4. 组装：返回所有上下文
        """
        logger.info(f"🔍 [Orchestrator] Processing: {query} (DB: {db_id})")

        # =================================================
        # 1. 多路粗召回 (Coarse Recall)
        # =================================================
        # 策略：宁滥勿缺。每路召回 15 个，确保正确答案在候选集里。
        COARSE_TOP_K = 15

        # 启动 Schema 检索 (线程池)
        schema_task = asyncio.to_thread(
            rag_store.search_vectors,
            collection_name="schema",
            query_text=query,
            db_id=db_id,
            top_k=COARSE_TOP_K  # 🔥 放大搜索范围
        )

        # 启动 Knowledge 检索
        knowledge_task = self.knowledge_retriever.search_knowledge(
            knowledge_keywords=[],
            knowledge_query=query,
            db_id=db_id,
            each_top_k=3
        )

        # 并发等待
        schema_hits, rules = await asyncio.gather(schema_task, knowledge_task)

        # =================================================
        # 2. 准备候选集 & 精排 (Rerank)
        # =================================================
        candidates = []
        if schema_hits and len(schema_hits) > 0:
            for hit in schema_hits[0]:
                ent = hit.entity
                # 构造包含丰富语义的文本供模型判断
                # 格式: Table: xxx | Column: xxx | Desc: (doc_text)
                content_str = (
                    f"Table: {ent.get('table_name')} | "
                    f"Column: {ent.get('column_name')} | "
                    f"Description: {ent.get('doc_text')}"
                )

                candidates.append({
                    "content": content_str,
                    "entity": ent,
                    "milvus_score": hit.score
                })

        logger.info(f"⚖️ [Rerank] Re-ranking {len(candidates)} candidates...")

        # 🔥 核心：调用 Reranker 进行精排
        # 最终保留 25 个给 LLM (上下文窗口足够大，多给点没关系)
        FINAL_TOP_K = 25
        ranked_results = reranker.rerank(query, candidates, top_k=FINAL_TOP_K)

        # =================================================
        # 3. 解析精排结果
        # =================================================
        selected_tables: Set[str] = set()
        retrieved_columns = []

        for item in ranked_results:
            ent = item['entity']
            tbl = ent.get("table_name")
            col = ent.get("column_name")

            if tbl:
                selected_tables.add(tbl)
                retrieved_columns.append({
                    "table": tbl,
                    "column": col,
                    "score": item['rerank_score'],  # 使用更准的 Rerank 分数
                    "desc": ent.get('doc_text', "")[:100]  # 截断一下描述，省token
                })

        logger.info(f"✅ [Orchestrator] Reranked tables: {list(selected_tables)}")

        # =================================================
        # 4. 图谱寻路 (Graph Routing)
        # =================================================
        join_paths = []
        # 只要精排后的结果里涉及 2 张及以上的表，就尝试找关系
        if len(selected_tables) >= 2:
            try:
                graph_service.load_graph()
                join_paths = graph_service.search_join_path(db_id, list(selected_tables))
                logger.info(f"🕸️ [Orchestrator] Found paths: {join_paths}")
            except Exception as e:
                logger.error(f"❌ [Orchestrator] Graph error: {e}")
        else:
            logger.info("ℹ️ [Orchestrator] Single table query, skipping graph search.")

        # =================================================
        # 5. 返回结果
        # =================================================
        return {
            "query": query,
            "db_id": db_id,
            "retrieved_tables": list(selected_tables),
            "retrieved_columns": retrieved_columns,  # 这里是 Top-25 精选
            "join_paths": join_paths,
            "business_rules": rules
        }


# 导出单例
orchestrator = RAGOrchestrator()