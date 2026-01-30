from __future__ import annotations
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.modules.retrieval.schema_retriever import fetch_table_metadata

from app.modules.retrieval.schema_retriever import SchemaRetriever
from app.modules.retrieval.knowledge_retriever import KnowledgeRetriever


class RetrievalOrchestrator:
    """
    双塔检索编排层（总调度器）

    职责：
    1) 接收 Router 的战术指令（needs_schema/needs_knowledge + schema_query/knowledge_keywords）
    2) 并行执行 SchemaRetriever 与 KnowledgeRetriever
    3) 融合 + Rescue（knowledge 强制 required_tables 时补齐 schema）
    4) 输出 schema_context / knowledge_context + 原始结构化结果
    """

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=10)
        self.schema = SchemaRetriever()
        self.knowledge = KnowledgeRetriever(executor=self._executor)

    async def retrieve_all(
        self,
        *,
        schema_query: str,
        knowledge_keywords: Optional[List[str]] = None,
        knowledge_query: Optional[str] = None,   # 通常就是原 question
        needs_schema: bool = True,
        needs_knowledge: bool = False,
        schema_top_k: int = 5,
        knowledge_each_top_k: int = 6,
    ) -> Dict[str, Any]:
        start_time = time.perf_counter()

        schema_q = (schema_query or "").strip()
        know_q = (knowledge_query or "").strip()
        kws = (knowledge_keywords or [])[:5]

        logger.info(
            f"🚀 [Retrieve] Start | schema={needs_schema} know={needs_knowledge} "
            f"| schema_q={schema_q[:60]}... | kws={kws}",
        )

        tasks: Dict[str, Any] = {}

        # 并行启动
        if needs_schema:
            tasks["schema"] = asyncio.create_task(self.schema.search_tables(schema_q, top_k_final=schema_top_k))

        if needs_knowledge:
            tasks["knowledge"] = asyncio.create_task(
                self.knowledge.search_knowledge(
                    knowledge_keywords=kws,
                    knowledge_query=know_q,
                    each_top_k=knowledge_each_top_k,
                )
            )

        if not tasks:
            return self._empty_result()

        results_list = await asyncio.gather(*tasks.values(), return_exceptions=True)

        schema_results: List[Dict[str, Any]] = []
        knowledge_hits: List[Dict[str, Any]] = []

        for key, res in zip(list(tasks.keys()), results_list):
            if isinstance(res, Exception):
                logger.error(f"[Retrieve] task {key} failed: {res}")
                continue
            if key == "schema":
                schema_results = res or []
            elif key == "knowledge":
                knowledge_hits = res or []

        # 融合补齐：保留你原来的 rescue 思路
        if needs_schema and needs_knowledge and knowledge_hits:
            schema_results = await self._fuse_and_rescue(schema_results, knowledge_hits)

        formatted = self._format_to_string(schema_results, knowledge_hits)

        total_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            f"✅ [Retrieve] Done {total_ms:.0f}ms | Schema:{len(schema_results)} | Know:{len(knowledge_hits)}"
        )

        return {
            "schema_context": formatted["schema"],
            "knowledge_context": formatted["knowledge"],
            "candidate_tables": schema_results,
            "raw_knowledge": knowledge_hits,
        }

    async def _fuse_and_rescue(self, schema_results: List[Dict], knowledge_hits: List[Dict]) -> List[Dict]:
        """
        融合逻辑：检查 Knowledge 中是否有 required_tables，如果有且 Schema 没搜到，强制补齐。
        """
        existing_tables = {item.get("table_name") or item.get("logical_table") for item in schema_results}
        existing_tables = {t for t in existing_tables if t}

        forced_tables = set()
        for hit in knowledge_hits:
            reqs = hit.get("required_tables", []) if isinstance(hit, dict) else []
            for t in (reqs or []):
                if t:
                    forced_tables.add(t)

        missing_tables = [t for t in forced_tables if t not in existing_tables]

        if missing_tables:
            logger.info(f"🛟 [Rescue] Knowledge 强制补回缺失表: {missing_tables}")
            try:
                supplementary_schema = await fetch_table_metadata(missing_tables)
                if supplementary_schema:
                    schema_results.extend(supplementary_schema)
            except Exception as e:
                logger.error(f"❌ [Rescue Failed] 补齐表失败: {e}")

        return schema_results

    def _format_to_string(self, schema_list: List[Dict], know_list: List[Dict]) -> Dict[str, str]:
        # =======================
        # Schema
        # =======================
        schema_strs: List[str] = []

        for item in schema_list or []:
            # 优先 DDL，其次 text，最后 Table:xxx
            content = item.get("ddl") or item.get("text")

            if not content or not str(content).strip():
                tb = item.get("table_name") or item.get("logical_table") or item.get("full_name")
                content = f"Table: {tb}" if tb else ""

            content = str(content).strip()
            if content:
                schema_strs.append(content)

        schema_ctx = "\n\n".join(schema_strs).strip()
        if not schema_ctx:
            schema_ctx = "(无相关表结构)"

        # =======================
        # Knowledge
        # =======================
        know_strs: List[str] = []
        for item in know_list or []:
            if not isinstance(item, dict):
                continue
            term = item.get("term")
            defn = item.get("definition")
            if term and defn:
                line = f"- **{term}**: {defn}"

                syns = item.get("synonyms")
                if syns:
                    line += f" (同义词: {', '.join(syns)})"

                reqs = item.get("required_tables")
                if reqs:
                    line += f" [关联表: {', '.join(reqs)}]"

                know_strs.append(line)

        know_ctx = "\n".join(know_strs).strip()
        if not know_ctx:
            know_ctx = "(无相关业务知识)"

        return {"schema": schema_ctx, "knowledge": know_ctx}

    def _empty_result(self) -> Dict[str, Any]:
        return {
            "schema_context": "",
            "knowledge_context": "",
            "candidate_tables": [],
            "raw_knowledge": [],
        }


# 单例导出（graph 用这个）
retriever = RetrievalOrchestrator()
