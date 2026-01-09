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
    # "main:app" 对应 文件名:变量名
    # reload=True 方便你改代码后自动重启
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)