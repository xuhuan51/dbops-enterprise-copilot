import json
import os
import sys
import time

# ==========================================
# 1. 路径与环境设置
# ==========================================
# 确保能导入 app 模块 (假设脚本在 scripts/ 目录下)
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

from pymilvus import utility  # 👈 utility 必须从 pymilvus 导入，rag_store 已经不暴露它了
from app.core.config import settings
from app.core.logger import logger
from app.core.rag_store import rag_store  # 引用最新的 DAO

# 定义数据文件路径
KNOWLEDGE_FILE = os.path.join(project_root, "data", "business_terms.json")


def ingest_knowledge():
    """
    负责入库 V2.0 版本的业务术语 (Knowledge)
    """
    # 1. 检查文件
    if not os.path.exists(KNOWLEDGE_FILE):
        logger.error(f"❌ 文件不存在: {KNOWLEDGE_FILE}")
        return

    logger.info(f"🧠 [Ingest] 准备导入业务知识: {KNOWLEDGE_FILE}")

    # ==========================================
    # 2. 暴力重建集合 (Schema Reset)
    # ==========================================
    # 必须删除重建，因为旧表的 Schema 可能不一样
    if utility.has_collection("rag_knowledge"):
        utility.drop_collection("rag_knowledge")
        logger.info("🗑️  [Milvus] 检测到旧集合，已删除 'rag_knowledge'")

    # 🔥 关键步骤：强制重新初始化 Collection
    # 因为刚才 drop 了，现在必须让 rag_store 重新运行建表逻辑 (create_collection + create_index)
    rag_store.knowledge_col = rag_store._init_knowledge_collection()
    logger.info("🆕 [Milvus] 新集合 'rag_knowledge' 创建成功 (Schema V2)")

    # ==========================================
    # 3. 读取并插入
    # ==========================================
    total_count = 0
    start_time = time.time()

    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

            if not isinstance(data, list):
                logger.error("❌ JSON 格式错误: 根节点必须是 List")
                return

            total_items = len(data)
            logger.info(f"📂 发现 {total_items} 条数据，开始向量化入库...")

            for i, item in enumerate(data):
                # 🔥 调用 DAO 的 add_knowledge
                # 它内部会自动处理:
                # 1. 拼接 text (term + definition)
                # 2. 调用模型做 Embedding
                # 3. 存入 payload_json
                rag_store.add_knowledge(item)

                total_count += 1

                # 打印进度条
                if total_count % 10 == 0:
                    print(f"   ... 已处理 {total_count}/{total_items}", end="\r")

    except Exception as e:
        logger.error(f"❌ 入库过程中断: {e}")
        # 这里不 return，尝试 flush 已有的数据

    # ==========================================
    # 4. 收尾工作
    # ==========================================
    # 刷盘：确保数据写入磁盘，立即可查
    rag_store.knowledge_col.flush()

    # 统计耗时
    cost = time.time() - start_time
    print(f"\n✅ [Ingest] 导入完成！共 {total_count} 条，耗时 {cost:.2f}s")

    # 简单验证一下
    num_entities = rag_store.knowledge_col.num_entities
    logger.info(f"📊 [Milvus] 当前集合内共有 {num_entities} 条实体")


if __name__ == "__main__":
    ingest_knowledge()