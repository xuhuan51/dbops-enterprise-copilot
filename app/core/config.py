import os
from dotenv import load_dotenv

# 加载 .env
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(project_root, ".env"))


class Settings:
    # MySQL 配置
    MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_CONNECT_DB = os.getenv("MYSQL_CONNECT_DB", "mysql")
    # SQL 执行超时时间 (毫秒)，默认 10秒
    # 逻辑：尝试去读环境变量 SQL_TIMEOUT_MS，读不到就用 "10000"，最后转成 int
    SQL_TIMEOUT_MS = int(os.getenv("SQL_TIMEOUT_MS", "10000"))

    # 结果集最大行数限制，默认 1000行
    # 逻辑：防止一次查出几万行数据撑爆内存
    RESULT_MAX_ROWS = int(os.getenv("RESULT_MAX_ROWS", "1000"))

    # 目标抓取库
    TARGET_DBS = os.getenv("TARGET_DBS", "").split(",")

    # LLM 配置
    LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    LLM_MODEL = os.getenv("LLM_MODEL_NAME", "qwen2.5:14b")

    # 输出路径
    OUT_PATH = os.path.join(project_root, "data", "schema_catalog.jsonl")


settings = Settings()