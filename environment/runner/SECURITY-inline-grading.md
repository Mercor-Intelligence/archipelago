# Inline grading: what runs where, and what protects it

Inline grading scores a trajectory inside the sandbox that produced it, instead
of shipping snapshots to a clean container on the async lane. That saves the S3
round trip and the lane's queue. It also puts the grade in a box the model was
working in, which is what this document is about.

## The two containers

Per trajectory there are two, and confusing them is the source of most of the
confusion about this feature.

**The runner.** A Modal function. The agent's brain runs here and this is where
the LLM provider keys live. It creates the sandbox below and drives it.

**The sandbox.** A Modal Sandbox created from the world image. The model's shell
commands execute here through a code-exec service. `/filesystem` and
`/.apps_data` live here, and so does the environment runner that serves `/grade`.
The grading image is mounted here at `/app/grading`.

Modal sandboxes run under gVisor, so root in the sandbox is not root on a host.

## The exposure

In the worlds this feature targets, `CODE_EXEC_RUN_AS_USER` is unset, so the
model's commands run as **root** in the sandbox. That variable is only ever
supplied by the GDM docker-world island and the GDM delivery export builder
(`world_zip_builder.py:1690` returns empty containers for every other world), so
uid separation is not available here and a guard on it would refuse every
trajectory in the rollout.

Two consequences follow, and both need a **live process** to exploit:

1. The grade subprocess's environment is readable through `/proc/<pid>/environ`.
2. `mount_image` has no read-only flag, so the grading tree is writable and the
   grader itself can be rewritten before it runs.

## What we do about it

### What travels

The judge goes through the LiteLLM proxy pair from
`grading_credentials(proxy_only=True)`. No `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY` or `GOOGLE_API_KEY` enters the sandbox. Those are
account-level and cannot be revoked per run, and a proxy token is scoped to our
own gateway.

Reference files (`artifacts_to_reference`, `sources`) parse through the same
document cache the lane reads, but never with the lane's key. The lane sends
`INTERNAL_API_KEY`, which opens every `/internal` route. The sandbox gets a
parse pass instead (`packages/document_cache/inline_pass.py`): an HMAC over its
audience, its trajectory and an expiry 30 minutes out, signed with a key
derived from `INTERNAL_API_KEY`. It opens one route,
`/inline-grading/document-cache/parse`, which parses only the bytes it is
sent. A stolen pass buys parses on our Reducto account until it expires.
`REDUCTO_API_KEY` does not travel. When the cache fails on a reference file in
a way that direct Reducto might not (a dropped connection, a 429 or a 5xx), the
lane parses with Reducto, so the in-sandbox grade errors and is handled like
any other failed inline grade.

On a no-web-access world the sandbox's egress allowlist also names the Studio
API host. The allowlist is set when the sandbox is created, so the host is
reachable for the whole episode, while the pass arrives only with the grade.

Only `prod` has a reachable proxy (`LITELLM_PROXY_PUBLIC_API_BASE_BY_ENV`).
Everywhere else this refuses and the lane grades, because falling back to the
direct keys is the one thing it must not do.

### The sandbox is emptied before the grader arrives

`POST /grade/sweep` kills every process started after a reference the caller read
from the sandbox before the agent loop, and kills the code-exec service, which is
the model's only route to running new commands.

Details that matter:

- The reference is **ticks since boot**, not wall clock. A root process can call
  `settimeofday`, so a wall-clock reference would be forgeable by exactly the
  process this exists to catch.
- Enumeration and signalling happen **in Python**, not through `ps` or `kill`.
  Root could have replaced either, and a sweep that asks a tampered tool what is
  running has answered nothing.
- It **refuses if `/proc` cannot be trusted**. Root can mount a tmpfs over it,
  and an empty process table would otherwise read as an empty sandbox, which is
  the one wrong answer: grading beside a watcher while reporting none exists.
- It **kills a parent that replaces a killed child**, however old the parent is.
  A worker pool's manager (PHP-FPM, Collabora's forkit) refills every worker the
  sweep kills, so without this the listing never comes back empty.
- It **refuses if anything survives**.

### The order is the protection

The grading image is mounted only **after** the sweep reports clean. So the
writable tree never exists while anything is alive to write to it, and the door
it could come back through is already shut.

### The answer key travels only after the answer is fixed

Goldens are what the grade compares against. Only `/grade/start` fetches them,
and only after a clean `/grade/sweep` in the same sandbox with request signing
enforced. The sweep answers `accepts_goldens`, the runner sends that answer on
the create-run request, and the server presigns goldens only when it is true.
By then the agent's processes and its code-exec door are gone, so nothing of the
agent's is left to change the tree the key is compared against. The archives
land in the root-only grade directory and go with it when the grade ends.

The sweep needs signing because the caller chooses its reference. Without
signing, any process on localhost could ask for a sweep that kills nothing and
comes back clean.

`/grade` and `/grade/relay` refuse goldens with a 409 and never consult the
sweep. `/grade/relay/score` reads no snapshots at all, so it never fetches a
golden either. hosted-envs posts to those routes and bakes its goldens into the
image, so nothing here changes for it.

The gate is not what stops the model fetching the key. The URLs are presigned
server-side and returned only to a caller holding an API key, so a grade the
model posts itself carries none.

What is left is exfiltration: something inside the sandbox reads the key and
sends it out, contaminating a later run of that task. The sweep has killed the
agent's processes and its code-exec door by then, so this needs a process that
predates the sweep reference and was subverted through an endpoint. Such a
process can already forge the score outright, so withholding the key from it
protects nothing.

Do not turn this off without a replacement. Every eval that reads goldens
depends on it, `agentic_verifier` included, and without it each of those grades
goes to the lane. An unconditional refusal on this route did exactly that for
weeks before anyone noticed. A change to the gate goes through the inline
grading owners and updates this section in the same pull request.
`.agents/rules/inline-grading-goldens.md` says the same to coding agents.

## What this does not do

Stated here so it is not discovered later.

**A subverted elder process survives.** The filter is by start time. Something
that predates the reference and was subverted through an endpoint is not caught.
It can read the grade subprocess's environment, which holds the parse pass
beside the proxy token.

**A root agent can forge the score, not just the artifact.** The lane grades a
snapshot in a clean container, so a subverted runner there has to build a
filesystem that satisfies verifiers it cannot see. Here the verdicts come back
from the sandbox, and `finish_inline_grade` checks only that the status is
`completed` and that the score is a number. The verifiers travel into the
sandbox in the bundle as well. So a forged score costs reading the rubric and
returning a number, where before it cost building a convincing tree.

The capability is not new: the same runner uploads the snapshot the lane grades,
so it could always doctor the artifact. The cost is what changed.

**The baseline snapshot id is not owned.** The runner names the post-populate
capture on the create request, because the trajectory row does not carry it
until the save. `generate_grading_snapshot_archive_url` presigns whatever id it
is handed, and no table records which trajectory a snapshot belongs to, so an
id for another campaign's capture presigns that campaign's baseline archive.
Two things bound it: the caller needs an API key with access to this
trajectory's campaign, and the world has to name itself in the allow-list.
Closing it needs a snapshot-to-owner record.

**The proxy token is shared, not per run.** A per-run token would not change
what is exposed. The same value already reaches the agent's own sandbox for the
whole trajectory: `deploy-agent-runner-modal-reusable.yml:113` sets
`AGENT_RUNNER_LITELLM_API_KEY` from `LITELLM_PROXY_API_KEY`, and
`agent_sandbox/litellm_env.py:20` hands it to Claude Code as
`ANTHROPIC_API_KEY`. Scoping it per run has to start on that path, not this one.

So the claim is not that a hostile agent is harmless here. It is that the
described attacks are closed and the bar moves from leaving a shell running to
subverting our own runner.

## Every refusal falls back to the lane

Consistent across this path: no reachable proxy, a sweep that cannot report
clean, a mount that cannot run a tool, a missing baseline or golden archive, an
env runner that did not accept goldens, a budget-halted batch, a batch that
names its own judges. None of these grade
differently. They all hand the trajectory to the lane and it is scored exactly as
it is today.
