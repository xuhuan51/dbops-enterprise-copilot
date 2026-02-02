import os
from dotenv import load_dotenv

# ==========================================================
# 🛡️ 强制网络分流 (内外分流隔离方案)
# ==========================================================

# 1. 显式锁定你的代理端口 (根据你的 10808 修改)
# 这样即便系统环境变量乱了，代码也能找到正确的代理
os.environ["HTTP_PROXY"] = "http://127.0.0.1:10808"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:10808"

# 2. 核心：强制白名单，防止本地流量误入代理
# 必须包含 127.0.0.1 和本地模型端口 11434
os.environ["NO_PROXY"] = "127.0.0.1,localhost,11434,3306,3307,19530"

# 3. 双重保险：启用国内镜像，即便代理抖动也能下模型
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# 打印日志确认，防止以后排查又忘了端口
print(f"📡 [Network] 代理端口已锁定为 10808，本地流量 (127.0.0.1) 已设为强制直连。")

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
    MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "rag_schema")

    LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
    # 💡 建议：将 localhost 统一改为 127.0.0.1 避开部分系统的 IPv6 代理坑
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    LLM_MODEL = os.getenv("LLM_MODEL_NAME", "qwen2.5-coder:14b")

    EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
    RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base")

    # 输出路径
    OUT_PATH = os.path.join(project_root, "data", "schema_catalog.jsonl")


settings = Settings()
print(f"🛡️ [System] 网络加固已启动: NO_PROXY={os.environ['NO_PROXY']}, HF_MIRROR=Enabled")