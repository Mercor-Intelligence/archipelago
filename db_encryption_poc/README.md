# DB decryption shim — proof of concept (Path B)

**Problem.** The agent can read app `.db` files directly with `bash` (e.g.
`cat /.apps_data/<svc>/data.db`). It should only ever reach that data through
the MCP tools. The customer's sandbox **strips all privilege** and runs under
**gVisor**, so *no access-control wall holds*: the agent's `bash`, the app, and
the grader all run as the same flat principal, and Landlock / namespaces aren't
available. There is nothing for the OS to discriminate on.

**Approach.** Stop guarding the file; lock the data. Encrypt the DB at rest and
load a small `LD_PRELOAD` shim into the **trusted app** process that holds the
key and decrypts page bytes on read. The app works normally; the agent's `bash`
is a separate process with no shim and no key, so it sees ciphertext. The
distinguisher is *possession of a secret tied to our code*, not privilege —
which is the one thing the privilege-flattening sandbox can't take away.

This is the sibling of `code_execution_server/sandbox_fs.c`: same interposition
technique, opposite job. That shim *denies* paths for the untrusted model; this
one *decrypts* for the trusted app (Séb's "LD_PRELOAD shim that decrypts the DB
in place on each read, on the app server side so it can't be removed by the
agent").

## Files

| File | What |
|------|------|
| `decrypt_fs.c` | The `LD_PRELOAD` shim. Intercepts `open`/`openat`/`pread`/`read`/`close`; for the configured DB path, decrypts bytes on read. |
| `encrypt_db.py` | Encrypts a plaintext SQLite file into the on-disk ciphertext format (and can decrypt). Pure Python (`cryptography`); testable on any host. |
| `run_test.sh` | End-to-end: build a sample DB, encrypt it, build the shim, then show ciphertext to a raw reader and plaintext to a shim-loaded `sqlite3`. |
| `Dockerfile` | Builds + runs the test on Linux (LD_PRELOAD can't run on macOS). |

## Run it

```bash
# From the repo root (Linux, or via the Dockerfile on macOS):
docker build -t db-decrypt-poc -f db_encryption_poc/Dockerfile db_encryption_poc
docker run --rm db-decrypt-poc
```

Expected: part **A** (no shim) prints ciphertext and `sqlite3` fails with "file
is not a database"; part **B** (shim loaded) prints the secret rows.

## Crypto scheme

AES-256-CTR over the whole file. The 128-bit counter is derived purely from the
byte offset (`counter = offset / 16`, big-endian, starting at 0). Because the
counter is a function of the offset, any aligned region decrypts independently —
exactly what SQLite needs for random-access **page** reads, with no need to hold
the whole DB in memory. `encrypt_db.py` and `decrypt_fs.c` implement the same
scheme and must stay in sync.

## Known limitations / TODO before this is production

- **Key management (most important).** The PoC reads the key from `DB_ENC_KEY`.
  That is for the test only. In production the key must be injected into the app
  process by the delivery layer at launch and **never** written to a file or an
  env var the agent's shell can read. (Corridor flagged this; it's the crux.)
  The agent could otherwise read the key and decrypt the file itself.
- **WAL / mmap.** This PoC handles the rollback-journal default and `pread`/
  `read`. SQLite in WAL mode adds `-wal`/`-shm` (shared memory) files, and with
  `mmap_size>0` reads bypass `pread`. Production must either pin
  `journal_mode`/`mmap_size=0` or intercept `mmap` and handle the side files.
- **Writes.** Only read-path decryption is implemented. If the app writes to the
  DB during a task, the shim must also encrypt on `pwrite` and keep the on-disk
  copy consistent. (Many seeded task DBs are read-only, so read-only is a valid
  first cut — but confirm per app.)
- **`openat` with a dirfd + relative path** is resolved best-effort; production
  should match on the resolved fd path robustly.
- **Residual risk.** Encryption converts "`cat` the file" into "extract the key
  from the running app's memory" — much harder, and enough to defeat the
  observed behavior, but not absolute under fully flat privilege. Keep
  `sandbox_fs.so` as defense-in-depth (it still blocks `/proc`).

## Where this belongs (not here long-term)

This lives in the harness repo only as a self-contained PoC. The real home is
the **app/framework side** — either the shared `mcp-shared` framework that apps
build on, or injected at the **delivery layer** (studio) at image-build time the
way `sandbox_fs.so` already is — so that **zero app repos** need editing. See
the conversation notes: most apps default to in-memory SQLite (no file, nothing
to protect); only apps with a file-backed `DATABASE_URL` (e.g. workday →
`/.apps_data/workday/workday_hcm.db`) are in scope.
