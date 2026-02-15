import asyncio
from typing import Dict, Any, List

from app.core.rag_store import rag_store
from app.core.reranker import reranker
from app.modules.retrieval.graph.service import graph_service
from app.modules.retrieval.knowledge.retriever import KnowledgeRetriever
from app.core.state import AgentState
from app.core.logger import logger
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
        v9.1: Group-Based Retrieval Orchestrator
        支持基于 ExpandNode 的语义分组并发检索
        """
        # --- Context Unpacking ---
        question = state.get("question", "")
        db_id = state.get("db_id", "")

        # 获取新版 Expand 输出
        expand_data = state.get("expand_data")
        hints = getattr(expand_data, "semantic_hints", None)

        logger.info(f"🔍 [Orchestrator] Retrieval for: {question[:50]}...")

        # =========================================================================
        # 🟢 Stage 1: Search Query Preparation (分组查询准备)
        # =========================================================================
        schema_search_tasks = []  # 存储 (group_name, query_string)
        value_kw_strs = []  # 存储用于值扫描的关键词
        all_keywords_backup = []  # 用于 Knowledge 检索的汇总

        if expand_data and expand_data.search_keywords:
            # 1.1 处理 Concepts (按组准备检索词)
            if expand_data.search_keywords.concepts:
                for group in expand_data.search_keywords.concepts:
                    if group.terms:
                        # 策略：每个组生成一个独立的查询串
                        # 例如 group="销量字段", terms=["销量", "sales"] -> query="销量 sales"
                        query_str = " ".join(group.terms)
                        group_name = group.group or "GENERAL"
                        schema_search_tasks.append((group_name, query_str))
                        all_keywords_backup.extend(group.terms)

            # 1.2 处理 Values (提取用于 Stage 5)
            if expand_data.search_keywords.values:
                for group in expand_data.search_keywords.values:
                    if group.terms:
                        value_kw_strs.extend(group.terms)
                        # Value 词有时候也能帮助召回 Schema (比如 "iPhone" 可能命中 tags 字段)
                        # 这里选择性加入 backup，或者也可以为 Value 单独开一个 Schema 检索任务
                        all_keywords_backup.extend(group.terms)

        # 兜底：如果没有 expand 数据，使用分词
        if not schema_search_tasks:
            raw_keywords = [w for w in question.split() if len(w) > 1]
            schema_search_tasks.append(("FALLBACK", " ".join(raw_keywords)))
            all_keywords_backup = raw_keywords

        # =========================================================================
        # 🔵 Stage 2: Concurrent Retrieval (分组并发召回)
        # =========================================================================
        tasks = []
        task_names = []

        # 2.1 Schema 召回 - 核心变更：按组发射任务
        # 既然我们分了组，就可以降低每个组的 top_k，依靠多路召回提升精度
        for group_name, query_str in schema_search_tasks:
            # top_k 可以设小一点，比如 10，因为我们有多个组
            tasks.append(asyncio.to_thread(rag_store.search_vectors, "schema", query_str, db_id, 10))
            task_names.append(f"SCHEMA_GROUP_{group_name}")

        # 2.2 Schema 召回 - Hints 补充 (保持不变)
        if hints:
            if hints.metric_hint:
                tasks.append(asyncio.to_thread(rag_store.search_vectors, "schema", hints.metric_hint, db_id, 10))
                task_names.append("SCHEMA_HINT_METRIC")
            if hints.filter_hints:
                # filter hints 往往包含列名信息
                tasks.append(
                    asyncio.to_thread(rag_store.search_vectors, "schema", " ".join(hints.filter_hints), db_id, 10))
                task_names.append("SCHEMA_HINT_FILTER")

        # 2.3 Knowledge 召回 (使用汇总的关键词)
        knowledge_query = " ".join(all_keywords_backup) or question
        tasks.append(self.knowledge_retriever.search_knowledge([], knowledge_query, db_id, 3))
        task_names.append("KNOWLEDGE")

        # 执行并发
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        # =========================================================================
        # 🟠 Stage 3: Result Processing (结果去重与合并)
        # =========================================================================
        unique_candidates = {}
        knowledge_results = []

        for i, res in enumerate(results_list):
            if not res or isinstance(res, Exception):
                if isinstance(res, Exception):
                    logger.error(f"❌ Task {task_names[i]} failed: {res}")
                continue

            t_name = task_names[i]

            # 处理 Schema 结果
            if t_name.startswith("SCHEMA"):
                all_hits = match_helper.recursive_extract_hits(res)
                for hit in all_hits:
                    ent = getattr(hit, 'entity', hit.get('entity'))
                    if ent and ent.get('table_name') and ent.get('column_name'):
                        key = f"{ent['table_name']}.{ent['column_name']}"

                        # 记录来源用于调试 (可选)
                        # logger.debug(f"Hit: {key} from {t_name}")

                        # 简单的去重逻辑：如果已存在，保留分数高的 (通常 Milvus 返回已排序，这里简单保留第一个即可)
                        # 或者在这里做分数融合 (加权)
                        if key not in unique_candidates:
                            unique_candidates[key] = {
                                "content": f"{ent['table_name']}.{ent['column_name']} {ent.get('column_comment', '')}",
                                "entity": ent,
                                "milvus_score": getattr(hit, 'score', 0),
                                "source_group": t_name  # 记录是哪个组召回的
                            }

            # 处理 Knowledge 结果
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

        # 🔥 瘦身优化：只提取必要字段
        retrieved_columns = []
        for r in ranked:
            ent = r['entity']

            # 只保留核心字段（瘦身版）
            slimmed_item = {
                # 基础标识
                "table_name": ent.get("table_name"),
                "column_name": ent.get("column_name"),

                # 核心元数据
                "data_type": ent.get("data_type"),
                "is_nullable": ent.get("is_nullable"),

                # 统计信息
                "sample_values": ent.get("sample_values", []),
                "distinct_count": ent.get("distinct_count"),
                "null_count": ent.get("null_count"),
                "numeric_stats": ent.get("numeric_stats"),

                # AI 语义描述
                "ai_description": ent.get("ai_description", ""),
            }

            retrieved_columns.append(slimmed_item)

        selected_tables = {c['table'] for c in retrieved_columns}

        # =========================================================================
        # 🔴 Stage 5: Value Scanning (使用 Values 分组)
        # =========================================================================
        final_constraints = []
        if retrieved_columns:
            # 这里的 all_keywords_backup 仅作为辅助，重点是 value_kw_strs
            scan_phrases = match_helper.prepare_scan_phrases(value_kw_strs, all_keywords_backup, hints)
            need_rescue = bool(hints and hints.filter_hints)

            try:
                raw_matches, rescue_cols = await asyncio.to_thread(
                    value_scanner.scan_tiered, db_id, retrieved_columns, scan_phrases, need_rescue
                )

                if rescue_cols:
                    # ... (rescue_cols 瘦身代码保持不变) ...
                    slimmed_rescue = []
                    for col in rescue_cols:
                        slimmed_rescue.append({
                            "table_name": col.get("table_name"),
                            "column_name": col.get("column_name"),
                            "table": col.get("table_name"),
                            "column": col.get("column_name"),
                            "data_type": col.get("data_type"),
                            "is_nullable": col.get("is_nullable"),
                            "sample_values": col.get("sample_values", []),
                            "distinct_count": col.get("distinct_count"),
                            "null_count": col.get("null_count"),
                            "numeric_stats": col.get("numeric_stats"),
                            "ai_description": col.get("ai_description", ""),
                        })
                    retrieved_columns.extend(slimmed_rescue)

                best = match_helper.select_best_matches(raw_matches, question, hints)
                hard = [m.format_constraint() for m in best if m.strength == "hard"][:2]
                soft = [m.format_constraint() for m in best if m.strength != "hard"][:2]

                final_constraints = hard + soft
                logger.info(f"🏆 [ValueLink] Final Constraints: {len(final_constraints)}")
            except Exception as e:
                logger.error(f"❌ Value scan failed: {e}")

        # ... (Stage 6 & 7 保持不变) ...
        # =========================================================================
        # 🟤 Stage 6: Graph Augmentation (图增强)
        # =========================================================================
        retrieved_columns = schema_helper.augment_with_join_keys(db_id, retrieved_columns)
        selected_tables = list({c['table'] for c in retrieved_columns})

        # =========================================================================
        # ⚫ Stage 7: Formatting (格式化)
        # =========================================================================
        retrieved_columns = await schema_helper.inject_sample_values(db_id, retrieved_columns)
        schema_str = schema_helper.format_schema_str(retrieved_columns)

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