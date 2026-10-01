# Standalone DB grading

`await run_from_evidence(input, final_filesystem_root=..., owned_scratch_root=...)`
uses the production `EvalImplInput` and returns the production `VerifierResult`.
The input contains separate initial and final archive streams, verifier/config
values and trajectory. Streams remain caller-owned and may be shared by sibling
readers: preparation uses independent cursors for Linux file descriptors,
`BytesIO`, and binary `SpooledTemporaryFile` streams. Callers must not modify or
close evidence during grading. Lazy trajectory content is resolved before threaded
preparation. The existing `db_code_verifier_eval` delegates with `/` and the system
temporary directory.

Call this entry inside a fresh privileged Linux grading worker. It launches the
existing shim as UID/GID 65534 with the existing AST gate, import policy, resource
limits and stripped environment. The worker must contain no application services,
model credentials or cloud credentials. Do not run it in a resources server.
The Python executable and installed dependencies must be accessible to UID 65534.

Both roots must already exist. The filesystem root contains `filesystem/` and
`.apps_data/`. The caller supplies the final filesystem view independently of the
archive streams. The function does not restore application state. It creates a
unique scratch child for staged code, databases, extraction files and the child's
home/temp directory, then removes that child after success, error or cancellation.
Archive preparation runs in a thread. Cancellation waits for preparation and
process launch to finish, kills/reaps the child, and only then removes scratch.
The caller must retain both roots and streams until the function finishes.

Missing or corrupt required DB evidence returns `status=ERROR`. Downstream grading
must treat that status as unavailable, even though the result's numeric score is
zero. A valid negative verdict has `status=OK`.

## Isolated worker obligations

`isolated_baseline_worker_v1` must enforce the following before NeMo admission.
This entry point alone does not implement that worker contract.

- Give authored code a final filesystem view it cannot modify, including through
  ownership, group permissions, ACLs or symlinks. Stage it under a trusted owner
  and remove write access for UID/GID 65534 before calling this entry. Use a
  disposable worker-owned copy so permission preparation preserves retained caller
  evidence bytes and modes. The shared filesystem helper adds read/traverse
  permissions; it does not remove existing write permissions. It expects
  permission changes to be possible, so passing an
  already read-only mount requires separate integration support.
- Start the whole worker without ambient credentials, including credential files,
  provider metadata access and runtime model/cloud secrets. The stripped child
  environment does not isolate the privileged preparation process or the worker's
  filesystem and network.
- Apply an outer deadline and memory/disk limits to the whole worker. The authored
  subprocess timeout starts after preparation. Cancelling the await cannot stop
  its preparation thread, and cleanup waits for that thread to finish. A hung
  preparation or cleanup therefore requires worker termination.
- Pass `max_output_bytes` to bound combined stdout/stderr before parsing. Overflow
  kills and reaps the child and returns `ERROR`. Without this option, the entry
  uses unbounded `communicate`; log tails are presentation limits only. Separately
  bound serialized result transfer and worker memory.
- On deadline, cancellation or transport failure, terminate the whole worker and
  its descendants, then reclaim its storage through the worker owner/reaper.
  Local cleanup kills/reaps the direct subprocess only. Keep retained evidence
  alive until worker termination is confirmed. Report limit and infrastructure
  failures as unavailable, never as a valid negative verdict.

## Runtime closure

`runtime_sources.json` lists source paths relative to `grading/`. Copy their bytes
from one immutable Archipelago source commit, retaining directory structure.
It includes package initializers and their imports, plus the subprocess shims.
Do not concatenate, rewrite or replace the production modules.

`requirements.in` pins direct dependencies to the Studio grading lock.
`requirements.txt` locks their transitive closure with hashes for CPython 3.13 on
Linux x86_64. Install with:

```sh
uv pip install --python /path/to/worker/python --require-hashes \
  -r runner/evals/db_code_verifier/requirements.txt
```

This includes pandas and openpyxl, both permitted by the canonical import policy,
and sqlglot/jsonpath-ng imported by helper package initializers. BLAS threads are
limited to one under the unchanged 512 MiB process address-space limit.
Legacy-configured imports and optional pandas/openpyxl backends outside this lock
need separate admission/dependency validation before packaging.

LiteLLM remains required because `runner/models.py` uses its `AllMessageValues`
and `Message` types in Pydantic trajectory models. These types participate in
runtime validation, so they cannot be moved behind `TYPE_CHECKING`. The DB worker
does not use LiteLLM to call a model. Removing the dependency requires separating
the shared trajectory models from LiteLLM first.

To regenerate against the grading project's lock, run from `grading/`:

```sh
uv export --frozen --no-hashes --no-emit-project --all-groups \
  -o /path/to/grading-constraints.txt
uv pip compile runner/evals/db_code_verifier/requirements.in \
  --constraint /path/to/grading-constraints.txt \
  --python-version 3.13 --python-platform x86_64-manylinux_2_28 \
  --exclude-newer 2026-09-07 --generate-hashes \
  -o runner/evals/db_code_verifier/requirements.txt
```

The standalone tests use real SQLite ZIPs and the actual privileged subprocess.
Run `pytest tests/test_db_code_verifier_standalone.py` in the privileged worker.
Non-root test runs explicitly skip the privilege-dependent cases.

## Source publication prerequisite

Studio's `oss_archipelago` island stages and publishes the public Archipelago tree.
The inspected public commit `16646501d2bdd96874588f4232146c519f278135`
does not contain `db_code_verifier`. Studio's OSS `filters.py` also excludes its
eval ID and module directory. A Studio-local verifier commit cannot ship through
the current NeMo source resolver alone.

Before enabling NeMo baseline admission, the source publication change must admit
the DB verifier's enum/registry entry and source directory, preserve this complete
closure and dependency lock, and publish the reviewed source through the existing
OSS process or a coordinated companion Archipelago PR. A direct upstream-only
change would be overwritten by the next Studio OSS sync unless those filters are
updated too. Pin and validate the resulting immutable public commit in the NeMo
export. Keep other islands' source resolution unchanged.
