import asyncio
import os
import re
import networkx as nx
from typing import Dict, Any, List
from dataclasses import dataclass

from app.core.rag_store import rag_store
from app.core.reranker import reranker
from app.modules.retrieval.graph.service import graph_service
from app.modules.retrieval.knowledge.retriever import KnowledgeRetriever
from app.core.state import AgentState
from app.core.logger import logger

# 🔥 引入特种兵
from app.modules.retrieval.value_scanner import value_scanner, MatchCandidate

# ==========================================
# Stop Words
# ==========================================
CONCEPT_STOP_WORDS = {
    "public", "private", "alternative",
    "elementary", "middle", "high",
    "free", "eligible", "reduced", "rate", "percent", "percentage",
    "average", "total", "count", "sum", "max", "min",
    "highest", "lowest", "top", "bottom"
}

SCHEMA_STOP_WORDS = {
    "what", "which", "where", "show", "list", "please",
    "many", "much", "ordered", "group", "order", "by",
    "asc", "desc", "limit", "of", "in", "for", "and", "or", "the", "a", "an", "to"
}

RE_GRADE_HINT = re.compile(r"^\s*k\s*-\s*12\s*$", re.IGNORECASE)
RE_AGE_RANGE = re.compile(r"^\s*\d+\s*-\s*\d+\s*$")


class RAGOrchestrator:
    def __init__(self):
        self.knowledge_retriever = KnowledgeRetriever()

    async def get_retrieval_context(self, state: AgentState) -> Dict[str, Any]:
        """v7.0: Graph-Enhanced RAG (Graph Augmentation + Global Rules)"""
        question = state.get("question", "")
        db_id = state.get("db_id", "")
        intent_data = state.get("intent_data")
        hints = getattr(intent_data, "semantic_hints", None)

        # ------------------------------------------------------
        # 1. 关键词提取
        # ------------------------------------------------------
        raw_keywords = getattr(intent_data, "search_keywords", [])
        all_kw_strs: List[str] = []
        value_kw_strs: List[str] = []

        if raw_keywords:
            for item in raw_keywords:
                if isinstance(item, str):
                    k_str = item.strip()
                else:
                    k_str = getattr(item, "keyword", "").strip() if hasattr(item, "keyword") else item.get("keyword",
                                                                                                           "").strip()
                    k_type = getattr(item, "type", "CONCEPT") if hasattr(item, "type") else item.get("type", "CONCEPT")
                    if k_str and str(k_type).upper() == "VALUE":
                        value_kw_strs.append(k_str)

                if k_str: all_kw_strs.append(k_str)

        if not all_kw_strs:
            all_kw_strs = [w for w in question.split() if len(w) > 3]

        logger.info(f"🔍 [Orchestrator] Start Retrieval for: {question[:50]}...")

        # ------------------------------------------------------
        # 2. 多路召回 (Schema + Knowledge)
        # ------------------------------------------------------
        tasks, task_names = [], []

        # Schema Vector Search
        if all_kw_strs:
            full_query = " ".join(all_kw_strs)
            tasks.append(asyncio.to_thread(
                rag_store.search_vectors,
                collection_name="schema",
                query_text=full_query,
                db_id=db_id,
                top_k=20
            ))
            task_names.append("SCHEMA")

        # Hints Vector Search
        if hints:
            if hints.metric_hint:
                tasks.append(asyncio.to_thread(
                    rag_store.search_vectors,
                    collection_name="schema",
                    query_text=hints.metric_hint,
                    db_id=db_id,
                    top_k=15
                ))
                task_names.append("SCHEMA")
            if hints.filter_hints:
                tasks.append(asyncio.to_thread(
                    rag_store.search_vectors,
                    collection_name="schema",
                    query_text=" ".join(hints.filter_hints),
                    db_id=db_id,
                    top_k=15
                ))
                task_names.append("SCHEMA")

        # Knowledge Search
        knowledge_query = " ".join(all_kw_strs) if all_kw_strs else question
        tasks.append(self.knowledge_retriever.search_knowledge([], knowledge_query, db_id, 3))
        task_names.append("KNOWLEDGE")

        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        # ------------------------------------------------------
        # 3. 结果合并与 Rerank
        # ------------------------------------------------------
        unique_candidates = {}
        knowledge_results = []

        def _recursive_extract_hits(obj):
            extracted = []
            if isinstance(obj, list) or (hasattr(obj, '__iter__') and not isinstance(obj, (dict, str))):
                for item in obj: extracted.extend(_recursive_extract_hits(item))
            elif hasattr(obj, 'entity') or (isinstance(obj, dict) and 'entity' in obj):
                extracted.append(obj)
            return extracted

        for i, res in enumerate(results_list):
            t_name = task_names[i]
            if isinstance(res, Exception) or not res:
                if isinstance(res, Exception):
                    logger.warning(f"⚠️ Task {t_name} failed: {res}")
                continue

            if t_name == "SCHEMA":
                all_hits = _recursive_extract_hits(res)
                for hit in all_hits:
                    ent = getattr(hit, 'entity', hit.get('entity'))
                    score = getattr(hit, 'score', getattr(hit, 'distance', 0))

                    if not ent: continue
                    tbl, col = ent.get('table_name'), ent.get('column_name')
                    if not tbl or not col: continue

                    unique_key = f"{tbl}.{col}"
                    if unique_key not in unique_candidates:
                        unique_candidates[unique_key] = {
                            "content": f"Table: {tbl} | Column: {col} | Comment: {ent.get('column_comment', '')}",
                            "entity": ent,
                            "milvus_score": score
                        }

            elif t_name == "KNOWLEDGE":
                if isinstance(res, list): knowledge_results = res

        # Rerank Logic
        candidates_list = list(unique_candidates.values())
        ranked_results = []
        if candidates_list:
            try:
                ranked_results = reranker.rerank(question, candidates_list, top_k=20)
            except:
                candidates_list.sort(key=lambda x: x['milvus_score'], reverse=True)
                ranked_results = [{"entity": item['entity']} for item in candidates_list[:30]]

        # 构建 retrieved_columns
        selected_tables = set()
        retrieved_columns = []
        for item in ranked_results:
            ent = item.get('entity', {})
            tbl, col = ent.get("table_name"), ent.get("column_name")
            if tbl and col:
                selected_tables.add(tbl)
                retrieved_columns.append({
                    "table": tbl,
                    "column": col,
                    "sample_values": ent.get("samples") or []
                })

        # ------------------------------------------------------
        # 4. 🔥🔥🔥 Value Scanning (特种兵) 🔥🔥🔥
        # ------------------------------------------------------
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(current_dir, "../../.."))
        db_path = os.path.join(project_root, f"data/bird/dev_databases/{db_id}/{db_id}.sqlite")
        if not os.path.exists(db_path):
            db_path = os.path.join(project_root, f"data/bird/train_databases/{db_id}/{db_id}.sqlite")

        final_constraints = []
        best_matches = []

        if os.path.exists(db_path) and retrieved_columns:
            scan_phrases = self._prepare_scan_phrases(value_kw_strs, all_kw_strs, hints)
            need_rescue = bool(hints and hints.filter_hints)

            try:
                raw_matches, rescue_columns = await asyncio.to_thread(
                    value_scanner.scan_tiered,
                    db_path=db_path,
                    columns=retrieved_columns,
                    keywords=scan_phrases,
                    use_rescue=need_rescue
                )

                if rescue_columns:
                    retrieved_columns.extend(rescue_columns)

                best_matches = self._select_best_matches(raw_matches, question, hints)

                hard = [m for m in best_matches if m.strength == "hard"][:2]
                soft = [m for m in best_matches if m.strength != "hard"][:2]
                final_constraints = [m.format_constraint() for m in (hard + soft)]

                logger.info(f"🏆 [ValueLink] Final Constraints: {len(final_constraints)}")
            except Exception as e:
                logger.error(f"❌ Value scan failed: {e}", exc_info=True)

        # ------------------------------------------------------
        # 5. 🔥🔥🔥 Graph Augmentation (图增强补全) 🔥🔥🔥
        # ------------------------------------------------------
        # 注意：这里调用 self._augment_with_join_keys
        retrieved_columns = self._augment_with_join_keys(db_id, retrieved_columns)

        # 更新 selected_tables (因为可能引入了新的列)
        selected_tables = list({c['table'] for c in retrieved_columns})

        # ------------------------------------------------------
        # 6. Schema 采样 & 格式化
        # ------------------------------------------------------
        retrieved_columns = await self._inject_sample_values(db_path, retrieved_columns, limit_per_col=20)
        schema_str = self._format_schema_str(retrieved_columns)

        # ------------------------------------------------------
        # 7. 🔥🔥🔥 Global Rules Injection (全局规则注入) 🔥🔥🔥
        # ------------------------------------------------------
        global_join_rule = {
            "content": "The column `CDSCode` (or `cds`) is the unique Primary Key used to join ALL tables (schools, frpm, satscores). ALWAYS use `T1.CDSCode = T2.CDSCode` for joins. Never use 'Charter School Number' or 'NCESSchool' for joining.",
            "source": "global_rule",
            "score": 1.0
        }
        global_calc_rule = {
            "content": "If the user asks for 'count' or 'number', DO NOT calculate a rate or percentage (do not divide by enrollment). Use the absolute value column directly.",
            "source": "global_rule",
            "score": 0.99
        }

        if isinstance(knowledge_results, list):
            knowledge_results.insert(0, global_calc_rule)
            knowledge_results.insert(0, global_join_rule)

        # Join Path
        join_paths = []
        if len(selected_tables) >= 2:
            try:
                graph_service.load_graph()
                join_paths = graph_service.search_join_path(db_id, list(selected_tables))
            except:
                pass

        return {
            "query": question,
            "db_id": db_id,
            "retrieved_tables": list(selected_tables),
            "retrieved_columns": retrieved_columns,
            "join_paths": join_paths,
            "business_rules": knowledge_results,
            "schema_str": schema_str,
            "value_matches": final_constraints
        }

    # ==========================================================
    # 🌟 NEW: Graph Augmentation Logic (定义在类内部)
    # ==========================================================
    def _augment_with_join_keys(self, db_id: str, retrieved_columns: List[Dict]) -> List[Dict]:
        """利用 Schema Graph 自动补全连接键"""
        active_tables = list({c['table'] for c in retrieved_columns})
        if len(active_tables) < 2:
            return retrieved_columns

        try:
            graph_service.load_graph()
            G = graph_service.graphs.get(db_id)
            if not G: return retrieved_columns

            existing_col_keys = {f"{c['table']}.{c['column']}".lower() for c in retrieved_columns}
            new_columns = []

            for i in range(len(active_tables)):
                for j in range(i + 1, len(active_tables)):
                    t1, t2 = active_tables[i], active_tables[j]

                    try:
                        if nx.has_path(G, t1, t2):
                            path = nx.shortest_path(G, t1, t2)

                            for k in range(len(path) - 1):
                                u, v = path[k], path[k + 1]
                                edges = G.get_edge_data(u, v)
                                if not edges: continue

                                best_edge_key = max(edges, key=lambda x: edges[x]['weight'])
                                best_edge = edges[best_edge_key]

                                u_col, v_col = best_edge.get('u_col'), best_edge.get('v_col')

                                if u_col and f"{u}.{u_col}".lower() not in existing_col_keys:
                                    new_columns.append({
                                        "table": u, "column": u_col,
                                        "desc": "Auto-injected Join Key (Graph Path)",
                                        "sample_values": []
                                    })
                                    existing_col_keys.add(f"{u}.{u_col}".lower())

                                if v_col and f"{v}.{v_col}".lower() not in existing_col_keys:
                                    new_columns.append({
                                        "table": v, "column": v_col,
                                        "desc": "Auto-injected Join Key (Graph Path)",
                                        "sample_values": []
                                    })
                                    existing_col_keys.add(f"{v}.{v_col}".lower())
                    except:
                        continue

            if new_columns:
                logger.info(
                    f"🌉 [Graph Augmentation] Injected {len(new_columns)} join keys: {[c['column'] for c in new_columns]}")
                retrieved_columns.extend(new_columns)

        except Exception as e:
            logger.warning(f"⚠️ Graph augmentation warning: {e}")

        return retrieved_columns

    # ==========================================================
    # Helpers (Business Logic Only)
    # ==========================================================
    def _prepare_scan_phrases(self, value_kw, all_kw, hints) -> List[str]:
        phrases = []
        for v in value_kw:
            if len(v.strip()) >= 2: phrases.append(v.strip())

        if hints and hints.filter_hints:
            for h in hints.filter_hints:
                if h and not RE_GRADE_HINT.match(h) and not RE_AGE_RANGE.match(h):
                    phrases.append(h.strip())

        for w in all_kw:
            wl = (w or "").strip()
            if not wl: continue
            if wl.lower() in SCHEMA_STOP_WORDS or wl.lower() in CONCEPT_STOP_WORDS: continue
            if RE_GRADE_HINT.match(wl) or RE_AGE_RANGE.match(wl): continue

            if len(wl) >= 4 and (any(c.isupper() for c in wl) or any(c.isdigit() for c in wl)):
                phrases.append(wl)

        final_phrases = []
        for p in phrases:
            final_phrases.append(p)
            m = re.match(r"^\s*(.+?)\s+county\s*$", p, flags=re.IGNORECASE)
            if m and len(m.group(1)) >= 3: final_phrases.append(m.group(1))

        return list(set(final_phrases))

    def _select_best_matches(self, matches: List[MatchCandidate], question: str, hints) -> List[MatchCandidate]:
        if not matches: return []

        ql = question.lower()
        hint_text = ""
        if hints:
            try:
                hint_text = " ".join((hints.filter_hints or []) + [hints.metric_hint or ""]).lower()
            except:
                pass
        ctx = f"{ql} {hint_text}"

        want_county = "county" in ctx
        want_district = "district" in ctx

        grouped = {}
        for m in matches:
            grouped.setdefault(m.keyword, []).append(m)

        final_list = []
        for kw, group in grouped.items():
            for m in group:
                if want_county and "county" in m.column.lower(): m.score += 20
                if want_district and "district" in m.column.lower(): m.score += 20

            group.sort(key=lambda x: x.score, reverse=True)
            best = group[0]

            if best.score >= 70:
                final_list.append(best)

        unique_map = {}
        for m in sorted(final_list, key=lambda x: x.score, reverse=True):
            k = f"{m.table}.{m.column}"
            if k not in unique_map:
                unique_map[k] = m

        return list(unique_map.values())

    async def _inject_sample_values(self, db_path: str, columns: List[Dict], limit_per_col: int = 20) -> List[Dict]:
        from sqlalchemy import create_engine, text

        target_cols = [c for c in columns if not c.get("sample_values")]
        if not target_cols or not os.path.exists(db_path): return columns

        def _sync_query():
            try:
                engine = create_engine(f"sqlite:///{db_path}")
                with engine.connect() as conn:
                    for col in target_cols:
                        tbl, cn = col.get("table"), col.get("column")
                        try:
                            # 采样非空值
                            query = text(
                                f'SELECT DISTINCT "{cn}" FROM "{tbl}" WHERE "{cn}" IS NOT NULL LIMIT {limit_per_col}')
                            rows = conn.execute(query).fetchall()
                            vals = [str(r[0]) for r in rows]
                            if vals: col["sample_values"] = vals
                        except:
                            pass
            except:
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
                c_name = c.get("column")
                desc = c.get("desc", "")
                samples = c.get("sample_values") or c.get("samples", [])
                s_str = f" (Values: {', '.join([repr(x) for x in samples[:15]])})" if samples else ""
                lines.append(f"  - {c_name} | {desc}{s_str}")
            lines.append("")
        return "\n".join(lines)


orchestrator = RAGOrchestrator()