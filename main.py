import time
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app.api.v1.agent import router

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("main")

# ==========================================
# 2. Core 组件引入 (带容错处理)
# ==========================================
HAS_RAG = False
try:
    from app.core.rag_store import rag_store
    from app.core.embedding import embedder
    from app.core.reranker import reranker

    HAS_RAG = True
except ImportError as e:
    logger.warning(f"⚠️ RAG Core components import failed: {e}")
    logger.warning("⚠️ System will run in degraded mode (No RAG support).")


# ==========================================
# 3. 生命周期管理 & 模型预热 (Lifespan)
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    🔥 系统启动预热逻辑
    """
    logger.info("🔥 [Startup] System warming up...")
    start_time = time.perf_counter()

    if HAS_RAG:
        try:
            # --- A. 激活 Milvus 连接 ---
            logger.info("   🛠️  Checking Milvus connection...")
            if hasattr(rag_store, "schema_col"):
                _ = rag_store.schema_col
            logger.info("      ✅ Milvus connected.")

            # --- B. 预热 Embedding 模型 ---
            logger.info("   🧠 Warming up Embedding Model...")
            _ = embedder.encode(["warmup_query"])
            logger.info("      ✅ Embedding Model warmed up.")

            # --- C. 预热 Reranker 模型 ---
            logger.info("   ⚖️  Warming up Reranker Model...")
            try:
                if hasattr(reranker, "compute_score"):
                    _ = reranker.compute_score([["warmup_query", "warmup_doc"]])
                elif hasattr(reranker, "predict"):
                    _ = reranker.predict([["warmup_query", "warmup_doc"]])
                logger.info("      ✅ Reranker Model warmed up.")
            except Exception:
                pass

        except Exception as e:
            logger.error(f"❌ [Startup Error] RAG Warmup failed: {e}")
    else:
        logger.info("   ⏩ RAG module disabled/missing, skipping warmup.")

    elapsed = time.perf_counter() - start_time
    logger.info(f"✅ [Startup] System Ready! Warmup took {elapsed:.2f}s")

    yield

    logger.info("🛑 [Shutdown] Cleaning up resources...")


# ==========================================
# 4. FastAPI App 定义
# ==========================================
app = FastAPI(
    title="DBOps Enterprise Copilot",
    description="Text-to-SQL Agent with RAG & Visualization",
    version="1.0.0",
    lifespan=lifespan
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 5. 🔗 挂载路由 (这就是连接 Agent 的地方)
# ==========================================
# 这里把你的 api.py (api_router) 接到了 /api/v1 路径下
# 最终访问地址: http://localhost:8000/api/v1/query
app.include_router(router, prefix="/api/v1", tags=["Agent Chat"])


@app.get("/health")
def health_check():
    return {"status": "ok", "rag_enabled": HAS_RAG}


# ==========================================
# 6. 启动入口
# ==========================================
if __name__ == "__main__":
    is_reload = os.getenv("UVICORN_RELOAD", "False").lower() == "true"
    print(f"🚀 Starting Uvicorn Server (Reload={is_reload})...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=is_reload
    )