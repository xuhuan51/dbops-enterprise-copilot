import os
from dotenv import load_dotenv

# 加载 .env
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(project_root, ".env"))


class Settings:
    # =========================
    # 🔌 A. 物理库配置 (仅用于 schema 提取或灌水脚本)
    # =========================
    MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_CONNECT_DB = os.getenv("MYSQL_CONNECT_DB", "mysql")

    # =========================
    # 🔌 B. Proxy 配置 (Agent 专用)
    # =========================
    # 如果 .env 没配，默认 fallback 到 PROXY_HOST 或报错
    PROXY_HOST = os.getenv("PROXY_HOST", "127.0.0.1")
    PROXY_PORT = int(os.getenv("PROXY_PORT", 3307))
    PROXY_USER = os.getenv("PROXY_USER", "root")
    PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "root")
    PROXY_LOGIC_DB = os.getenv("PROXY_LOGIC_DB", "dbops_proxy")

    # =========================
    # 🛠️ 通用工具配置
    # =========================
    SQL_TIMEOUT_MS = int(os.getenv("SQL_TIMEOUT_MS", "10000"))
    RESULT_MAX_ROWS = int(os.getenv("RESULT_MAX_ROWS", "1000"))
    TARGET_DBS = os.getenv("TARGET_DBS", "").split(",")

    # =========================
    # 🧠 AI & 向量库配置
    # =========================
    MILVUS_HOST = os.getenv("MILVUS_HOST", "127.0.0.1")
    MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
    MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "schema_catalog_v2")

    LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    LLM_MODEL = os.getenv("LLM_MODEL_NAME", "qwen2.5:14b")

    EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
    RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base")  # 注意这里我改回了 base，和你 env 一致

    # 输出路径
    OUT_PATH = os.path.join(project_root, "data", "schema_catalog.jsonl")


settings = Settings()