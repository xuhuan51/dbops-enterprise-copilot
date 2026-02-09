import os
import sqlite3
import json
import pandas as pd
import logging
import time

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class DatabaseProfiler:
    def __init__(self, db_path: str, output_dir: str):
        self.db_path = db_path
        self.db_name = os.path.basename(db_path).replace(".sqlite", "")
        self.output_dir = output_dir
        # 允许多线程连接（为了批量处理时不报错）
        self.conn = sqlite3.connect(db_path, check_same_thread=False)

        self.ignore_cols = {'id', 'name', 'description', 'comment', 'year', 'date', 'type', 'status'}

    def close(self):
        """关闭连接"""
        if self.conn:
            self.conn.close()

    def get_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        return [row[0] for row in cursor.fetchall()]

    def get_column_values(self, table, column):
        try:
            query = f"SELECT DISTINCT \"{column}\" FROM \"{table}\" WHERE \"{column}\" IS NOT NULL"
            df = pd.read_sql_query(query, self.conn)
            values = set(df.iloc[:, 0].astype(str).str.strip())
            values.discard('')
            values.discard('None')
            values.discard('nan')
            return values
        except Exception as e:
            # logger.warning(f"  ⚠️ Skip reading {table}.{column}: {e}")
            return set()

    def is_potential_key(self, table, col, values):
        col_lower = col.lower()
        if col_lower in self.ignore_cols: return False

        # 规则 1: 名字像 ID
        if any(x in col_lower for x in ['id', 'code', 'num', 'key', 'no.']):
            return True

        # 规则 2: 数据特征
        if len(values) > 10:
            sample = next(iter(values))
            if sample.isdigit() or len(sample) > 4:
                return True
        return False

    def analyze(self):
        # logger.info(f"🔎 Analyzing DB: {self.db_name}")
        tables = self.get_tables()
        col_data = {}

        # 1. Indexing
        for table in tables:
            try:
                df_schema = pd.read_sql_query(f"PRAGMA table_info(\"{table}\")", self.conn)
                for _, row in df_schema.iterrows():
                    col = row['name']
                    values = self.get_column_values(table, col)
                    if self.is_potential_key(table, col, values):
                        key = f"{table}.{col}"
                        col_data[key] = {"table": table, "col": col, "set": values}
            except Exception as e:
                logger.error(f"Error analyzing table {table}: {e}")

        # 2. Matching
        evidence_edges = []
        keys = list(col_data.keys())

        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                k1, k2 = keys[i], keys[j]
                d1, d2 = col_data[k1], col_data[k2]

                if d1['table'] == d2['table']: continue

                s1, s2 = d1['set'], d2['set']
                if not s1 or not s2: continue

                intersection = len(s1 & s2)
                if intersection == 0: continue

                min_len = min(len(s1), len(s2))
                score = intersection / min_len

                # 阈值 0.9
                if score >= 0.9:
                    logger.info(f"    🔥 MATCH FOUND: {self.db_name} | {k1} <-> {k2} (Score: {score:.2f})")
                    evidence_edges.append({
                        "u": d1['table'], "v": d2['table'],
                        "u_col": d1['col'], "v_col": d2['col'],
                        "weight": 0.05, "type": "CONTENT_PROFILE", "score": score
                    })

        # 3. Saving
        if evidence_edges:
            os.makedirs(self.output_dir, exist_ok=True)
            out_path = os.path.join(self.output_dir, f"{self.db_name}.json")
            with open(out_path, 'w') as f:
                json.dump(evidence_edges, f, indent=2)
            logger.info(f"✅ Saved {len(evidence_edges)} edges for {self.db_name}")
        else:
            logger.info(f"💤 No hidden joins found for {self.db_name}")


# ==========================================
# 🔥🔥🔥 核心修改：批量扫描逻辑 🔥🔥🔥
# ==========================================
def batch_process(root_dir, output_dir):
    """
    递归查找 root_dir 下的所有 .sqlite 文件并处理
    """
    logger.info(f"🚀 Starting Batch Profiling from: {root_dir}")

    db_files = []
    # 1. 扫描所有文件
    for root, dirs, files in os.walk(root_dir):
        for file in files:
            if file.endswith(".sqlite"):
                full_path = os.path.join(root, file)
                db_files.append(full_path)

    total = len(db_files)
    logger.info(f"📋 Found {total} databases. Processing...")

    # 2. 逐个处理
    for idx, db_path in enumerate(db_files):
        try:
            print(f"[{idx + 1}/{total}] Processing: {os.path.basename(db_path)}...")
            profiler = DatabaseProfiler(db_path, output_dir)
            profiler.analyze()
            profiler.close()
        except Exception as e:
            logger.error(f"❌ Failed to process {db_path}: {e}")


if __name__ == "__main__":
    # 👉 根据你的截图，你的 dev_databases 文件夹路径
    # 请确保这个路径相对于脚本运行的位置是正确的
    # 如果脚本在 scripts/ 目录下，向上退两级找 data

    # 假设你在项目根目录运行，或者脚本在 app/.. 下
    # 建议写绝对路径，或者根据你的目录结构调整

    # 根据截图推测的相对路径：
    DB_ROOT_DIR = "../data/bird/dev_databases"
    OUTPUT_DIR = "../data/bird/metadata/evidence_joins"

    # 检查路径是否存在，防止报错
    if not os.path.exists(DB_ROOT_DIR):
        # 尝试回退寻找（容错）
        DB_ROOT_DIR = "../data/bird/dev_databases"
        if not os.path.exists(DB_ROOT_DIR):
            DB_ROOT_DIR = "../../../data/bird/dev_databases"

    if os.path.exists(DB_ROOT_DIR):
        batch_process(DB_ROOT_DIR, OUTPUT_DIR)
    else:
        logger.error(f"❌ Cannot find directory: {DB_ROOT_DIR}. Please check the path configuration.")