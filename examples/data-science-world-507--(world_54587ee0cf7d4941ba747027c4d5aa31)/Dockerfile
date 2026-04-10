# === BUILDER STAGE ===
FROM python:3.13-slim-trixie AS builder

# Install build-time dependencies (not in final image)
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates curl git xz-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv package manager
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH=/root/.local/bin:$PATH

# Create directory structure
RUN mkdir -p /filesystem /app/tools /app/tasks /.apps_data

# Copy tools (runner, mcp_servers, dependencies)
COPY tools/ /app/tools/

# Normalize copied MCP code readability across unzip umask variants
RUN chmod -R a+rX /app/tools/mcp_servers 2>/dev/null || true

# Make start.sh executable
RUN chmod +x /app/tools/start.sh

# Link apps_data to expected location for ArCo build commands
RUN mkdir -p /app/tools/.apps_data \
 && rm -rf /.apps_data \
 && ln -s /app/tools/.apps_data /.apps_data

# Create per-service state directories under /.apps_data
RUN mkdir -p /.apps_data/code_execution /.apps_data/excel /.apps_data/filesystem /.apps_data/looker /.apps_data/pdfs /.apps_data/powerpoint /.apps_data/sql_execution /.apps_data/tableau /.apps_data/word

# Link /app/files to /filesystem for MCP servers
RUN ln -sf /filesystem /app/files

# Install base tools environment
RUN cd /app/tools \
 && uv venv .venv \
 && . .venv/bin/activate && uv sync --all-extras --no-cache

# Install extra pip packages into system Python
RUN /usr/local/bin/python3 -m pip install --no-cache-dir PyMuPDF python-docx python-pptx openpyxl Pillow

# Install: Code Execution
RUN cd /app/tools/mcp_servers/code_execution && export INSTALL_MEDICINE=false INSTALL_SCICOMP=false && apt-get update -qq && apt-get install -y -qq gcc libc6-dev poppler-utils python3-pip python-is-python3; mkdir -p /app/lib; gcc -shared -fPIC -O2 -o /app/lib/sandbox_fs.so mcp_servers/code_execution_server/sandbox_fs.c -ldl -lpthread; chmod 755 /app/lib/sandbox_fs.so; rm -f /usr/lib/python3*/EXTERNALLY-MANAGED; ln -sf /usr/bin/pip3 /usr/local/bin/pip; GROUPS=""; [ "$INSTALL_MEDICINE" = "true" ] && GROUPS="$GROUPS --group medicine"; [ "$INSTALL_SCICOMP" = "true" ] && GROUPS="$GROUPS --group scicomp"; uv sync --all-extras$GROUPS; uv export --no-dev --no-hashes$GROUPS > /tmp/requirements.txt; grep -v '^-e ' /tmp/requirements.txt | grep -v '^mercor-rls-code-execution' | grep -v '^Resolved ' | grep -v '^pip=' > /tmp/requirements_filtered.txt; /usr/local/bin/python3 -m pip install --break-system-packages -q -r /tmp/requirements_filtered.txt; rm /tmp/requirements.txt /tmp/requirements_filtered.txt

# Install: Excel
RUN cd /app/tools/mcp_servers/excel && apt-get update -qq && apt-get install -y -qq libreoffice-calc; uv sync --all-extras

# Install: Filesystem
RUN cd /app/tools/mcp_servers/filesystem && uv sync --all-extras

# Install: Looker
RUN cd /app/tools/mcp_servers/looker && export STATE_LOCATION=/.apps_data/looker && uv sync --all-extras

# Install: PDFs
RUN cd /app/tools/mcp_servers/pdfs && uv sync --all-extras

# Install: Powerpoint
RUN cd /app/tools/mcp_servers/powerpoint && uv sync --all-extras

# Install: SQL Execution
RUN cd /app/tools/mcp_servers/sql_execution && uv sync --all-extras

# Install: Tableau
RUN cd /app/tools/mcp_servers/tableau && export DATABASE_URL=sqlite+aiosqlite:////.apps_data/tableau/data.db STATE_LOCATION=/.apps_data/tableau TABLEAU_DATABASE_URL=sqlite+aiosqlite:////.apps_data/tableau/data.db && uv sync --all-extras

# Install: Word
RUN cd /app/tools/mcp_servers/word && uv sync --all-extras

# Build: run migrations and data imports
RUN cd /app/tools/mcp_servers/tableau && export DATABASE_URL=sqlite+aiosqlite:////.apps_data/tableau/data.db STATE_LOCATION=/.apps_data/tableau TABLEAU_DATABASE_URL=sqlite+aiosqlite:////.apps_data/tableau/data.db && mkdir -p ${STATE_LOCATION} && cd ./mcp_servers/tableau && (uv run --no-sync alembic -c alembic.ini upgrade head || echo 'Alembic migration skipped')

# Copy world files
COPY files/ /filesystem/

# Copy task files
COPY tasks/ /app/tasks/

# Clean up build artifacts to reduce image size
RUN find /app -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true \
 && find /app -type f -name '*.pyc' -delete 2>/dev/null || true \
 && find /app -type d -name '.git' -exec rm -rf {} + 2>/dev/null || true \
 && rm -rf /root/.cache/uv /root/.cache/pip 2>/dev/null || true \
 && rm -f /app/files

# === RUNTIME STAGE ===
FROM python:3.13-slim-trixie AS runtime

# Install runtime dependencies
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libreoffice-calc libreoffice-impress libreoffice-writer proot \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy built artifacts from builder stage
COPY --from=builder /app/ /app/

# Copy /filesystem from builder stage
COPY --from=builder /filesystem/ /filesystem/

# Copy system Python packages from builder stage
COPY --from=builder /usr/local/lib/ /usr/local/lib/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

# Copy uv binary from builder (for runtime venv activation)
COPY --from=builder /root/.local/bin/uv /usr/local/bin/uv
ENV PATH=/usr/local/bin:$PATH

# Recreate symlinks (not preserved by COPY)
RUN rm -rf /.apps_data \
 && ln -s /app/tools/.apps_data /.apps_data \
 && rm -rf /app/files \
 && ln -s /filesystem /app/files

# Add tools to PYTHONPATH for imports
ENV PYTHONPATH=/app/tools

# Expose MCP port
EXPOSE 8000

# No ENTRYPOINT/CMD: invoker supplies startup command at runtime
