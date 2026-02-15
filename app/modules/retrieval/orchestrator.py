import asyncio
from typing import Dict, Any, List

from app.core.rag_store import rag_store
from app.modules.retrieval.knowledge.retriever import KnowledgeRetriever
from app.core.state import AgentState
from app.core.logger import logger
from app.modules.retrieval.match_helper import match_helper, MatchCandidate


class RAGOrchestrator:
    """RAG 编排器 - 协调检索流程"""

    def __init__(self):
        self.knowledge_retriever = KnowledgeRetriever()

    async def get_retrieval_context(self, state: AgentState) -> Dict[str, Any]:
        """
        v10: 简化版 Retrieval Orchestrator
        - 直接使用 rag_store 返回的干净数据
        - 去掉重排序（交给选列节点）
        - 按表分组返回
        """
        # ─────────────────────────────────────────────────────────────────────
        # Context Unpacking
        # ─────────────────────────────────────────────────────────────────────
        question = state.get("question", "")
        db_id = state.get("db_id", "")

        expand_data = state.get("expand_data")
        hints = getattr(expand_data, "semantic_hints", None) if expand_data else None

        logger.info(f"🔍 [Orchestrator] Retrieval for: {question[:50]}...")

        # ═════════════════════════════════════════════════════════════════════
        # 🟢 Stage 1: Query Preparation (从 expand_data 提取关键词)
        # ═════════════════════════════════════════════════════════════════════
        schema_search_tasks = []  # (group_name, query_string)
        value_scan_phrases = []  # 用于值匹配
        all_keywords = []  # 用于 knowledge 检索

        if expand_data and hasattr(expand_data, 'search_keywords'):
            search_kw = expand_data.search_keywords

            # 1.1 处理 Concepts (用于 Schema 检索)
            if hasattr(search_kw, 'concepts') and search_kw.concepts:
                for group in search_kw.concepts:
                    if group.terms:
                        query_str = " ".join(group.terms)
                        group_name = group.group or "GENERAL"
                        schema_search_tasks.append((group_name, query_str))
                        all_keywords.extend(group.terms)

            # 1.2 处理 Values (用于值匹配)
            if hasattr(search_kw, 'values') and search_kw.values:
                for group in search_kw.values:
                    if group.terms:
                        value_scan_phrases.extend(group.terms)
                        all_keywords.extend(group.terms)

        # 兜底：如果没有 expand 数据
        if not schema_search_tasks:
            raw_keywords = [w for w in question.split() if len(w) > 1]
            schema_search_tasks.append(("FALLBACK", " ".join(raw_keywords)))
            all_keywords = raw_keywords

        # ═════════════════════════════════════════════════════════════════════
        # 🔵 Stage 2: Concurrent Retrieval (分组并发召回)
        # ═════════════════════════════════════════════════════════════════════
        tasks = []
        task_names = []

        # 2.1 Schema 召回 - 按组发射任务
        for group_name, query_str in schema_search_tasks:
            tasks.append(
                asyncio.to_thread(rag_store.search_vectors, "schema", query_str, db_id, 10)
            )
            task_names.append(f"SCHEMA_GROUP_{group_name}")

        # 2.2 Schema 召回 - Hints 补充
        if hints:
            if hints.metric_hint:
                tasks.append(
                    asyncio.to_thread(rag_store.search_vectors, "schema", hints.metric_hint, db_id, 10)
                )
                task_names.append("SCHEMA_HINT_METRIC")

            if hints.filter_hints:
                tasks.append(
                    asyncio.to_thread(rag_store.search_vectors, "schema", " ".join(hints.filter_hints), db_id, 10)
                )
                task_names.append("SCHEMA_HINT_FILTER")

        # 2.3 Knowledge 召回
        knowledge_query = " ".join(all_keywords) or question
        tasks.append(
            self.knowledge_retriever.search_knowledge([], knowledge_query, db_id, 3)
        )
        task_names.append("KNOWLEDGE")

        # 执行并发
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        # ═════════════════════════════════════════════════════════════════════
        # 🟠 Stage 3: Result Processing (简化！因为已经是干净的列表了)
        # ═════════════════════════════════════════════════════════════════════
        unique_candidates = {}  # key: table.column, value: col_info
        knowledge_results = []

        for i, res in enumerate(results_list):
            if not res or isinstance(res, Exception):
                if isinstance(res, Exception):
                    logger.error(f"❌ Task {task_names[i]} failed: {res}")
                continue

            t_name = task_names[i]

            # 处理 Schema 结果（现在超级简单！）
            if t_name.startswith("SCHEMA"):
                if isinstance(res, list):
                    for col_info in res:  # ← 直接遍历，已经是干净的字典了
                        table_name = col_info.get("table_name")
                        column_name = col_info.get("column_name")

                        if table_name and column_name:
                            key = f"{table_name}.{column_name}"

                            # 去重：保留分数高的
                            if key not in unique_candidates:
                                unique_candidates[key] = col_info
                            elif col_info.get("score", 0) > unique_candidates[key].get("score", 0):
                                unique_candidates[key] = col_info

            # 处理 Knowledge 结果
            elif t_name == "KNOWLEDGE":
                knowledge_results = res if isinstance(res, list) else []

        # ═════════════════════════════════════════════════════════════════════
        # 🟣 Stage 4: 排序（不限制数量，让选列节点决定）
        # ═════════════════════════════════════════════════════════════════════
        candidates = list(unique_candidates.values())

        # 按召回分数排序
        candidates.sort(key=lambda x: x.get('score', 0), reverse=True)

        # 🔥 不再限制 Top N，把所有召回的列都给选列节点
        retrieved_columns = candidates

        # 统计信息
        table_count = len(set(c.get('table_name') for c in retrieved_columns))
        logger.info(
            f"📊 [Retrieval] Retrieved {len(retrieved_columns)} columns "
            f"from {table_count} tables"
        )

        # ═════════════════════════════════════════════════════════════════════
        # 🔴 Stage 5: Value Matching (值匹配)
        # ═════════════════════════════════════════════════════════════════════
        value_mappings = []

        if retrieved_columns and value_scan_phrases:
            try:
                raw_matches = []

                # 遍历每个扫描词
                for phrase in value_scan_phrases:
                    # 遍历每个召回的列
                    for col in retrieved_columns:
                        sample_values = col.get("sample_values", [])

                        # 遍历样本值，计算匹配分数
                        for sample_val in sample_values:
                            sample_str = str(sample_val)
                            score, reason = match_helper.calculate_match_score(phrase, sample_str)

                            # 只保留及格的匹配
                            if score >= 70:
                                match = MatchCandidate(
                                    keyword=phrase,
                                    db_val=sample_str,
                                    table=col.get("table_name"),
                                    column=col.get("column_name"),
                                    score=score,
                                    strength="hard" if score >= 90 else "soft",
                                    reason=reason
                                )
                                raw_matches.append(match)

                # 筛选最佳匹配
                if raw_matches:
                    best_matches = match_helper.select_best_matches(raw_matches, min_score=75)
                    value_mappings = [m.to_simple_dict() for m in best_matches[:5]]
                    logger.info(f"🏆 [ValueMatch] Found {len(value_mappings)} mappings")
                else:
                    logger.warning("⚠️ [ValueMatch] No matches found (check sample_values)")

            except Exception as e:
                logger.error(f"❌ Value matching failed: {e}")

        # ═════════════════════════════════════════════════════════════════════
        # 🟤 Stage 6: Schema Formatting (按表分组)
        # ═════════════════════════════════════════════════════════════════════
        retrieved_schema = self._group_columns_by_table(retrieved_columns)

        # ═════════════════════════════════════════════════════════════════════
        # ⚫ Stage 7: Business Rules Formatting
        # ═════════════════════════════════════════════════════════════════════
        business_rules = []
        if isinstance(knowledge_results, list):
            for r in knowledge_results:
                if isinstance(r, dict):
                    business_rules.append({
                        "content": r.get("content", ""),
                        "score": r.get("score", 0.0)
                    })

        logger.info(f"📚 [Knowledge] Formatted {len(business_rules)} business rules")

        # ═════════════════════════════════════════════════════════════════════
        # 🎯 Final Return
        # ═════════════════════════════════════════════════════════════════════
        return {
            "retrieved_schema": retrieved_schema,
            "value_mappings": value_mappings,
            "business_rules": business_rules,
            "join_paths": []  # 等选列节点填充
        }

    @staticmethod
    def _group_columns_by_table(columns: List[Dict]) -> Dict[str, Dict]:
        """
        按表分组列信息

        输入:
        [
            {"table_name": "orders", "column_name": "id", ...},
            {"table_name": "orders", "column_name": "status", ...},
            {"table_name": "users", "column_name": "name", ...}
        ]

        输出:
        {
            "orders": {
                "columns": [
                    {"column_name": "id", ...},
                    {"column_name": "status", ...}
                ]
            },
            "users": {
                "columns": [
                    {"column_name": "name", ...}
                ]
            }
        }
        """
        grouped = {}

        for col in columns:
            table = col.get("table_name")
            if not table:
                continue

            if table not in grouped:
                grouped[table] = {"columns": []}

            # 去掉 table_name，只保留列级字段
            slim_col = {
                "column_name": col.get("column_name"),
                "data_type": col.get("data_type"),
                "is_nullable": col.get("is_nullable"),
                "sample_values": col.get("sample_values", []),
                "distinct_count": col.get("distinct_count"),
                "null_count": col.get("null_count"),
                "numeric_stats": col.get("numeric_stats"),
                "ai_description": col.get("ai_description", ""),
            }
            grouped[table]["columns"].append(slim_col)

        return grouped


# 全局实例
orchestrator = RAGOrchestrator()