# FMP MCP Server Makefile
# Development commands for running, testing, and maintaining the FMP MCP server

SERVER_NAME ?= fmp_server
SERVER_PATH ?= mcp_servers/$(SERVER_NAME)/
TEST_PATH ?= tests/

.PHONY: help install install-dev lint lint-all format test test-watch test-cov \
        run-server run-debug run-inspector clean check verify ensure-mcpinspector \
        typecheck

# Default target
.DEFAULT_GOAL := help

##@ Core
help: ## Display this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

##@ Installation
install: ## Install project dependencies
	@uv sync

install-dev: ## Install project with all dev extras
	@uv sync --all-extras

ensure-mcpinspector: ## Ensure MCP inspector is available (via npx)
	@command -v npx >/dev/null 2>&1 || { echo "npx not found. Please install Node.js"; exit 1; }
	@echo "MCP Inspector will be run via npx (no global install needed)"

##@ MCP Server
run-server: ## Run FMP MCP server (offline mode by default)
	@uv run python -m mcp_servers.fmp_server.main

run-debug: ## Run FMP MCP server with DEBUG logging
	@FMP_LOG_LEVEL=DEBUG uv run python -m mcp_servers.fmp_server.main

run-online: ## Run FMP MCP server in online mode (requires FMP_API_KEY)
	@FMP_MODE=online uv run python -m mcp_servers.fmp_server.main

run-inspector: ensure-mcpinspector ## Run MCP inspector with FMP server (offline mode)
	@npx @modelcontextprotocol/inspector ./run_server.sh

run-inspector-online: ensure-mcpinspector ## Run MCP inspector with FMP server (online mode)
	@FMP_MODE=online npx @modelcontextprotocol/inspector ./run_server.sh

##@ Testing
test: ## Run all tests
	@uv run pytest $(TEST_PATH) -v --tb=short

test-fmp: ## Run FMP server specific tests
	@uv run pytest tests/test_fmp_server/ -v --tb=short

test-integration: ## Run integration tests
	@uv run pytest tests/test_integration/ -v --tb=short

test-cov: ## Run tests with coverage report (requires dev extra: pytest-cov)
	@uv run --extra dev pytest $(TEST_PATH) --cov=$(SERVER_PATH) --cov-report=html --cov-report=term-missing -v

test-watch: ## Run tests in watch mode (requires pytest-watch)
	@uv run ptw -- -v --tb=short

##@ Code Quality
format: ## Format code with ruff
	@uv run ruff format .
	@uv run ruff check . --fix

lint: ## Lint FMP server code
	@uv run ruff check $(SERVER_PATH)

lint-all: ## Lint entire project
	@uv run ruff check .

typecheck: ## Run type checking with basedpyright
	@uv run basedpyright $(SERVER_PATH)

##@ Validation
check: lint test ## Run lint and tests
	@echo "All checks passed!"

verify: lint-all format ## Full verification (lint + format + test)
	@uv run ruff format --check .
	@uv run pytest $(TEST_PATH) -v --tb=short
	@echo "Verification complete!"

quick-test: ## Quick server startup test (3 second timeout)
	@timeout 3 uv run python -m mcp_servers.fmp_server.main || true
	@echo "Server startup test complete"

##@ Database
db-status: ## Show database statistics and record counts
	@uv run python -m scripts.manage_db --status

db-report: ## Generate detailed database diagnostic report
	@uv run python -m scripts.db_report

db-report-summary: ## Generate summary database report
	@uv run python -m scripts.db_report --summary

db-report-tools: ## Show all tools and their data sources
	@uv run python -m scripts.db_report --tools

db-report-availability: ## Show tool availability with data counts
	@uv run python -m scripts.db_report --availability

db-report-md: ## Generate availability report in markdown format
	@uv run python -m scripts.db_report --availability -f markdown -o db_report.md && echo "Report saved to db_report.md"

db-init: ## Initialize the FMP database with schema
	@uv run python -m scripts.manage_db init

db-seed: ## Seed database with fixture data
	@uv run python -m scripts.manage_db seed

db-reset: ## Reset database (drop all tables and reinitialize)
	@uv run python -m scripts.manage_db reset

db-download-fixtures: ## Download fixtures from FMP API (requires FMP_API_KEY)
	@uv run python -m scripts.download_fixtures

db-use-bundled: ## Load database from bundled fixtures (no API key needed)
	@uv run python -m scripts.download_fixtures --use-bundled

db-export: ## Export database to compressed fixtures for distribution
	@uv run python -m scripts.download_fixtures --export

db-compress: ## Download from API and save compressed fixtures
	@uv run python -m scripts.download_fixtures --compress

##@ Utilities
clean: ## Clean up cache and build artifacts
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@find . -type f -name ".coverage" -delete 2>/dev/null || true
	@rm -rf dist/ build/ 2>/dev/null || true
	@echo "Cleaned up cache and build artifacts"

rest-bridge: ## Run MCP REST bridge for custom UI testing
	@uv run python scripts/mcp_rest_bridge.py --mcp-server mcp_servers.fmp_server.main --port 8001

log-watch: ## Watch Claude Desktop logs for MCP communication
	@./scripts/claude-log-watch.sh
