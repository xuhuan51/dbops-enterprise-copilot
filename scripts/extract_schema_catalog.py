import os
import json
import pymysql
from dotenv import load_dotenv

# --- 路径与环境变量 ---
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(project_root, ".env")
load_dotenv(env_path)

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_CONNECT_DB = os.getenv("MYSQL_CONNECT_DB", "mysql")

# 你要抽取的目标库（逗号分隔）
TARGET_DBS = os.getenv(
    "TARGET_DBS",
    "corp_trade_center,corp_user_center,corp_scm_erp,corp_marketing,corp_data_log"
).split(",")

OUT_PATH = os.path.join(project_root, "data", "schema_catalog.jsonl")
os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)


def get_conn():
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_CONNECT_DB,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor
    )


def build_text(db, table, t_comment, cols, pks, indexes):
    col_lines = []
    for c in cols:
        name = c["COLUMN_NAME"]
        dtype = c["DATA_TYPE"]
        cmt = c.get("COLUMN_COMMENT") or ""
        col_lines.append(f"- {name} ({dtype}) {cmt}".strip())

    pk_line = ", ".join(pks) if pks else ""
    idx_line = ", ".join([f"{i['INDEX_NAME']}({i['COLUMNS']})" for i in indexes]) if indexes else ""

    text = (
        f"库: {db}\n"
        f"表: {table}\n"
        f"表描述: {t_comment or ''}\n"
        f"主键: {pk_line}\n"
        f"索引: {idx_line}\n"
        f"字段:\n" + "\n".join(col_lines)
    )
    return text


def main():
    conn = get_conn()
    cur = conn.cursor()

    total = 0
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for db in [x.strip() for x in TARGET_DBS if x.strip()]:
            # 表列表
            cur.execute("""
                SELECT table_name, table_comment
                FROM information_schema.tables
                WHERE table_schema=%s AND table_type='BASE TABLE'
            """, (db,))
            tables = cur.fetchall()
            print(f"📦 {db}: {len(tables)} tables")

            for t in tables:
                table = t.get("table_name") or t.get("TABLE_NAME")
                t_comment = t.get("table_comment") or t.get("TABLE_COMMENT") or ""

                # 字段
                cur.execute("""
                    SELECT COLUMN_NAME, DATA_TYPE, COLUMN_COMMENT, IS_NULLABLE, COLUMN_KEY
                    FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=%s
                    ORDER BY ORDINAL_POSITION
                """, (db, table))
                cols = cur.fetchall()

                # 主键
                pks = [x["COLUMN_NAME"] for x in cols if x.get("COLUMN_KEY") == "PRI"]

                # 索引（聚合 index_name -> columns）
                cur.execute("""
                    SELECT INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS COLUMNS
                    FROM information_schema.statistics
                    WHERE table_schema=%s AND table_name=%s
                    GROUP BY INDEX_NAME
                """, (db, table))
                indexes = cur.fetchall()

                text = build_text(db, table, t_comment, cols, pks, indexes)

                obj = {
                    "db": db,
                    "table": table,
                    "table_comment": t_comment,
                    "columns": [
                        {
                            "name": x["COLUMN_NAME"],
                            "type": x["DATA_TYPE"],
                            "comment": x.get("COLUMN_COMMENT") or "",
                            "nullable": x.get("IS_NULLABLE") == "YES",
                            "key": x.get("COLUMN_KEY") or "",
                        } for x in cols
                    ],
                    "pk": pks,
                    "indexes": [{"index": i["INDEX_NAME"], "columns": i["COLUMNS"]} for i in indexes],
                    "text": text,
                }

                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                total += 1

    cur.close()
    conn.close()

    print(f"\n✅ catalog 写入完成: {OUT_PATH}")
    print(f"✅ total tables: {total}")


if __name__ == "__main__":
    main()
