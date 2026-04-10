"""Standalone CLI entrypoint for the grading runner.

Accepts a single --grading-config JSON file (containing grading_settings,
verifiers, eval_configs, and scoring_config) instead of four separate files.

Usage:
    python -m runner.cli \
        --initial-snapshot /data/snapshot_before.zip \
        --final-snapshot /data/snapshot_after.zip \
        --trajectory /data/trajectory.json \
        --grading-config /data/grading_config.json \
        --output /data/results.json \
        [--golden-snapshot /data/golden.zip]
"""

import argparse
import asyncio
import io
import json
import sys
import uuid
from typing import Any

from pydantic import TypeAdapter
from runner.evals.models import EvalConfig
from runner.models import (
    AgentTrajectoryOutput,
    GradingSettings,
    ScoringMethodResult,
    Verifier,
    VerifierResult,
)
from runner.scoring_methods.models import ScoringConfig

from .main import main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone grader CLI — runs verifiers and scoring against a trajectory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  python -m runner.cli \\
    --initial-snapshot before.zip \\
    --final-snapshot after.zip \\
    --trajectory trajectory.json \\
    --grading-config grading_config.json \\
    --output results.json
""",
    )
    parser.add_argument(
        "--initial-snapshot",
        type=str,
        required=True,
        help="Path to the pre-agent-run filesystem snapshot ZIP",
    )
    parser.add_argument(
        "--final-snapshot",
        type=str,
        required=True,
        help="Path to the post-agent-run filesystem snapshot ZIP",
    )
    parser.add_argument(
        "--trajectory",
        type=str,
        required=True,
        help="Path to the agent trajectory JSON file",
    )
    parser.add_argument(
        "--grading-config",
        type=str,
        required=True,
        help="Path to the unified grading config JSON (grading_settings, verifiers, eval_configs, scoring_config)",
    )
    parser.add_argument(
        "--golden-snapshot",
        type=str,
        action="append",
        dest="golden_snapshots",
        help="Path to a golden response snapshot ZIP (can be repeated)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path to write the results JSON (default: stdout)",
    )
    parser.add_argument(
        "--grading-run-id",
        type=str,
        default=None,
        help="Optional grading run ID (auto-generated if omitted)",
    )
    return parser.parse_args(argv)


def load_grading_config(
    path: str,
) -> tuple[GradingSettings, list[Verifier], list[EvalConfig], ScoringConfig]:
    """Load and validate the unified grading config JSON."""
    with open(path) as f:
        raw = json.load(f)

    grading_settings = GradingSettings.model_validate(raw["grading_settings"])
    verifiers = TypeAdapter(list[Verifier]).validate_python(raw["verifiers"])
    eval_configs = TypeAdapter(list[EvalConfig]).validate_python(raw["eval_configs"])
    scoring_config = ScoringConfig.model_validate(raw["scoring_config"])

    return grading_settings, verifiers, eval_configs, scoring_config


def load_snapshot(path: str) -> io.BytesIO:
    """Load a snapshot ZIP file into memory."""
    with open(path, "rb") as f:
        return io.BytesIO(f.read())


def format_output(
    grading_run_id: str,
    grading_run_status: str,
    verifier_results: list[VerifierResult],
    scoring_results: ScoringMethodResult,
) -> dict[str, Any]:
    return {
        "grading_run_id": grading_run_id,
        "grading_run_status": str(grading_run_status),
        "verifier_results": [v.model_dump(mode="json") for v in verifier_results],
        "scoring_results": scoring_results.model_dump(mode="json"),
    }


def cli_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Load inputs
    initial_snapshot = load_snapshot(args.initial_snapshot)
    final_snapshot = load_snapshot(args.final_snapshot)

    with open(args.trajectory) as f:
        trajectory = AgentTrajectoryOutput.model_validate(json.loads(f.read()))

    grading_settings, verifiers, eval_configs, scoring_config = load_grading_config(
        args.grading_config
    )

    golden_snapshots: list[io.BytesIO] = []
    if args.golden_snapshots:
        for path in args.golden_snapshots:
            golden_snapshots.append(load_snapshot(path))

    grading_run_id = args.grading_run_id or f"gr_{uuid.uuid4().hex}"

    # Run grading
    (
        result_grading_run_id,
        grading_run_status,
        verifier_results,
        scoring_results,
    ) = asyncio.run(
        main(
            grading_run_id=grading_run_id,
            trajectory_id="standalone",
            initial_snapshot_bytes=initial_snapshot,
            final_snapshot_bytes=final_snapshot,
            trajectory=trajectory,
            grading_settings=grading_settings,
            verifiers=verifiers,
            eval_configs=eval_configs,
            scoring_config=scoring_config,
            golden_snapshots=golden_snapshots if golden_snapshots else None,
        )
    )

    output = format_output(
        result_grading_run_id,
        grading_run_status,
        verifier_results,
        scoring_results,
    )

    if args.output:
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2)
    else:
        json.dump(output, sys.stdout, indent=2)
        print()

    return 0 if str(grading_run_status) == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(cli_main())
