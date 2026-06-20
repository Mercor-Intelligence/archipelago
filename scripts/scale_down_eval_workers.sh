#!/usr/bin/env bash
# Stop running Archipelago eval worker VMs to reduce idle GCE cost.
#
# Usage:
#   archipelago/scripts/scale_down_eval_workers.sh
#   DRY_RUN=1 archipelago/scripts/scale_down_eval_workers.sh
#   WORKER_PREFIX=archipelago-eval-auto archipelago/scripts/scale_down_eval_workers.sh

set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
WORKER_PREFIX="${WORKER_PREFIX:-archipelago-eval}"
DRY_RUN="${DRY_RUN:-0}"

if [[ -z "${PROJECT}" ]]; then
  echo "PROJECT is empty. Set PROJECT or run: gcloud config set project <project-id>" >&2
  exit 1
fi

RUNNING_WORKERS=()
while IFS= read -r line; do
  [[ -n "${line}" ]] && RUNNING_WORKERS+=("${line}")
done < <(
  gcloud compute instances list \
    --project="${PROJECT}" \
    --filter="name~^${WORKER_PREFIX} AND status=RUNNING" \
    --format="csv[no-heading](name,zone)"
)

if [[ "${#RUNNING_WORKERS[@]}" -eq 0 ]]; then
  echo "[scale-down] no RUNNING instances found for prefix '${WORKER_PREFIX}' in project '${PROJECT}'"
  exit 0
fi

echo "[scale-down] project=${PROJECT} prefix=${WORKER_PREFIX}"
printf '%s\n' "${RUNNING_WORKERS[@]}" | awk -F, '{printf "  - %s (%s)\n", $1, $2}'

if [[ "${DRY_RUN}" == "1" || "${DRY_RUN}" == "true" ]]; then
  echo "[scale-down] DRY_RUN enabled; no instances stopped"
  exit 0
fi

ZONES=()
while IFS= read -r zone; do
  [[ -n "${zone}" ]] && ZONES+=("${zone}")
done < <(printf '%s\n' "${RUNNING_WORKERS[@]}" | awk -F, '{print $2}' | sort -u)

for zone in "${ZONES[@]}"; do
  NAMES=()
  while IFS= read -r name; do
    [[ -n "${name}" ]] && NAMES+=("${name}")
  done < <(
    printf '%s\n' "${RUNNING_WORKERS[@]}" \
      | awk -F, -v zone="${zone}" '$2 == zone {print $1}'
  )
  if [[ "${#NAMES[@]}" -eq 0 ]]; then
    continue
  fi
  echo "[scale-down] stopping ${#NAMES[@]} instance(s) in ${zone}: ${NAMES[*]}"
  gcloud compute instances stop "${NAMES[@]}" \
    --project="${PROJECT}" \
    --zone="${zone}" \
    --quiet
done

echo "[scale-down] final status"
gcloud compute instances list \
  --project="${PROJECT}" \
  --filter="name~^${WORKER_PREFIX}" \
  --format="table(name,zone,machineType.basename(),status)"
