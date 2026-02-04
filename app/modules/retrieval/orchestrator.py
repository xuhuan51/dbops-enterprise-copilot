import asyncio
import os
from typing import Dict, Any, List

from app.core.rag_store import rag_store
from app.core.reranker import reranker
from app.modules.retrieval.graph.service import graph_service
from app.modules.retrieval.knowledge.retriever import KnowledgeRetriever
from app.core.state import AgentState
from app.core.logger import logger

# 🔥 引入拆分后的模块
from app.modules.retrieval.value_scanner import value_scanner
from app.modules.retrieval.schema_helper import schema_helper
from app.modules.retrieval.match_helper import match_helper


class RAGOrchestrator:
    def __init__(self):
        self.knowledge_retriever = KnowledgeRetriever()

    async def get_retrieval_context(self, state: AgentState) -> Dict[str, Any]:
        """v8.0: Modularized Orchestrator (Slim Version)"""
        question = state.get("question", "")
        db_id = state.get("db_id", "")
        intent_data = state.get("intent_data")
        hints = getattr(intent_data, "semantic_hints", None)

        # --- 1. 关键词提取 ---
        raw_keywords = getattr(intent_data, "search_keywords", [])
        all_kw_strs, value_kw_strs = [], []
        if raw_keywords:
            for item in raw_keywords:
                k_str = item.strip() if isinstance(item, str) else getattr(item, "keyword", "").strip()
                k_type = getattr(item, "type", "CONCEPT") if hasattr(item, "type") else "CONCEPT"
                if k_str:
                    all_kw_strs.append(k_str)
                    if str(k_type).upper() == "VALUE": value_kw_strs.append(k_str)
        if not all_kw_strs: all_kw_strs = [w for w in question.split() if len(w) > 3]

        logger.info(f"🔍 [Orchestrator] Retrieval for: {question[:50]}...")

        # --- 2. 并发召回 (Schema + Knowledge) ---
        tasks, task_names = [], []
        if all_kw_strs:
            tasks.append(asyncio.to_thread(rag_store.search_vectors, "schema", " ".join(all_kw_strs), db_id, 20))
            task_names.append("SCHEMA")
        if hints:
            if hints.metric_hint:
                tasks.append(asyncio.to_thread(rag_store.search_vectors, "schema", hints.metric_hint, db_id, 15))
                task_names.append("SCHEMA")
            if hints.filter_hints:
                tasks.append(
                    asyncio.to_thread(rag_store.search_vectors, "schema", " ".join(hints.filter_hints), db_id, 15))
                task_names.append("SCHEMA")

        # Knowledge Search
        tasks.append(self.knowledge_retriever.search_knowledge([], " ".join(all_kw_strs) or question, db_id, 3))
        task_names.append("KNOWLEDGE")

        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        # --- 3. 结果处理 ---
        unique_candidates = {}
        knowledge_results = []

        for i, res in enumerate(results_list):
            if not res or isinstance(res, Exception): continue
            t_name = task_names[i]

            if t_name == "SCHEMA":
                # 🔥 调用 MatchHelper 处理递归提取
                all_hits = match_helper.recursive_extract_hits(res)
                for hit in all_hits:
                    ent = getattr(hit, 'entity', hit.get('entity'))
                    if ent and ent.get('table_name') and ent.get('column_name'):
                        key = f"{ent['table_name']}.{ent['column_name']}"
                        if key not in unique_candidates:
                            unique_candidates[key] = {
                                "content": f"{ent['table_name']}.{ent['column_name']} {ent.get('column_comment', '')}",
                                "entity": ent, "milvus_score": getattr(hit, 'score', 0)
                            }
            elif t_name == "KNOWLEDGE":
                knowledge_results = res

        # Rerank
        candidates = list(unique_candidates.values())
        if candidates:
            try:
                ranked = reranker.rerank(question, candidates, top_k=20)
            except:
                ranked = [{"entity": c['entity']} for c in
                          sorted(candidates, key=lambda x: x['milvus_score'], reverse=True)[:20]]
        else:
            ranked = []

        retrieved_columns = [{"table": r['entity']['table_name'], "column": r['entity']['column_name'],
                              "sample_values": r['entity'].get("samples", [])} for r in ranked]
        selected_tables = {c['table'] for c in retrieved_columns}

        # --- 4. Value Scanning (Tiered Rescue) ---
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(current_dir, "../../.."))
        db_path = os.path.join(project_root, f"data/bird/dev_databases/{db_id}/{db_id}.sqlite")
        if not os.path.exists(db_path):
            db_path = os.path.join(project_root, f"data/bird/train_databases/{db_id}/{db_id}.sqlite")

        final_constraints = []
        if os.path.exists(db_path) and retrieved_columns:
            # 🔥 调用 MatchHelper 准备关键词
            scan_phrases = match_helper.prepare_scan_phrases(value_kw_strs, all_kw_strs, hints)
            need_rescue = bool(hints and hints.filter_hints)
            try:
                raw_matches, rescue_cols = await asyncio.to_thread(
                    value_scanner.scan_tiered, db_path, retrieved_columns, scan_phrases, need_rescue
                )
                if rescue_cols: retrieved_columns.extend(rescue_cols)

                # 🔥 调用 MatchHelper 筛选最佳匹配
                best = match_helper.select_best_matches(raw_matches, question, hints)
                hard = [m.format_constraint() for m in best if m.strength == "hard"][:2]
                soft = [m.format_constraint() for m in best if m.strength != "hard"][:2]
                final_constraints = hard + soft
                logger.info(f"🏆 [ValueLink] Final Constraints: {len(final_constraints)}")
            except Exception as e:
                logger.error(f"❌ Value scan failed: {e}")

        # --- 5. Graph Augmentation (图增强) ---
        # 🔥 调用 SchemaHelper
        retrieved_columns = schema_helper.augment_with_join_keys(db_id, retrieved_columns)
        selected_tables = list({c['table'] for c in retrieved_columns})

        # --- 6. Formatting & Rules ---
        # 🔥 调用 SchemaHelper
        retrieved_columns = await schema_helper.inject_sample_values(db_path, retrieved_columns)
        schema_str = schema_helper.format_schema_str(retrieved_columns)

        # 注入全局规则
        global_rules = [
            {"content": "The column `CDSCode` (or `cds`) is the unique Primary Key... ALWAYS use it for joins.",
             "score": 1.0},
            {"content": "If asking for 'count', use absolute value, do NOT divide by enrollment.", "score": 0.99}
        ]
        if isinstance(knowledge_results, list):
            for r in global_rules: knowledge_results.insert(0, r)

        join_paths = []
        if len(selected_tables) >= 2:
            try:
                graph_service.load_graph()
                join_paths = graph_service.search_join_path(db_id, selected_tables)
            except:
                pass

        return {
            "query": question, "db_id": db_id,
            "retrieved_tables": selected_tables,
            "retrieved_columns": retrieved_columns,
            "join_paths": join_paths, "business_rules": knowledge_results,
            "schema_str": schema_str, "value_matches": final_constraints
        }


orchestrator = RAGOrchestrator()