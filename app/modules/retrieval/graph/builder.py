"""
表关系图构建器 - 简化修复版
==========================

只保留最可靠的3种关联策略：
1. 外键（权重0.1）- 数据库定义的
2. 表名注入（权重0.3）- 如 products.category_id -> categories.category_id
3. 同名主外键（权重1.0）- 如 users.user_id = orders.user_id

删除了不可靠的内容匹配和AI语义匹配
"""

import collections
import networkx as nx
from typing import List, Dict, Optional

from app.core.logger import logger


class SchemaGraphBuilder:
    """简表关系图构建器 - 只保留最可靠的策略"""

    def __init__(self, schema_catalog: List[Dict], encoder: Optional = None):
        """
        初始化

        参数:
            schema_catalog: 列信息列表
            encoder: 忽略（暂不使用AI）
        """
        self.raw_catalog = schema_catalog
        self.encoder = None  # 暂时禁用AI

        logger.info(f"✅ [图构建器] 初始化完成，共 {len(schema_catalog)} 个列")

    def build_graph(self) -> nx.MultiGraph:
        """构建关系图"""
        logger.info("🏗️  [图构建器] 开始构建...")

        G = nx.MultiGraph()

        # 建立索引
        table_map = self._build_table_index()
        pk_map = self._find_primary_keys(table_map)

        logger.info(f"   发现 {len(table_map)} 张表, {len(pk_map)} 个主键")

        # 只添加3层最可靠的边
        count1 = self._add_foreign_key_edges(G)
        count2 = self._add_name_injection_edges(G, table_map, pk_map)
        count3 = self._add_same_name_pk_edges(G, table_map)

        total = G.number_of_edges()
        logger.info(f"✅ [图构建器] 完成: {total} 条边 (外键{count1}, 注入{count2}, 同名{count3})")

        return G

    def _build_table_index(self) -> Dict:
        """构建表索引"""
        table_map = collections.defaultdict(dict)
        for item in self.raw_catalog:
            table = item.get("table_name")
            column = item.get("column_name")
            if table and column:
                table_map[table][column] = item
        return table_map

    def _find_primary_keys(self, table_map: Dict) -> Dict:
        """找主键"""
        pk_map = {}
        for table, columns in table_map.items():
            for col, info in columns.items():
                if info.get("is_primary_key"):
                    pk_map[table] = col
                    break
            if table not in pk_map and "id" in columns:
                pk_map[table] = "id"
        return pk_map

    def _add_foreign_key_edges(self, G: nx.MultiGraph) -> int:
        """
        Layer 1: 外键（最可靠）
        """
        count = 0
        for item in self.raw_catalog:
            if not item.get("is_foreign_key"):
                continue

            fk_target = item.get("foreign_key_target")
            if not fk_target:
                continue

            source_table = item["table_name"]
            source_column = item["column_name"]
            target_table = fk_target.get("table")
            target_column = fk_target.get("column")

            if target_table and target_column:
                self._add_edge(G, source_table, target_table,
                              source_column, target_column, 0.1, "外键")
                count += 1

        logger.info(f"   [外键] 添加 {count} 条")
        return count

    def _add_name_injection_edges(self, G: nx.MultiGraph, table_map: Dict, pk_map: Dict) -> int:
        """
        Layer 2: 表名注入
        例如: products.category_id -> categories.category_id
        """
        count = 0
        tables = list(table_map.keys())

        # 预计算单数形式
        singular_map = {t: self._to_singular(t) for t in tables}

        for target_table in tables:
            pk_column = pk_map.get(target_table)
            if not pk_column:
                continue

            # 期望的列名: category -> category_id
            expected_column = f"{singular_map[target_table]}_id"

            for source_table in tables:
                if source_table == target_table:
                    continue

                for col_name, col_info in table_map[source_table].items():
                    if col_name.lower() == expected_column:
                        # 类型兼容 + 不是已有外键
                        if (self._is_type_compatible(col_info, table_map[target_table][pk_column])
                            and not col_info.get("is_foreign_key")):
                            self._add_edge(G, source_table, target_table,
                                          col_name, pk_column, 0.3, "表名注入")
                            count += 1

        logger.info(f"   [表名注入] 添加 {count} 条")
        return count

    def _add_same_name_pk_edges(self, G: nx.MultiGraph, table_map: Dict) -> int:
        """
        Layer 3: 同名主键/外键
        只匹配以_id结尾的列
        """
        count = 0

        # 收集所有_id结尾的列
        id_columns = collections.defaultdict(list)

        for table, columns in table_map.items():
            for col, info in columns.items():
                if col.endswith('_id') and (info.get("is_primary_key") or info.get("is_foreign_key")):
                    id_columns[col.lower()].append((table, info))

        # 配对
        for col_name, occurrences in id_columns.items():
            if len(occurrences) < 2:
                continue

            # 跳过已有外键的
            for i in range(len(occurrences)):
                for j in range(i + 1, len(occurrences)):
                    t1, m1 = occurrences[i]
                    t2, m2 = occurrences[j]

                    if t1 == t2:
                        continue

                    # 至少一个是主键
                    if not (m1.get("is_primary_key") or m2.get("is_primary_key")):
                        continue

                    # 都不是外键（外键已经处理过了）
                    if m1.get("is_foreign_key") and m2.get("is_foreign_key"):
                        continue

                    if self._is_type_compatible(m1, m2):
                        self._add_edge(G, t1, t2, m1["column_name"],
                                      m2["column_name"], 1.0, "同名主外键")
                        count += 1

        logger.info(f"   [同名主外键] 添加 {count} 条")
        return count

    def _add_edge(self, G: nx.MultiGraph, u: str, v: str,
                  u_col: str, v_col: str, weight: float, edge_type: str):
        """添加边"""
        if u == v:
            return

        G.add_edge(u, v,
                  key=f"{u_col}:{v_col}",
                  weight=float(weight),
                  u_col=u_col,
                  v_col=v_col,
                  on=f"{u}.{u_col} = {v}.{v_col}",
                  type=edge_type)

    def _to_singular(self, table_name: str) -> str:
        """表名转单数"""
        t = table_name.lower().strip()
        if t.endswith('ies') and len(t) > 3:
            return t[:-3] + 'y'
        elif t.endswith('sses') and len(t) > 4:
            return t[:-2]
        elif t.endswith('s') and len(t) > 3:
            return t[:-1]
        return t

    def _get_type_category(self, col_info: Dict) -> str:
        """获取类型分类"""
        data_type = col_info.get("data_type", "").upper()
        if any(t in data_type for t in ["INT", "DECIMAL", "FLOAT", "DOUBLE", "NUMERIC"]):
            return "NUM"
        if any(t in data_type for t in ["CHAR", "VARCHAR", "TEXT"]):
            return "TEXT"
        return "OTHER"

    def _is_type_compatible(self, col1: Dict, col2: Dict) -> bool:
        """类型兼容检查"""
        type1 = self._get_type_category(col1)
        type2 = self._get_type_category(col2)
        return type1 == type2 and type1 != "OTHER"