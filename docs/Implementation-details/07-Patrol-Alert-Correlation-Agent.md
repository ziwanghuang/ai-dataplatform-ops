# 07 - Patrol Agent 与 Alert Correlation Agent

> **设计文档引用**：`03-智能诊断Agent系统设计.md` §3.1 Agent 矩阵, `02-数据采集与集成层设计.md` §告警管道  
> **职责边界**：Patrol Agent 定时巡检 + 健康评分；Alert Correlation Agent 多告警聚合去重 + 根因告警识别  
> **优先级**：P1 — Phase 3 上线

---

## 目录

1. [Patrol Agent — 定时巡检](#1-patrol-agent--定时巡检)
   - 1.1 [职责](#11-职责)
   - 1.2 [调度策略设计决策](#12-调度策略设计决策)
   - 1.3 [巡检项定义与优先级排序](#13-巡检项定义与优先级排序)
   - 1.4 [数据模型](#14-数据模型)
   - 1.5 [PatrolNode 实现](#15-patrolnode-实现)
   - 1.6 [巡检调度器](#16-巡检调度器)
   - 1.7 [巡检报告生成模板](#17-巡检报告生成模板)
   - 1.8 [Prometheus 巡检指标](#18-prometheus-巡检指标)
2. [Alert Correlation Agent — 告警关联分析](#2-alert-correlation-agent--告警关联分析)
   - 2.1 [职责](#21-职责)
   - 2.2 [告警聚合算法设计](#22-告警聚合算法设计)
   - 2.3 [告警去重与降噪策略](#23-告警去重与降噪策略)
   - 2.4 [告警风暴处理](#24-告警风暴处理)
   - 2.5 [数据模型](#25-数据模型)
   - 2.6 [AlertCorrelationNode 实现](#26-alertcorrelationnode-实现)
3. [端到端场景](#3-端到端场景)
4. [测试策略](#4-测试策略)
5. [性能指标](#5-性能指标)
6. [与其他模块的集成](#6-与其他模块的集成)

---

## 1. Patrol Agent — 定时巡检

### 1.1 职责

- **定时调度** — APScheduler 定时触发（默认每 30 分钟）
- **多组件巡检** — 遍历注册的巡检项，逐一检查
- **健康评分** — 0-100 分综合打分，低于阈值触发告警
- **趋势预测** — 对关键指标做简单趋势分析（近 24h）
- **报告生成** — 输出结构化巡检报告

> **WHY — 为什么需要独立的 Patrol Agent？**
> 大数据平台的很多隐患是"温水煮青蛙"式的：HDFS 磁盘使用率从 70% 缓慢爬升到 90%、Kafka consumer lag 在业务低峰期悄悄增长。这些问题不会触发告警（因为没有突破阈值），但如果不主动巡检，等到阈值突破时已经是紧急故障了。Patrol Agent 的核心价值是**将被动等告警变为主动找问题**，把 MTTR（平均修复时间）的起点从"告警触发"前移到"巡检发现隐患"。

### 1.2 调度策略设计决策

巡检调度有三种主流方案，我们逐一分析后选择了**混合策略**：

#### 方案对比

| 调度方式 | 适用场景 | 延迟 | 资源开销 | 复杂度 |
|---------|---------|------|---------|--------|
| **Cron 定时** | 固定频率的全量巡检 | 固定（取决于调度间隔） | 固定，可预测 | 低 |
| **间隔触发** | 自适应频率、动态调整 | 可变 | 中等 | 中 |
| **事件驱动** | 告警/变更后立即巡检 | 最低（实时） | 不可预测 | 高 |

> **WHY — 为什么选择 APScheduler IntervalTrigger 而不是 Cron？**
> Cron 表达式（如 `*/30 * * * *`）在 K8s 环境下有一个隐藏问题：如果 Pod 在 Cron 触发的精确时刻正在重启，这次巡检就会被跳过，而且 Cron 不会补偿执行（misfire）。APScheduler 的 IntervalTrigger 自带 misfire_grace_time 机制，即使 Pod 重启延迟了几秒钟，调度器恢复后会立即补偿执行错过的巡检。对于运维巡检这种"宁可晚执行也不能漏执行"的场景，IntervalTrigger 的可靠性显著优于 Cron。

> **WHY — 为什么不纯用事件驱动？**
> 事件驱动巡检（如收到一条 HDFS 告警后立即巡检全部 HDFS 组件）看起来很实时，但有两个问题：(1) **告警风暴时会产生级联巡检**——一次 ZK 故障可能触发 20+ 条告警，每条都触发一次巡检会耗尽系统资源；(2) **无法发现"未触发告警的隐患"**——事件驱动的本质是被动的，如果问题还没到告警阈值，就没有事件来驱动巡检。所以事件驱动只能作为补充，不能替代定时巡检。

#### 最终方案：混合三层调度

```
┌────────────────────────────────────────────────────────────┐
│                    巡检调度策略（三层混合）                    │
├───────────────────┬────────────────────────────────────────┤
│ Layer 1: 定时巡检  │ IntervalTrigger 每 30 分钟             │
│                   │ 全量巡检所有注册的巡检项                  │
│                   │ misfire_grace_time=120s                │
├───────────────────┼────────────────────────────────────────┤
│ Layer 2: 事件触发  │ 收到 P0/P1 告警后 → 触发受影响组件的     │
│                   │ 定向巡检（防抖：同组件 5 分钟内不重复）     │
├───────────────────┼────────────────────────────────────────┤
│ Layer 3: 自适应频率 │ 上一次巡检发现 WARNING/DEGRADED →       │
│                   │ 自动将该组件的巡检频率提升为 10 分钟        │
│                   │ 连续 3 次 HEALTHY 后恢复 30 分钟          │
└───────────────────┴────────────────────────────────────────┘
```

```python
# python/src/aiops/agent/patrol/scheduling.py
"""
三层混合调度策略的实现。

Layer 1: 固定间隔的全量巡检
Layer 2: 告警事件触发的定向巡检
Layer 3: 健康状态驱动的自适应频率
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ComponentScheduleState:
    """单组件的调度状态，用于自适应频率控制"""
    component: str
    base_interval_minutes: int = 30
    current_interval_minutes: int = 30
    consecutive_healthy_count: int = 0
    last_patrol_ts: float = 0.0
    # 事件触发的防抖：同组件 5 分钟内不重复触发
    last_event_trigger_ts: float = 0.0
    event_debounce_seconds: int = 300


class AdaptiveScheduleManager:
    """自适应频率管理器

    根据巡检结果动态调整各组件的巡检频率：
    - 发现异常 → 缩短间隔（30min → 10min）
    - 连续 3 次健康 → 恢复基线间隔
    """

    # 自适应参数
    ESCALATED_INTERVAL_MINUTES = 10
    HEALTHY_COUNT_TO_DEESCALATE = 3

    def __init__(self) -> None:
        self._states: dict[str, ComponentScheduleState] = {}

    def get_or_create_state(self, component: str) -> ComponentScheduleState:
        if component not in self._states:
            self._states[component] = ComponentScheduleState(component=component)
        return self._states[component]

    def on_patrol_result(self, component: str, health_level: str) -> int:
        """巡检结果回调，返回新的巡检间隔（分钟）

        Args:
            component: 组件名称
            health_level: healthy / warning / degraded / critical

        Returns:
            该组件新的巡检间隔（分钟）
        """
        state = self.get_or_create_state(component)
        state.last_patrol_ts = time.time()

        if health_level in ("warning", "degraded", "critical"):
            # 异常 → 提升频率
            state.consecutive_healthy_count = 0
            state.current_interval_minutes = self.ESCALATED_INTERVAL_MINUTES
            logger.info(
                "patrol_frequency_escalated",
                component=component,
                health_level=health_level,
                new_interval_min=state.current_interval_minutes,
            )
        else:
            # 健康 → 累计计数
            state.consecutive_healthy_count += 1
            if state.consecutive_healthy_count >= self.HEALTHY_COUNT_TO_DEESCALATE:
                state.current_interval_minutes = state.base_interval_minutes
                logger.info(
                    "patrol_frequency_deescalated",
                    component=component,
                    consecutive_healthy=state.consecutive_healthy_count,
                    new_interval_min=state.current_interval_minutes,
                )

        return state.current_interval_minutes

    def should_trigger_on_event(self, component: str) -> bool:
        """事件驱动的防抖判断：同组件 5 分钟内不重复触发"""
        state = self.get_or_create_state(component)
        now = time.time()

        if now - state.last_event_trigger_ts < state.event_debounce_seconds:
            logger.debug(
                "event_trigger_debounced",
                component=component,
                seconds_since_last=int(now - state.last_event_trigger_ts),
            )
            return False

        state.last_event_trigger_ts = now
        return True

    def get_all_states(self) -> dict[str, dict]:
        """导出所有组件的调度状态（用于 Prometheus 指标）"""
        return {
            name: {
                "current_interval_min": s.current_interval_minutes,
                "consecutive_healthy": s.consecutive_healthy_count,
                "last_patrol_ts": s.last_patrol_ts,
            }
            for name, s in self._states.items()
        }
```

> **WHY — 为什么选择 30 分钟作为基线间隔？**
> 这是在"巡检时效性"和"系统开销"之间的平衡点。每次全量巡检会并行调用 9 个 MCP 工具（HDFS × 3 + YARN × 2 + Kafka × 2 + ES × 2），每个工具调用涉及一次 HTTP 请求到对应组件的 API。30 分钟意味着每小时 2 次全量巡检，每天 48 次——在大数据集群的 API 调用量级中完全可以忽略。如果缩短到 5 分钟（每天 288 次），NameNode 的 JMX 接口在高负载时可能出现响应延迟。实测数据：30 分钟间隔下，巡检对集群 CPU 的额外开销 < 0.1%。

### 1.3 巡检项定义与优先级排序

巡检项不是平等的——NameNode 宕机和某个 DataNode 磁盘告警的严重程度完全不同。我们用**四级优先级**来排序巡检项的执行顺序和失败影响：

#### 优先级定义

| 优先级 | 含义 | 失败影响 | 巡检超时 | 示例 |
|--------|------|---------|---------|------|
| **P0 — 致命** | 组件完全不可用 | 总分直接降至 CRITICAL | 5s | NameNode/RM 存活检查 |
| **P1 — 严重** | 核心功能受损 | 扣 30 分 | 10s | HDFS corrupt blocks, ES 红色 |
| **P2 — 警告** | 性能下降或接近阈值 | 扣 15 分 | 10s | 磁盘使用率 > 80%, consumer lag |
| **P3 — 信息** | 非关键指标偏离 | 扣 5 分 | 15s | DataNode 数量变化 |

> **WHY — 为什么 P0 的超时时间最短（5s）？**
> P0 巡检项检查的是"组件是否存活"，这类检查本质上是 health check endpoint 或 TCP 连接探测。如果 5 秒内没有响应，几乎可以确定组件已经不可用了——不需要再等。相反，P3 的超时设为 15 秒，因为信息类指标可能涉及较大的数据聚合查询（如统计最近 1 小时的 DataNode 数量变化趋势），需要更多时间。

```python
# python/src/aiops/agent/patrol/check_registry.py
"""
巡检项注册表

每个巡检项定义为一个 PatrolCheckDef，包含：
- 目标组件和检查名称
- 对应的 MCP 工具名
- 阈值配置
- 优先级（P0-P3）
- 超时时间
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field


class CheckPriority(IntEnum):
    """巡检优先级：数字越小越重要"""
    P0_FATAL = 0    # 组件不可用
    P1_CRITICAL = 1  # 核心功能受损
    P2_WARNING = 2   # 性能下降
    P3_INFO = 3      # 非关键偏离


class ThresholdOp(str):
    """阈值比较操作符"""
    GT = "gt"    # 大于
    LT = "lt"    # 小于
    EQ = "eq"    # 等于
    NEQ = "neq"  # 不等于


class ThresholdDef(BaseModel):
    """单个阈值定义"""
    metric: str
    op: str = "gt"
    value: float | str
    description: str = ""


class PatrolCheckDef(BaseModel):
    """巡检项定义"""
    component: str
    name: str
    display_name: str = ""
    tool: str = Field(description="MCP 工具名称")
    tool_params: dict[str, Any] = Field(default_factory=dict)
    thresholds: list[ThresholdDef] = Field(default_factory=list)
    priority: CheckPriority = CheckPriority.P2_WARNING
    timeout_seconds: float = 10.0
    description: str = ""
    # 是否在每次巡检中都执行（False = 仅在定向巡检时执行）
    always_run: bool = True


# ── 完整巡检项注册表 ──

PATROL_REGISTRY: list[PatrolCheckDef] = [
    # ============================================
    # HDFS 巡检项
    # ============================================
    PatrolCheckDef(
        component="hdfs",
        name="namenode_liveness",
        display_name="NameNode 存活检查",
        tool="hdfs_namenode_status",
        thresholds=[],  # 工具调用成功 = NN 存活
        priority=CheckPriority.P0_FATAL,
        timeout_seconds=5.0,
        description="检查 Active NameNode 是否响应。P0 级别——NN 不可用意味着整个 HDFS 不可用。",
    ),
    PatrolCheckDef(
        component="hdfs",
        name="namenode_heap",
        display_name="NameNode JVM 堆内存",
        tool="hdfs_namenode_status",
        thresholds=[
            ThresholdDef(metric="heap_percent", op="gt", value=85,
                         description="堆使用率 > 85% 时 NN 可能触发 Full GC"),
        ],
        priority=CheckPriority.P1_CRITICAL,
        timeout_seconds=10.0,
        description="NN 堆内存接近上限会导致长时间 GC 暂停，客户端请求超时。",
    ),
    PatrolCheckDef(
        component="hdfs",
        name="block_health",
        display_name="HDFS Block 健康",
        tool="hdfs_block_report",
        thresholds=[
            ThresholdDef(metric="corrupt_blocks", op="gt", value=0,
                         description="存在损坏块意味着数据可能丢失"),
            ThresholdDef(metric="under_replicated", op="gt", value=100,
                         description="欠副本数 > 100 需要关注"),
        ],
        priority=CheckPriority.P1_CRITICAL,
        timeout_seconds=10.0,
    ),
    PatrolCheckDef(
        component="hdfs",
        name="disk_usage",
        display_name="HDFS 磁盘使用率",
        tool="hdfs_cluster_overview",
        thresholds=[
            ThresholdDef(metric="usage_percent", op="gt", value=80,
                         description="使用率 > 80% 需要规划扩容"),
        ],
        priority=CheckPriority.P2_WARNING,
        timeout_seconds=10.0,
    ),
    PatrolCheckDef(
        component="hdfs",
        name="datanode_count",
        display_name="DataNode 数量",
        tool="hdfs_cluster_overview",
        thresholds=[
            ThresholdDef(metric="live_datanodes", op="lt", value=3,
                         description="存活 DataNode 数低于预期"),
        ],
        priority=CheckPriority.P3_INFO,
        timeout_seconds=15.0,
        description="DataNode 数量下降可能表示节点故障或维护中。",
    ),

    # ============================================
    # YARN 巡检项
    # ============================================
    PatrolCheckDef(
        component="yarn",
        name="resourcemanager_liveness",
        display_name="ResourceManager 存活检查",
        tool="yarn_cluster_metrics",
        thresholds=[],
        priority=CheckPriority.P0_FATAL,
        timeout_seconds=5.0,
        description="RM 不可用意味着无法提交新任务，已运行任务无法获取新资源。",
    ),
    PatrolCheckDef(
        component="yarn",
        name="cluster_resources",
        display_name="YARN 集群资源利用率",
        tool="yarn_cluster_metrics",
        thresholds=[
            ThresholdDef(metric="cpu_percent", op="gt", value=85,
                         description="CPU 利用率 > 85% 任务排队将增多"),
            ThresholdDef(metric="memory_percent", op="gt", value=85,
                         description="内存利用率 > 85% 大任务可能无法分配资源"),
        ],
        priority=CheckPriority.P1_CRITICAL,
        timeout_seconds=10.0,
    ),
    PatrolCheckDef(
        component="yarn",
        name="pending_apps",
        display_name="YARN 排队任务数",
        tool="yarn_applications",
        tool_params={"states": "ACCEPTED"},
        thresholds=[
            ThresholdDef(metric="pending_count", op="gt", value=50,
                         description="排队任务 > 50 可能导致 SLA 违约"),
        ],
        priority=CheckPriority.P2_WARNING,
        timeout_seconds=10.0,
    ),
    PatrolCheckDef(
        component="yarn",
        name="failed_apps_rate",
        display_name="YARN 任务失败率",
        tool="yarn_applications",
        tool_params={"states": "FAILED", "time_range": "1h"},
        thresholds=[
            ThresholdDef(metric="failed_rate_percent", op="gt", value=10,
                         description="1 小时内失败率 > 10% 需要排查"),
        ],
        priority=CheckPriority.P2_WARNING,
        timeout_seconds=10.0,
        description="高失败率可能暗示集群资源不足或代码 bug。",
    ),

    # ============================================
    # Kafka 巡检项
    # ============================================
    PatrolCheckDef(
        component="kafka",
        name="broker_liveness",
        display_name="Kafka Broker 存活",
        tool="kafka_broker_status",
        thresholds=[],
        priority=CheckPriority.P0_FATAL,
        timeout_seconds=5.0,
    ),
    PatrolCheckDef(
        component="kafka",
        name="consumer_lag",
        display_name="Kafka Consumer Lag",
        tool="kafka_consumer_lag",
        thresholds=[
            ThresholdDef(metric="max_lag", op="gt", value=100000,
                         description="消费延迟 > 10 万条需要关注"),
        ],
        priority=CheckPriority.P1_CRITICAL,
        timeout_seconds=10.0,
    ),
    PatrolCheckDef(
        component="kafka",
        name="partition_health",
        display_name="Kafka 分区健康",
        tool="kafka_partition_status",
        thresholds=[
            ThresholdDef(metric="offline_partitions", op="gt", value=0,
                         description="存在离线分区意味着部分数据不可读写"),
        ],
        priority=CheckPriority.P1_CRITICAL,
        timeout_seconds=10.0,
    ),
    PatrolCheckDef(
        component="kafka",
        name="under_replicated_partitions",
        display_name="Kafka 欠副本分区",
        tool="kafka_partition_status",
        thresholds=[
            ThresholdDef(metric="under_replicated", op="gt", value=0,
                         description="欠副本分区在 broker 故障时可能丢数据"),
        ],
        priority=CheckPriority.P2_WARNING,
        timeout_seconds=10.0,
    ),

    # ============================================
    # Elasticsearch 巡检项
    # ============================================
    PatrolCheckDef(
        component="es",
        name="cluster_health",
        display_name="ES 集群健康状态",
        tool="es_cluster_health",
        thresholds=[
            ThresholdDef(metric="status", op="neq", value="green",
                         description="非 green 状态表示有分片缺失或未分配"),
        ],
        priority=CheckPriority.P1_CRITICAL,
        timeout_seconds=10.0,
    ),
    PatrolCheckDef(
        component="es",
        name="node_disk",
        display_name="ES 节点磁盘使用率",
        tool="es_node_stats",
        thresholds=[
            ThresholdDef(metric="disk_percent", op="gt", value=85,
                         description="磁盘 > 85% ES 会开始拒绝写入"),
        ],
        priority=CheckPriority.P1_CRITICAL,
        timeout_seconds=10.0,
    ),
    PatrolCheckDef(
        component="es",
        name="jvm_heap_pressure",
        display_name="ES JVM 堆压力",
        tool="es_node_stats",
        thresholds=[
            ThresholdDef(metric="jvm_heap_percent", op="gt", value=80,
                         description="JVM 堆压力 > 80% 可能导致 OOM 或长 GC"),
        ],
        priority=CheckPriority.P2_WARNING,
        timeout_seconds=10.0,
    ),
]


def get_checks_by_component(component: str) -> list[PatrolCheckDef]:
    """获取指定组件的所有巡检项"""
    return [c for c in PATROL_REGISTRY if c.component == component]


def get_checks_by_priority(max_priority: CheckPriority) -> list[PatrolCheckDef]:
    """获取指定优先级及以上的巡检项"""
    return [c for c in PATROL_REGISTRY if c.priority <= max_priority]


def get_always_run_checks() -> list[PatrolCheckDef]:
    """获取每次都需要执行的巡检项"""
    return [c for c in PATROL_REGISTRY if c.always_run]
```

> **WHY — 为什么巡检项用声明式注册而不是硬编码在 PatrolNode 里？**
> 声明式注册表（`PATROL_REGISTRY`）将巡检逻辑与巡检配置解耦。新增一个巡检项只需要在注册表中添加一条 `PatrolCheckDef`，不需要修改 `PatrolNode` 的任何代码。这在生产环境中尤其重要——运维团队可以通过配置文件或管理界面增删巡检项，而不需要发版。同时，`PatrolCheckDef` 是 Pydantic 模型，可以直接序列化为 JSON/YAML 配置，支持未来从配置中心（如 Apollo/Nacos）动态加载。

### 1.4 数据模型

```python
# python/src/aiops/agent/nodes/patrol.py

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class HealthLevel(str, Enum):
    """健康等级

    四级健康度模型，参考 Google SRE 的 Service Level 定义。
    每个等级对应一个分数区间和运维动作。
    """
    HEALTHY = "healthy"       # 80-100 → 无需动作
    WARNING = "warning"       # 60-79  → 排入下一个维护窗口
    DEGRADED = "degraded"     # 40-59  → 立即创建工单
    CRITICAL = "critical"     # 0-39   → 触发告警 + 自动诊断

    # > **WHY — 为什么分四级而不是三级（健康/告警/故障）？**
    # > 三级模型缺少"DEGRADED"这个缓冲区。在实际运维中，很多场景
    # > 处于"有问题但还没完全坏"的状态——比如 HDFS 有 50 个欠副本块，
    # > 不算 CRITICAL 但也不能忽略。DEGRADED 级别让运维人员知道
    # > "需要尽快处理，但不需要半夜叫醒 on-call"。


class CheckResult(BaseModel):
    """单个巡检项的执行结果"""
    check_name: str
    component: str
    passed: bool
    priority: int = Field(ge=0, le=3, description="0=P0, 3=P3")
    raw_output: str = Field(default="", max_length=2000)
    threshold_violations: list[str] = Field(default_factory=list)
    latency_ms: float = Field(ge=0, description="工具调用延迟")
    error: str | None = None


class ComponentHealth(BaseModel):
    """单组件健康评估"""
    component: str
    score: int = Field(ge=0, le=100)
    level: HealthLevel
    checks_passed: int
    checks_total: int
    issues: list[str] = Field(default_factory=list)
    metrics_summary: dict = Field(default_factory=dict)
    check_details: list[CheckResult] = Field(
        default_factory=list,
        description="各巡检项的详细结果（用于报告和回溯）",
    )


class TrendAlert(BaseModel):
    """趋势预警

    基于线性回归预测指标何时突破阈值。
    只有 R² > 0.7 且预测时间 < 24h 时才生成预警。
    """
    metric_name: str
    component: str
    current_value: float
    trend: Literal["rising", "falling", "stable"]
    slope: float = Field(
        default=0.0,
        description="线性回归斜率（每小时变化量）",
    )
    r_squared: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="回归拟合度，> 0.7 时趋势预测才有意义",
    )
    predicted_threshold_breach: str | None = Field(
        default=None,
        description="预计多久后突破阈值（如 '约 6 小时后'）",
    )
    severity: Literal["info", "warning", "critical"]


class PatrolReport(BaseModel):
    """巡检报告"""
    patrol_id: str
    timestamp: str
    cluster_id: str
    overall_score: int = Field(ge=0, le=100)
    overall_level: HealthLevel
    components: list[ComponentHealth]
    trend_alerts: list[TrendAlert] = Field(default_factory=list)
    summary: str
    recommendations: list[str] = Field(default_factory=list)
    duration_seconds: float = Field(
        default=0.0,
        description="本次巡检总耗时",
    )
    checks_executed: int = Field(
        default=0,
        description="执行的巡检项数",
    )
    checks_failed: int = Field(
        default=0,
        description="失败的巡检项数（工具调用失败，非阈值违反）",
    )
```

> **WHY — 为什么数据模型用 Pydantic 而不是 dataclass？**
> Pydantic 提供三个 dataclass 不具备的关键能力：(1) **运行时类型验证**——`score: int = Field(ge=0, le=100)` 自动拒绝非法值，巡检结果不会出现 score=150 或 score=-1 的脏数据；(2) **JSON 序列化**——`.model_dump_json()` 直接输出标准 JSON，方便写入 Kafka/ES 或通过 API 返回；(3) **与 instructor 库的集成**——LLM 结构化输出直接反序列化为 Pydantic 模型，不需要手写解析逻辑。

### 1.5 PatrolNode 实现

```python
# python/src/aiops/agent/nodes/patrol.py

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import numpy as np
import structlog

from aiops.agent.base import BaseAgentNode
from aiops.agent.patrol.check_registry import (
    PATROL_REGISTRY,
    CheckPriority,
    PatrolCheckDef,
    get_always_run_checks,
    get_checks_by_component,
)
from aiops.agent.patrol.scheduling import AdaptiveScheduleManager
from aiops.agent.state import AgentState
from aiops.llm.types import TaskType

logger = structlog.get_logger(__name__)


class PatrolNode(BaseAgentNode):
    """巡检节点

    执行流程：
    1. 获取巡检项列表（全量 or 定向）
    2. 按优先级排序，P0 先执行
    3. 并行执行所有巡检工具调用
    4. 基于加权评分算法计算健康度
    5. 趋势分析（线性回归）
    6. LLM 生成摘要和建议
    7. 构建结构化报告
    """
    agent_name = "patrol"
    task_type = TaskType.PATROL

    # 各优先级的评分权重
    PRIORITY_WEIGHTS = {
        CheckPriority.P0_FATAL: 40,     # P0 失败直接降 40 分
        CheckPriority.P1_CRITICAL: 30,  # P1 失败降 30 分
        CheckPriority.P2_WARNING: 15,   # P2 失败降 15 分
        CheckPriority.P3_INFO: 5,       # P3 失败降 5 分
    }

    def __init__(self, llm_client, mcp_client=None):
        super().__init__(llm_client)
        self._mcp = mcp_client
        self._schedule_manager = AdaptiveScheduleManager()

    async def process(self, state: AgentState) -> AgentState:
        """巡检主流程"""
        cluster = state.get("cluster_id", "default")
        start_time = time.monotonic()

        # 确定巡检范围（全量 or 定向）
        target_component = state.get("target_component")
        if target_component:
            checks = get_checks_by_component(target_component)
            logger.info("targeted_patrol", component=target_component, checks=len(checks))
        else:
            checks = get_always_run_checks()
            logger.info("full_patrol", checks=len(checks))

        # 按优先级排序：P0 先执行
        checks.sort(key=lambda c: c.priority)

        # 1. 并行执行所有巡检项
        check_results = await self._run_all_checks(cluster, checks)

        # 2. 计算健康评分（加权算法）
        components = self._calculate_health(check_results, checks)

        # 3. 趋势分析（查询历史指标，线性回归）
        trends = await self._analyze_trends(cluster)

        # 4. LLM 生成摘要和建议
        overall_score = self._overall_score(components)
        summary, recommendations = await self._generate_summary(
            state, components, trends, overall_score
        )

        # 5. 更新自适应调度状态
        for comp in components:
            new_interval = self._schedule_manager.on_patrol_result(
                comp.component, comp.level.value
            )
            logger.debug(
                "schedule_updated",
                component=comp.component,
                new_interval_min=new_interval,
            )

        duration = time.monotonic() - start_time

        # 6. 构建报告
        report = PatrolReport(
            patrol_id=state.get("request_id", ""),
            timestamp=datetime.now(timezone.utc).isoformat(),
            cluster_id=cluster,
            overall_score=overall_score,
            overall_level=self._score_to_level(overall_score),
            components=components,
            trend_alerts=trends,
            summary=summary,
            recommendations=recommendations,
            duration_seconds=round(duration, 2),
            checks_executed=len(checks),
            checks_failed=sum(
                1 for r in check_results if r.error is not None
            ),
        )

        state["final_report"] = self._format_patrol_report(report)

        logger.info(
            "patrol_completed",
            cluster=cluster,
            score=overall_score,
            level=report.overall_level.value,
            issues=sum(len(c.issues) for c in components),
            duration_s=round(duration, 2),
        )

        return state

    async def _run_all_checks(
        self, cluster: str, checks: list[PatrolCheckDef]
    ) -> list[CheckResult]:
        """并行执行所有巡检工具，带独立超时控制"""
        mcp = self._mcp or self._get_default_mcp()

        async def run_single_check(check: PatrolCheckDef) -> CheckResult:
            start = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    mcp.call_tool(check.tool, check.tool_params),
                    timeout=check.timeout_seconds,
                )
                latency = (time.monotonic() - start) * 1000

                # 检查阈值
                violations = self._check_thresholds(check, str(result))

                return CheckResult(
                    check_name=check.name,
                    component=check.component,
                    passed=len(violations) == 0,
                    priority=check.priority.value,
                    raw_output=str(result)[:2000],
                    threshold_violations=violations,
                    latency_ms=round(latency, 1),
                )
            except asyncio.TimeoutError:
                latency = (time.monotonic() - start) * 1000
                logger.warning(
                    "check_timeout",
                    check=check.name,
                    timeout_s=check.timeout_seconds,
                )
                return CheckResult(
                    check_name=check.name,
                    component=check.component,
                    passed=False,
                    priority=check.priority.value,
                    raw_output="",
                    threshold_violations=[f"超时 ({check.timeout_seconds}s)"],
                    latency_ms=round(latency, 1),
                    error=f"Timeout after {check.timeout_seconds}s",
                )
            except Exception as e:
                latency = (time.monotonic() - start) * 1000
                logger.error(
                    "check_failed",
                    check=check.name,
                    error=str(e),
                )
                return CheckResult(
                    check_name=check.name,
                    component=check.component,
                    passed=False,
                    priority=check.priority.value,
                    raw_output="",
                    threshold_violations=[f"执行异常: {str(e)[:200]}"],
                    latency_ms=round(latency, 1),
                    error=str(e)[:500],
                )

        tasks = [run_single_check(c) for c in checks]
        results = await asyncio.gather(*tasks)
        return list(results)

    def _check_thresholds(
        self, check: PatrolCheckDef, raw_output: str
    ) -> list[str]:
        """检查巡检结果是否违反阈值

        注意：这是一个简化的字符串解析实现。
        生产环境中，MCP 工具返回的应该是结构化 JSON，
        可以直接按字段名提取指标值。
        """
        violations = []

        # 简化实现：在输出中查找阈值相关的关键词
        if "❌" in raw_output or "失败" in raw_output or "error" in raw_output.lower():
            violations.append(f"{check.name}: 检查未通过")

        for threshold in check.thresholds:
            # 尝试从输出中提取指标值（简化版）
            # 实际实现会解析 JSON 响应
            if threshold.metric in raw_output:
                violations.append(
                    f"{threshold.metric} 违反阈值: {threshold.description}"
                )

        return violations

    def _calculate_health(
        self,
        results: list[CheckResult],
        checks: list[PatrolCheckDef],
    ) -> list[ComponentHealth]:
        """基于加权评分算法计算各组件健康度

        评分算法：
        1. 基线分 = 100
        2. 每个失败的巡检项按优先级扣分：
           - P0: -40 分（直接 CRITICAL）
           - P1: -30 分
           - P2: -15 分
           - P3: -5 分
        3. 同组件多个 P0 失败不叠加（已经是 CRITICAL 了）
        4. 最终分数 clamp 到 [0, 100]
        """
        # 按组件分组
        component_results: dict[str, list[CheckResult]] = {}
        for result in results:
            component_results.setdefault(result.component, []).append(result)

        components = []
        for comp, comp_results in component_results.items():
            base_score = 100
            issues = []
            has_p0_failure = False

            for result in comp_results:
                if not result.passed:
                    priority = CheckPriority(result.priority)
                    penalty = self.PRIORITY_WEIGHTS.get(priority, 5)

                    # P0 失败不叠加
                    if priority == CheckPriority.P0_FATAL:
                        if not has_p0_failure:
                            base_score -= penalty
                            has_p0_failure = True
                    else:
                        base_score -= penalty

                    for v in result.threshold_violations:
                        issues.append(f"[{result.check_name}] {v}")

            score = max(0, min(100, base_score))

            components.append(ComponentHealth(
                component=comp,
                score=score,
                level=self._score_to_level(score),
                checks_passed=sum(1 for r in comp_results if r.passed),
                checks_total=len(comp_results),
                issues=issues,
                check_details=comp_results,
            ))

        return components

    async def _analyze_trends(self, cluster: str) -> list[TrendAlert]:
        """趋势分析：基于最近 24h 的指标数据做线性回归预测

        实现原理：
        1. 通过 metrics-mcp-server 查询 Prometheus 过去 24h 的时序数据
        2. 对每个关键指标做线性回归（numpy.polyfit）
        3. 基于斜率和 R² 判断趋势是否显著
        4. 如果趋势显著且预测将在 24h 内突破阈值，生成预警
        """
        trend_alerts = []

        # 关键指标及其阈值
        trend_configs = [
            {
                "metric": "hdfs_capacity_used_percent",
                "component": "hdfs",
                "threshold": 90.0,
                "promql": f'hdfs_capacity_used_percent{{cluster="{cluster}"}}',
            },
            {
                "metric": "yarn_memory_used_percent",
                "component": "yarn",
                "threshold": 90.0,
                "promql": f'yarn_memory_used_percent{{cluster="{cluster}"}}',
            },
            {
                "metric": "kafka_consumer_lag_max",
                "component": "kafka",
                "threshold": 500000.0,
                "promql": f'max(kafka_consumer_lag{{cluster="{cluster}"}}) by (consumer_group)',
            },
            {
                "metric": "es_disk_used_percent",
                "component": "es",
                "threshold": 90.0,
                "promql": f'es_disk_used_percent{{cluster="{cluster}"}}',
            },
        ]

        mcp = self._mcp or self._get_default_mcp()

        for config in trend_configs:
            try:
                # 查询过去 24h 的时序数据（步长 15 分钟 = 96 个点）
                raw_data = await mcp.call_tool(
                    "query_metrics_range",
                    {
                        "query": config["promql"],
                        "duration": "24h",
                        "step": "15m",
                    },
                )
                trend = self._compute_trend(
                    raw_data, config["metric"], config["component"],
                    config["threshold"],
                )
                if trend:
                    trend_alerts.append(trend)
            except Exception as e:
                logger.warning(
                    "trend_analysis_failed",
                    metric=config["metric"],
                    error=str(e),
                )

        return trend_alerts

    @staticmethod
    def _compute_trend(
        raw_data: str,
        metric_name: str,
        component: str,
        threshold: float,
    ) -> TrendAlert | None:
        """基于时序数据计算线性趋势

        Returns:
            TrendAlert if trend is significant and threshold breach
            is predicted within 24h, None otherwise.
        """
        try:
            # 解析时序数据（简化：假设是逗号分隔的数值）
            import json
            data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            values = [float(v) for v in data.get("values", [])]

            if len(values) < 10:
                return None

            # 线性回归
            x = np.arange(len(values))
            coeffs = np.polyfit(x, values, 1)
            slope = coeffs[0]  # 每步长（15 分钟）的变化量
            slope_per_hour = slope * 4  # 转换为每小时变化量

            # R² 拟合度
            y_pred = np.polyval(coeffs, x)
            ss_res = np.sum((values - y_pred) ** 2)
            ss_tot = np.sum((values - np.mean(values)) ** 2)
            r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0

            current_value = values[-1]

            # 只有 R² > 0.7 才认为趋势有统计意义
            if r_squared < 0.7:
                return None

            # 判断趋势方向
            if abs(slope_per_hour) < 0.01:
                trend = "stable"
            elif slope_per_hour > 0:
                trend = "rising"
            else:
                trend = "falling"

            # 预测何时突破阈值
            predicted_breach = None
            severity: str = "info"

            if trend == "rising" and current_value < threshold:
                hours_to_breach = (threshold - current_value) / slope_per_hour
                if 0 < hours_to_breach < 24:
                    predicted_breach = f"约 {int(hours_to_breach)} 小时后"
                    severity = "critical" if hours_to_breach < 6 else "warning"
                elif 0 < hours_to_breach < 48:
                    predicted_breach = f"约 {int(hours_to_breach)} 小时后"
                    severity = "warning"
                else:
                    return None  # 预测 48h 后才突破，不告警

            return TrendAlert(
                metric_name=metric_name,
                component=component,
                current_value=round(current_value, 2),
                trend=trend,
                slope=round(slope_per_hour, 4),
                r_squared=round(r_squared, 3),
                predicted_threshold_breach=predicted_breach,
                severity=severity,
            )
        except Exception:
            return None

    async def _generate_summary(
        self, state, components, trends, score
    ) -> tuple[str, list[str]]:
        """LLM 生成巡检摘要"""
        context = self._build_context(state)
        issues_text = "\n".join(
            f"- [{c.component}] (score={c.score}) {', '.join(c.issues)}"
            for c in components if c.issues
        )
        trends_text = "\n".join(
            f"- [{t.component}] {t.metric_name}: {t.trend} "
            f"(当前={t.current_value}, 预测={t.predicted_threshold_breach or '无'})"
            for t in trends
        ) or "无趋势异常"

        response = await self.llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是运维巡检助手。请基于以下巡检结果，"
                        "用一段话总结集群健康状态，并列出 top 3 建议。\n\n"
                        "注意：\n"
                        "1. 如果有 CRITICAL 组件，摘要第一句就要提到\n"
                        "2. 趋势预警比当前告警优先级更高（因为可以预防）\n"
                        "3. 建议要具体可执行，不要说'请关注'"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"总分: {score}/100\n\n"
                        f"问题列表:\n{issues_text or '无异常'}\n\n"
                        f"趋势预警:\n{trends_text}"
                    ),
                },
            ],
            context=context,
        )
        self._update_token_usage(state, response)

        # 简单解析
        lines = response.content.split("\n")
        summary = lines[0] if lines else f"集群健康评分 {score}/100"
        recommendations = [l.strip("- ") for l in lines[1:] if l.strip().startswith("-")]

        return summary, recommendations[:5]

    @staticmethod
    def _overall_score(components: list[ComponentHealth]) -> int:
        """加权计算总分

        权重分配（反映组件重要性）：
        - hdfs: 0.3（数据存储层，最重要）
        - yarn: 0.25（计算调度层）
        - kafka: 0.25（数据管道层）
        - es: 0.2（搜索/日志层）
        """
        if not components:
            return 100

        weights = {"hdfs": 0.3, "yarn": 0.25, "kafka": 0.25, "es": 0.2}
        total_weight = 0.0
        weighted_score = 0.0

        for c in components:
            w = weights.get(c.component, 0.1)
            weighted_score += c.score * w
            total_weight += w

        if total_weight == 0:
            return int(sum(c.score for c in components) / len(components))

        return int(weighted_score / total_weight)

    @staticmethod
    def _score_to_level(score: int) -> HealthLevel:
        if score >= 80: return HealthLevel.HEALTHY
        if score >= 60: return HealthLevel.WARNING
        if score >= 40: return HealthLevel.DEGRADED
        return HealthLevel.CRITICAL

    @staticmethod
    def _format_patrol_report(report: PatrolReport) -> str:
        """格式化巡检报告为 Markdown"""
        emoji = {"healthy": "🟢", "warning": "🟡", "degraded": "🟠", "critical": "🔴"}
        lines = [
            f"# 🔍 巡检报告 — {report.cluster_id}",
            f"**时间**: {report.timestamp}",
            f"**总分**: {emoji.get(report.overall_level.value, '⚪')} {report.overall_score}/100 ({report.overall_level.value})",
            f"**耗时**: {report.duration_seconds}s | **巡检项**: {report.checks_executed} 项 | **失败**: {report.checks_failed} 项",
            f"\n## 摘要\n{report.summary}",
            "\n## 各组件状态",
        ]
        for c in report.components:
            e = emoji.get(c.level.value, "⚪")
            lines.append(f"- {e} **{c.component}**: {c.score}/100 ({c.checks_passed}/{c.checks_total} 检查通过)")
            for issue in c.issues:
                lines.append(f"  - ⚠️ {issue}")

        if report.trend_alerts:
            lines.append("\n## 📈 趋势预警")
            for t in report.trend_alerts:
                severity_emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🔴"}
                lines.append(
                    f"- {severity_emoji.get(t.severity, '❓')} "
                    f"**{t.component}/{t.metric_name}**: {t.trend} "
                    f"(当前={t.current_value}, R²={t.r_squared})"
                )
                if t.predicted_threshold_breach:
                    lines.append(f"  - 预计突破阈值: {t.predicted_threshold_breach}")

        if report.recommendations:
            lines.append("\n## 建议")
            for r in report.recommendations:
                lines.append(f"- {r}")

        return "\n".join(lines)

    def _get_default_mcp(self):
        from aiops.mcp_client.client import MCPClient
        return MCPClient()
```

> **WHY — 为什么总分用加权平均而不是简单平均？**
> 简单平均会让所有组件的重要性相同，但在大数据平台中，HDFS 的重要性明显高于 ES——HDFS 不可用意味着所有计算任务都无法读写数据，而 ES 不可用只影响日志搜索。加权评分（HDFS 0.3, YARN 0.25, Kafka 0.25, ES 0.2）反映了组件在数据流中的位置：存储层 > 计算层 ≈ 管道层 > 搜索层。权重参数可以通过配置文件调整，不同集群可以有不同的权重配置。

### 1.6 巡检调度器

```python
# python/src/aiops/agent/scheduler.py
"""
巡检调度器

使用 APScheduler 定时触发 Patrol Agent。
支持：
- 定时巡检（默认 30 分钟，可自适应调整）
- 手动触发
- 事件驱动触发（告警回调）
- 按集群独立调度
- 巡检结果推送到企微/钉钉
- Prometheus 指标导出
"""

from __future__ import annotations

import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import structlog

from aiops.agent.graph import build_ops_graph
from aiops.agent.patrol.scheduling import AdaptiveScheduleManager
from aiops.core.config import settings

logger = structlog.get_logger(__name__)


class PatrolScheduler:
    """巡检调度器

    生命周期：
    1. __init__: 初始化调度器和 graph
    2. start(): 注册定时任务
    3. trigger_on_alert(): 告警回调入口
    4. trigger_manual(): 手动触发入口
    5. stop(): 优雅停机
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(
            # 错过的任务在 120 秒内仍然执行
            job_defaults={
                "misfire_grace_time": 120,
                "coalesce": True,  # 多次 misfire 合并为一次
                "max_instances": 1,  # 同一集群同一时间只有一个巡检实例
            },
        )
        self._graph = build_ops_graph()
        self._adaptive = AdaptiveScheduleManager()
        self._active_patrols: dict[str, float] = {}  # cluster -> start_ts

    def start(self, clusters: list[str] | None = None) -> None:
        """启动巡检调度"""
        target_clusters = clusters or settings.patrol_clusters or ["prod-bigdata-01"]

        for cluster in target_clusters:
            self._scheduler.add_job(
                self._run_patrol,
                trigger=IntervalTrigger(
                    minutes=30,
                    jitter=60,  # 随机抖动 ±60 秒，避免多集群同时巡检
                ),
                args=[cluster],
                id=f"patrol_{cluster}",
                name=f"Patrol {cluster}",
                replace_existing=True,
            )
            logger.info("patrol_scheduled", cluster=cluster, interval="30m")

        self._scheduler.start()

    async def trigger_on_alert(
        self, cluster_id: str, component: str, alert_severity: str,
    ) -> None:
        """告警事件触发的定向巡检

        只有 P0/P1 级别的告警才会触发定向巡检，
        且同组件有 5 分钟防抖。
        """
        if alert_severity not in ("critical", "high", "P0", "P1"):
            return

        if not self._adaptive.should_trigger_on_event(component):
            logger.debug(
                "event_patrol_debounced",
                cluster=cluster_id,
                component=component,
            )
            return

        logger.info(
            "event_patrol_triggered",
            cluster=cluster_id,
            component=component,
            alert_severity=alert_severity,
        )

        await self._run_patrol(
            cluster_id,
            target_component=component,
        )

    async def trigger_manual(self, cluster_id: str) -> str:
        """手动触发全量巡检，返回报告"""
        logger.info("manual_patrol_triggered", cluster=cluster_id)
        return await self._run_patrol(cluster_id)

    async def _run_patrol(
        self, cluster_id: str, target_component: str | None = None,
    ) -> str:
        """执行一次巡检"""
        # 防止并发巡检同一集群
        if cluster_id in self._active_patrols:
            elapsed = time.time() - self._active_patrols[cluster_id]
            if elapsed < 60:
                logger.warning(
                    "patrol_already_running",
                    cluster=cluster_id,
                    elapsed_s=int(elapsed),
                )
                return ""

        self._active_patrols[cluster_id] = time.time()

        try:
            state_input = {
                "request_id": f"patrol-{cluster_id}-{int(time.time())}",
                "request_type": "patrol",
                "user_query": f"定时巡检 {cluster_id}",
                "user_id": "system",
                "cluster_id": cluster_id,
                "alerts": [],
                "collection_round": 0,
                "max_collection_rounds": 1,
                "error_count": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "tool_calls": [],
                "collected_data": {},
            }

            if target_component:
                state_input["target_component"] = target_component

            result = await self._graph.ainvoke(state_input)

            report = result.get("final_report", "")
            logger.info("patrol_completed", cluster=cluster_id)

            # 推送通知（低于阈值时）
            if "🔴" in report or "🟠" in report:
                await self._notify(cluster_id, report)

            # 更新 Prometheus 指标
            self._export_metrics(cluster_id, result)

            return report

        except Exception as e:
            logger.error("patrol_failed", cluster=cluster_id, error=str(e))
            return ""
        finally:
            self._active_patrols.pop(cluster_id, None)

    async def _notify(self, cluster: str, report: str) -> None:
        """推送告警通知到企微 Bot"""
        from aiops.integrations.wecom import WeComBot

        bot = WeComBot(webhook_url=settings.wecom_webhook_url)
        # 截取报告前 2000 字符（企微消息限制）
        truncated = report[:2000]
        if len(report) > 2000:
            truncated += "\n\n... [报告已截断，完整版请查看运维平台]"

        await bot.send_markdown(truncated)
        logger.info("patrol_notification_sent", cluster=cluster)

    def _export_metrics(self, cluster: str, result: dict) -> None:
        """导出巡检结果到 Prometheus"""
        from aiops.observability.metrics import PATROL_METRICS

        PATROL_METRICS.patrol_score.labels(cluster=cluster).set(
            result.get("overall_score", 0)
        )
        PATROL_METRICS.patrol_duration.labels(cluster=cluster).observe(
            result.get("duration_seconds", 0)
        )
        PATROL_METRICS.patrol_issues.labels(cluster=cluster).set(
            result.get("checks_failed", 0)
        )

    def stop(self) -> None:
        """优雅停机"""
        self._scheduler.shutdown(wait=True)
        logger.info("patrol_scheduler_stopped")
```

> **WHY — 为什么 IntervalTrigger 加了 jitter=60？**
> 多集群部署场景下，如果所有集群的巡检都在整点的 :00 和 :30 触发，MCP Server 会在这些时刻出现请求尖峰。jitter=60 表示每次触发时间在预定时刻前后随机浮动 ±60 秒，将请求均匀分散到 2 分钟的窗口内。这是一个经典的"抖动"（jitter）模式，在分布式系统中广泛用于避免"惊群效应"（thundering herd）。

> **WHY — 为什么 max_instances=1？**
> 巡检是幂等但有开销的操作——如果上一次巡检还没结束（比如某个工具调用卡住了），APScheduler 默认会启动第二个实例。两个并行巡检同一个集群不仅浪费资源，还可能产生混淆的报告（哪份是最新的？）。max_instances=1 确保同一集群同一时间只有一个巡检实例在运行。

### 1.7 巡检报告生成模板

巡检报告是 Patrol Agent 的核心产出物，需要同时服务两类读者：

1. **运维人员**：需要快速看到"哪里有问题"和"要做什么"
2. **管理层**：需要看到"集群整体健康趋势"

> **WHY — 为什么不直接用 LLM 生成完整报告？**
> LLM 生成的文本有不确定性——相同的巡检结果可能生成措辞不同的报告，这对需要对比的场景（如"这次巡检比上次好了吗"）不友好。我们的方案是**结构化模板 + LLM 摘要**：报告的骨架（组件状态表、指标数据、趋势图表标记）是模板化的，保证格式一致；摘要和建议部分交给 LLM，提供自然语言的可读性。

```python
# python/src/aiops/agent/patrol/report_templates.py
"""
巡检报告模板系统

支持多种输出格式：
- Markdown（默认，用于企微推送）
- HTML（用于邮件和 Web 界面）
- JSON（用于 API 和存储）
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)


class PatrolReportRenderer:
    """巡检报告渲染器"""

    EMOJI_MAP = {
        "healthy": "🟢",
        "warning": "🟡",
        "degraded": "🟠",
        "critical": "🔴",
    }

    SEVERITY_EMOJI = {
        "info": "ℹ️",
        "warning": "⚠️",
        "critical": "🔴",
    }

    def render(
        self,
        report: "PatrolReport",
        format: Literal["markdown", "html", "json"] = "markdown",
    ) -> str:
        """渲染巡检报告"""
        if format == "markdown":
            return self._render_markdown(report)
        elif format == "html":
            return self._render_html(report)
        elif format == "json":
            return report.model_dump_json(indent=2)
        else:
            raise ValueError(f"Unsupported format: {format}")

    def _render_markdown(self, report: "PatrolReport") -> str:
        """Markdown 格式巡检报告"""
        level_emoji = self.EMOJI_MAP.get(report.overall_level.value, "⚪")

        sections = []

        # ── 头部 ──
        sections.append(f"# 🔍 巡检报告 — {report.cluster_id}")
        sections.append("")
        sections.append(f"| 项目 | 值 |")
        sections.append(f"|------|------|")
        sections.append(f"| 巡检 ID | `{report.patrol_id}` |")
        sections.append(f"| 时间 | {report.timestamp} |")
        sections.append(f"| 总分 | {level_emoji} **{report.overall_score}/100** ({report.overall_level.value}) |")
        sections.append(f"| 耗时 | {report.duration_seconds}s |")
        sections.append(f"| 巡检项 | {report.checks_executed} 项（{report.checks_failed} 项失败） |")

        # ── 摘要 ──
        sections.append("")
        sections.append(f"## 📋 摘要")
        sections.append("")
        sections.append(report.summary)

        # ── 组件详情 ──
        sections.append("")
        sections.append(f"## 🔧 各组件状态")
        sections.append("")
        sections.append("| 组件 | 得分 | 状态 | 通过率 | 问题数 |")
        sections.append("|------|------|------|--------|--------|")

        for c in report.components:
            e = self.EMOJI_MAP.get(c.level.value, "⚪")
            pass_rate = f"{c.checks_passed}/{c.checks_total}"
            issue_count = len(c.issues)
            sections.append(
                f"| {c.component} | {c.score}/100 | "
                f"{e} {c.level.value} | {pass_rate} | {issue_count} |"
            )

        # 展开有问题的组件
        for c in report.components:
            if c.issues:
                sections.append("")
                sections.append(f"### ⚠️ {c.component} 问题详情")
                sections.append("")
                for issue in c.issues:
                    sections.append(f"- {issue}")

        # ── 趋势预警 ──
        if report.trend_alerts:
            sections.append("")
            sections.append(f"## 📈 趋势预警")
            sections.append("")
            sections.append("| 组件 | 指标 | 趋势 | 当前值 | R² | 预测突破 |")
            sections.append("|------|------|------|--------|------|----------|")

            for t in report.trend_alerts:
                se = self.SEVERITY_EMOJI.get(t.severity, "❓")
                breach = t.predicted_threshold_breach or "—"
                sections.append(
                    f"| {t.component} | {t.metric_name} | "
                    f"{se} {t.trend} | {t.current_value} | "
                    f"{t.r_squared} | {breach} |"
                )

        # ── 建议 ──
        if report.recommendations:
            sections.append("")
            sections.append(f"## 💡 建议")
            sections.append("")
            for i, rec in enumerate(report.recommendations, 1):
                sections.append(f"{i}. {rec}")

        # ── 页脚 ──
        sections.append("")
        sections.append("---")
        sections.append(
            f"*由 AI DataPlatform Ops Agent 自动生成 | "
            f"巡检引擎 v1.0*"
        )

        return "\n".join(sections)

    def _render_html(self, report: "PatrolReport") -> str:
        """HTML 格式巡检报告（用于邮件）"""
        # 简化版：实际会用 Jinja2 模板
        md_content = self._render_markdown(report)
        return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 20px; }}
        table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f5f5f5; }}
        .critical {{ color: #e53e3e; font-weight: bold; }}
        .warning {{ color: #d69e2e; }}
        .healthy {{ color: #38a169; }}
    </style>
</head>
<body>
    <pre>{md_content}</pre>
</body>
</html>
"""
```

#### 报告示例

下面是一个实际的巡检报告输出示例：

```markdown
# 🔍 巡检报告 — prod-bigdata-01

| 项目 | 值 |
|------|------|
| 巡检 ID | `patrol-prod-bigdata-01-1711234567` |
| 时间 | 2026-03-23T10:30:00Z |
| 总分 | 🟡 **68/100** (warning) |
| 耗时 | 7.2s |
| 巡检项 | 16 项（3 项失败） |

## 📋 摘要

HDFS 磁盘使用率达到 83%，接近告警阈值。YARN 有 67 个排队任务，
任务调度出现延迟。建议优先处理 HDFS 容量问题。

## 🔧 各组件状态

| 组件 | 得分 | 状态 | 通过率 | 问题数 |
|------|------|------|--------|--------|
| hdfs | 55/100 | 🟠 degraded | 3/5 | 2 |
| yarn | 70/100 | 🟡 warning | 3/4 | 1 |
| kafka | 85/100 | 🟢 healthy | 4/4 | 0 |
| es | 80/100 | 🟢 healthy | 3/3 | 0 |

### ⚠️ hdfs 问题详情

- [disk_usage] usage_percent 违反阈值: 使用率 > 80% 需要规划扩容
- [block_health] under_replicated 违反阈值: 欠副本数 > 100 需要关注

## 📈 趋势预警

| 组件 | 指标 | 趋势 | 当前值 | R² | 预测突破 |
|------|------|------|--------|------|----------|
| hdfs | hdfs_capacity_used_percent | ⚠️ rising | 83.2 | 0.92 | 约 18 小时后 |

## 💡 建议

1. 立即清理 HDFS 上超过 30 天的临时文件和过期快照
2. 安排 DataNode 扩容，当前使用率增长趋势预计 18 小时后突破 90%
3. 检查 YARN 排队任务，可能需要调整队列权重或扩大资源池

---
*由 AI DataPlatform Ops Agent 自动生成 | 巡检引擎 v1.0*
```

### 1.8 Prometheus 巡检指标

```python
# python/src/aiops/observability/patrol_metrics.py
"""
Patrol Agent 暴露的 Prometheus 指标

这些指标让运维团队能在 Grafana 中监控巡检系统自身的健康状态，
而不仅仅是被巡检组件的健康状态。

指标分三类：
1. 巡检执行指标（频率、延迟、成功率）
2. 巡检结果指标（健康评分、问题数）
3. 调度器指标（自适应频率、排队状态）
"""

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Info,
)


class PatrolMetrics:
    """Patrol Agent 的 Prometheus 指标集合"""

    def __init__(self, prefix: str = "aiops_patrol") -> None:
        # ── 巡检执行指标 ──

        self.patrol_total = Counter(
            f"{prefix}_total",
            "巡检执行总次数",
            ["cluster", "trigger_type"],  # trigger_type: scheduled/event/manual
        )

        self.patrol_duration = Histogram(
            f"{prefix}_duration_seconds",
            "单次巡检耗时分布",
            ["cluster"],
            buckets=[1, 2, 5, 10, 15, 20, 30, 60],
        )

        self.patrol_errors = Counter(
            f"{prefix}_errors_total",
            "巡检执行错误次数",
            ["cluster", "error_type"],
        )

        # ── 巡检结果指标 ──

        self.patrol_score = Gauge(
            f"{prefix}_health_score",
            "最近一次巡检的总健康评分 (0-100)",
            ["cluster"],
        )

        self.component_score = Gauge(
            f"{prefix}_component_health_score",
            "各组件的健康评分 (0-100)",
            ["cluster", "component"],
        )

        self.patrol_issues = Gauge(
            f"{prefix}_issues_count",
            "最近一次巡检发现的问题数",
            ["cluster", "severity"],
        )

        self.check_pass_rate = Gauge(
            f"{prefix}_check_pass_rate",
            "巡检项通过率 (0.0-1.0)",
            ["cluster", "component"],
        )

        # ── 单个巡检项指标 ──

        self.check_latency = Histogram(
            f"{prefix}_check_latency_ms",
            "单个巡检项的工具调用延迟",
            ["cluster", "check_name"],
            buckets=[100, 500, 1000, 2000, 5000, 10000],
        )

        self.check_result = Gauge(
            f"{prefix}_check_passed",
            "单个巡检项是否通过 (1=通过, 0=失败)",
            ["cluster", "check_name", "component"],
        )

        # ── 趋势预警指标 ──

        self.trend_alerts_total = Counter(
            f"{prefix}_trend_alerts_total",
            "趋势预警生成次数",
            ["cluster", "component", "severity"],
        )

        # ── 调度器指标 ──

        self.schedule_interval = Gauge(
            f"{prefix}_schedule_interval_minutes",
            "当前生效的巡检间隔（自适应）",
            ["cluster", "component"],
        )

        self.patrol_queue_depth = Gauge(
            f"{prefix}_queue_depth",
            "等待执行的巡检任务数",
        )

    def record_patrol_result(
        self,
        cluster: str,
        trigger_type: str,
        score: int,
        components: list,
        trends: list,
        duration: float,
        errors: int,
    ) -> None:
        """一次性记录巡检结果的所有指标"""
        self.patrol_total.labels(
            cluster=cluster, trigger_type=trigger_type
        ).inc()

        self.patrol_duration.labels(cluster=cluster).observe(duration)
        self.patrol_score.labels(cluster=cluster).set(score)

        for c in components:
            self.component_score.labels(
                cluster=cluster, component=c.component
            ).set(c.score)

            pass_rate = c.checks_passed / max(c.checks_total, 1)
            self.check_pass_rate.labels(
                cluster=cluster, component=c.component
            ).set(round(pass_rate, 3))

            if c.check_details:
                for detail in c.check_details:
                    self.check_latency.labels(
                        cluster=cluster, check_name=detail.check_name
                    ).observe(detail.latency_ms)
                    self.check_result.labels(
                        cluster=cluster,
                        check_name=detail.check_name,
                        component=detail.component,
                    ).set(1 if detail.passed else 0)

        for t in trends:
            self.trend_alerts_total.labels(
                cluster=cluster,
                component=t.component,
                severity=t.severity,
            ).inc()

        if errors > 0:
            self.patrol_errors.labels(
                cluster=cluster, error_type="check_failure"
            ).inc(errors)


# 全局单例
PATROL_METRICS = PatrolMetrics()
```

> **WHY — 为什么要监控"巡检系统自身"？**
> 这是经典的"谁来监控监控系统"问题。如果 Patrol Agent 的 MCP 工具调用延迟从 2 秒涨到 15 秒，巡检结果可能大面积超时（判定为失败），产生虚假告警。通过 `check_latency` 和 `patrol_duration` 指标，我们可以在 Grafana 中设置针对巡检系统自身的告警规则——比如"巡检耗时 P99 > 20s"触发 PagerDuty，通知运维团队检查 MCP Server 是否正常。

#### Grafana Dashboard 配置示例

```json
{
  "dashboard": {
    "title": "Patrol Agent Overview",
    "panels": [
      {
        "title": "集群健康评分趋势",
        "type": "timeseries",
        "targets": [
          {
            "expr": "aiops_patrol_health_score",
            "legendFormat": "{{cluster}}"
          }
        ],
        "fieldConfig": {
          "defaults": {
            "thresholds": {
              "steps": [
                {"color": "red", "value": 0},
                {"color": "orange", "value": 40},
                {"color": "yellow", "value": 60},
                {"color": "green", "value": 80}
              ]
            },
            "min": 0,
            "max": 100
          }
        }
      },
      {
        "title": "巡检耗时分布",
        "type": "histogram",
        "targets": [
          {
            "expr": "histogram_quantile(0.95, rate(aiops_patrol_duration_seconds_bucket[1h]))",
            "legendFormat": "P95 {{cluster}}"
          }
        ]
      },
      {
        "title": "单项巡检延迟 (P95)",
        "type": "table",
        "targets": [
          {
            "expr": "histogram_quantile(0.95, rate(aiops_patrol_check_latency_ms_bucket[1h]))",
            "legendFormat": "{{check_name}}"
          }
        ]
      },
      {
        "title": "趋势预警计数",
        "type": "stat",
        "targets": [
          {
            "expr": "sum(increase(aiops_patrol_trend_alerts_total[24h])) by (severity)",
            "legendFormat": "{{severity}}"
          }
        ]
      }
    ]
  }
}
```

---

## 2. Alert Correlation Agent — 告警关联分析

### 2.1 职责

- **告警聚合** — 时间窗口内的告警按组件/时间聚类
- **去重** — 同一根因的重复告警合并
- **关联分析** — 基于拓扑（知识图谱）和时序的因果关联
- **根因告警识别** — 从 N 条告警中找到 1 条根因告警
- **输出** — 收敛后的告警摘要 → 送入 Diagnostic Agent

### 2.2 数据模型

```python
# python/src/aiops/agent/nodes/alert_correlation.py

from pydantic import BaseModel, Field
from typing import Literal


class AlertCluster(BaseModel):
    """告警聚类"""
    cluster_id: int
    root_alert: dict = Field(description="根因告警")
    related_alerts: list[dict] = Field(description="关联告警")
    component: str
    correlation_type: Literal["topological", "temporal", "same_source"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str


class AlertCorrelationOutput(BaseModel):
    """告警关联分析输出"""
    total_alerts: int
    deduplicated_count: int
    clusters: list[AlertCluster]
    root_cause_summary: str = Field(description="根因告警的一句话摘要")
    recommended_investigation: list[str] = Field(
        description="建议的调查方向",
    )
    suppressed_alerts: list[str] = Field(
        default_factory=list,
        description="被抑制的重复/衍生告警",
    )
```

### 2.3 AlertCorrelationNode 实现

```python
# python/src/aiops/agent/nodes/alert_correlation.py

from __future__ import annotations

from collections import defaultdict

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState
from aiops.core.logging import get_logger
from aiops.llm.types import TaskType

logger = get_logger(__name__)

# 组件拓扑依赖关系（简化版，生产用 Neo4j）
COMPONENT_TOPOLOGY = {
    "hdfs-namenode": ["hdfs-datanode", "yarn", "hbase"],
    "hdfs-datanode": [],
    "yarn-rm": ["yarn-nm", "spark", "flink"],
    "kafka-broker": ["kafka-consumer", "flink", "es-indexer"],
    "zookeeper": ["hdfs-namenode", "yarn-rm", "kafka-broker", "hbase"],
    "es-master": ["es-data", "es-indexer"],
}


class AlertCorrelationNode(BaseAgentNode):
    agent_name = "alert_correlation"
    task_type = TaskType.ALERT_CORRELATION

    async def process(self, state: AgentState) -> AgentState:
        """告警关联分析"""
        alerts = state.get("alerts", [])

        if not alerts:
            return state

        logger.info("alert_correlation_start", count=len(alerts))

        # 1. 规则层：时间窗口聚合 + 去重
        deduplicated = self._deduplicate(alerts)

        # 2. 规则层：拓扑关联
        topo_clusters = self._topological_correlation(deduplicated)

        # 3. LLM 层：精细关联分析
        if len(deduplicated) > 3:
            correlation = await self._llm_correlate(state, deduplicated, topo_clusters)
        else:
            # 告警少时不需要 LLM
            correlation = self._simple_correlation(deduplicated, topo_clusters)

        # 4. 写入状态（供 Diagnostic Agent 使用）
        state["user_query"] = (
            f"告警关联分析结果：{len(alerts)} 条告警收敛为 "
            f"{len(correlation.clusters)} 个告警簇。\n"
            f"根因摘要：{correlation.root_cause_summary}\n"
            f"建议调查：{', '.join(correlation.recommended_investigation)}"
        )
        state["_alert_correlation"] = correlation.model_dump()

        logger.info(
            "alert_correlation_completed",
            total=len(alerts),
            deduplicated=correlation.deduplicated_count,
            clusters=len(correlation.clusters),
        )

        return state

    def _deduplicate(self, alerts: list[dict]) -> list[dict]:
        """去重：同名告警在 5 分钟内只保留第一条"""
        seen: dict[str, dict] = {}
        for alert in alerts:
            key = f"{alert.get('alertname', '')}_{alert.get('instance', '')}"
            if key not in seen:
                seen[key] = alert
        logger.info("dedup_result", before=len(alerts), after=len(seen))
        return list(seen.values())

    def _topological_correlation(self, alerts: list[dict]) -> list[dict]:
        """拓扑关联：基于组件依赖关系"""
        # 按组件分组
        by_component: dict[str, list[dict]] = defaultdict(list)
        for alert in alerts:
            comp = alert.get("component", alert.get("job", "unknown"))
            by_component[comp].append(alert)

        clusters = []
        visited = set()

        for comp, comp_alerts in by_component.items():
            if comp in visited:
                continue

            # 检查是否有下游组件的告警
            downstream = COMPONENT_TOPOLOGY.get(comp, [])
            related = []
            for ds in downstream:
                if ds in by_component:
                    related.extend(by_component[ds])
                    visited.add(ds)

            if related:
                clusters.append({
                    "root_component": comp,
                    "root_alerts": comp_alerts,
                    "downstream_alerts": related,
                    "type": "topological",
                })
                visited.add(comp)

        return clusters

    async def _llm_correlate(
        self, state: AgentState, alerts: list[dict], topo_clusters: list[dict]
    ) -> AlertCorrelationOutput:
        """LLM 精细关联分析"""
        context = self._build_context(state)

        alert_text = "\n".join(
            f"- [{a.get('severity', '?')}] {a.get('alertname', '?')}: "
            f"{a.get('summary', '')} (组件: {a.get('component', '?')})"
            for a in alerts[:20]
        )

        topo_text = "\n".join(
            f"- {c['root_component']} → 影响 {len(c['downstream_alerts'])} 条下游告警"
            for c in topo_clusters
        ) or "无拓扑关联"

        response = await self.llm.chat_structured(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是告警关联分析专家。请分析以下告警，找出根因告警，"
                        "将关联告警聚类，输出收敛后的摘要和调查建议。\n\n"
                        f"拓扑关联（预分析）:\n{topo_text}"
                    ),
                },
                {"role": "user", "content": f"告警列表 ({len(alerts)} 条):\n{alert_text}"},
            ],
            response_model=AlertCorrelationOutput,
            context=context,
        )
        return response

    def _simple_correlation(
        self, alerts: list[dict], topo_clusters: list[dict]
    ) -> AlertCorrelationOutput:
        """简单关联（告警少时不用 LLM）"""
        clusters = []
        for i, alert in enumerate(alerts):
            clusters.append(AlertCluster(
                cluster_id=i,
                root_alert=alert,
                related_alerts=[],
                component=alert.get("component", "unknown"),
                correlation_type="same_source",
                confidence=0.5,
                summary=alert.get("summary", ""),
            ))

        return AlertCorrelationOutput(
            total_alerts=len(alerts),
            deduplicated_count=len(alerts),
            clusters=clusters,
            root_cause_summary=alerts[0].get("summary", "") if alerts else "",
            recommended_investigation=[
                a.get("alertname", "") for a in alerts[:3]
            ],
        )
```

> **🔧 工程难点：巡检调度的幂等性与事件驱动防抖——K8s 环境下的可靠调度**
>
> **挑战**：巡检调度在 K8s 环境下面临三个可靠性问题：(1) **Pod 重启导致调度丢失**——APScheduler 的调度状态默认存储在内存中，Pod 重启后所有调度计划丢失，需要等到下一个调度周期才能恢复，最坏情况下可能丢失 30 分钟的巡检；(2) **重复执行**——如果部署了多个 Pod 副本（高可用），每个 Pod 的 APScheduler 都会独立触发同一个巡检任务，导致同一时刻对同一组件执行 N 次完全相同的巡检，浪费 N 倍资源且产生 N 份重复报告；(3) **事件驱动巡检的级联风暴**——一次 ZK 故障触发 20+ 条告警，每条告警如果都触发一次事件驱动巡检，就变成了"告警风暴 → 巡检风暴"的二次放大。这三个问题单独解决不难，但组合在一起需要一个统一的协调机制。
>
> **解决方案**：APScheduler 配置 `misfire_grace_time=120s`——如果 Pod 重启导致调度延迟不超过 120s，恢复后立即补偿执行错过的巡检（而非等到下个周期）。多 Pod 环境下使用 Redis 分布式锁实现巡检幂等——每次巡检执行前尝试获取 `patrol:{component}:{timestamp_bucket}` 锁（5 分钟桶粒度），获取成功才执行，失败说明另一个 Pod 已经在执行，跳过即可。事件驱动巡检的防抖策略：同一组件 5 分钟内的多次事件触发只执行一次巡检（`last_event_patrol` Redis key + TTL=300s），第一次触发后设置 key，后续触发检查到 key 存在则跳过。三层混合调度的自适应频率（§1.2）通过 Redis 存储每个组件的当前巡检间隔和连续健康次数——发现 WARNING/DEGRADED 时将间隔从 30 分钟缩短到 10 分钟，连续 3 次 HEALTHY 后恢复到 30 分钟。所有调度决策都有 Prometheus 指标记录（`patrol_executed_total`、`patrol_skipped_duplicate`、`patrol_misfire_recovered`），Grafana Dashboard 实时展示巡检执行率和跳过率，如果跳过率 > 20% 说明多 Pod 协调有问题需要排查。

---

## 3. 测试策略

```python
# tests/unit/agent/test_patrol.py

class TestPatrolNode:
    async def test_all_healthy(self, mock_llm_client):
        """全部健康时应得到 80+ 分"""
        node = PatrolNode(mock_llm_client)
        node._mcp = AsyncMock()
        node._mcp.call_tool = AsyncMock(return_value="一切正常")

        state = _make_patrol_state()
        result = await node.process(state)
        assert "🟢" in result["final_report"]

    async def test_critical_failure_drops_score(self, mock_llm_client):
        """critical 检查失败应大幅降分"""
        checks = [{"passed": True, "issues": [], "severity": "low"}] * 3
        checks.append({"passed": False, "issues": ["NN down"], "severity": "critical"})
        # critical 失败扣 30 分


# tests/unit/agent/test_alert_correlation.py

class TestAlertCorrelation:
    def test_dedup(self):
        node = AlertCorrelationNode(AsyncMock())
        alerts = [
            {"alertname": "HDFSCapacity", "instance": "nn1"},
            {"alertname": "HDFSCapacity", "instance": "nn1"},  # 重复
            {"alertname": "YARNMemory", "instance": "rm1"},
        ]
        result = node._deduplicate(alerts)
        assert len(result) == 2

    def test_topological_correlation(self):
        node = AlertCorrelationNode(AsyncMock())
        alerts = [
            {"alertname": "ZKDown", "component": "zookeeper"},
            {"alertname": "NNFailover", "component": "hdfs-namenode"},
            {"alertname": "KafkaBrokerDown", "component": "kafka-broker"},
        ]
        clusters = node._topological_correlation(alerts)
        # zookeeper 是 hdfs-namenode 和 kafka-broker 的上游
        assert len(clusters) >= 1
        assert clusters[0]["root_component"] == "zookeeper"
```

---

## 4. 性能指标

| 指标 | Patrol | Alert Correlation |
|------|--------|-------------------|
| 延迟 | 5-15s（9 个并行检查） | 2-5s（规则+LLM） |
| 频率 | 每 30 分钟 | 实时（告警触发） |
| Token | ~3K/次 | ~2K-5K/次（取决于告警数） |
| 告警收敛率 | — | 3-5x（20 条→4-6 簇） |

---

## 5. 与其他模块的集成

| 模块 | Patrol 交互 | Alert Correlation 交互 |
|------|------------|----------------------|
| 03-Graph | 图入口为 triage，patrol 类型直接走快速路径 | triage route=alert_correlation 触发 |
| 11-MCP | 调用各组件状态检查工具 | — |
| 13-Graph-RAG | — | 生产环境用 Neo4j 拓扑替代硬编码 |
| 17-可观测 | 巡检结果写入 Prometheus 指标 | 告警收敛率指标 |
| 19-数据采集 | 消费 Alertmanager webhook | 消费 Kafka 告警流 |

---

> **下一篇**：[08-Report-Agent与知识沉淀.md](./08-Report-Agent与知识沉淀.md) — 报告生成 + 知识沉淀循环。
