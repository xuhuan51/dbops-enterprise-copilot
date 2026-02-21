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
        # 🔧 Stage 6.5: 核心字段自动补全（防止向量检索遗漏关键字段）
        # ═════════════════════════════════════════════════════════════════════
        retrieved_schema = self._enrich_mandatory_columns(retrieved_schema)

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

    # ══════════════════════════════════════════════════════════════════════
    # 🔧 核心字段自动补全
    # ══════════════════════════════════════════════════════════════════════

    # 每张表被检索到时，必须自动补全的字段（主键、外键、状态字段）
    # 这些字段高频出现在 JOIN / WHERE / GROUP BY 中，但向量检索按语义相似度
    # 经常召回不了它们（如用户问"销售额"，检索不到 order_status）
    MANDATORY_COLUMNS: Dict[str, List[Dict[str, str]]] = {
        "orders": [
            {"column_name": "order_id",      "data_type": "bigint",  "ai_description": "订单主键"},
            {"column_name": "order_status",   "data_type": "varchar", "ai_description": "订单状态(pending/paid/shipped/delivered/cancelled/refunded)"},
            {"column_name": "user_id",        "data_type": "bigint",  "ai_description": "下单用户ID，关联users表"},
        ],
        "order_items": [
            {"column_name": "item_id",        "data_type": "bigint",  "ai_description": "订单明细主键"},
            {"column_name": "order_id",       "data_type": "bigint",  "ai_description": "关联orders表的外键"},
            {"column_name": "product_id",     "data_type": "bigint",  "ai_description": "关联products表的外键"},
            {"column_name": "sku_id",         "data_type": "bigint",  "ai_description": "关联product_skus表的外键"},
        ],
        "users": [
            {"column_name": "user_id",        "data_type": "bigint",  "ai_description": "用户主键"},
            {"column_name": "account_status",  "data_type": "varchar", "ai_description": "账号状态(active/inactive/banned)"},
        ],
        "products": [
            {"column_name": "product_id",     "data_type": "bigint",  "ai_description": "商品主键"},
            {"column_name": "brand_id",       "data_type": "bigint",  "ai_description": "关联brands表的外键"},
            {"column_name": "category_id",    "data_type": "bigint",  "ai_description": "关联categories表的外键(二级分类)"},
        ],
        "product_skus": [
            {"column_name": "sku_id",         "data_type": "bigint",  "ai_description": "SKU主键"},
            {"column_name": "product_id",     "data_type": "bigint",  "ai_description": "关联products表的外键"},
        ],
        "product_reviews": [
            {"column_name": "review_id",      "data_type": "bigint",  "ai_description": "评价主键"},
            {"column_name": "product_id",     "data_type": "bigint",  "ai_description": "关联products表的外键"},
            {"column_name": "order_item_id",  "data_type": "bigint",  "ai_description": "关联order_items.item_id的外键"},
            {"column_name": "user_id",        "data_type": "bigint",  "ai_description": "评价用户ID"},
        ],
        "brands": [
            {"column_name": "brand_id",       "data_type": "bigint",  "ai_description": "品牌主键"},
        ],
        "categories": [
            {"column_name": "category_id",    "data_type": "bigint",  "ai_description": "分类主键"},
            {"column_name": "parent_category_id", "data_type": "bigint", "ai_description": "父分类ID，用于自连接查询一级分类"},
            {"column_name": "category_level",  "data_type": "int",    "ai_description": "分类层级(1=一级,2=二级)"},
        ],
        "user_addresses": [
            {"column_name": "user_id",        "data_type": "bigint",  "ai_description": "关联users表的外键"},
        ],
        "product_favorites": [
            {"column_name": "user_id",        "data_type": "bigint",  "ai_description": "收藏用户ID"},
            {"column_name": "product_id",     "data_type": "bigint",  "ai_description": "收藏商品ID"},
        ],
        "user_browse_history": [
            {"column_name": "user_id",        "data_type": "bigint",  "ai_description": "浏览用户ID"},
            {"column_name": "product_id",     "data_type": "bigint",  "ai_description": "浏览商品ID"},
        ],
        "shopping_cart": [
            {"column_name": "user_id",        "data_type": "bigint",  "ai_description": "加购用户ID"},
            {"column_name": "sku_id",         "data_type": "bigint",  "ai_description": "加购SKU ID"},
            {"column_name": "product_id",     "data_type": "bigint",  "ai_description": "加购商品ID"},
        ],
    }

    @classmethod
    def _enrich_mandatory_columns(cls, retrieved_schema: Dict[str, Dict]) -> Dict[str, Dict]:
        """
        对已检索到的表，自动补全主键/外键/状态字段。
        只补全表已存在于 retrieved_schema 中的情况，不会凭空新增表。
        """
        injected_count = 0

        for table_name, mandatory_cols in cls.MANDATORY_COLUMNS.items():
            if table_name not in retrieved_schema:
                continue

            existing_col_names = {
                c.get("column_name")
                for c in retrieved_schema[table_name].get("columns", [])
            }

            for col_def in mandatory_cols:
                if col_def["column_name"] not in existing_col_names:
                    retrieved_schema[table_name]["columns"].append({
                        "column_name": col_def["column_name"],
                        "data_type": col_def.get("data_type", "UNKNOWN"),
                        "is_nullable": "YES",
                        "sample_values": [],
                        "distinct_count": None,
                        "null_count": None,
                        "numeric_stats": None,
                        "ai_description": col_def.get("ai_description", ""),
                        "_auto_injected": True,  # 标记为自动注入，便于调试
                    })
                    injected_count += 1

        if injected_count > 0:
            logger.info(f"🔧 [Enrichment] Auto-injected {injected_count} mandatory columns")

        return retrieved_schema

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