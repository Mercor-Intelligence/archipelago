#!/usr/bin/env bash
set -euo pipefail
repo=$(git rev-parse --show-toplevel)
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
mkdir -p "$context/runner/agents" "$context/tests"
touch "$context/tests/__init__.py"
cp -R "$repo/archipelago/agents/runner/agents/harbor_atlas" "$context/runner/agents/"
cp "$repo/rl-studio/server/packages/code_data_evals/lighthouse/harnesses/harbor/mini_swe_runtime.py" "$context/runtime.py"
cp "$repo/archipelago/agents/tests/test_harbor_retry_deadline.py" "$context/tests/"
cp "$repo/archipelago/agents/tests/test_atlas_offline_runtime.py" "$context/tests/"
cat > "$context/Dockerfile" <<'DOCKER'
FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends busybox-static git && rm -rf /var/lib/apt/lists/*
RUN pip install uv==0.11.25 harbor==0.18.0 pytest==8.4.2 pytest-asyncio==1.2.0
COPY runtime.py /opt/atlas-mini-swe-runtime.py
RUN python -c "import runpy,subprocess; subprocess.run(runpy.run_path('/opt/atlas-mini-swe-runtime.py')['runtime_bake_command']('1.92.0','2026-07-12T01:16:00Z'),shell=True,check=True)"
RUN mkdir /worker-runtime && mv /opt/lighthouse-mini-swe.tar.gz /worker-runtime/ && useradd -m solver && mkdir -p /app /logs/agent && chown -R solver /app /logs/agent
COPY . /smoke
ENV PYTHONPATH=/smoke LITELLM_LOCAL_MODEL_COST_MAP=True ATLAS_TEST_OFFLINE_RUNTIME=1
WORKDIR /smoke
CMD ["python", "-m", "pytest", "-q", "tests/test_atlas_offline_runtime.py", "tests/test_harbor_retry_deadline.py"]
DOCKER
docker build -t atlas-offline-runtime:test "$context"
docker run --rm --network none \
    --tmpfs /opt/lighthouse-mini-swe:exec,size=2g \
    --tmpfs /home/solver:exec,uid=1000,gid=1000 \
    --tmpfs /logs/agent:uid=1000,gid=1000 \
    --tmpfs /tmp:exec,mode=1777 atlas-offline-runtime:test
