# app/core/config.py
import os
from dataclasses import dataclass
from dotenv import load_dotenv

# 尽量从项目根目录加载 .env
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

@dataclass(frozen=True)
class Settings:
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER: str = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_CONNECT_DB: str = os.getenv("MYSQL_CONNECT_DB", "mysql")

    # 执行安全参数
    SQL_DEFAULT_LIMIT: int = int(os.getenv("SQL_DEFAULT_LIMIT", "200"))
    SQL_MAX_LIMIT: int = int(os.getenv("SQL_MAX_LIMIT", "1000"))
    SQL_TIMEOUT_MS: int = int(os.getenv("SQL_TIMEOUT_MS", "5000"))  # 5s

    # 结果返回控制
    RESULT_MAX_ROWS: int = int(os.getenv("RESULT_MAX_ROWS", "200"))

settings = Settings()
