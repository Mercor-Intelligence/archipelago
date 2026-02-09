FROM python:3.13-bookworm

# Install system dependencies for Claude Code
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    tree \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (optional - only needed for claude_code agent)
RUN curl -fsSL https://static.claudeusercontent.com/install-claudecode.sh | bash || true

# Set Claude Code to use the bridge proxy for API calls (when using claude_code agent)
ENV ANTHROPIC_BASE_URL=http://localhost:13131

# Set working directory to where Inspect copies files
WORKDIR /tmp/sandbox

# Keep container running for Inspect to connect
CMD ["tail", "-f", "/dev/null"]
