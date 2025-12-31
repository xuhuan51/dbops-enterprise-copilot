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

### 3.1 当前阶段（MVP）


```mermaid
graph TD
    %% 定义样式
    classDef user fill:#f9f,stroke:#333,stroke-width:2px;
    classDef core fill:#e1f5fe,stroke:#0277bd,stroke-width:2px;
    classDef agent fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px;
    classDef db fill:#fff9c4,stroke:#fbc02d,stroke-width:2px;
    classDef guard fill:#ffebee,stroke:#c62828,stroke-width:2px,stroke-dasharray: 5 5;
    classDef obs fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px;

    %% 外部入口
    User((User / Client)):::user
    Gateway[API Gateway / Auth Middleware]:::core
    
    %% 核心路由层
    subgraph "🧠 语义路由层 (Semantic Router)"
        Classifier[Intent Classifier<br/>LLM / Semantic Router]:::core
        RouteDecision{Decision}
    end

    %% SQL Agent 链路
    subgraph "📊 SQL Agent (Structured Data)"
        PermCheck[🔒 权限过滤<br/>RBAC Filter]:::guard
        SchemaLink[🔍 Schema Linking<br/>Vector Search]:::agent
        SQLGen[📝 SQL Generation<br/>Text-to-SQL]:::agent
        
        subgraph "🛡️ 安全护栏"
            SyntaxCheck[语法检查]:::guard
            SecurityCheck[DML/DDL 拦截<br/>LIMIT 强制注入]:::guard
        end
        
        Executor[⚙️ SQL Executor]:::agent
        DataInterp[💡 结果解释<br/>Data-to-Text]:::agent
    end

    %% Doc RAG 链路
    subgraph "📄 Doc RAG Agent (Unstructured Data)"
        DocIngest[📥 Ingestion Pipeline<br/>Hash Check / Chunking]:::agent
        HybridSearch[🔍 混合检索<br/>BM25 + Embedding]:::agent
        Rerank[📶 Rerank<br/>重排序]:::agent
        RefinePrompt[📝 Context Refinement]:::agent
        DocGen[💡 引用生成<br/>Answer + Citations]:::agent
    end

    %% 数据存储层
    subgraph "💾 存储与基础设施"
        VectorDB[(ChromaDB / Milvus<br/>Schemas & Docs)]:::db
        BusinessDB[(Business DB<br/>MySQL / PG)]:::db
        Redis[(Redis Cache)]:::db
    end

    %% 可观测性侧车
    subgraph "👀 可观测性 & 审计"
        Trace[Trace ID 追踪]:::obs
        AuditLog[审计日志 JSONL]:::obs
        Feedback[用户反馈 Loop]:::obs
    end

    %% 连线逻辑
    User --> Gateway
    Gateway --> Classifier
    Classifier -->|Route & Confidence| RouteDecision

    %% 分流逻辑
    RouteDecision -->|SQL Intent| PermCheck
    RouteDecision -->|Doc Intent| HybridSearch
    RouteDecision -->|Ambiguous| Clarify[❓ 追问/澄清]:::core

    %% SQL 流程
    PermCheck --> SchemaLink
    SchemaLink <--> VectorDB
    SchemaLink --> SQLGen
    SQLGen --> SyntaxCheck
    SyntaxCheck --> SecurityCheck
    SecurityCheck -->|Pass| Executor
    SecurityCheck -->|Block| ErrorHandler[🚫 拒绝执行]:::guard
    Executor <--> BusinessDB
    Executor --> DataInterp

    %% RAG 流程
    HybridSearch <--> VectorDB
    HybridSearch --> Rerank
    Rerank --> RefinePrompt
    RefinePrompt --> DocGen

    %% 输出与监控
    DataInterp --> Output[最终响应]
    DocGen --> Output
    Output --> User
    
    %% 监控连线
    Gateway -.-> Trace
    Executor -.-> AuditLog
    DocGen -.-> Feedback
