import time
import aiomysql
from contextlib import asynccontextmanager
from fastapi import FastAPI

# 引入路由
from app.api.v1.agent_query import router as agent_router
from app.api.v1.query import router as raw_sql_router
from app.api.v1.analyze import router as analyze_router

# 🔥 引入 Master Graph 的注入函数和配置
from app.core.master_graph import init_master_app, DB_CONFIG

# 引入 RAG 模块 (容错)
try:
    from app.api.v1.retrieve_tables import (
        router as retrieve_router,
        get_embed_model,
        ensure_milvus_connection
    )

    HAS_RETRIEVE = True
except ImportError:
    HAS_RETRIEVE = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n🔥 [Startup] System is warming up...")
    t0 = time.perf_counter()

    # ===========================
    # 1. 初始化 MySQL 记忆连接池
    # ===========================
    print("   🔌 Connecting to MySQL Memory...")
    # 创建全局连接池
    pool = await aiomysql.create_pool(**DB_CONFIG)

    # 🔥 关键：把池子注入给 Graph，让 master_app 拥有记忆
    init_master_app(pool)

    print("   ✅ MySQL Memory Connected.")

    # ===========================
    # 2. 初始化 RAG 资源
    # ===========================
    if HAS_RETRIEVE:
        try:
            if ensure_milvus_connection():
                print("   ✅ Milvus connection established.")
            else:
                print("   ⚠️ Milvus connection failed.")

            print("   ↳ Loading Embedding model...")
            get_embed_model().encode(["warmup"], normalize_embeddings=True)
        except Exception as e:
            print(f"   ⚠️ RAG Warmup skipped: {e}")

    elapsed = time.perf_counter() - t0
    print(f"✅ [Startup] Ready! Took {elapsed:.2f}s\n")

    yield

    # ===========================
    # 3. 关闭资源
    # ===========================
    print("🛑 [Shutdown] Closing MySQL pool...")
    pool.close()
    await pool.wait_closed()


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
    import uvicorn
    import os

    # 获取环境变量，如果没有设置，默认为 False (关闭热重载)
    # 这样只有你在开发时显式开启才会有 reload，跑测试脚本时更稳定
    is_reload = os.getenv("UVICORN_RELOAD", "True").lower() == "true"

    print(f"🚀 Starting Uvicorn with reload={is_reload}")

    # 建议 1: 在 Windows 跑这种重型 AI 应用，强烈建议把 reload 设为 False
    # 建议 2: 如果必须要热重载，请确保不要在跑高并发测试脚本时修改代码
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=False  # <--- 🚨 核心修改：这里暂时改为 False
    )