
import os
import pickle
import time
import networkx as nx
from typing import List, Dict, Optional
import json
from app.core.config import settings
from app.core.logger import logger

# 导入你的 embedder 单例
from app.core.embedding import embedder

from .builder import SchemaGraphBuilder
from .searcher import SchemaPathFinder


class SchemaGraphService:
    """
    Schema Graph 单例服务 (Service Layer)
    """
    _instance = None

    # 显式定义，防止 AttributeError
    graphs: Dict[str, nx.MultiGraph] = {}
    _searchers: Dict[str, SchemaPathFinder] = {}
    _is_loaded = False

    def __new__(cls):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            # 确保初始化时 graphs 存在（即使为空）
            cls._instance.graphs = {}
            cls._instance._searchers = {}
            cls._instance._is_loaded = False
        return cls._instance

    def load_graph(self, catalog_path: str = None):
        """加载图数据 (缓存优先 -> 构建)"""
        # 如果已经加载且不为空，直接返回
        if self._is_loaded and self.graphs:
            return

        # 1. 动态确定 catalog_path
        if not catalog_path:
            # 兼容你的路径逻辑
            current_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(current_dir, "../../../.."))
            catalog_path = os.path.join(project_root, "data/bird/metadata/schema_catalog.json")

        if not os.path.exists(catalog_path):
            logger.warning(f"⚠️ [GraphService] Catalog not found: {catalog_path}")
            return

        cache_path = os.path.join(os.path.dirname(catalog_path), "graph_cache.pkl")

        # ==========================================
        # 🔥 策略 A: 尝试加载缓存
        # ==========================================
        if os.path.exists(cache_path):
            json_mtime = os.path.getmtime(catalog_path)
            pkl_mtime = os.path.getmtime(cache_path)

            if pkl_mtime > json_mtime:
                try:
                    logger.info(f"🚀 [GraphService] Loading graph from cache: {cache_path}")
                    t0 = time.time()
                    with open(cache_path, 'rb') as f:
                        raw_graphs = pickle.load(f)

                    # 🔥 核心修复：必须把加载的数据赋值给 self.graphs
                    self.graphs = raw_graphs
                    self._init_searchers(raw_graphs)

                    logger.info(f"✅ Graph loaded from cache in {time.time() - t0:.2f}s")
                    self._is_loaded = True
                    return
                except Exception as e:
                    logger.warning(f"⚠️ Cache load failed (will rebuild): {e}")
                    # 加载失败，重置为空，继续往下走构建流程
                    self.graphs = {}
            else:
                logger.info("🔄 [GraphService] Cache is outdated. Rebuilding...")

        # ==========================================
        # 🔥 策略 B: 重新计算 (构建模式)
        # ==========================================
        try:
            logger.info(f"🏗️ [GraphService] Building graphs from source: {catalog_path}...")
            t0 = time.time()

            with open(catalog_path, 'r', encoding='utf-8') as f:
                catalog_data = json.load(f)

            # 显式加载模型
            logger.info("🔌 [GraphService] Warming up embedding model...")
            embedder.load_model()

            # 构建图
            builder = SchemaGraphBuilder(catalog_data, encoder=embedder)
            raw_graphs = builder.build_all()

            # 🔥 核心修复：赋值给 self.graphs
            self.graphs = raw_graphs

            # 初始化 Searchers
            self._init_searchers(raw_graphs)

            # 保存缓存
            try:
                logger.info(f"💾 [GraphService] Saving cache to {cache_path}...")
                with open(cache_path, 'wb') as f:
                    pickle.dump(raw_graphs, f)
            except Exception as e:
                logger.warning(f"⚠️ Failed to save graph cache: {e}")

            logger.info(f"✅ [GraphService] Initialized for {len(raw_graphs)} DBs in {time.time() - t0:.2f}s")
            self._is_loaded = True

        except Exception as e:
            logger.error(f"❌ [GraphService] Init failed: {e}", exc_info=True)
            self.graphs = {}  # 兜底

    def _init_searchers(self, raw_graphs: Dict):
        """初始化路径搜索器"""
        count = 0
        self._searchers = {}  # 清空旧的
        for db_id, mg in raw_graphs.items():
            self._searchers[db_id] = SchemaPathFinder(mg)
            count += 1
        return count

    def get_graph(self, db_id: str) -> nx.MultiGraph:
        """获取指定 DB 的原始图对象 (供 schema_helper 使用)"""
        if not self._is_loaded:
            self.load_graph()
        return self.graphs.get(db_id)

    def search_join_path(self, db_id: str, tables: List[str]) -> List[str]:
        """查找 Join 路径 SQL 片段 (供 Generator 参考)"""
        if not self._is_loaded:
            self.load_graph()

        # 兼容性处理：如果 tables 为空，直接返回
        if not tables: return []

        finder = self._searchers.get(db_id)
        if finder:
            return finder.find_path(tables)
        return []

    # 🔥🔥🔥 新增这个方法供 schema_helper 调用 🔥🔥🔥
    def get_shortest_join_keys(self, db_id: str, tables: List[str]) -> List[str]:
        """
        获取连接这些表所需的关键列名 (Primary Keys / Foreign Keys)
        用于 RAG 检索后的 Schema 补全。
        """
        if not self._is_loaded:
            self.load_graph()

        if not tables: return []

        finder = self._searchers.get(db_id)
        if finder:
            # 调用我们在 Searcher 里刚写的新方法
            return finder.get_shortest_join_keys(tables)
        return []

    def reload(self):
        """强制重载"""
        self._is_loaded = False
        self.graphs.clear()
        self._searchers.clear()
        self.load_graph()


graph_service = SchemaGraphService()