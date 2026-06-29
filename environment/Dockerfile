FROM debian:trixie-slim

# Install system dependencies
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates chromium coreutils curl fonts-liberation git libreoffice-calc libreoffice-core libsqlite3-dev netcat-openbsd nodejs npm passwd python3-pip python3.13 python3.13-dev python3.13-venv socat util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && update-ca-certificates

# Make python3 point to 3.13
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.13 1 \
 && python3 --version

# Install uv (Python package/dependency manager)
RUN python3 -m pip install --no-cache-dir --break-system-packages uv

WORKDIR /app

# Copy dependency files
COPY environment/pyproject.toml environment/uv.lock /app/
RUN --mount=type=secret,id=github_token,required=false export UV_GITHUB_TOKEN=$(cat /run/secrets/github_token 2>/dev/null) && if [ -n "$UV_GITHUB_TOKEN" ]; then git config --global url."https://x-access-token:${UV_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; fi \
 && uv sync --all-groups \
 && rm -f ~/.gitconfig

# Copy application code
COPY environment/ /app/
COPY mcp_servers/ /app/mcp_servers/

# Create subsystem directories
RUN mkdir -p /filesystem /.apps_data

# Install: documents
RUN --mount=type=secret,id=github_token,required=false export UV_GITHUB_TOKEN=$(cat /run/secrets/github_token 2>/dev/null) && if [ -n "$UV_GITHUB_TOKEN" ]; then git config --global url."https://x-access-token:${UV_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; fi \
 && ( cd /app/mcp_servers/documents/mcp_servers/docs_server && uv sync --all-extras ) \
 && rm -f ~/.gitconfig

# Install: calendar
RUN --mount=type=secret,id=github_token,required=false export UV_GITHUB_TOKEN=$(cat /run/secrets/github_token 2>/dev/null) && if [ -n "$UV_GITHUB_TOKEN" ]; then git config --global url."https://x-access-token:${UV_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; fi \
 && ( cd /app/mcp_servers/calendar/mcp_servers/calendar_server && uv sync --all-extras ) \
 && rm -f ~/.gitconfig

# Install: mail
RUN --mount=type=secret,id=github_token,required=false export UV_GITHUB_TOKEN=$(cat /run/secrets/github_token 2>/dev/null) && if [ -n "$UV_GITHUB_TOKEN" ]; then git config --global url."https://x-access-token:${UV_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; fi \
 && ( cd /app/mcp_servers/mail/mcp_servers/mail_server && uv sync --all-extras ) \
 && rm -f ~/.gitconfig

# Install: code
RUN --mount=type=secret,id=github_token,required=false export UV_GITHUB_TOKEN=$(cat /run/secrets/github_token 2>/dev/null) && if [ -n "$UV_GITHUB_TOKEN" ]; then git config --global url."https://x-access-token:${UV_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; fi \
 && ( cd /app/mcp_servers/code/mcp_servers/code_execution_server && export INSTALL_MEDICINE=false INSTALL_SCICOMP=false && apt-get update -qq && apt-get install -y -qq gcc libc6-dev poppler-utils python3-pip python-is-python3; mkdir -p /app/lib; gcc -shared -fPIC -O2 -o /app/lib/sandbox_fs.so mcp_servers/code_execution_server/sandbox_fs.c -ldl -lpthread; chmod 755 /app/lib/sandbox_fs.so; rm -f /usr/lib/python3*/EXTERNALLY-MANAGED; ln -sf /usr/bin/pip3 /usr/local/bin/pip; GROUPS=""; [ "$INSTALL_MEDICINE" = "true" ] && GROUPS="$GROUPS --group medicine"; [ "$INSTALL_SCICOMP" = "true" ] && GROUPS="$GROUPS --group scicomp"; uv sync --all-extras$GROUPS; uv export --no-dev --no-hashes$GROUPS > /tmp/requirements.txt; grep -v '^-e ' /tmp/requirements.txt | grep -v '^mercor-rls-code-execution' | grep -v '^Resolved ' | grep -v '^pip=' > /tmp/requirements_filtered.txt; /usr/bin/python3 -m pip install --break-system-packages -q -r /tmp/requirements_filtered.txt; rm /tmp/requirements.txt /tmp/requirements_filtered.txt ) \
 && rm -f ~/.gitconfig

# Install: presentations
RUN --mount=type=secret,id=github_token,required=false export UV_GITHUB_TOKEN=$(cat /run/secrets/github_token 2>/dev/null) && if [ -n "$UV_GITHUB_TOKEN" ]; then git config --global url."https://x-access-token:${UV_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; fi \
 && ( cd /app/mcp_servers/presentations/mcp_servers/slides_server && uv sync --all-extras ) \
 && rm -f ~/.gitconfig

# Install: chat
RUN --mount=type=secret,id=github_token,required=false export UV_GITHUB_TOKEN=$(cat /run/secrets/github_token 2>/dev/null) && if [ -n "$UV_GITHUB_TOKEN" ]; then git config --global url."https://x-access-token:${UV_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; fi \
 && ( cd /app/mcp_servers/chat/mcp_servers/chat_server && uv sync --all-extras ) \
 && rm -f ~/.gitconfig

# Install: filesystem
RUN --mount=type=secret,id=github_token,required=false export UV_GITHUB_TOKEN=$(cat /run/secrets/github_token 2>/dev/null) && if [ -n "$UV_GITHUB_TOKEN" ]; then git config --global url."https://x-access-token:${UV_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; fi \
 && ( cd /app/mcp_servers/filesystem/mcp_servers/filesystem_server && uv sync --all-extras ) \
 && rm -f ~/.gitconfig

# Install: pdfs
RUN --mount=type=secret,id=github_token,required=false export UV_GITHUB_TOKEN=$(cat /run/secrets/github_token 2>/dev/null) && if [ -n "$UV_GITHUB_TOKEN" ]; then git config --global url."https://x-access-token:${UV_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; fi \
 && ( cd /app/mcp_servers/pdfs/mcp_servers/pdf_server && uv sync --all-extras ) \
 && rm -f ~/.gitconfig

# Install: spreadsheets
RUN --mount=type=secret,id=github_token,required=false export UV_GITHUB_TOKEN=$(cat /run/secrets/github_token 2>/dev/null) && if [ -n "$UV_GITHUB_TOKEN" ]; then git config --global url."https://x-access-token:${UV_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; fi \
 && ( cd /app/mcp_servers/spreadsheets/mcp_servers/sheets_server && apt-get update -qq && apt-get install -y -qq libreoffice-calc; uv sync --all-extras ) \
 && rm -f ~/.gitconfig

ENV PATH=/app/.venv/bin:$PATH \
    UV_SYSTEM_PYTHON=1 \
    PORT=8080

# Expose FastAPI port
EXPOSE 8080

CMD ["uv", "run", "uvicorn", "runner.main:app", "--host", "0.0.0.0", "--port", "8080"]
