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
    MYSQL_DB = os.getenv("MYSQL_CONNECT_DB", "mysql")

    # 目标抓取库
    TARGET_DBS = os.getenv("TARGET_DBS", "").split(",")

    # LLM 配置
    LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    LLM_MODEL = os.getenv("LLM_MODEL_NAME", "qwen2.5:14b")

    # 输出路径
    OUT_PATH = os.path.join(project_root, "data", "schema_catalog.jsonl")


settings = Settings()