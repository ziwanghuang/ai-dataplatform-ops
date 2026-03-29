# 09 - MCP Server 实现（Go）

> **设计文档引用**：`03-智能诊断Agent系统设计.md` §4.1-4.3 MCP Server 架构, `01-系统总体架构.md` §MCP 工具集  
> **职责边界**：8 个内部 MCP Server（Go 实现）、42 个工具、MCP JSON-RPC 2.0 协议层  
> **优先级**：P0

---

## 1. 模块概述

### 1.1 架构

```
                  MCP Gateway (Go Fiber)
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
   ┌───────────┐ ┌───────────┐ ┌───────────┐
   │ hdfs-mcp  │ │ kafka-mcp │ │ es-mcp    │ ...（8 个）
   │ 8 tools   │ │ 6 tools   │ │ 6 tools   │
   └───────────┘ └───────────┘ └───────────┘
```

### 1.2 8 个 MCP Server × 42 个工具

| Server | 工具数 | 核心工具 | 数据源 |
|--------|--------|---------|--------|
| hdfs-mcp | 8 | cluster_overview, namenode_status, datanode_list, block_report | HDFS WebHDFS API |
| yarn-mcp | 5 | cluster_metrics, queue_status, applications, node_status | YARN REST API |
| kafka-mcp | 6 | cluster_overview, consumer_lag, topic_list, partition_status | Kafka Admin API |
| es-mcp | 6 | cluster_health, node_stats, index_status, shard_allocation | ES REST API |
| metrics-mcp | 4 | query_metrics, query_metrics_range, anomaly_detection | VictoriaMetrics/Prometheus |
| log-mcp | 4 | search_logs, search_logs_context, log_pattern_analysis | Elasticsearch/Loki |
| config-mcp | 4 | get_component_config, diff_config_versions, validate_config | 配置中心/文件系统 |
| ops-mcp | 5 | restart_service, scale_resource, failover_namenode ⚠️ | 各组件管理 API |

---

## 2. MCP 协议层

### 2.1 JSON-RPC 2.0 消息类型

```go
// go/internal/protocol/types.go
package protocol

// MCP JSON-RPC 2.0 请求
type Request struct {
    JSONRPC string          `json:"jsonrpc"`
    ID      interface{}     `json:"id"`
    Method  string          `json:"method"`
    Params  json.RawMessage `json:"params,omitempty"`
}

// MCP JSON-RPC 2.0 响应
type Response struct {
    JSONRPC string      `json:"jsonrpc"`
    ID      interface{} `json:"id"`
    Result  interface{} `json:"result,omitempty"`
    Error   *RPCError   `json:"error,omitempty"`
}

type RPCError struct {
    Code    int         `json:"code"`
    Message string      `json:"message"`
    Data    interface{} `json:"data,omitempty"`
}

// MCP Tool 定义
type ToolDefinition struct {
    Name        string                 `json:"name"`
    Description string                 `json:"description"`
    InputSchema map[string]interface{} `json:"inputSchema"`
}

// MCP Tool 调用参数
type ToolCallParams struct {
    Name      string                 `json:"name"`
    Arguments map[string]interface{} `json:"arguments"`
}

// MCP Tool 调用结果
type ToolResult struct {
    Content []ContentBlock `json:"content"`
    IsError bool           `json:"isError,omitempty"`
}

type ContentBlock struct {
    Type string `json:"type"` // "text"
    Text string `json:"text"`
}
```

### 2.2 工具注册中心

```go
// go/internal/protocol/registry.go
package protocol

import (
    "fmt"
    "sync"
)

// Tool 工具接口
type Tool interface {
    Name() string
    Description() string
    Schema() map[string]interface{}
    RiskLevel() RiskLevel
    Execute(ctx context.Context, params map[string]interface{}) (*ToolResult, error)
}

type RiskLevel int

const (
    RiskNone     RiskLevel = 0  // 只读
    RiskLow      RiskLevel = 1
    RiskMedium   RiskLevel = 2
    RiskHigh     RiskLevel = 3
    RiskCritical RiskLevel = 4
)

// Registry 工具注册中心
type Registry struct {
    mu    sync.RWMutex
    tools map[string]Tool
}

func NewRegistry() *Registry {
    return &Registry{tools: make(map[string]Tool)}
}

func (r *Registry) Register(tool Tool) {
    r.mu.Lock()
    defer r.mu.Unlock()
    r.tools[tool.Name()] = tool
}

func (r *Registry) Get(name string) (Tool, error) {
    r.mu.RLock()
    defer r.mu.RUnlock()
    tool, ok := r.tools[name]
    if !ok {
        return nil, fmt.Errorf("tool not found: %s", name)
    }
    return tool, nil
}

func (r *Registry) ListDefinitions() []ToolDefinition {
    r.mu.RLock()
    defer r.mu.RUnlock()
    defs := make([]ToolDefinition, 0, len(r.tools))
    for _, t := range r.tools {
        defs = append(defs, ToolDefinition{
            Name:        t.Name(),
            Description: t.Description(),
            InputSchema: t.Schema(),
        })
    }
    return defs
}
```

### 2.2.1 RiskLevel 设计与安全模型

> **WHY** — 大数据运维场景中，工具的风险差异巨大：`hdfs_namenode_status`（只读查看）和 `ops_restart_service`（重启生产服务）
> 的危险程度完全不同。如果不在工具定义层标注风险等级，就无法在调用链路中自动化地拦截和审批高风险操作。

#### 5 级风险等级定义

| 等级 | 常量 | 含义 | 判定标准 | 代表工具 |
|------|------|------|---------|---------|
| 0 | `RiskNone` | 纯只读 | 只查询、不修改任何状态 | `hdfs_namenode_status`, `kafka_consumer_lag` |
| 1 | `RiskLow` | 低风险只读 | 查询可能产生负载（如全量扫描） | `log_search`（大范围查询可能影响 ES 性能） |
| 2 | `RiskMedium` | 中等风险 | 修改非关键配置、触发安全的运维操作 | `ops_clear_cache`, `ops_trigger_gc` |
| 3 | `RiskHigh` | 高风险 | 影响服务可用性的操作 | `ops_restart_service`, `ops_scale_resource` |
| 4 | `RiskCritical` | 极高风险 | 不可逆或影响整个集群的操作 | `ops_decommission_node`, `ops_failover_namenode` |

> **WHY 5 级而不是 3 级或布尔值？**
> - 布尔值（safe/unsafe）粒度太粗——`clear_cache`（影响小）和 `restart_service`（影响大）会被归为同一类
> - 3 级（low/medium/high）无法区分"只读但可能造成负载"（Level 1）和"真正的只读"（Level 0）
> - 5 级与 HITL 审批流程对齐：Level 0-1 自动执行，Level 2 需要通知，Level 3 需要审批，Level 4 需要双人审批

#### 风险等级 → HITL 审批映射

```
RiskLevel 0-1 → 自动执行，仅记录审计日志
RiskLevel 2   → 自动执行，但推送通知到值班群
RiskLevel 3   → 暂停执行 → HITL 审批（企微 Bot → 值班人员确认） → 执行/拒绝
RiskLevel 4   → 暂停执行 → HITL 双人审批（需要两位 SRE 确认） → 执行/拒绝
```

> **WHY 把风险评估放在工具定义层（Tool.RiskLevel()）而非中间件层？**
> - 工具开发者最清楚自己的工具有多危险——让开发者在定义工具时就声明风险等级
> - 中间件只做"根据 RiskLevel 执行策略"，不做"判断这个工具危不危险"
> - 如果放在中间件层，就需要维护一个"工具名 → 风险等级"的映射表，每新增工具都要改两个地方
> - 如果放在中间件层不改，中间件就需要理解工具的语义——这违反了单一职责原则

#### 风险评估中间件实现

```go
// go/internal/middleware/risk_assessment.go
package middleware

import (
    "context"
    "fmt"
    "time"

    "github.com/rs/zerolog/log"
    "github.com/yourorg/aiops-mcp/internal/protocol"
)

// RiskAssessmentMiddleware 根据工具的 RiskLevel 决定是否需要 HITL 审批
func RiskAssessmentMiddleware(tool protocol.Tool, next protocol.ToolHandler) protocol.ToolHandler {
    return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
        risk := tool.RiskLevel()

        // Level 0-1: 直接执行
        if risk <= protocol.RiskLow {
            log.Debug().
                Str("tool", tool.Name()).
                Int("risk", int(risk)).
                Msg("auto-approved: low risk")
            return next(ctx, params)
        }

        // Level 2: 执行但发通知
        if risk == protocol.RiskMedium {
            log.Info().
                Str("tool", tool.Name()).
                Int("risk", int(risk)).
                Interface("params", params).
                Msg("executing medium-risk tool, sending notification")
            go notifyOnCallGroup(ctx, tool.Name(), params) // 异步通知，不阻塞执行
            return next(ctx, params)
        }

        // Level 3-4: 需要 HITL 审批
        approvalRequired := 1
        if risk == protocol.RiskCritical {
            approvalRequired = 2 // 双人审批
        }

        log.Warn().
            Str("tool", tool.Name()).
            Int("risk", int(risk)).
            Int("approvals_required", approvalRequired).
            Interface("params", params).
            Msg("high-risk tool call, requesting HITL approval")

        // 创建审批请求
        approvalReq := &ApprovalRequest{
            ToolName:          tool.Name(),
            RiskLevel:         int(risk),
            Parameters:        params,
            ApprovalsRequired: approvalRequired,
            RequestedAt:       time.Now(),
            Timeout:           5 * time.Minute,
        }

        // 发送到审批队列（Redis + 企微 Bot）
        approved, err := requestHITLApproval(ctx, approvalReq)
        if err != nil {
            return &protocol.ToolResult{
                Content: []protocol.ContentBlock{{
                    Type: "text",
                    Text: fmt.Sprintf("❌ HITL 审批请求失败: %s\n工具 %s (风险等级 %d) 未执行。",
                        err, tool.Name(), risk),
                }},
                IsError: true,
            }, nil
        }

        if !approved {
            return &protocol.ToolResult{
                Content: []protocol.ContentBlock{{
                    Type: "text",
                    Text: fmt.Sprintf("🚫 HITL 审批被拒绝\n工具 %s 未执行。值班人员认为当前不适合执行此操作。",
                        tool.Name()),
                }},
                IsError: true,
            }, nil
        }

        log.Info().
            Str("tool", tool.Name()).
            Msg("HITL approved, executing high-risk tool")
        return next(ctx, params)
    }
}

// ApprovalRequest HITL 审批请求
type ApprovalRequest struct {
    ToolName          string                 `json:"tool_name"`
    RiskLevel         int                    `json:"risk_level"`
    Parameters        map[string]interface{} `json:"parameters"`
    ApprovalsRequired int                    `json:"approvals_required"`
    RequestedAt       time.Time              `json:"requested_at"`
    Timeout           time.Duration          `json:"timeout"`
}

// requestHITLApproval 发送审批请求并等待结果
// 详见 15-HITL人机协作系统.md
func requestHITLApproval(ctx context.Context, req *ApprovalRequest) (bool, error) {
    // 1. 将审批请求写入 Redis 队列
    // 2. 通过企微 Bot 推送审批消息给值班人员
    // 3. 等待审批结果（polling Redis 或 channel）
    // 4. 超时则自动拒绝（fail-safe）
    // 具体实现见 15-HITL人机协作系统.md §审批状态机
    return false, fmt.Errorf("not implemented in this example")
}
```

> **WHY 超时自动拒绝（fail-safe）而不是超时自动通过？**
> - 对于高风险操作，安全的默认行为是"不执行"而非"执行"
> - 如果值班人员不在线，自动通过可能导致灾难性后果
> - 宁可一次误拒（Agent 重试或人工处理），也不要一次误批（服务中断）

### 2.3 JSON-RPC Handler

```go
// go/internal/protocol/handler.go
package protocol

import (
    "context"
    "encoding/json"
    "fmt"
)

type Handler struct {
    registry   *Registry
    middleware func(Tool, ToolHandler) ToolHandler  // 中间件链
}

func NewHandler(registry *Registry, mw func(Tool, ToolHandler) ToolHandler) *Handler {
    return &Handler{registry: registry, middleware: mw}
}

func (h *Handler) HandleRequest(ctx context.Context, raw []byte) ([]byte, error) {
    var req Request
    if err := json.Unmarshal(raw, &req); err != nil {
        return h.errorResponse(nil, -32700, "Parse error")
    }

    switch req.Method {
    case "tools/list":
        return h.handleToolsList(req)
    case "tools/call":
        return h.handleToolsCall(ctx, req)
    default:
        return h.errorResponse(req.ID, -32601, fmt.Sprintf("Method not found: %s", req.Method))
    }
}

func (h *Handler) handleToolsList(req Request) ([]byte, error) {
    defs := h.registry.ListDefinitions()
    return h.successResponse(req.ID, map[string]interface{}{"tools": defs})
}

func (h *Handler) handleToolsCall(ctx context.Context, req Request) ([]byte, error) {
    var params ToolCallParams
    if err := json.Unmarshal(req.Params, &params); err != nil {
        return h.errorResponse(req.ID, -32602, "Invalid params")
    }

    tool, err := h.registry.Get(params.Name)
    if err != nil {
        return h.errorResponse(req.ID, -32602, err.Error())
    }

    // 包装中间件链
    handler := func(ctx context.Context, p map[string]interface{}) (*ToolResult, error) {
        return tool.Execute(ctx, p)
    }
    if h.middleware != nil {
        handler = h.middleware(tool, handler)
    }

    result, err := handler(ctx, params.Arguments)
    if err != nil {
        return h.successResponse(req.ID, &ToolResult{
            Content: []ContentBlock{{Type: "text", Text: fmt.Sprintf("Error: %s", err)}},
            IsError: true,
        })
    }

    return h.successResponse(req.ID, result)
}

func (h *Handler) successResponse(id interface{}, result interface{}) ([]byte, error) {
    return json.Marshal(Response{JSONRPC: "2.0", ID: id, Result: result})
}

func (h *Handler) errorResponse(id interface{}, code int, msg string) ([]byte, error) {
    return json.Marshal(Response{JSONRPC: "2.0", ID: id, Error: &RPCError{Code: code, Message: msg}})
}
```

### 2.4 MCP 协议设计决策

> 本节解释 MCP Server 协议层的每一个关键选择——为什么这样做，不这样做会怎样。
> 这些决策在项目初期经过充分讨论，直接影响了系统的可维护性、性能和生态兼容性。

#### 2.4.1 WHY JSON-RPC 2.0 而不是 gRPC / REST

> **WHY** — MCP 协议规范（Model Context Protocol）原生定义在 JSON-RPC 2.0 之上。
> 我们遵循标准而非发明自己的协议，这意味着任何兼容 MCP 的 Client（Claude Desktop、Cursor、自研 Python Agent）
> 都可以零适配接入。如果我们选择 gRPC 或自定义 REST，就需要在 Client 侧写额外的协议转换层。

**三种方案对比：**

| 维度 | JSON-RPC 2.0 | gRPC | REST (OpenAPI) |
|------|-------------|------|----------------|
| **MCP 原生兼容** | ✅ 原生 | ❌ 需转换 | ❌ 需转换 |
| **Claude Desktop 集成** | ✅ 直接支持 | ❌ 不支持 | ❌ 不支持 |
| **Cursor/Windsurf 集成** | ✅ 直接支持 | ❌ 不支持 | ❌ 不支持 |
| **传输协议灵活性** | ✅ HTTP + stdio + SSE | ❌ 仅 HTTP/2 | ✅ HTTP |
| **类型安全** | ⚠️ JSON 弱类型 | ✅ Protobuf 强类型 | ⚠️ JSON Schema |
| **浏览器调试** | ✅ 直观 | ❌ 二进制 | ✅ 直观 |
| **Streaming 支持** | ✅ SSE / stdio | ✅ Server stream | ⚠️ 非标准 |
| **序列化性能** | ⚠️ 中等 | ✅ 高（Protobuf） | ⚠️ 中等 |
| **生态工具链** | ✅ MCP SDK | ✅ grpc-go | ✅ Swagger UI |
| **学习成本** | ✅ 低 | ⚠️ 中 | ✅ 低 |

> **WHY NOT gRPC?**
> - 最核心原因：MCP 协议是 JSON-RPC 2.0，gRPC 意味着我们要维护一个协议翻译层
> - gRPC 需要 HTTP/2，某些企业代理和防火墙对 HTTP/2 支持不完善
> - stdio 模式（Claude Desktop 本地集成必需）无法用 gRPC 实现
> - 大数据 API 的调用频率是每秒几十次而非几万次，Protobuf 的性能优势在这里可以忽略

> **WHY NOT REST?**
> - REST 没有标准化的"工具发现"机制（`tools/list`），需要自己实现 OpenAPI schema 到 MCP ToolDefinition 的映射
> - REST 的请求-响应模式与 JSON-RPC 的 batch 调用不兼容
> - MCP 生态（SDK、Inspector、测试工具）全部基于 JSON-RPC

> **WHY 不在内部用 gRPC + 外部用 JSON-RPC 桥接？**
> - 增加一层转换意味着增加一个故障点和延迟
> - 42 个工具的 Protobuf 定义维护成本不低——每新增一个工具就要改 `.proto` 文件
> - 我们的工具输入输出都是 `map[string]interface{}`，JSON 的动态类型反而是优势

```go
// 如果选择 gRPC，每个工具需要定义 proto（维护成本示例）：
//
// message HdfsNameNodeStatusRequest {
//     string namenode = 1;  // "nn1", "nn2", "active"
// }
// message HdfsNameNodeStatusResponse {
//     string hostname = 1;
//     string ha_state = 2;
//     int64 heap_used = 3;
//     ...  // 每个工具都要定义一套 message
// }
//
// 42 个工具 = 84 个 message 定义 + service 接口
// 而 JSON-RPC 只需要 ToolCallParams + ToolResult 两个通用类型

// 我们的实际做法：用 json.RawMessage 处理所有工具的参数
type ToolCallParams struct {
    Name      string                 `json:"name"`
    Arguments map[string]interface{} `json:"arguments"` // 通用，无需为每个工具定义类型
}
```

#### 2.4.2 WHY SSE（Server-Sent Events）而非 WebSocket 作为流式传输

> **WHY** — MCP 规范的 HTTP 传输模式使用 SSE 作为 server-to-client 的流式通道。
> 我们遵循规范选择 SSE，但也理解了这个选择背后的工程理由。

**SSE vs WebSocket 在工具调用场景的对比：**

| 维度 | SSE | WebSocket |
|------|-----|-----------|
| **方向** | 单向（Server→Client） | 双向 |
| **协议** | 标准 HTTP/1.1 | 独立协议（ws://） |
| **自动重连** | ✅ 浏览器原生 | ❌ 需要手动实现 |
| **负载均衡** | ✅ 标准 HTTP LB | ⚠️ 需要 sticky session |
| **企业防火墙** | ✅ 友好（HTTP） | ❌ 常被阻断 |
| **stdio 兼容** | ✅ 概念一致（单向流） | ❌ 不适用 |
| **MCP 适配** | ✅ 原生 | ❌ 需适配层 |

> **WHY NOT WebSocket?**
> - MCP 的通信模式本质是请求-响应，不需要 Client→Server 的推送通道
> - 工具调用结果可能很大（如日志搜索返回 100 行），SSE 的流式下发正好适合
> - SSE 走标准 HTTP，企业内部的 API Gateway（如 Kong、Nginx）不需要特殊配置
> - WebSocket 的双向能力在我们的场景中完全用不上——Agent 不需要"被服务器主动推送"
> - 如果未来需要 Server 推送（如实时告警），可以加一个独立的 WebSocket 通道，不影响工具调用链路

```go
// SSE Handler 实现（用于流式返回长时间运行的工具结果）
func (h *Handler) handleSSE(w http.ResponseWriter, r *http.Request) {
    w.Header().Set("Content-Type", "text/event-stream")
    w.Header().Set("Cache-Control", "no-cache")
    w.Header().Set("Connection", "keep-alive")

    flusher, ok := w.(http.Flusher)
    if !ok {
        http.Error(w, "SSE not supported", http.StatusInternalServerError)
        return
    }

    // 发送工具调用进度
    for progress := range progressChan {
        fmt.Fprintf(w, "event: progress\ndata: %s\n\n", progress)
        flusher.Flush()
    }

    // 发送最终结果
    fmt.Fprintf(w, "event: result\ndata: %s\n\n", finalResult)
    flusher.Flush()
}
```

#### 2.4.3 WHY 工具描述放在 Server 侧而非 Client 侧

> **WHY** — 工具的 Description 和 Schema 由 MCP Server 管理、通过 `tools/list` 动态返回。
> 而不是在 Python Agent 侧硬编码工具描述。

**这个决策的核心收益：**

1. **单一信息源（Single Source of Truth）**：工具描述和实现在同一个 Go 文件中，不会出现描述和实现不一致的问题
2. **动态发现**：Agent 启动时通过 `tools/list` 获取当前可用工具，而非编译时确定。新增工具只需部署 MCP Server，Agent 无需重新部署
3. **描述优化闭环**：当我们发现 Agent 对某个工具的理解不准确时，只需修改 Server 侧的 Description，不需要改 Python 代码

> **WHY NOT Client 侧管理描述？**
> - 42 个工具 × 每个工具的 Description + Schema ≈ 1500 行 Python 代码专门用于工具定义
> - 每次 Go Server 修改了工具参数，Python 侧也要同步修改——两份代码两份维护
> - 不同 Agent（分诊 Agent、诊断 Agent）看到的工具描述应该一致，Server 侧管理保证了一致性

```go
// 示例：工具描述和实现在同一个文件中（namenode_status.go）
// Agent 通过 tools/list 获取描述，永远和实现保持同步

func (t *NameNodeStatusTool) Description() string {
    // 这段描述直接被 LLM 读取来决定是否调用这个工具
    // 修改这里 = 立即影响 Agent 行为，无需重新部署 Python 侧
    return `获取 HDFS NameNode 的详细状态信息。
返回：HA 状态、JVM 堆内存、SafeMode、RPC 队列、容量、副本不足/损坏块数。
使用场景：HDFS 延迟升高、写入失败、NN 告警时首先检查。`
}

func (t *NameNodeStatusTool) Schema() map[string]interface{} {
    // InputSchema 也由 Server 侧管理
    // Agent 通过 tools/list 获取，自动知道这个工具接受什么参数
    return map[string]interface{}{
        "type": "object",
        "properties": map[string]interface{}{
            "namenode": map[string]interface{}{
                "type": "string", "description": "NN 标识", 
                "enum": []string{"nn1", "nn2", "active"}, 
                "default": "active",
            },
        },
    }
}
```

#### 2.4.4 WHY 用 `map[string]interface{}` 而不是强类型参数

> **WHY** — MCP 协议的 `tools/call` 参数是 JSON 动态类型。42 个工具的参数结构各不相同，
> 用 `map[string]interface{}` 作为通用入口，在每个工具的 `Execute` 方法内部做类型断言和校验。

**如果用强类型怎么样？**

```go
// 方案 A：强类型（需要为每个工具定义结构体）
type NameNodeStatusParams struct {
    NameNode string `json:"namenode" validate:"oneof=nn1 nn2 active"`
}

type ConsumerLagParams struct {
    ConsumerGroup string `json:"consumer_group"`
    TopN          int    `json:"top_n" validate:"min=1,max=100"`
}

// 42 个工具 = 42 个 Params 结构体
// 优点：编译时类型检查
// 缺点：大量样板代码、新增工具成本高

// 方案 B：map[string]interface{}（我们的选择）
func (t *NameNodeStatusTool) Execute(ctx context.Context, params map[string]interface{}) (*ToolResult, error) {
    target := "active"
    if v, ok := params["namenode"].(string); ok {
        target = v
    }
    // ...
}
// 优点：灵活、新增工具零样板代码
// 缺点：运行时类型断言，需要在 Schema() 中定义 JSON Schema 来保证输入合法
```

> **我们的折中**：用 `map[string]interface{}` 保持灵活性，但通过 `Schema()` 返回 JSON Schema 让 MCP Client（和 LLM）知道合法参数。
> 参数校验由 MCP 中间件统一完成（见 `10-MCP中间件链.md` §输入校验中间件）。

---

## 3. 工具实现示例

### 3.1 HDFS NameNode Status（完整示例）

```go
// go/internal/tools/hdfs/namenode_status.go
package hdfs

import (
    "context"
    "fmt"
    "strings"

    "go.opentelemetry.io/otel/attribute"
    "go.opentelemetry.io/otel/trace"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

type NameNodeStatusTool struct {
    client  HDFSClient
    tracer  trace.Tracer
}

func NewNameNodeStatusTool(client HDFSClient) *NameNodeStatusTool {
    return &NameNodeStatusTool{client: client}
}

func (t *NameNodeStatusTool) Name() string        { return "hdfs_namenode_status" }
func (t *NameNodeStatusTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }

func (t *NameNodeStatusTool) Description() string {
    return `获取 HDFS NameNode 的详细状态信息。
返回：HA 状态、JVM 堆内存、SafeMode、RPC 队列、容量、副本不足/损坏块数。
使用场景：HDFS 延迟升高、写入失败、NN 告警时首先检查。`
}

func (t *NameNodeStatusTool) Schema() map[string]interface{} {
    return map[string]interface{}{
        "type": "object",
        "properties": map[string]interface{}{
            "namenode": map[string]interface{}{
                "type": "string", "description": "NN 标识", "enum": []string{"nn1", "nn2", "active"}, "default": "active",
            },
        },
    }
}

func (t *NameNodeStatusTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
    target := "active"
    if v, ok := params["namenode"].(string); ok {
        target = v
    }

    status, err := t.client.GetNameNodeStatus(ctx, target)
    if err != nil {
        return nil, fmt.Errorf("get NN status: %w", err)
    }

    // 构建结构化文本输出
    var sb strings.Builder
    sb.WriteString(fmt.Sprintf("## HDFS NameNode 状态 (%s)\n\n", status.Hostname))

    // HA
    haEmoji := map[string]string{"active": "🟢", "standby": "🟡"}
    sb.WriteString(fmt.Sprintf("**HA 状态**: %s %s\n", haEmoji[status.HAState], status.HAState))

    // 堆内存
    heapPct := float64(status.HeapUsed) / float64(status.HeapMax) * 100
    heapE := "🟢"
    if heapPct > 90 { heapE = "🔴" } else if heapPct > 80 { heapE = "🟡" }
    sb.WriteString(fmt.Sprintf("**堆内存**: %s %.1f%% (%s / %s)\n", heapE, heapPct,
        formatBytes(status.HeapUsed), formatBytes(status.HeapMax)))

    // SafeMode
    if status.SafeMode {
        sb.WriteString(fmt.Sprintf("**SafeMode**: 🚨 是 (原因: %s)\n", status.SafeModeReason))
    } else {
        sb.WriteString("**SafeMode**: 🟢 否\n")
    }

    // RPC
    sb.WriteString(fmt.Sprintf("**RPC 队列**: %d (延迟 %.1fms)\n", status.RPCQueueLen, status.RPCLatencyMs))

    // 块状态
    sb.WriteString(fmt.Sprintf("**副本不足块**: %d\n", status.UnderReplicatedBlocks))
    sb.WriteString(fmt.Sprintf("**损坏块**: %d\n", status.CorruptBlocks))
    sb.WriteString(fmt.Sprintf("**丢失块**: %d\n", status.MissingBlocks))

    // 异常检测
    var alerts []string
    if heapPct > 85 { alerts = append(alerts, fmt.Sprintf("⚠️ 堆内存 %.1f%% 超过 85%% 警戒线", heapPct)) }
    if status.SafeMode { alerts = append(alerts, "🚨 NameNode 处于 SafeMode") }
    if status.CorruptBlocks > 0 { alerts = append(alerts, fmt.Sprintf("🚨 %d 个损坏块", status.CorruptBlocks)) }
    if status.MissingBlocks > 0 { alerts = append(alerts, fmt.Sprintf("🚨 %d 个丢失块", status.MissingBlocks)) }
    if status.RPCQueueLen > 50 { alerts = append(alerts, fmt.Sprintf("⚠️ RPC 队列 %d 超过正常范围", status.RPCQueueLen)) }
    if status.RPCLatencyMs > 100 { alerts = append(alerts, fmt.Sprintf("⚠️ RPC 延迟 %.1fms 偏高", status.RPCLatencyMs)) }

    if len(alerts) > 0 {
        sb.WriteString("\n### ⚠️ 异常检测\n")
        for _, a := range alerts { sb.WriteString(fmt.Sprintf("- %s\n", a)) }
    }

    return &protocol.ToolResult{
        Content: []protocol.ContentBlock{{Type: "text", Text: sb.String()}},
    }, nil
}
```

### 3.2 HDFS Client 封装

```go
// go/internal/tools/hdfs/client.go
package hdfs

import (
    "context"
    "encoding/json"
    "fmt"
    "net/http"
    "time"
)

// HDFSClient HDFS WebHDFS API 客户端
type HDFSClient interface {
    GetNameNodeStatus(ctx context.Context, target string) (*NameNodeStatus, error)
    GetClusterOverview(ctx context.Context) (*ClusterOverview, error)
    GetDataNodes(ctx context.Context) ([]DataNodeInfo, error)
    GetBlockReport(ctx context.Context) (*BlockReport, error)
}

// NameNodeStatus NameNode 状态信息
type NameNodeStatus struct {
    Hostname             string  `json:"hostname"`
    HAState              string  `json:"haState"`              // active | standby
    HeapUsed             int64   `json:"heapUsed"`
    HeapMax              int64   `json:"heapMax"`
    HeapCommitted        int64   `json:"heapCommitted"`
    SafeMode             bool    `json:"safeMode"`
    SafeModeReason       string  `json:"safeModeReason"`
    RPCQueueLen          int     `json:"rpcQueueLen"`
    RPCLatencyMs         float64 `json:"rpcLatencyMs"`
    UnderReplicatedBlocks int    `json:"underReplicatedBlocks"`
    CorruptBlocks        int     `json:"corruptBlocks"`
    MissingBlocks        int     `json:"missingBlocks"`
    TotalFiles           int64   `json:"totalFiles"`
    TotalBlocks          int64   `json:"totalBlocks"`
    CapacityTotal        int64   `json:"capacityTotal"`
    CapacityUsed         int64   `json:"capacityUsed"`
    CapacityRemaining    int64   `json:"capacityRemaining"`
    UpTime               string  `json:"upTime"`
}

// ClusterOverview HDFS 集群概览
type ClusterOverview struct {
    CapacityTotal     int64 `json:"capacityTotal"`
    CapacityUsed      int64 `json:"capacityUsed"`
    CapacityRemaining int64 `json:"capacityRemaining"`
    TotalNodes        int   `json:"totalNodes"`
    LiveNodes         int   `json:"liveNodes"`
    DeadNodes         int   `json:"deadNodes"`
    DecommNodes       int   `json:"decommNodes"`
    TotalFiles        int64 `json:"totalFiles"`
    TotalBlocks       int64 `json:"totalBlocks"`
}

type hdfsClientImpl struct {
    baseURL    string
    httpClient *http.Client
}

func NewClient(baseURL string) HDFSClient {
    return &hdfsClientImpl{
        baseURL: baseURL,
        httpClient: &http.Client{
            Timeout: 10 * time.Second,
        },
    }
}

func (c *hdfsClientImpl) GetNameNodeStatus(ctx context.Context, target string) (*NameNodeStatus, error) {
    // 查询 JMX 接口
    url := fmt.Sprintf("%s/jmx?qry=Hadoop:service=NameNode,name=*", c.baseURL)

    req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
    if err != nil {
        return nil, fmt.Errorf("create request: %w", err)
    }

    resp, err := c.httpClient.Do(req)
    if err != nil {
        return nil, fmt.Errorf("fetch JMX: %w", err)
    }
    defer resp.Body.Close()

    if resp.StatusCode != http.StatusOK {
        return nil, fmt.Errorf("JMX returned %d", resp.StatusCode)
    }

    var jmxData struct {
        Beans []map[string]interface{} `json:"beans"`
    }
    if err := json.NewDecoder(resp.Body).Decode(&jmxData); err != nil {
        return nil, fmt.Errorf("decode JMX: %w", err)
    }

    // 从 JMX beans 中提取状态信息
    status := &NameNodeStatus{}
    for _, bean := range jmxData.Beans {
        name, _ := bean["name"].(string)
        switch {
        case name == "Hadoop:service=NameNode,name=FSNamesystem":
            status.TotalFiles = int64(getFloat(bean, "FilesTotal"))
            status.TotalBlocks = int64(getFloat(bean, "BlocksTotal"))
            status.UnderReplicatedBlocks = int(getFloat(bean, "UnderReplicatedBlocks"))
            status.CorruptBlocks = int(getFloat(bean, "CorruptBlocks"))
            status.MissingBlocks = int(getFloat(bean, "MissingBlocks"))
            status.CapacityTotal = int64(getFloat(bean, "CapacityTotal"))
            status.CapacityUsed = int64(getFloat(bean, "CapacityUsed"))
            status.CapacityRemaining = int64(getFloat(bean, "CapacityRemaining"))
        case name == "Hadoop:service=NameNode,name=JvmMetrics":
            status.HeapUsed = int64(getFloat(bean, "MemHeapUsedM") * 1024 * 1024)
            status.HeapMax = int64(getFloat(bean, "MemHeapMaxM") * 1024 * 1024)
        case name == "Hadoop:service=NameNode,name=RpcActivityForPort8020":
            status.RPCQueueLen = int(getFloat(bean, "CallQueueLength"))
            status.RPCLatencyMs = getFloat(bean, "RpcProcessingTimeAvgTime")
        }
    }

    return status, nil
}

func getFloat(m map[string]interface{}, key string) float64 {
    if v, ok := m[key].(float64); ok {
        return v
    }
    return 0
}

func formatBytes(b int64) string {
    const unit = 1024
    if b < unit { return fmt.Sprintf("%d B", b) }
    div, exp := int64(unit), 0
    for n := b / unit; n >= unit; n /= unit { div *= unit; exp++ }
    return fmt.Sprintf("%.1f %cB", float64(b)/float64(div), "KMGTPE"[exp])
}
```

### 3.3 Kafka Consumer Lag 工具

```go
// go/internal/tools/kafka/consumer_lag.go
package kafka

import (
    "context"
    "fmt"
    "sort"
    "strings"

    "github.com/IBM/sarama"
    "github.com/yourorg/aiops-mcp/internal/protocol"
)

type ConsumerLagTool struct {
    admin sarama.ClusterAdmin
}

func NewConsumerLagTool(brokers []string) (*ConsumerLagTool, error) {
    config := sarama.NewConfig()
    config.Version = sarama.V3_5_0_0
    admin, err := sarama.NewClusterAdmin(brokers, config)
    if err != nil {
        return nil, fmt.Errorf("create kafka admin: %w", err)
    }
    return &ConsumerLagTool{admin: admin}, nil
}

func (t *ConsumerLagTool) Name() string                       { return "kafka_consumer_lag" }
func (t *ConsumerLagTool) RiskLevel() protocol.RiskLevel      { return protocol.RiskNone }
func (t *ConsumerLagTool) Description() string {
    return `获取 Kafka 消费者组的 Lag 详情。
返回：各分区的 committed offset / latest offset / lag。
使用场景：消费延迟告警、消费者组异常排查。`
}

func (t *ConsumerLagTool) Schema() map[string]interface{} {
    return map[string]interface{}{
        "type": "object",
        "properties": map[string]interface{}{
            "consumer_group": map[string]interface{}{
                "type": "string", "description": "消费者组名称（留空列出所有组）",
            },
            "top_n": map[string]interface{}{
                "type": "integer", "description": "只显示 lag 最大的 N 个组", "default": 10,
            },
        },
    }
}

func (t *ConsumerLagTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
    groupName, _ := params["consumer_group"].(string)
    topN := 10
    if v, ok := params["top_n"].(float64); ok {
        topN = int(v)
    }

    var groups []string
    if groupName != "" {
        groups = []string{groupName}
    } else {
        // 列出所有消费者组
        listed, err := t.admin.ListConsumerGroups()
        if err != nil {
            return nil, fmt.Errorf("list groups: %w", err)
        }
        for g := range listed {
            groups = append(groups, g)
        }
        sort.Strings(groups)
    }

    type groupLag struct {
        Name     string
        TotalLag int64
        Detail   string
    }
    var results []groupLag

    for _, g := range groups {
        offsets, err := t.admin.ListConsumerGroupOffsets(g, nil)
        if err != nil {
            continue
        }

        var totalLag int64
        var details []string

        for topic, partitions := range offsets.Blocks {
            for partition, block := range partitions {
                // 获取 latest offset
                latest, err := t.admin.GetOffset(topic, partition, sarama.OffsetNewest)
                if err != nil {
                    continue
                }
                lag := latest - block.Offset
                if lag < 0 {
                    lag = 0
                }
                totalLag += lag

                if lag > 0 {
                    details = append(details, fmt.Sprintf("  %s[%d]: committed=%d, latest=%d, lag=%d",
                        topic, partition, block.Offset, latest, lag))
                }
            }
        }

        results = append(results, groupLag{Name: g, TotalLag: totalLag, Detail: strings.Join(details, "\n")})
    }

    // 按 lag 降序排序
    sort.Slice(results, func(i, j int) bool { return results[i].TotalLag > results[j].TotalLag })
    if len(results) > topN {
        results = results[:topN]
    }

    // 格式化输出
    var sb strings.Builder
    sb.WriteString("## Kafka Consumer Lag\n\n")

    for _, r := range results {
        lagEmoji := "🟢"
        if r.TotalLag > 1000000 { lagEmoji = "🔴" } else if r.TotalLag > 100000 { lagEmoji = "🟡" }
        sb.WriteString(fmt.Sprintf("### %s %s (total lag: %d)\n", lagEmoji, r.Name, r.TotalLag))
        if r.Detail != "" {
            sb.WriteString(r.Detail)
            sb.WriteString("\n")
        }
        sb.WriteString("\n")
    }

    return &protocol.ToolResult{
        Content: []protocol.ContentBlock{{Type: "text", Text: sb.String()}},
    }, nil
}
```

### 3.4 Ops 高风险工具示例

```go
// go/internal/tools/ops/restart_service.go
package ops

import (
    "context"
    "fmt"
    "time"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

type RestartServiceTool struct {
    client OpsClient
}

func (t *RestartServiceTool) Name() string                       { return "ops_restart_service" }
func (t *RestartServiceTool) RiskLevel() protocol.RiskLevel      { return protocol.RiskHigh } // ⚠️ 高风险

func (t *RestartServiceTool) Description() string {
    return `重启指定大数据组件服务。
⚠️ 高风险操作：需要 HITL 审批。
支持 rolling（滚动重启，不影响服务）和 force（强制重启，有短暂不可用）。`
}

func (t *RestartServiceTool) Schema() map[string]interface{} {
    return map[string]interface{}{
        "type": "object",
        "required": []string{"service", "mode"},
        "properties": map[string]interface{}{
            "service": map[string]interface{}{
                "type": "string", "description": "服务名", "enum": []string{
                    "hdfs-namenode", "hdfs-datanode", "yarn-resourcemanager",
                    "yarn-nodemanager", "kafka-broker", "es-node", "zookeeper",
                },
            },
            "mode": map[string]interface{}{
                "type": "string", "description": "重启方式", "enum": []string{"rolling", "force"},
            },
            "target_nodes": map[string]interface{}{
                "type": "array", "items": map[string]interface{}{"type": "string"},
                "description": "目标节点列表（空=全部）",
            },
            "reason": map[string]interface{}{
                "type": "string", "description": "重启原因（用于审计）",
            },
        },
    }
}

// Execute 会被 RiskAssessmentMiddleware 拦截 → HITL 审批
func (t *RestartServiceTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
    service, _ := params["service"].(string)
    mode, _ := params["mode"].(string)
    reason, _ := params["reason"].(string)

    // 执行前置检查
    if err := t.client.PreflightCheck(ctx, service); err != nil {
        return &protocol.ToolResult{
            Content: []protocol.ContentBlock{{
                Type: "text",
                Text: fmt.Sprintf("❌ 前置检查失败：%s\n建议先解决上述问题再重启。", err),
            }},
            IsError: true,
        }, nil
    }

    // 创建操作快照（用于回滚）
    snapshotID, err := t.client.CreateSnapshot(ctx, service)
    if err != nil {
        return nil, fmt.Errorf("create snapshot: %w", err)
    }

    // 执行重启
    startTime := time.Now()
    result, err := t.client.RestartService(ctx, service, mode, params)
    if err != nil {
        return &protocol.ToolResult{
            Content: []protocol.ContentBlock{{
                Type: "text",
                Text: fmt.Sprintf("❌ 重启失败：%s\n快照ID：%s（可用于回滚）", err, snapshotID),
            }},
            IsError: true,
        }, nil
    }

    elapsed := time.Since(startTime)

    return &protocol.ToolResult{
        Content: []protocol.ContentBlock{{
            Type: "text",
            Text: fmt.Sprintf(
                "✅ 服务 %s 已 %s 重启完成\n"+
                    "- 耗时: %s\n"+
                    "- 影响节点: %d\n"+
                    "- 快照ID: %s\n"+
                    "- 原因: %s\n"+
                    "- 状态: %s",
                service, mode, elapsed, result.AffectedNodes, snapshotID, reason, result.Status,
            ),
        }},
    }, nil
}
```

> **🔧 工程难点：高风险操作工具的安全拦截与审批集成——ops-mcp 的"执行前必须审批"机制**
>
> **挑战**：42 个工具中有 5 个属于 ops-mcp（`restart_service`、`scale_resource`、`failover_namenode` 等），这些工具可以修改生产环境——重启服务、扩缩容、触发 NameNode Failover。如果 LLM 在诊断过程中"自作主张"调用了 `restart_service`（GPT-4 在 Agent 场景下有 ~5% 的概率未经请求就调用修改类工具），就可能导致灾难性后果。但简单地"不注册 ops-mcp 工具"又不行——修复建议（Remediation）需要知道有哪些可用的修复操作来生成方案。核心矛盾是：**LLM 需要知道这些工具存在（用于规划），但不能自由调用（需要人工审批）**。同时，ops-mcp 的安全策略比只读工具严格得多——需要独立的 RBAC、独立的审计日志、独立的网络策略，和只读工具混在同一个 Server 中不合适。
>
> **解决方案**：ops-mcp 作为独立的 MCP Server 部署（§9.3 WHY 独立 Server），有独立的 K8s NetworkPolicy（只允许 MCP Gateway 访问）和 ServiceAccount（最小权限原则）。每个 ops 工具在 `ToolDefinition` 中标注 `risk_level: "high"` 或 `"critical"`，MCP 中间件链（§10）中的 `RiskCheckMiddleware` 在工具调用到达 Handler 之前拦截——`risk_level >= high` 的调用自动触发 HITL 审批流程（§15）：将操作详情（工具名、参数、影响范围、当前操作者）推送到企微 Bot 并写入 Redis 审批队列，等待值班人员确认。超时自动拒绝（fail-safe 原则——"超时不执行"比"超时自动执行"安全得多）。审批通过后，工具执行前创建 `CheckpointManager` 快照（记录操作前的组件状态），执行后通过 `_verify_step()` 验证操作是否成功（如重启后服务是否恢复健康），失败则基于快照自动回滚。所有 ops 工具调用（无论成功/失败/被拦截）都写入独立的审计日志表（PostgreSQL `ops_audit_log`），包含操作者、审批人、参数、结果、耗时，用于事后审计和合规检查。`tools/list` 响应中 ops 工具的 `description` 特别标注"⚠️ 此工具需要人工审批才能执行"，引导 LLM 在生成修复方案时将其标记为"建议操作"而非"已执行操作"。

### 3.5 Metrics Query 工具

```go
// go/internal/tools/metrics/query.go
package metrics

import (
    "context"
    "encoding/json"
    "fmt"
    "net/http"
    "net/url"
    "strings"
    "time"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

type QueryMetricsTool struct {
    promURL    string
    httpClient *http.Client
}

func NewQueryMetricsTool(promURL string) *QueryMetricsTool {
    return &QueryMetricsTool{
        promURL:    promURL,
        httpClient: &http.Client{Timeout: 15 * time.Second},
    }
}

func (t *QueryMetricsTool) Name() string                  { return "query_metrics" }
func (t *QueryMetricsTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *QueryMetricsTool) Description() string {
    return `执行 PromQL 查询获取指标数据。
支持 instant query 和 range query。
使用场景：获取 CPU/内存/磁盘/网络等任何 Prometheus 指标。`
}

func (t *QueryMetricsTool) Schema() map[string]interface{} {
    return map[string]interface{}{
        "type": "object",
        "required": []string{"query"},
        "properties": map[string]interface{}{
            "query": map[string]interface{}{
                "type": "string", "description": "PromQL 查询表达式",
            },
            "time_range": map[string]interface{}{
                "type": "string", "description": "时间范围（如 '1h', '30m', '24h'）", "default": "instant",
            },
            "step": map[string]interface{}{
                "type": "string", "description": "Range query 步长（如 '1m', '5m'）", "default": "1m",
            },
        },
    }
}

func (t *QueryMetricsTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
    query, _ := params["query"].(string)
    timeRange, _ := params["time_range"].(string)

    if timeRange == "" || timeRange == "instant" {
        return t.instantQuery(ctx, query)
    }
    return t.rangeQuery(ctx, query, timeRange, params)
}

func (t *QueryMetricsTool) instantQuery(ctx context.Context, query string) (*protocol.ToolResult, error) {
    apiURL := fmt.Sprintf("%s/api/v1/query?query=%s", t.promURL, url.QueryEscape(query))

    req, _ := http.NewRequestWithContext(ctx, "GET", apiURL, nil)
    resp, err := t.httpClient.Do(req)
    if err != nil {
        return nil, fmt.Errorf("prometheus query failed: %w", err)
    }
    defer resp.Body.Close()

    var result struct {
        Data struct {
            ResultType string `json:"resultType"`
            Result     []struct {
                Metric map[string]string `json:"metric"`
                Value  []interface{}     `json:"value"`
            } `json:"result"`
        } `json:"data"`
    }

    if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
        return nil, fmt.Errorf("decode response: %w", err)
    }

    var sb strings.Builder
    sb.WriteString(fmt.Sprintf("## PromQL: `%s`\n\n", query))
    sb.WriteString(fmt.Sprintf("结果类型: %s, 数据点: %d\n\n", result.Data.ResultType, len(result.Data.Result)))

    for i, r := range result.Data.Result {
        if i >= 20 { // 限制输出
            sb.WriteString(fmt.Sprintf("... 还有 %d 条结果\n", len(result.Data.Result)-20))
            break
        }
        labels := formatLabels(r.Metric)
        value := fmt.Sprintf("%v", r.Value[1])
        sb.WriteString(fmt.Sprintf("- %s: **%s**\n", labels, value))
    }

    return &protocol.ToolResult{
        Content: []protocol.ContentBlock{{Type: "text", Text: sb.String()}},
    }, nil
}

func formatLabels(m map[string]string) string {
    parts := make([]string, 0, len(m))
    for k, v := range m {
        if k == "__name__" { continue }
        parts = append(parts, fmt.Sprintf("%s=%q", k, v))
    }
    return "{" + strings.Join(parts, ", ") + "}"
}
```

### 3.6 ES Cluster Health 工具

> **WHY 需要这个工具** — ES 集群健康是运维中最高频的检查之一。当告警触发"ES 集群 yellow/red"时，
> Diagnostic Agent 需要快速获取集群整体状态、unassigned shards 原因、节点分布等信息。
> 这个工具将 ES 的 `_cluster/health`、`_cluster/stats`、`_cat/allocation` 三个 API 的结果
> 整合为一个结构化的诊断视图。

> **WHY 输出格式设计** — 我们使用 Markdown 文本而非 JSON 输出，因为：
> 1. LLM 理解 Markdown 比 JSON 更准确（减少 token 浪费在解析 JSON 结构上）
> 2. 带 emoji 的状态标注让 LLM 能快速识别异常（🔴 = 需要关注）
> 3. 输出会被嵌入到 LLM 的 context 中，Markdown 的信息密度比 JSON 高

```go
// go/internal/tools/es/cluster_health.go
package es

import (
    "context"
    "encoding/json"
    "fmt"
    "net/http"
    "strings"
    "time"

    "github.com/rs/zerolog/log"
    "go.opentelemetry.io/otel/attribute"
    "go.opentelemetry.io/otel/trace"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

// ESClient Elasticsearch REST API 客户端
type ESClient interface {
    ClusterHealth(ctx context.Context) (*ClusterHealth, error)
    ClusterStats(ctx context.Context) (*ClusterStats, error)
    CatAllocation(ctx context.Context) ([]AllocationInfo, error)
    CatIndices(ctx context.Context, pattern string) ([]IndexInfo, error)
    SearchLogs(ctx context.Context, req *SearchRequest) (*SearchResponse, error)
}

// ClusterHealth ES _cluster/health 响应
type ClusterHealth struct {
    ClusterName                 string  `json:"cluster_name"`
    Status                      string  `json:"status"`  // green | yellow | red
    NumberOfNodes               int     `json:"number_of_nodes"`
    NumberOfDataNodes           int     `json:"number_of_data_nodes"`
    ActivePrimaryShards         int     `json:"active_primary_shards"`
    ActiveShards                int     `json:"active_shards"`
    RelocatingShards            int     `json:"relocating_shards"`
    InitializingShards          int     `json:"initializing_shards"`
    UnassignedShards            int     `json:"unassigned_shards"`
    DelayedUnassignedShards     int     `json:"delayed_unassigned_shards"`
    NumberOfPendingTasks        int     `json:"number_of_pending_tasks"`
    NumberOfInFlightFetch       int     `json:"number_of_in_flight_fetch"`
    TaskMaxWaitingInQueueMillis int64   `json:"task_max_waiting_in_queue_millis"`
    ActiveShardsPercentAsNumber float64 `json:"active_shards_percent_as_number"`
}

// ClusterStats ES _cluster/stats 摘要
type ClusterStats struct {
    Indices struct {
        Count  int   `json:"count"`
        Shards struct {
            Total      int     `json:"total"`
            Primaries  int     `json:"primaries"`
            Replication float64 `json:"replication"`
        } `json:"shards"`
        Docs struct {
            Count   int64 `json:"count"`
            Deleted int64 `json:"deleted"`
        } `json:"docs"`
        Store struct {
            SizeInBytes int64 `json:"size_in_bytes"`
        } `json:"store"`
    } `json:"indices"`
    Nodes struct {
        Total      int `json:"total"`
        Successful int `json:"successful"`
        Failed     int `json:"failed"`
    } `json:"_nodes"`
}

// AllocationInfo 节点磁盘分配
type AllocationInfo struct {
    Node       string `json:"node"`
    Shards     int    `json:"shards"`
    DiskUsed   string `json:"disk.used"`
    DiskAvail  string `json:"disk.avail"`
    DiskTotal  string `json:"disk.total"`
    DiskPercent string `json:"disk.percent"`
}

type ClusterHealthTool struct {
    client ESClient
    tracer trace.Tracer
}

func NewClusterHealthTool(client ESClient) *ClusterHealthTool {
    return &ClusterHealthTool{client: client}
}

func (t *ClusterHealthTool) Name() string                       { return "es_cluster_health" }
func (t *ClusterHealthTool) RiskLevel() protocol.RiskLevel      { return protocol.RiskNone }

func (t *ClusterHealthTool) Description() string {
    return `获取 Elasticsearch 集群的综合健康状态。
返回：集群颜色（green/yellow/red）、节点数、分片状态、磁盘分配、pending tasks。
使用场景：ES 告警、搜索/写入异常、集群 yellow/red 时首先检查。
输出包含异常检测，自动标注需要关注的指标。`
}

func (t *ClusterHealthTool) Schema() map[string]interface{} {
    return map[string]interface{}{
        "type": "object",
        "properties": map[string]interface{}{
            "include_allocation": map[string]interface{}{
                "type": "boolean", "description": "是否包含节点磁盘分配信息", "default": true,
            },
        },
    }
}

func (t *ClusterHealthTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
    includeAlloc := true
    if v, ok := params["include_allocation"].(bool); ok {
        includeAlloc = v
    }

    // 并行获取 health 和 stats
    healthCh := make(chan *ClusterHealth, 1)
    statsCh := make(chan *ClusterStats, 1)
    allocCh := make(chan []AllocationInfo, 1)
    errCh := make(chan error, 3)

    go func() {
        h, err := t.client.ClusterHealth(ctx)
        if err != nil {
            errCh <- fmt.Errorf("cluster health: %w", err)
            return
        }
        healthCh <- h
    }()

    go func() {
        s, err := t.client.ClusterStats(ctx)
        if err != nil {
            errCh <- fmt.Errorf("cluster stats: %w", err)
            return
        }
        statsCh <- s
    }()

    if includeAlloc {
        go func() {
            a, err := t.client.CatAllocation(ctx)
            if err != nil {
                errCh <- fmt.Errorf("cat allocation: %w", err)
                return
            }
            allocCh <- a
        }()
    }

    // 收集结果
    var health *ClusterHealth
    var stats *ClusterStats
    var alloc []AllocationInfo

    for i := 0; i < 2; i++ {
        select {
        case h := <-healthCh:
            health = h
        case s := <-statsCh:
            stats = s
        case err := <-errCh:
            return nil, err
        case <-ctx.Done():
            return nil, ctx.Err()
        }
    }

    if includeAlloc {
        select {
        case a := <-allocCh:
            alloc = a
        case err := <-errCh:
            log.Warn().Err(err).Msg("failed to get allocation, continuing without it")
        case <-ctx.Done():
            return nil, ctx.Err()
        }
    }

    // 构建输出
    var sb strings.Builder
    statusEmoji := map[string]string{"green": "🟢", "yellow": "🟡", "red": "🔴"}
    sb.WriteString(fmt.Sprintf("## ES 集群健康状态 (%s)\n\n", health.ClusterName))
    sb.WriteString(fmt.Sprintf("**状态**: %s %s\n", statusEmoji[health.Status], strings.ToUpper(health.Status)))
    sb.WriteString(fmt.Sprintf("**节点**: %d 总 / %d 数据节点\n", health.NumberOfNodes, health.NumberOfDataNodes))
    sb.WriteString(fmt.Sprintf("**活跃分片比例**: %.1f%%\n\n", health.ActiveShardsPercentAsNumber))

    // 分片状态
    sb.WriteString("### 分片状态\n")
    sb.WriteString(fmt.Sprintf("- 活跃主分片: %d\n", health.ActivePrimaryShards))
    sb.WriteString(fmt.Sprintf("- 活跃总分片: %d\n", health.ActiveShards))
    if health.UnassignedShards > 0 {
        sb.WriteString(fmt.Sprintf("- 🔴 未分配分片: %d\n", health.UnassignedShards))
    }
    if health.RelocatingShards > 0 {
        sb.WriteString(fmt.Sprintf("- 🟡 迁移中分片: %d\n", health.RelocatingShards))
    }
    if health.InitializingShards > 0 {
        sb.WriteString(fmt.Sprintf("- 🟡 初始化分片: %d\n", health.InitializingShards))
    }

    // 集群统计
    if stats != nil {
        sb.WriteString(fmt.Sprintf("\n### 集群统计\n"))
        sb.WriteString(fmt.Sprintf("- 索引数: %d\n", stats.Indices.Count))
        sb.WriteString(fmt.Sprintf("- 文档数: %d\n", stats.Indices.Docs.Count))
        sb.WriteString(fmt.Sprintf("- 存储大小: %s\n", formatBytes(stats.Indices.Store.SizeInBytes)))
        sb.WriteString(fmt.Sprintf("- Pending Tasks: %d (最长等待 %dms)\n",
            health.NumberOfPendingTasks, health.TaskMaxWaitingInQueueMillis))
    }

    // 磁盘分配
    if len(alloc) > 0 {
        sb.WriteString("\n### 节点磁盘分配\n")
        sb.WriteString("| 节点 | 分片数 | 已用 | 可用 | 总量 | 使用率 |\n")
        sb.WriteString("|------|--------|------|------|------|--------|\n")
        for _, a := range alloc {
            sb.WriteString(fmt.Sprintf("| %s | %d | %s | %s | %s | %s |\n",
                a.Node, a.Shards, a.DiskUsed, a.DiskAvail, a.DiskTotal, a.DiskPercent))
        }
    }

    // 异常检测
    var alerts []string
    if health.Status == "red" {
        alerts = append(alerts, "🚨 集群状态 RED —— 存在主分片未分配，部分数据不可用")
    } else if health.Status == "yellow" {
        alerts = append(alerts, "⚠️ 集群状态 YELLOW —— 副本分片未完全分配，数据冗余不足")
    }
    if health.UnassignedShards > 0 {
        alerts = append(alerts, fmt.Sprintf("🔴 %d 个未分配分片 —— 检查节点磁盘空间和分片分配策略", health.UnassignedShards))
    }
    if health.NumberOfPendingTasks > 100 {
        alerts = append(alerts, fmt.Sprintf("⚠️ %d 个 pending tasks —— 集群负载过高", health.NumberOfPendingTasks))
    }
    if health.TaskMaxWaitingInQueueMillis > 30000 {
        alerts = append(alerts, fmt.Sprintf("⚠️ 最长 pending task 等待 %dms —— 可能存在阻塞", health.TaskMaxWaitingInQueueMillis))
    }

    if len(alerts) > 0 {
        sb.WriteString("\n### ⚠️ 异常检测\n")
        for _, a := range alerts {
            sb.WriteString(fmt.Sprintf("- %s\n", a))
        }
    }

    return &protocol.ToolResult{
        Content: []protocol.ContentBlock{{Type: "text", Text: sb.String()}},
    }, nil
}

func formatBytesES(b int64) string {
    const unit = 1024
    if b < unit { return fmt.Sprintf("%d B", b) }
    div, exp := int64(unit), 0
    for n := b / unit; n >= unit; n /= unit { div *= unit; exp++ }
    return fmt.Sprintf("%.1f %cB", float64(b)/float64(div), "KMGTPE"[exp])
}
```

### 3.7 YARN Queue Status 工具

> **WHY 需要这个工具** — YARN 队列资源分配是大数据集群管理的核心问题。
> 当用户报告"作业提交后一直 ACCEPTED 不运行"时，根因通常是队列资源耗尽。
> Agent 需要快速了解各队列的资源使用率、pending applications 数量、以及哪个队列在"饿死"其他队列。

> **WHY 计算"有效使用率"而不是直接返回 YARN 的 usedCapacity** —
> YARN 的 `usedCapacity` 是相对于 `capacity` 的百分比，而非相对于集群总资源。
> 一个队列 capacity=10%, usedCapacity=200% 实际只用了集群 20% 资源。
> 我们计算 `absoluteUsed = usedCapacity × capacity / 100` 让 Agent 理解真实的集群级占用。

```go
// go/internal/tools/yarn/queue_status.go
package yarn

import (
    "context"
    "fmt"
    "strings"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

// YARNClient YARN REST API 客户端
type YARNClient interface {
    GetSchedulerInfo(ctx context.Context) (*SchedulerInfo, error)
    GetClusterMetrics(ctx context.Context) (*ClusterMetrics, error)
    GetApplications(ctx context.Context, states []string) ([]Application, error)
    GetNodes(ctx context.Context) ([]NodeInfo, error)
}

// SchedulerInfo YARN 调度器信息
type SchedulerInfo struct {
    Type   string         `json:"type"`   // capacityScheduler | fairScheduler
    Queues []QueueInfo    `json:"queues"`
}

// QueueInfo 队列信息
type QueueInfo struct {
    QueueName         string      `json:"queueName"`
    State             string      `json:"state"`
    Capacity          float64     `json:"capacity"`           // 配置容量百分比
    UsedCapacity      float64     `json:"usedCapacity"`       // 已用容量（相对于自身 capacity）
    MaxCapacity       float64     `json:"maxCapacity"`        // 最大弹性容量
    AbsoluteCapacity  float64     `json:"absoluteCapacity"`   // 绝对容量
    AbsoluteUsedCap   float64     `json:"absoluteUsedCapacity"`
    NumApplications   int         `json:"numApplications"`
    NumPendingApps    int         `json:"numPendingApplications"`
    NumContainers     int         `json:"numContainers"`
    AllocatedMB       int64       `json:"allocatedMB"`
    AllocatedVCores   int         `json:"allocatedVCores"`
    PendingMB         int64       `json:"pendingMB"`
    PendingVCores     int         `json:"pendingVCores"`
    ChildQueues       []QueueInfo `json:"childQueues,omitempty"`
}

// ClusterMetrics YARN 集群指标
type ClusterMetrics struct {
    TotalMB         int64 `json:"totalMB"`
    TotalVCores     int   `json:"totalVirtualCores"`
    AllocatedMB     int64 `json:"allocatedMB"`
    AllocatedVCores int   `json:"allocatedVirtualCores"`
    AvailableMB     int64 `json:"availableMB"`
    AvailableVCores int   `json:"availableVirtualCores"`
    TotalNodes      int   `json:"totalNodes"`
    ActiveNodes     int   `json:"activeNodes"`
    DecommNodes     int   `json:"decommissionedNodes"`
    LostNodes       int   `json:"lostNodes"`
    UnhealthyNodes  int   `json:"unhealthyNodes"`
    AppsSubmitted   int   `json:"appsSubmitted"`
    AppsRunning     int   `json:"appsRunning"`
    AppsPending     int   `json:"appsPending"`
}

type QueueStatusTool struct {
    client YARNClient
}

func NewQueueStatusTool(client YARNClient) *QueueStatusTool {
    return &QueueStatusTool{client: client}
}

func (t *QueueStatusTool) Name() string                       { return "yarn_queue_status" }
func (t *QueueStatusTool) RiskLevel() protocol.RiskLevel      { return protocol.RiskNone }
func (t *QueueStatusTool) Description() string {
    return `获取 YARN 队列的详细资源使用状态。
返回：各队列的容量配置、实际使用率、pending 应用数、vcore/内存分配。
使用场景：作业长时间 ACCEPTED、队列资源不足、调度异常排查。
包含"有效使用率"计算，直观显示队列对集群总资源的实际占用。`
}

func (t *QueueStatusTool) Schema() map[string]interface{} {
    return map[string]interface{}{
        "type": "object",
        "properties": map[string]interface{}{
            "queue_name": map[string]interface{}{
                "type": "string", "description": "队列名称（留空返回所有队列）",
            },
            "include_children": map[string]interface{}{
                "type": "boolean", "description": "是否包含子队列", "default": true,
            },
        },
    }
}

func (t *QueueStatusTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
    queueName, _ := params["queue_name"].(string)
    includeChildren := true
    if v, ok := params["include_children"].(bool); ok {
        includeChildren = v
    }

    // 获取调度器信息和集群指标
    scheduler, err := t.client.GetSchedulerInfo(ctx)
    if err != nil {
        return nil, fmt.Errorf("get scheduler info: %w", err)
    }

    clusterMetrics, err := t.client.GetClusterMetrics(ctx)
    if err != nil {
        return nil, fmt.Errorf("get cluster metrics: %w", err)
    }

    var sb strings.Builder
    sb.WriteString("## YARN 队列状态\n\n")
    sb.WriteString(fmt.Sprintf("**调度器类型**: %s\n", scheduler.Type))
    sb.WriteString(fmt.Sprintf("**集群总资源**: %s 内存, %d vCores\n",
        formatMB(clusterMetrics.TotalMB), clusterMetrics.TotalVCores))
    sb.WriteString(fmt.Sprintf("**已分配**: %s 内存 (%.1f%%), %d vCores (%.1f%%)\n",
        formatMB(clusterMetrics.AllocatedMB),
        float64(clusterMetrics.AllocatedMB)/float64(clusterMetrics.TotalMB)*100,
        clusterMetrics.AllocatedVCores,
        float64(clusterMetrics.AllocatedVCores)/float64(clusterMetrics.TotalVCores)*100))
    sb.WriteString(fmt.Sprintf("**应用**: %d 运行中, %d 等待中\n\n",
        clusterMetrics.AppsRunning, clusterMetrics.AppsPending))

    // 队列表格
    sb.WriteString("### 队列资源分配\n\n")
    sb.WriteString("| 队列 | 状态 | 配置容量 | 有效使用率 | 运行/等待 | 已分配内存 | 已分配vCores |\n")
    sb.WriteString("|------|------|---------|-----------|----------|-----------|-------------|\n")

    var alerts []string
    queues := scheduler.Queues
    if queueName != "" {
        queues = filterQueues(queues, queueName)
    }

    var printQueue func(q QueueInfo, depth int)
    printQueue = func(q QueueInfo, depth int) {
        indent := strings.Repeat("  ", depth)
        prefix := ""
        if depth > 0 {
            prefix = "└─ "
        }

        // 计算有效使用率（相对于集群总资源）
        effectiveUsage := q.AbsoluteUsedCap
        usageEmoji := "🟢"
        if effectiveUsage > 90 {
            usageEmoji = "🔴"
        } else if effectiveUsage > 75 {
            usageEmoji = "🟡"
        }

        sb.WriteString(fmt.Sprintf("| %s%s%s | %s | %.1f%% | %s %.1f%% | %d / %d | %s | %d |\n",
            indent, prefix, q.QueueName,
            q.State,
            q.AbsoluteCapacity,
            usageEmoji, effectiveUsage,
            q.NumApplications, q.NumPendingApps,
            formatMB(q.AllocatedMB),
            q.AllocatedVCores))

        // 异常检测
        if q.NumPendingApps > 10 {
            alerts = append(alerts, fmt.Sprintf("⚠️ 队列 %s 有 %d 个 pending 应用 —— 可能资源不足",
                q.QueueName, q.NumPendingApps))
        }
        if effectiveUsage > 95 {
            alerts = append(alerts, fmt.Sprintf("🔴 队列 %s 使用率 %.1f%% —— 接近满载，新作业将排队",
                q.QueueName, effectiveUsage))
        }
        if q.State != "RUNNING" {
            alerts = append(alerts, fmt.Sprintf("🚨 队列 %s 状态为 %s —— 不接受新作业",
                q.QueueName, q.State))
        }

        // 递归打印子队列
        if includeChildren {
            for _, child := range q.ChildQueues {
                printQueue(child, depth+1)
            }
        }
    }

    for _, q := range queues {
        printQueue(q, 0)
    }

    // 异常汇总
    if len(alerts) > 0 {
        sb.WriteString("\n### ⚠️ 异常检测\n")
        for _, a := range alerts {
            sb.WriteString(fmt.Sprintf("- %s\n", a))
        }
    }

    return &protocol.ToolResult{
        Content: []protocol.ContentBlock{{Type: "text", Text: sb.String()}},
    }, nil
}

func formatMB(mb int64) string {
    if mb < 1024 {
        return fmt.Sprintf("%d MB", mb)
    }
    return fmt.Sprintf("%.1f GB", float64(mb)/1024)
}

func filterQueues(queues []QueueInfo, name string) []QueueInfo {
    var result []QueueInfo
    for _, q := range queues {
        if q.QueueName == name {
            result = append(result, q)
        }
        if len(q.ChildQueues) > 0 {
            result = append(result, filterQueues(q.ChildQueues, name)...)
        }
    }
    return result
}
```

### 3.8 Log Search 工具

> **WHY 需要这个工具** — 日志是根因分析的最后一公里。当 Diagnostic Agent 通过指标和状态信息
> 缩小了故障范围后，需要查看具体的错误日志来确认根因。这个工具将 Elasticsearch 的日志查询
> 封装为一个对 LLM 友好的接口。

> **WHY 限制返回行数（max 200 行）** —
> 1. LLM 的 context window 有限，返回 10000 行日志只会淹没有用信息
> 2. 大量文本增加 token 消耗，一次日志查询可能消耗 $0.1+ 的 API 成本
> 3. 200 行足以覆盖"错误前后上下文"的诊断需求
> 4. 如果需要更多，Agent 可以通过 `log_search_context` 工具追溯特定日志行的前后文

> **WHY 用 Lucene Query 而不是自然语言查询** —
> - 直接暴露 Lucene 查询语法，让 LLM 利用它已经学到的 ES 查询知识
> - 自然语言→Lucene 的转换层会引入误差（LLM 生成的查询已经足够准确）
> - 保留了全部 Lucene 能力：正则、范围、布尔组合

```go
// go/internal/tools/logtools/search_logs.go
package logtools

import (
    "context"
    "encoding/json"
    "fmt"
    "net/http"
    "strings"
    "time"

    "github.com/rs/zerolog/log"
    "github.com/yourorg/aiops-mcp/internal/protocol"
)

// SearchRequest 日志搜索请求
type SearchRequest struct {
    Index     string `json:"index"`      // ES 索引名或模式
    Query     string `json:"query"`      // Lucene 查询语法
    TimeFrom  string `json:"time_from"`  // ISO8601 或相对时间
    TimeTo    string `json:"time_to"`
    MaxLines  int    `json:"max_lines"`
    SortOrder string `json:"sort_order"` // asc | desc
}

// SearchResponse 日志搜索响应
type SearchResponse struct {
    TotalHits int64     `json:"total_hits"`
    Took      int       `json:"took_ms"`
    Hits      []LogLine `json:"hits"`
}

// LogLine 单条日志
type LogLine struct {
    Timestamp string `json:"@timestamp"`
    Level     string `json:"level"`
    Logger    string `json:"logger"`
    Host      string `json:"host"`
    Message   string `json:"message"`
    StackTrace string `json:"stack_trace,omitempty"`
}

type SearchLogsTool struct {
    esURL      string
    httpClient *http.Client
}

func NewSearchLogsTool(esURL string) *SearchLogsTool {
    return &SearchLogsTool{
        esURL:      esURL,
        httpClient: &http.Client{Timeout: 10 * time.Second},
    }
}

func (t *SearchLogsTool) Name() string                       { return "log_search" }
func (t *SearchLogsTool) RiskLevel() protocol.RiskLevel      { return protocol.RiskLow } // Level 1: 大范围搜索可能影响 ES
func (t *SearchLogsTool) Description() string {
    return `在 Elasticsearch 中搜索日志。
支持 Lucene 查询语法，可按时间范围、日志级别、主机名过滤。
使用场景：故障根因分析时查看错误日志、追踪异常堆栈。
限制：最多返回 200 行日志（避免上下文溢出）。
提示：
- 搜索错误: level:ERROR AND message:"OutOfMemoryError"
- 按主机过滤: host:datanode03 AND level:WARN
- 时间范围查询: 使用 time_from/time_to 参数`
}

func (t *SearchLogsTool) Schema() map[string]interface{} {
    return map[string]interface{}{
        "type": "object",
        "required": []string{"query"},
        "properties": map[string]interface{}{
            "query": map[string]interface{}{
                "type": "string",
                "description": "Lucene 查询语法（如 'level:ERROR AND message:\"timeout\"'）",
            },
            "index": map[string]interface{}{
                "type": "string",
                "description": "ES 索引名或模式（如 'hadoop-logs-*'）",
                "default": "hadoop-logs-*",
            },
            "time_from": map[string]interface{}{
                "type": "string",
                "description": "开始时间（ISO8601 或 'now-1h', 'now-30m'）",
                "default": "now-1h",
            },
            "time_to": map[string]interface{}{
                "type": "string",
                "description": "结束时间",
                "default": "now",
            },
            "max_lines": map[string]interface{}{
                "type": "integer",
                "description": "最大返回行数（上限 200）",
                "default": 50,
            },
            "sort_order": map[string]interface{}{
                "type": "string",
                "description": "排序方式",
                "enum": []string{"asc", "desc"},
                "default": "desc",
            },
        },
    }
}

func (t *SearchLogsTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
    query, _ := params["query"].(string)
    index, _ := params["index"].(string)
    if index == "" {
        index = "hadoop-logs-*"
    }
    timeFrom, _ := params["time_from"].(string)
    if timeFrom == "" {
        timeFrom = "now-1h"
    }
    timeTo, _ := params["time_to"].(string)
    if timeTo == "" {
        timeTo = "now"
    }
    maxLines := 50
    if v, ok := params["max_lines"].(float64); ok {
        maxLines = int(v)
    }
    // 硬性上限：200 行
    if maxLines > 200 {
        maxLines = 200
    }
    sortOrder, _ := params["sort_order"].(string)
    if sortOrder == "" {
        sortOrder = "desc"
    }

    // 构建 ES 查询
    esQuery := buildESQuery(query, timeFrom, timeTo, maxLines, sortOrder)

    log.Debug().
        Str("index", index).
        Str("query", query).
        Str("time_range", fmt.Sprintf("%s ~ %s", timeFrom, timeTo)).
        Int("max_lines", maxLines).
        Msg("executing log search")

    // 发送请求到 ES
    url := fmt.Sprintf("%s/%s/_search", t.esURL, index)
    reqBody, _ := json.Marshal(esQuery)

    req, err := http.NewRequestWithContext(ctx, "POST", url, strings.NewReader(string(reqBody)))
    if err != nil {
        return nil, fmt.Errorf("create request: %w", err)
    }
    req.Header.Set("Content-Type", "application/json")

    resp, err := t.httpClient.Do(req)
    if err != nil {
        return nil, fmt.Errorf("ES search: %w", err)
    }
    defer resp.Body.Close()

    if resp.StatusCode != http.StatusOK {
        return nil, fmt.Errorf("ES returned %d", resp.StatusCode)
    }

    // 解析响应
    var esResp struct {
        Took int `json:"took"`
        Hits struct {
            Total struct {
                Value int64 `json:"value"`
            } `json:"total"`
            Hits []struct {
                Source LogLine `json:"_source"`
            } `json:"hits"`
        } `json:"hits"`
    }

    if err := json.NewDecoder(resp.Body).Decode(&esResp); err != nil {
        return nil, fmt.Errorf("decode ES response: %w", err)
    }

    // 格式化输出
    var sb strings.Builder
    sb.WriteString(fmt.Sprintf("## 日志搜索结果\n\n"))
    sb.WriteString(fmt.Sprintf("**查询**: `%s`\n", query))
    sb.WriteString(fmt.Sprintf("**索引**: %s\n", index))
    sb.WriteString(fmt.Sprintf("**时间范围**: %s ~ %s\n", timeFrom, timeTo))
    sb.WriteString(fmt.Sprintf("**总匹配**: %d 条 (显示 %d 条)\n", esResp.Hits.Total.Value, len(esResp.Hits.Hits)))
    sb.WriteString(fmt.Sprintf("**耗时**: %dms\n\n", esResp.Took))

    if len(esResp.Hits.Hits) == 0 {
        sb.WriteString("_没有匹配的日志记录_\n")
    } else {
        // 按级别统计
        levelCounts := make(map[string]int)
        for _, hit := range esResp.Hits.Hits {
            levelCounts[hit.Source.Level]++
        }
        sb.WriteString("**级别分布**: ")
        for level, count := range levelCounts {
            emoji := levelEmoji(level)
            sb.WriteString(fmt.Sprintf("%s %s=%d ", emoji, level, count))
        }
        sb.WriteString("\n\n---\n\n")

        // 输出日志行
        for _, hit := range esResp.Hits.Hits {
            line := hit.Source
            emoji := levelEmoji(line.Level)
            sb.WriteString(fmt.Sprintf("%s `%s` [%s] **%s** @%s\n",
                emoji, line.Timestamp, line.Level, line.Host, line.Logger))
            sb.WriteString(fmt.Sprintf("  %s\n", line.Message))
            if line.StackTrace != "" {
                // 只显示前 5 行堆栈
                stLines := strings.Split(line.StackTrace, "\n")
                if len(stLines) > 5 {
                    stLines = append(stLines[:5], fmt.Sprintf("  ... 还有 %d 行", len(stLines)-5))
                }
                sb.WriteString("  ```\n")
                for _, sl := range stLines {
                    sb.WriteString(fmt.Sprintf("  %s\n", sl))
                }
                sb.WriteString("  ```\n")
            }
            sb.WriteString("\n")
        }

        // 截断提示
        if esResp.Hits.Total.Value > int64(maxLines) {
            sb.WriteString(fmt.Sprintf("\n---\n⚠️ 显示了 %d/%d 条结果。使用 `log_search_context` 查看特定日志的上下文。\n",
                len(esResp.Hits.Hits), esResp.Hits.Total.Value))
        }
    }

    return &protocol.ToolResult{
        Content: []protocol.ContentBlock{{Type: "text", Text: sb.String()}},
    }, nil
}

// buildESQuery 构建 ES 查询 DSL
func buildESQuery(luceneQuery, timeFrom, timeTo string, size int, sortOrder string) map[string]interface{} {
    return map[string]interface{}{
        "size": size,
        "sort": []map[string]interface{}{
            {"@timestamp": map[string]string{"order": sortOrder}},
        },
        "query": map[string]interface{}{
            "bool": map[string]interface{}{
                "must": []map[string]interface{}{
                    {
                        "query_string": map[string]interface{}{
                            "query":            luceneQuery,
                            "default_field":    "message",
                            "analyze_wildcard": true,
                        },
                    },
                },
                "filter": []map[string]interface{}{
                    {
                        "range": map[string]interface{}{
                            "@timestamp": map[string]interface{}{
                                "gte": timeFrom,
                                "lte": timeTo,
                            },
                        },
                    },
                },
            },
        },
        "_source": []string{"@timestamp", "level", "logger", "host", "message", "stack_trace"},
    }
}

func levelEmoji(level string) string {
    switch strings.ToUpper(level) {
    case "ERROR", "FATAL":
        return "🔴"
    case "WARN":
        return "🟡"
    case "INFO":
        return "🟢"
    case "DEBUG":
        return "⚪"
    default:
        return "⚪"
    }
}
```

> **WHY RiskLevel = RiskLow（1）而不是 RiskNone（0）？**
> - 日志搜索是只读操作，不会修改任何数据
> - 但大范围搜索（如 `*` 查询 + 30天时间范围）可能导致 ES 节点高 CPU
> - Level 1 意味着：自动执行，但记录更详细的审计日志（包含查询语句和返回行数）
> - 这让我们可以在事后分析中发现"Agent 是否在频繁执行昂贵的日志查询"

---

## 4. Server 入口

```go
// go/cmd/mcp-server/main.go
package main

import (
    "context"
    "fmt"
    "os"
    "os/signal"
    "syscall"
    "time"

    "github.com/gofiber/fiber/v2"
    "github.com/gofiber/fiber/v2/middleware/cors"
    "github.com/gofiber/fiber/v2/middleware/recover"
    "github.com/rs/zerolog"
    "github.com/rs/zerolog/log"

    "github.com/yourorg/aiops-mcp/internal/config"
    "github.com/yourorg/aiops-mcp/internal/middleware"
    "github.com/yourorg/aiops-mcp/internal/protocol"
    "github.com/yourorg/aiops-mcp/internal/tools/hdfs"
    "github.com/yourorg/aiops-mcp/internal/tools/yarn"
    "github.com/yourorg/aiops-mcp/internal/tools/kafka"
    "github.com/yourorg/aiops-mcp/internal/tools/es"
    "github.com/yourorg/aiops-mcp/internal/tools/metrics"
    "github.com/yourorg/aiops-mcp/internal/tools/logtools"
    "github.com/yourorg/aiops-mcp/internal/tools/configtools"
    "github.com/yourorg/aiops-mcp/internal/tools/ops"
)

var (
    Version   = "dev"
    BuildTime = "unknown"
)

func main() {
    // 加载配置
    cfg, err := config.Load("configs/production.yaml")
    if err != nil {
        log.Fatal().Err(err).Msg("load config")
    }

    // 初始化日志
    setupLogger(cfg.Server.LogLevel)
    log.Info().Str("version", Version).Str("build_time", BuildTime).Msg("starting MCP server")

    // 注册所有工具
    registry := protocol.NewRegistry()
    registerAllTools(registry, cfg)
    log.Info().Int("tools", len(registry.ListDefinitions())).Msg("tools registered")

    // 中间件链
    mw := func(tool protocol.Tool, handler protocol.ToolHandler) protocol.ToolHandler {
        return middleware.BuildMiddlewareChain(handler, tool, cfg)
    }

    // JSON-RPC Handler
    handler := protocol.NewHandler(registry, mw)

    // 启动模式
    if cfg.Server.Mode == "stdio" {
        log.Info().Msg("starting in stdio mode")
        startStdioServer(handler)
    } else {
        log.Info().Int("port", cfg.Server.Port).Msg("starting in HTTP mode")
        startHTTPServer(handler, cfg, registry)
    }
}

func registerAllTools(registry *protocol.Registry, cfg *config.Config) {
    // === HDFS (8 tools) ===
    hdfsClient := hdfs.NewClient(cfg.HDFS.NameNodeURL)
    registry.Register(hdfs.NewClusterOverviewTool(hdfsClient))
    registry.Register(hdfs.NewNameNodeStatusTool(hdfsClient))
    registry.Register(hdfs.NewDataNodeListTool(hdfsClient))
    registry.Register(hdfs.NewBlockReportTool(hdfsClient))
    registry.Register(hdfs.NewFsckStatusTool(hdfsClient))
    registry.Register(hdfs.NewSnapshotListTool(hdfsClient))
    registry.Register(hdfs.NewSafeModeTool(hdfsClient))
    registry.Register(hdfs.NewDecommissionStatusTool(hdfsClient))

    // === YARN (7 tools) ===
    yarnClient := yarn.NewClient(cfg.YARN.ResourceManagerURL)
    registry.Register(yarn.NewClusterMetricsTool(yarnClient))
    registry.Register(yarn.NewQueueStatusTool(yarnClient))
    registry.Register(yarn.NewApplicationsTool(yarnClient))
    registry.Register(yarn.NewNodeListTool(yarnClient))
    registry.Register(yarn.NewAppAttemptLogsTool(yarnClient))
    registry.Register(yarn.NewSchedulerInfoTool(yarnClient))
    registry.Register(yarn.NewNodeResourceUsageTool(yarnClient))

    // === Kafka (7 tools) ===
    kafkaClient, _ := kafka.NewClient(cfg.Kafka.Brokers)
    registry.Register(kafka.NewClusterOverviewTool(kafkaClient))
    registry.Register(kafka.NewConsumerLagTool(kafkaClient))
    registry.Register(kafka.NewTopicListTool(kafkaClient))
    registry.Register(kafka.NewTopicDetailTool(kafkaClient))
    registry.Register(kafka.NewBrokerConfigsTool(kafkaClient))
    registry.Register(kafka.NewUnderReplicatedTool(kafkaClient))
    registry.Register(kafka.NewConsumerGroupsTool(kafkaClient))

    // === ES (6 tools) ===
    esClient := es.NewClient(cfg.Elasticsearch.URL)
    registry.Register(es.NewClusterHealthTool(esClient))
    registry.Register(es.NewNodeStatsTool(esClient))
    registry.Register(es.NewIndexStatsTool(esClient))
    registry.Register(es.NewPendingTasksTool(esClient))
    registry.Register(es.NewShardAllocationTool(esClient))
    registry.Register(es.NewHotThreadsTool(esClient))

    // === Metrics (4 tools) ===
    registry.Register(metrics.NewQueryMetricsTool(cfg.Prometheus.URL))
    registry.Register(metrics.NewQueryRangeTool(cfg.Prometheus.URL))
    registry.Register(metrics.NewAnomalyDetectionTool(cfg.Prometheus.URL))
    registry.Register(metrics.NewAlertQueryTool(cfg.Prometheus.URL))

    // === Log (4 tools) ===
    registry.Register(logtools.NewSearchLogsTool(cfg.Elasticsearch.URL))
    registry.Register(logtools.NewSearchLogsContextTool(cfg.Elasticsearch.URL))
    registry.Register(logtools.NewLogPatternTool(cfg.Elasticsearch.URL))
    registry.Register(logtools.NewRecentErrorsTool(cfg.Elasticsearch.URL))

    // === Config (3 tools) ===
    registry.Register(configtools.NewGetConfigTool(cfg))
    registry.Register(configtools.NewDiffConfigTool(cfg))
    registry.Register(configtools.NewValidateConfigTool(cfg))

    // === ZooKeeper (3 tools) ===
    // ... 注册 ZK 工具

    // === Ops (6 tools, 高风险) ===
    opsClient := ops.NewClient(cfg)
    registry.Register(ops.NewRestartServiceTool(opsClient))
    registry.Register(ops.NewScaleResourceTool(opsClient))
    registry.Register(ops.NewUpdateConfigTool(opsClient))
    registry.Register(ops.NewClearCacheTool(opsClient))
    registry.Register(ops.NewTriggerGCTool(opsClient))
    registry.Register(ops.NewDecommissionNodeTool(opsClient))
}

func startHTTPServer(handler *protocol.Handler, cfg *config.Config, registry *protocol.Registry) {
    app := fiber.New(fiber.Config{
        ReadTimeout:  30 * time.Second,
        WriteTimeout: 30 * time.Second,
        IdleTimeout:  120 * time.Second,
        BodyLimit:    1 * 1024 * 1024, // 1MB
    })

    // 中间件
    app.Use(recover.New())
    app.Use(cors.New(cors.Config{
        AllowOrigins: cfg.Server.CORSOrigins,
    }))

    // 路由
    app.Post("/mcp", func(c *fiber.Ctx) error {
        ctx := c.UserContext()
        result, err := handler.HandleRequest(ctx, c.Body())
        if err != nil {
            return c.Status(500).JSON(fiber.Map{"error": err.Error()})
        }
        c.Set("Content-Type", "application/json")
        return c.Send(result)
    })

    // 健康检查
    app.Get("/health", func(c *fiber.Ctx) error {
        return c.JSON(fiber.Map{
            "status":     "healthy",
            "version":    Version,
            "build_time": BuildTime,
            "tools":      len(registry.ListDefinitions()),
            "uptime":     time.Since(startTime).String(),
        })
    })

    // Prometheus 指标
    app.Get("/metrics", func(c *fiber.Ctx) error {
        // promhttp handler
        return nil
    })

    // 优雅关闭
    go func() {
        if err := app.Listen(fmt.Sprintf(":%d", cfg.Server.Port)); err != nil {
            log.Fatal().Err(err).Msg("server failed")
        }
    }()

    quit := make(chan os.Signal, 1)
    signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
    <-quit

    log.Info().Msg("shutting down...")
    ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
    defer cancel()
    _ = app.ShutdownWithContext(ctx)
    log.Info().Msg("server stopped")
}

var startTime = time.Now()

func setupLogger(level string) {
    zerolog.TimeFieldFormat = zerolog.TimeFormatUnix
    lvl, _ := zerolog.ParseLevel(level)
    zerolog.SetGlobalLevel(lvl)
    log.Logger = zerolog.New(os.Stdout).With().Timestamp().Caller().Logger()
}
```

### 4.1 Stdio 模式实现

> **WHY 支持 stdio 模式** — MCP 协议定义了两种传输方式：HTTP（用于服务器部署）和 stdio（用于本地进程间通信）。
> stdio 模式的核心价值：
> 1. **Claude Desktop / Cursor 集成**：这些工具通过 `stdin/stdout` 与 MCP Server 通信，不走 HTTP
> 2. **本地开发调试**：开发者可以直接在终端运行 MCP Server，用管道测试工具调用
> 3. **零网络依赖**：不需要监听端口，不涉及 TLS/认证，简化了本地开发环境

> **WHY NOT 只支持 HTTP 模式？**
> - Claude Desktop 的 MCP 插件只支持 stdio，不支持 HTTP —— 如果不实现 stdio，就无法在 Claude Desktop 中使用我们的工具
> - 本地开发时，启动一个 HTTP 服务器、配置端口、处理 CORS 是不必要的复杂度
> - stdio 模式天然无并发问题（单 goroutine 读写），适合调试

```go
// go/internal/transport/stdio.go
package transport

import (
    "bufio"
    "context"
    "encoding/json"
    "fmt"
    "io"
    "os"
    "os/signal"
    "syscall"

    "github.com/rs/zerolog"
    "github.com/rs/zerolog/log"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

// StartStdioServer 启动 stdio 模式的 MCP Server
// 读取 stdin 的 JSON-RPC 请求，处理后将响应写入 stdout
// 日志输出到 stderr（不污染 JSON-RPC 通信通道）
func StartStdioServer(handler *protocol.Handler) {
    // CRITICAL: stdio 模式下日志必须输出到 stderr
    // 如果输出到 stdout 会与 JSON-RPC 响应混在一起，Client 无法解析
    log.Logger = zerolog.New(os.Stderr).With().Timestamp().Logger()
    log.Info().Msg("MCP server starting in stdio mode")

    ctx, cancel := context.WithCancel(context.Background())
    defer cancel()

    // 监听退出信号
    sigCh := make(chan os.Signal, 1)
    signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
    go func() {
        <-sigCh
        log.Info().Msg("received signal, shutting down stdio server")
        cancel()
    }()

    scanner := bufio.NewScanner(os.Stdin)
    // MCP 消息可能很大（如工具返回大量日志），增大缓冲区
    scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024) // 1MB max

    for scanner.Scan() {
        select {
        case <-ctx.Done():
            return
        default:
        }

        line := scanner.Bytes()
        if len(line) == 0 {
            continue
        }

        log.Debug().
            Int("bytes", len(line)).
            Msg("received stdin request")

        // 处理请求
        response, err := handler.HandleRequest(ctx, line)
        if err != nil {
            // JSON-RPC 规范：即使出错也返回 JSON 格式错误
            errResp := protocol.Response{
                JSONRPC: "2.0",
                Error:   &protocol.RPCError{Code: -32603, Message: err.Error()},
            }
            response, _ = json.Marshal(errResp)
        }

        // 写入 stdout（每个响应一行 + 换行符）
        if _, err := fmt.Fprintf(os.Stdout, "%s\n", response); err != nil {
            log.Error().Err(err).Msg("failed to write stdout response")
            return
        }
    }

    if err := scanner.Err(); err != nil && err != io.EOF {
        log.Error().Err(err).Msg("stdin scanner error")
    }
}
```

**stdio vs HTTP 模式选择策略：**

| 场景 | 推荐模式 | 原因 |
|------|---------|------|
| 生产部署 | HTTP | 多 Client 并发、负载均衡、监控指标 |
| Claude Desktop 集成 | stdio | Claude Desktop 只支持 stdio |
| Cursor IDE 集成 | stdio | 本地进程通信，最低延迟 |
| 本地开发调试 | stdio | 无需端口、无需配置 |
| 自研 Python Agent | HTTP | 跨进程/跨机器通信 |
| 集成测试 | stdio | 进程内通信，测试更快更稳定 |

```bash
# 启动模式选择
# 生产：HTTP 模式
./mcp-server --config configs/production.yaml

# Claude Desktop：stdio 模式（在 claude_desktop_config.json 中配置）
# {
#   "mcpServers": {
#     "aiops-hdfs": {
#       "command": "/path/to/mcp-server",
#       "args": ["--config", "configs/local.yaml", "--mode", "stdio"]
#     }
#   }
# }

# 本地调试：stdio + 管道
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ./mcp-server --mode stdio
```

### 4.2 连接池与性能优化

> **WHY 需要连接池** — MCP Server 是一个长驻进程，持续接收来自 Agent 的工具调用请求。
> 每次调用都需要连接 HDFS/YARN/Kafka/ES 等后端。如果每次都新建 TCP 连接：
> - 三次握手 + TLS 协商（如果有）≈ 50-200ms 延迟
> - 后端连接数暴涨，可能触发连接限制
> - GC 压力增大（短生命周期的 net.Conn 对象）

#### HTTP Client 连接池配置

> **WHY 差异化超时** — 不同大数据组件的 API 响应特性差异巨大：
> - **HDFS JMX**（10s）：直接读取 JVM 内存中的 MBean，正常响应 < 100ms，10s 超时能覆盖 GC 卡顿
> - **Kafka Admin API**（15s）：需要与所有 Broker 协调获取 metadata，大集群可能慢
> - **ES REST API**（5s）：ES 本身有超时机制（`timeout` 参数），MCP 侧 5s 足够
> - **YARN REST API**（10s）：ResourceManager 可能在处理大量 app 时变慢
> - **PromQL**（15s）：range query 可能扫描大量时间序列数据
>
> 如果统一用 30s 超时：慢查询会长时间占用 goroutine，快速请求也得等更久才能发现后端故障。

```go
// go/internal/pool/http_pool.go
package pool

import (
    "crypto/tls"
    "net"
    "net/http"
    "time"

    "github.com/rs/zerolog/log"
)

// ComponentConfig 各组件的连接配置
type ComponentConfig struct {
    // 基础超时
    ConnectTimeout time.Duration // TCP 连接超时
    RequestTimeout time.Duration // 请求总超时（含读写）
    IdleTimeout    time.Duration // 空闲连接保持时间

    // 连接池
    MaxIdleConns        int // 全局最大空闲连接数
    MaxIdleConnsPerHost int // 每个 host 最大空闲连接
    MaxConnsPerHost     int // 每个 host 最大并发连接
}

// DefaultConfigs 各组件的默认连接配置
var DefaultConfigs = map[string]ComponentConfig{
    "hdfs": {
        ConnectTimeout:      3 * time.Second,
        RequestTimeout:      10 * time.Second,
        IdleTimeout:         90 * time.Second,
        MaxIdleConns:        10,
        MaxIdleConnsPerHost: 5,   // HA 两个 NameNode，每个 5 个连接
        MaxConnsPerHost:     20,
    },
    "yarn": {
        ConnectTimeout:      3 * time.Second,
        RequestTimeout:      10 * time.Second,
        IdleTimeout:         90 * time.Second,
        MaxIdleConns:        10,
        MaxIdleConnsPerHost: 5,
        MaxConnsPerHost:     20,
    },
    "kafka": {
        ConnectTimeout:      5 * time.Second,  // Kafka 连接建立可能较慢
        RequestTimeout:      15 * time.Second,  // Admin API 需要等 Broker 协调
        IdleTimeout:         120 * time.Second, // Kafka 连接复用价值高
        MaxIdleConns:        15,
        MaxIdleConnsPerHost: 5,
        MaxConnsPerHost:     30,
    },
    "elasticsearch": {
        ConnectTimeout:      2 * time.Second,  // ES 连接通常很快
        RequestTimeout:      5 * time.Second,  // ES 自带超时机制
        IdleTimeout:         60 * time.Second,
        MaxIdleConns:        20,
        MaxIdleConnsPerHost: 10,  // ES 请求量大，多保留连接
        MaxConnsPerHost:     50,
    },
    "prometheus": {
        ConnectTimeout:      2 * time.Second,
        RequestTimeout:      15 * time.Second,  // Range query 可能慢
        IdleTimeout:         60 * time.Second,
        MaxIdleConns:        5,
        MaxIdleConnsPerHost: 3,
        MaxConnsPerHost:     10,
    },
}

// NewHTTPClient 根据组件配置创建带连接池的 HTTP Client
func NewHTTPClient(component string) *http.Client {
    cfg, ok := DefaultConfigs[component]
    if !ok {
        log.Warn().Str("component", component).Msg("no pool config, using defaults")
        cfg = DefaultConfigs["hdfs"] // 保守默认
    }

    transport := &http.Transport{
        DialContext: (&net.Dialer{
            Timeout:   cfg.ConnectTimeout,
            KeepAlive: 30 * time.Second,
        }).DialContext,

        MaxIdleConns:        cfg.MaxIdleConns,
        MaxIdleConnsPerHost: cfg.MaxIdleConnsPerHost,
        MaxConnsPerHost:     cfg.MaxConnsPerHost,
        IdleConnTimeout:     cfg.IdleTimeout,

        // TLS 配置（内网通常不需要）
        TLSClientConfig: &tls.Config{
            InsecureSkipVerify: false, // 生产环境不跳过证书验证
        },
        TLSHandshakeTimeout: 5 * time.Second,

        // 响应头读取超时（防止服务端 hang 住不返回 header）
        ResponseHeaderTimeout: cfg.RequestTimeout / 2,

        // 开启 HTTP/2（ES 和 Prometheus 支持）
        ForceAttemptHTTP2: true,
    }

    return &http.Client{
        Timeout:   cfg.RequestTimeout,
        Transport: transport,
    }
}
```

> **WHY `MaxConnsPerHost` 设置不同？**
> - ES 的 `MaxConnsPerHost=50`：诊断场景中，Agent 可能同时查询 health + stats + allocation + indices，并发度高
> - HDFS/YARN 的 `MaxConnsPerHost=20`：通常是单次查询，20 个连接足以应对突发
> - Prometheus 的 `MaxConnsPerHost=10`：通常只有一个 Prometheus 实例，不需要太多连接

#### 连接健康检查

> **WHY 需要健康检查** — 空闲连接可能已经被服务端关闭（半开连接），如果不检查，
> 下次使用时会遇到 "connection reset by peer" 错误，导致工具调用失败。

```go
// go/internal/pool/health_check.go
package pool

import (
    "context"
    "fmt"
    "net/http"
    "sync"
    "time"

    "github.com/rs/zerolog/log"
)

// HealthChecker 后端健康检查器
type HealthChecker struct {
    mu       sync.RWMutex
    backends map[string]*BackendStatus
    interval time.Duration
}

// BackendStatus 后端状态
type BackendStatus struct {
    Name       string        `json:"name"`
    URL        string        `json:"url"`
    Healthy    bool          `json:"healthy"`
    LastCheck  time.Time     `json:"last_check"`
    LastError  string        `json:"last_error,omitempty"`
    Latency    time.Duration `json:"latency"`
    CheckCount int64         `json:"check_count"`
    FailCount  int64         `json:"fail_count"`
}

func NewHealthChecker(interval time.Duration) *HealthChecker {
    return &HealthChecker{
        backends: make(map[string]*BackendStatus),
        interval: interval,
    }
}

// RegisterBackend 注册后端
func (hc *HealthChecker) RegisterBackend(name, healthURL string) {
    hc.mu.Lock()
    defer hc.mu.Unlock()
    hc.backends[name] = &BackendStatus{
        Name: name,
        URL:  healthURL,
    }
}

// Start 启动后台健康检查
func (hc *HealthChecker) Start(ctx context.Context) {
    ticker := time.NewTicker(hc.interval)
    defer ticker.Stop()

    // 立即执行一次
    hc.checkAll(ctx)

    for {
        select {
        case <-ctx.Done():
            log.Info().Msg("health checker stopped")
            return
        case <-ticker.C:
            hc.checkAll(ctx)
        }
    }
}

func (hc *HealthChecker) checkAll(ctx context.Context) {
    hc.mu.RLock()
    backends := make([]string, 0, len(hc.backends))
    for name := range hc.backends {
        backends = append(backends, name)
    }
    hc.mu.RUnlock()

    var wg sync.WaitGroup
    for _, name := range backends {
        wg.Add(1)
        go func(name string) {
            defer wg.Done()
            hc.checkOne(ctx, name)
        }(name)
    }
    wg.Wait()
}

func (hc *HealthChecker) checkOne(ctx context.Context, name string) {
    hc.mu.RLock()
    backend := hc.backends[name]
    hc.mu.RUnlock()

    checkCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
    defer cancel()

    start := time.Now()
    req, _ := http.NewRequestWithContext(checkCtx, "GET", backend.URL, nil)
    resp, err := http.DefaultClient.Do(req)
    latency := time.Since(start)

    hc.mu.Lock()
    defer hc.mu.Unlock()

    backend.CheckCount++
    backend.LastCheck = time.Now()
    backend.Latency = latency

    if err != nil {
        backend.Healthy = false
        backend.LastError = err.Error()
        backend.FailCount++
        log.Warn().
            Str("backend", name).
            Err(err).
            Dur("latency", latency).
            Msg("health check failed")
        return
    }
    defer resp.Body.Close()

    if resp.StatusCode >= 200 && resp.StatusCode < 300 {
        backend.Healthy = true
        backend.LastError = ""
    } else {
        backend.Healthy = false
        backend.LastError = fmt.Sprintf("HTTP %d", resp.StatusCode)
        backend.FailCount++
    }
}

// IsHealthy 检查指定后端是否健康
func (hc *HealthChecker) IsHealthy(name string) bool {
    hc.mu.RLock()
    defer hc.mu.RUnlock()
    if b, ok := hc.backends[name]; ok {
        return b.Healthy
    }
    return false
}

// GetAllStatus 获取所有后端状态（用于 /health 端点）
func (hc *HealthChecker) GetAllStatus() map[string]*BackendStatus {
    hc.mu.RLock()
    defer hc.mu.RUnlock()
    result := make(map[string]*BackendStatus, len(hc.backends))
    for k, v := range hc.backends {
        cp := *v
        result[k] = &cp
    }
    return result
}
```

> **WHY 检查间隔用 30s 而不是更短？**
> - 太频繁（如 1s）会给后端带来不必要的负载
> - 30s 在"快速发现故障"和"减少开销"之间取得平衡
> - 熔断器（见 `10-MCP中间件链.md`）会在实际调用失败时更快响应，健康检查是兜底机制

---

## 5. 配置

```yaml
# go/configs/production.yaml
server:
  mode: "http"          # http | stdio
  port: 8080
  log_level: "info"
  cors_origins: "https://aiops.internal.example.com"

hdfs:
  namenode_url: "http://namenode:9870"
  namenode_ha:
    nn1: "http://nn1:9870"
    nn2: "http://nn2:9870"

yarn:
  resourcemanager_url: "http://resourcemanager:8088"

kafka:
  brokers: ["kafka1:9092", "kafka2:9092", "kafka3:9092"]

elasticsearch:
  url: "http://es:9200"

prometheus:
  url: "http://prometheus:9090"

# 中间件配置
middleware:
  timeout_seconds: 30
  cache_ttl_seconds: 30
  circuit_breaker:
    threshold: 5
    timeout_seconds: 30
  rate_limit:
    global_rps: 100
    per_user_rps: 10
  audit:
    enabled: true
    pg_dsn: "postgresql://aiops:pass@postgres:5432/aiops"
```

### 5.1 配置热更新机制

> **WHY 需要热更新** — 生产环境中，MCP Server 的配置变更（如新增 Kafka Broker 节点、调整超时阈值、
> 更新告警规则）不应该需要重启服务。重启意味着：
> 1. 所有正在进行的工具调用被中断
> 2. Agent 侧看到连接断开，可能触发重试风暴
> 3. 重启期间的告警无法被处理
> 4. 在高可用部署中，需要协调多实例滚动重启

> **WHY 不用 ConfigMap watch（K8s）或 Consul/etcd？**
> - 项目目标是可以在非 K8s 环境运行（VM 部署、本地开发）
> - 文件系统是最通用的配置变更机制——K8s ConfigMap 最终也是挂载为文件
> - 减少外部依赖：不需要 Consul/etcd 就能实现配置热更新

```go
// go/internal/config/watcher.go
package config

import (
    "context"
    "sync"
    "time"

    "github.com/fsnotify/fsnotify"
    "github.com/rs/zerolog/log"
)

// ConfigWatcher 监听配置文件变更并触发重载
type ConfigWatcher struct {
    configPath  string
    watcher     *fsnotify.Watcher
    current     *Config
    mu          sync.RWMutex
    onChange    []func(*Config)      // 变更回调
    debounce    time.Duration        // 防抖间隔
}

// NewConfigWatcher 创建配置监听器
func NewConfigWatcher(configPath string, initial *Config) (*ConfigWatcher, error) {
    w, err := fsnotify.NewWatcher()
    if err != nil {
        return nil, err
    }

    cw := &ConfigWatcher{
        configPath: configPath,
        watcher:    w,
        current:    initial,
        debounce:   2 * time.Second, // 2s 防抖，防止编辑器保存时的多次触发
    }

    if err := w.Add(configPath); err != nil {
        return nil, err
    }

    return cw, nil
}

// OnChange 注册配置变更回调
func (cw *ConfigWatcher) OnChange(fn func(*Config)) {
    cw.onChange = append(cw.onChange, fn)
}

// Current 获取当前配置（线程安全）
func (cw *ConfigWatcher) Current() *Config {
    cw.mu.RLock()
    defer cw.mu.RUnlock()
    return cw.current
}

// Watch 开始监听（阻塞，建议 go cw.Watch(ctx)）
func (cw *ConfigWatcher) Watch(ctx context.Context) {
    var debounceTimer *time.Timer

    for {
        select {
        case <-ctx.Done():
            cw.watcher.Close()
            return

        case event := <-cw.watcher.Events:
            if event.Op&(fsnotify.Write|fsnotify.Create) == 0 {
                continue
            }

            // 防抖：2s 内多次修改只触发一次重载
            if debounceTimer != nil {
                debounceTimer.Stop()
            }
            debounceTimer = time.AfterFunc(cw.debounce, func() {
                cw.reload()
            })

        case err := <-cw.watcher.Errors:
            log.Error().Err(err).Msg("config watcher error")
        }
    }
}

func (cw *ConfigWatcher) reload() {
    log.Info().Str("path", cw.configPath).Msg("config file changed, reloading")

    newCfg, err := Load(cw.configPath)
    if err != nil {
        log.Error().Err(err).Msg("failed to reload config, keeping current")
        return
    }

    // 验证新配置
    if err := newCfg.Validate(); err != nil {
        log.Error().Err(err).Msg("new config validation failed, keeping current")
        return
    }

    cw.mu.Lock()
    oldCfg := cw.current
    cw.current = newCfg
    cw.mu.Unlock()

    log.Info().
        Str("old_version", oldCfg.Version).
        Str("new_version", newCfg.Version).
        Msg("config reloaded successfully")

    // 触发回调
    for _, fn := range cw.onChange {
        go fn(newCfg)
    }
}
```

> **WHY 防抖 2 秒？**
> - 文本编辑器保存文件时通常会触发多次文件系统事件（write → truncate → write）
> - 没有防抖会导致在配置文件不完整时尝试解析，产生错误
> - 2 秒足以覆盖大多数编辑器的保存行为

> **WHY 验证新配置后才替换？**
> - 配置错误（如 YAML 语法错误、端口冲突）不应该导致服务异常
> - "加载失败保持旧配置"是 fail-safe 策略——最差情况是配置没更新，不会是服务挂掉

---

## 6. 测试

```go
// go/internal/tools/hdfs/namenode_status_test.go
package hdfs_test

import (
    "context"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/mock"

    "github.com/yourorg/aiops-mcp/internal/tools/hdfs"
)

type MockHDFSClient struct {
    mock.Mock
}

func (m *MockHDFSClient) GetNameNodeStatus(ctx context.Context, target string) (*hdfs.NameNodeStatus, error) {
    args := m.Called(ctx, target)
    return args.Get(0).(*hdfs.NameNodeStatus), args.Error(1)
}

func TestNameNodeStatusTool_Execute(t *testing.T) {
    mockClient := new(MockHDFSClient)
    mockClient.On("GetNameNodeStatus", mock.Anything, "active").Return(&hdfs.NameNodeStatus{
        Hostname:             "nn1.example.com",
        HAState:              "active",
        HeapUsed:             8 * 1024 * 1024 * 1024, // 8GB
        HeapMax:              16 * 1024 * 1024 * 1024, // 16GB
        SafeMode:             false,
        RPCQueueLen:          5,
        RPCLatencyMs:         2.5,
        UnderReplicatedBlocks: 0,
        CorruptBlocks:         0,
        MissingBlocks:         0,
    }, nil)

    tool := hdfs.NewNameNodeStatusTool(mockClient)

    result, err := tool.Execute(context.Background(), map[string]interface{}{
        "namenode": "active",
    })

    assert.NoError(t, err)
    assert.NotNil(t, result)
    assert.Len(t, result.Content, 1)
    assert.Contains(t, result.Content[0].Text, "nn1.example.com")
    assert.Contains(t, result.Content[0].Text, "🟢 active")
    assert.Contains(t, result.Content[0].Text, "50.0%") // 8/16 GB
    assert.NotContains(t, result.Content[0].Text, "⚠️") // 无异常
}

func TestNameNodeStatusTool_HighHeapAlert(t *testing.T) {
    mockClient := new(MockHDFSClient)
    mockClient.On("GetNameNodeStatus", mock.Anything, "active").Return(&hdfs.NameNodeStatus{
        Hostname:             "nn1.example.com",
        HAState:              "active",
        HeapUsed:             15 * 1024 * 1024 * 1024, // 15GB
        HeapMax:              16 * 1024 * 1024 * 1024, // 16GB → 93.75%
        SafeMode:             false,
        RPCQueueLen:          5,
        RPCLatencyMs:         2.5,
        UnderReplicatedBlocks: 100,
        CorruptBlocks:         3,
    }, nil)

    tool := hdfs.NewNameNodeStatusTool(mockClient)
    result, err := tool.Execute(context.Background(), map[string]interface{}{})

    assert.NoError(t, err)
    assert.Contains(t, result.Content[0].Text, "🔴") // 93.75% > 90%
    assert.Contains(t, result.Content[0].Text, "⚠️ 堆内存")
    assert.Contains(t, result.Content[0].Text, "🚨 3 个损坏块")
}

func TestNameNodeStatusTool_SafeMode(t *testing.T) {
    mockClient := new(MockHDFSClient)
    mockClient.On("GetNameNodeStatus", mock.Anything, "active").Return(&hdfs.NameNodeStatus{
        Hostname:       "nn1.example.com",
        HAState:        "active",
        HeapUsed:       4 * 1024 * 1024 * 1024,
        HeapMax:        16 * 1024 * 1024 * 1024,
        SafeMode:       true,
        SafeModeReason: "Resources are low on NN",
    }, nil)

    tool := hdfs.NewNameNodeStatusTool(mockClient)
    result, err := tool.Execute(context.Background(), map[string]interface{}{})

    assert.NoError(t, err)
    assert.Contains(t, result.Content[0].Text, "🚨 是")
    assert.Contains(t, result.Content[0].Text, "SafeMode")
}

// === Registry 测试 ===

func TestRegistry_ListDefinitions(t *testing.T) {
    reg := protocol.NewRegistry()
    mockClient := new(MockHDFSClient)
    reg.Register(hdfs.NewNameNodeStatusTool(mockClient))
    reg.Register(hdfs.NewClusterOverviewTool(mockClient))

    defs := reg.ListDefinitions()
    assert.Len(t, defs, 2)
}

func TestRegistry_GetNonExistent(t *testing.T) {
    reg := protocol.NewRegistry()
    _, err := reg.Get("nonexistent_tool")
    assert.Error(t, err)
}
```

### 6.1 集成测试：完整 JSON-RPC 请求-响应

> **WHY 集成测试** — 单元测试验证了每个工具的逻辑，但没有覆盖 JSON-RPC 协议层。
> 集成测试模拟一个真实的 Client 发送 JSON-RPC 请求，验证整个链路：
> JSON 解析 → 路由 → 工具查找 → 中间件链 → 工具执行 → 响应序列化。

```go
// go/internal/protocol/handler_integration_test.go
package protocol_test

import (
    "context"
    "encoding/json"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

// mockTool 测试用的 mock 工具
type mockTool struct {
    name      string
    risk      protocol.RiskLevel
    execFunc  func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error)
}

func (m *mockTool) Name() string                       { return m.name }
func (m *mockTool) RiskLevel() protocol.RiskLevel      { return m.risk }
func (m *mockTool) Description() string                { return "mock tool for testing" }
func (m *mockTool) Schema() map[string]interface{}     { return map[string]interface{}{"type": "object"} }
func (m *mockTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
    return m.execFunc(ctx, params)
}

func TestHandler_ToolsList(t *testing.T) {
    registry := protocol.NewRegistry()
    registry.Register(&mockTool{name: "tool_a", risk: protocol.RiskNone})
    registry.Register(&mockTool{name: "tool_b", risk: protocol.RiskHigh})

    handler := protocol.NewHandler(registry, nil)

    // 发送 tools/list 请求
    req := `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`
    respBytes, err := handler.HandleRequest(context.Background(), []byte(req))
    require.NoError(t, err)

    var resp protocol.Response
    require.NoError(t, json.Unmarshal(respBytes, &resp))

    assert.Equal(t, "2.0", resp.JSONRPC)
    assert.Equal(t, float64(1), resp.ID) // JSON number
    assert.Nil(t, resp.Error)

    // 验证返回了两个工具定义
    result := resp.Result.(map[string]interface{})
    tools := result["tools"].([]interface{})
    assert.Len(t, tools, 2)
}

func TestHandler_ToolsCall_Success(t *testing.T) {
    registry := protocol.NewRegistry()
    registry.Register(&mockTool{
        name: "test_tool",
        risk: protocol.RiskNone,
        execFunc: func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            return &protocol.ToolResult{
                Content: []protocol.ContentBlock{{Type: "text", Text: "hello from test_tool"}},
            }, nil
        },
    })

    handler := protocol.NewHandler(registry, nil)

    req := `{
        "jsonrpc": "2.0",
        "id": 42,
        "method": "tools/call",
        "params": {"name": "test_tool", "arguments": {"key": "value"}}
    }`

    respBytes, err := handler.HandleRequest(context.Background(), []byte(req))
    require.NoError(t, err)

    var resp protocol.Response
    require.NoError(t, json.Unmarshal(respBytes, &resp))

    assert.Equal(t, float64(42), resp.ID)
    assert.Nil(t, resp.Error)

    // 验证工具执行结果
    result := resp.Result.(map[string]interface{})
    content := result["content"].([]interface{})
    assert.Len(t, content, 1)
    block := content[0].(map[string]interface{})
    assert.Equal(t, "text", block["type"])
    assert.Equal(t, "hello from test_tool", block["text"])
}

func TestHandler_ToolsCall_NotFound(t *testing.T) {
    registry := protocol.NewRegistry()
    handler := protocol.NewHandler(registry, nil)

    req := `{
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "nonexistent", "arguments": {}}
    }`

    respBytes, err := handler.HandleRequest(context.Background(), []byte(req))
    require.NoError(t, err)

    var resp protocol.Response
    require.NoError(t, json.Unmarshal(respBytes, &resp))

    // 工具不存在应返回错误
    assert.NotNil(t, resp.Error)
    assert.Equal(t, -32602, resp.Error.Code)
    assert.Contains(t, resp.Error.Message, "not found")
}

func TestHandler_InvalidJSON(t *testing.T) {
    registry := protocol.NewRegistry()
    handler := protocol.NewHandler(registry, nil)

    respBytes, err := handler.HandleRequest(context.Background(), []byte("not json"))
    require.NoError(t, err)

    var resp protocol.Response
    require.NoError(t, json.Unmarshal(respBytes, &resp))

    assert.NotNil(t, resp.Error)
    assert.Equal(t, -32700, resp.Error.Code)  // Parse error
}

func TestHandler_MethodNotFound(t *testing.T) {
    registry := protocol.NewRegistry()
    handler := protocol.NewHandler(registry, nil)

    req := `{"jsonrpc":"2.0","id":1,"method":"unknown/method"}`
    respBytes, err := handler.HandleRequest(context.Background(), []byte(req))
    require.NoError(t, err)

    var resp protocol.Response
    require.NoError(t, json.Unmarshal(respBytes, &resp))

    assert.NotNil(t, resp.Error)
    assert.Equal(t, -32601, resp.Error.Code)  // Method not found
}
```

### 6.2 性能基准测试

> **WHY 基准测试** — MCP Server 在诊断链路上，是影响 Agent 响应速度的关键路径。
> 我们需要确保协议层开销可忽略（< 1ms），工具调用的延迟主要来自后端 API 而非 MCP Server 自身。

```go
// go/internal/protocol/handler_bench_test.go
package protocol_test

import (
    "context"
    "testing"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

func BenchmarkHandler_ToolsList(b *testing.B) {
    registry := protocol.NewRegistry()
    // 注册 42 个 mock 工具（模拟真实场景）
    for i := 0; i < 42; i++ {
        registry.Register(&mockTool{
            name: fmt.Sprintf("tool_%d", i),
            risk: protocol.RiskNone,
        })
    }

    handler := protocol.NewHandler(registry, nil)
    req := []byte(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`)
    ctx := context.Background()

    b.ResetTimer()
    b.ReportAllocs()

    for i := 0; i < b.N; i++ {
        _, _ = handler.HandleRequest(ctx, req)
    }
    // 预期: ~1-5μs/op, 0-2 allocs/op
}

func BenchmarkHandler_ToolsCall_NoMiddleware(b *testing.B) {
    registry := protocol.NewRegistry()
    registry.Register(&mockTool{
        name: "fast_tool",
        risk: protocol.RiskNone,
        execFunc: func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            return &protocol.ToolResult{
                Content: []protocol.ContentBlock{{Type: "text", Text: "ok"}},
            }, nil
        },
    })

    handler := protocol.NewHandler(registry, nil)
    req := []byte(`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"fast_tool","arguments":{}}}`)
    ctx := context.Background()

    b.ResetTimer()
    b.ReportAllocs()

    for i := 0; i < b.N; i++ {
        _, _ = handler.HandleRequest(ctx, req)
    }
    // 预期: ~5-20μs/op（JSON 序列化/反序列化开销）
}

func BenchmarkHandler_ToolsCall_WithMiddleware(b *testing.B) {
    registry := protocol.NewRegistry()
    registry.Register(&mockTool{
        name: "tool_with_mw",
        risk: protocol.RiskNone,
        execFunc: func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            return &protocol.ToolResult{
                Content: []protocol.ContentBlock{{Type: "text", Text: "ok"}},
            }, nil
        },
    })

    // 模拟 8 层中间件（与生产环境一致）
    mw := func(tool protocol.Tool, next protocol.ToolHandler) protocol.ToolHandler {
        return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            // 模拟中间件开销（logging、tracing、audit等）
            return next(ctx, params)
        }
    }

    handler := protocol.NewHandler(registry, mw)
    req := []byte(`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"tool_with_mw","arguments":{}}}`)
    ctx := context.Background()

    b.ResetTimer()
    b.ReportAllocs()

    for i := 0; i < b.N; i++ {
        _, _ = handler.HandleRequest(ctx, req)
    }
    // 预期: ~10-50μs/op（中间件链开销）
}
```

### 6.3 Kafka Consumer Lag 工具 Mock 测试

> **WHY 用 Mock 测试 Kafka 工具** — Kafka Admin API 需要连接真实 Broker，
> 集成测试需要 Docker Compose 启动 Kafka 集群，CI 环境不一定可用。
> Mock 测试覆盖了工具的核心逻辑：offset 计算、排序、格式化输出。

```go
// go/internal/tools/kafka/consumer_lag_test.go
package kafka_test

import (
    "context"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
    "github.com/stretchr/testify/mock"

    "github.com/yourorg/aiops-mcp/internal/tools/kafka"
)

type MockKafkaAdmin struct {
    mock.Mock
}

func (m *MockKafkaAdmin) ListConsumerGroups() (map[string]string, error) {
    args := m.Called()
    return args.Get(0).(map[string]string), args.Error(1)
}

func (m *MockKafkaAdmin) ListConsumerGroupOffsets(group string, topicPartitions map[string][]int32) (*sarama.OffsetFetchResponse, error) {
    args := m.Called(group, topicPartitions)
    return args.Get(0).(*sarama.OffsetFetchResponse), args.Error(1)
}

func (m *MockKafkaAdmin) GetOffset(topic string, partition int32, time int64) (int64, error) {
    args := m.Called(topic, partition, time)
    return args.Get(0).(int64), args.Error(1)
}

func TestConsumerLagTool_SingleGroup(t *testing.T) {
    mockAdmin := new(MockKafkaAdmin)

    // Mock: group "etl-pipeline" 消费 topic "events"，partition 0-2
    offsets := &sarama.OffsetFetchResponse{
        Blocks: map[string]map[int32]*sarama.OffsetFetchResponseBlock{
            "events": {
                0: {Offset: 1000},
                1: {Offset: 2000},
                2: {Offset: 500},
            },
        },
    }
    mockAdmin.On("ListConsumerGroupOffsets", "etl-pipeline", mock.Anything).Return(offsets, nil)

    // Mock: latest offsets
    mockAdmin.On("GetOffset", "events", int32(0), sarama.OffsetNewest).Return(int64(1500), nil)  // lag=500
    mockAdmin.On("GetOffset", "events", int32(1), sarama.OffsetNewest).Return(int64(2000), nil)  // lag=0
    mockAdmin.On("GetOffset", "events", int32(2), sarama.OffsetNewest).Return(int64(3000), nil)  // lag=2500

    tool := kafka.NewConsumerLagToolWithAdmin(mockAdmin)
    result, err := tool.Execute(context.Background(), map[string]interface{}{
        "consumer_group": "etl-pipeline",
    })

    require.NoError(t, err)
    text := result.Content[0].Text

    // 验证总 lag = 500 + 0 + 2500 = 3000
    assert.Contains(t, text, "total lag: 3000")
    // 验证 partition 级别详情
    assert.Contains(t, text, "events[0]")
    assert.Contains(t, text, "lag=500")
    assert.Contains(t, text, "events[2]")
    assert.Contains(t, text, "lag=2500")
    // lag=0 的分区不应该出现在详情中
    assert.NotContains(t, text, "lag=0")
}

func TestConsumerLagTool_TopN(t *testing.T) {
    mockAdmin := new(MockKafkaAdmin)

    // 5 个消费者组
    groups := map[string]string{
        "group-a": "consumer", "group-b": "consumer", "group-c": "consumer",
        "group-d": "consumer", "group-e": "consumer",
    }
    mockAdmin.On("ListConsumerGroups").Return(groups, nil)

    // 每个组一个 partition，lag 分别为 100, 500, 200, 1000, 50
    for group, lag := range map[string]int64{"group-a": 100, "group-b": 500, "group-c": 200, "group-d": 1000, "group-e": 50} {
        offsets := &sarama.OffsetFetchResponse{
            Blocks: map[string]map[int32]*sarama.OffsetFetchResponseBlock{
                "topic-x": {0: {Offset: 0}},
            },
        }
        mockAdmin.On("ListConsumerGroupOffsets", group, mock.Anything).Return(offsets, nil)
        mockAdmin.On("GetOffset", "topic-x", int32(0), sarama.OffsetNewest).Return(lag, nil)
    }

    tool := kafka.NewConsumerLagToolWithAdmin(mockAdmin)
    result, err := tool.Execute(context.Background(), map[string]interface{}{
        "top_n": float64(3),
    })

    require.NoError(t, err)
    text := result.Content[0].Text

    // top 3 应该是 group-d(1000), group-b(500), group-c(200)
    assert.Contains(t, text, "group-d")
    assert.Contains(t, text, "group-b")
    assert.Contains(t, text, "group-c")
    // group-a 和 group-e 不应该出现
    assert.NotContains(t, text, "group-e")
}
```

### 6.4 Ops 高风险工具安全拦截测试

> **WHY 测试安全拦截** — 高风险工具的 HITL 审批拦截是安全关键路径。
> 如果中间件 bug 导致高风险工具跳过审批直接执行，后果是灾难性的。
> 这类测试需要特别关注边界条件：中间件缺失、风险等级修改、审批超时等。

```go
// go/internal/middleware/risk_assessment_test.go
package middleware_test

import (
    "context"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"

    "github.com/yourorg/aiops-mcp/internal/middleware"
    "github.com/yourorg/aiops-mcp/internal/protocol"
)

func TestRiskAssessment_LowRisk_AutoApproved(t *testing.T) {
    executed := false
    tool := &mockTool{name: "safe_tool", risk: protocol.RiskNone}
    handler := middleware.RiskAssessmentMiddleware(tool, func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
        executed = true
        return &protocol.ToolResult{
            Content: []protocol.ContentBlock{{Type: "text", Text: "ok"}},
        }, nil
    })

    result, err := handler(context.Background(), map[string]interface{}{})
    require.NoError(t, err)
    assert.True(t, executed, "low risk tool should be auto-approved")
    assert.Equal(t, "ok", result.Content[0].Text)
}

func TestRiskAssessment_HighRisk_Blocked(t *testing.T) {
    executed := false
    tool := &mockTool{name: "ops_restart_service", risk: protocol.RiskHigh}
    handler := middleware.RiskAssessmentMiddleware(tool, func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
        executed = true
        return &protocol.ToolResult{
            Content: []protocol.ContentBlock{{Type: "text", Text: "restarted"}},
        }, nil
    })

    result, err := handler(context.Background(), map[string]interface{}{
        "service": "hdfs-namenode",
        "mode":    "force",
    })

    // 高风险工具应该被拦截（在测试中 HITL 不可用 → 返回错误）
    require.NoError(t, err)          // 不应该返回 Go error
    assert.False(t, executed, "high risk tool should NOT be executed without approval")
    assert.True(t, result.IsError)   // 应该在 ToolResult 中标记错误
    assert.Contains(t, result.Content[0].Text, "审批")
}

func TestRiskAssessment_CriticalRisk_DualApproval(t *testing.T) {
    tool := &mockTool{name: "ops_decommission_node", risk: protocol.RiskCritical}
    handler := middleware.RiskAssessmentMiddleware(tool, func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
        return &protocol.ToolResult{
            Content: []protocol.ContentBlock{{Type: "text", Text: "decommissioned"}},
        }, nil
    })

    result, err := handler(context.Background(), map[string]interface{}{
        "node": "datanode05",
    })

    require.NoError(t, err)
    assert.True(t, result.IsError)
    assert.Contains(t, result.Content[0].Text, "审批")  // 需要双人审批
}
```

### 6.5 错误响应格式测试

> **WHY 测试错误格式** — MCP Client（Python Agent）需要正确解析错误响应来决定后续策略。
> 如果错误格式不符合 JSON-RPC 2.0 规范，Client 侧会 crash 或进入未知状态。

```go
// go/internal/protocol/error_format_test.go
package protocol_test

import (
    "context"
    "encoding/json"
    "fmt"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

func TestErrorResponse_JSONRPCFormat(t *testing.T) {
    tests := []struct {
        name     string
        input    string
        wantCode int
        wantMsg  string
    }{
        {
            name:     "ParseError_InvalidJSON",
            input:    `{not json}`,
            wantCode: -32700,
            wantMsg:  "Parse error",
        },
        {
            name:     "MethodNotFound",
            input:    `{"jsonrpc":"2.0","id":1,"method":"invalid/method"}`,
            wantCode: -32601,
            wantMsg:  "Method not found",
        },
        {
            name:     "InvalidParams_MissingName",
            input:    `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}`,
            wantCode: -32602,
            wantMsg:  "",  // 具体消息不重要，重要是 code 正确
        },
        {
            name:     "ToolNotFound",
            input:    `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ghost_tool","arguments":{}}}`,
            wantCode: -32602,
            wantMsg:  "not found",
        },
    }

    registry := protocol.NewRegistry()
    handler := protocol.NewHandler(registry, nil)

    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            respBytes, err := handler.HandleRequest(context.Background(), []byte(tt.input))
            require.NoError(t, err)

            var resp protocol.Response
            require.NoError(t, json.Unmarshal(respBytes, &resp))

            // 验证 JSON-RPC 2.0 格式
            assert.Equal(t, "2.0", resp.JSONRPC)
            assert.NotNil(t, resp.Error, "should have error for: %s", tt.name)
            assert.Equal(t, tt.wantCode, resp.Error.Code)
            if tt.wantMsg != "" {
                assert.Contains(t, resp.Error.Message, tt.wantMsg)
            }
        })
    }
}

func TestToolResult_ErrorVsGoError(t *testing.T) {
    // 区分：工具业务错误（ToolResult.IsError=true）和 Go 运行时错误
    registry := protocol.NewRegistry()

    // 工具返回业务错误（如"前置检查失败"）
    registry.Register(&mockTool{
        name: "biz_error_tool",
        risk: protocol.RiskNone,
        execFunc: func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            return &protocol.ToolResult{
                Content: []protocol.ContentBlock{{Type: "text", Text: "❌ 磁盘空间不足"}},
                IsError: true,  // 业务错误，不是系统错误
            }, nil
        },
    })

    // 工具返回 Go error（如"网络不可达"）
    registry.Register(&mockTool{
        name: "sys_error_tool",
        risk: protocol.RiskNone,
        execFunc: func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            return nil, fmt.Errorf("connection refused: dial tcp 10.0.0.1:9870")
        },
    })

    handler := protocol.NewHandler(registry, nil)

    // 业务错误：应该在 result 中（不是 response error）
    req1 := `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"biz_error_tool","arguments":{}}}`
    resp1Bytes, _ := handler.HandleRequest(context.Background(), []byte(req1))
    var resp1 protocol.Response
    json.Unmarshal(resp1Bytes, &resp1)

    assert.Nil(t, resp1.Error, "business error should NOT be in response.error")
    result1 := resp1.Result.(map[string]interface{})
    assert.True(t, result1["isError"].(bool))
    content1 := result1["content"].([]interface{})
    block1 := content1[0].(map[string]interface{})
    assert.Contains(t, block1["text"], "磁盘空间不足")

    // 系统错误：也应该包装在 result 中（MCP 规范）
    req2 := `{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"sys_error_tool","arguments":{}}}`
    resp2Bytes, _ := handler.HandleRequest(context.Background(), []byte(req2))
    var resp2 protocol.Response
    json.Unmarshal(resp2Bytes, &resp2)

    assert.Nil(t, resp2.Error, "system error should be wrapped in result, not response.error")
    result2 := resp2.Result.(map[string]interface{})
    assert.True(t, result2["isError"].(bool))
}
```

> **WHY 区分业务错误和系统错误？**
> - 业务错误（如"磁盘不足"）是有价值的诊断信息，Agent 应该分析错误内容并调整策略
> - 系统错误（如"连接拒绝"）说明 MCP Server 和后端之间的通信有问题
> - 两者都通过 `ToolResult.IsError=true` 返回（符合 MCP 规范），但 Go error 需要额外包装
> - **JSON-RPC level error**（-327xx）只用于协议层错误（解析、路由），不用于工具执行错误

---

## 8. 端到端场景

> 本节通过三个完整场景展示 MCP Server 在真实诊断链路中的工作方式。
> 每个场景覆盖从 Python Agent 发起调用到最终响应的全链路。

### 8.1 场景 1：Python Agent 调用 hdfs_namenode_status 完整链路

> **场景描述**：收到告警"HDFS 写入延迟升高"，Diagnostic Agent 调用 `hdfs_namenode_status` 检查 NameNode 状态。

```
                        完整调用链路
                        
[告警: HDFS write latency > 500ms]
         │
         ▼
┌─────────────────────────────────┐
│  Python Diagnostic Agent        │
│  (决定调用 hdfs_namenode_status) │
└──────────┬──────────────────────┘
           │ HTTP POST /mcp
           │ JSON-RPC 2.0
           ▼
┌─────────────────────────────────┐
│  MCP Gateway (Go Fiber)         │
│  1. 解析 JSON-RPC               │
│  2. 提取 trace context          │
│     (W3C traceparent header)    │
│  3. 路由到 hdfs-mcp             │
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│  中间件链 (8 layers)             │
│  ① OTel Tracing → 创建 span    │
│  ② Audit Log → 记录调用         │
│  ③ Cache → 未命中               │
│  ④ Input Validation → 通过      │
│  ⑤ Risk Assessment → Level 0   │
│  ⑥ Rate Limiter → 通过          │
│  ⑦ Circuit Breaker → closed    │
│  ⑧ Timeout → 10s deadline      │
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│  NameNodeStatusTool.Execute()   │
│  1. 确定 target = "active"      │
│  2. HTTP GET NN JMX API         │
│     (带 timeout context)        │
│  3. 解析 JMX beans              │
│  4. 构建 Markdown 输出          │
│  5. 内嵌异常检测                │
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│  响应回传                        │
│  ① Cache → 写入缓存 (TTL=30s)  │
│  ② Audit → 记录结果             │
│  ③ Tracing → 关闭 span         │
│  ④ 序列化 JSON-RPC 响应         │
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│  Python Agent 收到响应           │
│  解析 ToolResult:               │
│  "堆内存 92% 🔴, RPC 队列 120"  │
│  → 决定继续调用 hdfs_block_report│
└─────────────────────────────────┘
```

**Trace Context Propagation（分布式追踪传播）：**

```go
// Python Agent 发送请求时注入 trace context
// HTTP Header: traceparent: 00-abc123...-def456...-01

// MCP Gateway 提取 trace context
func (h *Handler) HandleHTTPRequest(c *fiber.Ctx) error {
    // 从 HTTP header 提取 W3C trace context
    ctx := otel.GetTextMapPropagator().Extract(
        c.UserContext(),
        propagation.HeaderCarrier(c.GetReqHeaders()),
    )

    // 创建子 span
    ctx, span := h.tracer.Start(ctx, "mcp.tools.call",
        trace.WithAttributes(
            attribute.String("mcp.tool.name", toolName),
            attribute.String("mcp.server", "hdfs-mcp"),
        ),
    )
    defer span.End()

    // 工具执行时传递 ctx → NN JMX 调用也会带上 trace
    result, err := handler.HandleRequest(ctx, c.Body())
    // ...
}
```

> **WHY 传播 trace context？**
> - 一个诊断请求可能调用 5-10 个工具，每个工具调用 1-3 个后端 API
> - 没有 trace propagation，就无法在 Grafana Tempo 中看到完整的调用链
> - 当某个工具调用慢时，trace 能精确定位是 MCP 中间件慢还是后端 API 慢

### 8.2 场景 2：高风险 ops_restart_service 被拦截 → HITL 审批 → 执行

> **场景描述**：Agent 分析后认为需要重启 DataNode 进程，调用 `ops_restart_service`。
> 这是 RiskLevel=3 的高风险操作，必须经过 HITL 审批。

```
[Agent 决策: 需要重启 datanode03]
         │
         ▼
┌─────────────────────────────────┐
│  tools/call: ops_restart_service│
│  params:                        │
│    service: "hdfs-datanode"     │
│    mode: "rolling"              │
│    target_nodes: ["dn03"]      │
│    reason: "OOM 后堆内存未回落" │
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────────────┐
│  中间件链                                │
│  ① Tracing ✓                            │
│  ② Audit ✓ (记录: 谁、什么时候、做什么)  │
│  ③ Cache → SKIP（写操作不缓存）          │
│  ④ Validation ✓ (service ∈ enum)         │
│  ⑤ Risk Assessment → Level 3 ⛔          │
│     ├── 创建审批请求                      │
│     ├── 推送到 Redis 审批队列             │
│     └── 企微 Bot 发送审批卡片 ──────────┐ │
│                                         │ │
│  ⏳ 等待审批（最长 5 分钟）              │ │
│                                         │ │
│  ┌──────────────────────────────────────┘ │
│  │  企微 Bot 审批卡片:                    │
│  │  ┌──────────────────────┐              │
│  │  │ 🔴 高风险操作审批     │              │
│  │  │                      │              │
│  │  │ 工具: restart_service │              │
│  │  │ 服务: hdfs-datanode   │              │
│  │  │ 节点: dn03            │              │
│  │  │ 方式: rolling         │              │
│  │  │ 原因: OOM 后堆内存    │              │
│  │  │       未回落           │              │
│  │  │                      │              │
│  │  │ [✅ 批准] [❌ 拒绝]   │              │
│  │  └──────────────────────┘              │
│  │                                        │
│  │  SRE 点击 [✅ 批准]                    │
│  │  → Redis 写入 approval = true          │
│  └────────────────────────────────────────┘
│                                            
│  ⑤ Risk Assessment → 审批通过 ✅           
│  ⑥ Rate Limiter ✓                         
│  ⑦ Circuit Breaker ✓                      
│  ⑧ Timeout → 120s (操作类超时更长)        
└──────────┬────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│  RestartServiceTool.Execute()   │
│  1. PreflightCheck → 通过       │
│  2. CreateSnapshot → snap_001   │
│  3. RollingRestart → 成功       │
│  4. 返回结果                    │
└──────────┬──────────────────────┘
           │
           ▼
┌─────────────────────────────────┐
│  ToolResult:                    │
│  "✅ hdfs-datanode 已滚动重启   │
│   耗时: 45s                     │
│   影响节点: 1                   │
│   快照ID: snap_001"             │
└─────────────────────────────────┘
```

> **WHY 写操作不走缓存？**
> - 写操作的结果不可缓存（每次执行的结果不同）
> - 如果缓存了"重启成功"的结果，第二次调用会直接返回缓存而不执行重启
> - 中间件通过检查 `tool.RiskLevel() > RiskNone` 来跳过缓存

> **WHY 操作类超时 120s 而不是标准的 30s？**
> - 滚动重启需要逐个节点操作，每个节点可能需要 30-60s
> - 包含 PreflightCheck 和 CreateSnapshot 的前置步骤
> - 超时太短会导致操作做到一半被中断，状态不一致

### 8.3 场景 3：Kafka 工具超时 → 熔断器打开 → 降级响应

> **场景描述**：Kafka 集群网络分区，`kafka_consumer_lag` 工具超时，
> 触发熔断器打开，后续调用直接返回降级响应而非继续超时。

```
时间线:

T+0s    Agent 调用 kafka_consumer_lag
        → 中间件: Circuit Breaker = CLOSED ✅
        → Kafka Admin API 调用...
        → 15s 超时 ⏰
        → 返回: Error "context deadline exceeded"
        → Circuit Breaker: failure_count = 1

T+20s   Agent 重试 kafka_consumer_lag
        → Kafka Admin API 调用...
        → 15s 超时 ⏰
        → Circuit Breaker: failure_count = 2

... 连续 5 次超时 ...

T+120s  Circuit Breaker: failure_count = 5 → threshold reached
        → STATE: CLOSED → OPEN 🔴
        → 开始 30s 冷却期

T+125s  Agent 调用 kafka_consumer_lag
        → 中间件: Circuit Breaker = OPEN 🔴
        → 直接返回降级响应（不调用后端）:
        ┌─────────────────────────────────────┐
        │ ToolResult (IsError=true):           │
        │                                      │
        │ ⚡ 熔断器已打开                       │
        │ 工具 kafka_consumer_lag 暂时不可用    │
        │ 原因: 连续 5 次调用失败               │
        │ 最后错误: context deadline exceeded   │
        │ 恢复时间: 约 25s 后尝试半开           │
        │                                      │
        │ 降级建议:                             │
        │ - 使用 query_metrics 查询             │
        │   kafka_consumer_lag_seconds 指标     │
        │ - 检查 Kafka 集群网络连通性           │
        └─────────────────────────────────────┘
        → Agent 收到降级响应，改用 query_metrics 工具

T+150s  Circuit Breaker: 冷却结束
        → STATE: OPEN → HALF-OPEN 🟡
        → 放行 1 个请求作为探针

T+152s  Agent 调用 kafka_consumer_lag
        → 中间件: Circuit Breaker = HALF-OPEN
        → 放行到 Kafka Admin API
        → 成功! (Kafka 网络恢复)
        → Circuit Breaker: STATE = CLOSED ✅
        → 后续调用正常
```

```go
// 熔断器降级响应实现
func CircuitBreakerMiddleware(tool protocol.Tool, next protocol.ToolHandler) protocol.ToolHandler {
    cb := getCircuitBreaker(tool.Name()) // 每个工具独立的熔断器

    return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
        // 检查熔断器状态
        if !cb.Allow() {
            // 熔断器打开 → 返回降级响应
            return &protocol.ToolResult{
                Content: []protocol.ContentBlock{{
                    Type: "text",
                    Text: fmt.Sprintf(
                        "⚡ 熔断器已打开\n"+
                            "工具 %s 暂时不可用\n"+
                            "原因: 连续 %d 次调用失败\n"+
                            "最后错误: %s\n"+
                            "恢复时间: 约 %ds 后尝试半开\n\n"+
                            "降级建议:\n%s",
                        tool.Name(),
                        cb.FailureCount(),
                        cb.LastError(),
                        cb.TimeToHalfOpen(),
                        getDegradationSuggestion(tool.Name()),
                    ),
                }},
                IsError: true,
            }, nil
        }

        // 执行工具
        result, err := next(ctx, params)
        if err != nil {
            cb.RecordFailure(err)
            return result, err
        }
        cb.RecordSuccess()
        return result, nil
    }
}

// getDegradationSuggestion 根据工具名返回降级建议
func getDegradationSuggestion(toolName string) string {
    suggestions := map[string]string{
        "kafka_consumer_lag": "- 使用 query_metrics 查询 kafka_consumer_lag_seconds 指标\n" +
            "- 检查 Kafka 集群网络连通性",
        "hdfs_namenode_status": "- 使用 query_metrics 查询 hadoop_namenode_* 指标\n" +
            "- 检查 NameNode JMX 端口连通性",
        "es_cluster_health": "- 使用 query_metrics 查询 elasticsearch_cluster_health_* 指标\n" +
            "- 检查 ES 集群网络连通性",
    }
    if s, ok := suggestions[toolName]; ok {
        return s
    }
    return "- 稍后重试\n- 检查后端服务连通性"
}
```

> **WHY 每个工具独立的熔断器而不是全局熔断？**
> - Kafka 不可用不应该影响 HDFS 工具的调用
> - 全局熔断会导致"一个组件故障，所有诊断能力丧失"
> - 独立熔断让 Agent 可以在 Kafka 熔断时仍然使用 HDFS、ES、YARN 工具

> **WHY 返回降级建议？**
> - Agent（LLM）需要知道"现在用什么替代方案"——不是所有信息来源都不可用
> - `query_metrics` 工具通过 Prometheus 获取预聚合指标，不直接依赖 Kafka Admin API
> - 降级建议让 Agent 能自动切换到替代数据源，而不是卡住等待

---

## 9. 设计决策总结

> 汇总 MCP Server 所有关键设计决策。面试时可以直接引用这个表格。

### 9.1 关键设计决策汇总表

| # | 决策 | 可选方案 | 我们的选择 | WHY |
|---|------|---------|-----------|-----|
| 1 | 通信协议 | JSON-RPC / gRPC / REST | JSON-RPC 2.0 | MCP 原生协议，Claude/Cursor 直接兼容 |
| 2 | 流式传输 | SSE / WebSocket | SSE | 单向流足够，HTTP LB 友好，企业防火墙不阻断 |
| 3 | 实现语言 | Go / Python / Rust | Go | 高并发、低延迟、单二进制部署、交叉编译 |
| 4 | 工具描述管理 | Server 侧 / Client 侧 | Server 侧 | 单一信息源，新增工具无需改 Python 代码 |
| 5 | 参数类型 | 强类型 struct / map[string]interface{} | map + JSON Schema | 灵活，减少样板代码，Schema 保证输入合法 |
| 6 | Server 拆分 | 8 个独立 Server / 1 个大 Server | 8 个独立 Server | 故障隔离、独立部署、独立扩缩容 |
| 7 | 风险评估位置 | 工具定义层 / 中间件层 | 工具定义层 | 工具开发者最了解风险，减少维护映射表 |
| 8 | 风险等级粒度 | 布尔 / 3级 / 5级 | 5 级 | 与 HITL 审批策略对齐（自动/通知/审批/双审批） |
| 9 | 超时策略 | 统一 / 差异化 | 差异化 | HDFS JMX 10s vs Kafka Admin 15s vs ES 5s |
| 10 | 连接管理 | 按需创建 / 连接池 | 连接池 | 减少握手延迟，控制后端连接数 |
| 11 | 传输模式 | HTTP only / stdio only / 双模式 | 双模式 | HTTP 用于生产，stdio 用于 Claude Desktop |
| 12 | 配置管理 | 静态 / 热更新 | 热更新 (fsnotify) | 不重启更新配置，减少诊断服务中断 |
| 13 | 熔断器粒度 | 全局 / 按 Server / 按工具 | 按工具 | 一个工具故障不影响其他工具 |
| 14 | 输出格式 | JSON / Markdown / 结构化文本 | Markdown | LLM 理解更准确，信息密度高，emoji 标注异常 |
| 15 | 错误处理 | Go error / ToolResult.IsError | 两层 | 协议层错误 vs 工具执行错误分离 |

### 9.2 WHY 用 Go 而不是 Python 实现 MCP Server

> 这是面试中最常被问到的决策之一。

| 维度 | Go | Python |
|------|-----|--------|
| **并发性能** | goroutine + channel，轻松处理数千并发工具调用 | asyncio 或线程池，GIL 限制 CPU 密集型操作 |
| **部署形态** | 单个静态链接二进制，`scp` 就能部署 | 需要 Python 环境 + 依赖 + 虚拟环境 |
| **内存占用** | ~20MB 基线 | ~80-150MB（Python runtime + 依赖） |
| **启动速度** | ~100ms | ~2-5s（import 依赖） |
| **交叉编译** | `GOOS=linux GOARCH=amd64 go build` | 不支持 |
| **类型安全** | 编译时检查 | 运行时发现 |
| **生态匹配** | 大数据组件的管理 API 多为 HTTP/REST，Go 的 net/http 原生支持 | 同样支持 |

> **WHY NOT Python？**
> - Python Agent 已经有很多 Python 代码了，MCP Server 用 Go 实现可以实现语言隔离——Agent 的 bug 不会影响工具层
> - MCP Server 是长驻进程，Go 的低内存占用在多实例部署时优势明显（8 个 Server × 20MB = 160MB vs 8 × 120MB = 960MB）
> - `sarama`（Kafka Go 客户端）比 `confluent-kafka-python` 更轻量，不需要 librdkafka C 依赖

> **WHY NOT Rust？**
> - Rust 的 MCP SDK 生态不如 Go 成熟（截至 2025 年）
> - 团队 Go 经验更丰富，Rust 的学习曲线会拖慢开发速度
> - Go 的性能对于"调用 HTTP API 并格式化输出"的场景已经足够，Rust 的零成本抽象在这里没有优势

### 9.3 WHY 每个组件一个 MCP Server 而非一个大 Server

```
方案 A：单一大 Server（42 个工具）       方案 B：8 个独立 Server（我们的选择）
┌──────────────────────────┐             ┌──────────┐ ┌──────────┐ ┌──────────┐
│       monolith-mcp       │             │ hdfs-mcp │ │kafka-mcp │ │  es-mcp  │
│  42 tools                │             │ 8 tools  │ │ 6 tools  │ │ 6 tools  │
│  所有后端依赖            │             └──────────┘ └──────────┘ └──────────┘
│  一个进程                │             ┌──────────┐ ┌──────────┐ ┌──────────┐
│  一份配置                │             │ yarn-mcp │ │ log-mcp  │ │  ops-mcp │
└──────────────────────────┘             └──────────┘ └──────────┘ └──────────┘
```

| 维度 | 单一大 Server | 8 个独立 Server |
|------|-------------|----------------|
| **故障隔离** | ❌ Kafka 连接泄漏可能 OOM 整个进程 | ✅ kafka-mcp OOM 不影响其他 |
| **独立部署** | ❌ 改一个工具要重新部署所有工具 | ✅ 只部署改动的 Server |
| **独立扩缩容** | ❌ 统一扩缩容，浪费资源 | ✅ 高频使用的 Server 独立扩容 |
| **资源限制** | ❌ 无法为不同工具设置不同的 CPU/内存限制 | ✅ K8s 可以为每个 Server 设置 resource limit |
| **复杂度** | ✅ 一个进程好管理 | ⚠️ 8 个进程需要编排 |
| **通信开销** | ✅ 进程内调用 | ⚠️ 跨进程 HTTP |
| **共享资源** | ✅ 连接池共享 | ❌ 每个 Server 独立的连接池 |

> **WHY 选择独立 Server？**
> - **故障隔离是第一优先**：运维场景中，"一个组件故障导致所有诊断能力丧失"是不可接受的
> - ops-mcp 需要更严格的安全策略（独立的网络策略、RBAC），和只读工具混在一起不合适
> - 实际部署中，通过 MCP Gateway 统一入口，Agent 感知不到背后是 8 个 Server

> **跨进程通信开销可接受吗？**
> - MCP Gateway → 各 Server 的通信走内网 HTTP，延迟 < 1ms
> - 对比后端 API 的延迟（10-500ms），MCP 层的通信开销可以忽略
> - 如果未来性能真的成为瓶颈，可以退化为 monolith 模式（代码结构已经支持）

---

## 7. 与其他模块集成

| 上游 | 说明 |
|------|------|
| 10-MCP 中间件 | 每个工具调用经过 8 层中间件（追踪/审计/缓存/校验/风控/限流/熔断/超时） |
| 11-MCP 客户端 | Python 侧通过 MCPClient 发起 JSON-RPC 2.0 调用 |

| 工具 → 数据源 | 协议 | 说明 |
|--------------|------|------|
| hdfs-mcp | WebHDFS REST + JMX (9870) | NameNode 状态、块报告、DataNode 列表 |
| yarn-mcp | YARN REST (8088) | 集群指标、队列、应用 |
| kafka-mcp | Kafka Admin (sarama) | 消费者 Lag、Topic、ISR |
| es-mcp | ES REST (9200) | 集群健康、节点统计、分片 |
| metrics-mcp | PromQL API (9090) | 任意 Prometheus 指标查询 |
| log-mcp | ES Lucene (9200) | 日志搜索、模式分析 |
| ops-mcp | 各组件管理 API + SSH | 高风险操作（重启/扩缩容/退役） |

---

> **下一篇**：[10-MCP中间件链.md](./10-MCP中间件链.md) — 8 层洋葱模型中间件的完整 Go 实现。
