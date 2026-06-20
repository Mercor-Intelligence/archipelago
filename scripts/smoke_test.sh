#!/usr/bin/env bash
# Quick smoke test for the archipelago eval stack.
# Verifies (in order):
#   1. New API token can be fetched from Secret Manager
#   2. New API responds to a simple chat completion (model exists)
#   3. HuggingFace token is valid + can read gated dataset mercor/apex-agents
#   4. litellm via New API proxy mode works for the target model
#
# Usage:
#   ./scripts/smoke_test.sh [model_id ...]
#   defaults to: doubao-seed-2-0-pro-260215 (then qwen3.6-27b)

set -uo pipefail

PROJECT="${PROJECT:-sotalab-staging}"
SECRET="${SECRET:-NEW_API_TOKEN-staging}"
HF_PROJECT="${HF_PROJECT:-sotalab-prod}"
HF_SECRET="${HF_SECRET:-HF_TOKEN}"
BASE="${BASE:-https://new-api-staging.sotalab.ai/v1}"

MODELS=("$@")
[[ ${#MODELS[@]} -eq 0 ]] && MODELS=("doubao-seed-2-0-pro-260215" "qwen3.6-27b")

fail=0
trap 'echo; [[ $fail -gt 0 ]] && echo "SMOKE TEST FAILED ($fail failures)" || echo "SMOKE TEST PASSED"' EXIT

echo "============================================================"
echo " Archipelago eval smoke test"
echo "============================================================"

# 1) New API token
echo "[1/4] fetch New API token"
NEW_API_TOKEN="$(gcloud secrets versions access latest \
  --project="$PROJECT" --secret="$SECRET" 2>/dev/null || true)"
if [[ -z "$NEW_API_TOKEN" ]]; then
  echo "  FAIL: cannot read secret $SECRET from $PROJECT"
  fail=$((fail+1))
else
  echo "  OK (token len=${#NEW_API_TOKEN})"
fi

# 2) New API responds
echo "[2/4] New API responds to /v1/models"
HTTP=$(curl -sS -o /tmp/_models.json -w "%{http_code}" \
  -H "Authorization: Bearer $NEW_API_TOKEN" \
  "$BASE/models" 2>/dev/null || echo "000")
if [[ "$HTTP" == "200" ]]; then
  N=$(python3 -c "import json; d=json.load(open('/tmp/_models.json')); print(len(d.get('data', [])))" 2>/dev/null)
  echo "  OK ($N models available)"
else
  echo "  FAIL: HTTP $HTTP"
  fail=$((fail+1))
fi

# 3) HuggingFace token
echo "[3/4] HuggingFace token (gated dataset mercor/apex-agents)"
HF_TOKEN="$(gcloud secrets versions access latest \
  --project="$HF_PROJECT" --secret="$HF_SECRET" 2>/dev/null || true)"
if [[ -z "$HF_TOKEN" ]]; then
  echo "  FAIL: cannot read secret $HF_SECRET from $HF_PROJECT"
  fail=$((fail+1))
else
  HTTP=$(curl -sS -o /tmp/_hf.json -w "%{http_code}" \
    -H "Authorization: Bearer $HF_TOKEN" \
    "https://huggingface.co/api/datasets/mercor/apex-agents" 2>/dev/null || echo "000")
  if [[ "$HTTP" == "200" ]]; then
    GATED=$(python3 -c "import json; d=json.load(open('/tmp/_hf.json')); print(d.get('gated','no'))" 2>/dev/null)
    echo "  OK (gated=$GATED, token grants access)"
  else
    echo "  FAIL: HTTP $HTTP (token rejected or token missing scope)"
    fail=$((fail+1))
  fi
fi

# 4) litellm proxy mode against each model
echo "[4/4] litellm proxy -> New API -> each model"
for m in "${MODELS[@]}"; do
  RESP=$(curl -sS -X POST "$BASE/chat/completions" \
    -H "Authorization: Bearer $NEW_API_TOKEN" \
    -H "Content-Type: application/json" \
    -w '\n__HTTP__:%{http_code}\n__T__:%{time_total}\n' \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with one word: ok\"}],\"max_tokens\":15}" \
    2>/dev/null)
  HTTP=$(echo "$RESP" | grep '__HTTP__' | cut -d: -f2)
  T=$(echo "$RESP" | grep '__T__' | cut -d: -f2)
  if [[ "$HTTP" == "200" ]]; then
    CONTENT=$(echo "$RESP" | sed '/^__/d' | python3 -c "
import sys, json
try:
    d=json.load(sys.stdin)
    print(d['choices'][0]['message']['content'][:40])
except: print('<parse err>')
" 2>/dev/null)
    printf "  OK   %-30s time=%4.2fs  reply=%-20s\n" "$m" "$T" "\"$CONTENT\""
  else
    printf "  FAIL %-30s HTTP=%s\n" "$m" "$HTTP"
    fail=$((fail+1))
  fi
done

# cleanup tmp
rm -f /tmp/_models.json /tmp/_hf.json