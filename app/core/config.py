import os
from dotenv import load_dotenv

# ==========================================================
# 🛡️ 强制网络分流 (保持不变，这部分很棒)
# ==========================================================
os.environ["HTTP_PROXY"] = "http://127.0.0.1:10808"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:10808"
os.environ["NO_PROXY"] = "127.0.0.1,localhost,11434,3306,3307,19530"
# os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

print(f"📡 [Network] 代理端口已锁定为 10808，本地流量 (127.0.0.1) 已设为强制直连。")

# 加载 .env
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(project_root, ".env"))


class Settings:
    # =========================
    # 💾 A. 物理数据库配置 (MySQL)
    # =========================
    # 在 Docker 中，如果你是在同一个 network 下，HOST 可以填 mysql-3306
    DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_USER = os.getenv("DB_USER", "root")
    # 这里的密码要和你 docker-compose.yml 里的 MYSQL_ROOT_PASSWORD 一致
    DB_PASSWORD = os.getenv("DB_PASSWORD", "2679622408")
    # 这里的库名要和你 docker-compose.yml 里的 MYSQL_DATABASE 一致
    DB_NAME = os.getenv("DB_NAME", "ecommerce")
    # 结果限制，防止 select * from big_table 撑爆内存
    RESULT_MAX_ROWS = int(os.getenv("RESULT_MAX_ROWS", "50"))

    # 执行超时时间 (秒)，防止死循环 SQL
    SQL_EXEC_TIMEOUT = 10

    # =========================
    # 🧠 B. AI & 向量库配置 (保持不变)
    # =========================
    MILVUS_HOST = os.getenv("MILVUS_HOST", "127.0.0.1")
    MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
    MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "rag_schema")

    LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    LLM_MODEL = os.getenv("LLM_MODEL_NAME", "qwen2.5-coder:32b")

    EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
    RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base")


settings = Settings()