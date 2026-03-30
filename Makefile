# Makefile — 顶层开发入口
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "v0.1.0-dev")
REGISTRY ?= registry.internal/aiops

.PHONY: help install dev dev-down test lint build format eval

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── 安装 ────────────────────────────
install: ## 安装所有依赖
	cd python && poetry install
	cd go && go mod download

# ─── 本地开发 ──────────────────────────
dev-up: ## 启动本地基础设施（Docker Compose）
	docker compose -f infra/docker/docker-compose.dev.yml up -d

dev-down: ## 停止本地基础设施
	docker compose -f infra/docker/docker-compose.dev.yml down

dev: dev-up ## 启动完整本地开发环境
	@echo "⏳ Waiting for services to be ready..."
	@sleep 5
	@echo "✅ Infrastructure ready. Starting services..."
	cd go && make dev-server &
	cd python && poetry run uvicorn aiops.web.app:app --reload --port 8080

# ─── 测试 ──────────────────────────────
test: test-python test-go ## 运行全部测试

test-python: ## Python 单元测试
	cd python && poetry run pytest tests/unit/ -v --cov=aiops --cov-report=term-missing

test-go: ## Go 测试
	cd go && make test

test-integration: ## 集成测试（需要 Docker Compose）
	cd python && poetry run pytest tests/integration/ -v

test-security: ## 安全测试
	cd python && poetry run pytest tests/security/ -v

# ─── Lint ──────────────────────────────
lint: lint-python lint-go ## 全部 lint

lint-python: ## Python lint
	cd python && poetry run ruff check src/ tests/
	cd python && poetry run ruff format --check src/ tests/

lint-go: ## Go lint
	cd go && golangci-lint run ./...

format: ## 自动格式化
	cd python && poetry run ruff format src/ tests/
	cd python && poetry run ruff check --fix src/ tests/
	cd go && gofmt -w .

# ─── 构建 ──────────────────────────────
build: build-agent build-mcp ## 构建所有镜像

build-agent: ## 构建 Agent 镜像
	docker build -f infra/docker/Dockerfile.agent -t $(REGISTRY)/agent-service:$(VERSION) .

build-mcp: ## 构建 MCP Server 镜像
	docker build -f infra/docker/Dockerfile.mcp -t $(REGISTRY)/mcp-server:$(VERSION) .

# ─── 评测 ──────────────────────────────
eval: ## 离线评测
	cd python && poetry run python -m eval.runners.offline --dataset eval/datasets/v2.1.yaml

eval-gate: ## 评测门禁
	cd python && poetry run python -m eval.runners.gate_check
