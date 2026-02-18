"""
═══════════════════════════════════════════════════════════════════════════════
📦 模块名称: orchestrator.py (v11 - 精简版)
📝 改动说明: 删除值匹配逻辑，值匹配移到 column_selector_node 之后由 value_linker 负责
═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
from typing import Dict, Any, List

from app.core.rag_store import rag_store
from app.modules.retrieval.knowledge.retriever import KnowledgeRetriever
from app.core.state import AgentState
from app.core.logger import logger



class RAGOrchestrator:
    """RAG 编排器 - 只负责 Schema 检索 + Knowledge 检索"""

    def __init__(self):
        self.knowledge_retriever = KnowledgeRetriever()

    async def get_retrieval_context(self, state: AgentState) -> Dict[str, Any]:
        """
        v11: 纯检索版 - 不再做值匹配
        值匹配已移到 column_selector_node 之后，由 value_linker 负责
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
        # 🟢 Stage 1: Query Preparation
        # ═════════════════════════════════════════════════════════════════════
        schema_search_tasks = []
        all_keywords = []

        # 🔥 改动: 同时收集 value_scan_phrases 传给下游（但不在这里匹配）
        value_scan_phrases = []

        if expand_data and hasattr(expand_data, 'search_keywords'):
            search_kw = expand_data.search_keywords

            if hasattr(search_kw, 'concepts') and search_kw.concepts:
                for group in search_kw.concepts:
                    if group.terms:
                        query_str = " ".join(group.terms)
                        group_name = group.group or "GENERAL"
                        schema_search_tasks.append((group_name, query_str))
                        all_keywords.extend(group.terms)

            if hasattr(search_kw, 'values') and search_kw.values:
                for group in search_kw.values:
                    if group.terms:
                        value_scan_phrases.extend(group.terms)
                        all_keywords.extend(group.terms)

        if not schema_search_tasks:
            raw_keywords = [w for w in question.split() if len(w) > 1]
            schema_search_tasks.append(("FALLBACK", " ".join(raw_keywords)))
            all_keywords = raw_keywords

        # ═════════════════════════════════════════════════════════════════════
        # 🔵 Stage 2: Concurrent Retrieval
        # ═════════════════════════════════════════════════════════════════════
        tasks = []
        task_names = []

        for group_name, query_str in schema_search_tasks:
            tasks.append(
                asyncio.to_thread(rag_store.search_vectors, "schema", query_str, db_id, 10)
            )
            task_names.append(f"SCHEMA_GROUP_{group_name}")

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

        knowledge_query = " ".join(all_keywords) or question
        tasks.append(
            self.knowledge_retriever.search_knowledge([], knowledge_query, db_id, 3)
        )
        task_names.append("KNOWLEDGE")

        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        # ═════════════════════════════════════════════════════════════════════
        # 🟠 Stage 3: Result Processing
        # ═════════════════════════════════════════════════════════════════════
        unique_candidates = {}
        knowledge_results = []

        for i, res in enumerate(results_list):
            if not res or isinstance(res, Exception):
                if isinstance(res, Exception):
                    logger.error(f"❌ Task {task_names[i]} failed: {res}")
                continue

            t_name = task_names[i]

            if t_name.startswith("SCHEMA"):
                if isinstance(res, list):
                    for col_info in res:
                        table_name = col_info.get("table_name")
                        column_name = col_info.get("column_name")
                        if table_name and column_name:
                            key = f"{table_name}.{column_name}"
                            if key not in unique_candidates:
                                unique_candidates[key] = col_info
                            elif col_info.get("score", 0) > unique_candidates[key].get("score", 0):
                                unique_candidates[key] = col_info

            elif t_name == "KNOWLEDGE":
                knowledge_results = res if isinstance(res, list) else []

        # ═════════════════════════════════════════════════════════════════════
        # 🟣 Stage 4: Sort (不限制数量)
        # ═════════════════════════════════════════════════════════════════════
        candidates = list(unique_candidates.values())
        candidates.sort(key=lambda x: x.get('score', 0), reverse=True)
        retrieved_columns = candidates

        table_count = len(set(c.get('table_name') for c in retrieved_columns))
        logger.info(
            f"📊 [Retrieval] Retrieved {len(retrieved_columns)} columns "
            f"from {table_count} tables"
        )

        # ═════════════════════════════════════════════════════════════════════
        # 🔴 Stage 5: 值匹配 —— 已删除！
        # 值匹配现在由 column_selector_node 之后的 value_linker 负责
        # ═════════════════════════════════════════════════════════════════════

        # ═════════════════════════════════════════════════════════════════════
        # 🟤 Stage 6: Schema Formatting
        # ═════════════════════════════════════════════════════════════════════
        retrieved_schema = self._group_columns_by_table(retrieved_columns)

        # ═════════════════════════════════════════════════════════════════════
        # ⚫ Stage 7: Business Rules
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
            "value_mappings": [],          # 🔥 不再在这里填充，留给下游
            "value_scan_phrases": value_scan_phrases,  # 🔥 新增: 传给下游做匹配
            "business_rules": business_rules,
            "join_paths": [],
        }

    @staticmethod
    def _group_columns_by_table(columns: List[Dict]) -> Dict[str, Dict]:
        """按表分组列信息（不变）"""
        grouped = {}

        for col in columns:
            table = col.get("table_name")
            if not table:
                continue

            if table not in grouped:
                grouped[table] = {"columns": []}

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