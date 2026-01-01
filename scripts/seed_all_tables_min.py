import os, random, string, json
from datetime import datetime, timedelta
import pymysql
from dotenv import load_dotenv

# ===== env =====
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(project_root, ".env"))

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_CONNECT_DB = os.getenv("MYSQL_CONNECT_DB", "mysql")

TARGET_DBS = os.getenv("TARGET_DBS", "corp_trade_center,corp_user_center,corp_scm_erp,corp_marketing,corp_data_log").split(",")
ROWS_PER_TABLE = int(os.getenv("ROWS_PER_TABLE", "20"))           # 全表兜底每表行数
RESET_DATA = os.getenv("RESET_DATA", "false").lower() == "true"   # true 则 TRUNCATE 再插
RANDOM_SEED = int(os.getenv("SEED_RANDOM_SEED", "42"))

random.seed(RANDOM_SEED)

def connect():
    return pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_CONNECT_DB, charset="utf8mb4", autocommit=False
    )

def dt_between(days_back=365):
    end = datetime.now()
    start = end - timedelta(days=days_back)
    return start + timedelta(seconds=random.randint(0, int((end-start).total_seconds())))

def rand_str(n=12):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))

def rand_ip():
    return ".".join(str(random.randint(1, 254)) for _ in range(4))

def get_tables(cur, db):
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema=%s AND table_type='BASE TABLE'", (db,)
    )
    return [r[0] for r in cur.fetchall()]

def get_columns(cur, db, table):
    cur.execute(
        "SELECT column_name, data_type, is_nullable, column_key, extra "
        "FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s "
        "ORDER BY ordinal_position",
        (db, table)
    )
    return cur.fetchall()

def truncate(cur, db, table):
    cur.execute(f"USE `{db}`")
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    cur.execute(f"TRUNCATE TABLE `{table}`")
    cur.execute("SET FOREIGN_KEY_CHECKS=1")

def gen_value(data_type, col_name, row_idx):
    t = data_type.lower()

    # 一些常见字段名做“更真实”的生成
    cname = col_name.lower()
    if cname in ("ip", "client_ip"):
        return rand_ip()
    if cname in ("mobile", "phone"):
        return "1" + "".join(random.choice(string.digits) for _ in range(10))
    if "time" in cname or cname in ("ts",):
        return dt_between(180)
    if "status" in cname:
        return random.randint(0, 3)
    if cname in ("url",):
        return random.choice(["/api/order", "/api/pay", "/api/user", "/api/search", "/api/login"])

    # 按类型兜底
    if t in ("tinyint", "smallint", "int", "integer", "bigint"):
        return row_idx + 1
    if t in ("decimal", "numeric", "float", "double"):
        return round(random.uniform(1, 5000), 2)
    if t in ("varchar", "char", "text", "longtext", "mediumtext"):
        return f"{col_name}_{rand_str(8)}"
    if t in ("datetime", "timestamp", "date"):
        return dt_between(365)
    if t in ("json",):
        return json.dumps({"k": random.randint(1, 100), "tag": rand_str(6)}, ensure_ascii=False)
    # 其他类型：先给 None
    return None

def main():
    conn = connect()
    cur = conn.cursor()
    try:
        for db in TARGET_DBS:
            db = db.strip()
            print(f"\n📦 Seeding DB: {db}")
            tables = get_tables(cur, db)

            for table in tables:
                cols = get_columns(cur, db, table)
                if not cols:
                    continue

                # 只对“我们能生成值的列”插入（跳过 auto_increment）
                insert_cols = []
                col_meta = []
                pk_cols = []
                for (name, dtype, nullable, key, extra) in cols:
                    if "auto_increment" in (extra or "").lower():
                        continue
                    insert_cols.append(name)
                    col_meta.append((name, dtype, key))
                    if key == "PRI":
                        pk_cols.append(name)

                if not insert_cols:
                    continue

                if RESET_DATA:
                    try:
                        truncate(cur, db, table)
                    except Exception:
                        pass

                # 构造 rows
                rows = []
                for i in range(ROWS_PER_TABLE):
                    row = []
                    for (name, dtype, key) in col_meta:
                        # 主键尽量唯一
                        if key == "PRI":
                            row.append(i + 1)
                        else:
                            row.append(gen_value(dtype, name, i))
                    rows.append(tuple(row))

                placeholders = ",".join(["%s"] * len(insert_cols))
                col_list = ",".join([f"`{c}`" for c in insert_cols])
                sql = f"INSERT IGNORE INTO `{db}`.`{table}` ({col_list}) VALUES ({placeholders})"

                try:
                    cur.executemany(sql, rows)
                except Exception:
                    # 某些表字段组合/约束会失败，跳过即可（兜底策略）
                    continue

            conn.commit()
            print(f"✅ Done DB: {db}")

        print("\n✅ 全表兜底灌水完成（每表约 %d 行）" % ROWS_PER_TABLE)

    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()
