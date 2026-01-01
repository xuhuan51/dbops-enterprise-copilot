from contextlib import contextmanager
import pymysql
from app.core.config import settings

@contextmanager
def mysql_conn():
    conn = pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_CONNECT_DB,
        charset="utf8mb4",
        autocommit=True,
        # 超时（秒）
        connect_timeout=max(1, settings.SQL_TIMEOUT_MS // 1000),
        read_timeout=max(1, settings.SQL_TIMEOUT_MS // 1000),
        write_timeout=max(1, settings.SQL_TIMEOUT_MS // 1000),
    )
    try:
        yield conn
    finally:
        conn.close()
