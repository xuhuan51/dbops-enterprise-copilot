import aiomysql
from contextlib import asynccontextmanager
from typing import Optional, List, Tuple, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.load import dumps, loads
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    CheckpointTuple,
)

class AsyncMySQLSaver(BaseCheckpointSaver):
    def __init__(self, pool: aiomysql.Pool):
        super().__init__()
        self.pool = pool
        print("✅ AsyncMySQLSaver initialized (Fast Mode).")

    @asynccontextmanager
    async def _get_conn(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                yield cur

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        # 获取最新的一条 Checkpoint
        sql = "SELECT thread_ts, parent_ts, checkpoint, metadata FROM checkpoints WHERE thread_id = %s ORDER BY thread_ts DESC LIMIT 1"

        async with self._get_conn() as cur:
            await cur.execute(sql, (thread_id,))
            row = await cur.fetchone()
            if not row:
                return None

            thread_ts, parent_ts, checkpoint_blob, metadata_blob = row

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

        # 序列化
        checkpoint_blob = dumps(checkpoint).encode("utf-8")
        metadata_blob = dumps(metadata).encode("utf-8")

        # 写入 checkpoints 表
        sql = """
              INSERT INTO checkpoints (thread_id, thread_ts, parent_ts, checkpoint, metadata)
              VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY 
              UPDATE 
                  parent_ts = VALUES(parent_ts), 
                  checkpoint = VALUES(checkpoint), 
                  metadata = VALUES(metadata)
              """

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (thread_id, thread_ts, parent_ts, checkpoint_blob, metadata_blob))
            await conn.commit()

        return {"configurable": {"thread_id": thread_id, "thread_ts": thread_ts}}

    # 🔥🔥 核心修复：补上这个方法，防止 NotImplementedError 报错 🔥🔥
    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: List[Tuple[str, Any]],
        task_id: str,
    ) -> None:
        """
        LangGraph 新版本必须要求实现此方法。
        这里我们做一个空实现（Pass），既能防止程序崩溃，又不需要创建额外的 checkpoint_writes 表。
        """
        # 如果未来需要完整的"时间旅行"调试功能，可以在这里把 writes 写入数据库
        pass

    async def alist(self, config, *, filter=None, before=None, limit=None):
        async for _ in []: yield _