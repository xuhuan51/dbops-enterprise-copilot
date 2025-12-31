# Enterprise Copilot (SQL Agent + Doc RAG) — 从 0 到 1

一个企业级 Data Copilot：同一入口自动路由到 **SQL Agent（结构化查询）** 或 **Doc RAG Agent（技术文档问答）**。  
目标场景：**单库几百表**，支持 **权限先行、可解释、可审计、可控成本**，并提供 Docker / K8s 部署能力。

---

## 1. 目标与特性

### 1.1 核心能力
- **SQL Agent**
  - 权限过滤（只在用户可访问表范围内检索）
  - 表/字段召回（TopK 缩小 prompt）
  - 约束式 SQL 生成（只允许使用候选表字段）
  - Guardrail（禁 DDL/DML、limit/时间条件、可选 EXPLAIN 阈值）
  - 执行与结果解释
- **Doc RAG Agent**
  - 文档导入（ingest）→ chunk → 索引（BM25/Embedding）
  - 检索增强生成（RAG）+ 引用溯源（citations）
  - 文档增量更新（hash 变更才重建）
- **Router（统一入口）**
  - 判断走 SQL / RAG / 澄清（clarify）
  - 输出 route + confidence + reason

### 1.2 工程化要求（企业级思维）
- **可复现**：本地先跑通，后续支持 Docker / K8s
- **可观测**：trace_id + 事件日志（JSONL）
- **可控**：权限先行 + SQL 安全护栏 + 超时/限流

---

## 2. 总体架构

见下方「架构图」Mermaid。

---

## 3. 项目结构（阶段性演进）
```text
dbops-enterprise-copilot/
├── 📂 .github/                  # CI/CD 流水线配置 (后续加)
├── 📂 deploy/                   # 部署相关 (Docker, K8s)
│   └── docker-compose.yml       # 🐳 [核心] 一键启动 Milvus, Redis, MySQL
│
├── 📂 app/                      # 🐍 核心代码库
│   ├── __init__.py
│   ├── main.py                  # 🚀 [入口] FastAPI 应用入口，全局异常处理
│   │
│   ├── 📂 api/                  # 🌐 [接口层] 定义 RESTful API
│   │   └── v1/
│   │       └── chat.py          # /chat 接口，接收用户请求
│   │
│   ├── 📂 core/                 # ⚙️ [核心层] 全局配置
│   │   ├── config.py            # 加载 .env，管理 Milvus/OpenAI 配置
│   │   └── logger.py            # 企业级日志配置 (Loguru)
│   │
│   ├── 📂 modules/              # 🧠 [业务逻辑层] 核心智能体模块
│   │   ├── router/
│   │   │   └── semantic_router.py # 意图分流 (Router)
│   │   │
│   │   ├── agent_sql/           # 📊 SQL 专家智能体
│   │   │   ├── schema_linker.py # Schema Linking (对接 Milvus)
│   │   │   ├── generator.py     # Text-to-SQL 生成逻辑
│   │   │   └── validator.py     # 安全护栏 (SQL 语法/权限检查)
│   │   │
│   │   └── agent_rag/           # 📄 文档专家智能体
│   │       ├── ingest.py        # 文档切片与入库
│   │       └── retriever.py     # 混合检索 (Milvus + BM25)
│   │
│   └── 📂 infrastructure/       # 🏗️ [基础设施层] 数据库连接器
│       ├── milvus_conn.py       # 🔌 Milvus 连接池封装
│       ├── mysql_conn.py        # 🔌 业务数据库连接
│       └── redis_conn.py        # 🔌 Redis 缓存连接
│
├── .env                         # 🔑 敏感信息 (API Key, DB密码)
├── .gitignore                   # ✅ Git 忽略配置
├── Dockerfile                   # 📦 应用镜像构建文件
└── requirements.txt             # 📦 依赖列表
```

### 3.1 当前阶段（MVP）


```mermaid
graph TD
    %% === 样式定义 ===
    classDef user fill:#2d3436,stroke:#fff,stroke-width:2px,color:#fff;
    classDef router fill:#0984e3,stroke:#fff,stroke-width:2px,color:#fff;
    classDef sqlAgent fill:#00b894,stroke:#fff,stroke-width:2px,color:#fff;
    classDef ragAgent fill:#6c5ce7,stroke:#fff,stroke-width:2px,color:#fff;
    classDef db fill:#f1c40f,stroke:#e67e22,stroke-width:2px,color:#2d3436;
    classDef shared fill:#95a5a6,stroke:#fff,stroke-width:1px,color:#fff;

    %% === 第一层：入口与分发 ===
    subgraph "Layer 1: 用户入口与路由"
        User(👱 用户提问 User Query):::user
        Router{🧠 意图分流 Router}:::router
        
        User --> Router
    end

    %% === 第二层：双 Agent 核心逻辑 ===
    subgraph "Layer 2: 智能体层 Agent Layer"
        direction TB
        
        %% 左侧：SQL Agent
        subgraph "📊 SQL Agent (查数据)"
            direction TB
            S1[1. Schema Linking<br/>只找相关的表]:::sqlAgent
            S2[2. SQL 生成<br/>Text-to-SQL]:::sqlAgent
            S3[3. 安全护栏<br/>语法/权限检查]:::sqlAgent
            S4[4. SQL 执行器<br/>Executor]:::sqlAgent
            
            S1 --> S2 --> S3 --> S3_Check{通过?}
            S3_Check -->|Yes| S4
            S3_Check -->|No| S_Err[🚫 拒绝/重试]:::sqlAgent
        end

        %% 右侧：RAG Agent
        subgraph "📄 RAG Agent (查文档)"
            direction TB
            R1[1. 混合检索<br/>Keyword + Vector]:::ragAgent
            R2[2. 重排序<br/>Rerank]:::ragAgent
            R3[3. 答案生成<br/>LLM + 引用]:::ragAgent
            
            R1 --> R2 --> R3
        end
    end

    %% === 第三层：基础设施与存储 ===
    subgraph "Layer 3: 基础设施 Infrastructure"
        direction TB
        
        VectorDB[(🗄️ 向量数据库<br/>ChromaDB / Milvus)]:::db
        BusinessDB[(💾 业务数据库<br/>MySQL / PG)]:::db
        Cache[(⚡ Redis 缓存<br/>Schema/Session)]:::shared
    end

    %% === 核心链路逻辑 ===
    
    %% 1. 路由分发
    Router -->|意图: 统计/查询| S1
    Router -->|意图: 知识/流程| R1

    %% 2. Agent 与 数据库的交互
    
    %% SQL Agent 的交互
    S1 -.->|检索表结构元数据| VectorDB
    S4 <-->|执行 SQL 查询| BusinessDB
    
    %% RAG Agent 的交互
    R1 <-->|检索文档切片| VectorDB

    %% 3. 输出
    S4 --> FinalOutput(📝 最终回复):::user
    R3 --> FinalOutput