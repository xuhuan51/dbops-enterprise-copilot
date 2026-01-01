import os
import json
import random
import string
from datetime import datetime, timedelta
import pymysql
from dotenv import load_dotenv

# =========================
# 配置加载
# =========================
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
env_path = os.path.join(project_root, ".env")
load_dotenv(env_path)

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_CONNECT_DB = os.getenv("MYSQL_CONNECT_DB", "mysql")

# 你建库脚本的规模参数（这里只用于命名/抽样）
ORDER_SHARDS = int(os.getenv("ORDER_SHARDS", "128"))
USER_SHARDS = int(os.getenv("USER_SHARDS", "64"))
LOG_WEEKS = int(os.getenv("LOG_WEEKS", "104"))
DIM_TABLES_PER_DB = int(os.getenv("DIM_TABLES_PER_DB", "50"))

# 灌水规模（可通过环境变量改）
SEED_RANDOM_SEED = int(os.getenv("SEED_RANDOM_SEED", "42"))
FILL_ORDER_SHARDS = int(os.getenv("FILL_ORDER_SHARDS", "10"))     # 订单灌多少个分片（不是每片都灌）
FILL_USER_SHARDS = int(os.getenv("FILL_USER_SHARDS", "10"))       # 用户灌多少个分片
FILL_LOG_WEEKS = int(os.getenv("FILL_LOG_WEEKS", "10"))           # 灌多少个周表
ORDERS_PER_SHARD = int(os.getenv("ORDERS_PER_SHARD", "200"))
MAX_ITEMS_PER_ORDER = int(os.getenv("MAX_ITEMS_PER_ORDER", "3"))
USERS_PER_SHARD = int(os.getenv("USERS_PER_SHARD", "300"))
LOGINS_PER_SHARD = int(os.getenv("LOGINS_PER_SHARD", "800"))
LOG_ROWS_PER_WEEK = int(os.getenv("LOG_ROWS_PER_WEEK", "1000"))

# 维表灌水：每库灌多少张维表（避免你 250 张维表全灌太慢）
FILL_DIM_TABLES_PER_DB = int(os.getenv("FILL_DIM_TABLES_PER_DB", "20"))
DIM_ROWS_PER_TABLE = int(os.getenv("DIM_ROWS_PER_TABLE", "100"))

# 是否清空目标表再灌水（true/false）
RESET_DATA = os.getenv("RESET_DATA", "false").lower() == "true"

# 目标数据库
DBS = {
    "corp_trade_center": "trade",
    "corp_user_center": "user",
    "corp_scm_erp": "scm",
    "corp_marketing": "mkt",
    "corp_data_log": "log",
}

random.seed(SEED_RANDOM_SEED)


def connect():
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_CONNECT_DB,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.Cursor,
    )


def exists_table(cur, db, table) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
        (db, table),
    )
    return int(cur.fetchone()[0]) > 0


def exec_sql(cur, sql, args=None):
    cur.execute(sql, args or ())


def rand_phone():
    return "1" + "".join(random.choice(string.digits) for _ in range(10))


def rand_ip():
    return ".".join(str(random.randint(1, 254)) for _ in range(4))


def rand_url():
    base = random.choice(["/api/order", "/api/pay", "/api/user", "/api/search", "/api/login"])
    q = random.choice(["", "?q=foo", "?page=1", "?id=123", "?debug=false"])
    return base + q


def dt_between(days_back=90):
    # 近 90 天随机时间
    end = datetime.now()
    start = end - timedelta(days=days_back)
    delta = end - start
    return start + timedelta(seconds=random.randint(0, int(delta.total_seconds())))


def truncate_table(cur, db, table):
    exec_sql(cur, f"USE `{db}`")
    exec_sql(cur, f"TRUNCATE TABLE `{table}`")


def seed_dim_tables(cur, db, prefix):
    # 找出 prefix_dim_000.. 的前 N 张表，灌水
    exec_sql(cur, f"USE `{db}`")

    dim_tables = [f"{prefix}_dim_{i:03d}" for i in range(min(DIM_TABLES_PER_DB, FILL_DIM_TABLES_PER_DB))]
    for t in dim_tables:
        if not exists_table(cur, db, t):
            continue
        if RESET_DATA:
            truncate_table(cur, db, t)

        rows = []
        for i in range(DIM_ROWS_PER_TABLE):
            rid = i + 1
            code = f"{t}_C{rid:03d}"
            name = f"{t}_NAME_{rid:03d}"
            ext = {"k": random.randint(1, 100), "flag": random.choice([True, False])}
            ctime = dt_between(365)
            rows.append((rid, code, name, json.dumps(ext, ensure_ascii=False), ctime))

        # ext_json 是 JSON 类型，pymysql 传 string 即可
        exec_sql(
            cur,
            f"INSERT IGNORE INTO `{t}` (id, code, name, ext_json, create_time) VALUES (%s,%s,%s,%s,%s)",
            rows[0],
        )
        # 批量
        cur.executemany(
            f"INSERT IGNORE INTO `{t}` (id, code, name, ext_json, create_time) VALUES (%s,%s,%s,%s,%s)",
            rows,
        )
    print(f"   ✅ {db}: dim tables seeded (top {len(dim_tables)} tables)")


def seed_trade_center(cur):
    db = "corp_trade_center"
    exec_sql(cur, f"USE `{db}`")

    # 选一些分片（默认取 0..FILL_ORDER_SHARDS-1，也可改成 random.sample）
    shards = list(range(min(ORDER_SHARDS, FILL_ORDER_SHARDS)))

    for si in shards:
        suffix = f"{si:03d}"
        t_order = f"t_order_{suffix}"
        t_item = f"t_order_item_{suffix}"
        t_pay = f"t_pay_flow_{suffix}"

        for t in [t_order, t_item, t_pay]:
            if not exists_table(cur, db, t):
                raise RuntimeError(f"Missing table {db}.{t}")

        if RESET_DATA:
            truncate_table(cur, db, t_item)
            truncate_table(cur, db, t_pay)
            truncate_table(cur, db, t_order)

        # 为了可 join/可统计：uid、amount、create_time
        order_rows = []
        item_rows = []
        pay_rows = []

        base_oid = si * 10_000_000  # 保证不同分片 oid 不冲突
        base_item_id = si * 10_000_000
        for j in range(ORDERS_PER_SHARD):
            oid = base_oid + j + 1
            uid = random.randint(1, 2_000_000)
            amount = round(random.uniform(10, 2000), 2)
            ctime = dt_between(90)

            order_rows.append((oid, uid, amount, ctime))

            n_items = random.randint(1, MAX_ITEMS_PER_ORDER)
            for k in range(n_items):
                item_id = base_item_id + j * 10 + k + 1
                sku_id = random.randint(1, 5000)
                sku_name = f"SKU_{sku_id:05d}"
                qty = random.randint(1, 5)
                item_rows.append((item_id, oid, sku_id, sku_name, qty))

            # 支付：80% 成功
            status = 1 if random.random() < 0.8 else 0
            flow_id = f"F{suffix}{oid}"
            pay_time = ctime + timedelta(minutes=random.randint(1, 120))
            pay_rows.append((flow_id, oid, uid, status, pay_time))

        cur.executemany(
            f"INSERT IGNORE INTO `{t_order}` (oid, uid, amount, create_time) VALUES (%s,%s,%s,%s)",
            order_rows,
        )
        cur.executemany(
            f"INSERT IGNORE INTO `{t_item}` (id, oid, sku_id, sku_name, qty) VALUES (%s,%s,%s,%s,%s)",
            item_rows,
        )
        cur.executemany(
            f"INSERT IGNORE INTO `{t_pay}` (flow_id, oid, uid, status, pay_time) VALUES (%s,%s,%s,%s,%s)",
            pay_rows,
        )

        print(f"   ✅ trade shard {suffix}: orders={len(order_rows)}, items={len(item_rows)}, pays={len(pay_rows)}")


def seed_user_center(cur):
    db = "corp_user_center"
    exec_sql(cur, f"USE `{db}`")

    shards = list(range(min(USER_SHARDS, FILL_USER_SHARDS)))

    for si in shards:
        suffix = f"{si:03d}"
        t_user = f"u_user_base_{suffix}"
        t_login = f"u_login_log_{suffix}"
        for t in [t_user, t_login]:
            if not exists_table(cur, db, t):
                raise RuntimeError(f"Missing table {db}.{t}")

        if RESET_DATA:
            truncate_table(cur, db, t_login)
            truncate_table(cur, db, t_user)

        # uid 分片内唯一：uid = suffix * 10^7 + i
        base_uid = si * 10_000_000
        user_rows = []
        for i in range(USERS_PER_SHARD):
            uid = base_uid + i + 1
            mobile = rand_phone()
            pwd_hash = "hash_" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(16))
            reg_time = dt_between(365)
            user_rows.append((uid, mobile, pwd_hash, reg_time))

        cur.executemany(
            f"INSERT IGNORE INTO `{t_user}` (uid, mobile, pwd_hash, reg_time) VALUES (%s,%s,%s,%s)",
            user_rows,
        )

        # 登录日志：从该分片用户里抽样
        login_rows = []
        base_id = si * 10_000_000
        for j in range(LOGINS_PER_SHARD):
            lid = base_id + j + 1
            uid = base_uid + random.randint(1, USERS_PER_SHARD)
            ip = rand_ip()
            ts = dt_between(90)
            login_rows.append((lid, uid, ip, ts))

        cur.executemany(
            f"INSERT IGNORE INTO `{t_login}` (id, uid, ip, ts) VALUES (%s,%s,%s,%s)",
            login_rows,
        )

        print(f"   ✅ user shard {suffix}: users={len(user_rows)}, logins={len(login_rows)}")


def seed_data_log(cur):
    db = "corp_data_log"
    exec_sql(cur, f"USE `{db}`")

    weeks = list(range(1, min(LOG_WEEKS, FILL_LOG_WEEKS) + 1))
    for w in weeks:
        week = f"2025W{w:03d}"
        t_api = f"log_api_access_{week}"
        t_err = f"log_err_report_{week}"

        for t in [t_api, t_err]:
            if not exists_table(cur, db, t):
                raise RuntimeError(f"Missing table {db}.{t}")

        if RESET_DATA:
            truncate_table(cur, db, t_api)
            truncate_table(cur, db, t_err)

        base_id = w * 10_000_000
        api_rows = []
        err_rows = []
        for i in range(LOG_ROWS_PER_WEEK):
            rid = base_id + i + 1
            url = rand_url()
            latency = random.randint(5, 5000)
            ts = dt_between(90)
            api_rows.append((rid, url, latency, ts))

            # 10% 生成错误
            if random.random() < 0.1:
                eid = base_id + i + 1
                err_code = random.choice(["E500", "E502", "E429", "E400", "E401"])
                stack = "stacktrace_" + "".join(random.choice(string.ascii_letters) for _ in range(30))
                err_rows.append((eid, err_code, stack, ts))

        cur.executemany(
            f"INSERT IGNORE INTO `{t_api}` (id, url, latency, ts) VALUES (%s,%s,%s,%s)",
            api_rows,
        )
        cur.executemany(
            f"INSERT IGNORE INTO `{t_err}` (id, err_code, stack, ts) VALUES (%s,%s,%s,%s)",
            err_rows,
        )

        print(f"   ✅ log week {week}: api={len(api_rows)}, err={len(err_rows)}")


def seed_simple_tables(cur, db):
    """
    给一些非分表库（scm_erp/marketing）灌少量数据，便于 demo。
    """
    exec_sql(cur, f"USE `{db}`")

    # 查该库里有哪些表（排除 dim 表我们已经在 dim 函数里灌了）
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
        (db,),
    )
    tables = [r[0] for r in cur.fetchall()]
    tables = [t for t in tables if not t.endswith(("_dim_000",))]  # 随便过滤一下，避免误删
    # 只对包含 id BIGINT 主键结构的表做简单灌水（你 ERP/营销表都是类似结构）
    for t in tables:
        if "_dim_" in t:
            continue
        # 跳过分表（不属于这个库）
        if t.startswith(("t_order_", "t_order_item_", "t_pay_flow_", "u_user_base_", "u_login_log_", "log_api_access_", "log_err_report_")):
            continue

        # 判断列结构是否包含这些字段（简单判断：尝试插入，失败就跳过）
        if RESET_DATA:
            try:
                truncate_table(cur, db, t)
            except Exception:
                pass

        rows = []
        for i in range(200):
            rid = i + 1
            code = f"{t}_CODE_{rid:04d}"
            create_by = random.choice(["alice", "bob", "carol", "dave"])
            create_time = dt_between(365)
            status = random.randint(0, 3)
            memo = f"memo_{rid}"
            rows.append((rid, code, create_by, create_time, status, memo))

        try:
            cur.executemany(
                f"INSERT IGNORE INTO `{t}` (id, code, create_by, create_time, status, memo) VALUES (%s,%s,%s,%s,%s,%s)",
                rows,
            )
        except Exception:
            # 有些营销表字段不同（name/start_time/end_time），我们再尝试另一种
            try:
                rows2 = []
                for i in range(200):
                    rid = i + 1
                    name = f"{t}_NAME_{rid:04d}"
                    st = dt_between(90)
                    et = st + timedelta(days=random.randint(1, 30))
                    status = random.randint(0, 3)
                    rows2.append((rid, name, st, et, status))
                cur.executemany(
                    f"INSERT IGNORE INTO `{t}` (id, name, start_time, end_time, status) VALUES (%s,%s,%s,%s,%s)",
                    rows2,
                )
            except Exception:
                # 还是不匹配就跳过
                continue

    print(f"   ✅ {db}: simple seed done")


def main():
    conn = connect()
    cur = conn.cursor()

    try:
        print("🚀 开始灌水（轻量可演示版）")
        print(f"   - RESET_DATA={RESET_DATA}")
        print(f"   - trade: shards={FILL_ORDER_SHARDS}, orders/shard={ORDERS_PER_SHARD}")
        print(f"   - user : shards={FILL_USER_SHARDS}, users/shard={USERS_PER_SHARD}, logins/shard={LOGINS_PER_SHARD}")
        print(f"   - log  : weeks={FILL_LOG_WEEKS}, rows/week={LOG_ROWS_PER_WEEK}")
        print(f"   - dim  : per_db_tables={FILL_DIM_TABLES_PER_DB}, rows/table={DIM_ROWS_PER_TABLE}")

        # 1) 维表先灌（每库一部分）
        for db, prefix in DBS.items():
            seed_dim_tables(cur, db, prefix)
            conn.commit()

        # 2) 交易中心分表灌水（可做趋势、TopN、join）
        seed_trade_center(cur)
        conn.commit()

        # 3) 用户中心分表灌水（可做活跃、留存）
        seed_user_center(cur)
        conn.commit()

        # 4) 日志库灌水（可做延迟分布、错误码统计）
        seed_data_log(cur)
        conn.commit()

        # 5) ERP/营销少量灌水
        seed_simple_tables(cur, "corp_scm_erp")
        seed_simple_tables(cur, "corp_marketing")
        conn.commit()

        print("\n✅ 灌水完成！你现在可以开始做：执行 SQL + 图表 + 分析结果。")

    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
