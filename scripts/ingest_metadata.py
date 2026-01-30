# ingest_metadata.py
import json
import os
import sys
import ast  # 引入这个库来处理单引号格式
from tqdm import tqdm

# 1. 确保能导入 app 模块 (解决路径问题)
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 尝试根据你的目录结构动态添加上级目录
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from app.core.config import settings
from app.core.logger import logger
from app.core.rag_store import rag_store, utility

# 指向你 ETL 脚本生成的那个文件
INPUT_FILE = settings.OUT_PATH


def main():
    # --- 检查文件是否存在 ---
    if not os.path.exists(INPUT_FILE):
        logger.error(f"❌ 找不到源文件: {INPUT_FILE}")
        logger.error("请先运行 ETL 脚本生成元数据！")
        return

    logger.info("♻️  正在重置 Schema 集合 (Drop & Rebuild)...")

    # --- 1. 暴力重建集合 ---
    # 必须删掉旧的，因为可能改了表结构
    if utility.has_collection("rag_schema"):
        utility.drop_collection("rag_schema")
        logger.info("   已删除旧集合 rag_schema")

    # --- 2. 重新初始化 Store ---
    # 这步很关键！只有重新 init，rag_store 才会按新结构去 Create Collection
    rag_store.__init__()

    # --- 3. 开始搬运数据 ---
    logger.info(f"🚀 开始入库: {INPUT_FILE}")

    success_count = 0
    error_count = 0

    # 读取总行数用于进度条
    try:
        total_lines = sum(1 for _ in open(INPUT_FILE, "r", encoding="utf-8"))
    except Exception:
        total_lines = 0

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        # 使用 tqdm 显示进度条
        for line in tqdm(f, total=total_lines, desc="Indexing Tables"):
            line = line.strip()
            if not line: continue

            try:
                # 🛠️ 增强解析逻辑 (核心修改点)
                # 1. 先尝试按标准 JSON 解析 (双引号)
                try:
                    card = json.loads(line)
                except json.JSONDecodeError:
                    # 2. 如果报错，说明可能是 Python 字典格式 (单引号)，尝试用 ast 安全解析
                    try:
                        card = ast.literal_eval(line)
                    except Exception:
                        # 如果还不行，那就真的是坏数据了，抛出原始错误
                        raise

                # 🔥 核心动作：调用 rag_store 的入库方法
                rag_store.add_schema_card(card)

                success_count += 1

                # 每 100 条刷盘一次，防止内存溢出
                if success_count % 100 == 0:
                    rag_store.schema_col.flush()

            except Exception as e:
                error_count += 1
                # 打印第一条错误，避免刷屏
                if error_count == 1:
                    logger.error(f"⚠️ 入库失败 (First Error): {e}")
                    logger.error(f"   出错行内容: {line[:50]}...")

    # --- 4. 收尾工作 ---
    logger.info("💾 正在刷盘并构建索引...")
    # 确保集合不为空再 flush，避免报错
    if success_count > 0:
        rag_store.schema_col.flush()

        # 显式构建索引 (HNSW)，保证检索速度
        index_params = {
            "metric_type": "COSINE",
            "index_type": "HNSW",
            "params": {"M": 16, "efConstruction": 200}
        }
        # 尝试建索引，如果已自动建立则忽略
        try:
            rag_store.schema_col.create_index(field_name="vector", index_params=index_params)
        except Exception:
            pass  # Milvus 有时会自动建

    logger.info(f"🎉 入库完成！成功: {success_count}, 失败: {error_count}")
    # 安全获取数量
    try:
        count = rag_store.schema_col.num_entities
    except:
        count = success_count
    logger.info(f"📊 当前集合总数: {count}")


if __name__ == "__main__":
    main()