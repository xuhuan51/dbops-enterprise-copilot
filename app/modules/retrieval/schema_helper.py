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
        利用图结构补全缺失的连接键 (PK/FK)。
        """
        if not retrieved_columns:
            return []

        # 1. 提取当前已有的所有 "table.column" 集合，避免重复添加
        existing_keys = {f"{r['table']}.{r['column']}" for r in retrieved_columns}

        # 2. 提取涉及的表名 (保持 RAG 排序，取前几名高置信度的表)
        # 假设 retrieved_columns 是按相关性排序的
        seen_tables = set()
        top_tables = []
        for r in retrieved_columns:
            if r['table'] not in seen_tables:
                top_tables.append(r['table'])
                seen_tables.add(r['table'])

        # ⚠️ 限制只对 Top 5 的表进行路径补全，避免把排名第 20 的垃圾表连进来
        target_tables = top_tables[:5]

        # 3. 调用 Graph Service 寻找连接键
        # 比如：输入 [schools, frpm] -> 输出 [schools.CDSCode, frpm.CDSCode]
        missing_keys = graph_service.get_shortest_join_keys(db_id, target_tables)

        # 4. 将缺失的 Key 构造成列对象加入列表
        new_columns = []
        for key_str in missing_keys:
            if key_str not in existing_keys:
                try:
                    tbl, col = key_str.split('.')
                    new_columns.append({
                        "table": tbl,
                        "column": col,
                        "sample_values": [],  # 或者是 ["<ID>"]
                        "column_comment": "PK/FK for Join",  # 💡 提示 LLM 这个列的用途
                        "is_structural": True  # 标记这是结构性补充列
                    })
                    existing_keys.add(key_str)  # 防止重复
                except:
                    pass

        if new_columns:
            logger.info(f"🔗 [Graph] Augmented {len(new_columns)} join keys: {[c['column'] for c in new_columns]}")

        # 5. 合并列表
        # 建议把补充的 Key 放在列表后面，或者紧跟在相关表后面
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