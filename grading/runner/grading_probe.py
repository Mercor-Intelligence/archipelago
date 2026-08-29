"""Candidate-snapshot synthesis for the high-fidelity grading probe.

The server sends the probe a battery of *candidate specs* (label / kind /
source); this module turns each spec into the ``final_snapshot_bytes`` the real
grader diffs against the base, plus the ``FINAL_ANSWER`` text carried on the
synthetic trajectory. It is pure stdlib (``io`` + ``zipfile``) so it is
unit-testable offline, with no Modal / grader dependency.

Two rules from the SPEC (§4) drive the shapes here:

- **A candidate's final snapshot is ``base ∪ deliverable``, never the deliverable
  alone.** The grader diffs the candidate against the task's base (initial) env;
  a snapshot holding only the deliverable would diff as "the agent deleted the
  entire environment". So every candidate overlays its deliverable onto a copy
  of the base members.
- **A degradation must change a file's size or path.** A same-size, same-path
  edit to a non-tabular file diffs as UNCHANGED and is never graded. ``truncate``
  / ``empty_file`` change size; ``drop_key_file`` changes the path set.

Snapshot layout: a ``ZIP_STORED`` zip whose deliverable files live under
``filesystem/`` (member names preserved verbatim). ``filesystem/answer.txt`` is
the conventional text-answer file; a text golden materialized there is read back
as the golden's ``FINAL_ANSWER``.
"""

from __future__ import annotations

import io
import zipfile
from typing import IO, Any, Protocol

SNAPSHOT_PREFIX = "filesystem/"
ANSWER_PATH = "filesystem/answer.txt"
NOTES_PATH = "filesystem/NOTES.md"

# Perturbations the probe applies to a golden deliverable (each changes size or
# path so the snapshot diff never marks the file UNCHANGED).
PERTURBATIONS = ("empty_file", "truncate", "drop_key_file")


class _VerifierLike(Protocol):
    """The two fields the cap-pruning reads off a runner ``Verifier``."""

    verifier_id: str
    verifier_dependencies: list[str] | None


def verifiers_past_agentic_cap[V: _VerifierLike](
    verifiers: list[V], agentic_ids: set[str]
) -> tuple[list[V], list[str]]:
    """The verifier set to grade for a candidate PAST the agentic cap.

    Past the cap the probe drops agentic verifiers — running a full coding agent
    for every degraded candidate is the cost the cap exists to bound. But a
    verifier that DEPENDS on a dropped one would then run with an unmet
    dependency and error, which reads as report noise, not a real verdict. So
    those dependents are dropped too, transitively (a dependent of a dependent
    goes as well).

    Returns ``(kept, pruned_dependent_ids)``: ``kept`` is the verifiers still
    graded for the candidate; ``pruned_dependent_ids`` is the non-agentic
    verifiers dropped only because they depend on a capped one (sorted, for a
    report note). Terminates on dependency cycles — ``dropped`` only grows and
    is bounded by the verifier count.
    """
    dropped: set[str] = set(agentic_ids)
    changed = True
    while changed:
        changed = False
        for v in verifiers:
            if v.verifier_id in dropped:
                continue
            if any(dep in dropped for dep in (v.verifier_dependencies or [])):
                dropped.add(v.verifier_id)
                changed = True
    kept = [v for v in verifiers if v.verifier_id not in dropped]
    return kept, sorted(dropped - set(agentic_ids))


def read_zip_members(data: IO[bytes]) -> dict[str, bytes]:
    """Every file member of a snapshot zip as ``{name: bytes}`` (dirs skipped).
    Leaves the stream seeked to 0 for reuse."""
    data.seek(0)
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(data) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            members[info.filename] = zf.read(info.filename)
    data.seek(0)
    return members


def write_stored_zip(members: dict[str, bytes]) -> io.BytesIO:
    """Pack ``members`` into a seekable ``ZIP_STORED`` spool (names verbatim)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    out.seek(0)
    return out


def _deliverable_names(members: dict[str, bytes]) -> list[str]:
    """The ``filesystem/`` deliverable file names (hidden components skipped,
    mirroring the diff/agentic-verifier convention)."""
    names: list[str] = []
    for name in members:
        if not name.startswith(SNAPSHOT_PREFIX):
            continue
        rel = name[len(SNAPSHOT_PREFIX) :]
        if not rel or name.endswith("/"):
            continue
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        names.append(name)
    return names


def pick_key_file(members: dict[str, bytes]) -> str | None:
    """The largest deliverable member (the file a degradation defects), or
    ``None`` when there is no deliverable to degrade."""
    names = _deliverable_names(members)
    if not names:
        return None
    return max(names, key=lambda n: len(members[n]))


def _apply_perturbation(
    members: dict[str, bytes], key: str | None, perturbation: str
) -> dict[str, bytes]:
    out = dict(members)
    if key is None:
        return out
    if perturbation == "empty_file":
        out[key] = b""
    elif perturbation == "truncate":
        out[key] = out[key][: len(out[key]) // 2]
    elif perturbation == "drop_key_file":
        out.pop(key, None)
    else:
        raise ValueError(f"unknown perturbation {perturbation!r}")
    return out


def golden_answer_text(golden_members: dict[str, bytes]) -> str:
    """The golden's text answer if it materialized one to ``answer.txt``, else
    ``""`` (an artifact golden is graded by its files, not a text answer)."""
    body = golden_members.get(ANSWER_PATH)
    if body is None:
        return ""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def build_candidate_snapshot(
    candidate: dict[str, Any],
    base_members: dict[str, bytes],
    golden_members: dict[str, bytes] | None,
) -> tuple[io.BytesIO, str]:
    """Build one candidate's ``(final_snapshot_bytes, final_answer_text)``.

    ``base_members`` is the task's base (initial) env; the returned snapshot is
    always ``base ∪ deliverable``. ``golden_members`` is the golden this
    candidate derives from (its own snapshot for ``golden``; the reference golden
    for ``degraded``); ``None``/empty is tolerated (the candidate degrades to an
    empty deliverable over the base).

    Source precedence (a spec sets exactly one): injection → perturbation →
    golden/snapshot → text.
    """
    base = dict(base_members or {})
    golden = golden_members or {}
    injection = candidate.get("injection_note")
    perturbation = candidate.get("perturbation")
    snapshot_id = candidate.get("snapshot_id")
    text = candidate.get("text")
    kind = candidate.get("kind")

    if injection:
        # A non-responsive deliverable whose only content is the steer note:
        # the grader should FAIL it; a PASS is evidence it was steered. Written
        # both as the answer and into a likely-read file an agentic grader sees.
        note = injection.encode("utf-8")
        members = {**base, ANSWER_PATH: note, NOTES_PATH: note}
        return write_stored_zip(members), injection

    if perturbation:
        overlaid = {**base, **golden}
        # Defect a file the golden actually CONTRIBUTES (differs from the base
        # env), not a shared base file the verifier ignores — else a "degraded"
        # candidate could still pass. Fall back to any golden/overlaid file when
        # the golden adds nothing distinct.
        golden_deliverable = {
            name: body for name, body in golden.items() if base.get(name) != body
        }
        key = (
            pick_key_file(golden_deliverable)
            or pick_key_file(golden)
            or pick_key_file(overlaid)
        )
        overlaid = _apply_perturbation(overlaid, key, perturbation)
        return write_stored_zip(overlaid), ""

    if snapshot_id is not None or kind == "golden":
        members = {**base, **golden}
        return write_stored_zip(members), golden_answer_text(golden)

    # text (including the empty "wrong" answer → base env, no deliverable added).
    members = dict(base)
    if text:
        members[ANSWER_PATH] = text.encode("utf-8")
    return write_stored_zip(members), text or ""
