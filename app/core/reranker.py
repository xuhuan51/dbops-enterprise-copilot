import torch
from sentence_transformers import CrossEncoder
from app.core.config import settings
from app.core.logger import logger


class RerankService:
    """
    Rerank 模型单例服务
    负责对召回的候选结果进行二次精排 (Re-ranking)
    """
    _instance = None
    model = None

    def __new__(cls):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._load_model()
        return cls._instance

    def _load_model(self):
        """加载模型 (只执行一次)"""
        model_name = settings.RERANK_MODEL  # 从 config 读取，例如 "BAAI/bge-reranker-v2-m3"
        logger.info(f"⚖️ [Reranker] Loading model: {model_name} ...")

        try:
            # 优先尝试使用 FP16 (半精度) 加载，节省显存并加速 (仅 GPU/MPS 有效)
            # 如果是 CPU，torch_dtype=float16 可能会报错或变慢，sentence-transformers 通常会自动处理
            # 这里我们显式指定 trust_remote_code=True 以支持新模型架构
            device = "cuda" if torch.cuda.is_available() else "cpu"

            self.model = CrossEncoder(
                model_name,
                device=device,
                trust_remote_code=True,
                automodel_args={"torch_dtype": torch.float16} if device == "cuda" else {}
            )
            logger.info(f"✅ [Reranker] Model loaded successfully on {device.upper()}.")
        except Exception as e:
            logger.warning(f"⚠️ [Reranker] FP16 load failed or error occurred: {e}")
            logger.info("🔄 [Reranker] Falling back to default precision...")
            # 降级重试
            try:
                self.model = CrossEncoder(model_name, trust_remote_code=True)
                logger.info("✅ [Reranker] Model loaded (Default Precision).")
            except Exception as e2:
                logger.error(f"❌ [Reranker] Fatal error loading model: {e2}")
                raise e2

    def rerank(self, query: str, candidates: list, top_k: int = 10):
        """
        执行重排序
        :param query: 用户问题
        :param candidates: 候选列表，每个元素必须包含 'content' 字段 (即用于打分的文本)
                           示例: [{"content": "...", "entity": ...}, ...]
        :param top_k: 返回前 K 个
        :return: 排序并截断后的列表
        """
        if not candidates:
            return []

        # 1. 构造模型输入 Pair: [[Query, Doc1], [Query, Doc2], ...]
        pairs = []
        valid_indices = []  # 记录有效数据的索引，防止 candidates 里有坏数据

        for i, item in enumerate(candidates):
            content = item.get("content")
            if content and isinstance(content, str):
                pairs.append([query, content])
                valid_indices.append(i)

        if not pairs:
            return []

        # 2. 模型预测打分
        try:
            # batch_size=32 是经验值，显存小可以调小
            scores = self.model.predict(pairs, batch_size=32, show_progress_bar=False)
        except Exception as e:
            logger.error(f"❌ [Reranker] Prediction failed: {e}")
            return candidates[:top_k]  # 失败则原样返回

        # 3. 将分数回填给对象
        # scores 是一个 numpy 数组或 list
        for i, original_idx in enumerate(valid_indices):
            # 将分数转为标准 float
            score_val = float(scores[i])
            candidates[original_idx]['rerank_score'] = score_val

        # 4. 按分数降序排列
        # 注意：如果有 invalid 的数据（没参与打分），它们的 rerank_score 可能是 None，这里过滤掉或设为 -999
        results = [c for c in candidates if 'rerank_score' in c]
        sorted_results = sorted(results, key=lambda x: x['rerank_score'], reverse=True)

        return sorted_results[:top_k]


# 导出全局单例
reranker = RerankService()