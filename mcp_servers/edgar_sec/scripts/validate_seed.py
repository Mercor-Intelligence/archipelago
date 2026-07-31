#!/usr/bin/env python3
"""Offline schema validator for the EDGAR SEC offline-data seed.

The populate task copies at most one ``*.zip`` from the data directory to
``data/edgar_offline.zip`` and extracts it to ``./offline_data``. The server
then reads from ``./offline_data/edgar_offline/`` (see
``mcp_servers/edgar_sec/config.py``: ``EDGAR_OFFLINE_DATA_DIR`` defaults to
``REPO_ROOT/offline_data/edgar_offline``). So the canonical layout is an
``edgar_offline`` tree:

    edgar_offline/
      company_tickers.json         (JSON object: index -> {cik_str, ticker, title})
      companyfacts/CIK*.json        (>=1 file, each named CIK<cik>.json)
      submissions/CIK*.json         (>=1 file, each named CIK<cik>.json)

This validator accepts either form found in the data directory:
  * an unzipped tree (the dir itself is ``edgar_offline`` or contains one), or
  * a single ``*.zip`` (its entries are inspected without extracting).

Calibration (error vs warning). The gate answers "is this seed clean and
loadable by the server?", not "would populate exit 0?". Errors (exit 1) are
things that leave the server unable to read the seed:
  * an absent seed (no tree and no zip) — a generated seed with nothing is a
    failed generation, not a pass;
  * a zip whose content is NOT under an ``edgar_offline/`` root — extraction
    would land it outside ``offline_data/edgar_offline/`` and the server would
    find nothing;
  * ``company_tickers.json`` missing, unparseable, or not the expected JSON
    object (ticker/CIK lookup reads it as a dict of records);
  * ``companyfacts/`` or ``submissions/`` missing, empty, or holding a file not
    named ``CIK*.json`` — the server keys lookups by ``CIK{cik}.json`` filename,
    so a mis-named file is invisible (data loss);
  * any ``*.json`` that does not parse.

Pure Python 3 stdlib. Exit 0 clean, 1 on any error, 2 on usage error.

Usage:
    python3 scripts/validate_seed.py <data_directory>
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

REQUIRED_SUBDIRS = ("companyfacts", "submissions")
# EDGAR filing files are named CIK followed by the (zero-padded) CIK number.
CIK_FILE_RE = re.compile(r"^CIK\d+\.json$")
ROOT_DIR = "edgar_offline"


def _validate_tickers_obj(obj: object) -> list[str]:
    """The company_tickers map must be a non-empty JSON object of records."""
    errors: list[str] = []
    if not isinstance(obj, dict):
        errors.append(f"company_tickers.json must be a JSON object (got {type(obj).__name__})")
        return errors
    if not obj:
        errors.append("company_tickers.json is an empty object (no ticker records)")
        return errors
    # Spot-check the record shape: each value is an object carrying at least a
    # CIK (cik_str) and a ticker — the fields the lookup path reads.
    for key, rec in obj.items():
        if not isinstance(rec, dict):
            errors.append(f"company_tickers.json['{key}'] is not an object")
            continue
        if "cik_str" not in rec:
            errors.append(f"company_tickers.json['{key}'] missing 'cik_str'")
        if "ticker" not in rec:
            errors.append(f"company_tickers.json['{key}'] missing 'ticker'")
    return errors


def validate_tree(root: Path) -> list[str]:
    errors: list[str] = []
    tickers = root / "company_tickers.json"
    try:
        obj = json.loads(tickers.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"company_tickers.json is not valid JSON ({exc})")
    else:
        errors += _validate_tickers_obj(obj)
    for sub in REQUIRED_SUBDIRS:
        d = root / sub
        if not d.is_dir():
            errors.append(f"missing required directory '{sub}/'")
            continue
        jsons = sorted(d.glob("*.json"))
        if not jsons:
            errors.append(f"'{sub}/' contains no *.json files")
            continue
        for jf in jsons:
            if not CIK_FILE_RE.match(jf.name):
                errors.append(
                    f"{sub}/{jf.name}: filename must match CIK<cik>.json "
                    "(the server keys lookups by CIK{cik}.json)"
                )
            try:
                json.loads(jf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{sub}/{jf.name} is not valid JSON ({exc})")
    return errors


def _find_root(data_dir: Path) -> Path | None:
    if (data_dir / "company_tickers.json").is_file():
        return data_dir
    child = data_dir / ROOT_DIR
    if (child / "company_tickers.json").is_file():
        return child
    return None


def _parse_zip_json(zf: zipfile.ZipFile, entry: str) -> object | str:
    """Return the parsed JSON, or an error string if the entry is not JSON."""
    try:
        return json.loads(zf.read(entry).decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"{entry} (in zip) is not valid JSON ({exc})"


def validate_zip(zpath: Path) -> list[str]:
    errors: list[str] = []
    try:
        zf = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile:
        return [f"{zpath.name} is not a valid zip archive"]
    names = [n for n in zf.namelist() if not n.endswith("/")]

    # The zip must carry an edgar_offline/ root: populate extracts it to
    # ./offline_data and the server reads ./offline_data/edgar_offline/. Content
    # outside that root ends up in the wrong place and is never read.
    non_rooted = [n for n in names if not n.startswith(f"{ROOT_DIR}/")]
    if non_rooted:
        errors.append(
            f"{zpath.name}: entries not under '{ROOT_DIR}/' root "
            f"(e.g. {non_rooted[0]}); the server reads offline_data/{ROOT_DIR}/"
        )

    tickers = [n for n in names if n == f"{ROOT_DIR}/company_tickers.json"]
    if not tickers:
        errors.append(f"{zpath.name}: no {ROOT_DIR}/company_tickers.json entry")
    else:
        parsed = _parse_zip_json(zf, tickers[0])
        if isinstance(parsed, str):
            errors.append(f"{zpath.name}: {parsed}")
        else:
            errors += [f"{zpath.name}: {e}" for e in _validate_tickers_obj(parsed)]

    for sub in REQUIRED_SUBDIRS:
        prefix = f"{ROOT_DIR}/{sub}/"
        sub_entries = [n for n in names if n.startswith(prefix) and n.endswith(".json")]
        if not sub_entries:
            errors.append(f"{zpath.name}: no '{ROOT_DIR}/{sub}/' *.json entries")
            continue
        for n in sub_entries:
            base = n.rsplit("/", 1)[-1]
            if not CIK_FILE_RE.match(base):
                errors.append(f"{zpath.name}: {n}: filename must match CIK<cik>.json")
            parsed = _parse_zip_json(zf, n)
            if isinstance(parsed, str):
                errors.append(f"{zpath.name}: {parsed}")
    return errors


def validate(data_dir: Path) -> int:
    # Validate EVERY seed form present. The populate task deploys a *.zip from
    # STATE_LOCATION, so a zip (when present) must always be validated — never
    # short-circuited by an on-disk tree. The bundled sample ships an unzipped
    # tree, which is also validated. An absent seed fails: this is a required
    # seed lifecycle check, so "no seed at all" is not a pass.
    errors: list[str] = []
    checked: list[str] = []

    zips = sorted(data_dir.glob("*.zip"))
    if len(zips) > 1:
        print(f"ERROR: expected at most 1 .zip, found {len(zips)}", file=sys.stderr)
        return 1
    if zips:
        errors += validate_zip(zips[0])
        checked.append(f"zip archive {zips[0].name}")

    root = _find_root(data_dir)
    if root is not None:
        errors += validate_tree(root)
        checked.append(f"unzipped tree at {root}")

    if not checked:
        print(
            f"ERROR: no EDGAR seed found in {data_dir} (no {ROOT_DIR} tree and no *.zip)",
            file=sys.stderr,
        )
        return 1
    checked_str = " + ".join(checked)

    if errors:
        print(f"=== {len(errors)} Validation Error(s) ({checked_str}) ===", file=sys.stderr)
        for i, err in enumerate(errors, 1):
            print(f"  {i}. {err}", file=sys.stderr)
        return 1
    print(f"OK: EDGAR offline seed valid ({checked_str}).")
    return 0


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: validate_seed.py <data_directory>", file=sys.stderr)
        sys.exit(2)
    data_dir = Path(sys.argv[1])
    if not data_dir.is_dir():
        print(f"ERROR: not a directory: {data_dir}", file=sys.stderr)
        sys.exit(2)
    sys.exit(validate(data_dir))


if __name__ == "__main__":
    main()
