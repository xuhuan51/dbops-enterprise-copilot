import aiomysql
from contextlib import asynccontextmanager
from typing import Optional

from langchain_core.runnables import RunnableConfig
# 🔥 1. 使用函数式序列化，彻底绕开 SerializerCompat 类
from langchain_core.load import dumps, loads
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    CheckpointTuple,
)


class AsyncMySQLSaver(BaseCheckpointSaver):
    def __init__(self, pool: aiomysql.Pool):
        # 🔥 2. 不传 serde 参数，让父类那一套彻底失效
        super().__init__()
        self.pool = pool
        print("✅ AsyncMySQLSaver initialized (Clean Mode).")

    @asynccontextmanager
    async def _get_conn(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                yield cur

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        # 注意：这里只取最新的一条
        sql = "SELECT thread_ts, parent_ts, checkpoint, metadata FROM checkpoints WHERE thread_id = %s ORDER BY thread_ts DESC LIMIT 1"

        async with self._get_conn() as cur:
            await cur.execute(sql, (thread_id,))
            row = await cur.fetchone()
            if not row:
                return None

            thread_ts, parent_ts, checkpoint_blob, metadata_blob = row

            # 🔥 3. 读的时候 decode + loads
            return CheckpointTuple(
                config,
                loads(checkpoint_blob.decode("utf-8")),
                loads(metadata_blob.decode("utf-8")),
                {"configurable": {"thread_id": thread_id, "thread_ts": thread_ts}},
                parent_ts,
            )

    async def aput(self, config, checkpoint, metadata, new_versions):
        thread_id = config["configurable"]["thread_id"]
        thread_ts = checkpoint["id"]
        parent_ts = config["configurable"].get("thread_ts")

        # 🔥 4. 写的时候 dumps + encode
        checkpoint_blob = dumps(checkpoint).encode("utf-8")
        metadata_blob = dumps(metadata).encode("utf-8")

        # 🔥 5. 优化后的 SQL (ON DUPLICATE KEY UPDATE)
        sql = """
              INSERT INTO checkpoints (thread_id, thread_ts, parent_ts, checkpoint, metadata)
              VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY \
              UPDATE \
                  parent_ts = \
              VALUES (parent_ts), checkpoint = \
              VALUES (checkpoint), metadata = \
              VALUES (metadata) \
              """

        # 🔥 6. 显式 commit (关键修复)
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (thread_id, thread_ts, parent_ts, checkpoint_blob, metadata_blob))
            await conn.commit()

        return {"configurable": {"thread_id": thread_id, "thread_ts": thread_ts}}

    async def alist(self, config, *, filter=None, before=None, limit=None):
        async for _ in []: yield _