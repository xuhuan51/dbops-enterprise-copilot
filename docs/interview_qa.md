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


# 核心面试题：在 Text-to-SQL 任务中，你是如何准确找到相关数据表的？(RAG Strategy)

## Q: 面对海量数据库表（成百上千张），你的 Agent 是如何精准定位到用户需要的那几张表的？

### 核心回答逻辑 (STAR法则)
我们采用了一套 **"离线语义增强 + 在线漏斗检索 + Agent 自愈"** 的三层架构，目前准确率可以达到 95% 以上。

---

### 1. 离线层：解决"语义鸿沟" (Semantic Gap)
**痛点**：数据库表名通常是英文缩写（如 `t_odr_01`），而用户问的是中文业务（如“查销量”）。直接 Embedding 效果很差。
**方案**：构建 **Table Card (数据资产卡片)**。
* **动作**：我写了一个 ETL 脚本，自动化提取 Schema 信息。
* **创新点**：
    * **语义丰富**：利用 LLM 自动为表生成 Summary（摘要）和 Synonyms（同义词）。例如把 `gmv` 字段标记为 `["成交额", "交易额", "流水"]`。
    * **样本增强**：把表里的 `Sample Data`（采样数据）也Embedding进去，这样用户搜具体的实体名（如“小米手机”）也能召回表。
    * **混合索引**：将处理好的 Table Card 存入 **Milvus**，建立向量索引。

### 2. 在线层：两段式检索漏斗 (Recall & Rerank)
**痛点**：单次检索要么召回不够（漏表），要么噪音太大（LLM 容易晕）。
**方案**：**Recall (广撒网) -> Rerank (精排序)**。
* **第一步：Milvus 粗排 (Top-50)**
    * 使用 `bge-m3` 模型进行向量相似度检索，快速圈定前 50 张可能相关的表。
    * *特点*：速度快，但包含噪音（如同名不同库的表）。
* **第二步：Cross-Encoder 精排 (Top-5)**
    * 引入 `bge-reranker` 交叉编码模型，对 Query 和 这 50 张表的 Schema 进行逐字逐句的深度比对。
    * *特点*：精度极高，能理解复杂的业务对应关系。
    * **截断策略**：最终只保留 Score > 0.01 的 Top-5 表给大模型，极大减少了 Token 消耗和幻觉干扰。

### 3. Agent 层：ReAct 自愈机制 (Self-Correction)
**痛点**：检索不是万能的，特别是跨库查询（Multi-hop）容易漏掉中间表。
**方案**：**Reflexion (反思架构)**。
* **预执行校验**：LLM 生成 SQL 后，我不直接执行，而是先跑 `EXPLAIN`。
* **错误归因**：如果数据库报错 `Table 'xxx' doesn't exist` 或者 `Unknown column`，Agent 会捕获这个错误。
* **自动补搜 (Repair Node)**：
    * Agent 会分析报错，提取缺少的关键词（例如 "缺用户信息表"）。
    * 触发 **Repair 工具**，带着新关键词去 Milvus 进行**二次补搜**。
    * 将新找回的表加入 Context，让 LLM 重写 SQL。

### 总结 (Outcome)
通过这套组合拳（Table Card 增强 + Rerank 精排 + ReAct 补搜），我们解决了传统 Text-to-SQL 系统中最头疼的“找不准表”和“幻觉字段”问题，实现了企业级的可用性。

# 面试题：在多智能体（Multi-Agent）架构中，你是如何管理记忆（Memory）与上下文的？

## 核心架构：分层管理 + 持久化存储 + 状态隔离

在我的 DBOps Agent 项目中，为了解决“服务无状态”、“上下文丢失”以及“Token 爆炸”等问题，我设计了一套基于 **LangGraph + MySQL** 的企业级记忆系统。

我的策略主要分为三个层面：

---

### 1. 存储层：基于 MySQL 的持久化 (Persistence)

我们没有使用简单的内存变量（List/Dict）来存历史，因为那无法应对服务重启和并发请求。

* **技术选型**：利用 LangGraph 的 **Checkpointer** 机制，配合自定义的 `AsyncMySQLSaver` 适配器。
* **实现原理**：
    * 在 MySQL 中维护 `checkpoints` 表。
    * **序列化**：每当 Agent 完成一步思考（Node Execution），系统会自动将当前的 `State`（包含对话历史、意图、中间变量）序列化为二进制 **BLOB**。
    * **写库**：通过 `thread_id`（会话 ID）作为主键，将快照写入数据库。
* **优势**：
    * **无状态服务**：API Server 重启后，只要前端带上 `session_id`，Agent 就能从 MySQL 瞬间恢复之前的“大脑状态”，用户体验无感。
    * **可回溯**：保存了每一次交互的版本快照（Snapshot），方便调试 Case。

### 2. 逻辑层：状态隔离与显式透传 (State Isolation)

在多智能体（Master-SubAgent）架构中，如果不加控制，上下文很容易混乱。我采用了**“全局-局部”分离**策略：

* **全局记忆 (Global Memory)**：
    * 由 **Master Agent** 持有。
    * 存储内容：用户与系统的核心对话流（User: ..., AI: ...）。
    * 生命周期：**永久存储**（在 MySQL 中）。
* **局部记忆 (Local Memory)**：
    * 由 **Sub-Agent**（如 SQL 查询专家）持有。
    * 存储内容：子任务执行过程中的中间思考（如：生成 SQL -> 报错 -> 反思 -> 重写）。
    * 生命周期：**临时存在**。任务结束后，只返回最终结果给 Master，中间繁琐的 Debug 过程**不回写**到全局记忆。
* **显式透传 (Propagation)**：
    * Master 在调用子智能体时，通过代码显式提取全局历史（`state["history"]`），并注入到子智能体的输入中。

### 3. 优化层：防上下文爆炸策略 (Optimization)

面对多轮长对话，为了防止 Token 溢出，我实施了 **Sliding Window（滑动窗口）** 机制：

* **裁剪机制**：在 Master 将记忆传递给子智能体（如 Query Agent）之前，我会对 `history` 列表进行切片处理（例如 `history[-6:]`）。
* **效果**：
    * 子智能体永远只看到最近的 3-5 轮对话，足以处理“*按这个条件再查一下*”这类指代性追问。
    * 无论用户聊了 100 轮还是 1000 轮，传给 LLM 的 Token 始终保持在恒定范围，保证了系统的稳定性和响应速度。

---

### 追问预警：具体代码是怎么实现的？

**回答示例：**
> “因为 LangGraph 官方只提供了 PostgreSQL 的适配器，为了复用公司现有的 MySQL 设施，我**手写了一个 `AsyncMySQLSaver` 类**。
>
> 核心逻辑是继承 `BaseCheckpointSaver`，重写了 `aget_tuple`（读档）和 `aput`（存档）两个异步方法。利用 `aiomysql` 连接池，将 LangGraph 的 Checkpoint 对象序列化后存入 `longblob` 字段。
>
> 在业务代码中，我使用 Python 的 `contextmanager` 将这个 Saver 注入到 Graph 的编译过程中：`workflow.compile(checkpointer=mysql_saver)`。”


# 🚀 面试题：Docker 容器隔离的底层原理是什么？

## 1. 一句话总结
Docker 本质上是一个**运行在宿主机上的特殊进程**。它不包含独立的内核（与宿主机共享内核），而是通过 Linux 内核的 **Namespace** 实现**资源隔离**，通过 **Cgroups** 实现**资源限制**，并通过 **UnionFS** 实现**文件系统隔离**。

---

## 2. 核心原理详解

### ① Namespace（命名空间）：实现“视图隔离”
Namespace 是 Linux 内核用来隔离内核资源的方式。它让容器看起来像是一个独立的操作系统，但实际上只是在欺骗进程的视图。

Docker 主要使用了以下 6 种 Namespace：

| Namespace 类型 | 隔离内容 | 作用描述 |
| :--- | :--- | :--- |
| **PID** | 进程编号 | 容器内的进程拥有独立的 PID 空间（比如容器内 PID=1，但在宿主机上可能是 PID=1234）。 |
| **NET** | 网络设备 | 容器拥有独立的网卡、IP 地址、路由表和端口号，互不干扰。 |
| **MNT** | 挂载点 | 容器拥有独立的文件系统挂载点，看不到宿主机或其他容器的文件系统。 |
| **UTS** | 主机名 | 容器可以拥有独立的主机名（Hostname）和域名。 |
| **IPC** | 进程间通信 | 容器内的进程间通信（消息队列、信号量）与宿主机隔离。 |
| **USER** | 用户权限 | 容器内的 Root 用户可以映射为宿主机的普通用户（安全性隔离）。 |

> **通俗理解**：Namespace 就像把进程关进了一个“独立房间”，它以为自己拥有整个大楼，其实只能看到房间里的东西。

### ② Cgroups（Control Groups）：实现“资源限制”
如果有进程在容器里死循环吃光 CPU 怎么办？Namespace 只能隔离视线，不能限制用量。这时候就需要 Cgroups。

Cgroups 是 Linux 内核提供的一种机制，用于限制、记录和隔离进程组所使用的物理资源。

* **资源限制**：限制容器使用的 CPU 份额、内存大小（如 OOM Killer）、磁盘 I/O 速度等。
* **优先级分配**：分配 CPU 时间片的优先级。
* **资源统计**：监控容器用了多少资源（`docker stats` 就是基于此）。

> **通俗理解**：Cgroups 就像给房间装了“电表和水表”，并且设置了上限，超过额度就自动断电或限流，防止一个租户把整栋楼的资源耗光。

### ③ UnionFS（联合文件系统）：实现“文件系统隔离”
Docker 镜像是由多个“只读层”叠加而成的。

* **分层存储**：Docker 镜像由多个只读层组成。
* **Copy-on-Write (写时复制)**：当容器启动时，Docker 会在镜像的最上层挂载一个**读写层**。
    * 如果要读取文件：直接从下层的只读镜像层读取。
    * 如果要修改文件：将文件从只读层复制到最上层的读写层，然后进行修改。
* **驱动实现**：常用的驱动有 Overlay2, AUFS, Devicemapper 等。

---

## 3. Docker vs 虚拟机 (VM) 的区别

这是面试中必定会问的关联问题：

| 特性 | Docker (容器) | Virtual Machine (虚拟机) |
| :--- | :--- | :--- |
| **底层架构** | 共享宿主机内核 | 独立的 Guest OS (完整的操作系统) |
| **隔离级别** | 进程级隔离 (较弱) | 系统级隔离 (完全隔离，更强) |
| **启动速度** | 秒级 (直接启动进程) | 分钟级 (需要经过 BIOS 自检、OS 启动) |
| **资源占用** | 极低 (几 MB) | 较高 (几 GB，因为要跑完整的 OS) |
| **性能损耗** | 接近原生 | 较高 (通过 Hypervisor 虚拟化指令) |

---

## 4. 总结 (面试高分话术)

> “Docker 的隔离原理主要依赖于 Linux 内核的三大特性：
> 1. 利用 **Namespace** 做**资源隔离**，让容器拥有独立的网络、进程、挂载点等视图；
> 2. 利用 **Cgroups** 做**资源限制**，防止容器抢占宿主机过多的 CPU 和内存；
> 3. 利用 **UnionFS (Overlay2)** 做**文件系统**的高效分层存储和写时复制。
> 相比虚拟机，Docker 不需要 Hypervisor 层，也不需要独立的 Guest OS，它是共享内核的进程级虚拟化，所以更轻量、启动更快。”