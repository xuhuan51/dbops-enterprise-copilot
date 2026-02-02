import os
import json
import pickle
import time
from typing import List, Dict, Optional

from app.core.config import settings
from app.core.logger import logger

# 🔥🔥🔥 修改这里：导入你的 embedder 单例，而不是函数
from app.core.embedding import embedder

from .builder import SchemaGraphBuilder
from .searcher import SchemaPathFinder


class SchemaGraphService:
    """
    Schema Graph 单例服务 (Service Layer)
    """
    _instance = None
    _searchers: Dict[str, SchemaPathFinder] = {}
    _is_loaded = False

    def __new__(cls):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load_graph(self, catalog_path: str = None):
        if self._is_loaded:
            return

        # 1. 动态确定 catalog_path
        if not catalog_path:
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
        # ⚠️ 如果刚才因为没模型导致缓存是残缺的，这里最好手动删一次缓存文件
        if os.path.exists(cache_path):
            json_mtime = os.path.getmtime(catalog_path)
            pkl_mtime = os.path.getmtime(cache_path)

            if pkl_mtime > json_mtime:
                try:
                    logger.info(f"🚀 [GraphService] Loading graph from cache: {cache_path}")
                    t0 = time.time()
                    with open(cache_path, 'rb') as f:
                        raw_graphs = pickle.load(f)

                    self._init_searchers(raw_graphs)
                    logger.info(f"✅ Graph loaded from cache in {time.time() - t0:.2f}s")
                    self._is_loaded = True
                    return
                except Exception as e:
                    logger.warning(f"⚠️ Cache load failed (will rebuild): {e}")
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

            # ✅ 2. 显式加载模型 (触发你的 Warmup 逻辑)
            # 虽然 embedder.encode() 会自动加载，但这里显式调用可以让日志更好看
            logger.info("🔌 [GraphService] Warming up embedding model...")
            embedder.load_model()

            # ✅ 3. 注入 embedder 到 Builder
            # 注意：GraphBuilder 内部会调用 encoder.encode()，你的 embedder 刚好有这个方法，完美兼容！
            builder = SchemaGraphBuilder(catalog_data, encoder=embedder)
            raw_graphs = builder.build_all()

            # 4. 初始化 Searchers
            self._init_searchers(raw_graphs)

            # 5. 保存缓存
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
        count = 0
        for db_id, mg in raw_graphs.items():
            self._searchers[db_id] = SchemaPathFinder(mg)
            count += 1
        return count

    def search_join_path(self, db_id: str, tables: List[str]) -> List[str]:
        if not self._is_loaded:
            self.load_graph()
        if not db_id or not tables:
            return []
        finder = self._searchers.get(db_id)
        if not finder:
            logger.warning(f"⚠️ [GraphService] No graph found for db: {db_id}")
            return []
        return finder.find_path(tables)

    def reload(self):
        self._is_loaded = False
        self._searchers.clear()
        self.load_graph()

graph_service = SchemaGraphService()