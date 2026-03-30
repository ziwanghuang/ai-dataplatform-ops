# go/Makefile
BINARY_NAME=mcp-server
GATEWAY_NAME=mcp-gateway
VERSION?=$(shell git describe --tags --always --dirty 2>/dev/null || echo "v0.1.0-dev")
LDFLAGS=-ldflags "-s -w -X main.Version=$(VERSION)"

.PHONY: build test lint clean dev-server

build: build-server build-gateway

build-server:
	CGO_ENABLED=0 go build $(LDFLAGS) -o bin/$(BINARY_NAME) ./cmd/mcp-server/

build-gateway:
	CGO_ENABLED=0 go build $(LDFLAGS) -o bin/$(GATEWAY_NAME) ./cmd/mcp-gateway/

test:
	go test ./... -v -race -count=1

test-coverage:
	go test ./... -coverprofile=coverage.out -covermode=atomic
	go tool cover -html=coverage.out -o coverage.html

lint:
	golangci-lint run ./...

clean:
	rm -rf bin/ coverage.out coverage.html

dev-server:
	go run ./cmd/mcp-server/ --config=configs/dev.yaml

dev-gateway:
	go run ./cmd/mcp-gateway/ --config=configs/dev.yaml
