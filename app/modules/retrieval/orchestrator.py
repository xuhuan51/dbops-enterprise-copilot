import asyncio
from typing import Dict, Any, List, Optional

from app.core.logger import logger

# 引入我们更新后的 Retriever
from app.modules.retrieval.schema.retriever import SchemaRetriever
from app.modules.retrieval.knowledge.retriever import KnowledgeRetriever

# 🔥 引入 V3 图服务 (单例)
from app.modules.retrieval.graph.service import graph_service


class RetrievalOrchestrator:
    """
    RAG 编排层：Schema (左塔) + Knowledge (右塔) + Graph (中塔/连接层)
    """

    def __init__(self):
        # 初始化双塔检索器
        self.schema_retriever = SchemaRetriever()
        # knowledge_retriever 内部会有线程池处理 Milvus IO
        self.knowledge_retriever = KnowledgeRetriever()

    async def retrieve_context(self, query: str, db_id: str) -> str:
        """
        全流程检索入口，返回组装好的 Prompt Context 字符串。

        Args:
            query: 用户的自然语言问题 (e.g. "计算加州学校的平均分")
            db_id: 目标数据库 ID (e.g. "california_schools")

        Returns:
            str: 包含 Schema、Join Paths 和 Business Rules 的格式化文本
        """
        if not db_id:
            logger.warning("⚠️ [Orchestrator] Missing db_id! Retrieval might be inaccurate (cross-db noise).")

        # ==========================================
        # 1. 并行检索 Schema 和 Knowledge
        # ==========================================
        # 针对 BIRD 数据集，Top-K 稍微放宽一点，依赖 LLM 筛选
        schema_k = 15
        knowledge_k = 5

        # 使用 asyncio.gather 并发执行 IO 密集型任务
        # 注意：这里的 retrieve 方法必须支持 db_id 参数
        schema_task = self.schema_retriever.retrieve(query, db_id, top_k=schema_k)

        # 知识检索主要靠 Query 匹配规则，关键词设为 None 让内部处理
        knowledge_task = self.knowledge_retriever.search_knowledge(
            knowledge_keywords=None,
            knowledge_query=query,
            db_id=db_id,
            each_top_k=knowledge_k
        )

        results = await asyncio.gather(schema_task, knowledge_task)

        schema_tables: List[Dict] = results[0]
        rules: List[str] = results[1]

        # ==========================================
        # 2. 图搜索：自动补全 JOIN 路径
        # ==========================================
        # 提取检索到的表名
        found_table_names = [t.get('table_name') for t in schema_tables if t.get('table_name')]

        join_paths = []
        # 只有找到两张及以上的表，才需要计算 Join 路径
        if db_id and len(found_table_names) >= 2:
            try:
                # 🔥 调用 V3 图服务，利用 Steiner Tree 算法寻找最佳路径
                join_paths = graph_service.search_join_path(db_id, found_table_names)

                if join_paths:
                    logger.info(f"🕸️ [Graph] Found {len(join_paths)} join paths for {found_table_names}")
                else:
                    logger.debug(f"🕸️ [Graph] No join path found (Tables might be isolated).")
            except Exception as e:
                logger.error(f"❌ [Graph] Path search failed: {e}")

        # ==========================================
        # 3. 组装 Context (Rich Formatting)
        # ==========================================
        context_parts = []

        # --- Part A: Database Schema ---
        if schema_tables:
            context_parts.append("【Database Schema】")
            for t in schema_tables:
                table_name = t.get('table_name', 'Unknown')
                context_parts.append(f"Table: {table_name}")

                for c in t.get('columns', []):
                    # 格式: - col_name (TYPE) [PK,FK]: comment (Samples: val1, val2)
                    c_name = c.get('name', 'unknown')
                    c_type = c.get('type', 'UNKNOWN')
                    c_str = f"  - {c_name} ({c_type})"

                    # 加 Key 标记 (对 LLM 理解表结构至关重要)
                    flags = []
                    if c.get('is_pk'): flags.append("PK")
                    if c.get('is_fk'): flags.append("FK")
                    if flags:
                        c_str += f" [{','.join(flags)}]"

                    # 加 Comment
                    comment = c.get('comment') or c.get('doc_text')  # 兼容不同字段名
                    # 如果 comment 太长或者是生成的 doc_text，可能需要精简，这里暂且不放，避免干扰

                    # 加 Samples (BIRD 高分秘籍：让 LLM 看到真实数据格式)
                    samples = c.get('samples', [])
                    if samples:
                        # 截断每个样本的长度，防止超长字符串
                        safe_samples = [str(s)[:50] for s in samples[:3]]
                        c_str += f" (Samples: {', '.join(safe_samples)})"

                    context_parts.append(c_str)
            context_parts.append("")  # 空行分隔

        # --- Part B: Suggested Joins (From Graph) ---
        if join_paths:
            context_parts.append("【Suggested Relationship Paths】")
            context_parts.append("Use these join conditions to connect tables correctly:")
            for p in join_paths:
                context_parts.append(f"- {p}")
            context_parts.append("")

        # --- Part C: Business Rules (From Knowledge) ---
        if rules:
            context_parts.append("【Business Rules & Evidence】")
            context_parts.append("Follow these rules to interpret the data:")
            for r in rules:
                context_parts.append(f"- {r}")
            context_parts.append("")

        final_context = "\n".join(context_parts)

        # 记录日志方便调试
        logger.info(
            f"✅ [Orchestrate] Built context with {len(schema_tables)} tables, {len(join_paths)} paths, {len(rules)} rules.")

        return final_context