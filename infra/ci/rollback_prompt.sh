#!/bin/bash
# ================================================================
# 秒级回滚 — Prompt / Config / Model 切换
#
# 原理: 修改 ConfigMap → 触发 rolling restart → 新 Pod 加载新配置
# 耗时: ~30s
# 文档参考: 20-部署CICD与运维.md §5.1
# ================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-aiops-production}"
DEPLOYMENT="agent-service"

usage() {
    echo "用法:"
    echo "  回滚 Prompt:  $0 prompt v2.2"
    echo "  切换 LLM:     $0 model gpt-4o-2024-08-06"
    echo "  回滚 Config:  $0 config v1.2.1"
    exit 1
}

[[ $# -lt 2 ]] && usage

TYPE=$1
TARGET=$2

echo "🔄 开始 ${TYPE} 回滚到 ${TARGET}..."

case $TYPE in
    prompt)
        kubectl -n "$NAMESPACE" patch configmap agent-config --type merge \
            -p "{\"data\":{\"PROMPT_VERSION\":\"$TARGET\"}}"
        echo "✅ Prompt 版本已更新为 $TARGET"
        ;;
    model)
        kubectl -n "$NAMESPACE" patch configmap agent-config --type merge \
            -p "{\"data\":{\"PRIMARY_LLM_MODEL\":\"$TARGET\"}}"
        echo "✅ 主 LLM 已切换为 $TARGET"
        ;;
    config)
        kubectl -n "$NAMESPACE" get configmap "agent-config-${TARGET}" -o yaml \
            | sed "s/agent-config-${TARGET}/agent-config/" \
            | kubectl apply -f -
        echo "✅ 配置已回滚到 $TARGET"
        ;;
    *)
        usage
        ;;
esac

# 触发 rolling restart
kubectl -n "$NAMESPACE" rollout restart deployment/"$DEPLOYMENT"
echo "⏳ 等待 rolling restart..."
kubectl -n "$NAMESPACE" rollout status deployment/"$DEPLOYMENT" --timeout=120s

# 验证
echo "🔍 验证健康状态..."
for i in $(seq 1 5); do
    STATUS=$(kubectl -n "$NAMESPACE" exec deploy/"$DEPLOYMENT" -- \
        curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/healthz 2>/dev/null || echo "000")
    if [[ "$STATUS" == "200" ]]; then
        echo "✅ 回滚完成，服务健康"
        exit 0
    fi
    sleep 2
done
echo "❌ 回滚后健康检查失败！"
exit 1
