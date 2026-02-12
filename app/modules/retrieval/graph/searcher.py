"""
表关系路径搜索器 (MySQL版本)
==========================

这个文件是干什么的？
------------------
有了"关系图"（builder.py构建的）后，这个文件负责在图上找路径。

场景举例：
用户问："查询买过iPhone的用户"
系统识别出需要这3个表：users, orders, products

问题来了：这3个表怎么连？
- users 和 orders 怎么JOIN？
- orders 和 products 怎么JOIN？

这个文件就是解决这个问题的！

核心功能：
---------
1. 输入：需要查询的表列表 ['users', 'orders', 'products']
2. 输出：JOIN条件列表 ['users.user_id = orders.user_id', 'orders.product_id = products.product_id']

算法：
-----
- 2个表：找最短路径（Dijkstra算法）
- 3个及以上：找Steiner树（最小生成树的变种）

使用示例：
---------
>>> finder = SchemaPathFinder(graph)
>>> joins = finder.find_path(['users', 'orders', 'products'])
>>> print(joins)
['users.user_id = orders.user_id', 'orders.product_id = products.product_id']
"""

import networkx as nx
from typing import List
from app.core.logger import logger


class SchemaPathFinder:
    """
    表关系路径搜索器

    职责：
    在关系图上找出连接多个表的最优路径
    """

    def __init__(self, raw_graph: nx.MultiGraph):
        """
        初始化搜索器

        参数：
            raw_graph: 由 SchemaGraphBuilder 构建的关系图
                      MultiGraph类型，两个表之间可能有多条边
        """
        self.raw_graph = raw_graph

        # 压平图：把MultiGraph转成简单Graph
        # 为什么？因为路径搜索算法不支持MultiGraph
        # 怎么压？保留每对表之间权重最小的那条边
        self.search_graph = self._flatten_graph(raw_graph)

    def _flatten_graph(self, multi_graph: nx.MultiGraph) -> nx.Graph:
        """
        压平多重图

        问题：
        两个表之间可能有多条边，例如：
        users -- orders 有3条边：
        1. users.user_id = orders.user_id (权重0.1, 外键)
        2. users.email = orders.email (权重2.0, 同名列)
        3. users.phone = orders.phone (权重2.5, AI匹配)

        解决：
        只保留权重最小的那条（最可靠的）

        结果：
        users -- orders 只保留边1
        """
        simple_graph = nx.Graph()

        # 遍历所有边
        for u, v, key, data in multi_graph.edges(keys=True, data=True):
            current_weight = data.get('weight', 999.0)

            if simple_graph.has_edge(u, v):
                # 已有边，比较权重
                existing_weight = simple_graph[u][v].get('weight', 999.0)

                # 如果新边更好（权重更小），替换
                if current_weight < existing_weight:
                    simple_graph.add_edge(u, v, **data)
            else:
                # 没有边，直接添加
                simple_graph.add_edge(u, v, **data)

        return simple_graph

    def find_path(self, tables: List[str]) -> List[str]:
        """
        找出连接多个表的最优JOIN路径

        参数：
            tables: 需要连接的表列表，例如 ['users', 'orders', 'products']

        返回：
            JOIN条件列表，例如：
            [
                'users.user_id = orders.user_id',
                'orders.product_id = products.product_id'
            ]

        算法选择：
        - 2个表 -> 最短路径（Dijkstra）
        - 3个及以上 -> Steiner树（近似算法）
        """
        # 第一步：过滤掉图中不存在的表
        valid_tables = [t for t in tables if self.search_graph.has_node(t)]

        # 少于2个表，无法JOIN
        if len(valid_tables) < 2:
            logger.debug(f"⚠️ [路径搜索] 可连接的表不足2个: {valid_tables}")
            return []

        try:
            # === 场景A: 只有2个表 ===
            if len(valid_tables) == 2:
                return self._find_two_table_path(valid_tables[0], valid_tables[1])

            # === 场景B: 3个表及以上 ===
            return self._find_multi_table_path(valid_tables)

        except nx.NetworkXNoPath:
            # 表之间没有路径（图不连通）
            logger.debug(f"⚠️ [路径搜索] 表之间无连接路径: {valid_tables}")
            return []

        except Exception as e:
            logger.error(f"❌ [路径搜索] 算法执行失败: {e}")
            return []

    def _find_two_table_path(self, table1: str, table2: str) -> List[str]:
        """
        两个表的最短路径

        算法：Dijkstra最短路径

        例如：
        users -> orders

        可能的路径：
        1. users -> orders (直连，权重0.1)
        2. users -> addresses -> orders (绕路，权重0.5)

        选择路径1
        """
        # 使用 weight 属性计算最短路径
        path_nodes = nx.shortest_path(
            self.search_graph,
            source=table1,
            target=table2,
            weight='weight'
        )

        # 将节点路径转换为JOIN条件
        # [users, orders] -> ['users.user_id = orders.user_id']
        return self._nodes_to_joins(path_nodes)

    def _find_multi_table_path(self, tables: List[str]) -> List[str]:
        """
        多个表的最优连接路径

        算法：Steiner树（近似算法）

        问题：
        给定N个表，找一个最小权重的子图连接它们

        例如：
        需要连接: users, orders, products

        可能的方案：
        1. users -> orders -> products (2条边)
        2. users -> addresses -> orders -> products (3条边)

        选择方案1（边少且权重小）
        """
        # 计算Steiner树
        subtree = nx.approximation.steiner_tree(
            self.search_graph,
            terminal_nodes=tables,
            weight='weight'
        )

        # 将树转换为边的列表（深度优先遍历）
        # 从第一个表开始遍历
        edges = list(nx.dfs_edges(subtree, source=tables[0]))

        # 将边转换为JOIN条件
        joins = []
        for u, v in edges:
            edge_data = self.search_graph.get_edge_data(u, v)
            if edge_data and 'on' in edge_data:
                joins.append(edge_data['on'])

        return joins

    def _nodes_to_joins(self, path_nodes: List[str]) -> List[str]:
        """
        将节点路径转换为JOIN条件

        输入：
            ['users', 'orders', 'products']

        输出：
            [
                'users.user_id = orders.user_id',
                'orders.product_id = products.product_id'
            ]
        """
        joins = []

        # 遍历相邻节点对
        for i in range(len(path_nodes) - 1):
            u, v = path_nodes[i], path_nodes[i + 1]

            # 获取边的信息
            edge_data = self.search_graph.get_edge_data(u, v)

            if edge_data and 'on' in edge_data:
                joins.append(edge_data['on'])

        return joins

    def get_join_keys(self, tables: List[str]) -> List[str]:
        """
        获取JOIN所需的所有关键列

        用途：
        当RAG检索后，需要补全schema信息时使用

        参数：
            tables: 需要连接的表列表

        返回：
            列名列表（带表前缀），例如：
            [
                'users.user_id',
                'orders.user_id',
                'orders.product_id',
                'products.product_id'
            ]

        工作流程：
        1. 找出连接这些表的最优路径
        2. 遍历路径上的每条边
        3. 从原始MultiGraph（不是压平后的）中提取所有可能的连接列
        4. 过滤掉权重过大的（不可靠的）
        """
        valid_tables = [t for t in tables if self.search_graph.has_node(t)]

        if len(valid_tables) < 2:
            return []

        # 限制表数量，避免计算过大
        if len(valid_tables) > 5:
            valid_tables = valid_tables[:5]

        needed_keys = set()

        try:
            # 第一步：找出连接路径
            if len(valid_tables) == 2:
                path_nodes = nx.shortest_path(
                    self.search_graph,
                    source=valid_tables[0],
                    target=valid_tables[1],
                    weight='weight'
                )
                edges = zip(path_nodes[:-1], path_nodes[1:])
            else:
                subtree = nx.approximation.steiner_tree(
                    self.search_graph,
                    terminal_nodes=valid_tables,
                    weight='weight'
                )
                edges = nx.dfs_edges(subtree)

            # 第二步：遍历路径上的每条边
            import re

            for u, v in edges:
                # 关键：从原始MultiGraph中获取所有边
                # 因为两个表之间可能有多种连接方式
                if self.raw_graph.has_edge(u, v):
                    all_edges = self.raw_graph[u][v]  # 字典：{key: edge_data}

                    for key, data in all_edges.items():
                        # 过滤：只保留权重较小的边（可靠的）
                        # 权重 > 1.8 的通常是不太可靠的同名匹配
                        if data.get('weight', 999) > 1.8:
                            continue

                        # 提取列名
                        if 'on' in data:
                            # 从 "users.user_id = orders.user_id" 中提取列名
                            matches = re.findall(
                                r'\b([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)\b',
                                data['on']
                            )

                            for table, column in matches:
                                needed_keys.add(f"{table}.{column}")

        except Exception as e:
            logger.error(f"❌ [路径搜索] 提取连接列失败: {e}")
            return []

        return list(needed_keys)

    def get_all_possible_joins(self, tables: List[str]) -> List[str]:
        """
        获取表之间所有可能的直接连接

        用途：
        暴力模式，找出所有可能的JOIN条件

        与 find_path 的区别：
        - find_path: 只返回最优路径（例如2-3条JOIN）
        - get_all_possible_joins: 返回所有可能的JOIN（可能10+条）

        适用场景：
        当不确定需要哪些JOIN时，先全部列出来
        """
        valid_tables = [t for t in tables if self.search_graph.has_node(t)]

        all_joins = []

        # 遍历所有表的两两组合
        for i in range(len(valid_tables)):
            for j in range(i + 1, len(valid_tables)):
                u, v = valid_tables[i], valid_tables[j]

                # 如果两表之间有直接连接
                if self.search_graph.has_edge(u, v):
                    edge_data = self.search_graph.get_edge_data(u, v)
                    if edge_data and 'on' in edge_data:
                        all_joins.append(edge_data['on'])

        return all_joins