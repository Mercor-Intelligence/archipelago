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
	@uv run ruff format .
	@uv run ruff check src tests scripts --fix

.PHONY:
install: ## installs the project
	@pip install --upgrade --no-cache-dir openbb-yfinance yfinance curl-cffi
	@pip install -e ".[dev]"

.PHONY:
update-openbb: ## installs latest openbb drivers, use this if yfinance is acting up with curl-cffi
	@pip install --upgrade --no-cache-dir openbb-yfinance yfinance curl-cffi


.PHONY:
ensure-mcpinspector:
	@npm install -g @modelcontextprotocol/inspector

.PHONY: install-tools
install-tools: ## Installs the tools as the git pre-commit hook for this repo as well a dependency tools.
	@pip install pre-commit
	@pre-commit install --install-hooks
	@pre-commit install --hook-type pre-push

.PHONY: lint
lint: ## Runs linters via pre-commit.
	@pre-commit run -v --all-files

.PHONY: github workflow
github-workflow: ## runs the github workflow mcp-server-quality locally
	@act pull_request -W .github/workflows/mcp-server-quality.yml

.PHONY: test
test: ## runs all tests
	@uv run pytest ./tests

##@ FastAPI Server

.PHONY:
run-fastapi: ## runs FastAPI server
	@python -m uvicorn fastapi_app.app:app --reload

fastapi-hello-world: ## hello world endpoint
	@curl http://127.0.0.1:8000/hello

fastapi-stream: ## stream endpoint
	@curl http://127.0.0.1:8000/stream

fastapi-docs: ## opens documentation
	@open http://127.0.0.1:8000/redoc

fastapi-test-beqs: ## test beqs
	curl -X POST http://localhost:8000/blp/refdata \
	  -H "Content-Type: application/json,text/event-stream" \
	  -d '{"requestType":"BeqsRequest","screenName":"Tech Stocks","screenType":"EQS","group":"US_EQUITIES","overrides":{"marketCapMin":1000000000,"marketCapMax":5000000000,"peRatioMin":10,"peRatioMax":30,"sector":"Technology"}}'

fastapi-test-refdata: ## test reference data request
	curl -X POST http://localhost:8000/blp/refdata \
  -H "Content-Type: application/json,text/event-stream" \
  -d '{"requestType": "ReferenceDataRequest","securities": ["AAPL US Equity", "MSFT US Equity"], "fields": ["PX_LAST", "EPS", "PE_RATIO"]}'


fastapi-test-intraday-bars: ## test intraday bars
	curl -X POST http://localhost:8000/blp/refdata \
  -H "Content-Type: application/json,text/event-stream" \
  -d '{"requestType": "IntradayBarRequest","security": "AAPL","interval": 5,"startDateTime": "2025-11-07T09:30:00Z","endDateTime": "2025-11-07T16:00:00Z"}'


fastapi-test-intraday-ticks: ## test intraday ticks
	curl -X POST http://localhost:8000/blp/refdata \
  -H "Content-Type: application/json,text/event-stream" \
  -d '{"requestType": "IntradayTickRequest","security": "AAPL","startDateTime": "2025-11-07T09:30:00Z","endDateTime": "2025-11-07T09:32:00Z","eventTypes": ["TRADE"],"includeConditionCodes": true,"includeExchangeCodes": true}'

fastapi-test-historical: ## test historical
	curl -X POST http://localhost:8000/blp/refdata \
		-H "Content-Type: application/json,text/event-stream" \
		-d '{"requestType": "HistoricalDataRequest","securities": ["AAPL US Equity", "MSFT US Equity"],"fields": ["PX_OPEN", "PX_HIGH", "PX_LOW", "PX_LAST", "PX_VOLUME"],"start_date": "2025-11-01T00:00:00Z","end_date": "2025-11-07T00:00:00Z","request_id": "req-001","periodicity_selection": "DAILY","adjustment_split": true,"adjustment_normal": true,"adjustment_abnormal": false}'

##@ MCP

.PHONY:
run-mcp: ## runs MCP server
	@cd ./src && python -m blpapi_mcp.main

.PHONY:
run-inspector: ## runs MCP server
	@mcp-inspector http://localhost:8001

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

##@ OFFLINE DATA

.PHONY:
off-help: ## offline mode cli help
	@uv run python -m scripts.seed_db.cli --help

.PHONY:
off-seed: ## seed offline db (fetch → save raw → load to DB)
	@uv run python -m scripts.seed_db.cli seed

.PHONY:
off-seed-force: ## seed offline db with force re-fetch
	@uv run python -m scripts.seed_db.cli seed --force

.PHONY:
off-raw-only: ## fetch and save raw JSON only (no DB load)
	@uv run python -m scripts.seed_db.cli seed --raw-only

.PHONY:
off-raw-only-force: ## fetch and save raw JSON (force full re-fetch)
	@uv run python -m scripts.seed_db.cli seed --raw-only --force

.PHONY:
off-from-raw: ## load from existing raw JSON files to DB
	@uv run python -m scripts.seed_db.cli seed --from-raw

.PHONY:
off-inspect: ## inspect the offline database
	@uv run python -m scripts.seed_db.cli inspect

.PHONY:
off-inspect-raw: ## inspect raw JSON files
	@uv run python -m scripts.seed_db.cli inspect-raw -v

.PHONY:
off-clear-raw: ## delete all raw JSON files
	@uv run python -m scripts.seed_db.cli clear-raw --force

.PHONY:
off-query: ## run SQL query (usage: make off-query SQL="SELECT * FROM historical_prices LIMIT 5")
	@uv run python -m scripts.seed_db.cli query "$(SQL)"
