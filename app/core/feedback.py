# app/core/feedback.py
import json
import os
import datetime
from app.core.rag_store import rag_store
from app.core.logger import logger

# 定义一个专门存“学习到的SQL”的文件路径
LEARNED_SQL_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data",
                                "golden_sqls_learned.jsonl")


class FeedbackService:
    """
    负责处理用户反馈，实现“自我进化”
    """

    def record_good_case(self, question: str, sql: str, user_id: str = "human_expert"):
        """
        核心方法：当用户点赞时调用
        """
        logger.info(f"👍 [Feedback] 收到好评! 正在学习: {question}")

        # 1. 【热存储】写入 Milvus (立刻生效)
        # 这样下一秒其他用户问类似问题，就能搜到了
        try:
            rag_store.add_verified_sql_realtime(
                question=question,
                sql=sql,
                desc=f"User Verified ({datetime.date.today()})"
            )
        except Exception as e:
            logger.error(f"❌ 写入 Milvus 失败: {e}")

        # 2. 【冷备份】写入 JSONL 文件 (持久化)
        # 即使 Milvus 挂了，数据还在文件里
        try:
            record = {
                "ts": datetime.datetime.now().isoformat(),
                "user": user_id,
                "question": question,
                "sql": sql,
                "source": "user_feedback"
            }
            # Append 模式写入
            with open(LEARNED_SQL_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

            logger.info(f"✅ 已备份到文件: {LEARNED_SQL_FILE}")

        except Exception as e:
            logger.error(f"❌ 写入文件失败: {e}")


feedback_service = FeedbackService()