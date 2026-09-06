#!/bin/bash
# Simple curl-based auth testing

BASE_URL="http://localhost:8000"

echo "=== Authentication Testing ===" 
echo

echo "TEST 1: Health Check (Public)"
curl -s http://localhost:8000/health | jq
echo

echo "TEST 2: Login as admin"
ADMIN_RESPONSE=$(curl -s -X POST "$BASE_URL/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "login_tool", "arguments": {"username": "admin", "password": "admin123"}}}')

# Parse SSE response
ADMIN_TOKEN=$(echo "$ADMIN_RESPONSE" | grep "^data:" | sed 's/^data: //' | jq -r '.result.structuredContent.token')
echo "Admin token: ${ADMIN_TOKEN:0:20}..."
echo

echo "TEST 3: Admin calls protected endpoint (read_data)"
curl -s -X POST "$BASE_URL/mcp" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read_data", "arguments": {}}}' \
  | grep "^data:" | sed 's/^data: //' | jq '.result.structuredContent'
echo

echo "TEST 4: Login as viewer"
VIEWER_RESPONSE=$(curl -s -X POST "$BASE_URL/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "login_tool", "arguments": {"username": "viewer", "password": "view123"}}}')

VIEWER_TOKEN=$(echo "$VIEWER_RESPONSE" | grep "^data:" | sed 's/^data: //' | jq -r '.result.structuredContent.token')
echo "Viewer token: ${VIEWER_TOKEN:0:20}..."
echo

echo "TEST 5: Viewer tries to write (should fail - no write scope)"
curl -s -X POST "$BASE_URL/mcp" \
  -H "Authorization: Bearer $VIEWER_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "write_data", "arguments": {"data": "test"}}}' \
  | grep "^data:" | sed 's/^data: //' | jq '.result.content[0].text'
echo

echo "TEST 6: Admin writes (should succeed)"
curl -s -X POST "$BASE_URL/mcp" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "write_data", "arguments": {"data": "test"}}}' \
  | grep "^data:" | sed 's/^data: //' | jq '.result.structuredContent'
echo

echo "=== Testing Complete ==="
