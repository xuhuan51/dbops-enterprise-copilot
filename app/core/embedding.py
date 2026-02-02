import torch
from typing import Union, List
from sentence_transformers import SentenceTransformer
from app.core.config import settings
from app.core.logger import logger


class EmbeddingService:
    """
    Embedding 模型单例服务
    负责将文本转换为向量 (Vector)
    """
    _instance = None
    model = None
    _dimension = None

    def __new__(cls):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            # 注意：这里不立即加载模型，改为懒加载或显式调用 load_model
            # 但为了方便，我们可以在这里做检查，或者在第一次调用 encode 时自动加载
        return cls._instance

    def load_model(self):
        """显式加载模型 (Warmup)"""
        if self.model is not None:
            return

        model_name = settings.EMBED_MODEL
        logger.info(f"🧠 [Embedding] Loading model: {model_name} ...")

        try:
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self.model = SentenceTransformer(model_name, device=device)

            # 计算维度缓存起来 (Milvus 建表需要)
            # BGE-M3 默认是 1024
            test_vec = self.model.encode("test")
            self._dimension = len(test_vec)

            logger.info(f"✅ [Embedding] Model loaded on {device.upper()}. Dimension: {self._dimension}")
        except Exception as e:
            logger.error(f"❌ [Embedding] Load failed: {e}")
            raise e

    def encode(self, texts: Union[str, List[str]], normalize_embeddings: bool = True) -> List[float]:
        """
        执行向量化
        """
        if self.model is None:
            self.load_model()

        return self.model.encode(texts, normalize_embeddings=normalize_embeddings)

    @property
    def dimension(self):
        """获取向量维度 (Lazy Load)"""
        if self._dimension is None:
            self.load_model()
        return self._dimension


# 导出全局单例
embedder = EmbeddingService()