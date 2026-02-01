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