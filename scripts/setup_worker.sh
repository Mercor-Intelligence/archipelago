#!/usr/bin/env bash
# GCE VM startup script for archipelago-eval worker.
#
# Embed this script as `startup-script` metadata when creating the VM.
# It will:
#   1. Install docker / uv / git / gsutil / python storage client
#   2. Clone the archipelago repo
#   3. Pull New API token from Secret Manager and write agents/.env
#   4. Install agents + grading dependencies via uv
#   5. Optionally register a dynamic GCS queue worker as a systemd service
#
# Required service-account roles:
#   roles/storage.objectAdmin
#   roles/secretmanager.secretAccessor
#
# Required env vars to set on the VM metadata (or replace inline):
#   ARCHIPELAGO_REPO_URL   - git URL of archipelago (default: ssh fallback / local path)
#   ARCHIPELAGO_REVISION   - branch / tag / commit (default: main)
#   EVAL_PROJECT_DIR       - GCS prefix for this eval project
#                            (default: eval-projects/seed-pro-pass3-20260616)
#   EVAL_BUCKET            - GCS bucket name (default: sotalab-archipelago-eval)
#   NEW_API_SECRET_NAME    - Secret Manager secret name (default: NEW_API_TOKEN-staging)
#   NEW_API_BASE           - New API base URL (default: https://new-api-staging.sotalab.ai)

set -euo pipefail

metadata_value() {
  local key="$1"
  local default="$2"
  local value
  value="$(curl -fsS -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/attributes/${key}" 2>/dev/null || true)"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
  else
    printf '%s' "$default"
  fi
}

config_value() {
  local key="$1"
  local default="$2"
  local current="${!key:-}"
  if [[ -n "$current" ]]; then
    printf '%s' "$current"
  else
    metadata_value "$key" "$default"
  fi
}

ARCHIPELAGO_REPO_URL="$(config_value ARCHIPELAGO_REPO_URL "https://github.com/SoTALab-ai/archipelago.git")"
ARCHIPELAGO_REVISION="$(config_value ARCHIPELAGO_REVISION "apex-local-samples")"
EVAL_PROJECT_DIR="$(config_value EVAL_PROJECT_DIR "eval-projects/seed-pro-pass3-20260616")"
EVAL_BUCKET="$(config_value EVAL_BUCKET "sotalab-archipelago-eval")"
NEW_API_SECRET_NAME="$(config_value NEW_API_SECRET_NAME "NEW_API_TOKEN-staging")"
HF_TOKEN_SECRET_NAME="$(config_value HF_TOKEN_SECRET_NAME "HF_TOKEN")"
NEW_API_BASE="$(config_value NEW_API_BASE "https://new-api-staging.sotalab.ai/v1")"
ARCHIPELAGO_DIR="$(config_value ARCHIPELAGO_DIR "/opt/archipelago")"
ARCHIPELAGO_TGZ_GCS_URI="$(config_value ARCHIPELAGO_TGZ_GCS_URI "")"
ENV_IMAGE="$(config_value ENV_IMAGE "asia-east1-docker.pkg.dev/sotalab-prod/docker-repo/sotalab-apex-archipelago-environment-prod")"
ENV_IMAGE_TAG="$(config_value ENV_IMAGE_TAG "latest")"
WORKER_USER="$(config_value WORKER_USER "lumin")"
RUN_DYNAMIC_QUEUE="$(config_value RUN_DYNAMIC_QUEUE "0")"
RUN_LEGACY_PUBSUB="$(config_value RUN_LEGACY_PUBSUB "0")"
QUEUE_NAME="$(config_value QUEUE_NAME "pass5")"
ATTEMPT_START="$(config_value ATTEMPT_START "1")"
ATTEMPT_END="$(config_value ATTEMPT_END "5")"
EVAL_MODEL="$(config_value EVAL_MODEL "doubao-seed-2-0-pro-260215")"
TEMPERATURE="$(config_value TEMPERATURE "0.7")"
MAX_STEPS="$(config_value MAX_STEPS "200")"
MAX_CONSECUTIVE_FAILURES="$(config_value MAX_CONSECUTIVE_FAILURES "2")"
JOBS_FILE="$(config_value JOBS_FILE "$ARCHIPELAGO_DIR/state/task_ids.json")"
TASK_IDS_GCS_URI="$(config_value TASK_IDS_GCS_URI "")"

echo "[setup_worker] starting at $(date -Iseconds)"
echo "[setup_worker] config: project_dir=$EVAL_PROJECT_DIR bucket=$EVAL_BUCKET dynamic=$RUN_DYNAMIC_QUEUE queue=$QUEUE_NAME attempts=$ATTEMPT_START..$ATTEMPT_END model=$EVAL_MODEL temperature=$TEMPERATURE"

# 1. System packages
echo "[setup_worker] installing system packages"
apt-get update -y
apt-get install -y --no-install-recommends \
  apt-transport-https ca-certificates curl gnupg lsb-release \
  python3 python3-pip python3-venv git

# Refresh config after curl is installed. Fresh images may not have curl before
# the first apt-get, so the initial metadata reads can fall back to defaults.
ARCHIPELAGO_REPO_URL="$(metadata_value ARCHIPELAGO_REPO_URL "$ARCHIPELAGO_REPO_URL")"
ARCHIPELAGO_REVISION="$(metadata_value ARCHIPELAGO_REVISION "$ARCHIPELAGO_REVISION")"
EVAL_PROJECT_DIR="$(metadata_value EVAL_PROJECT_DIR "$EVAL_PROJECT_DIR")"
EVAL_BUCKET="$(metadata_value EVAL_BUCKET "$EVAL_BUCKET")"
NEW_API_SECRET_NAME="$(metadata_value NEW_API_SECRET_NAME "$NEW_API_SECRET_NAME")"
HF_TOKEN_SECRET_NAME="$(metadata_value HF_TOKEN_SECRET_NAME "$HF_TOKEN_SECRET_NAME")"
NEW_API_BASE="$(metadata_value NEW_API_BASE "$NEW_API_BASE")"
ARCHIPELAGO_DIR="$(metadata_value ARCHIPELAGO_DIR "$ARCHIPELAGO_DIR")"
ARCHIPELAGO_TGZ_GCS_URI="$(metadata_value ARCHIPELAGO_TGZ_GCS_URI "$ARCHIPELAGO_TGZ_GCS_URI")"
ENV_IMAGE="$(metadata_value ENV_IMAGE "$ENV_IMAGE")"
ENV_IMAGE_TAG="$(metadata_value ENV_IMAGE_TAG "$ENV_IMAGE_TAG")"
WORKER_USER="$(metadata_value WORKER_USER "$WORKER_USER")"
RUN_DYNAMIC_QUEUE="$(metadata_value RUN_DYNAMIC_QUEUE "$RUN_DYNAMIC_QUEUE")"
RUN_LEGACY_PUBSUB="$(metadata_value RUN_LEGACY_PUBSUB "$RUN_LEGACY_PUBSUB")"
QUEUE_NAME="$(metadata_value QUEUE_NAME "$QUEUE_NAME")"
ATTEMPT_START="$(metadata_value ATTEMPT_START "$ATTEMPT_START")"
ATTEMPT_END="$(metadata_value ATTEMPT_END "$ATTEMPT_END")"
EVAL_MODEL="$(metadata_value EVAL_MODEL "$EVAL_MODEL")"
TEMPERATURE="$(metadata_value TEMPERATURE "$TEMPERATURE")"
MAX_STEPS="$(metadata_value MAX_STEPS "$MAX_STEPS")"
MAX_CONSECUTIVE_FAILURES="$(metadata_value MAX_CONSECUTIVE_FAILURES "$MAX_CONSECUTIVE_FAILURES")"
JOBS_FILE="$(metadata_value JOBS_FILE "$JOBS_FILE")"
TASK_IDS_GCS_URI="$(metadata_value TASK_IDS_GCS_URI "$TASK_IDS_GCS_URI")"
echo "[setup_worker] refreshed config: project_dir=$EVAL_PROJECT_DIR bucket=$EVAL_BUCKET dynamic=$RUN_DYNAMIC_QUEUE queue=$QUEUE_NAME attempts=$ATTEMPT_START..$ATTEMPT_END model=$EVAL_MODEL temperature=$TEMPERATURE"

# Docker (debian-style)
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
fi
systemctl enable docker
systemctl start docker

if ! id "$WORKER_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$WORKER_USER"
fi
usermod -aG docker "$WORKER_USER" || true
WORKER_HOME="$(getent passwd "$WORKER_USER" | cut -d: -f6)"

# Pull prebuilt environment image so the worker doesn't need to build it.
echo "[setup_worker] pulling environment image ${ENV_IMAGE}:${ENV_IMAGE_TAG}"
gcloud auth configure-docker asia-east1-docker.pkg.dev --quiet || true
if docker pull "${ENV_IMAGE}:${ENV_IMAGE_TAG}"; then
  docker tag "${ENV_IMAGE}:${ENV_IMAGE_TAG}" apex-test-environment:latest
  echo "[setup_worker] tagged ${ENV_IMAGE}:${ENV_IMAGE_TAG} as apex-test-environment:latest"
else
  echo "[setup_worker] WARNING: could not pull ${ENV_IMAGE}:${ENV_IMAGE_TAG}; main.py will fall back to docker compose --build"
fi

# Google Cloud SDK (gsutil)
if ! command -v gsutil >/dev/null 2>&1; then
  echo "[setup_worker] installing gsutil"
  echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
    > /etc/apt/sources.list.d/google-cloud-sdk.list
  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
  apt-get update -y
  apt-get install -y --no-install-recommends google-cloud-cli
fi

# uv (Python package manager)
if ! command -v uv >/dev/null 2>&1; then
  echo "[setup_worker] installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs into ~/.local/bin. Copy the binary out of /root so workers
  # running as the login user can execute it.
  if [[ -x /root/.local/bin/uv ]]; then
    install -m 0755 /root/.local/bin/uv /usr/local/bin/uv
  fi
fi

# 2. Fetch archipelago code from GCS (avoids GitHub auth on fresh VMs)
echo "[setup_worker] fetching archipelago code from GCS"
mkdir -p "$ARCHIPELAGO_DIR"
if [[ -z "$ARCHIPELAGO_TGZ_GCS_URI" ]]; then
  ARCHIPELAGO_TGZ_GCS_URI="gs://${EVAL_BUCKET}/${EVAL_PROJECT_DIR}/setup/archipelago.tgz"
fi
if gsutil cp "$ARCHIPELAGO_TGZ_GCS_URI" /tmp/archipelago.tgz; then
  rm -rf "$ARCHIPELAGO_DIR"
  tar xzf /tmp/archipelago.tgz -C /opt
  echo "[setup_worker] extracted to $ARCHIPELAGO_DIR"
else
  echo "[setup_worker] GCS fetch failed; falling back to git clone"
  if [[ -d "$ARCHIPELAGO_DIR/.git" ]]; then
    cd "$ARCHIPELAGO_DIR"
    git fetch --all
    git checkout "$ARCHIPELAGO_REVISION"
  else
    git clone --branch "$ARCHIPELAGO_REVISION" "$ARCHIPELAGO_REPO_URL" "$ARCHIPELAGO_DIR"
  fi
fi

# 3. New API token from Secret Manager
echo "[setup_worker] fetching New API token"
NEW_API_TOKEN="$(gcloud secrets versions access latest \
  --project="sotalab-prod" \
  --secret="$NEW_API_SECRET_NAME")"

# 3b. HuggingFace token (gated dataset mercor/apex-agents)
echo "[setup_worker] fetching HF token"
HF_TOKEN_VAL="$(gcloud secrets versions access latest \
  --project="sotalab-prod" \
  --secret="$HF_TOKEN_SECRET_NAME" 2>/dev/null || true)"
if [[ -n "$HF_TOKEN_VAL" ]]; then
  mkdir -p /root/.cache/huggingface
  echo -n "$HF_TOKEN_VAL" > /root/.cache/huggingface/token
  chmod 600 /root/.cache/huggingface/token
  if [[ -n "$WORKER_HOME" ]]; then
    mkdir -p "$WORKER_HOME/.cache/huggingface"
    echo -n "$HF_TOKEN_VAL" > "$WORKER_HOME/.cache/huggingface/token"
    chown -R "$WORKER_USER:$WORKER_USER" "$WORKER_HOME/.cache"
    chmod 600 "$WORKER_HOME/.cache/huggingface/token"
  fi
  # Also expose for uv run
  export HF_TOKEN="$HF_TOKEN_VAL"
  echo "[setup_worker] HF_TOKEN written to local HuggingFace token files"
fi

cat > "$ARCHIPELAGO_DIR/agents/.env" <<EOF
ENV=local
AGENT_TIMEOUT_SECONDS=81000
LITELLM_PROXY_API_BASE=$NEW_API_BASE
LITELLM_PROXY_API_KEY=$NEW_API_TOKEN
EOF

# 4. Install deps
echo "[setup_worker] uv sync in agents/"
chown -R "$WORKER_USER:$WORKER_USER" "$ARCHIPELAGO_DIR"
sudo -u "$WORKER_USER" env PATH="/usr/local/bin:$PATH" bash -lc "cd '$ARCHIPELAGO_DIR/agents' && uv sync --locked"

echo "[setup_worker] uv sync in grading/"
sudo -u "$WORKER_USER" env PATH="/usr/local/bin:$PATH" bash -lc "cd '$ARCHIPELAGO_DIR/grading' && uv sync --locked"

chown -R "$WORKER_USER:$WORKER_USER" "$ARCHIPELAGO_DIR"

if [[ -n "$TASK_IDS_GCS_URI" ]]; then
  echo "[setup_worker] fetching task ids from $TASK_IDS_GCS_URI"
  mkdir -p "$(dirname "$JOBS_FILE")"
  gsutil cp "$TASK_IDS_GCS_URI" "$JOBS_FILE"
  chown -R "$WORKER_USER:$WORKER_USER" "$(dirname "$JOBS_FILE")"
fi

# 5. Worker environment file (read by eval_worker.py and systemd unit)
cat > /etc/archipelago-eval.env <<EOF
ARCHIPELAGO_DIR=$ARCHIPELAGO_DIR
EVAL_PROJECT_DIR=$EVAL_PROJECT_DIR
EVAL_BUCKET=$EVAL_BUCKET
EOF

# 6. Python GCS client (system-wide so systemd doesn't depend on uv venv)
pip3 install --break-system-packages -q \
  google-cloud-storage

if [[ "$RUN_DYNAMIC_QUEUE" == "1" ]]; then
  cat > /etc/systemd/system/archipelago-queue-worker.service <<UNIT
[Unit]
Description=Archipelago eval worker (dynamic GCS queue)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=$WORKER_USER
WorkingDirectory=$ARCHIPELAGO_DIR
Environment=PATH=/usr/local/bin:/usr/bin:/bin
EnvironmentFile=/etc/archipelago-eval.env
ExecStart=/usr/bin/python3 $ARCHIPELAGO_DIR/scripts/worker_queue.py --jobs-file $JOBS_FILE --project-dir $EVAL_PROJECT_DIR --bucket $EVAL_BUCKET --queue-name $QUEUE_NAME --attempt-start $ATTEMPT_START --attempt-end $ATTEMPT_END --model $EVAL_MODEL --temperature $TEMPERATURE --max-steps $MAX_STEPS --shuffle --max-consecutive-failures $MAX_CONSECUTIVE_FAILURES
Restart=on-failure
RestartSec=30
StandardOutput=append:/var/log/archipelago-queue-worker.log
StandardError=append:/var/log/archipelago-queue-worker.log

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable archipelago-queue-worker.service
  systemctl restart archipelago-queue-worker.service
  echo "[setup_worker] dynamic queue worker service started"
elif [[ "$RUN_LEGACY_PUBSUB" == "1" ]]; then
  # 7. Legacy Pub/Sub worker service. Kept for manual recovery only; the normal
  # eval path uses the GCS queue above.
  PUBSUB_SUBSCRIPTION="$(metadata_value PUBSUB_SUBSCRIPTION "archipelago-eval-workers")"
  PUBSUB_PROJECT="$(metadata_value PUBSUB_PROJECT "sotalab-prod")"
  cat >> /etc/archipelago-eval.env <<EOF
PUBSUB_SUBSCRIPTION=$PUBSUB_SUBSCRIPTION
PUBSUB_PROJECT=$PUBSUB_PROJECT
EOF
  pip3 install --break-system-packages -q google-cloud-pubsub
  cat > /etc/systemd/system/archipelago-eval-worker.service <<'UNIT'
[Unit]
Description=Archipelago eval worker (Pub/Sub subscriber)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
EnvironmentFile=/etc/archipelago-eval.env
ExecStart=/usr/bin/python3 /opt/archipelago/scripts/eval_worker.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/archipelago-eval-worker.log
StandardError=append:/var/log/archipelago-eval-worker.log

[Install]
WantedBy=multi-user.target
UNIT

  systemctl daemon-reload
  systemctl enable archipelago-eval-worker.service
  systemctl restart archipelago-eval-worker.service

  echo "[setup_worker] worker service started"
else
  echo "[setup_worker] no worker service started; use RUN_DYNAMIC_QUEUE=1 for GCS queue workers or launch worker_static.py manually"
fi
echo "[setup_worker] done at $(date -Iseconds)"
