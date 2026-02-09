import time
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn


# 1. 引入 RAG 相关的单例 (Core 层)
try:
    from app.core.rag_store import rag_store
    from app.core.embedding import embedder  # ✅ 新增
    from app.core.reranker import reranker
    # 假设你的 retrieve_router 在这里
    from app.api.v1.retrieve_tables import router as retrieve_router

    HAS_RETRIEVE = True
except ImportError as e:
    print(f"⚠️ RAG Import Warning: {e}")
    HAS_RETRIEVE = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n🔥 [Startup] System is warming up...")
    t0 = time.perf_counter()

    if HAS_RETRIEVE:
        try:
            print("   🛠️  Initializing Milvus connection...")
            # 这一步会触发 Milvus 连接
            # rag_store 是单例，只要访问它，它内部的 __init__ 就会跑
            # 而 rag_store.__init__ 现在会调用 embedder.dimension，进而触发模型加载
            _ = rag_store.schema_col
            print("   ✅ Milvus connected & Schema loaded.")

            # 如果你想显式预热 Embedding (虽然上面一行可能已经触发了)
            print("   🧠 Pre-loading Embedding model...")
            embedder.load_model()

            # 显式预热 Reranker
            print("   ⚖️  Pre-loading Rerank model...")
            reranker._load_model()  # 触发加载

        except Exception as e:
            print(f"   ⚠️ RAG Warmup skipped or failed: {e}")
    else:
        print("   ⏩ RAG module disabled.")

    elapsed = time.perf_counter() - t0
    print(f"✅ [Startup] Ready! Took {elapsed:.2f}s\n")

    yield

    print("🛑 [Shutdown] Cleaning up...")


app = FastAPI(title="dbops-enterprise-copilot", lifespan=lifespan)



if HAS_RETRIEVE:
    app.include_router(retrieve_router, prefix="/api/v1")


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    # 获取环境变量，如果没有设置，默认为 False (关闭热重载以稳定调试)
    is_reload = os.getenv("UVICORN_RELOAD", "False").lower() == "true"
    print(f"🚀 Starting Uvicorn with reload={is_reload}")

    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=is_reload
    )