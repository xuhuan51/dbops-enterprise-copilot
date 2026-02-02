import asyncio
import os
from typing import Dict, Any, List, Set
from sqlalchemy import create_engine, text

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
        全链路检索入口 (v3.4 最终优化版：智能值分离)
        """
        question = state.get("question", "")
        db_id = state.get("db_id", "")
        intent_data = state.get("intent_data")

        # 1. 基础关键词提取
        keywords = getattr(intent_data, "search_keywords", [])
        if not keywords:
            keywords = [w for w in question.split() if len(w) > 3]

        hints = getattr(intent_data, "semantic_hints", None)

        logger.info(f"🔍 [Orchestrator] Start Retrieval for: {question[:50]}...")

        # ------------------------------------------------------
        # 2. 多路召回 (Schema Linking) - 保持不变
        # ------------------------------------------------------
        tasks = []
        task_names = []

        # (A) Broad Search (全局混合搜)
        if keywords:
            full_query = " ".join(keywords)
            tasks.append(asyncio.to_thread(
                rag_store.search_vectors,
                collection_name="schema",
                query_text=full_query,
                db_id=db_id,  # ✅ 显式指定参数，防止错位
                top_k=20  # ✅ 显式指定参数
            ))
            task_names.append("Broad_Search")

        # (B) Metric Hint (精准指标搜)
        if hints and hints.metric_hint:
            tasks.append(asyncio.to_thread(
                rag_store.search_vectors,
                collection_name="schema",
                query_text=hints.metric_hint,
                db_id=db_id,
                top_k=15
            ))
            task_names.append("Metric_Hint")

        # (C) Filter Hint (精准过滤搜)
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

        # (D) Target Hint (主体搜)
        if hints and hints.target_hint:
            tasks.append(asyncio.to_thread(
                rag_store.search_vectors,
                collection_name="schema",
                query_text=hints.target_hint,
                db_id=db_id,
                top_k=10
            ))
            task_names.append("Target_Hint")

        # (E) Knowledge Base
        knowledge_query = " ".join(keywords) if keywords else question
        tasks.append(self.knowledge_retriever.search_knowledge(
            knowledge_keywords=[], knowledge_query=knowledge_query, db_id=db_id, each_top_k=3
        ))
        task_names.append("Knowledge_Base")

        logger.info(f"🚀 [Orchestrator] Launching {len(tasks)} parallel tasks...")
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        # ------------------------------------------------------
        # 3. 合并与 Rerank - 保持不变
        # ------------------------------------------------------
        unique_candidates = {}
        knowledge_results = []

        for i, res in enumerate(results_list):
            name = task_names[i]
            if isinstance(res, Exception): continue
            if name == "Knowledge_Base":
                knowledge_results = res if isinstance(res, list) else []
                continue
            if not res: continue
            try:
                hits = res[0]
            except:
                continue

            for hit in hits:
                try:
                    ent = hit.entity
                    tbl, col = ent.get('table_name'), ent.get('column_name')
                    if not tbl or not col: continue
                    unique_key = f"{tbl}.{col}"
                    if unique_key not in unique_candidates:
                        unique_candidates[unique_key] = {
                            "content": f"Table: {tbl} | Column: {col}",
                            "entity": ent,
                            "milvus_score": hit.score,
                            "source": name
                        }
                except:
                    continue

        candidates_list = list(unique_candidates.values())
        if candidates_list:
            try:
                ranked_results = reranker.rerank(question, candidates_list, top_k=30)
            except:
                candidates_list.sort(key=lambda x: x['milvus_score'], reverse=True)
                ranked_results = []
                for item in candidates_list[:30]:
                    ranked_results.append({
                        "entity": item['entity'], "rerank_score": item['milvus_score'], "source": item['source']
                    })
        else:
            ranked_results = []

        selected_tables = set()
        retrieved_columns = []
        for item in ranked_results:
            ent = item.get('entity', {})
            tbl, col = ent.get("table_name"), ent.get("column_name")
            if tbl:
                selected_tables.add(tbl)
                retrieved_columns.append({
                    "table": tbl, "column": col, "desc": ent.get('doc_text', "")[:100]
                })

        join_paths = []
        if len(selected_tables) >= 2:
            try:
                graph_service.load_graph()
                join_paths = graph_service.search_join_path(db_id, list(selected_tables))
            except:
                pass

        # ------------------------------------------------------
        # 4. 确定 SQLite 路径
        # ------------------------------------------------------
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(current_dir, "../../.."))
        db_path = os.path.join(project_root, f"data/bird/dev_databases/{db_id}/{db_id}.sqlite")
        if not os.path.exists(db_path):
            db_path = os.path.join(project_root, f"data/bird/train_databases/{db_id}/{db_id}.sqlite")

        # ------------------------------------------------------
        # 🔥🔥🔥 核心优化: 智能值分离 (Smart Value Separation) 🔥🔥🔥
        # ------------------------------------------------------
        scan_candidates = []

        # 策略 A: 优先使用 Filter Hints (高置信度)
        if hints and hints.filter_hints:
            for hint in hints.filter_hints:
                scan_candidates.append(hint)
                # 拆分短语: "Alameda County" -> "Alameda", "County"
                scan_candidates.extend(hint.split())

        # 策略 B: 如果没有 Hints，回退到启发式 Keyword 筛选
        # 并且只保留看起来像实体的词 (大写开头, 纯数字, 或长词)
        if not scan_candidates and keywords:
            for k in keywords:
                if k[0].isupper() or k.isdigit() or len(k) > 4:
                    scan_candidates.append(k)

        # 去重
        scan_candidates = list(set(scan_candidates))

        value_matches = []
        if os.path.exists(db_path) and scan_candidates:
            # ✅ 只传筛选过的 scan_candidates，不再传所有 keywords
            # ✅ 传入 retrieved_columns (Targeted Scan)
            value_matches = await self._scan_for_values(db_path, retrieved_columns, scan_candidates)

        # ------------------------------------------------------
        # 5. Schema 采样与格式化
        # ------------------------------------------------------
        retrieved_columns = await self._inject_sample_values(db_path, retrieved_columns, limit_per_col=20)
        schema_str = self._format_schema_str(retrieved_columns)

        return {
            "query": question,
            "db_id": db_id,
            "retrieved_tables": list(selected_tables),
            "retrieved_columns": retrieved_columns,
            "join_paths": join_paths,
            "business_rules": knowledge_results,
            "schema_str": schema_str,
            # ✅ 别忘了这里！
            "value_matches": value_matches
        }


    # ==========================================================
    # 🛠️ 辅助方法
    # ==========================================================

    async def _scan_for_values(self, db_path: str, target_columns: List[Dict], keywords: List[str]) -> List[str]:
        """
        [v3.3 优化版] 精准打击：只在 Rerank 选出的高分列中搜索，包含噪音过滤和类型检查。
        Args:
            target_columns: Reranker 选出的 Top-K 列信息 (List[Dict])
        """
        # 1. 🛑 关键词清洗 (黑名单机制 - 过滤 Schema 词)
        SCHEMA_STOP_WORDS = {
            'what', 'which', 'where', 'show', 'list', 'count', 'total', 'average', 'highest', 'lowest',
            'many', 'much', 'ordered', 'group', 'order', 'by', 'asc', 'desc', 'limit', 'top',
            'school', 'schools', 'district', 'districts', 'county', 'counties', 'code', 'zip', 'id',
            'name', 'type', 'level', 'grade', 'number', 'rate', 'percent', 'avg', 'sum', 'all', 'of', 'in'
        }

        valid_keywords = [
            k for k in keywords
            if len(k) >= 3 and k.lower() not in SCHEMA_STOP_WORDS and not k.isdigit()
        ]

        if not valid_keywords:
            return []

        # 2. 🎯 构建搜索目标 (去重)
        # 提取 target_columns 中的 (table, column) 对，避免重复扫描
        scan_targets = set()
        for col in target_columns:
            t = col.get("table")
            c = col.get("column")
            if t and c:
                scan_targets.add((t, c))

        logger.info(
            f"🕵️ [ValueLink] Targeted Scan: {len(valid_keywords)} keywords in {len(scan_targets)} specific columns.")

        matches = []

        def _sync_scan():
            try:
                # 设定超时，防止卡死
                engine = create_engine(f"sqlite:///{db_path}", connect_args={'timeout': 3})
                with engine.connect() as conn:
                    # 针对每一个潜在的 (Table, Column) 对进行检查
                    for (tbl, col) in scan_targets:

                        # ⚡️ 预先检查：这列是不是文本列？(避免去数字/日期列里搜文本)
                        try:
                            # SQLite 特有的检查方式，其他 DB 可能不同
                            type_query = text(f"SELECT typeof(`{col}`) FROM `{tbl}` WHERE `{col}` IS NOT NULL LIMIT 1")
                            type_check = conn.execute(type_query).scalar()
                            # 如果不是 text, blob (有时候 text 被识别为 blob), 或 null，则跳过
                            if type_check and str(type_check).lower() not in ['text', 'blob', 'null']:
                                continue
                        except Exception:
                            # 如果出错（如列名带特殊字符导致），保守起见跳过或继续
                            continue

                        for kw in valid_keywords:
                            safe_kw = kw.replace("'", "''")  # 简单的 SQL 注入防护
                            try:
                                # 3. ⚡️ 优先精确匹配 (COLLATE NOCASE 忽略大小写)
                                query = text(f"""
                                    SELECT DISTINCT `{col}` FROM `{tbl}` 
                                    WHERE `{col}` = '{safe_kw}' COLLATE NOCASE
                                    LIMIT 1
                                """)
                                res = conn.execute(query).fetchone()

                                # 4. ⚡️ 只有长词才允许模糊匹配 (防止 'an' 匹配 'Andorra')
                                # 并且如果精确匹配已经找到了，就不做模糊匹配了
                                if not res and len(safe_kw) > 4:
                                    query = text(f"""
                                        SELECT DISTINCT `{col}` FROM `{tbl}` 
                                        WHERE `{col}` LIKE '%{safe_kw}%' 
                                        LIMIT 1
                                    """)
                                    res = conn.execute(query).fetchone()

                                if res:
                                    found_val = res[0]
                                    hint = f"Confirmed Entity: User mentions '{kw}' -> matches value '{found_val}' in `{tbl}`.`{col}`"
                                    matches.append(hint)
                                    logger.info(f"✅ {hint}")

                                    # 优化：如果这个关键词在这列找到了，通常不用再继续搜这个词了？
                                    # 视业务情况而定，这里暂不 break，以防同名词出现在多列
                            except Exception:
                                continue
            except Exception as e:
                logger.error(f"❌ [ValueLink] Scan error: {e}")

        await asyncio.to_thread(_sync_scan)
        return list(set(matches))

    async def _inject_sample_values(self, db_path: str, columns: List[Dict], limit_per_col: int = 20) -> List[Dict]:
        """
        采样 Schema 数据，帮助 LLM 理解数据格式 (保持原逻辑)
        """
        target_cols = []
        for col in columns[:20]:
            c_name = col.get("column", "").lower()
            if "id" in c_name and "grade" not in c_name and "code" not in c_name: continue
            if "date" in c_name or "time" in c_name: continue
            if "count" in c_name or "total" in c_name or "num" in c_name: continue
            if "rate" in c_name or "percent" in c_name: continue
            target_cols.append(col)

        if not target_cols or not os.path.exists(db_path): return columns

        def _sync_query():
            try:
                engine = create_engine(f"sqlite:///{db_path}")
                with engine.connect() as conn:
                    for col in target_cols:
                        tbl, cn = col.get("table"), col.get("column")
                        try:
                            # 简单的去重采样
                            query = text(
                                f'SELECT DISTINCT "{cn}" FROM "{tbl}" WHERE "{cn}" IS NOT NULL AND "{cn}" != "" LIMIT {limit_per_col}')
                            result = conn.execute(query).fetchall()
                            values = [str(row[0]) for row in result]
                            if values: col["sample_values"] = values
                        except:
                            continue
            except Exception:
                pass

        await asyncio.to_thread(_sync_query)
        return columns

    def _format_schema_str(self, columns: List[Dict]) -> str:
        lines = []
        tables = {}
        for col in columns:
            t = col.get("table")
            if t not in tables: tables[t] = []
            tables[t].append(col)
        for t, cols in tables.items():
            lines.append(f"Table: {t}")
            for c in cols:
                c_name, c_desc = c.get("column"), c.get("desc", "")
                samples = c.get("sample_values", [])
                sample_str = f" (Values: {', '.join([repr(x) for x in samples[:10]])})" if samples else ""
                lines.append(f"  - {c_name} | {c_desc}{sample_str}")
            lines.append("")
        return "\n".join(lines)


orchestrator = RAGOrchestrator()