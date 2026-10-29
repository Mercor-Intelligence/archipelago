"""Write the package profile's MANIFEST.json; exit 1 unless the venv holds exactly the pinned set.

Run with the profile venv's interpreter:
  <venv>/bin/python -I scripts/write_profile_manifest.py <profile> <requirements> <launcher> <out>
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from importlib import metadata


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(requirements_path: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in open(requirements_path, encoding="utf-8"):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = line.partition("==")
        if not sep or not version.strip():
            raise SystemExit(f"profile line is not an exact pin: {raw.strip()!r}")
        pins[_norm(name.strip())] = version.strip()
    return pins


def main(profile: str, requirements_path: str, launcher: str, out_path: str) -> int:
    pins = _pins(requirements_path)
    installed = {_norm(d.metadata["Name"]): d.version for d in metadata.distributions()}
    if installed != pins:
        missing = sorted(set(pins) - set(installed))
        extra = sorted(set(installed) - set(pins))
        wrong = sorted(n for n in set(pins) & set(installed) if pins[n] != installed[n])
        print(
            f"profile {profile} mismatch: missing={missing} extra={extra} wrong={wrong}",
            file=sys.stderr,
        )
        return 1
    with open(requirements_path, "rb") as f:
        requirements_sha256 = hashlib.sha256(f.read()).hexdigest()
    manifest = {
        "profile": profile,
        "requirements_sha256": requirements_sha256,
        "python": sys.executable,
        "python_version": platform.python_version(),
        "launcher": launcher,
        "packages": dict(sorted(installed.items())),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:5]))
