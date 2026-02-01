import os
import json
import pickle
import time
from typing import List, Dict, Optional

from app.core.config import settings
from app.core.logger import logger
from .builder import SchemaGraphBuilder
from .searcher import SchemaPathFinder


class SchemaGraphService:
    """
    Schema Graph 单例服务 (Service Layer)

    功能：
    1. 管理所有数据库的图谱 (MultiGraph)。
    2. 提供持久化缓存 (Pickle) 以加速启动。
    3. 路由请求到对应的 Searcher 进行寻路。
    """
    _instance = None

    # 存储每个库的寻路器: {db_id: SchemaPathFinder}
    _searchers: Dict[str, SchemaPathFinder] = {}
    _is_loaded = False

    def __new__(cls):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load_graph(self, catalog_path: str = None):
        """
        初始化入口：加载数据 -> 构建/读取缓存 -> 初始化 Searchers
        """
        if self._is_loaded:
            return

        # 1. 动态确定 catalog_path (不强依赖 settings.PROJECT_ROOT)
        if not catalog_path:
            # 获取当前文件所在目录
            current_dir = os.path.dirname(os.path.abspath(__file__))
            # 回溯到项目根目录
            project_root = os.path.abspath(os.path.join(current_dir, "../../../.."))
            catalog_path = os.path.join(project_root, "data/bird/metadata/schema_catalog.json")

        if not os.path.exists(catalog_path):
            logger.warning(f"⚠️ [GraphService] Catalog not found: {catalog_path}")
            return

        # 定义缓存文件路径 (同目录下)
        cache_path = os.path.join(os.path.dirname(catalog_path), "graph_cache.pkl")

        # ==========================================
        # 🔥 策略 A: 尝试加载缓存 (极速模式)
        # ==========================================
        if os.path.exists(cache_path):
            # 检查文件修改时间：如果 JSON 更新了，缓存必须失效
            json_mtime = os.path.getmtime(catalog_path)
            pkl_mtime = os.path.getmtime(cache_path)

            if pkl_mtime > json_mtime:
                try:
                    logger.info(f"🚀 [GraphService] Loading graph from cache: {cache_path}")
                    t0 = time.time()
                    with open(cache_path, 'rb') as f:
                        # 直接加载构建好的 MultiGraphs
                        raw_graphs = pickle.load(f)

                    # 恢复 Searchers
                    self._init_searchers(raw_graphs)

                    logger.info(f"✅ Graph loaded from cache in {time.time() - t0:.2f}s")
                    self._is_loaded = True
                    return
                except Exception as e:
                    logger.warning(f"⚠️ Cache load failed (will rebuild): {e}")
            else:
                logger.info("🔄 [GraphService] Cache is outdated (JSON changed). Rebuilding...")

        # ==========================================
        # 🔥 策略 B: 重新计算 (构建模式)
        # ==========================================
        try:
            logger.info(f"🏗️ [GraphService] Building graphs from source: {catalog_path}...")
            t0 = time.time()

            with open(catalog_path, 'r', encoding='utf-8') as f:
                catalog_data = json.load(f)

            # 1. 调用 V3 Builder 计算所有边
            builder = SchemaGraphBuilder(catalog_data)
            raw_graphs = builder.build_all()

            # 2. 初始化 Searchers
            self._init_searchers(raw_graphs)

            # 3. 保存缓存 (Pickle)
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

    def _init_searchers(self, raw_graphs: Dict):
        """将 MultiGraph 转换为 Searcher (降维 + 索引)"""
        count = 0
        for db_id, mg in raw_graphs.items():
            self._searchers[db_id] = SchemaPathFinder(mg)
            count += 1
        return count

    def search_join_path(self, db_id: str, tables: List[str]) -> List[str]:
        """
        核心 API：根据表名寻找 JOIN 路径
        """
        if not self._is_loaded:
            # 懒加载尝试
            self.load_graph()

        if not db_id or not tables:
            return []

        # 1. 找到对应库的 searcher
        finder = self._searchers.get(db_id)
        if not finder:
            # 简单的防错：有时候 db_id 可能大小写不一致
            logger.warning(f"⚠️ [GraphService] No graph found for db: {db_id}")
            return []

        # 2. 执行搜索
        return finder.find_path(tables)

    def reload(self):
        """强制重载 (用于开发调试或文件更新后)"""
        logger.info("🔄 [GraphService] Reloading...")
        self._is_loaded = False
        self._searchers.clear()
        self.load_graph()


# 单例导出
graph_service = SchemaGraphService()