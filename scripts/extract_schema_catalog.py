import os
import re
import sys
import json
import pymysql
import datetime
import concurrent.futures
from decimal import Decimal
from tqdm import tqdm

# 添加项目根目录到 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.core.llm import chat_completion
from app.core.prompts import TABLE_CARD_GOVERNANCE_PROMPT
from app.core.logger import logger

OUTPUT_FILE = settings.OUT_PATH
MAX_WORKERS = 5  # 🔥 并发数 (根据你的 LLM Rate Limit 调整，太高会报错)

# ==========================================
# 🧹 核心清洗逻辑 (Quality Control)
# ==========================================
SYNONYM_BLACKLIST = re.compile(r"(表|记录|数据|信息|管理|服务|列表|明细)$")


def clean_synonyms(synonyms: list, table_name: str) -> list:
    """
    清洗同义词：
    1. 去掉包含 '表', '记录' 等泛词的词
    2. 去掉和表名完全一样的词
    3. 限制数量 (Top 5)
    """
    clean = []
    seen = set()

    # 优先保留短词 (通常是核心概念)
    for w in sorted(synonyms, key=len):
        w = w.strip()
        # 过滤空、过滤表名本身、过滤泛词后缀
        if not w or w == table_name:
            continue
        if len(w) > 10:  # 太长的词通常是解释，不是同义词
            continue
        if SYNONYM_BLACKLIST.search(w):
            continue

        if w not in seen:
            clean.append(w)
            seen.add(w)

    return clean[:5]


def extract_key_fields(columns_desc: str) -> str:
    """
    从 Schema 描述中提取硬锚点 (Key Fields)
    规则：提取主键、外键(_id)、时间(_time/_date)、状态(status/type)
    """
    keys = []
    lines = columns_desc.split('\n')
    for line in lines:
        # line 格式: "- order_id (bigint) [PK]: 订单ID"
        # 简单正则提取字段名
        match = re.search(r"- (\w+)", line)
        if not match: continue
        col_name = match.group(1).lower()

        # 锚点策略
        if " [PK]" in line:  # 主键必选
            keys.append(col_name)
        elif col_name.endswith("_id") or col_name.endswith("_code"):  # 外键/编码
            keys.append(col_name)
        elif "status" in col_name or "type" in col_name:  # 核心维度
            keys.append(col_name)
        elif "amount" in col_name or "price" in col_name or "gmv" in col_name:  # 核心指标
            keys.append(col_name)

    # 限制长度，防止 Token 爆炸
    return ", ".join(keys[:8])


# ==========================================
# 基础工具
# ==========================================
def get_connection():
    # 🔥 注意：在多线程里，每个线程必须创建自己的连接，不能共享
    return pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_CONNECT_DB,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor
    )


class DateEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime.datetime, datetime.date)):
            return obj.strftime('%Y-%m-%d %H:%M:%S')
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def get_logical_name(table_name: str) -> str:
    name = re.sub(r'_\d{4}W\d{2,3}$', '', table_name, flags=re.IGNORECASE)
    name = re.sub(r'_\d{8}$', '', name)
    name = re.sub(r'_\d+$', '', name)
    return name


def get_all_tables_list(conn, db_name):
    """只负责获取表名列表，不负责重的数据操作"""
    with conn.cursor() as cur:
        sql = "SELECT table_name, table_comment FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name"
        cur.execute(sql, (db_name,))
        rows = cur.fetchall()
        return [{k.lower(): v for k, v in r.items()} for r in rows]


def get_schema_info_str(conn, db_name, table_name):
    with conn.cursor() as cur:
        sql = """
              SELECT column_name, column_type, column_comment, column_key
              FROM information_schema.columns
              WHERE table_schema = %s \
                AND table_name = %s
              ORDER BY ordinal_position \
              """
        cur.execute(sql, (db_name, table_name))
        rows = cur.fetchall()
        columns = [{k.lower(): v for k, v in r.items()} for r in rows]

        col_desc_list = []
        for c in columns:
            comment = c.get('column_comment') or ""
            key = " [PK]" if c.get('column_key') == 'PRI' else ""
            col_desc_list.append(f"- {c['column_name']} ({c['column_type']}){key}: {comment}")

        return "\n".join(col_desc_list)


def get_samples_json(conn, db_name, table_name, limit=3):
    with conn.cursor() as cur:
        try:
            cur.execute(f"SELECT * FROM `{db_name}`.`{table_name}` LIMIT %s", (limit,))
            rows = cur.fetchall()
            if rows:
                rows = [{k.lower(): v for k, v in r.items()} for r in rows]
            return json.dumps(rows, cls=DateEncoder, ensure_ascii=False, indent=None)
        except Exception:
            return "[]"


# ==========================================
# 🧵 线程工作函数 (Worker)
# ==========================================
def process_single_logical_table(db, logical_name, physical_table, table_comment):
    """
    单个逻辑表的 ETL 处理函数 (在线程池中运行)
    """
    # 1. 每个线程建立独立连接
    conn = get_connection()
    try:
        # 获取元数据
        columns_desc = get_schema_info_str(conn, db, physical_table)
        samples_json = get_samples_json(conn, db, physical_table, limit=3)

        # 2. 提取硬锚点 (Hard Anchors)
        key_fields = extract_key_fields(columns_desc)

        # 3. 调用 LLM (耗时操作)
        prompt = TABLE_CARD_GOVERNANCE_PROMPT.format(
            db=db,
            logical_table=logical_name,
            table=physical_table,
            domain="unknown",
            table_comment=table_comment,
            columns_desc=columns_desc,
            samples=samples_json
        )

        try:
            llm_resp = chat_completion(prompt)
            llm_data = json.loads(llm_resp)
        except Exception as e:
            logger.warning(f"⚠️ LLM Failed for {logical_name}: {e}")
            llm_data = {"summary": f"{logical_name} 数据表", "synonyms": [], "risk_level": "normal",
                        "table_type": "fact"}

        # 4. 🔥 质量优化：同义词清洗
        raw_synonyms = llm_data.get('synonyms', [])
        cleaned_synonyms = clean_synonyms(raw_synonyms, logical_name)

        # 5. 🔥 质量优化：Rich Text 结构重组
        # 优先展示：业务域 -> 类型 -> 关键字段 -> 总结 -> 同义词 -> 结构
        rich_text = (
            f"表名: {logical_name}\n"
            f"业务域: {llm_data.get('domain_suggestion', 'unknown')}\n"
            f"类型: {llm_data.get('table_type', 'fact')}\n"
            f"关键字段: {key_fields}\n"  # ⚓️ 硬锚点
            f"业务描述: {llm_data.get('summary', '')}\n"
            f"同义词: {','.join(cleaned_synonyms)}\n"  # 🧹 清洗后的
            f"字段结构:\n{columns_desc}\n"
            f"样本数据:\n{samples_json}"
        )

        card = {
            "identity": {
                "db": db,
                "logical_table": logical_name,
                "physical_table_example": physical_table,
                "domain": llm_data.get("domain_suggestion", "unknown")
            },
            "llm": {
                "risk_level": llm_data.get("risk_level", "normal"),
                "table_type": llm_data.get("table_type", "unknown"),
                "summary": llm_data.get("summary", ""),
                "synonyms": cleaned_synonyms  # 存清洗后的
            },
            "text": rich_text,
            "last_update": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        return card

    except Exception as e:
        logger.error(f"❌ Error processing {logical_name}: {e}")
        return None
    finally:
        conn.close()


def main():
    logger.info(f"🚀 Start ETL (Concurrency: {MAX_WORKERS})")

    # 获取主库表清单 (这一步很快，单线程即可)
    conn = get_connection()
    target_dbs = settings.TARGET_DBS

    tasks = []  # (db, logical_name, physical_name, comment)

    for db in target_dbs:
        db = db.strip()
        if not db: continue

        logger.info(f"📂 Scanning DB: {db}")
        tables = get_all_tables_list(conn, db)

        # 分表归一化
        seen_logical = set()
        for t in tables:
            p_name = t['table_name']
            l_name = get_logical_name(p_name)
            if l_name in seen_logical: continue

            seen_logical.add(l_name)
            # 添加到任务列表
            tasks.append((db, l_name, p_name, t.get('table_comment', '')))

    conn.close()

    total_tasks = len(tasks)
    logger.info(f"📋 Total Logical Tables to Process: {total_tasks}")

    # 线程池并发处理
    results = []

    # 使用 tqdm 显示进度
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 提交任务
        future_to_table = {
            executor.submit(process_single_logical_table, db, l_name, p_name, comment): l_name
            for (db, l_name, p_name, comment) in tasks
        }

        for future in tqdm(concurrent.futures.as_completed(future_to_table), total=total_tasks,
                           desc="Processing Tables"):
            try:
                card = future.result()
                if card:
                    results.append(card)
            except Exception as e:
                logger.error(f"Thread Error: {e}")

    # 写入文件
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for card in results:
            f.write(json.dumps(card, ensure_ascii=False) + "\n")

    logger.info(f"🎉 ETL Done! Saved {len(results)} tables to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()