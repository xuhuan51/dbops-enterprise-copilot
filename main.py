import time
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn

# 1. 引入路由
from app.api.v1.agent_query import router as agent_router
from app.api.v1.query import router as raw_sql_router
from app.api.v1.analyze import router as analyze_router

# 2. 引入数据库基础设施 (用于优雅关闭)
# 假设你的 infrastructure.db.mysql 里有一个 close_pool 或类似的清理函数
# 如果没有，暂时注释掉 shutdown 里的清理逻辑也没关系
try:
    from app.infrastructure.db.mysql import close_pool
except ImportError:
    close_pool = None

# 3. 引入 RAG 模块 (路径已修正)
HAS_RETRIEVE = False
try:
    # 刚才我们在 schema_retriever.py 里确认过这些函数
    from app.modules.retrieval.schema_retriever import (
        get_embed_model,
        ensure_milvus_connection
    )
    # 这个 router 应该还在 api 层
    from app.api.v1.retrieve_tables import router as retrieve_router
    HAS_RETRIEVE = True
except ImportError as e:
    print(f"⚠️ RAG Import Warning: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n🔥 [Startup] System is warming up...")
    t0 = time.perf_counter()

    # ===========================
    # 1. 数据库连接池无需在此初始化
    # ===========================
    # 新架构下，Executor 会在第一次调用时通过 infrastructure 自动获取连接池。
    # 我们这里可以什么都不做，或者简单打印一下。
    print("   🔌 Database: Lazy connection mode (Managed by Infrastructure).")

    # ===========================
    # 2. 初始化 RAG 资源
    # ===========================
    if HAS_RETRIEVE:
        try:
            print("   🛠️ Checking Milvus connection...")
            if ensure_milvus_connection():
                print("   ✅ Milvus connected.")
            else:
                print("   ⚠️ Milvus connection failed (Soft fail).")

            print("   🧠 Loading Embedding model...")
            # 预加载模型，避免第一次请求卡顿
            get_embed_model()
        except Exception as e:
            print(f"   ⚠️ RAG Warmup skipped: {e}")
    else:
        print("   ⏩ RAG module disabled or missing.")

    elapsed = time.perf_counter() - t0
    print(f"✅ [Startup] Ready! Took {elapsed:.2f}s\n")

    yield

    # ===========================
    # 3. 关闭资源
    # ===========================
    print("🛑 [Shutdown] Cleaning up...")
    if close_pool:
        await close_pool()
        print("   ✅ Database pool closed.")


app = FastAPI(title="dbops-enterprise-copilot", lifespan=lifespan)

# 注册路由
app.include_router(agent_router, prefix="/api/v1")
app.include_router(raw_sql_router, prefix="/api/v1")
app.include_router(analyze_router, prefix="/api/v1")

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