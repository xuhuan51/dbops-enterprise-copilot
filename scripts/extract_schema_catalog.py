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
MAX_WORKERS = 5

# ==========================================
# 🧹 核心清洗逻辑 (Quality Control)
# ==========================================
SYNONYM_BLACKLIST = re.compile(r"(表|记录|数据|信息|管理|服务|列表|明细)$")


def clean_synonyms(synonyms: list, table_name: str) -> list:
    clean = []
    seen = set()
    for w in sorted(synonyms, key=len):
        w = w.strip()
        if not w or w == table_name: continue
        if len(w) > 10: continue
        if SYNONYM_BLACKLIST.search(w): continue
        if w not in seen:
            clean.append(w)
            seen.add(w)
    return clean[:5]


def extract_key_fields(columns_desc: str) -> str:
    keys = []
    lines = columns_desc.split('\n')
    for line in lines:
        match = re.search(r"- (\w+)", line)
        if not match: continue
        col_name = match.group(1).lower()
        if " [PK]" in line:
            keys.append(col_name)
        elif col_name.endswith("_id") or col_name.endswith("_code"):
            keys.append(col_name)
        elif "status" in col_name or "type" in col_name:
            keys.append(col_name)
        elif "amount" in col_name or "price" in col_name or "gmv" in col_name:
            keys.append(col_name)
    return ", ".join(keys[:8])


# ==========================================
# 基础工具
# ==========================================
def get_connection():
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
    """
    🔥 修复1：改用 SHOW TABLE STATUS，解决 information_schema 查不到表的问题
    """
    with conn.cursor() as cur:
        try:
            # 强制切库
            cur.execute(f"USE {db_name}")
            cur.execute(f"SHOW TABLE STATUS")
            rows = cur.fetchall()

            result = []
            for r in rows:
                name = r.get('Name') or r.get('name')
                comment = r.get('Comment') or r.get('comment') or ""
                if name:
                    result.append({"table_name": name, "table_comment": comment})

            return result
        except Exception as e:
            print(f"   [WARN] SHOW TABLE STATUS failed: {e}")
            return []


def get_schema_info_str(conn, db_name, table_name):
    """
    🔥 修复2：改用 SHOW FULL COLUMNS，解决 information_schema 触发 Proxy 内部 Bug (Error 30000)
    """
    with conn.cursor() as cur:
        try:
            # ShardingSphere 对 SHOW FULL COLUMNS 支持很好
            sql = f"SHOW FULL COLUMNS FROM `{table_name}` FROM `{db_name}`"
            cur.execute(sql)
            rows = cur.fetchall()

            col_desc_list = []
            for r in rows:
                # 兼容不同驱动返回的大小写
                field = r.get('Field') or r.get('field')
                type_ = r.get('Type') or r.get('type')
                comment = r.get('Comment') or r.get('comment') or ""
                key_val = r.get('Key') or r.get('key')

                key_mark = " [PK]" if key_val == 'PRI' else ""
                col_desc_list.append(f"- {field} ({type_}){key_mark}: {comment}")

            return "\n".join(col_desc_list)
        except Exception as e:
            # 如果某张表真的查不到，返回空，不要让整个脚本崩掉
            print(f"   [WARN] Failed to fetch schema for {table_name}: {e}")
            return f"Error fetching schema: {e}"


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
    conn = get_connection()
    try:
        # 获取元数据 (现在用 SHOW FULL COLUMNS，稳得一批)
        columns_desc = get_schema_info_str(conn, db, physical_table)
        samples_json = get_samples_json(conn, db, physical_table, limit=3)

        key_fields = extract_key_fields(columns_desc)

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
            # LLM 偶尔失败不影响大局
            llm_data = {"summary": f"{logical_name} 数据表", "synonyms": [], "risk_level": "normal",
                        "table_type": "fact"}

        raw_synonyms = llm_data.get('synonyms', [])
        cleaned_synonyms = clean_synonyms(raw_synonyms, logical_name)

        rich_text = (
            f"表名: {logical_name}\n"
            f"业务域: {llm_data.get('domain_suggestion', 'unknown')}\n"
            f"类型: {llm_data.get('table_type', 'fact')}\n"
            f"关键字段: {key_fields}\n"
            f"业务描述: {llm_data.get('summary', '')}\n"
            f"同义词: {','.join(cleaned_synonyms)}\n"
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
                "synonyms": cleaned_synonyms
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
    conn = get_connection()
    target_dbs = settings.TARGET_DBS
    tasks = []

    for db in target_dbs:
        db = db.strip()
        if not db: continue

        logger.info(f"📂 Scanning DB: {db}")
        tables = get_all_tables_list(conn, db)

        seen_logical = set()
        for t in tables:
            p_name = t['table_name']
            l_name = get_logical_name(p_name)
            if l_name in seen_logical: continue
            seen_logical.add(l_name)
            tasks.append((db, l_name, p_name, t.get('table_comment', '')))

    conn.close()

    total_tasks = len(tasks)
    logger.info(f"📋 Total Logical Tables to Process: {total_tasks}")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
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

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for card in results:
            f.write(json.dumps(card, ensure_ascii=False) + "\n")

    logger.info(f"🎉 ETL Done! Saved {len(results)} tables to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()