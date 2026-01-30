# app/infrastructure/db/mysql.py

import asyncio
import aiomysql
import pymysql
from pymysql.cursors import DictCursor
from aiomysql import DictCursor as AsyncDictCursor
from app.core.config import settings
from app.core.logger import logger

# ========================================================
# 全局变量 (Singleton)
# ========================================================
_async_pool = None


# ========================================================
# 1. 异步连接池 (生产环境核心 - 给 Agent/API 用)
# ========================================================
async def get_async_pool():
    """
    获取全局异步连接池。
    基于 aiomysql，完美适配 FastAPI 的 async 机制。
    """
    global _async_pool

    if _async_pool is None:
        try:
            logger.info(f"🔌 [Infra] Init Async MySQL Pool -> {settings.MYSQL_HOST}:{settings.MYSQL_PORT}")

            _async_pool = await aiomysql.create_pool(
                host=settings.MYSQL_HOST,
                port=settings.MYSQL_PORT,
                user=settings.MYSQL_USER,
                password=settings.MYSQL_PASSWORD,
                db=settings.MYSQL_CONNECT_DB,

                # 关键配置
                charset="utf8mb4",
                autocommit=True,
                cursorclass=AsyncDictCursor,  # 让结果变成字典 {"id": 1}

                # 连接池大小控制
                minsize=int(getattr(settings, "DB_POOL_MIN", 2)),
                maxsize=int(getattr(settings, "DB_POOL_MAX", 20)),

                # 自动回收空闲连接 (防止 MySQL 8小时断开)
                pool_recycle=3600,
                loop=asyncio.get_running_loop()
            )
        except Exception as e:
            logger.error(f"❌ [Infra] Failed to create async pool: {e}")
            raise e

    return _async_pool


async def close_pool():
    """系统关闭时调用"""
    global _async_pool
    if _async_pool:
        _async_pool.close()
        await _async_pool.wait_closed()
        logger.info("🛑 [Infra] Async Pool Closed.")


# ========================================================
# 2. 同步连接工厂 (给脚本/元数据获取用)
# ========================================================
def get_sync_connection():
    """
    获取一个标准的 pymysql 同步连接。
    用于 fetch_table_metadata 或其他不适合 async 的场景。
    """
    return pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_CONNECT_DB,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=10
    )