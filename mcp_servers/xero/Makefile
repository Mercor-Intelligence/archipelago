##@ Core
.PHONY: help
help:  ## display this help message.
	@echo "Usage:"
	@echo "  make [target]"
	@awk 'BEGIN {FS = ":.*?## "} \
		/^[a-zA-Z0-9_-]+:.*?## / { \
			printf "\033[36m  %-45s\033[0m %s\n", $$1, $$2 \
		} \
		/^##@/ { \
			printf "\n\033[1m%s\033[0m\n", substr($$0, 5) \
		}' $(MAKEFILE_LIST)

.PHONY:
format: ## formats all python code
	@uv run black .
	@uv run ruff check src tests --fix

.PHONY:
install: ## installs the project
	@pip install -e .

.PHONY:
ensure-mcpinspector:
	@npm install -g @modelcontextprotocol/inspector

.PHONY: install-tools
install-tools: ## Installs the tools as the git pre-commit hook for this repo as well a dependency tools.
	@pip install pre-commit
	@pre-commit install --install-hooks

.PHONY: lint
lint: ## Runs linters via pre-commit.
	@pre-commit run -v --all-files

.PHONY: github workflow
github-workflow: ## runs the github workflow mcp-server-quality locally
	@act pull_request -W .github/workflows/mcp-server-quality.yml

.PHONY: setup-env
setup-env: ## Create .env file from template
	@if [ ! -f mcp_servers/xero/.env ]; then \
		cp mcp_servers/xero/.env.example mcp_servers/xero/.env 2>/dev/null || \
		echo "XERO_OFFLINE_MODE=true" > mcp_servers/xero/.env; \
		echo "Created .env file in mcp_servers/xero/"; \
	else \
		echo ".env file already exists"; \
	fi

##@ MCP Server

.PHONY:
run-mcp: setup-env ## runs MCP server (offline mode by default)
	@APPS_DATA_ROOT=./.apps_data PYTHONPATH=src .venv/bin/python3 -m mcp_servers.xero.main

.PHONY:
run-mcp-online: setup-env ## runs MCP server in online mode (requires Xero credentials)
	@APPS_DATA_ROOT=./.apps_data XERO_OFFLINE_MODE=false PYTHONPATH=src .venv/bin/python3 -m mcp_servers.xero.main

.PHONY:
run-mcp-offline: setup-env ## runs MCP server in offline mode (synthetic data)
	@APPS_DATA_ROOT=./.apps_data XERO_OFFLINE_MODE=true PYTHONPATH=src .venv/bin/python3 -m mcp_servers.xero.main

.PHONY:
run-inspector: ## runs MCP inspector with offline mode
	@APPS_DATA_ROOT=./.apps_data XERO_OFFLINE_MODE=true npx @modelcontextprotocol/inspector ./run_server.sh

.PHONY:
run-inspector-online: ## runs MCP inspector with online mode
	@APPS_DATA_ROOT=./.apps_data XERO_OFFLINE_MODE=false npx @modelcontextprotocol/inspector ./run_server.sh

.PHONY:
mcp-create-session: ## creates a session
	@curl -i -X POST http://localhost:8001/sse \
	  -H "Content-Type: application/json" \
		-H "Accept: application/json,text/event-stream" \
	  -d '{ \
	    "jsonrpc": "2.0", \
	    "method": "initialize", \
	    "params": { \
	      "protocolVersion": "2024-11-05", \
	      "capabilities": {}, \
	      "clientInfo": { \
	        "name": "curl-client", \
	        "version": "1.0.0" \
	      } \
	    }, \
	    "id": 1 \
	  }' | grep -i '^mcp-session-id:' | awk '{print $$2}' | tr -d '\r' > .session_id
	@echo "Session ID: $$(cat .session_id)"

##@ Testing

.PHONY: test
test: ## Run tests
	@pytest tests/ -v

.PHONY: typecheck
typecheck: ## Run type checking
	@basedpyright src/

##@ Cleanup

.PHONY: clean
clean: ## Clean up generated files
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete
	@find . -type f -name "*.pyo" -delete
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache
	@rm -f .session_id
	@echo "Cleanup complete"
