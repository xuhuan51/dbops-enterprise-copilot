import os
import json
import collections
import networkx as nx
import numpy as np
from typing import List, Dict, Any, Tuple, Optional
from sklearn.metrics.pairwise import cosine_similarity

from app.core.config import settings
from app.core.logger import logger

try:
    from app.core.rag_store import encoder
except ImportError:
    logger.warning("⚠️ [GraphBuilder] encoder not found. Semantic matching disabled.")
    encoder = None


class SchemaGraphBuilder:
    """
    🏆 SOTA Strategy V2: Robust & Multi-Path

    Changes from V1:
    - Base: nx.MultiGraph (Allow parallel edges)
    - Filter: Smart ignore (Keys are never ignored)
    - Content: Cardinality check (Avoid enum overlap)
    - Injection: Better plural handling (categories -> category)
    - Edge: Structured attributes (u_col, v_col)
    """

    def __init__(self, catalog_data: List[Dict[str, Any]]):
        self.raw_catalog = catalog_data
        self.db_groups = collections.defaultdict(list)
        self._group_by_db()

        self.evidence_dir = os.path.join("../data/bird/metadata/evidence_joins")

        # 脏词表 (仅对非 Key 列生效)
        self.ignore_substrings = {
            "name", "desc", "description", "comment", "remark", "note",
            "status", "type", "category", "kind", "sex", "gender",
            "date", "time", "year", "month", "day", "hour",
            "created", "updated", "create", "update", "cnt", "count",
            "total", "sum", "avg"
        }
        self.key_suffixes = ("_id", "_code", "_no", "_key", "_num", "_sk", "_pk")

    def build_all(self) -> Dict[str, nx.MultiGraph]:
        """Entry point: Build MultiGraphs for all databases."""
        graphs = {}
        total = len(self.db_groups)
        logger.info(f"🕸️ [GraphBuilder V2] Building MultiGraphs for {total} DBs...")

        for i, (db_id, items) in enumerate(self.db_groups.items()):
            if i % 10 == 0: logger.debug(f"  > Processing {i}/{total}: {db_id}")
            graphs[db_id] = self._build_single_db_graph(db_id, items)

        return graphs

    def _group_by_db(self):
        for item in self.raw_catalog:
            if item.get("db_id"):
                self.db_groups[item["db_id"]].append(item)

    # =========================================================================
    # 🏗️ Single DB Build Logic
    # =========================================================================
    def _build_single_db_graph(self, db_id: str, items: List[Dict[str, Any]]) -> nx.MultiGraph:
        # 🔥 Change 1: Use MultiGraph to keep multiple potential join paths
        G = nx.MultiGraph()

        table_map = collections.defaultdict(dict)
        inverted_index = collections.defaultdict(list)
        pk_cols = {}

        # 1. Indexing & Pre-calculation
        for item in items:
            t, c = item.get("table"), item.get("column")
            if not t or not c: continue

            table_map[t][c] = item

            # Identify PK: Explicit > 'id' > First column ending with 'id'
            if item.get("is_pk"):
                pk_cols[t] = c
            elif c.lower() == "id" and t not in pk_cols:
                pk_cols[t] = c

            # Inverted Index (Smart Filter Applied)
            if not self._is_ignored(item):
                cn = self._col_norm(item)
                inverted_index[cn].append((t, item))

        # ---------------------------------------------------------
        # Layer 1: Explicit FK (Weight 0.1)
        # ---------------------------------------------------------
        self._add_explicit_fk_edges(G, items)

        # ---------------------------------------------------------
        # Layer 2: Evidence Edges (Weight 0.2)
        # ---------------------------------------------------------
        self._add_evidence_edges(G, db_id)

        # ---------------------------------------------------------
        # Layer 3: Table Name Injection (Weight 0.3)
        # ---------------------------------------------------------
        self._add_injection_edges(G, table_map, pk_cols)

        # ---------------------------------------------------------
        # Layer 4: Content-based IND (Weight 0.5 - 1.5)
        # ---------------------------------------------------------
        self._add_content_based_edges(G, items)

        # ---------------------------------------------------------
        # Layer 5: Same Key Name (Weight 1.2 - 2.0)
        # ---------------------------------------------------------
        self._add_same_key_name_edges(G, inverted_index)

        # ---------------------------------------------------------
        # Layer 6: Semantic Matching (Weight 2.0 - 3.0)
        # ---------------------------------------------------------
        if encoder:
            self._add_semantic_edges(G, table_map)

        return G

    # =========================================================================
    # 🧩 Edge Strategies
    # =========================================================================

    def _add_explicit_fk_edges(self, G, items):
        for item in items:
            if item.get("is_fk") and item.get("fk_to"):
                u, u_col = item["table"], item["column"]
                for fk in item["fk_to"]:
                    v, v_col = fk.get("table"), fk.get("column")
                    if v and v_col:
                        self._add_edge(G, u, v, u_col, v_col, 0.1, "EXPLICIT")

    def _add_evidence_edges(self, G, db_id):
        if not os.path.exists(self.evidence_dir): return
        fpath = os.path.join(self.evidence_dir, f"{db_id}.json")
        if os.path.exists(fpath):
            try:
                with open(fpath, 'r') as f:
                    edges = json.load(f)
                    for e in edges:
                        self._add_edge(G, e['u'], e['v'], e['u_col'], e['v_col'], 0.2, "EVIDENCE")
            except:
                pass

    def _add_injection_edges(self, G, table_map, pk_cols):
        tables = list(table_map.keys())
        # Cache singular forms
        singular_map = {t: self._singular_table(t) for t in tables}

        for b in tables:
            b_pk = pk_cols.get(b)
            if not b_pk: continue

            # Robust Pattern: "category_id" matching "categories"
            target_col = f"{singular_map[b]}_id"
            b_pk_meta = table_map[b][b_pk]

            for a in tables:
                if a == b: continue

                # Scan A's columns
                for a_col, a_meta in table_map[a].items():
                    a_norm = self._col_norm(a_meta)

                    if a_norm == target_col:
                        # 🔥 Change: Strict type check for Injection
                        if self._is_type_compatible(a_meta, b_pk_meta):
                            self._add_edge(G, a, b, a_col, b_pk, 0.3, "INJECTION")

    def _add_content_based_edges(self, G, items):
        """
        🔥 Change: Cardinality Check
        Avoid joining boolean/enum columns (e.g. 0/1, M/F)
        """
        # 1. Filter candidates
        candidates = []
        for item in items:
            if not self._is_discrete_type(item): continue

            samples = item.get("samples", [])
            if not samples: continue

            # 🔥 Check: Unique Value Count
            # Convert to string to handle mixed types
            unique_vals = set(str(s) for s in samples)

            # Threshold: If samples are fewer than 5 and cardinality is low, skip.
            # Example: samples=[1, 0, 1], unique=2 -> SKIP
            # Example: samples=[101, 102, 103], unique=3 -> KEEP (if sample limit is 3)
            # Heuristic: If we only fetched 10 samples, and got 2 unique, it's risky.
            if len(unique_vals) < 3 and len(samples) >= 5:
                continue

            candidates.append({"meta": item, "set": unique_vals})

        # 2. Group by type category
        type_groups = collections.defaultdict(list)
        for c in candidates:
            type_groups[self._get_type_cat(c["meta"])].append(c)

        # 3. Pairwise check
        for cat, group in type_groups.items():
            if len(group) < 2: continue
            n = len(group)

            for i in range(n):
                for j in range(i + 1, n):
                    c1, c2 = group[i], group[j]
                    t1, col1 = c1["meta"]["table"], c1["meta"]["column"]
                    t2, col2 = c2["meta"]["table"], c2["meta"]["column"]

                    if t1 == t2: continue

                    s1, s2 = c1["set"], c2["set"]
                    intersection = len(s1 & s2)
                    min_len = min(len(s1), len(s2))

                    if min_len == 0: continue
                    overlap = intersection / min_len

                    # 🔥 Change: Threshold Tuning
                    if overlap >= 0.9:
                        self._add_edge(G, t1, t2, col1, col2, 0.5, "CONTENT_STRONG")
                    elif overlap >= 0.6:
                        # Only allow weak content edge if names are vaguely similar?
                        # Or just give it high weight.
                        self._add_edge(G, t1, t2, col1, col2, 1.5, "CONTENT_WEAK")

    def _add_same_key_name_edges(self, G, inverted_index):
        for col_norm, nodes in inverted_index.items():
            if len(nodes) < 2: continue
            if col_norm in ("id", "name"): continue  # Skip too generic

            for i in range(len(nodes)):
                for j in range(i + 1, len(nodes)):
                    t1, m1 = nodes[i]
                    t2, m2 = nodes[j]
                    if t1 == t2: continue

                    if self._is_type_compatible(m1, m2):
                        is_pk = bool(m1.get("is_pk") or m2.get("is_pk"))
                        # Lower weight than injection/content
                        weight = 1.2 if is_pk else 2.0
                        self._add_edge(G, t1, t2, m1["column"], m2["column"], weight, "SAME_NAME")

    def _add_semantic_edges(self, G, table_map):
        """
        🔥 Change: Top-K Optimization
        Avoid NxN matrix if N is large.
        """
        candidates = []
        for t, cols in table_map.items():
            for c, meta in cols.items():
                # Only check columns that "look like keys" or have comments
                if self._looks_like_key(meta) or meta.get("column_comment"):
                    candidates.append(meta)

        if len(candidates) < 2: return

        # Optimization: Limit candidate size (e.g., max 500 per DB)
        # If > 500, maybe filter stricter
        if len(candidates) > 500:
            candidates = [c for c in candidates if self._looks_like_key(c)]

        texts = [
            f"{m['table']} {m['column']} {m.get('column_comment', '')}"
            for m in candidates
        ]

        try:
            vecs = encoder.encode(texts, normalize_embeddings=True)
            # Dot product
            sim_matrix = np.dot(vecs, vecs.T)

            # 🔥 Top-K filtering
            # We only care about pairs with Sim > 0.85
            # Using numpy masking is faster than iterating N^2
            rows, cols = np.where(sim_matrix > 0.85)

            for r, c in zip(rows, cols):
                if r >= c: continue  # Upper triangle

                c1, c2 = candidates[r], candidates[c]
                if c1['table'] == c2['table']: continue

                if self._is_type_compatible(c1, c2):
                    self._add_edge(G, c1['table'], c2['table'],
                                   c1['column'], c2['column'], 2.5, "SEMANTIC")
        except Exception as e:
            logger.warning(f"Semantic build error: {e}")

    # =========================================================================
    # 🛠️ Helpers
    # =========================================================================
    def _add_edge(self, G, u, v, u_col, v_col, weight, type_):
        # 🔥 Change: MultiGraph allows multiple edges.
        # We store structured data, not just "on" string.
        if u == v: return

        G.add_edge(
            u, v,
            key=f"{u_col}:{v_col}",  # Unique key for MultiGraph
            weight=float(weight),
            u_col=u_col,
            v_col=v_col,
            on=f"{u}.{u_col} = {v}.{v_col}",  # Legacy support
            type=type_
        )

    def _col_norm(self, meta):
        return meta.get("column_norm", meta["column"]).strip().lower()

    def _singular_table(self, t):
        t = t.lower().strip()
        # 🔥 Change: Better heuristics
        if t.endswith('ies') and len(t) > 3: return t[:-3] + 'y'
        if t.endswith('sses') and len(t) > 4: return t[:-2]
        if t.endswith('s') and len(t) > 3: return t[:-1]
        return t

    def _is_ignored(self, item):
        norm = self._col_norm(item)

        # 🔥 Change: Smart Ignore
        # If it looks like a key, NEVER ignore it (even if it contains 'status')
        if self._looks_like_key(item): return False

        # Otherwise, check blocklist
        if norm in self.ignore_substrings: return True
        return any(x in norm for x in self.ignore_substrings)

    def _looks_like_key(self, item):
        cn = self._col_norm(item)
        if item.get("is_pk"): return True
        if cn == "id": return True
        if cn.endswith(self.key_suffixes): return True
        return False

    def _get_type_cat(self, meta):
        t = (meta.get("column_type") or "").upper()
        if any(x in t for x in ["INT", "NUM", "DEC", "REAL", "FLOAT", "DOUBLE"]): return "NUM"
        if any(x in t for x in ["CHAR", "TEXT", "CLOB", "VARCHAR"]): return "TEXT"
        return "OTHER"

    def _is_discrete_type(self, meta):
        return self._get_type_cat(meta) in ["NUM", "TEXT"]

    def _is_type_compatible(self, m1, m2):
        return self._get_type_cat(m1) == self._get_type_cat(m2) and self._get_type_cat(m1) != "OTHER"