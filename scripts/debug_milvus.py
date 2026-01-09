# debug_milvus.py
from app.core.config import settings
from pymilvus import connections, Collection

# 1. 连接 Milvus
connections.connect(alias="default", host=settings.MILVUS_HOST, port=settings.MILVUS_PORT)

# 2. 获取集合
col = Collection(settings.MILVUS_COLLECTION)
col.load() # 加载到内存

print(f"📊 当前集合内总条数: {col.num_entities}")

# 3. 随便查一条看看
res = col.query(expr="full_name != ''", output_fields=["db", "logical_table", "full_name"], limit=1)

if res:
    print("✅ 抽样数据:", res[0])
    if res[0]['db'] == 'dbops_proxy':
        print("🎉 状态完美！数据是新的！(请检查 Agent 是否重启)")
    else:
        print(f"❌ 状态异常！数据库名是: {res[0]['db']} (应该是 dbops_proxy)")
else:
    print("❌ 集合是空的！Agent 啥也查不到！")