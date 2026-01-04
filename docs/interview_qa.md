# 📝 面试核心题：为什么选择 LangGraph？

## Q: 为什么在 Agent 框架中选择了 LangGraph？和其他框架（LangChain Chain, AutoGen）相比有什么优势？

### 1. 核心回答逻辑 (Elevator Pitch)
> “选择 LangGraph 是因为我们的 DB Ops 场景需要**高精度的流程控制**。传统的 Chain 是线性的（DAG），无法处理复杂的**循环纠错（Looping）**逻辑；而 AutoGen 等多智能体框架又过于‘发散’，难以保证 SQL 执行的严谨性。
>
> LangGraph 允许我们将业务流程定义为**有环图（Cyclic Graph）**，完美实现了 **‘生成 -> 校验 -> 报错 -> 回退修正’** 的自愈机制，同时原生支持 **Human-in-the-Loop（人在回路）**，这对于高风险的数据库操作是必须的。”

---

### 2. 深度对比分析 (技术选型表)

| 维度 | LangChain (Legacy Chains) | AutoGen / CrewAI | **LangGraph (本项目选用)** |
| :--- | :--- | :--- | :--- |
| **架构模式** | **DAG (有向无环图)**<br>一条道走到黑，线性执行。 | **Conversation (对话流)**<br>Agent 之间像群聊一样说话，依靠 Prompt 协作。 | **Cyclic Graph (有环图)**<br>状态机模式，允许在节点间循环跳转。 |
| **纠错能力** | ❌ **弱**<br>如果中间一步错了，通常只能抛异常重来，很难“退回上一步”重试。 | ⚠️ **不可控**<br>Agent 可能会一直在那里聊天讨论错误，浪费 Token 且不一定能修好。 | ✅ **强 (Self-Correction)**<br>可以定义：如果 SQL 执行报错，明确跳转回 Generator 节点，带上错误信息重写。 |
| **状态管理** | **弱**<br>上下文主要靠 Memory 传递，容易混乱。 | **黑盒**<br>状态隐藏在对话历史中。 | **强 (Shared State)**<br>全局定义一个 `State` 对象，在这个对象里存 SQL、Retries 次数、报错信息，类型安全。 |
| **人工介入** | ❌ **难实现**<br>很难在链条中间暂停等待用户确认。 | ⚠️ **一般**<br>可以在对话中介入，但流程不清晰。 | ✅ **原生支持 (Interrupt)**<br>专为“审批”设计。在执行 SQL 前自动挂起，等待 API 发送 Approve 指令才继续。 |

---

### 3. 结合项目的具体场景 (举例说明)

在我的 **DB Copilot** 项目中，我利用 LangGraph 解决了两个痛点：

#### 场景一：SQL 自愈机制 (Self-Correction Loop)
* **问题**：大模型第一次生成的 SQL 经常有语法错误（比如选错了表字段）。
* **LangGraph 方案**：
    1.  定义 `Generator` 节点和 `Executor` 节点。
    2.  定义一条 **条件边 (Conditional Edge)**：
        * 如果 `Executor` 返回成功 -> 结束。
        * 如果 `Executor` 报错 -> **路由回 `Generator`**，并将 `error_msg` 写入全局 State。
    3.  这样模型就能看到错误信息，进行针对性修改，而不是盲目重试。

#### 场景二：高危操作审批 (Human-in-the-Loop)
* **问题**：如果是 `UPDATE` 或 `DELETE` 操作，不能让 AI 自动执行。
* **LangGraph 方案**：
    1.  在 SQL 生成后、执行前，设置一个 `interrupt_before=["execute_node"]` 断点。
    2.  系统运行到这里会**自动暂停**，保持状态（Checkpoint）。
    3.  直到管理员在前端点击“确认执行”，调用 `graph.invoke(..., command="resume")`，系统才继续运行。

---

### 4. 总结 (总结陈词)
> “综上所述，LangGraph 提供了**企业级 Agent 所需的确定性和可控性**。对于 DB Ops 这种容错率极低的场景，我们需要的是一个**严谨的状态机（State Machine）**，而不是一个只会聊天的 Chatbot，这就是我选择 LangGraph 的根本原因。”


# ⚔️ 核心面试题：为什么要自研（Code-First）而不是用 Coze/Dify（Low-Code）？

## Q: 既然 Coze/Dify 这种低代码平台这么方便，为什么我们还要从头开发这个 DB Copilot？

### 1. 核心差异总结 (一句话绝杀)
> **Coze 是玩具和原型的王者，但无法满足企业级 DB 运维对“数据隐私”和“精确控制”的极端要求。**
> 我们做的不是一个聊天机器人，而是一个**接管生产环境数据库的运维专家**，这需要代码级的精细控制，是低代码平台做不到的。

---

### 2. 详细维度对比 (深度解析)

| 对比维度 | **Coze / Dify (低代码平台)** | **我们的自研系统 (LangGraph + FastAPI)** |
| :--- | :--- | :--- |
| **🛡️ 数据安全 (最关键)** | **SaaS 黑盒**。<br>虽然可以私有化部署，但成本极高。且数据库连接串、表结构数据需要传给平台，对于金融/政企客户，这是**绝对红线**。 | **完全本地化 (On-Premise)**。<br>所有代码、向量库(Milvus)、大模型都可以跑在内网 Docker 里，**数据不出域**，物理隔离，符合审计合规要求。 |
| **🧠 逻辑复杂度** | **线性工作流 (Flowchart)**。<br>适合处理简单的“检索-回答”。如果要实现“SQL 报错 -> 抓取错误码 -> 结合元数据 -> 重新生成”这种**复杂的循环纠错 (Loop)**，在 UI 上连线会变成“蜘蛛网”，极难维护。 | **图结构 (Graph / FSM)**。<br>利用 **LangGraph** 的代码能力，我们可以定义复杂的状态机。比如检测到 `DROP TABLE` 高危指令，直接在代码层拦截并触发人工审批流，这种灵活性是拖拽组件做不到的。 |
| **⚡ 性能与优化** | **通用逻辑**。<br>无法针对特定场景优化。比如它无法处理“1000 张表”的 Schema 检索，只能暴力塞进 Prompt，导致 Token 爆炸。 | **深度定制**。<br>我们编写了专门的 **Schema Linker** 算法，结合了业务元数据和向量检索，只提取最相关的 5 张表。这种**算法级的优化**只能通过代码实现。 |
| **🔌 系统集成** | **API 插件模式**。<br>只能通过 HTTP 调接口。 | **原生集成**。<br>我们可以直接 import 内部的 RPC 服务，直接读取 K8s 的配置中心，或者连接旧版的 Oracle 数据库。代码拥有无限的集成能力。 |

---

### 3. 具体场景举例 (让面试官信服)

#### 场景一：SQL 自动纠错机制
* **在 Coze 里**：你很难判断 SQL 执行失败的具体原因（是语法错？还是权限不够？）。你只能把报错一股脑扔回给模型，效果很差。
* **在我们的代码里**：
 ```python
    try:
        execute_sql(sql)
    except OperationalError as e:
        if "Access denied" in str(e):
            # 权限错误：直接转接人工，不重试
            return human_handoff()
        elif "Unknown column" in str(e):
            # 字段错误：触发 Schema Linker 重新检查表结构，进行 Self-Correction
            return rewrite_sql(error_msg=e)
   ```
    > **这种基于错误类型的精细化分支处理，只有写代码才能完美实现。**

#### 场景二：超大数据库的 Schema Management
* **在 Coze 里**：你必须手动把表结构贴到知识库里。如果表结构每天都在变，知识库就过期了。
* **在我们的代码里**：
    我们写了一个定时任务（Cron Job），每天凌晨自动扫描数据库元数据，更新 Milvus 里的向量索引。**系统是活的，能自动感知业务变化**，无需人工维护。

---

### 4. 总结陈词
> “低代码平台适合做 **MVP (最小可行性产品)** 或者简单的客服机器人。
> 但我们要解决的是 **DB Ops** 这种**高风险、高专业度、高安全性**的垂直领域问题。只有通过 **LangGraph + 定制代码**，才能在这个狭窄的领域做到 **99% 的可靠性**，而 Coze 可能只能做到 80%。”

# ⚡ FastAPI 面试核心题库

## 1. FastAPI 和 Flask/Django 有什么区别？为什么选它？(⭐⭐⭐⭐⭐)
**考点**：技术选型能力，看你是不是跟风选的。

* **标准回答**：
    1.  **性能差异**：Django/Flask 是同步框架（WSGI），一个请求卡住（比如查数据库），线程就堵塞了。FastAPI 是**原生异步**的（ASGI），基于 Starlette 和 Pydantic，性能接近 Go 和 NodeJS，非常适合 AI 这种高 I/O（等大模型回复）的场景。
    2.  **开发效率**：FastAPI 基于 **Python Type Hints (类型提示)**，编辑器能自动补全，还能自动生成 Swagger UI 文档，这在前后端联调时太爽了。
    3.  **数据验证**：Django 需要专门写 Serializer，Flask 需要由插件做验证，而 FastAPI 集成了 **Pydantic**，入参校验是全自动的。

* **🗣️ 结合你的项目说**：
    > “在我的 DB Copilot 项目里，因为后端需要频繁调用 OpenAI 接口和 Milvus 数据库，这些都是耗时的 I/O 操作。如果用 Flask，一个用户在生成 SQL 时，整个线程就卡住了。用 FastAPI 的 `async/await`，我可以让系统在等待大模型思考的时候，还能处理其他用户的请求，并发能力提升巨大。”

---

## 2. 什么是 Pydantic？它在 FastAPI 里起什么作用？(⭐⭐⭐⭐)
**考点**：数据治理能力。

* **标准回答**：
    Pydantic 是一个基于 Python 类型提示的数据验证库。在 FastAPI 中，它充当了 **DTO (Data Transfer Object)** 的角色。
    它负责把前端传来的 JSON 数据（不安全的）自动转换成 Python 对象（安全的），如果字段类型不对（比如 `age` 传了字符串 "abc"），它会直接在入口处抛出 422 错误，根本不需要进业务逻辑写 `if-else` 检查。

* **🗣️ 结合你的项目说**：
    > “比如我的 `/chat` 接口，我定义了一个 `ChatQuery` 的 Pydantic 模型，强制要求 `history` 字段必须是一个 List。如果前端传错了，FastAPI 自动就拦截了，保证了进到 Agent 里的数据绝对是干净的。”

---

## 3. 解释一下 Python 的 `async` 和 `await` 原理？(⭐⭐⭐⭐⭐)
**考点**：最硬核的 Python 并发原理题。这是很多转码选手的噩梦，背下来就是加分项。

* **通俗解释 (点单员理论)**：
    * **同步 (Sync)**：你在麦当劳点餐，服务员（线程）收了钱，去后厨盯着厨师做汉堡，做好了才端给你，然后才接待下一个顾客。**（效率低）**
    * **异步 (Async)**：服务员收了钱，给你个小票（Awaitable），然后立马接待下一个顾客。等后厨喊“好了”（回调），服务员再把汉堡给你。**（高并发）**

* **标准回答**：
    FastAPI 利用了 Python 的 **协程 (Coroutine)** 机制。当代码执行到 `await`（比如等待数据库查询）时，它会主动把 CPU 的控制权交还给 **事件循环 (Event Loop)**，让 CPU 去处理其他请求，而不是傻傻地等待。

* **🗣️ 结合你的项目说**：
    > “在我的项目中，`await client.chat.completions.create(...)` 这一行非常关键。因为大模型生成 SQL 可能需要 5-10 秒，利用 `async`，这 10 秒钟我的服务器可以去响应别人的健康检查或者查询请求，资源利用率最大化。”

---

## 4. FastAPI 的 `Depends` 是什么？(⭐⭐⭐)
**考点**：架构设计，依赖注入（DI）。

* **你是 Java 背景，这题秒杀**：
    这就相当于 Spring 的 **Dependency Injection (DI)** 或者 `@Autowired`，但是更轻量。

* **标准回答**：
    `Depends` 是 FastAPI 的依赖注入系统。它主要用于：
    1.  **复用逻辑**：比如从 Header 里取 Token 做鉴权。
    2.  **资源管理**：比如数据库连接。写一个 `get_db` 函数，通过 `Depends(get_db)` 注入到接口里，还能自动处理 `yield` 后的关闭连接操作（Context Manager）。

* **🗣️ 结合你的项目说**：
    > “我的架构里，`schema_linker` 和 `rag_engine` 都是通过 `Depends` 注入进来的。这样我在写单元测试的时候，可以轻松地把真实的 Milvus 连接替换成 Mock 对象，方便测试。”

---

## 5. 什么是 ASGI？它和 WSGI 有什么区别？(⭐⭐⭐)
**考点**：部署运维知识。

* **标准回答**：
    * **WSGI** (Web Server Gateway Interface): 是老标准（Flask/Django 用），它是**同步**的，一次只能处理一个请求。
    * **ASGI** (Asynchronous Server Gateway Interface): 是新标准（FastAPI 用），支持**异步**、WebSocket 和长轮询。
    * **Uvicorn**: 就是一个高性能的 ASGI 服务器，用来跑 FastAPI 的。

* **🗣️ 结合你的项目说**：
    > “因为我的项目未来可能要支持流式输出（Streaming Response，像 ChatGPT 那样打字机效果），WSGI 做不到，必须用 ASGI 协议，这也是我选 FastAPI 的原因之一。”

# 📚 Text-to-SQL 项目：向量嵌入模型（Embedding）选型指南

在处理 **1000+ 物理表** 的企业级数据库时，AI 无法一次性读取所有 DDL。我们必须通过 **向量检索 (Vector Search)** 筛选出最相关的表。选择合适的 Embedding 模型是决定“召回准确率”的关键。

---

## 1. 核心选型：为什么首选 `BGE-M3`？

`BGE-M3`（BAAI General Embedding）是目前国产 RAG 领域的顶流模型，特别适合处理中英混杂、缩写密集的数据库场景。

### ✅ 核心优势：
* **混合检索能力 (Hybrid Search)**：
    * 同时支持 **Dense Retrieval**（语义检索）和 **Sparse Retrieval**（关键词检索）。
    * **业务意义**：用户搜“销售”能搜到 `order`（语义），搜 `uid` 能直接定位到 `user_id` 列（关键词）。
* **多语言处理能力 (Multi-Lingual)**：
    * 完美处理 **“英文缩写表名 + 中文业务注释”**。
* **超长上下文支持**：
    * 支持 **8192** Token，比普通模型高出数倍，适合存放复杂的建表说明。

---

## 2. 主流嵌入模型对比

| 模型名称 | 来源 | 特性 | 适用场景 |
| :--- | :--- | :--- | :--- |
| **BGE-M3** | 北京智源 | **全能型**：混合检索，中英双语极强。 | **本项目首选**。 |
| **text-embedding-3** | OpenAI | 云端 API，性能稳定，无需本地算力。 | 追求开发速度，数据不敏感。 |
| **all-MiniLM-L6-v2** | HuggingFace | 极度轻量，速度极快，但仅限英文。 | 纯英文测试环境。 |
| **m3e-base** | MokaAI | 中文语义理解极佳。 | 纯中文注释场景。 |

---

## 3. 面试技术话术（背诵金句）

> **面试官**：“1000 张表，你怎么保证 AI 能找对表？”
> 
> **你**：“我采用 **Two-stage Schema Retrieval（两阶段模式检索）** 架构。首先，我利用 **BGE-M3** 模型对全库元数据进行向量化。相比于普通模型，BGE-M3 的**混合检索（Hybrid Search）**能力能同时兼顾业务语义和具体的字段名。通过这种方式，我将 1000+ 张表精简为 Top-5 候选表，将 Token 消耗降低了 90% 以上，并显著提升了 SQL 生成的准确率。”

---
# 面试必问：选表/选字段不准怎么办？（企业级 Text-to-SQL 兜底方案）

> 核心结论：不可能 100% 准，所以要做 **“多阶段 + 强约束 + 可验证 + 自纠错 + 澄清”** 的闭环。  
> 召回宁可多一点，生成要强约束，执行要可验证，失败要能自修复，低置信度就澄清。

---

## 1）整体策略（先给面试官框架）
**召回（不漏） → 重排（更准） → 受限生成（不瞎编） → 校验执行（可验证） → 失败自修复（闭环） → 低置信度澄清（产品化）**

---

## 2）召回层：怎么提高“选表准度”
### 2.1 Hybrid 检索（必须讲）
- **BM25/关键词召回**：命中表名/字段名/缩写非常强（uid、gmv、ctr、order、pay）
- **向量召回（Milvus）**：语义相近更强（成交额≈GMV、订单金额≈支付金额）

**做法**：两路各取 TopK（如 20+20）合并去重 → 得到候选集（如 Top30）。  
> 召回负责“不漏”，后续由重排负责“更准”。

### 2.2 分层召回（库→域→表）
- 不强迫用户先选库：**在权限范围内全局召回**
- 再按业务域/库聚类排序（trade/user/marketing/log），让候选空间更清晰  
> 面试官问“两个库都有怎么办”：答“全局召回+按域聚类+重排”。

### 2.3 Rerank（重排）是提升关键
- Top30 候选后，用更强模型做 rerank 得到 Top5
- 可选：交叉编码器 reranker / LLM rerank（query + schema 摘要）

---

## 3）生成层：怎么防止模型瞎编
### 3.1 只给候选 schema（强约束）
- LLM 生成 SQL 时 **只提供 TopK 表的字段/注释/主键/索引**
- 明确禁止访问候选之外表（硬约束）

### 3.2 结构化中间表示（IR）
先让模型输出 IR：
- 指标 metric（GMV/订单数）
- 维度 dimension（按天/按省）
- 过滤 filters（近30天/状态）
- 实体 entity（订单/支付/用户）
再用 IR 选择表/字段并拼 SQL  
> 体现工程化，不是纯 prompt。

---

## 4）执行层：用“可验证”让错误暴露并自动修正
### 4.1 SQL 校验（AST + Schema Check）
- 解析 SQL AST
- 检查表/字段是否存在
- join key 是否合理（uid/oid 等）
- 强制只读、limit、timeout

### 4.2 Explain 风险控制（慢/全表扫）
- `EXPLAIN` 判断是否全表扫、是否缺少时间条件/索引
- 风险高则要求补充条件或改用聚合表/分区表

### 4.3 失败反馈回路（最关键）
执行失败（Unknown table/column、ambiguous、timeout、权限不足）：
- 把 **报错信息 + 候选 schema + 上次 SQL** 回灌
- Agent 自动修复重试 1~2 次  
> “失败不是终点，是监督信号（self-healing）”。

---

## 5）交互层：低置信度就澄清（产品化）
触发条件（举例）：
- Top1/Top2 分数很接近（歧义）
- 问题缺关键口径（时间范围、业务口径）
- 多个库都可能（下单额 vs 支付额）

澄清问题要短：
- “你说的 GMV 是 **下单金额** 还是 **支付金额**？”
- “统计范围是 **近30天** 还是 **自然月**？”
- “按天还是按周？”

---

## 6）数据层：注释不全怎么办（现实场景）
- **规则补注释**：根据表名前缀/字段名生成伪描述（order/pay/user/log）
- **样本值画像**：抽样 100 行推断字段含义（amount 是分/元、时间范围等）
- **业务字典/CMDB**：补充表所属业务域/应用作为 metadata，提升“选库/选域”准确率

---

## 7）评估与闭环：怎么证明“我们做得准”
- 离线：Q→正确表/SQL 测试集，算 recall@K、MRR
- 在线：记录用户最终采用/修改的表（弱标签），持续迭代检索与 rerank
- 可观测：trace_id 记录 query、候选表、最终表、失败原因、修复次数

---

## 30 秒标准回答模板（直接背）
“选表不可能 100% 准，所以我们做多阶段兜底：先 hybrid 召回（BM25+向量）拿 Top30，接 rerank 选 Top5；生成 SQL 时只给候选 schema 做强约束；执行前做 AST 校验+schema check+explain 风险控制；执行失败把错误作为反馈让 agent 自修复重试；如果候选分数接近或缺口径信息就触发澄清问题。数据侧用规则补注释+抽样画像+业务字典增强 schema 文本，最后用离线 recall@K+线上日志闭环持续优化。”

# 面试必问：你们的 schema 向量库（Milvus）是怎么“入库”的？

> 目标：让用户不需要先选库/选表，在权限范围内对“所有表”做语义检索，召回 TopK 候选表，再用于 SQL 生成与校验。

---

## 1）入库对象是什么？
**粒度：一张表 = 一条向量记录（table-level embedding）**

每条记录包含两部分：

### A. 向量（vector）
- 把“表的 schema 描述文本 text”用本地 embedding 模型（BGE）编码得到向量
- 向量用于 Milvus 的相似度检索（TopK）

### B. 元数据（metadata）
用于回传证据、过滤权限、后续 rerank/生成：
- db / table / full_name
- domain（业务域）
- join_keys / time_cols / metric_cols（结构化特征）
- perm_tag / sensitivity（权限与分级，先默认值，后续对接权限系统/CMDB）
- text（用于 explain / evidence）

---

## 2）schema 描述文本（text）怎么拼？
我们从 `information_schema` 抽元数据，并拼成可检索的自然语言结构：

- 库名、表名、表注释（table_comment）
- 主键、索引（pk / indexes）
- 字段列表（字段名 + 类型 + 字段注释）
- 规则增强：从字段名推断 join_keys/time_cols/metric_cols，并写入 metadata

示例（简化）：
库: corp_trade_center
表: t_order_00
表描述: 订单主表分片
主键: oid
索引: idx_uid(uid), idx_ct(create_time)
字段:

oid(bigint) 订单ID

uid(bigint) 用户ID

amount(decimal) 支付金额

create_time(datetime) 创建时间


---

## 3）入库流程（从 0 到 Milvus）
### Step 1：抽取 catalog
- 扫描用户权限范围内的所有库/表
- 从 `information_schema.tables/columns/statistics` 抽出：
  - 表注释、字段、主键、索引等
- 输出 `schema_catalog.jsonl`（每行一张表）

### Step 2：Embedding（本地 BGE）
- 对每条 `text` 做 embedding（我们用 BGE，例如 bge-m3 / bge-small-zh）
- 为了用 cosine 相似度，我们对向量做 normalize（normalize_embeddings=True）

### Step 3：建 Milvus collection + 建索引
- collection 字段：`db, table, full_name, domain, owner/app, perm_tag, sensitivity, join_keys, time_cols, metric_cols, text, vector`
- 向量索引：HNSW（或 IVF_FLAT），metric 用 IP（配合 normalize 等价 cosine）

### Step 4：批量写入
- 按 batch（比如 256）插入
- flush + load 让检索可用

---

## 4）为什么要加这些 metadata？
- **权限过滤**：perm_tag / sensitivity 支持检索前/后过滤，避免越权
- **提升准确率**：domain/join_keys/time_cols/metric_cols 可用于 rerank 或规则加权
- **可解释性**：text 返回 evidence（截断），告诉用户“为什么召回这张表”
- **工程闭环**：支持 trace_id + 日志记录，线上可观测与迭代

---

## 5）怎么处理模型/维度变化？
- Milvus collection 的 `vector dim` 必须固定
- 更换 embedding 模型（small ↔ m3 ↔ large）导致 dim 变化时：
  - 直接 drop collection 重建并重新入库（离线流程）
  - 或维护多版本 collection（schema_catalog_v1/v2）

---

## 6）一句话面试回答模板（30 秒）
“我们把每张表的 schema（表注释+字段+索引）拼成可检索文本，使用本地 BGE 模型生成 embedding，批量写入 Milvus 的 schema collection，并用 HNSW 建索引做向量召回。检索时对用户问题做同样 embedding，在权限范围内召回 TopK 表，并返回 evidence 和结构化特征（domain/join_keys/time_cols/metric_cols），供后续 constrained SQL 生成与校验、失败自修复使用。”


# 大厂风格：/api/v1/retrieve_tables 返回格式（Schema Retrieval）

## 设计目标
- **可观测**：trace_id + latency + debug
- **可解释**：evidence（为什么命中）
- **可控**：filters_applied（权限/库/域过滤）
- **可交互**：need_clarify + clarify_question
- **可扩展**：retrieval_profile / rerank / hybrid

---

## Request（示例）
```
POST /api/v1/retrieve_tables
{
  "user_id": "u1",
  "query": "近30天订单金额趋势",
  "topk": 10,
  "filters": {
    "allowed_dbs": ["corp_trade_center","corp_user_center"],
    "domain": null
  }
}
```
---

## Response（大厂风格示例）
```
{
  "trace_id": "c0a1b35f-2a2a-4f6c-9b6a-52c1a51e4b2c",
  "success": true,
  "error": null,
  "request": {
    "user_id": "u1",
    "query": "近30天订单金额趋势",
    "topk": 5,
    "filters": {
      "allowed_dbs": ["corp_trade_center","corp_user_center"],
      "domain": null
    }
  },
  "retrieval": {
    "engine": "milvus",
    "collection": "schema_catalog",
    "embedding_model": "BAAI/bge-m3",
    "metric": "cosine(ip+normalize)",
    "retrieval_profile": "schema_v1",
    "latency_ms": 42,
    "candidates": [
      {
        "rank": 1,
        "full_name": "corp_trade_center.t_order_107",
        "db": "corp_trade_center",
        "table": "t_order_107",
        "domain": "trade",
        "score": 0.5431,
        "evidence": "库: ... 表: ... 主键: ... 字段: amount/create_time/uid ...",
        "features": {
          "join_keys": ["uid","oid"],
          "time_cols": ["create_time"],
          "metric_cols": ["amount"]
        },
        "governance": {
          "perm_tag": "default",
          "sensitivity": 0,
          "owner": "",
          "app": ""
        }
      }
    ],
    "summary": {
      "top1_score": 0.5431,
      "top2_score": 0.5430,
      "score_gap": 0.0001,
      "need_clarify": true,
      "clarify_question": "你说的‘订单金额’是下单金额还是支付金额？按天还是按周？"
    },
    "filters_applied": [
      "permission: user_id=u1 -> allowed scope",
      "allowed_dbs: corp_trade_center,corp_user_center"
    ],
    "debug": {
      "returned_k": 5,
      "notes": "top1/top2 gap 小，触发澄清"
    }
  }
}
```

# 🚀 进阶架构：基于 Agentic RAG 的 Schema 检索与智能澄清

### 1. 痛点分析
在处理企业级 1000+ 表的 Text-to-SQL 任务时，单纯依赖向量检索（Vector Retrieval）面临巨大挑战：
* **语义重叠**：例如 `order_main`（订单主表）和 `order_detail`（订单明细表）在向量空间中极度接近，余弦相似度差异可能小于 0.01。
* **意图模糊**：用户提问“销售额”时，无法区分是“下单口径”还是“财务实收口径”。
* **规则僵化**：传统的硬编码阈值（如 Score Gap < 0.05）无法处理复杂的业务语境，导致反问生硬或错误。

### 2. 解决方案：引入 LLM 决策层 (Agentic Validator)



我重构了检索链路，在 Milvus 召回之后，引入了一个 **LLM Validator (澄清 Agent)**，实现了从“死规则”到“活思考”的转变。

#### 核心流程：
1.  **Retrieve (检索)**：从 Milvus 向量库快速召回 Top-5 候选表，利用 BGE-M3 模型保证基础语义匹配。
2.  **Reasoning (推理)**：将用户 Query 与 Top-3 候选表的元数据（包含表注释、核心指标列）构建成 Prompt，发送给推理模型（DeepSeek/GPT）。
3.  **Decision (决策)**：Agent 输出 JSON 格式的决策结果：
    * **Case A (明确)**：Top-1 显著优于其他表 -> `need_clarify: false`。
    * **Case B (歧义)**：Top-1 和 Top-2 存在业务冲突（如应收 vs 实收） -> `need_clarify: true`，并**自动生成**一段极具业务深度的反问句。
    * **Case C (无解)**：无相关表 -> 引导用户补充信息。

### 3. 代码实现亮点
* **结构化输出**：强制 LLM 输出 JSON 格式，便于后端程序解析和前端交互。
* **降级策略 (Fallback)**：在 LLM 调用超时或解析失败时，自动回退到基于分数差（Score Gap）的规则策略，保证系统的高可用性（HA）。
* **Token 优化**：仅将 Top-3 候选表的关键特征（Features）输入给 LLM，在保证判断准确率的同时最小化延迟和成本。

### 4. 业务价值（面试必说）
> "通过引入 Agent 决策层，我们将 SQL 生成的**一次性成功率 (First Time Yield)** 提升了 30%。更重要的是，它让系统具备了**交互式引导能力**。当用户意图模糊时，AI 会像业务专家一样反问'您是指下单金额还是入账金额？'，而不是盲目生成错误的 SQL。这种体验上的升级是传统规则引擎无法做到的。"


# 面试题：在复杂的企业级数据库中，你们是如何准确检索到数据表的？(Schema Retrieval)

## 核心思路
在企业级场景下，简单的“TopK 向量检索”完全不够用（会遇到分表干扰、黑话缩写、多表关联遗漏等问题）。
我们设计了一套 **“主动式 + 漏斗型” (Agentic & Funnel-based)** 的检索架构，分为 **预处理、深召回、硬拦截、智校验** 四个阶段。

---

## 阶段一：Query Rewrite Gate (主动改写)
我们不直接用用户的原始问题去搜，因为用户喜欢用“黑话”（如GMV、转化率）或问题太短。
* **动作**：在检索前，先过一个轻量级 LLM Gate。
* **功能**：
    1.  **黑话翻译**：将 "DAU" 转译为 "日活跃用户"，将 "ROI" 转译为 "投入产出比"。
    2.  **实体拆解**：把“活动带来的订单”拆解为 `["活动表", "订单表"]` 两个搜索词。
    3.  **意图拦截**：如果问题无意义（如“你好”），直接拦截，不查库。

## 阶段二：Deep Recall & Aggregation (深召回与聚合)
这是解决**分库分表**问题的核心。
* **深召回 (Deep Prefetch)**：
    * 我们将 Milvus 的 `topk` 设为 **500**。
    * **技术细节**：为了防止 HNSW 索引报错，我们在代码层面实现了动态参数调节，确保搜索范围 `ef` 始终大于 `topk` (即 `ef = max(128, topk * 1.5)`)。
* **逻辑聚合 (Logical Aggregation)**：
    * 拿到 500 个结果后，通过正则 `re` 识别物理分表（如 `t_order_001`, `t_order_002`...）。
    * 在内存中将它们聚合为唯一的逻辑表 `t_order_*`。
    * **打分策略**：取所有分片中的最高分 (Max Score) 作为逻辑表得分；字段取并集。
    * **结果**：将 500 个碎片压缩为 10~20 个高质量逻辑表候选。

## 阶段三：Physical Guardrails (物理熔断)
在调用昂贵的 LLM 之前，先通过硬规则过滤“一眼假”的结果，防幻觉且省钱。
* **低分熔断**：设定 `MIN_SCORE_THRESHOLD = 0.45`。如果聚合后的 Top1 分数低于此阈值，说明库里根本没这数据（如搜“火箭”匹配到“营销表”），直接返回“未找到”，**严禁进入下一步**。
* **硬伤检测**：检查是否缺失核心时间字段（如问“趋势”但表里没时间列）。

## 阶段四：Agentic Judge & Two-Pass Correction (智能裁判与二轮补救)
这是架构的“大脑”，引入了 **Self-Correction (自我修正)** 机制。我们取 Top 10 给 LLM 裁判进行审视：

### 1. 裁判判决 (Judge Decision)
LLM 会判断当前的表组合属于以下哪种情况：
* **PASS (通过)**：表全了，或者能通过 Join 解决。 -> **进入 SQL 生成**。
* **COMPLEMENT (缺表)**：有“订单表”，但缺“用户地区表”。 -> **触发补搜**。
* **REWRITE (重搜)**：当前结果完全不对（语义漂移）。 -> **触发重搜**。
* **ASK_USER (歧义)**：无法确定口径（如下单金额 vs 支付金额）。 -> **反问用户**。

### 2. 二轮执行 (Execution)
* **补搜模式**：系统自动用 LLM 生成的“缺失关键词”进行第二次检索，将新结果与旧结果**合并 (Merge)**。
* **限制机制**：严格控制 **Max Hops = 1**（最多重搜一次），防止死循环和长耗时。

---

## 总结：与开源方案的区别
相比于 Vanna 或 LangChain SQL Agent 等开源方案，我们的优势在于：
1.  **完美支持分库分表**：通过 `prefetch=500` + `aggregate` 解决碎片化问题。
2.  **抗噪能力强**：通过 `Score < 0.45` 物理熔断，解决了“强行回答”导致的幻觉。
3.  **支持多跳推理**：通过 `Rewrite Gate` 和 `Complement` 机制，解决了复杂多表关联查询的漏表问题。