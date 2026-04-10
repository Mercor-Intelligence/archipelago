# =============================================================================
# Stage 1: Build the Next.js UI
# =============================================================================
FROM node:20-slim AS ui-builder

WORKDIR /app/ui

# Copy UI source
COPY ui/tableau/package*.json ./

# Install dependencies (use npm install since package-lock.json may not exist)
RUN npm install --legacy-peer-deps

# Copy the rest of the UI code
COPY ui/tableau/ ./

# Build and export static files for Modal (served from root /)
# BUILD_MODE=export creates static files without basePath
ENV BUILD_MODE=export
RUN npm run build

# For Next.js static export, check if 'out' directory exists
# If not, the build output is in '.next' and we need different handling
RUN if [ -d "out" ]; then \
        echo "Static export found in 'out' directory"; \
    elif [ -d ".next/static" ]; then \
        echo "Found .next build, creating static export structure"; \
        mkdir -p out/_next/static && \
        cp -r .next/static/* out/_next/static/ 2>/dev/null || true; \
    fi

# =============================================================================
# Stage 2: Python backend with UI
# =============================================================================
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv package manager
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Copy project files
COPY pyproject.toml uv.lock README.md ./
COPY packages/ ./packages/
COPY mcp_servers/tableau/ ./mcp_servers/tableau/
COPY scripts/mcp_rest_bridge.py ./scripts/
COPY scripts/db_tools.py ./scripts/
COPY ui_generator/ ./ui_generator/

# Copy built UI from the builder stage
COPY --from=ui-builder /app/ui/out ./ui/dist

# Install dependencies (all extras)
RUN uv sync --frozen --all-extras
RUN uv pip install packages/mcp_auth packages/mcp_cache packages/mcp_middleware

# Install boto3 for S3 import/export
RUN uv pip install boto3

# Set Python path to include the app directory
ENV PYTHONPATH=/app

# Set database path to unify MCP server and REST bridge databases
ENV TABLEAU_DATABASE_URL=sqlite+aiosqlite:////app/mcp_servers/tableau/data.db

# Run Alembic migrations to create database tables
RUN cd mcp_servers/tableau && uv run --no-sync alembic -c alembic.ini upgrade head

# Set default environment variables for Tableau
ARG TABLEAU_SERVER_URL=""
ARG TABLEAU_SITE_ID=""
ARG TABLEAU_TOKEN_NAME=""
ARG TABLEAU_TOKEN_SECRET=""
ARG TABLEAU_API_VERSION="3.21"

ENV TABLEAU_SERVER_URL=${TABLEAU_SERVER_URL}
ENV TABLEAU_SITE_ID=${TABLEAU_SITE_ID}
ENV TABLEAU_TOKEN_NAME=${TABLEAU_TOKEN_NAME}
ENV TABLEAU_TOKEN_SECRET=${TABLEAU_TOKEN_SECRET}
ENV TABLEAU_API_VERSION=${TABLEAU_API_VERSION}

# Set REST API port configuration
# Use port 8080 for Modal sandbox compatibility (Modal routes to 8080 by default)
ENV PORT=8080
ENV HOST=0.0.0.0

# Expose the REST API port
EXPOSE 8080

# Health check endpoint
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Run the MCP REST bridge for tableau with UI
CMD uv run python scripts/mcp_rest_bridge.py \
    --mcp-server mcp_servers.tableau.main \
    --port ${PORT} \
    --host ${HOST} \
    --static-dir ui/dist
