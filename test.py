import os
import sqlite3

import pandas as pd

# 获取当前脚本所在目录的绝对路径
base_dir = os.path.dirname(os.path.abspath(__file__))
# 拼接数据库路径（根据你的截图，中间应该有 databases 文件夹）
db_path = os.path.join(base_dir, 'data/bird/databases/financial/financial.sqlite')

print(f"正在尝试打开: {db_path}")

if not os.path.exists(db_path):
    print("❌ 错误：文件真的不存在，请检查路径！")
else:
    conn = sqlite3.connect(db_path)
    print("✅ 成功打开数据库！")

# 查看库里所有的表名
tables = pd.read_sql("SELECT name FROM sqlite_master WHERE type='table'", conn)
print("数据库里的表：\n", tables)

# 查看某一号表的具体数据
df = pd.read_sql("SELECT * FROM client LIMIT 5", conn)
print("\nClient 表的前5行：\n", df)