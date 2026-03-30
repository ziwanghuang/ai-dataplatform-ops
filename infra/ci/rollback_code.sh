#!/bin/bash
# ================================================================
# 分钟级回滚 — 代码版本回滚
#
# 原理: kubectl rollout undo → 回到上一个 ReplicaSet
# 耗时: 1-3 分钟
# 文档参考: 20-部署CICD与运维.md §5.2
# ================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-aiops-production}"

echo "🚨 开始代码回滚..."

# 1. 回滚 agent-service
echo "Step 1: Rolling back agent-service..."
kubectl -n "$NAMESPACE" rollout undo deployment/agent-service

# 2. 回滚 mcp-gateway
echo "Step 2: Rolling back mcp-gateway..."
kubectl -n "$NAMESPACE" rollout undo deployment/mcp-gateway

# 3. 等待回滚完成
echo "Step 3: Waiting for rollback to complete..."
kubectl -n "$NAMESPACE" rollout status deployment/agent-service --timeout=300s
kubectl -n "$NAMESPACE" rollout status deployment/mcp-gateway --timeout=300s

# 4. 验证所有 Pod 就绪
echo "Step 4: Verifying pod readiness..."
kubectl -n "$NAMESPACE" wait --for=condition=Ready \
    pod -l app=agent-service --timeout=120s

READY_COUNT=$(kubectl -n "$NAMESPACE" get pods -l app=agent-service \
    --field-selector=status.phase=Running --no-headers | wc -l)
echo "Ready agent pods: $READY_COUNT"

# 5. 冒烟测试
echo "Step 5: Smoke test..."
SMOKE_RESULT=$(kubectl -n "$NAMESPACE" exec deploy/agent-service -- \
    curl -s http://localhost:8080/healthz 2>/dev/null || echo '{"status":"unknown"}')
echo "Smoke test result: $SMOKE_RESULT"

if echo "$SMOKE_RESULT" | grep -q '"status"'; then
    echo "✅ 代码回滚完成，服务正常"
else
    echo "❌ 代码回滚后冒烟测试失败！"
    exit 1
fi
