import pymysql
import re
import os
from app.core.config import settings


# ==========================================
# 1. 测试数据库直连 (Live DB Check)
# ==========================================
def test_db_columns(table_name="t_order"):
    print(f"\n🧪 [测试 1] 正在尝试从数据库获取表 '{table_name}' 的列...")
    print(f"   - 目标: {settings.PROXY_HOST}:{settings.PROXY_PORT} (User: {settings.PROXY_USER})")
    print(f"   - 库名: {settings.PROXY_LOGIC_DB}")

    try:
        conn = pymysql.connect(
            host=settings.PROXY_HOST,
            port=settings.PROXY_PORT,
            user=settings.PROXY_USER,
            password=settings.PROXY_PASSWORD,
            database=settings.PROXY_LOGIC_DB,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5
        )
        print("   ✅ 连接建立成功！")

        with conn.cursor() as cur:
            # 1. 检查当前连的是哪个库
            cur.execute("SELECT DATABASE()")
            current_db = cur.fetchone()
            print(f"   ℹ️  当前连接的库: {current_db}")

            # 2. 检查表是否存在 (SHOW TABLES)
            print(f"   QUERY: SHOW TABLES LIKE '{table_name}'")
            cur.execute(f"SHOW TABLES LIKE '{table_name}'")
            exists = cur.fetchone()
            if not exists:
                print(f"   ❌ 致命错误: 逻辑表 '{table_name}' 在当前库中不存在！")
                print("      可能的原因为：")
                print("      1. 你连到了 3306 (物理库) 而不是 3307 (Proxy)？")
                print("      2. ShardingSphere 的逻辑表名配置错了吗？")
                print("      3. 还是说表名是 t_order_0 而不是 t_order？")
                return

            # 3. 核心测试: SHOW COLUMNS
            sql = f"SHOW COLUMNS FROM `{table_name}`"
            print(f"   QUERY: {sql}")
            cur.execute(sql)
            results = cur.fetchall()

            if not results:
                print("   ⚠️  警告: SQL 执行成功，但返回结果为空！(表里没列？)")
            else:
                print(f"   ✅ 成功获取 {len(results)} 个列:")
                cols = [row['Field'] for row in results]
                print(f"      -> {cols}")

        conn.close()

    except Exception as e:
        print(f"   ❌ 数据库连接或查询失败: {e}")


# ==========================================
# 2. 测试 DDL 正则解析 (Regex Check)
# ==========================================
def test_regex_parsing():
    print(f"\n🧪 [测试 2] 正在测试 DDL 正则解析 (双保险机制)...")

    # 模拟一段从 RAG 拿回来的脏文本
    mock_text = """
    这是 t_order 的表结构：
    CREATE TABLE `t_order` (
      `oid` bigint(20) NOT NULL COMMENT '订单ID',
      `user_id` int(11) DEFAULT NULL,
      amount decimal(10,2),
      create_time datetime,
      PRIMARY KEY (`oid`)
    ) ENGINE=InnoDB;
    """

    print("   📄 模拟文本片段:")
    print(mock_text.strip()[:100] + "...")

    columns = []
    lines = mock_text.split('\n')
    for line in lines:
        line = line.strip()
        if not line: continue
        # 忽略 DDL 关键字
        if line.upper().startswith(("CREATE", "TABLE", ")", "PRIMARY", "KEY", "CONSTRAINT", "UNIQUE", "--", "ENGINE")):
            continue

        # 你的正则逻辑
        match = re.match(r"^[`']?([a-zA-Z0-9_]+)[`']?", line)
        if match:
            col = match.group(1)
            if col.upper() not in ["AND", "OR", "ON", "IN", "NOT", "NULL", "DEFAULT", "COMMENT"]:
                columns.append(col)

    if columns:
        print(f"   ✅ 解析成功，提取列: {columns}")
        if "oid" in columns and "amount" in columns:
            print("      -> 关键列提取正确。")
    else:
        print("   ❌ 解析失败: 没提取到任何列。请检查正则 `_extract_columns_from_ddl`。")


if __name__ == "__main__":
    test_db_columns("t_order")  # 换成你实际失败的表名，比如 u_user_base
    test_regex_parsing()