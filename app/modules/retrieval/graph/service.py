"""
表关系图服务 (MySQL版本)
======================

这个文件是干什么的？
------------------
这是一个服务层（Service Layer），负责管理整个表关系图系统。

它把 builder.py 和 searcher.py 整合在一起，提供简单的API。

核心功能：
---------
1. 从MySQL数据库加载schema信息
2. 构建表关系图（缓存机制）
3. 提供查询接口（给其他模块用）

使用示例：
---------

>>>
>>> # 自动加载（第一次会构建图，之后从缓存读取）
>>> graph_service.load_graph()
>>>
>>> # 查找JOIN路径
>>> joins = graph_service.find_join_path(['users', 'orders', 'products'])
>>> print(joins)
['users.user_id = orders.user_id', 'orders.product_id = products.product_id']
>>>
>>> # 获取JOIN所需的列
>>> keys = graph_service.get_join_keys(['users', 'orders'])
>>> print(keys)
['users.user_id', 'orders.user_id']

架构设计：
---------
这是一个**单例模式**（Singleton）：
- 整个应用只有一个实例
- 图数据只加载一次
- 避免重复计算

缓存策略：
---------
1. 第一次运行：从MySQL读取 -> 构建图 -> 保存到 graph_cache.pkl
2. 后续运行：检查缓存是否最新 -> 直接加载缓存
3. 如果数据库变化：自动重建图
"""

import os
import pickle
import json
import time
import networkx as nx
from typing import List, Dict
import pymysql

# 导入配置
from app.core.config import settings
from app.core.logger import logger
from app.modules.retrieval.graph.builder import SchemaGraphBuilder
from app.modules.retrieval.graph.searcher import SchemaPathFinder


class SchemaGraphService:
    """
    表关系图服务（单例）

    职责：
    1. 管理图的生命周期（加载、缓存、重建）
    2. 对外提供统一的查询接口
    """

    # 单例模式：类变量
    _instance = None
    _is_initialized = False

    def __new__(cls):
        """
        单例模式实现

        确保整个应用只有一个 SchemaGraphService 实例
        """
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """
        初始化（只会执行一次）
        """
        if self._is_initialized:
            return

        # 图数据
        self.graph: nx.MultiGraph = None
        self.searcher: SchemaPathFinder = None

        # 缓存路径
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
        self.data_dir = os.path.join(project_root, "data")
        self.cache_path = os.path.join(self.data_dir, "graph_cache.pkl")
        self.schema_path = os.path.join(self.data_dir, "ecommerce_schema.json")

        # 确保data目录存在
        os.makedirs(self.data_dir, exist_ok=True)

        self._is_initialized = True
        logger.info("✅ [图服务] 初始化完成")

    def load_graph(self, force_rebuild: bool = False):
        """
        加载表关系图

        参数：
            force_rebuild: 是否强制重建（默认False，优先使用缓存）

        工作流程：
        1. 如果已加载 -> 直接返回
        2. 检查缓存 -> 缓存有效则加载
        3. 否则 -> 从MySQL重建图
        """
        # 如果已经加载，直接返回
        if self.graph is not None and not force_rebuild:
            logger.debug("📊 [图服务] 图已加载，跳过")
            return

        logger.info("🚀 [图服务] 开始加载表关系图...")
        start_time = time.time()

        # 策略A：尝试从缓存加载
        if not force_rebuild and self._try_load_from_cache():
            logger.info(f"✅ [图服务] 从缓存加载成功，耗时 {time.time() - start_time:.2f}秒")
            return

        # 策略B：从MySQL重建
        self._rebuild_from_database()
        logger.info(f"✅ [图服务] 构建完成，耗时 {time.time() - start_time:.2f}秒")

    def _try_load_from_cache(self) -> bool:
        """
        尝试从缓存加载图

        返回：
            True: 加载成功
            False: 加载失败（需要重建）

        缓存有效性检查：
        1. 缓存文件存在
        2. 缓存比schema文件新（没有过期）
        """
        # 检查缓存文件是否存在
        if not os.path.exists(self.cache_path):
            logger.info("📝 [图服务] 缓存不存在，需要重建")
            return False

        # 检查缓存是否过期
        if os.path.exists(self.schema_path):
            schema_time = os.path.getmtime(self.schema_path)
            cache_time = os.path.getmtime(self.cache_path)

            if schema_time > cache_time:
                logger.info("🔄 [图服务] 缓存已过期，需要重建")
                return False

        # 尝试加载
        try:
            logger.info(f"📂 [图服务] 从缓存加载: {self.cache_path}")

            with open(self.cache_path, 'rb') as f:
                self.graph = pickle.load(f)

            # 初始化搜索器
            self.searcher = SchemaPathFinder(self.graph)

            logger.info(f"✅ [图服务] 缓存加载成功: {self.graph.number_of_nodes()} 个表, "
                       f"{self.graph.number_of_edges()} 条关联")
            return True

        except Exception as e:
            logger.warning(f"⚠️ [图服务] 缓存加载失败: {e}，将重建")
            return False

    def _rebuild_from_database(self):
        """
        从MySQL数据库重建图

        步骤：
        1. 连接MySQL，提取schema信息
        2. 调用 SchemaGraphBuilder 构建图
        3. 保存缓存
        """
        logger.info("🏗️ [图服务] 从MySQL重建表关系图...")

        # 第一步：提取schema信息
        schema_data = self._extract_schema_from_mysql()

        if not schema_data:
            logger.error("❌ [图服务] 无法提取schema信息")
            return

        # 第二步：构建图
        try:
            # 这里可以选择是否启用AI语义匹配
            # encoder = embedder  # 如果要用AI
            encoder = None  # 暂时不用AI，加快速度

            builder = SchemaGraphBuilder(schema_data, encoder=encoder)
            self.graph = builder.build_graph()

            # 初始化搜索器
            self.searcher = SchemaPathFinder(self.graph)

        except Exception as e:
            logger.error(f"❌ [图服务] 构建失败: {e}", exc_info=True)
            return

        # 第三步：保存缓存
        try:
            logger.info(f"💾 [图服务] 保存缓存到: {self.cache_path}")

            with open(self.cache_path, 'wb') as f:
                pickle.dump(self.graph, f)

            logger.info("✅ [图服务] 缓存保存成功")

        except Exception as e:
            logger.warning(f"⚠️ [图服务] 缓存保存失败: {e}")

    def _extract_schema_from_mysql(self) -> List[Dict]:
        """
        从MySQL数据库提取schema信息

        返回：
            列信息列表，每个元素包含：
            {
                "table_name": "users",
                "column_name": "user_id",
                "data_type": "BIGINT",
                "is_primary_key": True,
                "is_foreign_key": False,
                "foreign_key_target": None,
                "sample_values": [1, 2, 3, 4, 5],
                "column_comment": "用户ID"
            }
        """
        logger.info(f"🔌 [图服务] 连接MySQL: {settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}")

        try:
            # 连接数据库
            conn = pymysql.connect(
                host=settings.DB_HOST,
                port=settings.DB_PORT,
                user=settings.DB_USER,
                password=settings.DB_PASSWORD,
                database=settings.DB_NAME,
                charset='utf8mb4'
            )
            cursor = conn.cursor()

            # 获取所有表
            cursor.execute("SHOW TABLES")
            tables = [row[0] for row in cursor.fetchall()]

            logger.info(f"📊 [图服务] 发现 {len(tables)} 张表")

            schema_data = []

            # 遍历每张表
            for table in tables:
                # 获取列信息
                cursor.execute(f"""
                    SELECT 
                        COLUMN_NAME,
                        COLUMN_TYPE,
                        COLUMN_KEY,
                        COLUMN_COMMENT
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = '{settings.DB_NAME}'
                    AND TABLE_NAME = '{table}'
                    ORDER BY ORDINAL_POSITION
                """)

                columns = cursor.fetchall()

                for col in columns:
                    col_name = col[0]
                    col_type = col[1]
                    col_key = col[2]
                    col_comment = col[3] or ""

                    # 获取示例数据
                    try:
                        cursor.execute(f"""
                            SELECT DISTINCT `{col_name}`
                            FROM `{table}`
                            WHERE `{col_name}` IS NOT NULL
                            LIMIT 5
                        """)
                        samples = [row[0] for row in cursor.fetchall()]
                    except:
                        samples = []

                    # 组装数据
                    schema_data.append({
                        "table_name": table,
                        "column_name": col_name,
                        "data_type": col_type,
                        "is_primary_key": (col_key == 'PRI'),
                        "is_foreign_key": (col_key == 'MUL'),  # 简化判断
                        "foreign_key_target": None,  # 需要查询外键约束表
                        "sample_values": samples,
                        "column_comment": col_comment
                    })

            conn.close()
            logger.info(f"✅ [图服务] 提取完成: {len(schema_data)} 个列")

            return schema_data

        except Exception as e:
            logger.error(f"❌ [图服务] MySQL连接失败: {e}")
            return []

    # =========================================================================
    # 对外API
    # =========================================================================

    def find_join_path(self, tables: List[str]) -> List[str]:
        """
        查找连接多个表的最优JOIN路径

        参数：
            tables: 表名列表，例如 ['users', 'orders', 'products']

        返回：
            JOIN条件列表，例如：
            [
                'users.user_id = orders.user_id',
                'orders.product_id = products.product_id'
            ]

        使用示例：
        >>> joins = graph_service.find_join_path(['users', 'orders'])
        >>> print(joins)
        ['users.user_id = orders.user_id']
        """
        # 确保图已加载
        if self.graph is None:
            self.load_graph()

        if not tables or self.searcher is None:
            return []

        return self.searcher.find_path(tables)

    def get_join_keys(self, tables: List[str]) -> List[str]:
        """
        获取JOIN所需的关键列

        用途：
        RAG检索后，需要补全schema信息时使用

        参数：
            tables: 表名列表

        返回：
            列名列表（带表前缀），例如：
            [
                'users.user_id',
                'orders.user_id',
                'orders.product_id',
                'products.product_id'
            ]

        使用示例：
        >>> keys = graph_service.get_join_keys(['users', 'orders'])
        >>> # 然后用这些keys去Milvus检索对应的列信息
        """
        # 确保图已加载
        if self.graph is None:
            self.load_graph()

        if not tables or self.searcher is None:
            return []

        return self.searcher.get_join_keys(tables)

    def get_all_tables(self) -> List[str]:
        """
        获取所有表名

        返回：
            表名列表
        """
        if self.graph is None:
            self.load_graph()

        return list(self.graph.nodes()) if self.graph else []

    def get_table_neighbors(self, table: str) -> List[str]:
        """
        获取与指定表直接相关的表

        参数：
            table: 表名

        返回：
            相关表列表

        使用示例：
        >>> neighbors = graph_service.get_table_neighbors('users')
        >>> print(neighbors)
        ['orders', 'addresses', 'favorites']
        """
        if self.graph is None:
            self.load_graph()

        if self.graph and self.graph.has_node(table):
            return list(self.graph.neighbors(table))

        return []

    def reload(self):
        """
        强制重新加载（清除缓存）

        使用场景：
        - 数据库结构发生变化
        - 调试时需要重建图
        """
        logger.info("🔄 [图服务] 强制重新加载...")
        self.graph = None
        self.searcher = None
        self.load_graph(force_rebuild=True)


# ============================================================================
# 导出单例
# ============================================================================

# 创建全局单例
graph_service = SchemaGraphService()
