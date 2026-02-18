# DeepOps Enterprise Copilot ⚡

> **Next-Gen Text-to-SQL Agent powered by LangGraph & Graph RAG**
> 
> *基于图谱增强与自我反思机制的企业级数据库运维智能助手*

[![Status](https://img.shields.io/badge/Status-Internal_v2.0-blueviolet)]()
[![Stack](https://img.shields.io/badge/Tech-LangGraph_|_NetworkX_|_Milvus_|_FastAPI-green)]()
[![License](https://img.shields.io/badge/License-MIT-blue)]()

![DeepOps Demo](assets/demo.gif)
*(演示：自然语言查询 -> 意图识别 -> 混合检索 -> 图谱推理 -> SQL 自愈生成 -> 智能可视化)*

## 📖 项目简介 (Introduction)

**DeepOps Enterprise Copilot** 是一个面向复杂企业数据库场景的智能运维 Agent。

针对传统 Text-to-SQL 在**多表关联 (JOIN)** 和 **业务术语理解** 上的痛点，本项目采用了 **LangGraph** 构建环形状态机，并创新性地引入了 **Schema Graph RAG** 技术。通过 NetworkX 构建的全库表关系图谱，系统能够自动计算最佳 JOIN 路径，结合语义检索（Milvus）和值扫描（Value Scanning），实现高准确率的 SQL 生成与自愈。

---

## 🚀 核心技术架构 (Architecture)

### 1. 🧠 Agentic Workflow (基于 LangGraph 的状态机)
系统摒弃了线性的 Chain 结构，采用 **LangGraph** 定义了一个具备“反思”能力的循环工作流：

```mermaid
graph TD
    %% --- 样式定义 ---
    classDef action fill:#0984e3,color:white,stroke:none,rx:5,ry:5;
    classDef check fill:#f1c40f,color:black,stroke:none,rx:5,ry:5;
    classDef db fill:#6c5ce7,color:white,stroke:none,rx:5,ry:5;
    classDef module fill:#2d3436,color:white,stroke:#fff,stroke-width:1px,stroke-dasharray: 5 5;
    classDef output fill:#00b894,color:white,stroke:none,rx:5,ry:5;

    %% --- 主流程开始 ---
    Start((用户提问)) --> Router["🚦 意图路由"]:::action
    Router --> Expand["🔍 关键词扩展"]:::action
    
    %% --- 检索与精选层 ---
    subgraph RetrievalLayer ["📚 检索与精选层 (Retrieval & Selection)"]
        direction TB
        
        %% 检索编排
        Expand --> Orchestrator("🧠 混合检索编排 (Orchestrator)"):::module
        
        Orchestrator -->|并行召回| VectorSearch["📐 向量检索<br/>(Schema / Few-Shot)"]:::db
        Orchestrator -->|并行召回| RuleSearch["🧠 知识库检索<br/>(Business Rules)"]:::db
        
        %% 结果融合
        VectorSearch --> RawContext
        RuleSearch --> RawContext("📦 原始上下文"):::module
        
        %% 新增：精选列节点
        RawContext --> Selector["🎯 智能精选列 (Column Selector)<br/>Token 压缩 / 干扰剔除"]:::action
    end

    %% --- 核心反思循环 ---
    subgraph CoreLoop ["⚡ 生成与反思循环 (Reflective Loop)"]
        Selector --> Generate["📝 SQL 生成"]:::action
        Generate --> Verify{"🛡️ 安全校验"}:::check
        
        Verify --"❌ 语法/权限拦截"--> Generate
        Verify --"✅ 校验通过"--> Execute{"⚡ SQL 执行"}:::check
        
        Execute --"❌ 运行时报错<br/>(触发自愈)"--> Generate
    end
    
    %% --- 分析与输出 (新增) ---
    subgraph InsightLayer ["📊 洞察与可视化层 (Insight Layer)"]
        Execute --"✅ 获取数据"--> Analysis["🤖 数据分析师 (Analysis)<br/>自然语言总结 + 图表配置"]:::output
    end

    Analysis --> End((最终响应))

    %% --- 底部图例 ---
    style RetrievalLayer fill:#dfe6e9,stroke:#b2bec3,color:#2d3436
    style InsightLayer fill:#e8f7f5,stroke:#00b894,color:#2d3436
```

2. 🌟 核心特性 (Features)

🛡️ 企业级 RAG 检索：融合向量检索 (Milvus) 与 关键词匹配，精准定位 Schema 和业务规则。

🎯 智能精选列 (Smart Selection)：在生成 SQL 前，先通过 LLM 剔除无关列，大幅减少上下文 Token 消耗，提升准确率。

⚡ SQL 自愈机制：生成 -> 校验 -> 执行 -> 报错重试 (Reflective Loop)，像人类工程师一样自动修复 SQL 错误。

📊 智能可视化 (Auto Viz)：Agent 自动判断数据特征，生成 ECharts/Streamlit 图表配置（柱状图、折线图、饼图等）。

🖥️ 全栈交互：提供 FastAPI 流式接口 (NDJSON) 与 Streamlit 交互式前端。

## 2. 项目结构（阶段性演进）
```text
dbops-enterprise-copilot/
├── 📜 main.py                    # [Entry] FastAPI 后端启动入口 (Lifespan 预热)
├── 📜 app_ui.py                  # [Entry] Streamlit 前端启动入口
├── 📂 app/
│   ├── 📂 api/
│   │   └── v1/
│   │       └── agent.py          # [API] 核心 Agent 对话接口路由
│   │
│   ├── 📂 core/                  # [Infra] 基础设施与单例配置
│   │   ├── config.py             # 全局环境变量配置
│   │   ├── embedding.py          # Embedding 模型单例
│   │   ├── llm.py                # LLM 模型工厂
│   │   ├── logger.py             # 日志配置系统
│   │   ├── mysql_saver.py        # LangGraph 状态持久化 (Checkpoint)
│   │   ├── prompts.py            # Prompt 模板库 (管理所有提示词)
│   │   ├── rag_store.py          # Milvus DAO (向量库管理)
│   │   ├── reranker.py           # Rerank 重排序模型单例
│   │   └── state.py              # AgentState 全局状态定义
│   │
│   ├── 📂 graph/                 # [Brain] LangGraph 智能体状态机
│   │   ├── graph.py              # 工作流编排 (Workflow & Edges)
│   │   └── 📂 nodes/             # 独立原子功能节点
│   │       ├── router_node.py          # 🚦 意图识别与路由
│   │       ├── expand_node.py          # 🔍 关键词扩展与联想
│   │       ├── retrieval_node.py       # 📚 混合检索调度 (Orchestrator调用)
│   │       ├── column_selector_node.py # 🎯 智能精选列 (Schema Pruning)
│   │       ├── generate_node.py        # 📝 SQL 生成与修正
│   │       ├── verification_node.py    # 🛡️ 语法检查与权限审计
│   │       ├── execution_node.py       # ⚡ SQL 执行与反馈
│   │       └── analysis_node.py        # 📊 结果分析与可视化配置
│   │
│   └── 📂 modules/               # [Components] 核心业务组件
│       ├── 📂 retrieval/         # 🔍 检索增强引擎 (RAG Engine)
│       │   ├── orchestrator.py       # 🧠 总检索编排器
│       │   ├── 📂 graph/             # 🕸️ Graph RAG (图谱增强)
│       │   │   ├── builder.py            # 图构建器 (NetworkX)
│       │   │   ├── searcher.py           # 路径搜索 (Steiner Tree)
│       │   │   └── service.py            # 图服务单例
│       │   ├── 📂 knowledge/         # 🧠 业务知识库
│       │   │   └── retriever.py          # 规则检索器
│       │   └── 📂 schema/            # 🗃️ Schema 向量库
│       │       ├── retriever.py          # 表/列向量检索
│       │       └── value_linker.py       # 值-列 语义映射 (Value Linking)
│       │
│       └── 📂 sql/               # ⚡ 执行层
│           ├── executor.py           # SQL 执行器 (连接池管理)
│           └── guardrail.py          # 安全护栏 (Read-Only 拦截)
│
└── 📂 assets/                    # 静态资源
    └── demo.gif
```
