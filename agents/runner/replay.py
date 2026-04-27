"""Replay a previous run from its manifest sidecar.

Stub implementation: validates the manifest, warns if git_sha
has drifted from current HEAD, and re-invokes runner.main with
the captured config. Does NOT attempt snapshot restoration -
that requires environment-side coordination and is out of scope
for this PR.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from loguru import logger


def replay(manifest_path: Path) -> int:
    data = json.loads(manifest_path.read_text())
    logger.info(f"Loaded manifest: trajectory_id={data['trajectory_id']}")

    manifest_sha = data.get("git_sha")
    if manifest_sha:
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if current and current != manifest_sha:
            logger.warning(
                f"git_sha drift: manifest={manifest_sha[:8]} "
                f"current={current[:8]}. Replay may not be faithful."
            )

    logger.warning(
        "Replay subcommand is a stub. Snapshot restoration is not "
        "yet implemented. Manifest validation passed; use the "
        "captured config to invoke runner.main manually:"
    )
    logger.info(
        json.dumps(
            {
                "agent_config_id": data["agent_config_id"],
                "orchestrator_model": data["orchestrator_model"],
                "orchestrator_extra_args": data["orchestrator_extra_args"],
                "seed": data.get("seed"),
                "deterministic": data.get("deterministic", False),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m runner.replay <manifest.json>")
        sys.exit(2)
    sys.exit(replay(Path(sys.argv[1])))
