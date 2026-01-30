import os
import re
import pymysql
from dotenv import load_dotenv
from datetime import datetime

# =========================================================
# 0) 配置加载
# =========================================================
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
env_path = os.path.join(project_root, ".env")
load_dotenv(env_path)

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_CONNECT_DB = os.getenv("MYSQL_CONNECT_DB", "mysql")

# 规模参数（你要“几百表”，这里直接拉高）
ORDER_SHARDS = int(os.getenv("ORDER_SHARDS", "128"))          # 订单分片数：128 -> 订单/明细/支付 = 384 表
USER_SHARDS = int(os.getenv("USER_SHARDS", "64"))             # 用户分片数：64 -> 用户基表/登录日志 = 128 表
LOG_WEEKS = int(os.getenv("LOG_WEEKS", "104"))                # 日志按周：104 周（2年）-> 208 表
DIM_TABLES_PER_DB = int(os.getenv("DIM_TABLES_PER_DB", "50")) # 每个库额外维表数量：50 * 5库 = 250 表

# 你最终的表总数大致是：
# trade_center: 4 + ORDER_SHARDS*3 + DIM(50)  = 4 + 384 + 50 = 438
# user_center : 3 + USER_SHARDS*2  + DIM(50)  = 3 + 128 + 50 = 181
# scm_erp     : 17 + DIM(50)                   = 67
# marketing   : 8 + DIM(50)                    = 58
# data_log    : LOG_WEEKS*2 + DIM(50)          = 208 + 50 = 258
# 合计 ~ 1002 表（足够“几百表”甚至上千表）


def _safe_ident(name: str) -> str:
    """
    仅允许字母数字下划线，防止注入（db/table 名称一般都符合这个）。
    """
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"Unsafe identifier: {name}")
    return name


def get_connection():
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_CONNECT_DB,
        charset="utf8mb4",
        autocommit=True,  # 省事：DDL 不用手动 commit
        cursorclass=pymysql.cursors.Cursor,
    )


def execute_sql(cursor, sql: str, silent: bool = True):
    try:
        cursor.execute(sql)
        return True
    except Exception as e:
        if not silent:
            print(f"❌ SQL Error: {str(e)[:200]}")
            print(f"   SQL: {sql[:200]}...")
        return False


def init_db(cursor, db_name: str):
    db_name = _safe_ident(db_name)
    print(f"\n📦 初始化数据库: [{db_name}] ...")
    execute_sql(cursor, f"CREATE DATABASE IF NOT EXISTS `{db_name}` DEFAULT CHARSET utf8mb4", silent=False)
    execute_sql(cursor, f"USE `{db_name}`", silent=False)


def add_dim_tables(cursor, prefix: str, count: int):
    """
    每个库生成大量维表/字典表（企业里最常见），让表数量快速上去。
    """
    prefix = _safe_ident(prefix)
    for i in range(count):
        t = f"{prefix}_dim_{i:03d}"
        t = _safe_ident(t)
        sql = f"""
        CREATE TABLE IF NOT EXISTS `{t}` (
          id BIGINT PRIMARY KEY,
          code VARCHAR(50),
          name VARCHAR(100),
          ext_json JSON,
          create_time DATETIME
        ) COMMENT='自动生成维表_{t}'
        """
        execute_sql(cursor, sql)


def count_tables(cursor, db_name: str) -> int:
    db_name = _safe_ident(db_name)
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s",
        (db_name,),
    )
    return int(cursor.fetchone()[0])


# =========================================================
# 1) 构建各业务库
# =========================================================

def build_trade_center(cursor):
    """交易中心：高并发核心，分表 + 维表"""
    db = "corp_trade_center"
    init_db(cursor, db)

    singles = {
        "t_cart": "(id BIGINT PRIMARY KEY, uid BIGINT, sku_id BIGINT, add_time DATETIME)",
        "t_after_sale_reason": "(id INT PRIMARY KEY, reason VARCHAR(50), type INT)",
        "t_freight_template": "(id INT PRIMARY KEY, name VARCHAR(50), calc_mode TINYINT)",
        "t_trade_config": "(cfg_key VARCHAR(50) PRIMARY KEY, cfg_value TEXT)",
    }
    for k, v in singles.items():
        k = _safe_ident(k)
        execute_sql(cursor, f"CREATE TABLE IF NOT EXISTS `{k}` {v} COMMENT='交易杂项表'")

    print(f"   - [分表] 生成订单/明细/支付流水切片：ORDER_SHARDS={ORDER_SHARDS} -> 共 {ORDER_SHARDS*3} 张")
    for i in range(ORDER_SHARDS):
        suffix = f"{i:03d}"
        execute_sql(
            cursor,
            f"CREATE TABLE IF NOT EXISTS `t_order_{suffix}` (oid BIGINT PRIMARY KEY, uid BIGINT, amount DECIMAL(18,2), create_time DATETIME) COMMENT='订单主表_{suffix}'",
        )
        execute_sql(
            cursor,
            f"CREATE TABLE IF NOT EXISTS `t_order_item_{suffix}` (id BIGINT PRIMARY KEY, oid BIGINT, sku_id BIGINT, sku_name VARCHAR(100), qty INT) COMMENT='订单明细_{suffix}'",
        )
        execute_sql(
            cursor,
            f"CREATE TABLE IF NOT EXISTS `t_pay_flow_{suffix}` (flow_id VARCHAR(50) PRIMARY KEY, oid BIGINT, uid BIGINT, status TINYINT, pay_time DATETIME) COMMENT='支付流水_{suffix}'",
        )
        if i % 32 == 31:
            print(f"     ... progress {i+1}/{ORDER_SHARDS}")

    print(f"   - [维表] 生成维表：{DIM_TABLES_PER_DB} 张")
    add_dim_tables(cursor, "trade", DIM_TABLES_PER_DB)


def build_user_center(cursor):
    """用户中心：千万级用户，分表 + 维表"""
    db = "corp_user_center"
    init_db(cursor, db)

    singles = {
        "u_level_def": "(level INT PRIMARY KEY, name VARCHAR(20), discount DECIMAL(4,2))",
        "u_tag_def": "(tag_id INT PRIMARY KEY, tag_name VARCHAR(50), rule_script TEXT)",
        "u_growth_task": "(task_id INT PRIMARY KEY, name VARCHAR(50), point_reward INT)",
    }
    for k, v in singles.items():
        k = _safe_ident(k)
        execute_sql(cursor, f"CREATE TABLE IF NOT EXISTS `{k}` {v}")

    print(f"   - [分表] 生成用户基表/登录日志：USER_SHARDS={USER_SHARDS} -> 共 {USER_SHARDS*2} 张")
    for i in range(USER_SHARDS):
        execute_sql(
            cursor,
            f"CREATE TABLE IF NOT EXISTS `u_user_base_{i:03d}` (uid BIGINT PRIMARY KEY, mobile VARCHAR(15), pwd_hash VARCHAR(100), reg_time DATETIME) COMMENT='用户基表_{i:03d}'",
        )
        execute_sql(
            cursor,
            f"CREATE TABLE IF NOT EXISTS `u_login_log_{i:03d}` (id BIGINT PRIMARY KEY, uid BIGINT, ip VARCHAR(40), ts DATETIME) COMMENT='登录日志_{i:03d}'",
        )
        if i % 32 == 31:
            print(f"     ... progress {i+1}/{USER_SHARDS}")

    print(f"   - [维表] 生成维表：{DIM_TABLES_PER_DB} 张")
    add_dim_tables(cursor, "user", DIM_TABLES_PER_DB)


def build_scm_erp(cursor):
    """供应链/ERP：复杂业务表 + 维表"""
    db = "corp_scm_erp"
    init_db(cursor, db)

    tables = [
        "scm_supplier_base", "scm_supplier_qualification", "scm_supplier_contract",
        "scm_purchase_req", "scm_purchase_order", "scm_purchase_return",
        "scm_wh_def", "scm_wh_zone", "scm_wh_bin",
        "scm_stock_in", "scm_stock_out", "scm_stock_transfer", "scm_stock_check",
        "scm_sku_base", "scm_sku_category", "scm_sku_price_history", "scm_sku_barcode",
    ]
    for t in tables:
        t = _safe_ident(t)
        execute_sql(
            cursor,
            f"""
            CREATE TABLE IF NOT EXISTS `{t}` (
              id BIGINT PRIMARY KEY,
              code VARCHAR(50),
              create_by VARCHAR(20),
              create_time DATETIME,
              status INT,
              memo TEXT
            ) COMMENT='ERP核心表_{t}'
            """,
        )

    print(f"   - [维表] 生成维表：{DIM_TABLES_PER_DB} 张")
    add_dim_tables(cursor, "scm", DIM_TABLES_PER_DB)


def build_marketing(cursor):
    """营销中心：活动规则多 + 维表"""
    db = "corp_marketing"
    init_db(cursor, db)

    tables = {
        "mkt_coupon_template": "优惠券模板",
        "mkt_coupon_send_log": "发券记录",
        "mkt_activity_main": "大促活动主表",
        "mkt_activity_rule": "活动互斥规则",
        "mkt_seckill_session": "秒杀场次",
        "mkt_seckill_sku": "秒杀商品配置",
        "mkt_live_room": "直播间配置",
        "mkt_live_goods": "直播带货商品",
    }
    for t, comment in tables.items():
        t = _safe_ident(t)
        execute_sql(
            cursor,
            f"CREATE TABLE IF NOT EXISTS `{t}` (id BIGINT PRIMARY KEY, name VARCHAR(100), start_time DATETIME, end_time DATETIME, status INT) COMMENT='{comment}'",
        )

    print(f"   - [维表] 生成维表：{DIM_TABLES_PER_DB} 张")
    add_dim_tables(cursor, "mkt", DIM_TABLES_PER_DB)


def build_data_warehouse(cursor):
    """数仓/日志：按周分表 + 维表"""
    db = "corp_data_log"
    init_db(cursor, db)

    print(f"   - [分表] 生成系统日志：LOG_WEEKS={LOG_WEEKS} -> 共 {LOG_WEEKS*2} 张")
    # 表名里别用 '-'，用 W + 3位数
    for w in range(1, LOG_WEEKS + 1):
        week = f"2025W{w:03d}"
        execute_sql(
            cursor,
            f"CREATE TABLE IF NOT EXISTS `log_api_access_{week}` (id BIGINT PRIMARY KEY, url VARCHAR(200), latency INT, ts DATETIME) COMMENT='API访问日志_{week}'",
        )
        execute_sql(
            cursor,
            f"CREATE TABLE IF NOT EXISTS `log_err_report_{week}` (id BIGINT PRIMARY KEY, err_code VARCHAR(20), stack TEXT, ts DATETIME) COMMENT='错误日志_{week}'",
        )
        if w % 26 == 0:
            print(f"     ... progress {w}/{LOG_WEEKS}")

    print(f"   - [维表] 生成维表：{DIM_TABLES_PER_DB} 张")
    add_dim_tables(cursor, "log", DIM_TABLES_PER_DB)


# =========================================================
# 2) main
# =========================================================

def main():
    try:
        conn = get_connection()
        cursor = conn.cursor()
    except Exception as e:
        print(f"❌ 连接失败，请检查 .env 配置: {e}")
        return

    print("🚀 启动 [企业级规模] 数据库构建程序")
    print(f"   - MYSQL_HOST={MYSQL_HOST}:{MYSQL_PORT}, user={MYSQL_USER}, connect_db={MYSQL_CONNECT_DB}")
    print(f"   - ORDER_SHARDS={ORDER_SHARDS}, USER_SHARDS={USER_SHARDS}, LOG_WEEKS={LOG_WEEKS}, DIM_TABLES_PER_DB={DIM_TABLES_PER_DB}")
    print(f"   - start_time={datetime.now().isoformat(timespec='seconds')}")

    # 构建 5 个库
    build_trade_center(cursor)
    build_user_center(cursor)
    build_scm_erp(cursor)
    build_marketing(cursor)
    build_data_warehouse(cursor)

    # 统计表数量
    dbs = ["corp_trade_center", "corp_user_center", "corp_scm_erp", "corp_marketing", "corp_data_log"]
    print("\n" + "=" * 60)
    print("✅ 构建完成！统计信息：")
    total = 0
    for db in dbs:
        n = count_tables(cursor, db)
        total += n
        print(f"   - {db}: {n} tables")
    print(f"   - TOTAL: {total} tables")
    print("=" * 60)

    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()
