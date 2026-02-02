import asyncio
from typing import Dict, Any, List, Set

from app.core.rag_store import rag_store
from app.core.reranker import reranker
from app.modules.retrieval.graph.service import graph_service
from app.modules.retrieval.knowledge.retriever import KnowledgeRetriever
from app.core.state import AgentState
from app.core.logger import logger


class RAGOrchestrator:
    def __init__(self):
        self.knowledge_retriever = KnowledgeRetriever()

    async def get_retrieval_context(self, state: AgentState) -> Dict[str, Any]:
        """
        全链路检索入口 (v3.0 分治策略版 + Milvus 适配修复)：
        """
        # 1. 解包 State
        question = state.get("question", "")
        db_id = state.get("db_id", "")
        intent_data = state.get("intent_data")

        # 获取 Expand 阶段产出的核心素材
        keywords = getattr(intent_data, "search_keywords", [])
        hints = getattr(intent_data, "semantic_hints", None)

        logger.info(f"🔍 [Orchestrator] Start Split-Retrieval for: {question[:50]}...")

        # =================================================
        # 2. 分治多路召回 (Split & Conquer)
        # =================================================
        tasks = []
        task_names = []

        # (A) 全局混合搜
        if keywords:
            full_query = " ".join(keywords)
            tasks.append(asyncio.to_thread(
                rag_store.search_vectors,
                collection_name="schema",
                query_text=full_query,
                db_id=db_id,
                top_k=20
            ))
            task_names.append("Broad_Search")

        # (B) 精准指标搜 (Metric)
        if hints and hints.metric_hint:
            tasks.append(asyncio.to_thread(
                rag_store.search_vectors,
                collection_name="schema",
                query_text=hints.metric_hint,
                db_id=db_id,
                top_k=15
            ))
            task_names.append("Metric_Hint")

        # (C) 精准过滤搜 (Filter)
        if hints and hints.filter_hints:
            filter_query = " ".join(hints.filter_hints)
            tasks.append(asyncio.to_thread(
                rag_store.search_vectors,
                collection_name="schema",
                query_text=filter_query,
                db_id=db_id,
                top_k=15
            ))
            task_names.append("Filter_Hint")

        # (D) 主体搜 (Target)
        if hints and hints.target_hint:
            tasks.append(asyncio.to_thread(
                rag_store.search_vectors,
                collection_name="schema",
                query_text=hints.target_hint,
                db_id=db_id,
                top_k=10
            ))
            task_names.append("Target_Hint")

        # (E) 知识库检索
        knowledge_query = " ".join(keywords) if keywords else question
        tasks.append(self.knowledge_retriever.search_knowledge(
            knowledge_keywords=[],
            knowledge_query=knowledge_query,
            db_id=db_id,
            each_top_k=3
        ))
        task_names.append("Knowledge_Base")

        # 🔥 并发执行
        logger.info(f"🚀 [Orchestrator] Launching {len(tasks)} parallel tasks: {task_names}")
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        # =================================================
        # 3. 结果合并与去重 (Merge & Deduplicate)
        # =================================================
        unique_candidates = {}
        knowledge_results = []

        for i, res in enumerate(results_list):
            name = task_names[i]

            # 异常处理
            if isinstance(res, Exception):
                logger.error(f"❌ Task [{name}] failed: {res}")
                continue

            # 处理知识库 (Knowledge_Base)
            if name == "Knowledge_Base":
                if res:
                    knowledge_results = res[1] if len(res) > 1 else []
                continue

            # 处理 Milvus 结果 (Schema)
            if not res:
                continue

            # 🔥🔥🔥【核心修复点】🔥🔥🔥
            # Milvus search 返回的是一个二维列表 (Results -> Hits)。
            # 因为我们每次只搜一个 query，所以取 res[0] 才是真正的 Hits 列表。
            try:
                hits = res[0]
            except IndexError:
                continue

            for hit in hits:
                # 获取 Entity (根据 rag_store.py 的 FieldSchema)
                # 使用 .get() 更加安全
                try:
                    ent = hit.entity
                    tbl = ent.get('table_name')
                    col = ent.get('column_name')
                    doc = ent.get('doc_text', '')
                    score = hit.score  # Milvus Hit 对象自带 score
                except Exception as e:
                    logger.warning(f"⚠️ Failed to parse hit in {name}: {e}")
                    continue

                if not tbl or not col:
                    continue

                unique_key = f"{tbl}.{col}"

                if unique_key not in unique_candidates:
                    content_str = (
                        f"Table: {tbl} | "
                        f"Column: {col} | "
                        f"Description: {doc}"
                    )

                    unique_candidates[unique_key] = {
                        "content": content_str,
                        "entity": ent,
                        "milvus_score": score,
                        "source": name
                    }

        logger.info(f"📦 [Orchestrator] Merged candidates: {len(unique_candidates)} unique columns.")

        # =================================================
        # 4. 全局精排 (Rerank)
        # =================================================
        candidates_list = list(unique_candidates.values())

        FINAL_TOP_K = 30

        # 如果没有候选集，跳过 Rerank
        if not candidates_list:
            logger.warning("⚠️ No candidates found from vector search.")
            ranked_results = []
        else:
            logger.info(f"⚖️ [Rerank] Reranking {len(candidates_list)} cols...")
            try:
                ranked_results = reranker.rerank(query=question, candidates=candidates_list, top_k=FINAL_TOP_K)
            except Exception as e:
                logger.error(f"❌ Rerank failed: {e}")
                # 降级：如果 Rerank 挂了，按 Milvus 分数排序返回前 30 个
                candidates_list.sort(key=lambda x: x['milvus_score'], reverse=True)
                ranked_results = []
                for item in candidates_list[:FINAL_TOP_K]:
                    ranked_results.append({
                        "entity": item['entity'],
                        "rerank_score": item['milvus_score'],
                        "source": item['source']
                    })

        # =================================================
        # 5. 解析 & 图谱寻路
        # =================================================
        selected_tables: Set[str] = set()
        retrieved_columns = []

        for item in ranked_results:
            ent = item.get('entity', {})
            tbl = ent.get("table_name")
            col = ent.get("column_name")

            if tbl:
                selected_tables.add(tbl)
                retrieved_columns.append({
                    "table": tbl,
                    "column": col,
                    "score": item.get('rerank_score', 0),
                    "desc": ent.get('doc_text', "")[:100],
                    "source": item.get("source", "unknown")
                })

        logger.info(f"✅ [Orchestrator] Final Tables: {list(selected_tables)}")

        # --- Graph Routing ---
        join_paths = []
        if len(selected_tables) >= 2:
            try:
                graph_service.load_graph()
                join_paths = graph_service.search_join_path(db_id, list(selected_tables))
                logger.info(f"🕸️ [Graph] Found join paths: {len(join_paths)}")
            except Exception as e:
                logger.warning(f"⚠️ Graph search skipped: {e}")
        else:
            logger.info("ℹ️ [Graph] Single table query, skipping path search.")

        # =================================================
        # 6. 返回结果
        # =================================================
        return {
            "query": question,
            "db_id": db_id,
            "retrieved_tables": list(selected_tables),
            "retrieved_columns": retrieved_columns,
            "join_paths": join_paths,
            "business_rules": knowledge_results
        }


# 导出单例
orchestrator = RAGOrchestrator()