import asyncio
from typing import Dict, Any, List

from app.core.rag_store import rag_store
from app.core.reranker import reranker
from app.modules.retrieval.graph.service import graph_service
from app.modules.retrieval.knowledge.retriever import KnowledgeRetriever
from app.core.state import AgentState
from app.core.logger import logger

# 🔥 引入拆分后的模块 (Helper Modules)
from app.modules.retrieval.value_scanner import value_scanner
from app.modules.retrieval.schema_helper import schema_helper
from app.modules.retrieval.match_helper import match_helper


class RAGOrchestrator:
    """
    RAG 编排器 (Orchestrator)
    负责协调整个检索流程：从理解用户意图、召回相关 Schema、重排序、到最终的值扫描和上下文组装。
    """

    def __init__(self):
        self.knowledge_retriever = KnowledgeRetriever()

    async def get_retrieval_context(self, state: AgentState) -> Dict[str, Any]:
        """
        v9.0: Modularized Orchestrator (MySQL Native)
        执行完整的 RAG 检索流程
        """
        # --- Context Unpacking (解包上下文) ---
        question = state.get("question", "")
        db_id = state.get("db_id", "")  # 这里通常是 "ecommerce"
        intent_data = state.get("intent_data")
        hints = getattr(intent_data, "semantic_hints", None)

        # =========================================================================
        # 🟢 Stage 1: Keyword Extraction (关键词提取)
        # =========================================================================
        raw_keywords = getattr(intent_data, "search_keywords", [])
        all_kw_strs, value_kw_strs = [], []

        if raw_keywords:
            for item in raw_keywords:
                k_str = item.strip() if isinstance(item, str) else getattr(item, "keyword", "").strip()
                k_type = getattr(item, "type", "CONCEPT") if hasattr(item, "type") else "CONCEPT"
                if k_str:
                    all_kw_strs.append(k_str)
                    if str(k_type).upper() == "VALUE":
                        value_kw_strs.append(k_str)

        if not all_kw_strs:
            all_kw_strs = [w for w in question.split() if len(w) > 3]

        logger.info(f"🔍 [Orchestrator] Retrieval for: {question[:50]}...")

        # =========================================================================
        # 🔵 Stage 2: Concurrent Retrieval (并发召回)
        # =========================================================================
        tasks, task_names = [], []

        # 2.1 Schema 召回 (Keywords + Hints)
        if all_kw_strs:
            tasks.append(asyncio.to_thread(rag_store.search_vectors, "schema", " ".join(all_kw_strs), db_id, 20))
            task_names.append("SCHEMA_KEYWORDS")

        if hints:
            if hints.metric_hint:
                tasks.append(asyncio.to_thread(rag_store.search_vectors, "schema", hints.metric_hint, db_id, 15))
                task_names.append("SCHEMA_METRIC")
            if hints.filter_hints:
                tasks.append(
                    asyncio.to_thread(rag_store.search_vectors, "schema", " ".join(hints.filter_hints), db_id, 15))
                task_names.append("SCHEMA_FILTER")

        # 2.2 Knowledge 召回
        tasks.append(self.knowledge_retriever.search_knowledge([], " ".join(all_kw_strs) or question, db_id, 3))
        task_names.append("KNOWLEDGE")

        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        # =========================================================================
        # 🟠 Stage 3: Result Processing (结果去重)
        # =========================================================================
        unique_candidates = {}
        knowledge_results = []

        for i, res in enumerate(results_list):
            if not res or isinstance(res, Exception):
                if isinstance(res, Exception):
                    logger.error(f"❌ Task {task_names[i]} failed: {res}")
                continue

            t_name = task_names[i]

            if t_name.startswith("SCHEMA"):
                all_hits = match_helper.recursive_extract_hits(res)
                for hit in all_hits:
                    ent = getattr(hit, 'entity', hit.get('entity'))
                    if ent and ent.get('table_name') and ent.get('column_name'):
                        key = f"{ent['table_name']}.{ent['column_name']}"
                        if key not in unique_candidates:
                            unique_candidates[key] = {
                                "content": f"{ent['table_name']}.{ent['column_name']} {ent.get('column_comment', '')}",
                                "entity": ent,
                                "milvus_score": getattr(hit, 'score', 0)
                            }
            elif t_name == "KNOWLEDGE":
                knowledge_results = res

        # =========================================================================
        # 🟣 Stage 4: Re-ranking (重排序)
        # =========================================================================
        candidates = list(unique_candidates.values())
        if candidates:
            try:
                ranked = reranker.rerank(question, candidates, top_k=20)
            except Exception as e:
                logger.warning(f"⚠️ Rerank failed: {e}")
                ranked = [{"entity": c['entity']} for c in
                          sorted(candidates, key=lambda x: x['milvus_score'], reverse=True)[:20]]
        else:
            ranked = []

        retrieved_columns = [{"table": r['entity']['table_name'], "column": r['entity']['column_name'],
                              "sample_values": r['entity'].get("samples", [])} for r in ranked]

        selected_tables = {c['table'] for c in retrieved_columns}

        # =========================================================================
        # 🔴 Stage 5: Value Scanning (值扫描)
        # =========================================================================
        final_constraints = []
        if retrieved_columns:
            scan_phrases = match_helper.prepare_scan_phrases(value_kw_strs, all_kw_strs, hints)
            need_rescue = bool(hints and hints.filter_hints)

            try:
                # 🔥 修改点：这里传入 db_id 而不是 db_path
                # 注意：你需要确保 value_scanner.scan_tiered 能够接受 db_id 并使用 settings 连接数据库
                raw_matches, rescue_cols = await asyncio.to_thread(
                    value_scanner.scan_tiered, db_id, retrieved_columns, scan_phrases, need_rescue
                )

                if rescue_cols:
                    retrieved_columns.extend(rescue_cols)

                best = match_helper.select_best_matches(raw_matches, question, hints)
                hard = [m.format_constraint() for m in best if m.strength == "hard"][:2]
                soft = [m.format_constraint() for m in best if m.strength != "hard"][:2]

                final_constraints = hard + soft
                logger.info(f"🏆 [ValueLink] Final Constraints: {len(final_constraints)}")
            except Exception as e:
                logger.error(f"❌ Value scan failed: {e}")

        # =========================================================================
        # 🟤 Stage 6: Graph Augmentation (图增强)
        # =========================================================================
        retrieved_columns = schema_helper.augment_with_join_keys(db_id, retrieved_columns)
        selected_tables = list({c['table'] for c in retrieved_columns})

        # =========================================================================
        # ⚫ Stage 7: Formatting (格式化)
        # =========================================================================
        # 🔥 修改点：传入 db_id，SchemaHelper 需适配 MySQL 查询
        retrieved_columns = await schema_helper.inject_sample_values(db_id, retrieved_columns)
        schema_str = schema_helper.format_schema_str(retrieved_columns)

        # [修改点] 移除了 BIRD 特有的 Global Rules (CDSCode 等)
        # global_rules = [{"content": "order_status=1 means Pending Payment", "score": 1.0}]
        # if isinstance(knowledge_results, list) and global_rules:
        #    for r in global_rules: knowledge_results.insert(0, r)

        join_paths = []
        if len(selected_tables) >= 2:
            try:
                graph_service.load_graph()
                join_paths = graph_service.search_join_path(db_id, selected_tables)
            except:
                pass

        return {
            "query": question,
            "db_id": db_id,
            "retrieved_tables": selected_tables,
            "retrieved_columns": retrieved_columns,
            "join_paths": join_paths,
            "business_rules": knowledge_results,
            "schema_str": schema_str,
            "value_matches": final_constraints
        }


orchestrator = RAGOrchestrator()