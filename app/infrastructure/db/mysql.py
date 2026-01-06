from contextlib import contextmanager
import pymysql
# 引入连接池模块
from dbutils.pooled_db import PooledDB
from app.core.config import settings

# ========================================================
# 初始化全局连接池 (Singleton Pattern)
# ========================================================
POOL = PooledDB(
    creator=pymysql,  # 使用 pymysql 库
    maxconnections=50,  # 连接池允许的最大连接数 (根据机器配置调整，例如 20-100)
    mincached=5,  # 初始化时至少创建的空闲连接
    maxcached=10,  # 连接池中最多闲置的连接
    maxshared=0,  # 0 表示所有连接都不共享 (推荐)
    blocking=True,  # 连接池满了是否阻塞等待 (True=等待, False=报错)

    # 以下是透传给 pymysql.connect 的参数
    host=settings.MYSQL_HOST,
    port=settings.MYSQL_PORT,
    user=settings.MYSQL_USER,
    password=settings.MYSQL_PASSWORD,
    database=settings.MYSQL_CONNECT_DB,
    charset="utf8mb4",
    autocommit=True,

    # 超时设置 (秒)
    # 注意：pymysql 的 connect_timeout 是建立连接的超时，不是查询超时
    # 查询超时(read_timeout) 我们通常在 executor 里通过 SET SESSION MAX_EXECUTION_TIME 控制更精准
    connect_timeout=10,
)


@contextmanager
def mysql_conn():
    """
    从连接池获取连接，使用完毕后归还（而不是断开）。

    Usage:
        with mysql_conn() as conn:
            cur = conn.cursor()
            cur.execute(...)
    """
    # 1. 从池子里“借”一个连接
    conn = POOL.connection()
    try:
        yield conn
    except Exception as e:
        # 如果发生异常，通常由调用方处理，但这里确保连接能归还
        # 有些严格的实现会在这里 conn.rollback()
        raise e
    finally:
        # 2. 用完“还”回池子
        # 注意：在 PooledDB 中，.close() 并不是关闭 TCP 连接，而是重置状态并放回池中
        conn.close()