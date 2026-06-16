#!/usr/bin/env bash
# End-to-end PoC test. Must run on Linux (LD_PRELOAD). See Dockerfile to run in
# a container from the macOS host.
set -euo pipefail
cd "$(dirname "$0")"

KEY="$(python3 encrypt_db.py genkey)"
echo "[poc] generated key: ${KEY:0:8}… (32 bytes)"

# 1. Build a plaintext sample DB with a 'secrets' table.
rm -f plain.db enc.db
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("plain.db")
c.execute("CREATE TABLE secrets(id INTEGER PRIMARY KEY, label TEXT, value TEXT)")
c.executemany(
    "INSERT INTO secrets(label, value) VALUES (?,?)",
    [("answer_key", "POLICY-42-APPROVED"), ("gt_grade", "9.7"), ("flag", "do-not-read")],
)
c.commit(); c.close()
print("[poc] built plain.db")
PY

# 2. Encrypt it (this is what the delivery layer would do at build time).
python3 encrypt_db.py encrypt --in plain.db --out enc.db --key "$KEY"

# 3. Build the shim.
gcc -shared -fPIC -O2 -o decrypt_fs.so decrypt_fs.c -ldl -lpthread -lcrypto
echo "[poc] built decrypt_fs.so"

echo
echo "=== A) AGENT's view: raw read of the encrypted file (NO shim) ==="
echo "--- first 16 bytes (should NOT be 'SQLite format 3') ---"
head -c 16 enc.db | xxd
echo "--- sqlite3 opening it directly (should FAIL: not a database) ---"
if sqlite3 enc.db "SELECT value FROM secrets;" 2>&1; then
  echo "[poc][FAIL] agent read the data without the shim!"; exit 1
else
  echo "[poc][OK] agent sees ciphertext — cannot read the secrets"
fi

echo
echo "=== B) APP's view: same file, opened WITH the decrypt shim ==="
GOT="$(DB_ENC_KEY="$KEY" DB_ENC_PATH="$PWD/enc.db" \
       LD_PRELOAD="$PWD/decrypt_fs.so" \
       sqlite3 enc.db "SELECT value FROM secrets ORDER BY id;" 2>&1)"
echo "$GOT"
EXPECT=$'POLICY-42-APPROVED\n9.7\ndo-not-read'
if [ "$GOT" = "$EXPECT" ]; then
  echo "[poc][OK] app decrypted transparently and read the rows"
else
  echo "[poc][FAIL] app did not read expected rows"; exit 1
fi

echo
echo "[poc] PASS — same bytes on disk: ciphertext to the agent, plaintext to the app."
