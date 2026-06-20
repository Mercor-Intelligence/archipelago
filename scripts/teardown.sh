#!/usr/bin/env bash
# Tear down all GCP resources created for the archipelago eval.
# Usage: ./scripts/teardown.sh <project> <zone> <bucket> <worker_count>
#
# Args default to the values used in the 2026-06-16 seed-pro-pass3 run.

set -uo pipefail

PROJECT="${1:-sotalab-prod}"
ZONE="${2:-asia-east1-b}"
BUCKET="${3:-sotalab-archipelago-eval}"
WORKER_COUNT="${4:-5}"

CONFIRM="${CONFIRM:-no}"
if [[ "${CONFIRM}" != "yes" ]]; then
  cat <<EOF
============================================================
TEARDOWN (preview, nothing destroyed)
============================================================
  project     : ${PROJECT}
  zone        : ${ZONE}
  bucket      : gs://${BUCKET}
  workers     : ${WORKER_COUNT}

To actually run, set CONFIRM=yes:
  CONFIRM=yes $0 $*

Or via Makefile:
  make teardown CONFIRM=yes
============================================================
EOF
  exit 0
fi

echo "[teardown] deleting ${WORKER_COUNT} VMs"
for i in $(seq 1 "${WORKER_COUNT}"); do
  name="archipelago-eval-worker-${i}"
  echo "  - $name"
  gcloud compute instances delete "$name" \
    --project="${PROJECT}" --zone="${ZONE}" --quiet 2>&1 | tail -1
done

echo "[teardown] emptying + deleting GCS bucket"
gsutil -m rm -r "gs://${BUCKET}/**" 2>&1 | tail -1 || true
gcloud storage buckets delete "gs://${BUCKET}" --project="${PROJECT}" --quiet 2>&1 | tail -1

echo "[teardown] done."
