##############################################################################
# Standalone Grader — Dockerfile
#
# Builds a self-contained grading container that can evaluate agent
# trajectories against verifiers and scoring configs.
#
# Build:
#   docker build -t grader .
#
# Run:
#   docker run --rm \
#     -v /path/to/data:/data \
#     -e OPENAI_API_KEY=sk-... \
#     grader \
#       --initial-snapshot /data/snapshot_before.zip \
#       --final-snapshot /data/snapshot_after.zip \
#       --trajectory /data/trajectory.json \
#       --grading-config /data/grading_config.json \
#       --output /data/results.json
##############################################################################

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# System dependencies required by verifiers:
#   - libreoffice: Office document → PDF conversion (used by multi-representation verifiers)
#   - poppler-utils: PDF rendering to images (used by pdf2image)
#   - build-essential: Native Python extension compilation
#   - curl, git: Fetching external resources
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libreoffice \
    poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml uv.lock* README.md /app/
RUN uv sync --locked --all-groups

# Data science packages used by programmatic verifiers
RUN uv pip install --python /app/.venv/bin/python \
    pandas==2.2.3 \
    numpy==2.0.2 \
    matplotlib==3.9.2 \
    scikit-learn==1.5.2 \
    xgboost==2.1.1 \
    imbalanced-learn==0.12.4 \
    statsmodels==0.14.4

# Copy grading runner source
COPY runner/ /app/runner/

ENV PATH="/app/.venv/bin:$PATH"

# LLM API keys should be provided at runtime via -e flags
# e.g. -e OPENAI_API_KEY=... or -e ANTHROPIC_API_KEY=...

ENTRYPOINT ["python", "-m", "runner.cli"]
