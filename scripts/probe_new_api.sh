#!/usr/bin/env bash
# Probe SoTALab New API staging model availability + a tiny completion.
#
# Verifies that the New API token can be fetched from Secret Manager, that
# the candidate models respond with sane tokens, and reports per-model
# latency + token count.
#
# Usage: ./scripts/probe_new_api.sh [model_id ...]
#   defaults to the models used in this repo's tests

set -uo pipefail

PROJECT="${PROJECT:-sotalab-staging}"
SECRET="${SECRET:-NEW_API_TOKEN-staging}"
BASE="${BASE:-https://new-api-staging.sotalab.ai/v1}"

MODELS=("$@")
if [[ ${#MODELS[@]} -eq 0 ]]; then
  MODELS=(
    "doubao-seed-2-0-pro-260215"
    "qwen3.6-27b"
    "qwen3.7-max"
    "gpt-5.4"
  )
fi

echo "============================================================"
echo " New API probe"
echo "   base    : $BASE"
echo "   secret  : $SECRET (project=$PROJECT)"
echo "============================================================"

TOKEN="$(gcloud secrets versions access latest \
  --project="$PROJECT" --secret="$SECRET" 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
  echo "ERROR: failed to fetch token from Secret Manager"
  exit 1
fi
echo "  token    : <${#TOKEN} chars>"
echo

# List endpoint
echo "  models endpoint (/v1/models):"
curl -sS "$BASE/models" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
ids = sorted({m['id'] for m in d.get('data', [])})
print(f'    {len(ids)} models:')
for i in ids:
    print(f'      - {i}')
"
echo

# Per-model completion probe
echo "  per-model smoke test:"
for m in "${MODELS[@]}"; do
  echo "    $m"
  t0=$(date +%s%N)
  resp=$(curl -sS "$BASE/chat/completions" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -w '\n__HTTP_CODE__:%{http_code}\n__TIME__:%{time_total}\n' \
    -d "{
      \"model\": \"$m\",
      \"messages\": [{\"role\": \"user\", \"content\": \"Reply with one word: ok\"}],
      \"max_tokens\": 30
    }")
  t1=$(date +%s%N)
  http_code=$(echo "$resp" | grep '__HTTP_CODE__' | cut -d: -f2)
  curl_time=$(echo "$resp" | grep '__TIME__' | cut -d: -f2)
  body=$(echo "$resp" | grep -v '__HTTP_CODE__\|__TIME__')
  if [[ "$http_code" == "200" ]]; then
    content=$(echo "$body" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d['choices'][0]['message']['content'][:50])
except Exception as e:
    print(f'<parse err: {e}>')
")
    usage=$(echo "$body" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    u = d.get('usage', {})
    print(f\"in={u.get('prompt_tokens','?')} out={u.get('completion_tokens','?')} total={u.get('total_tokens','?')}\")
except: pass
" 2>/dev/null)
    printf "      HTTP %s  time=%ss  reply=%-20s  %s\n" \
      "$http_code" "$curl_time" "\"$content\"" "$usage"
  else
    printf "      HTTP %s  FAILED: %s\n" "$http_code" \
      "$(echo "$body" | head -c 200)"
  fi
done

echo
echo "  done."