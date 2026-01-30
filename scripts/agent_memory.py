import asyncio
import os
import sys
import aiomysql

# 1. 确保能导入项目配置
sys.path.append(os.getcwd())

try:
    from app.core.config import settings

    print(f"✅ 已加载配置: Host={settings.PROXY_HOST}, Port={settings.PROXY_PORT}, DB={settings.PROXY_LOGIC_DB}")
except ImportError:
    print("❌ 无法导入 app.core.config，请确保将此脚本放在项目根目录运行。")
    sys.exit(1)

# ================= SQL 定义 =================
DDL_STATEMENTS = [
    # 1. checkpoints 表 (对应 AsyncMySQLSaver.aput)
    """
    CREATE TABLE IF NOT EXISTS checkpoints
    (
        thread_id
        VARCHAR
    (
        255
    ) NOT NULL COMMENT '会话ID',
        thread_ts VARCHAR
    (
        255
    ) NOT NULL COMMENT '当前步骤的版本号/时间戳',
        parent_ts VARCHAR
    (
        255
    ) DEFAULT NULL COMMENT '父节点版本号',
        checkpoint LONGBLOB COMMENT '核心状态数据 (二进制存储)',
        metadata LONGBLOB COMMENT '元数据 (二进制存储)',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
        PRIMARY KEY
    (
        thread_id,
        thread_ts
    )
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LangGraph 状态快照表';
    """,

    # 2. checkpoint_writes 表 (LangGraph 标准结构)
    """
    CREATE TABLE IF NOT EXISTS checkpoint_writes
    (
        thread_id
        VARCHAR
    (
        255
    ) NOT NULL,
        thread_ts VARCHAR
    (
        255
    ) NOT NULL,
        task_id VARCHAR
    (
        255
    ) NOT NULL COMMENT '任务ID',
        idx INT NOT NULL COMMENT '写入顺序',
        channel VARCHAR
    (
        255
    ) NOT NULL COMMENT '通道名称',
        type VARCHAR
    (
        255
    ) COMMENT '数据类型',
        value LONGBLOB COMMENT '写入的具体值',
        PRIMARY KEY
    (
        thread_id,
        thread_ts,
        task_id,
        idx
    )
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LangGraph 中间写入表';
    """
]


async def init_tables():
    print(f"\n🔌 正在连接数据库 (Proxy)...")

    try:
        # 直接连接 Proxy
        conn = await aiomysql.connect(
            host=settings.PROXY_HOST,
            port=settings.PROXY_PORT,
            user=settings.PROXY_USER,
            password=settings.PROXY_PASSWORD,
            db=settings.PROXY_LOGIC_DB,  # 连你的 corp_marketing (或配置的库名)
            autocommit=True
        )

        async with conn.cursor() as cur:
            for sql in DDL_STATEMENTS:
                # 提取表名用于打印
                table_name = sql.split("TABLE IF NOT EXISTS")[1].split("(")[0].strip()
                print(f"🔨 正在创建/检查表: {table_name} ...")
                await cur.execute(sql)
                print(f"✅ {table_name} 就绪!")

        conn.close()
        print("\n🎉 初始化完成！Memory 记忆功能已就绪。")

    except Exception as e:
        print(f"\n❌ 初始化失败: {e}")
        print("💡 提示: 请检查 Proxy 是否已启动 (docker ps) 且配置正确。")


if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(init_tables())