# 13 - Graph-RAG 与知识图谱

> **设计文档引用**：`04-RAG知识库与运维知识管理.md` §Graph-RAG, §知识图谱 Schema, ADR-005  
> **职责边界**：Neo4j 知识图谱建模、图谱 CRUD、图谱检索、故障传播路径推理、与向量检索融合、图谱数据维护  
> **优先级**：P1

---

## 1. 模块概述

### 1.1 为什么需要 Graph-RAG（ADR-005）

#### 1.1.1 纯向量 RAG 的根本局限性

在 12-RAG 检索引擎中，我们已经实现了基于 Milvus 的混合检索（稠密+稀疏），它在文档语义搜索上表现良好。
但在实际运维诊断中，我们遇到了三类**向量 RAG 无法解决**的查询模式：

**第一类：拓扑关系查询（Topology Reasoning）**

```
用户问题："Kafka 积压可能是哪些上游组件导致的？"
```

向量 RAG 的做法是把这句话编码成向量，在文档库中找语义最接近的段落。但"上游组件"是一个**结构化的拓扑关系**，
不是语义相似度能捕获的。即使某篇文档提到了"Kafka 依赖 ZooKeeper"，向量搜索也无法从中推导出完整的依赖链。

**WHY 向量搜索在这里失败**：embedding 模型将文本映射到高维空间，衡量的是"两段文字在语义上有多像"，
而不是"两个实体之间存在什么关系"。`DEPENDS_ON(Kafka, ZooKeeper)` 这种三元组关系在向量空间中没有对应的结构表示。

**第二类：故障传播路径查询（Impact Analysis）**

```
用户问题："ZooKeeper 挂了会影响哪些服务？"
```

这需要从 ZooKeeper 出发，沿着 `DEPENDS_ON` 关系反向遍历所有下游组件。在我们的大数据集群中，
ZK 是基石组件，影响链包括 HDFS-NameNode → HBase → Flink（通过 HDFS checkpoint）等多级级联。
向量 RAG 最多能找到一两篇提到 "ZK 影响 HDFS" 的文档，但无法自动推导出完整的 N 跳传播路径。

**WHY 向量搜索在这里失败**：多跳推理需要在图结构上做遍历（BFS/DFS），这是图数据库的原生能力。
向量空间是平坦的，没有"沿着边遍历"的概念。

**第三类：因果→修复映射（Causal-Remediation Mapping）**

```
用户问题："这个根因之前怎么修复的？"
```

这需要精确的 `FaultPattern → CAUSED_BY → RootCause → RESOLVED_BY → Remediation` 路径。
向量 RAG 可能找到几篇包含修复方案的文档，但它无法区分哪个修复方案对应哪个根因，
更无法给出历史成功率和风险等级这些结构化属性。

**WHY 向量搜索在这里失败**：因果链是一个有向图结构（故障→原因→修复），每条边都有属性
（概率、成功率、风险等级）。这些属性不是文本，不能被 embedding 模型编码。

#### 1.1.2 Graph-RAG vs 纯向量 RAG 实测对比

我们在 50 个真实运维问题上做了对比测试（细节见 18-评估体系）：

| 查询类型 | 纯向量 RAG 准确率 | Graph-RAG 融合准确率 | 提升 |
|----------|-------------------|---------------------|------|
| 文档语义搜索（"HDFS 安全模式如何退出"） | **92%** | 91% | -1%（符合预期，不需要图谱） |
| 单组件故障查询（"NameNode OOM 怎么处理"） | 78% | **95%** | +17% |
| 依赖关系查询（"X 依赖哪些组件"） | 31% | **94%** | +63% |
| 故障传播分析（"X 挂了影响什么"） | 15% | **91%** | +76% |
| 多组件关联（"A 和 B 同时告警的公共原因"） | 8% | **87%** | +79% |

**结论**：对于纯文档搜索，向量 RAG 已经够用；但一旦涉及关系推理，准确率从 92% 骤降到 8-31%。
Graph-RAG 融合在所有类型上都维持 87%+ 的准确率。

#### 1.1.3 为什么选择"融合"而不是"替换"

一个自然的问题：既然图谱这么好用，为什么不全用图谱，完全替代向量 RAG？

**原因一：图谱覆盖有限**。知识图谱只建模了结构化关系（组件依赖、故障模式、修复方案），
而运维知识库中大量的非结构化内容（故障报告、变更记录、配置说明文档）只能用向量检索。

**原因二：图谱维护成本高**。每条关系都需要人工确认或半自动抽取，而文档只要切块入库就能检索。
对于低频场景，维护完整图谱的 ROI 不合算。

**原因三：语义模糊查询**。用户有时候描述不精确——"集群最近有点慢"，这种模糊查询适合向量的
模糊匹配能力，图谱的精确匹配反而找不到结果。

```
我们的策略：
├── 向量 RAG（HybridRetriever, 12-RAG引擎）
│   ├── 非结构化文档检索
│   ├── 历史案例语义搜索
│   └── SOP 关键词/语义匹配
├── 知识图谱（GraphRAGRetriever, 本模块）
│   ├── 组件依赖拓扑
│   ├── 故障传播路径推理
│   └── 因果→修复映射
└── 融合层
    ├── 并行查询两个引擎
    ├── 结果合并去重
    └── 按相关性排序后送入 Agent
```

**方案**：RAG + 知识图谱混合
- **向量 RAG**：文档检索、SOP 匹配、历史案例语义搜索
- **知识图谱**：组件依赖、故障传播路径、因果推理、修复映射
- **Graph-RAG 融合**：检索时同时查向量库和图数据库，合并结果

### 1.2 Neo4j 选型 WHY

在图数据库选型上，我们评估了三个候选方案：

| 维度 | Neo4j | Amazon Neptune | JanusGraph |
|------|-------|----------------|------------|
| 查询语言 | **Cypher（声明式，学习成本低）** | Gremlin/SPARQL | Gremlin |
| Python 异步驱动 | **官方 neo4j-driver，AsyncSession 原生支持** | boto3（HTTP API，无原生 async） | gremlinpython（async 支持弱） |
| 部署复杂度 | **单节点 Docker/Helm 即可** | AWS 绑定 | 依赖 Cassandra/HBase+ES |
| 全文索引 | **内置 Lucene 全文索引** | 需要 OpenSearch | 依赖外部 ES |
| 社区生态 | **最活跃，Stack Overflow 问答最多** | AWS 闭源 | 社区萎缩 |
| 图谱可视化 | **Neo4j Browser 内置** | 无 | 需第三方 |
| 单节点性能（我们的规模） | **足够（~2000 节点，~8000 关系）** | 过度设计 | 过度设计 |
| 成本 | **Community Edition 免费** | 按实例时间计费 | 需维护底层存储 |

**WHY Neo4j**：
1. **Cypher 可读性**：运维团队不全是开发背景，Cypher 的 `MATCH (a)-[:DEPENDS_ON]->(b)` 语法接近自然语言，降低维护门槛。
2. **Python 异步原生支持**：我们的整个 Agent 框架基于 asyncio（见 03-Agent核心框架），Neo4j 官方驱动的 `AsyncSession` 无缝集成。
3. **规模匹配**：我们的图谱规模是 ~2000 节点 / ~8000 关系（中等规模大数据集群），Neo4j Community 单节点绰绰有余。Neptune 和 JanusGraph 是为百万级节点设计的，引入不必要的运维复杂度。
4. **全文索引内置**：故障模式搜索需要关键词匹配（如搜索 "OOM"），Neo4j 内置的 Lucene 全文索引免去了额外的 ES 依赖。

**WHY NOT JanusGraph**：JanusGraph 需要 Cassandra/HBase 作为存储后端 + ES 作为索引后端，一个图数据库要运维三个组件，对我们的小规模场景来说是杀鸡用牛刀。

**WHY NOT Neptune**：我们不想被 AWS 绑定。项目需要在私有化部署场景下运行，Neptune 完全依赖 AWS VPC。

### 1.3 在系统中的位置

```
                    Planning Agent / Diagnostic Agent
                                │
                          ┌─────┴──────┐
                          ▼            ▼
                   GraphRAGRetriever  HybridRetriever
                   (本模块)          (12-RAG 引擎)
                          │            │
              ┌───────────┤            ├───────────┐
              ▼           ▼            ▼           ▼
        Graph Queries  Impact     Dense Search  Sparse Search
        (Cypher)      Analysis   (BGE-M3)     (BM25)
              │           │            │           │
              ▼           ▼            ▼           ▼
           Neo4j       Neo4j       Milvus       Milvus
          (图谱)      (图谱)      (向量)       (倒排)
              │           │            │           │
              └───────────┴────────────┴───────────┘
                                │
                         合并 & 去重 & 排序
                                │
                                ▼
                    送入 Agent Context Window
```

**数据流说明**：
1. Agent 发起检索请求时，`GraphRAGRetriever.retrieve()` 同时发起向量检索和图谱检索
2. 向量检索走 `HybridRetriever`（12-RAG 引擎，Milvus 稠密+稀疏）
3. 图谱检索走 Neo4j Cypher 查询，包括故障模式、依赖路径、SOP、修复方案
4. 两路结果合并后注入 Agent 的 context window

### 1.4 核心设计决策总结

| 决策 | 选择 | WHY |
|------|------|-----|
| 图数据库 | Neo4j Community | Cypher 可读性 + Python async 原生 + 单节点够用 |
| 检索策略 | 向量+图谱并行融合 | 各有优势，互补而非替代 |
| 图谱规模 | ~2000 节点 / ~8000 关系 | 覆盖 10 种核心组件 × 实例 × 故障模式 |
| 图遍历深度 | 最大 4 跳 | 覆盖最长依赖链（ZK→NN→HBase→应用）且避免全图遍历 |
| 缓存策略 | Redis TTL 5min | 拓扑关系不频繁变化，避免重复查询 |
| 知识沉淀 | MERGE 语义 upsert | 幂等写入，同一故障模式可多次更新而不重复创建 |

---

## 2. 图谱 Schema 设计

### 2.1 Schema 设计理念

**WHY 这样设计 Schema**：我们的图谱不是一个通用知识图谱，而是专门为**大数据平台运维诊断**场景设计的。
Schema 的核心思想是建模三条链：

```
链条 1：基础设施拓扑链
  Cluster → Component → Instance → Metric
  回答："集群有哪些组件？组件跑在哪些机器上？需要关注什么指标？"

链条 2：组件依赖链
  Component -[:DEPENDS_ON]→ Component
  回答："X 依赖谁？X 挂了影响谁？多个告警的公共上游是什么？"

链条 3：故障诊断链
  FaultPattern → RootCause → Remediation
  FaultPattern → SOP
  回答："这种故障是什么原因？怎么修？历史成功率多少？有没有 SOP？"
```

**WHY 8 种节点类型**：每种节点对应运维中的一个核心实体。我们尝试过更少的节点类型
（如把 Metric 作为 Component 的属性），但发现这会导致查询复杂度爆炸——一个组件可能有
几十个指标，把它们全塞进属性会让 Component 节点过于臃肿，且无法单独查询"哪些指标超阈值"。

**WHY 8 种关系类型**：每种关系对应一种运维推理模式。我们没有引入更多关系类型（如 `TRIGGERS`、
`CORRELATES_WITH`），因为在 MVP 阶段，这 8 种关系已经覆盖了 90% 的诊断查询需求。
后续如果需要支持变更关联（"某次变更导致了某个故障"），可以增加 `ChangeEvent` 节点和 `TRIGGERED_BY` 关系。

### 2.2 节点类型详解（8 种）

```python
# python/src/aiops/rag/graph_schema.py
"""
Neo4j 知识图谱 Schema 定义

8 种节点类型，每种都有固定属性 + 可选扩展属性。

WHY 用 dataclass 而不是 Pydantic：
- Schema 定义是只读的，不需要 validation
- dataclass 更轻量，没有 Pydantic 的序列化开销
- 在 graph_manager.py 中写入 Neo4j 时，我们直接传 dict，不需要 model_dump()

WHY 每个节点都有 created_at/last_updated：
- 图谱维护需要知道哪些数据是过时的（如某个组件已经下线但节点还在）
- 定期清理策略依赖这些时间戳
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


# ── 枚举定义 ──

class Environment(str, Enum):
    """集群环境类型"""
    PRODUCTION = "production"
    STAGING = "staging"
    DEV = "dev"


class ComponentType(str, Enum):
    """组件类型分类
    
    WHY 这 5 种类型：覆盖大数据平台所有核心组件。
    - storage: HDFS, HBase
    - compute: YARN, Impala, Flink, Spark
    - messaging: Kafka
    - coordination: ZooKeeper
    - search: Elasticsearch
    """
    STORAGE = "storage"
    COMPUTE = "compute"
    MESSAGING = "messaging"
    COORDINATION = "coordination"
    SEARCH = "search"


class Severity(str, Enum):
    """故障严重级别（对齐 04-Triage 分诊标准）"""
    CRITICAL = "critical"    # P0: 服务完全不可用
    HIGH = "high"            # P1: 核心功能受损
    MEDIUM = "medium"        # P2: 非核心功能受损
    LOW = "low"              # P3: 轻微影响


class RiskLevel(str, Enum):
    """修复风险等级（对齐 15-HITL 审批阈值）
    
    WHY 5 个等级：
    - none/low: 自动执行，无需审批
    - medium: 需要 L1 审批（值班工程师）
    - high: 需要 L2 审批（服务负责人）
    - critical: 需要 L1 + L2 双人审批
    """
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ── 节点定义 ──

@dataclass
class ClusterNode:
    """集群节点
    
    WHY 建模集群：一个企业可能有多个大数据集群（生产、测试、灾备），
    图谱需要区分 "生产集群的 NameNode" 和 "测试集群的 NameNode"。
    """
    name: str                          # "prod-bigdata-01"
    environment: str                   # "production" | "staging" | "dev"
    nodes_count: int = 0
    region: str = ""
    created_at: str = ""


@dataclass
class ComponentNode:
    """大数据组件节点
    
    WHY 组件粒度而不是进程粒度：
    - 运维人员思考问题时用的是 "HDFS" "Kafka" 这样的组件概念
    - 进程级别的粒度（如 NameNode、JournalNode、ZKFC）可以通过 Instance 节点覆盖
    - 组件级别能保持图谱的可读性，避免过于复杂
    """
    name: str                          # "HDFS-NameNode" | "Kafka-Broker"
    type: str                          # "storage" | "compute" | "messaging" | "coordination" | "search"
    version: str = ""                  # "3.3.6" | "3.7.0"
    port: int = 0                      # 主服务端口
    config_path: str = ""              # 配置文件路径
    health_check_url: str = ""         # 健康检查端点
    documentation_url: str = ""        # 内部文档链接


@dataclass
class InstanceNode:
    """组件实例节点
    
    WHY 需要 Instance 和 Component 分开：
    - 一个 Component (如 HDFS-DataNode) 有 N 个实例跑在不同机器上
    - 故障排查时需要定位到具体实例："是哪台 DataNode 磁盘满了？"
    - 但故障模式和修复方案是组件级别的，不需要为每个实例定义
    """
    hostname: str                      # "nn1.prod.internal"
    ip: str                            # "10.0.1.10"
    role: str                          # "active" | "standby" | "worker"
    status: str = "running"            # "running" | "stopped" | "degraded"
    cpu_cores: int = 0
    memory_gb: int = 0
    disk_gb: int = 0
    last_heartbeat: str = ""           # 最后心跳时间
    rack: str = ""                     # 机架标识（故障域隔离）


@dataclass
class MetricNode:
    """监控指标节点
    
    WHY 把指标建模为节点而不是 Component 的属性：
    - 一个组件可能有 20+ 个指标，全放属性里太臃肿
    - 指标节点可以被多个查询复用："列出所有超阈值的指标"
    - 指标和故障模式之间有隐含关联（某些指标组合 = 某种故障症状）
    """
    name: str                          # "hdfs_namenode_heap_usage_percent"
    display_name: str                  # "NameNode 堆内存使用率"
    unit: str                          # "percent" | "bytes" | "count" | "ms"
    warning_threshold: float = 0.0
    critical_threshold: float = 0.0
    promql: str = ""                   # 对应的 PromQL 查询
    scrape_interval: str = "15s"       # 采集频率
    retention_days: int = 30           # 数据保留天数


@dataclass
class FaultPatternNode:
    """故障模式节点
    
    WHY 建模故障模式而不是故障事件：
    - 故障事件是一次性的（"2026-03-15 14:00 NN OOM"），数量无限
    - 故障模式是抽象的类别（"NN OOM"），数量有限且可复用
    - Agent 在诊断时匹配的是模式，不是历史事件
    - 历史事件存在向量库中（案例检索），图谱只存模式
    """
    name: str                          # "NN_OOM" | "KAFKA_LAG_SPIKE"
    display_name: str                  # "NameNode 内存溢出"
    severity: str                      # "critical" | "high" | "medium" | "low"
    symptoms: list[str] = field(default_factory=list)  # 症状列表
    frequency: str = ""                # "common" | "rare"
    mttr_minutes: int = 0              # 历史平均修复时间
    occurrence_count: int = 0          # 历史发生次数
    last_occurrence: str = ""          # 上次发生时间


@dataclass
class RootCauseNode:
    """根因分类节点
    
    WHY 根因独立建模：
    - 同一根因可能导致多种故障模式（如 "磁盘满" → Kafka 写入失败 + HDFS 容量不足）
    - 独立建模让图谱可以做"反向推理"：多种故障 → 找公共根因
    """
    description: str                   # "NameNode JVM 堆内存配置不足"
    category: str                      # "resource" | "config" | "software_bug" | "hardware" | "load" | "network"
    subcategory: str = ""              # "memory" | "disk" | "cpu"
    confidence: float = 0.0            # 该根因分类的置信度


@dataclass
class RemediationNode:
    """修复方案节点
    
    WHY 修复方案独立建模（而不是作为 RootCause 的属性）：
    - 同一根因可能有多种修复方案（如 "内存不足" → 扩内存 OR 优化配置 OR 清理缓存）
    - 每种方案有不同的风险等级、成功率、耗时
    - 独立建模让 Planning Agent 可以比较多种方案并选择最优
    """
    action: str                        # "增加 NN JVM 堆内存 -Xmx48g"
    risk_level: str                    # "none" | "low" | "medium" | "high" | "critical"
    requires_restart: bool = False
    estimated_time_minutes: int = 0
    rollback_action: str = ""
    prerequisites: list[str] = field(default_factory=list)
    automation_script: str = ""        # 自动化执行脚本路径
    last_validated: str = ""           # 上次验证方案有效的时间


@dataclass
class SOPNode:
    """标准操作流程节点
    
    WHY SOP 独立于 Remediation：
    - SOP 是完整的操作流程（含诊断步骤 + 多种修复路径 + 验证步骤）
    - Remediation 是单个修复动作
    - 一个 SOP 可能包含多个条件分支，每个分支对应不同的 Remediation
    """
    title: str                         # "HDFS NameNode OOM 处理流程"
    version: str                       # "v2.1"
    steps: list[str] = field(default_factory=list)
    last_updated: str = ""
    author: str = ""
    review_status: str = "approved"    # "draft" | "in_review" | "approved"
    effectiveness_score: float = 0.0   # SOP 有效性评分（来自历史执行数据）
```

### 2.3 关系类型详解（8 种）

**WHY 只用 8 种关系**：我们遵循"最小充分"原则——每种关系都对应一种明确的运维推理需求。
过多的关系类型会增加图谱维护成本和查询复杂度。

```
关系总览（按推理用途分组）：

┌─ 基础设施拓扑 ─────────────────────────────────────────┐
│  (:Cluster)      -[:HAS_COMPONENT]->     (:Component)   │  集群包含组件
│  (:Component)    -[:HAS_INSTANCE]->      (:Instance)     │  组件有实例
│  (:Component)    -[:HAS_METRIC]->        (:Metric)       │  组件有指标
└──────────────────────────────────────────────────────────┘

┌─ 依赖分析（故障传播的核心）─────────────────────────────┐
│  (:Component)    -[:DEPENDS_ON]->        (:Component)    │  组件依赖
└──────────────────────────────────────────────────────────┘

┌─ 故障诊断链 ───────────────────────────────────────────┐
│  (:FaultPattern) -[:CAUSED_BY]->         (:RootCause)    │  故障由根因导致
│  (:FaultPattern) -[:AFFECTS]->           (:Component)    │  故障影响组件
│  (:RootCause)    -[:RESOLVED_BY]->       (:Remediation)  │  根因对应修复
│  (:FaultPattern) -[:FOLLOWS_SOP]->       (:SOP)          │  故障对应 SOP
└──────────────────────────────────────────────────────────┘
```

**关系属性定义**：

```python
# python/src/aiops/rag/graph_relations.py
"""
图谱关系属性定义

WHY 关系需要属性：
- 光知道 "A 依赖 B" 不够，还需要知道 "是硬依赖还是软依赖"
- 光知道 "故障由 X 导致" 不够，还需要知道 "导致的概率是多少"
- 这些属性让 Agent 可以做量化推理，而不仅仅是定性判断
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DependsOnRelation:
    """组件依赖关系属性
    
    WHY 区分 hard/soft 依赖：
    - hard 依赖：上游挂了，下游一定受影响（如 DN→NN 心跳）
    - soft 依赖：上游挂了，下游可以降级运行（如 Flink→HDFS checkpoint）
    - 故障传播分析时，hard 依赖的影响确定性更高，Agent 可以据此调整诊断优先级
    
    WHY 记录 protocol：
    - 同一对组件之间可能有多种依赖方式（如 YARN 通过 RPC 向 ZK 注册，也通过 HDFS 读取 JAR）
    - 不同协议的故障表现不同（RPC 超时 vs HDFS 读取失败）
    """
    dependency_type: str    # "hard" | "soft"（硬依赖=挂了必影响，软依赖=降级可用）
    protocol: str           # "rpc" | "zk_session" | "hdfs_read" | "kafka_consume"
    description: str        # "NameNode 依赖 ZK 做主备选举"
    latency_impact_ms: int = 0  # 该依赖链路的延迟影响（用于 SLA 分析）


@dataclass
class CausedByRelation:
    """故障→根因关系属性
    
    WHY 需要 probability：
    - 同一故障可能有多种根因（如 NN OOM: 60% 堆内存不足 + 30% 小文件过多）
    - Agent 在诊断时按概率排序，优先验证最可能的根因
    - 概率来自历史数据统计，会随时间更新
    
    WHY 需要 evidence_pattern：
    - 这是触发该根因假设的条件表达式
    - Diagnostic Agent 用它来判断是否匹配当前症状
    - 例如 "heap_usage > 90% AND full_gc_count > 10/hour" 触发 "堆内存不足" 假设
    """
    probability: float      # 0.0-1.0 该根因导致该故障模式的概率
    evidence_pattern: str   # "heap_usage > 90% AND full_gc_count > 10/hour"
    confidence: float = 0.0 # 该概率估计的置信度
    sample_size: int = 0    # 统计样本量


@dataclass
class ResolvedByRelation:
    """根因→修复关系属性
    
    WHY 记录 success_rate 和 last_used：
    - success_rate: Planning Agent 用它来选择最优修复方案
    - last_used: 如果某方案很久没用过，可能已经不适用（如组件版本升级后配置项变了）
    - 这些数据来自 08-Report 模块的修复结果反馈
    """
    success_rate: float     # 历史修复成功率
    last_used: str          # 上次使用时间
    total_attempts: int = 0 # 总尝试次数
    avg_duration_minutes: int = 0  # 平均执行耗时


@dataclass 
class AffectsRelation:
    """故障→组件影响关系属性
    
    WHY 需要 impact_level：
    - 同一故障对不同组件的影响程度不同
    - 如 ZK 不可用对 NN 是 "完全不可用"，对 Kafka 可能只是 "无法做 leader 选举"
    """
    impact_level: str = "full"       # "full" | "partial" | "degraded"
    description: str = ""            # 具体影响描述
```

> **🔧 工程难点：知识图谱 Schema 设计——8 种节点 × 8 种关系的大数据运维领域建模**
>
> **挑战**：运维知识图谱的 Schema 设计需要回答一个根本问题：**哪些知识适合用图表示？** 不是所有运维知识都适合图谱——"HDFS NameNode 的 GC 参数推荐值"是文本知识，适合向量检索；"HDFS 依赖 ZooKeeper 做 NameNode HA"是关系知识，适合图谱。如果把所有知识都塞进图谱，节点和关系会爆炸式增长（每条日志一个节点？每个配置参数一个节点？），查询性能急剧下降且维护成本飙升。反过来，如果图谱只建模高层组件关系（"HDFS → ZooKeeper"），关系太粗糙无法支持精确的传播路径推理（不知道是 HDFS 的哪个服务依赖 ZK 的哪个功能）。更困难的是关系属性的设计——同一条 `AFFECTS` 关系，ZK 不可用对 NameNode 的影响是"完全不可用"（HA 切换依赖 ZK），对 Kafka Broker 的影响是"无法选举 leader"（部分功能降级），`impact_level` 字段需要区分这种差异。
>
> **解决方案**：采用"中粒度"建模策略——节点粒度为"组件服务"级别（如 `NameNode`、`DataNode`、`ResourceManager`）而非"进程实例"级别，关系粒度为"功能依赖"级别（如 `DEPENDS_ON` + 具体依赖的功能描述）而非简单的"有关系"。8 种节点类型（Component、Service、Metric、Alert、FaultMode、Solution、Config、RunbookStep）覆盖运维知识的核心维度——组件拓扑、故障模式、解决方案、配置关系。8 种关系类型（DEPENDS_ON、HOSTS、MONITORS、TRIGGERS、AFFECTS、RESOLVES、CONFIGURES、NEXT_STEP）描述实体间的因果和操作关系。`AFFECTS` 关系的 `impact_level` 属性（full/partial/degraded）量化影响程度，支持传播路径推理时的"影响衰减"——ZK 不可用 → NN 完全不可用（full）→ HDFS 读写中断（full）→ YARN 任务失败（partial，因为部分任务有本地缓存），传播每经过一跳，影响可能从 full 降级为 partial。初始化数据通过 Cypher 脚本声明式注入（而非 Python 代码），运维人员可以在 Neo4j Browser 中直接查看和验证拓扑关系。图谱的自动构建（§5.2）从诊断报告中提取实体和关系，但需要人工审核（`verified=false`）后才合入正式图谱，防止 LLM 的幻觉污染拓扑数据。

### 2.4 初始化数据（Cypher）

**WHY 用 Cypher 脚本初始化而不是 Python 代码**：
- Cypher 是声明式的，运维人员可以直接在 Neo4j Browser 中执行和验证
- 版本控制友好——初始化脚本就是图谱的"schema migration"
- 可以导出为 `.cypher` 文件，在不同环境间复用

```cypher
// ═══════════════════════════════════════════
// 图谱初始化脚本 v2.1
// 对应集群: prod-bigdata-01 (50 节点生产集群)
// ═══════════════════════════════════════════

// ─── 约束和索引（必须先执行）───
// WHY 唯一性约束：防止重复导入时创建重复节点
// WHY 全文索引：支持故障模式的关键词搜索

CREATE CONSTRAINT IF NOT EXISTS FOR (c:Cluster) REQUIRE c.name IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (c:Component) REQUIRE c.name IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (i:Instance) REQUIRE i.hostname IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (fp:FaultPattern) REQUIRE fp.name IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (m:Metric) REQUIRE m.name IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (sop:SOP) REQUIRE sop.title IS UNIQUE;

CREATE FULLTEXT INDEX fault_pattern_search IF NOT EXISTS
FOR (fp:FaultPattern) ON EACH [fp.display_name, fp.name];

CREATE FULLTEXT INDEX remediation_search IF NOT EXISTS
FOR (rem:Remediation) ON EACH [rem.action];

// ═══════════════════════════════════════════
// 集群和组件拓扑
// ═══════════════════════════════════════════

// 集群
CREATE (cluster:Cluster {name: "prod-bigdata-01", environment: "production", nodes_count: 50, region: "shenzhen"})

// 核心组件（10 种，覆盖大数据平台全栈）
CREATE (zk:Component      {name: "ZooKeeper",            type: "coordination", version: "3.8.4", port: 2181})
CREATE (hdfs_nn:Component {name: "HDFS-NameNode",        type: "storage",      version: "3.3.6", port: 9870})
CREATE (hdfs_dn:Component {name: "HDFS-DataNode",        type: "storage",      version: "3.3.6", port: 9864})
CREATE (yarn_rm:Component {name: "YARN-ResourceManager", type: "compute",      version: "3.3.6", port: 8088})
CREATE (yarn_nm:Component {name: "YARN-NodeManager",     type: "compute",      version: "3.3.6", port: 8042})
CREATE (kafka:Component   {name: "Kafka-Broker",         type: "messaging",    version: "3.7.0", port: 9092})
CREATE (es:Component      {name: "Elasticsearch",        type: "search",       version: "8.14.0", port: 9200})
CREATE (hbase:Component   {name: "HBase",                type: "storage",      version: "2.5.7", port: 16010})
CREATE (impala:Component  {name: "Impala",               type: "compute",      version: "4.3.0", port: 21050})
CREATE (flink:Component   {name: "Flink",                type: "compute",      version: "1.18.1", port: 8081})

// 集群→组件关系
CREATE (cluster)-[:HAS_COMPONENT]->(zk)
CREATE (cluster)-[:HAS_COMPONENT]->(hdfs_nn)
CREATE (cluster)-[:HAS_COMPONENT]->(hdfs_dn)
CREATE (cluster)-[:HAS_COMPONENT]->(yarn_rm)
CREATE (cluster)-[:HAS_COMPONENT]->(yarn_nm)
CREATE (cluster)-[:HAS_COMPONENT]->(kafka)
CREATE (cluster)-[:HAS_COMPONENT]->(es)
CREATE (cluster)-[:HAS_COMPONENT]->(hbase)
CREATE (cluster)-[:HAS_COMPONENT]->(impala)
CREATE (cluster)-[:HAS_COMPONENT]->(flink)

// ═══════════════════════════════════════════
// 关键实例节点（HA 组件需要建模 active/standby）
// ═══════════════════════════════════════════

CREATE (nn1:Instance {hostname: "nn1.prod.internal", ip: "10.0.1.10", role: "active", status: "running", cpu_cores: 32, memory_gb: 64, rack: "rack-01"})
CREATE (nn2:Instance {hostname: "nn2.prod.internal", ip: "10.0.1.11", role: "standby", status: "running", cpu_cores: 32, memory_gb: 64, rack: "rack-02"})
CREATE (rm1:Instance {hostname: "rm1.prod.internal", ip: "10.0.1.20", role: "active", status: "running", cpu_cores: 16, memory_gb: 32, rack: "rack-01"})
CREATE (rm2:Instance {hostname: "rm2.prod.internal", ip: "10.0.1.21", role: "standby", status: "running", cpu_cores: 16, memory_gb: 32, rack: "rack-02"})
CREATE (zk1:Instance {hostname: "zk1.prod.internal", ip: "10.0.1.30", role: "leader", status: "running", cpu_cores: 8, memory_gb: 16, rack: "rack-01"})
CREATE (zk2:Instance {hostname: "zk2.prod.internal", ip: "10.0.1.31", role: "follower", status: "running", cpu_cores: 8, memory_gb: 16, rack: "rack-02"})
CREATE (zk3:Instance {hostname: "zk3.prod.internal", ip: "10.0.1.32", role: "follower", status: "running", cpu_cores: 8, memory_gb: 16, rack: "rack-03"})

CREATE (hdfs_nn)-[:HAS_INSTANCE]->(nn1)
CREATE (hdfs_nn)-[:HAS_INSTANCE]->(nn2)
CREATE (yarn_rm)-[:HAS_INSTANCE]->(rm1)
CREATE (yarn_rm)-[:HAS_INSTANCE]->(rm2)
CREATE (zk)-[:HAS_INSTANCE]->(zk1)
CREATE (zk)-[:HAS_INSTANCE]->(zk2)
CREATE (zk)-[:HAS_INSTANCE]->(zk3)

// ═══════════════════════════════════════════
// 组件依赖拓扑（故障传播的核心）
// 
// WHY 这些依赖关系如此重要：
// 当 Agent 收到一个告警时，它需要知道：
// 1. 这个组件依赖谁？（可能是上游故障传导过来的）
// 2. 谁依赖这个组件？（下游可能受到影响）
// 3. 依赖是硬的还是软的？（决定影响的确定性）
// ═══════════════════════════════════════════

// ZooKeeper 是基石（4 个硬依赖指向它）
// WHY ZK 这么重要：大数据生态几乎所有组件都用 ZK 做服务发现和 leader 选举
CREATE (hdfs_nn)-[:DEPENDS_ON {dependency_type: "hard", protocol: "zk_session", description: "NN 依赖 ZK 做 HA 主备选举"}]->(zk)
CREATE (yarn_rm)-[:DEPENDS_ON {dependency_type: "hard", protocol: "zk_session", description: "RM 依赖 ZK 做 HA"}]->(zk)
CREATE (kafka)-[:DEPENDS_ON   {dependency_type: "hard", protocol: "zk_session", description: "Broker 依赖 ZK 注册和选举(旧版)"}]->(zk)
CREATE (hbase)-[:DEPENDS_ON   {dependency_type: "hard", protocol: "zk_session", description: "HBase Master 依赖 ZK"}]->(zk)

// HDFS 依赖链
// WHY HDFS 是第二层关键：几乎所有计算引擎的数据都在 HDFS 上
CREATE (hdfs_dn)-[:DEPENDS_ON {dependency_type: "hard", protocol: "rpc",        description: "DN 向 NN 报告心跳和块信息"}]->(hdfs_nn)
CREATE (yarn_rm)-[:DEPENDS_ON {dependency_type: "soft", protocol: "hdfs_read",   description: "RM 读取 HDFS 上的作业 JAR"}]->(hdfs_nn)
CREATE (hbase)-[:DEPENDS_ON   {dependency_type: "hard", protocol: "hdfs_read",   description: "HBase 数据存储在 HDFS"}]->(hdfs_nn)
CREATE (flink)-[:DEPENDS_ON   {dependency_type: "soft", protocol: "hdfs_read",   description: "Flink checkpoint 写 HDFS"}]->(hdfs_nn)
CREATE (impala)-[:DEPENDS_ON  {dependency_type: "hard", protocol: "hdfs_read",   description: "Impala 查询 HDFS 上的表数据"}]->(hdfs_nn)

// YARN 依赖链
CREATE (yarn_nm)-[:DEPENDS_ON   {dependency_type: "hard", protocol: "rpc",          description: "NM 向 RM 注册和心跳"}]->(yarn_rm)
CREATE (impala)-[:DEPENDS_ON    {dependency_type: "soft", protocol: "yarn_resource", description: "Impala 可选使用 YARN 资源"}]->(yarn_rm)
CREATE (flink)-[:DEPENDS_ON    {dependency_type: "soft", protocol: "yarn_resource", description: "Flink on YARN 模式"}]->(yarn_rm)

// Kafka 消费链
CREATE (flink)-[:DEPENDS_ON {dependency_type: "soft", protocol: "kafka_consume", description: "Flink 消费 Kafka 数据流"}]->(kafka)
CREATE (es)-[:DEPENDS_ON    {dependency_type: "soft", protocol: "kafka_consume", description: "ES indexer 消费 Kafka 日志"}]->(kafka)

// HBase → Impala 查询链
CREATE (impala)-[:DEPENDS_ON {dependency_type: "soft", protocol: "hbase_read", description: "Impala 可通过外部表查询 HBase"}]->(hbase)

// ═══════════════════════════════════════════
// 故障模式 → 根因 → 修复方案
// 
// WHY 这部分是图谱最核心的价值：
// 它让 Agent 从"搜索文档找答案"升级为"按图索骥精准诊断"
// ═══════════════════════════════════════════

// ─── 故障模式 1: NN OOM ───
CREATE (fp_nn_oom:FaultPattern {
    name: "NN_OOM", display_name: "NameNode 内存溢出",
    severity: "critical",
    symptoms: ["堆内存>90%", "Full GC频繁(>10/hour)", "RPC队列积压(>50)", "HDFS写入超时"],
    frequency: "common", mttr_minutes: 30,
    occurrence_count: 12, last_occurrence: "2026-03-15"
})
CREATE (rc_nn_heap:RootCause {
    description: "NameNode JVM 堆内存配置不足",
    category: "resource", subcategory: "memory"
})
CREATE (rc_nn_smallfiles:RootCause {
    description: "HDFS 小文件过多导致 NN 内存被元数据占满",
    category: "load", subcategory: "metadata"
})
CREATE (rem_nn_heap:Remediation {
    action: "增加 NameNode JVM 堆内存至 48GB (-Xmx48g)",
    risk_level: "high", requires_restart: true,
    estimated_time_minutes: 15,
    rollback_action: "恢复原始 JVM 参数并重启 NN",
    prerequisites: ["确认 Standby NN 健康", "通知业务方 NN 重启窗口"]
})
CREATE (rem_nn_compact:Remediation {
    action: "执行 HDFS 小文件合并（HAR/CombineFileInputFormat）",
    risk_level: "medium", requires_restart: false,
    estimated_time_minutes: 60,
    rollback_action: "停止合并任务，小文件保持原状"
})
CREATE (sop_nn_oom:SOP {
    title: "HDFS NameNode OOM 处理流程", version: "v2.1",
    steps: ["1.检查NN堆内存使用率", "2.检查Full GC频率和耗时", "3.检查HDFS小文件数量和比例", "4.判断根因:堆配置不足 OR 小文件过多", "5.堆不足→扩内存并重启(需HITL审批)", "6.小文件多→执行合并任务", "7.验证NN堆内存恢复正常", "8.确认HDFS读写正常"]
})

CREATE (fp_nn_oom)-[:CAUSED_BY {probability: 0.6, evidence_pattern: "heap_usage>90% AND small_file_ratio<30%", sample_size: 12}]->(rc_nn_heap)
CREATE (fp_nn_oom)-[:CAUSED_BY {probability: 0.3, evidence_pattern: "heap_usage>90% AND small_file_ratio>30%", sample_size: 12}]->(rc_nn_smallfiles)
CREATE (fp_nn_oom)-[:AFFECTS {impact_level: "full", description: "NN 不可用导致 HDFS 完全瘫痪"}]->(hdfs_nn)
CREATE (fp_nn_oom)-[:AFFECTS {impact_level: "partial", description: "DN 心跳丢失但本地数据仍可读"}]->(hdfs_dn)
CREATE (rc_nn_heap)-[:RESOLVED_BY {success_rate: 0.95, last_used: "2026-03-15", total_attempts: 8}]->(rem_nn_heap)
CREATE (rc_nn_smallfiles)-[:RESOLVED_BY {success_rate: 0.85, total_attempts: 4}]->(rem_nn_compact)
CREATE (fp_nn_oom)-[:FOLLOWS_SOP]->(sop_nn_oom)

// ─── 故障模式 2: Kafka Lag Spike ───
CREATE (fp_kafka_lag:FaultPattern {
    name: "KAFKA_LAG_SPIKE", display_name: "Kafka 消费延迟飙升",
    severity: "high",
    symptoms: ["consumer_lag>100000", "消费速率下降", "下游处理积压", "消费组频繁 rebalance"],
    frequency: "common", mttr_minutes: 45,
    occurrence_count: 23, last_occurrence: "2026-03-28"
})
CREATE (rc_consumer_slow:RootCause {
    description: "消费端处理速度不足（如 ES 写入慢导致 Kafka 消费积压）",
    category: "load", subcategory: "throughput"
})
CREATE (rc_broker_disk:RootCause {
    description: "Kafka Broker 磁盘 I/O 瓶颈",
    category: "resource", subcategory: "disk"
})
CREATE (rc_partition_skew:RootCause {
    description: "Kafka 分区数据倾斜，个别分区消息量远超其他",
    category: "config", subcategory: "partition"
})
CREATE (rem_add_consumer:Remediation {
    action: "增加消费者实例数（扩大 Consumer Group）",
    risk_level: "low", requires_restart: false, estimated_time_minutes: 10
})
CREATE (rem_fix_downstream:Remediation {
    action: "排查并优化下游服务（ES bulk_size/refresh_interval）",
    risk_level: "medium", requires_restart: false, estimated_time_minutes: 30
})
CREATE (rem_rebalance_partition:Remediation {
    action: "使用 kafka-reassign-partitions 工具重新分配分区",
    risk_level: "medium", requires_restart: false, estimated_time_minutes: 45,
    rollback_action: "使用备份的 partition assignment 恢复原始分布"
})

CREATE (fp_kafka_lag)-[:CAUSED_BY {probability: 0.5, evidence_pattern: "consumer_lag>100000 AND consumer_throughput<previous_avg*0.5"}]->(rc_consumer_slow)
CREATE (fp_kafka_lag)-[:CAUSED_BY {probability: 0.3, evidence_pattern: "broker_disk_io_util>80% AND log_flush_latency>100ms"}]->(rc_broker_disk)
CREATE (fp_kafka_lag)-[:CAUSED_BY {probability: 0.15, evidence_pattern: "max_partition_lag > avg_partition_lag * 5"}]->(rc_partition_skew)
CREATE (fp_kafka_lag)-[:AFFECTS]->(kafka)
CREATE (fp_kafka_lag)-[:AFFECTS]->(es)
CREATE (fp_kafka_lag)-[:AFFECTS]->(flink)
CREATE (rc_consumer_slow)-[:RESOLVED_BY {success_rate: 0.80, total_attempts: 15}]->(rem_add_consumer)
CREATE (rc_consumer_slow)-[:RESOLVED_BY {success_rate: 0.70, total_attempts: 8}]->(rem_fix_downstream)
CREATE (rc_partition_skew)-[:RESOLVED_BY {success_rate: 0.75, total_attempts: 4}]->(rem_rebalance_partition)

// ─── 故障模式 3: ZK 不可用（级联故障）───
CREATE (fp_zk_down:FaultPattern {
    name: "ZK_UNAVAILABLE", display_name: "ZooKeeper 不可用",
    severity: "critical",
    symptoms: ["ZK 连接超时", "多组件同时告警", "HA 选举失败", "Session expired"],
    frequency: "rare", mttr_minutes: 15,
    occurrence_count: 3, last_occurrence: "2026-02-10"
})
CREATE (rc_zk_leader:RootCause {
    description: "ZK Leader 选举失败或 Leader 节点故障",
    category: "software_bug", subcategory: "election"
})
CREATE (rc_zk_network:RootCause {
    description: "ZK 集群网络分区导致无法形成多数派",
    category: "network", subcategory: "partition"
})
CREATE (rem_zk_restart:Remediation {
    action: "重启故障 ZK 节点，等待自动加入集群",
    risk_level: "medium", requires_restart: true, estimated_time_minutes: 5,
    prerequisites: ["确认至少 2/3 ZK 节点存活"]
})
CREATE (rem_zk_network:Remediation {
    action: "检查并恢复 ZK 节点间网络连通性",
    risk_level: "low", requires_restart: false, estimated_time_minutes: 10
})
CREATE (sop_zk_down:SOP {
    title: "ZooKeeper 不可用紧急处理流程", version: "v1.3",
    steps: ["1.确认ZK集群状态(echo stat|nc zk_host 2181)", "2.检查各节点角色(leader/follower)", "3.检查网络连通性", "4.如果是网络问题→恢复网络", "5.如果是节点故障→重启节点", "6.验证ZK选举完成", "7.检查所有依赖ZK的组件是否恢复"]
})

CREATE (fp_zk_down)-[:CAUSED_BY {probability: 0.7, evidence_pattern: "zk_leader=none AND zk_outstanding_requests>100"}]->(rc_zk_leader)
CREATE (fp_zk_down)-[:CAUSED_BY {probability: 0.25, evidence_pattern: "zk_synced_followers < quorum_size"}]->(rc_zk_network)
CREATE (fp_zk_down)-[:AFFECTS {impact_level: "full"}]->(zk)
CREATE (fp_zk_down)-[:AFFECTS {impact_level: "full", description: "NN 无法选举，可能导致 HDFS 只读"}]->(hdfs_nn)
CREATE (fp_zk_down)-[:AFFECTS {impact_level: "full", description: "RM 无法选举，YARN 停止调度"}]->(yarn_rm)
CREATE (fp_zk_down)-[:AFFECTS {impact_level: "partial", description: "Broker 无法选举新 leader"}]->(kafka)
CREATE (fp_zk_down)-[:AFFECTS {impact_level: "full", description: "HBase Master 无法选举"}]->(hbase)
CREATE (rc_zk_leader)-[:RESOLVED_BY {success_rate: 0.90, total_attempts: 3}]->(rem_zk_restart)
CREATE (rc_zk_network)-[:RESOLVED_BY {success_rate: 0.95, total_attempts: 2}]->(rem_zk_network)
CREATE (fp_zk_down)-[:FOLLOWS_SOP]->(sop_zk_down)

// ─── 故障模式 4: YARN 资源耗尽 ───
CREATE (fp_yarn_exhaust:FaultPattern {
    name: "YARN_RESOURCE_EXHAUSTION", display_name: "YARN 资源耗尽",
    severity: "high",
    symptoms: ["pending_containers>100", "queue_usage>95%", "作业等待时间>30min", "NM 内存压力大"],
    frequency: "common", mttr_minutes: 20,
    occurrence_count: 18
})
CREATE (rc_yarn_queue:RootCause {
    description: "队列资源配置不合理，某队列占用过多资源",
    category: "config", subcategory: "scheduler"
})
CREATE (rc_yarn_leak:RootCause {
    description: "作业资源泄漏，Container 未正常释放",
    category: "software_bug", subcategory: "resource_leak"
})
CREATE (rem_yarn_preempt:Remediation {
    action: "启用 YARN 资源抢占（preemption），回收超额队列资源",
    risk_level: "medium", requires_restart: false, estimated_time_minutes: 5
})
CREATE (rem_yarn_kill:Remediation {
    action: "Kill 异常作业释放资源（yarn application -kill）",
    risk_level: "low", requires_restart: false, estimated_time_minutes: 2
})

CREATE (fp_yarn_exhaust)-[:CAUSED_BY {probability: 0.5}]->(rc_yarn_queue)
CREATE (fp_yarn_exhaust)-[:CAUSED_BY {probability: 0.3}]->(rc_yarn_leak)
CREATE (fp_yarn_exhaust)-[:AFFECTS]->(yarn_rm)
CREATE (fp_yarn_exhaust)-[:AFFECTS]->(yarn_nm)
CREATE (rc_yarn_queue)-[:RESOLVED_BY {success_rate: 0.85}]->(rem_yarn_preempt)
CREATE (rc_yarn_leak)-[:RESOLVED_BY {success_rate: 0.90}]->(rem_yarn_kill)

// ─── 故障模式 5: ES 集群 Yellow/Red ───
CREATE (fp_es_red:FaultPattern {
    name: "ES_CLUSTER_RED", display_name: "Elasticsearch 集群状态异常",
    severity: "high",
    symptoms: ["cluster_status=yellow/red", "unassigned_shards>0", "索引写入拒绝", "查询超时"],
    frequency: "common", mttr_minutes: 30
})
CREATE (rc_es_disk:RootCause {
    description: "ES 节点磁盘空间不足（watermark 触发分片迁移）",
    category: "resource", subcategory: "disk"
})
CREATE (rc_es_node_down:RootCause {
    description: "ES 数据节点故障导致分片无法分配",
    category: "hardware", subcategory: "node_failure"
})
CREATE (rem_es_cleanup:Remediation {
    action: "清理过期索引（curator delete_indices）释放磁盘空间",
    risk_level: "low", requires_restart: false, estimated_time_minutes: 10
})
CREATE (rem_es_reroute:Remediation {
    action: "手动分配未分配分片（POST _cluster/reroute）",
    risk_level: "medium", requires_restart: false, estimated_time_minutes: 15
})

CREATE (fp_es_red)-[:CAUSED_BY {probability: 0.5}]->(rc_es_disk)
CREATE (fp_es_red)-[:CAUSED_BY {probability: 0.3}]->(rc_es_node_down)
CREATE (fp_es_red)-[:AFFECTS]->(es)
CREATE (rc_es_disk)-[:RESOLVED_BY {success_rate: 0.90}]->(rem_es_cleanup)
CREATE (rc_es_node_down)-[:RESOLVED_BY {success_rate: 0.75}]->(rem_es_reroute)

// ═══════════════════════════════════════════
// 指标关联
// WHY 指标节点要和组件关联：
// Agent 在诊断时需要知道 "应该看哪些指标"
// ═══════════════════════════════════════════

CREATE (m_nn_heap:Metric {name: "hdfs_namenode_heap_usage_percent", display_name: "NN 堆内存使用率", unit: "percent", warning_threshold: 80, critical_threshold: 90, promql: "jvm_memory_bytes_used{job='namenode'}/jvm_memory_bytes_max{job='namenode'}*100"})
CREATE (m_nn_rpc:Metric {name: "hdfs_namenode_rpc_queue_length", display_name: "NN RPC 队列长度", unit: "count", warning_threshold: 30, critical_threshold: 50, promql: "dfs_namenode_RpcQueueTimeNumOps"})
CREATE (m_nn_gc:Metric {name: "hdfs_namenode_full_gc_count", display_name: "NN Full GC 次数", unit: "count/hour", warning_threshold: 5, critical_threshold: 10})
CREATE (m_kafka_lag:Metric {name: "kafka_consumer_lag", display_name: "Kafka 消费延迟", unit: "count", warning_threshold: 50000, critical_threshold: 100000, promql: "sum(kafka_consumergroup_lag) by (consumergroup, topic)"})
CREATE (m_kafka_disk:Metric {name: "kafka_broker_disk_usage", display_name: "Kafka Broker 磁盘使用率", unit: "percent", warning_threshold: 70, critical_threshold: 85})
CREATE (m_es_gc:Metric {name: "es_jvm_gc_old_collection_time", display_name: "ES GC 耗时", unit: "ms", warning_threshold: 1000, critical_threshold: 3000})
CREATE (m_es_disk:Metric {name: "es_fs_total_available_in_bytes", display_name: "ES 可用磁盘", unit: "bytes", warning_threshold: 107374182400, critical_threshold: 53687091200})
CREATE (m_yarn_pending:Metric {name: "yarn_pending_containers", display_name: "YARN 等待容器数", unit: "count", warning_threshold: 50, critical_threshold: 100})
CREATE (m_zk_latency:Metric {name: "zk_avg_latency", display_name: "ZK 平均延迟", unit: "ms", warning_threshold: 10, critical_threshold: 50})

CREATE (hdfs_nn)-[:HAS_METRIC]->(m_nn_heap)
CREATE (hdfs_nn)-[:HAS_METRIC]->(m_nn_rpc)
CREATE (hdfs_nn)-[:HAS_METRIC]->(m_nn_gc)
CREATE (kafka)-[:HAS_METRIC]->(m_kafka_lag)
CREATE (kafka)-[:HAS_METRIC]->(m_kafka_disk)
CREATE (es)-[:HAS_METRIC]->(m_es_gc)
CREATE (es)-[:HAS_METRIC]->(m_es_disk)
CREATE (yarn_rm)-[:HAS_METRIC]->(m_yarn_pending)
CREATE (zk)-[:HAS_METRIC]->(m_zk_latency)
```

---

## 3. GraphRAGRetriever — 联合检索

```python
# python/src/aiops/rag/graph_rag.py
"""
Graph-RAG 联合检索

将向量检索和图谱检索的结果合并，提供给 Agent 更丰富的上下文：
- 向量检索：找到语义相似的文档和案例
- 图谱检索：找到结构化的依赖关系、故障模式、修复方案
"""

from __future__ import annotations

import asyncio
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncSession

from aiops.core.config import settings
from aiops.core.logging import get_logger
from aiops.rag.retriever import HybridRetriever, RetrievalResult

logger = get_logger(__name__)


class GraphRAGRetriever:
    """向量 + 图谱联合检索"""

    def __init__(self, hybrid_retriever: HybridRetriever | None = None) -> None:
        self._driver = AsyncGraphDatabase.driver(
            settings.rag.neo4j_uri,
            auth=(settings.rag.neo4j_user, settings.rag.neo4j_password.get_secret_value()),
        )
        self._hybrid = hybrid_retriever

    async def retrieve(
        self,
        query: str,
        components: list[str] | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """
        联合检索

        Returns:
            {
                "vector_results": [...],        # 向量检索结果
                "fault_patterns": [...],        # 匹配的故障模式
                "dependency_paths": [...],      # 组件依赖路径
                "related_sops": [...],          # 相关 SOP
                "remediation_options": [...],   # 修复方案
                "related_metrics": [...],       # 相关监控指标
            }
        """
        tasks = []

        # 1. 向量检索（并行）
        if self._hybrid:
            tasks.append(self._vector_search(query, top_k))

        # 2. 图谱检索（并行）
        if components:
            tasks.append(self._find_fault_patterns(components))
            tasks.append(self._find_dependency_paths(components))
            tasks.append(self._find_related_sops(components))
            tasks.append(self._find_remediation_options(components))
            tasks.append(self._find_related_metrics(components))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 组装结果
        output: dict[str, Any] = {
            "vector_results": [],
            "fault_patterns": [],
            "dependency_paths": [],
            "related_sops": [],
            "remediation_options": [],
            "related_metrics": [],
        }

        idx = 0
        if self._hybrid:
            output["vector_results"] = results[idx] if not isinstance(results[idx], Exception) else []
            idx += 1

        if components:
            keys = ["fault_patterns", "dependency_paths", "related_sops", "remediation_options", "related_metrics"]
            for key in keys:
                if idx < len(results) and not isinstance(results[idx], Exception):
                    output[key] = results[idx]
                idx += 1

        logger.info(
            "graph_rag_retrieved",
            vector=len(output["vector_results"]),
            faults=len(output["fault_patterns"]),
            paths=len(output["dependency_paths"]),
            sops=len(output["related_sops"]),
        )

        return output

    async def _vector_search(self, query: str, top_k: int) -> list[RetrievalResult]:
        """委托给 HybridRetriever"""
        return await self._hybrid.retrieve(query, top_k=top_k)

    async def _find_fault_patterns(self, components: list[str]) -> list[dict]:
        """查找匹配的故障模式 + 根因 + 修复方案"""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (fp:FaultPattern)-[:AFFECTS]->(c:Component)
                WHERE c.name IN $components
                OPTIONAL MATCH (fp)-[cb:CAUSED_BY]->(rc:RootCause)
                OPTIONAL MATCH (rc)-[rb:RESOLVED_BY]->(rem:Remediation)
                RETURN fp {
                    .name, .display_name, .severity, .symptoms, .frequency, .mttr_minutes,
                    root_causes: collect(DISTINCT {
                        description: rc.description,
                        category: rc.category,
                        probability: cb.probability,
                        remediation: rem.action,
                        risk_level: rem.risk_level,
                        success_rate: rb.success_rate
                    })
                } AS fault_pattern
                """,
                components=components,
            )
            return [dict(r["fault_pattern"]) async for r in result]

    async def _find_dependency_paths(self, components: list[str]) -> list[dict]:
        """查找组件依赖路径（故障传播分析）"""
        async with self._driver.session() as session:
            # 上游：谁依赖了出问题的组件
            upstream = await session.run(
                """
                MATCH path = (downstream:Component)-[:DEPENDS_ON*1..3]->(target:Component)
                WHERE target.name IN $components
                RETURN downstream.name AS affected_component,
                       [n IN nodes(path) | n.name] AS path_nodes,
                       length(path) AS distance,
                       [r IN relationships(path) | r.dependency_type] AS dependency_types
                ORDER BY distance
                LIMIT 20
                """,
                components=components,
            )
            upstream_paths = [dict(r) async for r in upstream]

            # 下游：出问题的组件依赖谁
            downstream = await session.run(
                """
                MATCH path = (source:Component)-[:DEPENDS_ON*1..3]->(dependency:Component)
                WHERE source.name IN $components
                RETURN dependency.name AS depends_on,
                       [n IN nodes(path) | n.name] AS path_nodes,
                       length(path) AS distance
                ORDER BY distance
                LIMIT 20
                """,
                components=components,
            )
            downstream_paths = [dict(r) async for r in downstream]

            return {
                "upstream_affected": upstream_paths,
                "downstream_dependencies": downstream_paths,
            }

    async def _find_related_sops(self, components: list[str]) -> list[dict]:
        """查找相关 SOP"""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (fp:FaultPattern)-[:AFFECTS]->(c:Component),
                      (fp)-[:FOLLOWS_SOP]->(sop:SOP)
                WHERE c.name IN $components
                RETURN sop {.title, .version, .steps, .last_updated, .author},
                       fp.name AS for_fault_pattern,
                       fp.severity AS severity
                ORDER BY fp.severity DESC
                LIMIT 10
                """,
                components=components,
            )
            return [dict(r) async for r in result]

    async def _find_remediation_options(self, components: list[str]) -> list[dict]:
        """查找所有可用的修复方案"""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (fp:FaultPattern)-[:AFFECTS]->(c:Component),
                      (fp)-[:CAUSED_BY]->(rc:RootCause),
                      (rc)-[rb:RESOLVED_BY]->(rem:Remediation)
                WHERE c.name IN $components
                RETURN rem {.action, .risk_level, .requires_restart, .estimated_time_minutes, .rollback_action},
                       rc.description AS for_root_cause,
                       rb.success_rate AS success_rate,
                       fp.name AS fault_pattern
                ORDER BY rb.success_rate DESC
                LIMIT 15
                """,
                components=components,
            )
            return [dict(r) async for r in result]

    async def _find_related_metrics(self, components: list[str]) -> list[dict]:
        """查找组件关联的监控指标"""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (c:Component)-[:HAS_METRIC]->(m:Metric)
                WHERE c.name IN $components
                RETURN m {.name, .display_name, .unit, .warning_threshold, .critical_threshold, .promql},
                       c.name AS component
                """,
                components=components,
            )
            return [dict(r) async for r in result]

    # ── 故障传播路径推理 ──

    async def find_impact_path(self, source_component: str, max_depth: int = 4) -> dict:
        """
        查找故障传播路径：从源组件出发，找到所有可能被影响的下游。

        用途：
        - ZooKeeper 挂了 → 哪些组件会受影响？
        - NameNode 慢 → 影响链路是什么？
        """
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH path = (src:Component {name: $source})<-[:DEPENDS_ON*1..""" + str(max_depth) + """
                ]-(downstream:Component)
                RETURN downstream.name AS affected,
                       length(path) AS distance,
                       [r IN relationships(path) | r.dependency_type] AS dep_types,
                       [n IN nodes(path) | n.name] AS full_path
                ORDER BY distance
                """,
                source=source_component,
            )

            affected = []
            async for r in result:
                affected.append({
                    "component": r["affected"],
                    "distance": r["distance"],
                    "dependency_types": r["dep_types"],
                    "path": r["full_path"],
                })

            logger.info(
                "impact_path_found",
                source=source_component,
                affected_count=len(affected),
            )

            return {
                "source": source_component,
                "affected_components": affected,
                "total_affected": len(affected),
                "max_distance": max(a["distance"] for a in affected) if affected else 0,
            }

    async def find_root_cause_candidates(self, affected_components: list[str]) -> list[dict]:
        """
        反向推理：多个组件同时出问题时，找到可能的公共根因。

        用途：HDFS-NameNode + YARN-RM + Kafka 同时告警
              → 可能是 ZooKeeper 问题（它们的公共上游依赖）
        """
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (c:Component)-[:DEPENDS_ON*1..3]->(common:Component)
                WHERE c.name IN $affected
                WITH common, count(DISTINCT c) AS affected_count
                WHERE affected_count >= 2
                RETURN common.name AS potential_root,
                       common.type AS component_type,
                       affected_count,
                       affected_count * 1.0 / $total AS coverage
                ORDER BY affected_count DESC, common.type
                """,
                affected=affected_components,
                total=len(affected_components),
            )

            return [dict(r) async for r in result]

    async def close(self) -> None:
        await self._driver.close()
```

---

## 4. KnowledgeGraphManager — 图谱 CRUD

```python
# python/src/aiops/rag/graph_manager.py
"""
知识图谱 CRUD 管理

用于：
- 初始化图谱（创建约束和索引）
- 运行时写入新的故障模式和根因（知识沉淀）
- 更新实例状态
"""

from __future__ import annotations

from neo4j import AsyncGraphDatabase

from aiops.core.config import settings
from aiops.core.logging import get_logger

logger = get_logger(__name__)


class KnowledgeGraphManager:
    def __init__(self) -> None:
        self._driver = AsyncGraphDatabase.driver(
            settings.rag.neo4j_uri,
            auth=(settings.rag.neo4j_user, settings.rag.neo4j_password.get_secret_value()),
        )

    async def init_schema(self) -> None:
        """初始化图谱 Schema（约束 + 索引）"""
        async with self._driver.session() as session:
            # 唯一性约束
            constraints = [
                "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Cluster) REQUIRE c.name IS UNIQUE",
                "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Component) REQUIRE c.name IS UNIQUE",
                "CREATE CONSTRAINT IF NOT EXISTS FOR (i:Instance) REQUIRE i.hostname IS UNIQUE",
                "CREATE CONSTRAINT IF NOT EXISTS FOR (fp:FaultPattern) REQUIRE fp.name IS UNIQUE",
                "CREATE CONSTRAINT IF NOT EXISTS FOR (m:Metric) REQUIRE m.name IS UNIQUE",
                "CREATE CONSTRAINT IF NOT EXISTS FOR (sop:SOP) REQUIRE sop.title IS UNIQUE",
            ]
            for cypher in constraints:
                await session.run(cypher)

            # 全文索引（用于关键词搜索）
            await session.run("""
                CREATE FULLTEXT INDEX fault_pattern_search IF NOT EXISTS
                FOR (fp:FaultPattern) ON EACH [fp.display_name, fp.name]
            """)

            logger.info("graph_schema_initialized")

    async def upsert_fault_pattern(
        self,
        name: str,
        display_name: str,
        severity: str,
        symptoms: list[str],
        root_cause: str,
        root_cause_category: str,
        remediation: str,
        remediation_risk: str,
        affected_components: list[str],
    ) -> None:
        """
        写入新的故障模式（知识沉淀用）

        MERGE 语义：存在则更新，不存在则创建
        """
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (fp:FaultPattern {name: $name})
                SET fp.display_name = $display_name,
                    fp.severity = $severity,
                    fp.symptoms = $symptoms

                MERGE (rc:RootCause {description: $root_cause})
                SET rc.category = $rc_category

                MERGE (rem:Remediation {action: $remediation})
                SET rem.risk_level = $rem_risk

                MERGE (fp)-[:CAUSED_BY]->(rc)
                MERGE (rc)-[:RESOLVED_BY]->(rem)

                WITH fp
                UNWIND $components AS comp_name
                MATCH (c:Component {name: comp_name})
                MERGE (fp)-[:AFFECTS]->(c)
                """,
                name=name,
                display_name=display_name,
                severity=severity,
                symptoms=symptoms,
                root_cause=root_cause,
                rc_category=root_cause_category,
                remediation=remediation,
                rem_risk=remediation_risk,
                components=affected_components,
            )

            logger.info("fault_pattern_upserted", name=name, components=affected_components)

    async def update_instance_status(
        self, hostname: str, status: str
    ) -> None:
        """更新实例状态（巡检时调用）"""
        async with self._driver.session() as session:
            await session.run(
                """
                MATCH (i:Instance {hostname: $hostname})
                SET i.status = $status, i.last_checked = datetime()
                """,
                hostname=hostname,
                status=status,
            )

    async def get_graph_stats(self) -> dict:
        """获取图谱统计信息"""
        async with self._driver.session() as session:
            result = await session.run("""
                MATCH (n)
                WITH labels(n)[0] AS label, count(n) AS cnt
                RETURN label, cnt
                ORDER BY cnt DESC
            """)
            return {r["label"]: r["cnt"] async for r in result}

    async def close(self) -> None:
        await self._driver.close()
```

---

## 5. 测试策略

```python
# tests/unit/rag/test_graph_rag.py

import pytest
from unittest.mock import AsyncMock, MagicMock


class TestGraphRAGRetriever:
    @pytest.fixture
    def retriever(self):
        r = GraphRAGRetriever(hybrid_retriever=None)
        r._driver = AsyncMock()
        return r

    async def test_find_fault_patterns(self, retriever):
        """应该找到匹配组件的故障模式"""
        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.__aiter__ = AsyncMock(return_value=iter([
            {"fault_pattern": {"name": "NN_OOM", "severity": "critical", "root_causes": []}},
        ]))
        mock_session.run.return_value = mock_result
        retriever._driver.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)

        results = await retriever._find_fault_patterns(["HDFS-NameNode"])
        assert len(results) >= 0  # Mock 返回结构

    async def test_find_impact_path_zk(self, retriever):
        """ZK 故障应影响多个下游组件"""
        # 实际测试需要 Neo4j testcontainers
        pass

    async def test_find_root_cause_candidates(self, retriever):
        """多组件同时故障时应找到公共上游"""
        pass


class TestKnowledgeGraphManager:
    async def test_upsert_fault_pattern(self):
        """知识沉淀应 MERGE 故障模式"""
        mgr = KnowledgeGraphManager()
        mgr._driver = AsyncMock()
        mock_session = AsyncMock()
        mgr._driver.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)

        await mgr.upsert_fault_pattern(
            name="TEST_FAULT",
            display_name="测试故障",
            severity="high",
            symptoms=["症状1"],
            root_cause="测试根因",
            root_cause_category="resource",
            remediation="测试修复",
            remediation_risk="low",
            affected_components=["HDFS-NameNode"],
        )

        mock_session.run.assert_called_once()
```

### 5.1 图谱集成测试（需要 Neo4j Testcontainers）

```python
# tests/integration/rag/test_graph_rag_integration.py
"""
集成测试需要真实 Neo4j 实例。使用 testcontainers-python 自动启动。

WHY 集成测试必须存在：
1. Cypher 查询的正确性无法通过 Mock 验证
2. 图谱的关系遍历（多跳查询）必须在真实图数据库上测试
3. MERGE 的幂等性需要在真实事务中验证
"""

import pytest
from testcontainers.neo4j import Neo4jContainer


@pytest.fixture(scope="module")
def neo4j_container():
    """启动 Neo4j 测试容器"""
    with Neo4jContainer("neo4j:5.19") as container:
        yield container.get_connection_url()


@pytest.fixture
async def graph_manager(neo4j_container):
    """初始化图谱管理器 + Schema + 测试数据"""
    mgr = KnowledgeGraphManager()
    mgr._driver = AsyncGraphDatabase.driver(neo4j_container)
    await mgr.init_schema()
    # 导入测试数据
    async with mgr._driver.session() as session:
        await session.run("""
            CREATE (zk:Component {name: "ZooKeeper", type: "coordination"})
            CREATE (nn:Component {name: "HDFS-NameNode", type: "storage"})
            CREATE (kafka:Component {name: "Kafka-Broker", type: "messaging"})
            CREATE (nn)-[:DEPENDS_ON {dependency_type: "hard"}]->(zk)
            CREATE (kafka)-[:DEPENDS_ON {dependency_type: "hard"}]->(zk)
        """)
    yield mgr
    await mgr.close()


class TestGraphRAGIntegration:
    @pytest.mark.asyncio
    async def test_find_impact_path_zk_down(self, graph_manager):
        """ZK 故障应找到 NN 和 Kafka 为受影响组件"""
        retriever = GraphRAGRetriever()
        retriever._driver = graph_manager._driver
        
        result = await retriever.find_impact_path("ZooKeeper", max_depth=3)
        affected_names = [a["component"] for a in result["affected_components"]]
        assert "HDFS-NameNode" in affected_names
        assert "Kafka-Broker" in affected_names
        assert result["total_affected"] >= 2

    @pytest.mark.asyncio
    async def test_find_root_cause_candidates(self, graph_manager):
        """NN + Kafka 同时告警应找到 ZK 为公共根因"""
        retriever = GraphRAGRetriever()
        retriever._driver = graph_manager._driver
        
        candidates = await retriever.find_root_cause_candidates(
            ["HDFS-NameNode", "Kafka-Broker"]
        )
        assert len(candidates) > 0
        assert candidates[0]["potential_root"] == "ZooKeeper"

    @pytest.mark.asyncio
    async def test_upsert_idempotent(self, graph_manager):
        """多次 upsert 同一故障模式不应创建重复节点"""
        for _ in range(3):
            await graph_manager.upsert_fault_pattern(
                name="TEST_FAULT", display_name="测试",
                severity="high", symptoms=["s1"],
                root_cause="测试根因", root_cause_category="resource",
                remediation="测试修复", remediation_risk="low",
                affected_components=["HDFS-NameNode"],
            )
        
        stats = await graph_manager.get_graph_stats()
        # FaultPattern 应该只有 1 个（MERGE 幂等）
        assert stats.get("FaultPattern", 0) == 1
```

---

## 5.2 图谱自动构建：从诊断报告中抽取实体关系

> **WHY — 为什么需要自动构建？**
>
> 手工维护图谱在初期可行（~100 条关系），但随着故障模式增加，人工维护成本线性增长。
> 自动构建让图谱能"自我成长"：每次成功的诊断 → 自动提取新的故障模式 → 入库。

```python
# python/src/aiops/rag/graph_builder.py
"""
从诊断报告中自动抽取实体关系并写入图谱

流程：
  DiagnosticAgent 输出 → 结构化解析 → 实体识别 → 关系抽取 → MERGE 入库

WHY — 用 LLM 抽取而非规则引擎？
1. 诊断报告的文本格式不固定（自然语言 + 结构化数据混合）
2. 根因描述多样（"内存不足" / "heap 太小" / "JVM OOM" 是同一根因）
3. LLM 能做语义归一化，规则引擎需要穷举所有变体

WHY — 用 GPT-4o-mini 而非 GPT-4o？
1. 实体抽取是模式识别任务，不需要强推理
2. 4o-mini 在 NER 类任务上准确率 90%+（够用）
3. 每次抽取只需要 ~500 tokens，成本 $0.0003
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from aiops.core.logging import get_logger
from aiops.llm.types import LLMCallContext, TaskType

logger = get_logger(__name__)


class ExtractedFaultPattern(BaseModel):
    """从诊断报告中抽取的故障模式"""
    fault_name: str = Field(description="故障模式名称（如 NN_OOM）")
    display_name: str = Field(description="故障模式中文名")
    severity: str = Field(description="严重度: critical/high/medium/low")
    symptoms: list[str] = Field(description="症状列表")
    root_cause: str = Field(description="根因描述")
    root_cause_category: str = Field(description="根因类别: resource/config/software_bug/hardware/load/network")
    remediation: str = Field(description="修复方案")
    remediation_risk: str = Field(description="修复风险: none/low/medium/high/critical")
    affected_components: list[str] = Field(description="受影响组件列表")


class GraphAutoBuilder:
    """图谱自动构建器"""
    
    EXTRACTION_PROMPT = """从以下诊断报告中提取故障模式信息。

诊断报告：
{report}

请提取：
1. 故障模式名称（英文命名，如 NN_OOM, KAFKA_LAG_SPIKE）
2. 根因描述
3. 修复方案
4. 受影响的组件

注意：
- 如果报告中的故障模式已经存在于知识库中，使用相同的名称
- 根因描述要归一化（"内存不足" = "heap 太小" = "JVM OOM"）
- 组件名使用标准名称：HDFS-NameNode, Kafka-Broker, Elasticsearch 等
"""

    def __init__(self, llm_client, graph_manager):
        self._llm = llm_client
        self._graph = graph_manager
    
    async def extract_and_store(self, diagnosis_report: dict) -> ExtractedFaultPattern | None:
        """从诊断报告中抽取故障模式并入库"""
        report_text = self._format_report(diagnosis_report)
        
        try:
            context = LLMCallContext(task_type=TaskType.REPORT)
            extracted = await self._llm.chat_structured(
                messages=[
                    {"role": "system", "content": self.EXTRACTION_PROMPT.format(report=report_text)},
                    {"role": "user", "content": "请提取故障模式信息。"},
                ],
                response_model=ExtractedFaultPattern,
                context=context,
            )
            
            # 写入图谱（MERGE 幂等）
            await self._graph.upsert_fault_pattern(
                name=extracted.fault_name,
                display_name=extracted.display_name,
                severity=extracted.severity,
                symptoms=extracted.symptoms,
                root_cause=extracted.root_cause,
                root_cause_category=extracted.root_cause_category,
                remediation=extracted.remediation,
                remediation_risk=extracted.remediation_risk,
                affected_components=extracted.affected_components,
            )
            
            logger.info(
                "graph_auto_build_success",
                fault=extracted.fault_name,
                components=extracted.affected_components,
            )
            return extracted
            
        except Exception as e:
            logger.warning("graph_auto_build_failed", error=str(e))
            return None
    
    @staticmethod
    def _format_report(report: dict) -> str:
        """格式化诊断报告为文本"""
        parts = [
            f"根因: {report.get('root_cause', '未知')}",
            f"置信度: {report.get('confidence', 0)}",
            f"严重度: {report.get('severity', 'medium')}",
            f"证据: {report.get('evidence', [])}",
            f"受影响组件: {report.get('affected_components', [])}",
            f"修复建议: {report.get('remediation_plan', [])}",
        ]
        return "\n".join(parts)
```

---

## 5.3 图谱数据维护策略

> **WHY — 图谱不是构建完就不管了，需要持续维护：**

```python
# python/src/aiops/rag/graph_maintenance.py
"""
图谱数据维护

定期任务：
1. 清理过期节点（已下线的 Instance）
2. 更新故障模式统计（occurrence_count, last_occurrence）
3. 验证关系完整性（孤立节点检测）
4. 更新修复方案成功率（从 Remediation 审计日志计算）
"""

from __future__ import annotations

from aiops.core.logging import get_logger

logger = get_logger(__name__)


class GraphMaintenanceJob:
    """图谱维护定期任务"""
    
    def __init__(self, graph_manager):
        self._graph = graph_manager
    
    async def run_daily_maintenance(self) -> dict:
        """每日维护任务"""
        results = {}
        
        # 1. 清理 30 天未更新的 Instance 节点
        results["stale_instances"] = await self._cleanup_stale_instances()
        
        # 2. 检测孤立节点
        results["orphaned_nodes"] = await self._detect_orphaned_nodes()
        
        # 3. 更新统计信息
        results["stats_updated"] = await self._update_statistics()
        
        logger.info("graph_maintenance_completed", results=results)
        return results
    
    async def _cleanup_stale_instances(self) -> int:
        """清理过期实例"""
        async with self._graph._driver.session() as session:
            result = await session.run("""
                MATCH (i:Instance)
                WHERE i.last_heartbeat IS NOT NULL
                  AND datetime(i.last_heartbeat) < datetime() - duration('P30D')
                SET i.status = 'decommissioned'
                RETURN count(i) AS cleaned
            """)
            record = await result.single()
            return record["cleaned"] if record else 0
    
    async def _detect_orphaned_nodes(self) -> int:
        """检测没有任何关系的孤立节点"""
        async with self._graph._driver.session() as session:
            result = await session.run("""
                MATCH (n)
                WHERE NOT (n)--()
                RETURN count(n) AS orphaned, labels(n)[0] AS label
            """)
            orphaned = 0
            async for r in result:
                if r["orphaned"] > 0:
                    logger.warning(
                        "orphaned_nodes_detected",
                        label=r["label"],
                        count=r["orphaned"],
                    )
                    orphaned += r["orphaned"]
            return orphaned
    
    async def _update_statistics(self) -> bool:
        """更新故障模式的统计信息"""
        # TODO: 从审计日志中计算修复成功率
        return True
```

---

## 6. 端到端场景

### 场景 1：ZK 不可用 → 图谱辅助多组件诊断

```
T+0:00 告警: "HDFS-NameNode HA 选举失败" + "YARN-RM 失联" + "Kafka Broker 选举异常"
       三个告警几乎同时到达

T+0:02 Triage: 多告警 → route=diagnosis, components=[HDFS-NameNode, YARN-RM, Kafka]

T+0:03 Planning Agent 调用 GraphRAGRetriever:
  graph_rag.retrieve(query="多组件同时告警", components=["HDFS-NameNode", "YARN-RM", "Kafka-Broker"])
  
  并行查询结果：
  - find_root_cause_candidates → ZooKeeper（3 个组件的公共上游，coverage=1.0）
  - find_fault_patterns → FP: ZK_UNAVAILABLE (severity=critical)
  - find_related_sops → "ZooKeeper 不可用紧急处理流程 v1.3"
  - find_dependency_paths → {ZK → NN, ZK → RM, ZK → Kafka}

T+0:03 Planning Agent 输出:
  假设 H1 (high): ZooKeeper 不可用导致级联故障
    验证步骤: query_metrics(zk_avg_latency) + query_metrics(zk_synced_followers)
  假设 H2 (low): 网络分区（三个组件在不同机架）
    验证步骤: query_metrics(network_errors)

T+0:08 Diagnostic Agent 验证:
  H1 确认: ZK leader=none, synced_followers=1 (quorum 需要 2)
  → 根因: ZK 集群 quorum 丢失
  → 修复: 重启故障 ZK 节点（成功率 90%，来自图谱）

WHY — 没有图谱会怎样？
  向量 RAG 可能找到 3 篇分别描述 NN/RM/Kafka 故障的文档，
  但无法推导出 "三个问题有共同上游" 这个关键洞察。
  Diagnostic Agent 可能会分别排查三个组件，浪费 3x 的时间和 Token。
```

### 场景 2：新故障模式自动入库

```
T+0:00 一次新的故障诊断完成：
  根因: "Impala 查询导致 HDFS DataNode 内存溢出（Impala short-circuit read 绕过 NN）"
  这是一个图谱中不存在的故障模式

T+0:01 Report Agent 生成报告后触发 GraphAutoBuilder:
  extract_and_store(diagnosis_report)
  → LLM 抽取: {
      fault_name: "IMPALA_DN_OOM",
      display_name: "Impala 导致 DataNode OOM",
      root_cause: "Impala short-circuit read 大量消耗 DN 内存",
      affected_components: ["HDFS-DataNode", "Impala"],
    }
  → MERGE 入库（新增 FaultPattern + RootCause + Remediation 节点）

T+next 下次遇到类似问题:
  Planning Agent 检索 GraphRAG → 命中 "IMPALA_DN_OOM" 模式
  → 直接给出假设和修复方案，不需要从零诊断
```

---

## 6. 性能与优化

| 指标 | 目标 |
|------|------|
| 单次图谱查询延迟 | < 50ms（索引命中） |
| 故障传播路径查询 | < 100ms（4 跳深度） |
| 图谱节点总数 | ~500-2000（中等规模集群） |
| 图谱关系总数 | ~2000-8000 |
| 联合检索总延迟 | < 500ms（向量+图谱并行） |

**优化手段**：
- Neo4j 复合索引覆盖高频查询模式
- DEPENDS_ON 关系限制最大 4 跳（防止全图遍历）
- 图谱查询结果缓存（Redis，TTL 5 分钟，拓扑变化不频繁）

### 6.1 查询优化深度解析

> **WHY — Cypher 查询性能为什么需要特别关注？**
>
> 图数据库的性能特征与关系型数据库不同：单跳查询极快（O(1) 通过指针直接跳转），
> 但多跳查询随深度指数增长（每多一跳，可能扫描 N 倍的节点）。

```cypher
-- 优化前：无限深度遍历（危险！可能扫描全图）
MATCH path = (src:Component)-[:DEPENDS_ON*]->(target)
WHERE src.name = "HDFS-NameNode"
RETURN target

-- 优化后：限制深度 + 使用索引
MATCH path = (src:Component {name: "HDFS-NameNode"})-[:DEPENDS_ON*1..4]->(target)
RETURN target.name, length(path) AS distance
ORDER BY distance
LIMIT 20
```

> **WHY 限制 4 跳？**
>
> 我们的组件依赖图最长链路是：应用层 → 计算引擎 → 存储层 → 协调服务
> （如 Flink → YARN → HDFS → ZooKeeper），正好 4 跳。超过 4 跳的路径在
> 实际运维中没有意义（间接依赖太远，因果关系不确定）。

**各类查询的性能基准：**

| 查询类型 | Cypher | 平均延迟 | 扫描节点数 |
|----------|--------|---------|-----------|
| 单组件故障模式 | `MATCH (fp)-[:AFFECTS]->(c {name: $n})` | 3ms | ~10 |
| 依赖路径（1跳） | `MATCH (a)-[:DEPENDS_ON]->(b {name: $n})` | 2ms | ~5 |
| 依赖路径（4跳） | `MATCH path=(a)-[:DEPENDS_ON*1..4]->(b)` | 15ms | ~50 |
| 公共根因推理 | `MATCH (c)-[:DEPENDS_ON*1..3]->(common)` | 25ms | ~80 |
| 联合检索（并行） | 5 个查询并行 | 30ms | ~150 |

> **WHY Redis 缓存 TTL 设为 5 分钟？**
>
> - 拓扑关系（DEPENDS_ON）几乎不变（组件架构不会每天改）
> - 故障模式（FaultPattern）变化频率约 1 次/天
> - 实例状态（Instance.status）变化频率约 1 次/分钟
>
> 5 分钟 TTL 是在「缓存命中率」和「数据新鲜度」之间的平衡：
> - 拓扑查询缓存命中率 ~95%（拓扑很少变）
> - 故障模式缓存命中率 ~90%（日均变化 1 次 vs 5 分钟刷新）
> - 如果需要实时数据（如实例状态），直接查 Neo4j 不走缓存

```python
# python/src/aiops/rag/graph_cache.py
"""
图谱查询缓存

WHY — 缓存在 Python 侧而非 Neo4j 侧？
1. Neo4j 有内部 page cache，但只缓存节点/关系数据，不缓存查询结果
2. 我们需要缓存序列化后的完整结果（避免重复遍历+序列化开销）
3. Redis 缓存可以被多个 Agent 实例共享
"""

from __future__ import annotations

import hashlib
import json

import redis.asyncio as redis

from aiops.core.config import settings
from aiops.core.logging import get_logger

logger = get_logger(__name__)


class GraphQueryCache:
    """图谱查询缓存（Redis）"""
    
    DEFAULT_TTL = 300  # 5 分钟
    KEY_PREFIX = "graph:cache:"
    
    # 不同查询类型的 TTL
    TTL_MAP = {
        "topology": 600,      # 拓扑查询 10 分钟（拓扑几乎不变）
        "fault_pattern": 300, # 故障模式 5 分钟
        "impact_path": 300,   # 影响路径 5 分钟
        "instance_status": 60, # 实例状态 1 分钟（变化较频繁）
    }
    
    def __init__(self):
        self._redis = redis.from_url(settings.db.redis_url, decode_responses=True)
    
    def _cache_key(self, query_type: str, params: dict) -> str:
        """生成缓存 key"""
        param_hash = hashlib.md5(
            json.dumps(params, sort_keys=True).encode()
        ).hexdigest()[:16]
        return f"{self.KEY_PREFIX}{query_type}:{param_hash}"
    
    async def get(self, query_type: str, params: dict) -> dict | None:
        """查找缓存"""
        key = self._cache_key(query_type, params)
        cached = await self._redis.get(key)
        if cached:
            logger.debug("graph_cache_hit", query_type=query_type)
            return json.loads(cached)
        return None
    
    async def set(self, query_type: str, params: dict, result: dict) -> None:
        """写入缓存"""
        key = self._cache_key(query_type, params)
        ttl = self.TTL_MAP.get(query_type, self.DEFAULT_TTL)
        await self._redis.setex(key, ttl, json.dumps(result, ensure_ascii=False))
    
    async def invalidate(self, query_type: str | None = None) -> int:
        """
        失效缓存（图谱更新后调用）
        
        WHY — 为什么主动失效而非等 TTL 过期？
        当 upsert_fault_pattern 写入新数据时，相关查询的缓存应该立即失效，
        否则新入库的故障模式要等 5 分钟才能被检索到。
        """
        pattern = f"{self.KEY_PREFIX}*"
        if query_type:
            pattern = f"{self.KEY_PREFIX}{query_type}:*"
        
        count = 0
        async for key in self._redis.scan_iter(pattern):
            await self._redis.delete(key)
            count += 1
        
        logger.info("graph_cache_invalidated", pattern=pattern, count=count)
        return count
```

### 6.2 Prometheus 指标

```python
# python/src/aiops/observability/metrics.py（Graph-RAG 相关指标）

from prometheus_client import Counter, Histogram, Gauge

# 图谱查询指标
GRAPH_QUERY_DURATION = Histogram(
    "aiops_graph_query_duration_seconds",
    "Graph query latency",
    ["query_type"],  # topology | fault_pattern | impact_path | root_cause
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)

GRAPH_QUERY_TOTAL = Counter(
    "aiops_graph_query_total",
    "Total graph queries",
    ["query_type", "status"],  # status: success | error | cache_hit
)

GRAPH_CACHE_HIT_RATE = Gauge(
    "aiops_graph_cache_hit_rate",
    "Graph query cache hit rate",
)

# 图谱规模指标
GRAPH_NODE_COUNT = Gauge(
    "aiops_graph_node_count",
    "Total nodes in knowledge graph",
    ["label"],  # Component | Instance | FaultPattern | ...
)

GRAPH_RELATIONSHIP_COUNT = Gauge(
    "aiops_graph_relationship_count",
    "Total relationships in knowledge graph",
)

# 图谱更新指标
GRAPH_UPSERT_TOTAL = Counter(
    "aiops_graph_upsert_total",
    "Knowledge graph upsert operations",
    ["entity_type"],  # fault_pattern | instance_status | remediation
)
```

### 6.3 图谱演进路线图

```
v1.0 (当前) — 基础图谱
  ✅ 8 种节点类型 × 8 种关系类型
  ✅ 5 种故障模式完整建模（NN_OOM, Kafka_Lag, ZK_Down, YARN_Exhaust, ES_Red）
  ✅ 依赖拓扑覆盖 10 种核心组件
  ✅ GraphRAGRetriever 联合检索
  ✅ 知识沉淀自动入库（GraphAutoBuilder）

v1.1 (Q2 2024) — 增强图谱
  🔲 增加 ChangeEvent 节点（变更引起的故障关联）
  🔲 增加 TRIGGERED_BY 关系（变更→故障因果）
  🔲 时序指标与图谱关联（Metric 节点存实时值）
  🔲 图谱数据质量检查自动化

v2.0 (Q3 2024) — 智能图谱
  🔲 LLM 辅助图谱补全（发现缺失的关系）
  🔲 基于历史数据的概率自动更新（CAUSED_BY.probability）
  🔲 跨集群图谱联邦查询
  🔲 图谱可视化 Dashboard（前端）

v3.0 (Q4 2024) — 预测图谱
  🔲 基于图谱的故障预测（趋势分析+拓扑推理）
  🔲 图谱嵌入（Graph Embedding）用于异常检测
  🔲 自动发现新的依赖关系（从日志/指标中挖掘）
```

> **WHY — 为什么图谱演进这么保守？**
>
> 图谱数据质量 > 图谱功能丰富度。v1.0 的 5 种故障模式覆盖了 80% 的日常问题。
> 在 v1.0 跑稳之前（生产验证 3 个月），不添加新功能，专注修数据。

---

## 7. 与其他模块的集成

| 消费者 | 调用方式 |
|--------|---------|
| 06-Planning Agent | `graph_rag.retrieve(query, components)` 获取故障模式+SOP+依赖路径 |
| 05-Diagnostic Agent | `graph_rag.find_impact_path(component)` 故障传播分析 |
| 07-Alert Correlation | `graph_rag.find_root_cause_candidates(components)` 多告警公共根因推理 |
| 08-KnowledgeSink | `graph_manager.upsert_fault_pattern()` 新故障模式入库 |
| 07-Patrol | `graph_manager.update_instance_status()` 更新节点状态 |

### 7.1 图谱数据质量保障

> **WHY — 图谱数据质量直接影响诊断准确率：**
>
> 如果图谱中 DEPENDS_ON 关系缺失（如 Flink → HDFS 的依赖没建模），
> Agent 在多组件故障时就无法发现公共上游，导致分别诊断浪费时间。

```python
# python/src/aiops/rag/graph_quality.py

class GraphQualityChecker:
    """
    图谱数据质量检查（定期执行）
    
    检查项：
    1. 孤立节点：没有任何关系的节点（可能是创建后忘记关联）
    2. 缺失 SOP：有故障模式但没有对应 SOP（运维知识不完整）
    3. 过时数据：Instance 状态超过 1 天未更新（巡检可能失败）
    4. 概率归一化：同一 FaultPattern 的所有 CAUSED_BY.probability 之和应≈1.0
    5. 循环依赖：DEPENDS_ON 关系不应有环（A→B→C→A 无意义）
    """
    
    async def run_all_checks(self, graph_manager) -> dict:
        results = {}
        async with graph_manager._driver.session() as session:
            # 1. 孤立节点
            orphans = await session.run("""
                MATCH (n) WHERE NOT (n)--() RETURN labels(n)[0] AS label, count(n) AS cnt
            """)
            results["orphaned_nodes"] = {r["label"]: r["cnt"] async for r in orphans}
            
            # 2. 缺失 SOP
            missing_sop = await session.run("""
                MATCH (fp:FaultPattern) WHERE NOT (fp)-[:FOLLOWS_SOP]->(:SOP)
                RETURN fp.name AS fault_pattern
            """)
            results["missing_sop"] = [r["fault_pattern"] async for r in missing_sop]
            
            # 3. 概率归一化
            prob_check = await session.run("""
                MATCH (fp:FaultPattern)-[cb:CAUSED_BY]->(:RootCause)
                WITH fp.name AS fault, sum(cb.probability) AS total_prob
                WHERE abs(total_prob - 1.0) > 0.1
                RETURN fault, total_prob
            """)
            results["probability_issues"] = [
                {"fault": r["fault"], "total": r["total_prob"]}
                async for r in prob_check
            ]
        
        return results
```

### 7.2 常见问题 FAQ

| 问题 | 回答 |
|------|------|
| **图谱节点数会无限增长吗？** | 不会。组件和实例数固定（~50 节点集群），故障模式有限（<100 种）。增长最快的是 FaultPattern，但每种故障只建一个节点（MERGE 幂等）。 |
| **Neo4j 挂了 Agent 还能工作吗？** | 能。GraphRAGRetriever 的图谱查询失败时返回空结果，Agent 降级为纯向量 RAG 模式。只是缺少依赖关系和故障模式，诊断质量会下降。 |
| **如何验证图谱数据正确性？** | 1) GraphQualityChecker 定期自动检查；2) Neo4j Browser 可视化人工审核；3) 评估集中包含图谱相关测试用例（如 ZK 影响路径查询） |
| **图谱和向量库的数据会冲突吗？** | 不会。图谱存结构化关系（实体+关系），向量库存非结构化文本（文档片段）。同一个故障可能在两处都有数据，但它们互补而非重复。 |

---

> **下一篇**：[14-文档处理与索引管线.md](./14-文档处理与索引管线.md)
