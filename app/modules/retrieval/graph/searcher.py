import networkx as nx
from typing import List, Dict, Any, Tuple
from app.core.logger import logger


class SchemaPathFinder:
    """
    负责在 MultiGraph 上执行路径搜索算法 (Shortest Path / Steiner Tree)。

    关键逻辑：
    由于 Steiner Tree 算法不支持 MultiGraph，我们需要维护一个 'flattened' 视图 (search_graph)，
    在两点之间的多条边中，只保留 Priority 最高 (数值最小) 且 Weight 最小的那条用于寻路。
    """

    def __init__(self, raw_multigraph: nx.MultiGraph):
        self.raw_graph = raw_multigraph
        # 预计算用于搜索的简单图 (无向、单边)
        self.search_graph = self._flatten_graph(raw_multigraph)

    def _flatten_graph(self, MG: nx.MultiGraph) -> nx.Graph:
        """
        降维打击：将 MultiGraph 压扁为 Graph。
        规则：如果 u,v 之间有多条边，选 (priority, weight) 最小的那条。
        """
        G = nx.Graph()

        # 遍历所有节点对之间的所有边
        # edges(keys=True, data=True) -> (u, v, key, data)
        for u, v, key, data in MG.edges(keys=True, data=True):
            # 获取新边的指标
            new_p = data.get('priority', 99)
            new_w = data.get('weight', 99.0)

            if G.has_edge(u, v):
                # 如果已存在边，比较优劣
                curr_data = G[u][v]
                curr_p = curr_data.get('priority', 99)
                curr_w = curr_data.get('weight', 99.0)

                # 核心 PK 逻辑：先比 Priority (小优)，再比 Weight (小优)
                if (new_p < curr_p) or (new_p == curr_p and new_w < curr_w):
                    # 新边胜出，覆盖旧边
                    G.add_edge(u, v, **data)
            else:
                # 没边，直接加
                G.add_edge(u, v, **data)

        return G

    def find_path(self, tables: List[str]) -> List[str]:
        """
        输入表名列表，返回最佳 JOIN 路径的 ON 子句列表。
        """
        # 1. 过滤无效节点
        valid_tables = [t for t in tables if self.search_graph.has_node(t)]

        # 如果只剩 0 或 1 张表，没法连
        if len(valid_tables) < 2:
            return []

        try:
            # === 场景 A: 两张表 -> 最短路径 ===
            if len(valid_tables) == 2:
                source, target = valid_tables[0], valid_tables[1]
                # 使用 weight 属性寻找最短路径
                path_nodes = nx.shortest_path(self.search_graph, source=source, target=target, weight='weight')
                return self._nodes_to_joins(path_nodes)

            # === 场景 B: 三张表及以上 -> 斯坦纳树 (Steiner Tree) ===
            # 寻找连接所有目标节点的最小权重子图
            # 注意: steiner_tree 返回的是一个 nx.Graph (子图)
            subtree = nx.approximation.steiner_tree(
                self.search_graph,
                terminal_nodes=valid_tables,
                weight='weight'
            )

            # 将树结构转化为一系列 JOIN 子句
            # 为了让 SQL 看起来自然，我们可以用 DFS 遍历一遍树的边
            edges = list(nx.dfs_edges(subtree, source=valid_tables[0]))

            joins = []
            for u, v in edges:
                # 从 search_graph (也就是 best edges) 中获取边数据
                edge_data = self.search_graph.get_edge_data(u, v)
                if edge_data:
                    joins.append(edge_data['on'])
            return joins

        except nx.NetworkXNoPath:
            # 图不连通
            logger.debug(f"⚠️ [GraphSearch] No path found between {valid_tables}")
            return []
        except Exception as e:
            logger.error(f"❌ [GraphSearch] Algorithm error: {e}")
            return []

    def _nodes_to_joins(self, path_nodes: List[str]) -> List[str]:
        """将节点序列 [A, B, C] 转换为 [A.x=B.x, B.y=C.y]"""
        joins = []
        for i in range(len(path_nodes) - 1):
            u, v = path_nodes[i], path_nodes[i + 1]
            edge_data = self.search_graph.get_edge_data(u, v)
            if edge_data:
                joins.append(edge_data['on'])
        return joins

    def get_all_direct_join_keys(self, tables: List[str]) -> List[str]:
        """
        暴力版：只要这几个表之间有直接连线，就把连线的列都拿出来。
        """
        needed_keys = set()
        valid_tables = [t for t in tables if self.search_graph.has_node(t)]

        # 遍历所有两两组合
        for i in range(len(valid_tables)):
            for j in range(i + 1, len(valid_tables)):
                u, v = valid_tables[i], valid_tables[j]

                # 检查是否有边
                if self.search_graph.has_edge(u, v):
                    edge_data = self.search_graph.get_edge_data(u, v)
                    # 从 edge_data['on'] 提取列名
                    import re
                    if edge_data and 'on' in edge_data:
                        matches = re.findall(r'([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)', edge_data['on'])
                        for tbl, col in matches:
                            needed_keys.add(f"{tbl}.{col}")

        return list(needed_keys)

    def get_shortest_join_keys(self, tables: List[str]) -> List[str]:
        """
        改进版：不仅找路径，还会把路径上节点之间所有可能的连接键都找出来 (Recall 模式)。
        """
        valid_tables = [t for t in tables if self.search_graph.has_node(t)]
        if len(valid_tables) < 2: return []
        if len(valid_tables) > 5: valid_tables = valid_tables[:5]

        needed_keys = set()

        try:
            # 1. 先用“压扁”的图算出最佳路径（决定谁跟谁连）
            edges = []
            if len(valid_tables) == 2:
                path_nodes = nx.shortest_path(self.search_graph, source=valid_tables[0], target=valid_tables[1],
                                              weight='weight')
                edges = zip(path_nodes[:-1], path_nodes[1:])
            else:
                subtree = nx.approximation.steiner_tree(self.search_graph, terminal_nodes=valid_tables, weight='weight')
                edges = nx.dfs_edges(subtree)

            import re

            # 2. 遍历路径上的每一跳 (u -> v)
            for u, v in edges:
                # 🔥🔥🔥 关键修改：去原始 MultiGraph 里找所有边，而不是去 search_graph 找一条边
                # self.raw_graph[u][v] 是一个字典，包含这两个节点之间的所有边（key 是边的 ID）
                if self.raw_graph.has_edge(u, v):
                    all_edges = self.raw_graph[u][v]

                    for key, data in all_edges.items():
                        # 🛡️ 过滤一下：如果权重太大（比如 > 1.8），说明是那种很不靠谱的关联，还是过滤掉比较好
                        # 这样既保留了 CDSCode (0.4) 也保留了 County (1.5)，但过滤掉了纯同名垃圾 (2.0+)
                        if data.get('weight', 99) > 1.8:
                            continue

                        if 'on' in data:
                            # 提取列名
                            matches = re.findall(r'\b([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)\b', data['on'])
                            for tbl, col in matches:
                                needed_keys.add(f"{tbl}.{col}")

        except Exception as e:
            logger.error(f"❌ [GraphSearch] Error extracting keys: {e}")
            return []

        return list(needed_keys)