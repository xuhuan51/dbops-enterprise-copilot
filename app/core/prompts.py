# ==================================================
# 检索模块 (Retrieval) 专用提示词
# ==================================================
RETRIEVAL_JUDGE_TEMPLATE = """
你是一个严谨的数据架构师。你的任务是从给定的 [候选数据表] 中，筛选出回答 [用户问题] 所需的**最小、最精准**的表集合。

### 核心原则 (Domain Principles)
{domain_rules}

### 参考思维范例 (Few-Shot Examples)
{few_shot_examples}

---

**Current Task**:
**User Question**: "{query}"
**Candidate Tables**: 
{candidates}

**输出格式 (JSON Only)**:
{{
    "status": "PASS" | "COMPLEMENT" | "ASK_USER",
    "selected_tables": ["table1", "table2"],
    "reason": "思考过程",
    "search_keywords": ["kw1", "kw2"],
    "clarify_question": "..."
}}
"""


# 专门用于 Schema 增强的 Prompt
SCHEMA_ENRICH_PROMPT = """
你是一个数据专家。请分析以下数据库表结构和样本数据，提取关键元数据。

# Schema
DB: {{db}}
Table: {{table}}
Comment: {{comment}}
Columns: {{columns}}

# Samples (Desensitized)
{{samples}}

# Requirements
请输出 JSON（只输出 JSON，不要解释），包含：
- domain: trade/user/scm/marketing/log/other
- summary: 一句话中文描述表业务含义
- join_keys: 适合关联的字段名列表（必须来自 Columns）
- time_cols: 时间字段名列表（必须来自 Columns）
- metric_cols: 可聚合数值指标字段名列表（必须来自 Columns）
- synonyms: 黑话/同义词列表（如 "GMV=成交额=交易额"）
- risk: sensitive 或 none

严格要求：
1) join_keys/time_cols/metric_cols 必须是 Columns 里真实存在的列名。
2) 如果不确定就输出空数组。
"""



TABLE_CARD_GOVERNANCE_PROMPT = """
你是一个企业级数据治理专家。请根据提供的数据库表结构（Schema）和样本数据（Samples），生成该表的【数据资产卡片（TableCard）】信息。

# 输入信息
- DB: {db}
- Table: {logical_table} (Logical) / {table} (Physical)
- Domain: {domain}
- Comment: {table_comment}
- Columns: 
{columns_desc}
- Samples (已脱敏): 
{samples}

# 任务要求
请分析表意图，输出严格的 JSON 格式（不要 Markdown 代码块），必须包含以下字段：

1. "summary": (string) 一句话描述表的业务含义（必须非空，精炼）。
2. "synonyms": (string[]) 业务黑话、同义词、搜索关键词。**必须至少提供 5 个**（例如：["订单主表", "交易流水", "GMV来源", "t_order", "OrderMaster"]）。
3. "table_type": (string) 必须是以下之一：["fact" (事实表), "dim" (维表), "bridge" (关联表), "log" (日志表), "config" (配置表), "unknown"]。
4. "entity_tags": (string[]) 涉及的核心实体，从集合中选（可多选）：["user", "order", "pay", "sku", "supplier", "activity", "log", "inventory", "unknown"]。
5. "risk_level": (string) 敏感等级，必须是以下之一：["normal" (普通), "sensitive" (敏感/PII), "forbidden" (核心机密)]。
6. "domain_suggestion": (string) 你认为该表最准确的归属域。
7. "domain_confidence": (float) 置信度 0~1。

# 注意事项
- 如果表注释为空，请根据列名和样本强行推断 summary。
- 风险判定：包含手机号、身份证、密码哈希的为 sensitive。
- 仅仅输出 JSON 对象，不要包含任何其他解释。
"""



INTENT_PROMPT = """
你是一个意图识别专家。请判断用户输入的内容属于以下哪类：

1. data_query: 用户想要查询数据库中的业务数据（如：统计、列表、金额、数量等）。
2. sensitive: 用户询问敏感信息（如：密码、工资、密钥、身份证）。
3. non_data: 闲聊、问候、或者询问天气等与数据库无关的问题。

用户输入: "{question}"
"""

GEN_SQL_PROMPT = """
你是一个精通 MySQL 的数据专家。请基于给定的 Schema 回答用户问题。

[候选表 Schema]:
{schema_context}

[用户问题]: "{question}"

{error_context}

### 严格约束：
1. **只能** 使用[候选表 Schema]中提供的表和字段。严禁臆造字段！
2. 如果候选表不足以回答问题（例如缺关联表），请将 confidence 打低分 (< 0.5)。
3. 输出标准 SQL，不要包含 Markdown 格式（```sql ... ```）。
"""

ERROR_CLASSIFY_PROMPT = """
你是一个数据库错误分析师。
[SQL]: {sql}
[Error]: {error_msg}

请分析错误类型并提供补救建议：

1. MISSING_COLUMN: 报错 'Unknown column'。说明候选表中缺字段，或者引用了不存在的列。
   -> 提取该列名（如 'region'）。
2. MISSING_TABLE: 报错 'Table doesn't exist' 或语义上无法关联。
   -> 提取缺少的实体名（如 'user_dim'）。
3. WRONG_TABLE: 语义错误，选错了表。
4. NON_FIXABLE: 语法严重错误，或无法通过补搜解决。

请提取用于去知识库补搜的关键词 (search_keywords)。
"""