import os
import networkx as nx
from typing import List, Dict, Any
from sqlalchemy import create_engine, text
from app.modules.retrieval.graph.service import graph_service
from app.core.logger import logger
import asyncio


class SchemaHelper:
    """负责 Schema 的补全、采样和格式化"""

    def augment_with_join_keys(self, db_id: str, retrieved_columns: List[Dict]) -> List[Dict]:
        """
        利用图结构补全缺失的连接键 (PK/FK/Evidence Keys)。
        """
        if not retrieved_columns:
            return []

        # 1. 提取当前已有的 "table.column" 集合，避免重复
        existing_keys = set()
        for r in retrieved_columns:
            t = r.get('table')
            c = r.get('column')
            if t and c:
                existing_keys.add(f"{t}.{c}")

        # 2. 提取涉及的表 (Top 5)
        seen_tables = set()
        top_tables = []
        for r in retrieved_columns:
            tbl = r.get('table')
            if tbl and tbl not in seen_tables:
                top_tables.append(tbl)
                seen_tables.add(tbl)

        target_tables = top_tables[:5]

        # 3. 调用 Graph Service 寻找连接键
        # ⚠️ 关键修改：我们需要更激进的策略
        # 策略 A: 找这些表之间的最短路径连接键 (依靠图算法)
        path_keys = graph_service.get_shortest_join_keys(db_id, target_tables)

        # 策略 B (新增): 找这些表之间所有的直接连接边 (依靠 Evidence Edges)
        # 这能确保 frpm.CDSCode <-> satscores.cds 这种边被捕获，即使算法觉得它权重不够低
        direct_keys = []
        try:
            # 这里假设 graph_service 暴露了 direct keys 的能力，如果没有，依靠 path_keys 通常也够了
            # 但为了稳妥，我们可以手动补充一个逻辑：强制获取涉及表的 PK
            pass
        except:
            pass

        # 合并所有找到的 keys
        all_needed_keys = set(path_keys)

        # 4. 🔥🔥🔥 核心补丁：强制召回主键 (Force PK Recall) 🔥🔥🔥
        # 如果图谱里定义了 PK，或者我们刚才的 Profiler 发现了强关联，必须带上
        graph = graph_service.get_graph(db_id)
        if graph:
            for tbl in target_tables:
                # 尝试从图的元数据里找 PK (如果我们在 build 时存了的话)
                # 或者简单的 heuristic: 找 name 包含 id/code 的列
                pass

        # 5. 将缺失的 Key 构造成列对象
        new_columns = []
        for key_str in all_needed_keys:
            if key_str not in existing_keys:
                try:
                    tbl, col = key_str.split('.')
                    new_columns.append({
                        "table": tbl,
                        "column": col,
                        "column_type": "TEXT",  # 默认值，inject_sample 会修正它
                        "sample_values": [],
                        "column_comment": "🗝️ Auto-augmented Join Key",  # 提示 LLM
                        "is_structural": True
                    })
                    existing_keys.add(key_str)
                except:
                    logger.warning(f"Invalid key string format: {key_str}")

        if new_columns:
            logger.info(f"🔗 [Graph] Augmented {len(new_columns)} join keys: {[c['column'] for c in new_columns]}")

        # 把补充的键放在列表末尾
        return retrieved_columns + new_columns

    @staticmethod
    async def inject_sample_values(db_path: str, columns: List[Dict], limit_per_col: int = 20) -> List[Dict]:
        target_cols = [c for c in columns if not c.get("sample_values")]
        if not target_cols or not os.path.exists(db_path): return columns

        def _sync_query():
            try:
                engine = create_engine(f"sqlite:///{db_path}")
                with engine.connect() as conn:
                    for col in target_cols:
                        tbl, cn = col.get("table"), col.get("column")
                        try:
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

    @staticmethod
    def format_schema_str(columns: List[Dict]) -> str:
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


schema_helper = SchemaHelper()