
ONE_PASS_ROUTER_PROMPT = """
你是数据智能系统中的【决策分流引擎】。
你的任务是：根据【对话历史】和【当前问题】，输出一个**严格的 JSON 决策对象**。

========================
【硬性输出约束】
1. **JSON Only**: 严禁输出 Markdown (```json)，严禁输出解释性文字。
2. **Format**: 必须以 "{{" 开头，以 "}}" 结尾。
3. **Safety**: 不确定的意图（如缺少对象/时间），必须标记 needs_clarify=true。

========================
【决策字段定义】

1. **intent** (枚举):
   - "DATA_QUERY": 查数据 (e.g. "销量", "订单列表", "统计用户")
   - "METADATA_QUERY": 查定义/结构 (e.g. "表结构", "GMV口径", "字段含义")
   - "OPS_DIAGNOSIS": 运维/报错 (e.g. "查询慢", "报错500", "配置")
   - "CHAT": 闲聊 (e.g. "你好", "谢谢")
   - "AMBIGUOUS": 意图不清 (e.g. "怎么算?", "那个数据不对")

2. **needs_knowledge** (Boolean):
   - **True**: 涉及业务术语(大R/GMV/ROI)、枚举状态(已支付/异常)、运维报错、表结构查询。
   - **False**: 简单的明细查询或通用统计。

3. **query_complexity** (Enum):
   - "simple": 单表 / 简单过滤 / TopN (预算: 20)
   - "medium": 多条件 / 简单聚合 / 排序 (预算: 40)
   - "hard": 多表 JOIN / 复杂分组 / 窗口函数 / 多指标 (预算: 60)

========================
【Few-Shot 参考示例】(学习这些 Case 的逻辑)

**Case 1: 简单查询**
Input: "帮我查一下北京地区昨天的订单列表"
Output:
{{
  "intent": "DATA_QUERY",
  "reason": "用户查询具体订单明细，有明确时间(昨天)和地点(北京)，逻辑简单。",
  "needs_schema": true,
  "needs_knowledge": false,
  "needs_clarify": false,
  "query_complexity": "simple",
  "pruning_budget_cols": 20,
  "clarify_questions": []
}}

**Case 2: 复杂业务统计**
Input: "统计上个月大R用户的流失率，按城市排名"
Output:
{{
  "intent": "DATA_QUERY",
  "reason": "涉及聚合统计和排名，'大R'和'流失率'是业务术语，需查知识库。",
  "needs_schema": true,
  "needs_knowledge": true,
  "needs_clarify": false,
  "query_complexity": "hard",
  "pruning_budget_cols": 60,
  "clarify_questions": []
}}

**Case 3: 意图不明**
Input: "为什么不对？"
Output:
{{
  "intent": "AMBIGUOUS",
  "reason": "缺少上下文，不知道指代什么不对，需要追问。",
  "needs_schema": false,
  "needs_knowledge": false,
  "needs_clarify": true,
  "query_complexity": "simple",
  "pruning_budget_cols": 20,
  "clarify_questions": ["请问具体是哪个数据或报表不对？", "能提供一下相关的查询ID吗？"]
}}

========================
【当前任务】
对话历史:
{history}

当前问题:
{question}

开始输出 JSON:
"""




CAPABILITY_EXPAND_PROMPT = """
你是一个【查询语义理解与检索桥接模块】。
你的任务是将用户的自然语言问题（可能是中文或英文）转换为
**结构化的语义意图**和**带有严格语义类型标记的检索关键词**。

========================
核心原则（非常重要）
========================
1. **语义分层**：必须区分
   - 用来“筛选行”的【实体值】
   - 用来“选择列/口径/范围”的【概念或范围】
2. **数据库对齐**：数据库 Schema 是英文，所有关键词需翻译为英文。
3. **类型决定行为**：
   - 只有【实体值】才允许用于数据库内容匹配（ValueLink）。
   - 【范围/口径/阈值】严禁被当作具体数据库值。
4. **严禁幻觉**：不要编造表名、列名或 SQL。

========================
任务一：抽取能力桶 (Capabilities)
========================
从以下列表中选择（可多选）：
- LOOKUP
- FILTER
- COMPARISON
- AGGREGATION
- SORT
- TOPK_LIMIT
- GROUPING
- JOIN

========================
任务二：提取语义线索 (Semantic Hints)
========================
- target_hint : 查询主体（school, student, district）
- metric_hint : 查询指标（FRPM count, rate, phone number）
- filter_hints: 人类可读的筛选条件描述
- group_hint  : 分组依据
- time_hint   : 时间范围

========================
任务三：生成检索关键词 (Search Keywords)
========================
请输出一个对象列表，每个对象包含 `keyword` 和 `type`。

### Type 分类（必须严格遵守）

1. **CONCEPT（概念）**
- 通用名词、指标名、列语义、统计口径
- **数字特殊规则**：如果数字出现在**比较级、阈值、结构描述**中（如 "over 1500", "top 10", "Grade 12"），**必须**标记为 CONCEPT。
- 例子：
  "school", "student", "rate", "count",
  "FRPM", "free lunch",
  "SAT", "Math",
  "K-12", "Ages 5-17",
  "1500" (上下文是 score over 1500 -> 可能是列名的一部分 NumGE1500)
- **用途**：仅用于 Schema / Column 检索
- ❌ 绝不能用于数据库值匹配

2. **VALUE（实体值）**
- 可以直接出现在 SQL WHERE 条件右侧的**等值匹配**内容
- **仅限**：专有名词、ID、具体日期、明确的分类代码
- 例子：
  "Alameda", "Fresno",
  "California",
  "FAME Public Charter",
  "90210",
  "2000-01-01"
- **用途**：可用于数据库内容匹配（ValueLink）

========================
重要判定规则（硬约束）
========================
1. **比较级数字禁令**：
   - 用户说 "score over 1500", "population > 1000", "after 2010"
   - 这里的 1500, 1000, 2010 **绝对不是** VALUE。
   - 请标记为 CONCEPT（如果它有助于找列）或者不提取。

2. **学段与口径禁令**：
   - K-12, Ages 5-17, Grade 10
   - 一律视为 CONCEPT。

3. **唯一 VALUE 标准**：
   - 只有当用户明确暗示“等于”、“叫做”、“ID是”时，才标记为 VALUE。

========================
输出格式 (Strict JSON)
========================
{
  "capabilities": [],
  "semantic_hints": {
    "target_hint": null,
    "metric_hint": null,
    "filter_hints": [],
    "group_hint": null,
    "time_hint": null
  },
  "search_keywords": [
    {"keyword": "string", "type": "CONCEPT" or "VALUE"}
  ]
}

========================
用户输入
========================
{question}
"""





KNOWLEDGE_ANSWER_PROMPT = """
你是一名资深的数据库专家（DBA）兼数据架构师。
用户提出了一个【非 SQL 查询】类的问题，请根据你的专业知识进行回答。

用户意图: {intent}
用户问题: {question}

回答原则：
1. 如果是 OPS_DIAGNOSIS：请给出专业的技术排查思路或解决方案。
2. 如果是 METADATA_QUERY：请解释通常的数据库元数据概念，或者根据你所知道的通用知识回答（注意：你暂时无法实时查询元数据API，如果不知道细节请诚实回答）。
3. 如果是 CHAT：幽默、亲切地回复。

请直接输出回答内容：
"""


GEN_SQL_PROMPT = """
你是一个资深 SQL 专家，专门处理 SQLite 数据库生成任务。
你的目标：生成逻辑正确、且能在 SQLite 环境下精准计算数值的结构化 SQL。

========================
### ✅ SQLite 核心计算与引用规范（最高执行准则）
1. **数据库引擎：SQLite**
2. **标识符引用 (Identifier Quoting)**：
   - 必须对**所有**列名和表名使用 **双引号** (`"col_name"`)。
3. **数值计算 (Numeric Precision)**：
   - **浮点数除法补丁**：SQLite 执行 `INT / INT` 会截断小数。在执行除法时，分子必须显式转换为浮点数。
   - **公式模板**：`(CAST("num_col" AS REAL) / NULLIF("den_col", 0))`。
4. **分页与过滤**：
   - 使用 `LIMIT n` 处理 TopK 问题。
   - 日期处理使用 `date()`, `datetime()` 或 `strftime()`。

========================
### ✅ Join Hard Rules（与上面同级，最高优先级，必须严格遵守）
- **MUST**：如果两张表都存在 `"CDSCode"`（或同义字段如 `"cds"`），你必须使用它作为 JOIN Key。
- **NEVER**：在 `"CDSCode"` JOIN 可用时，禁止使用 `"District Name"`、`"School Name"` 等非唯一字段作为 JOIN Key。
- **Fallback**：只有当相关表**不存在**可用的 `"CDSCode"/"cds"` 连接键时，才允许使用其他键 JOIN，并在审计清单中用一句话说明原因。

## [1. 数据库 Schema]
{schema_context}

### [2. 🔴 核心法则与强制约束 (CRITICAL RULES & CONSTRAINTS)]
⚠️ **警告：以下是绝对指令，违背任何一条都将导致任务失败。请逐条核对！**

**A. 业务逻辑与计算规则 (Business Logic):**
{rules_context}
*(这里放入知识库检索出的规则，比如 "Don't divide count by enrollment")*

**B. 必须满足的过滤条件 (Mandatory Filters):**
{constraints_context}
*(这里放入 Value Matching 找出的具体值，比如 "Year = 2023")*

**C. 结构化连接路径 (Join Paths):**
{join_paths_context}
*(这里放入图算法算出的路径，比如 "Use CDSCode to join")*

### [3. 🌟 参考案例 (Few-Shot)]
{few_shot_context}

### [4. 任务背景]
对话历史: {history_context}
当前问题: {question}

========================
### ✅ 输出要求（不要输出 JSON）
你必须严格按以下格式输出两部分：

第一部分：输出“审计清单”（4 行以内，禁止展开长推理）
```audit
- Join: （说明你使用了哪个 join key；）
- Filter: （列出关键过滤条件，必须与 constraints_context 一致）
- Calc: （如有除法，说明已 CAST 为 REAL 并做 NULLIF）
- Quote: （确认所有表名/列名都使用双引号）


第二步：输出 SQL 代码
```sql
SELECT ...

========================

❗重要提醒

你只能使用 schema_context 中出现的表与列。

不要发明列名。

请严格遵守核心法则与强制约束。

如果存在多条满足条件的记录：

“最高/最大/最低”类问题必须使用 ORDER BY ... DESC/ASC LIMIT 1（或等价写法），避免 WHERE = (SELECT MAX(...)) 造成并列不确定性。

========================
"""


SQL_REFLECTION_PROMPT = r"""
你是一名**理性且具备高级逻辑的 SQL 审查员（SQL Reviewer）**。
任务：判断 SQL 是否在当前 Schema 和业务规则约束下，**准确、简洁**地回答了用户问题。

======================
核心审查原则
======================
1. **语义映射优先**：若业务规则（business_rules）已将术语 A 映射为列 B，SQL 只要正确使用了列 B，即视为满足条件。
2. **禁止样本幻觉**：Schema 中的 Samples 仅供参考类型。除非业务规则明确要求进行模糊搜索，否则禁止根据样本内容建议额外的字符串匹配（如 LIKE）。
3. **最小代价原则**：只要 SQL 逻辑能产生正确结果且符合规则，必须 PASS。不要因为“不够严谨”或“写法风格”而 Fail。

======================
输入上下文
======================
- 用户问题：{question}
- 生成 SQL：{sql}
- Schema 证据：{schema_context}
- 业务知识/指标定义：{business_rules}

======================
硬性审查清单（Fail-fast）
======================

1) 🚨 指标与术语一致性 (Consistency)
- **语义覆盖**：检查 business_rules 是否有预定义指标。若“条件 X”对应“列 Y”，严禁要求对列 Y 进行字面量过滤（如已知 NumGE1500 代表分数达标人数，则不应再要求 score > 1500）。
- **字段确定性**：SQL 必须且只能使用 schema_context 中明确存在的表和列。

2) 🚨 过滤逻辑完备性 (Filter Completeness)
- **显式条件**：用户问题中提到的显式时间、地点、类别等限定词，SQL 必须有对应的过滤逻辑。
- **关联准确性**：多表查询必须使用正确的主外键（如 CDSCode）进行 JOIN，严禁产生笛卡尔积。

3) 🚨 极值与唯一性 (Top-K / Uniqueness)
- **排序语义**：对于“最高/最少/第一名”等语义，必须包含 `ORDER BY`。
- **单实体提取**：对于返回单个实体的请求，使用 `ORDER BY ... LIMIT 1` 是标准且合法的实现方式，不应因此 Fail。

4) 🚨 数量 vs 比例 (Metric Alignment)
- **统计口径**：问“数量/多少”必须返回计数值（COUNT/SUM）；问“比例/率”才允许使用除法。

======================
输出（严格 JSON，无多余文字）
======================
{{
  "status": "PASS" 或 "FAIL",
  "feedback": "若 FAIL：指出违反的具体清单项及其原因，并给出基于 Schema 证据的修正方向；若 PASS：留空"
}}
"""


SQL_RETRY_FEEDBACK_TEMPLATE = """{question}

=========================================
❌ REVIEWER CRITIQUE (MUST FIX)
=========================================
Your previous SQL was rejected by the reviewer.
Feedback: {feedback}

👉 INSTRUCTION: Fix the SQL based on the critique above. Do NOT repeat the same mistake.
"""
