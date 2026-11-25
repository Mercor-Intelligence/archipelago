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
# Depot runners cannot reach public PyPI (INC-698); CI mounts a netrc for the package firewall.
RUN printf '%s\n' 'if [ -s /run/secrets/netrc ]; then export NETRC=/run/secrets/netrc PIP_INDEX_URL=https://firewall.mercor.com/pypi/simple/ UV_DEFAULT_INDEX=https://firewall.mercor.com/pypi/simple/; fi' > /usr/local/bin/package-firewall
RUN apt-get update && apt-get install -y --no-install-recommends busybox-static git && rm -rf /var/lib/apt/lists/*
# The firewall withholds uv 0.10-0.13.3, so the pinned binary comes from Astral's image.
COPY --from=ghcr.io/astral-sh/uv:0.11.25@sha256:1e3808aa9023d0980e7c15b1fa7c1ac16ff35925780cf5c459858b2d693f01a9 /uv /uvx /usr/local/bin/
RUN --mount=type=secret,id=netrc,required=false \
    . /usr/local/bin/package-firewall; pip install harbor==0.18.0 pytest==8.4.2 pytest-asyncio==1.2.0
COPY runtime.py /opt/atlas-mini-swe-runtime.py
RUN --mount=type=secret,id=netrc,required=false \
    . /usr/local/bin/package-firewall; python -c "import runpy,subprocess; subprocess.run(runpy.run_path('/opt/atlas-mini-swe-runtime.py')['runtime_bake_command']('1.92.0','2026-07-12T01:16:00Z'),shell=True,check=True)"
RUN mkdir /worker-runtime && mv /opt/lighthouse-mini-swe.tar.gz /worker-runtime/ && useradd -m solver && mkdir -p /app /logs/agent && chown -R solver /app /logs/agent
COPY . /smoke
ENV PYTHONPATH=/smoke LITELLM_LOCAL_MODEL_COST_MAP=True ATLAS_TEST_OFFLINE_RUNTIME=1
WORKDIR /smoke
CMD ["python", "-m", "pytest", "-q", "tests/test_atlas_offline_runtime.py", "tests/test_harbor_retry_deadline.py"]
DOCKER
docker build ${NETRC_CONTENT:+--secret id=netrc,env=NETRC_CONTENT} -t atlas-offline-runtime:test "$context"
docker run --rm --network none \
    --tmpfs /opt/lighthouse-mini-swe:exec,size=2g \
    --tmpfs /home/solver:exec,uid=1000,gid=1000 \
    --tmpfs /logs/agent:uid=1000,gid=1000 \
    --tmpfs /tmp:exec,mode=1777 atlas-offline-runtime:test
