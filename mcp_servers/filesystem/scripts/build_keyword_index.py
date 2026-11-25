"""Build a keyword search index ahead of time, for upload to a world or task.

Same builder and format the app's populate task uses. Upload the result under
``.apps_data/filesystem/`` (any name ending in ``.sqlite``).

    uv run python scripts/build_keyword_index.py ./world_files world.sqlite
    uv run python scripts/build_keyword_index.py ./turn2 turn_2.sqlite --prefix turn_inputs/turn_2
"""

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "mcp_servers" / "filesystem_server")
)

from keyword_search.config import SUPPORTED_FILE_TYPES  # noqa: E402
from keyword_search.index import build_index  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "root", type=Path, help="Folder whose contents become /filesystem in the run"
    )
    parser.add_argument("output", type=Path, help="Index file to write (.sqlite)")
    parser.add_argument(
        "--prefix",
        default="",
        help="Path the root will have under /filesystem, e.g. turn_inputs/turn_2",
    )
    parser.add_argument(
        "--file-types",
        nargs="+",
        default=list(SUPPORTED_FILE_TYPES),
        choices=SUPPORTED_FILE_TYPES,
    )
    parser.add_argument("--max-seconds-per-file", type=float, default=120.0)
    parser.add_argument("--max-total-text-mb", type=float, default=2048.0)
    args = parser.parse_args()

    if not args.root.is_dir():
        parser.error(f"{args.root} is not a folder")
    if args.output.suffix != ".sqlite":
        parser.error(
            "the output file must end in .sqlite, or the server will not find it"
        )

    def show(done: int, total: int) -> None:
        print(f"\r{done}/{total} files", end="", file=sys.stderr, flush=True)

    summary = build_index(
        args.root.resolve(),
        args.output,
        file_types=args.file_types,
        path_prefix=args.prefix,
        max_seconds_per_file=args.max_seconds_per_file,
        max_total_chars=int(args.max_total_text_mb * 1024 * 1024),
        on_progress=show,
    )
    print(
        f"\n{summary.files_total} files in {summary.seconds:.1f}s: {summary.counts}",
        file=sys.stderr,
    )
    with closing(sqlite3.connect(args.output)) as conn:
        for path, status, detail in conn.execute(
            "SELECT path, status, detail FROM files WHERE status != 'indexed' ORDER BY status, path"
        ):
            print(f"{status}\t{path}\t{detail or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
