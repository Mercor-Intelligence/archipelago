"""Shared helpers for mapping verifiers to the 1-based positions shown in the UI."""

from collections import defaultdict

from runner.models import Verifier


def build_display_positions(verifiers: list[Verifier]) -> dict[str, int]:
    """Map verifier_id → 1-based display position within its scope.

    The UI renders World and Task verifiers in separate tables, each numbered
    from 1 by row position (``row.index + 1``).  We group verifiers by their
    scope key (task_id or world_id) so the number shown in error messages
    matches the table ops actually sees.  Within each group the verifiers are
    sorted by ``verifier_index`` and assigned sequential 1-based positions.
    """
    groups: dict[str | None, list[Verifier]] = defaultdict(list)
    for v in verifiers:
        scope = v.task_id or v.world_id
        groups[scope].append(v)

    positions: dict[str, int] = {}
    for group in groups.values():
        for i, v in enumerate(sorted(group, key=lambda v: v.verifier_index)):
            positions[v.verifier_id] = i + 1
    return positions
