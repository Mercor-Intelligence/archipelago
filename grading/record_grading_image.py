r"""Tell Studio which image a grade should mount, or that it should mount none.

Reads the id that `build_mountable_image.py` wrote, adds provenance, and posts
it. Run by the grading deploy:

    uv run python record_grading_image.py \
        --modal-environment rl-studio-dev --image-json image.json

A missing `--image-json` means the build failed, and is handled the same way as
a failed post: clear the pointer, which means grade on the lane.

`--app-deployed false` suppresses the clear. The grading app was not shipped,
so the recorded id still matches the lane.

Exit codes, because the caller treats them differently:

    0  recorded
    1  not recorded, and the pointer is safe: cleared, or deliberately left
       because the app was not deployed. Grades go to the lane.
    2  not recorded and not cleared. The pointer may name an image built from
       older grading code than the lane now runs.

A 404 from the record alone proves nothing: the server rolls out gradually, so
that request may have hit an instance without the route while a grade reads the
row from one that has it. Only a 404 from the clear as well means the route is
absent everywhere this reaches, and then nothing can mount the row either. That
is the state on the release that first adds them.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

RECORD_PATH = "/internal/grading-images/mountable"
# The WAF blocks default user agents. This is documented in
# docs/guides/trajectory-and-modal-debugging-runbook.md, which has said since
# 2026-07-28: "A script using the default urllib/requests user agent gets a
# silent 403 while curl to the same URL succeeds. Always set an explicit
# User-Agent on every request."
#
# It rejects before the request reaches verify_api_key, which answers 401 and
# not 403. Reproduced against dev: urllib's default 403s, curl's and this one
# both 200.
USER_AGENT = "rl-studio-grading-deploy"
EXIT_RECORDED = 0
EXIT_ON_THE_LANE = 1
EXIT_POINTER_MAY_BE_STALE = 2
ATTEMPTS = 3
BACKOFF_SECONDS = (2, 4)
TIMEOUT_SECONDS = 30


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def provenance() -> dict[str, str]:
    """The commit, and the hash of the grading tree inside it.

    Two commits that leave `archipelago/grading` alone share the tree hash, so
    it identifies the source in the image where the commit does not.
    """
    return {
        "built_from_sha": _git("rev-parse", "HEAD"),
        "grading_tree_sha": _git("rev-parse", "HEAD:archipelago/grading"),
    }


def _send(
    url: str, headers: dict[str, str], *, method: str, body: dict[str, Any] | None
) -> None:
    """One request. Raises URLError or HTTPError, which the callers catch."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for name, value in {
        **headers,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }.items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        _ = response.read()


def post_with_retries(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    send: Any = _send,
    sleep: Any = time.sleep,
) -> tuple[bool, int | None]:
    """POST the id, retrying twice with a 2s then 4s backoff.

    Returns whether it landed, and the HTTP status of the last failure so the
    caller can tell a missing route from a broken one.
    """
    last_status: int | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            send(url, headers, method="POST", body=body)
            return True, None
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            # The body, because a rejection from the edge and one from the app
            # both arrive as a bare status. Cloudflare puts a ray id in here,
            # and verify_api_key puts its own detail. A 403 cost a day of
            # guessing without it.
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400].strip()
            except Exception:
                pass
            served_by = exc.headers.get("server") or exc.headers.get("Server") or "?"
            print(
                f"attempt {attempt}/{ATTEMPTS} failed: {exc} "
                f"[server={served_by}] {detail}",
                file=sys.stderr,
            )
        except (urllib.error.URLError, OSError) as exc:
            last_status = None
            print(f"attempt {attempt}/{ATTEMPTS} failed: {exc}", file=sys.stderr)
        if attempt < ATTEMPTS:
            sleep(BACKOFF_SECONDS[attempt - 1])
    return False, last_status


def clear(
    url: str, headers: dict[str, str], environment: str, *, send: Any = _send
) -> tuple[bool, int | None]:
    """Drop the pointer. Returns whether it went, and the failing status."""
    query = urllib.parse.urlencode({"modal_environment": environment})
    try:
        send(f"{url}?{query}", headers, method="DELETE", body=None)
        return True, None
    except urllib.error.HTTPError as exc:
        print(f"could not clear the pointer: {exc}", file=sys.stderr)
        return False, exc.code
    except (urllib.error.URLError, OSError) as exc:
        print(f"could not clear the pointer: {exc}", file=sys.stderr)
        return False, None


def _bool(value: str) -> bool:
    """Strict, because a typo must not pick a branch silently."""
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise ValueError(f"--app-deployed wants true or false, got {value!r}")


def _run(args) -> int:
    api = os.environ.get("RL_STUDIO_API", "").strip().rstrip("/")
    key = os.environ.get("RL_STUDIO_API_KEY", "").strip()
    # A key missing from the secrets blob renders as an empty string, not an
    # error, and an empty base builds a schemeless URL that raises deep in
    # urllib. Refuse here, where the reason is legible.
    if not api or not key:
        missing = ", ".join(
            n for n, v in (("RL_STUDIO_API", api), ("RL_STUDIO_API_KEY", key)) if not v
        )
        raise ValueError(f"{missing} is empty")
    headers = {"X-API-Key": key}
    url = f"{api}{RECORD_PATH}"
    app_deployed = _bool(args.app_deployed)

    image_json = Path(args.image_json)
    if image_json.exists():
        body: dict[str, Any] = json.loads(image_json.read_text())
        body.update(provenance())
        recorded, _ = post_with_retries(url, headers, body)
        if recorded:
            print(f"recorded {body['image_id']} for {body['modal_environment']}")
            return EXIT_RECORDED
    else:
        print(f"no {image_json}: the build did not produce an id", file=sys.stderr)

    if not app_deployed:
        print(
            "::warning::could not record the mountable grading image. The "
            "grading app was not redeployed, so the existing pointer still "
            "matches the lane and is left alone",
        )
        return EXIT_ON_THE_LANE

    print("::warning::could not record the mountable grading image; clearing it")
    cleared, clear_status = clear(url, headers, args.modal_environment)
    if cleared:
        return EXIT_ON_THE_LANE

    if clear_status == 404:
        print(
            "::warning::the grading-images route is not deployed yet, so "
            "nothing can read a pointer either",
        )
        return EXIT_ON_THE_LANE

    print(
        "::error::could not clear the pointer either; it may name an image "
        "built from older grading code than this deploy shipped",
    )
    return EXIT_POINTER_MAY_BE_STALE


def main() -> int:
    ap = argparse.ArgumentParser()
    _ = ap.add_argument("--image-json", required=True)
    _ = ap.add_argument("--modal-environment", required=True)
    _ = ap.add_argument(
        "--app-deployed",
        default="true",
        help="whether this deploy shipped the grading app",
    )
    args = ap.parse_args()

    try:
        return _run(args)
    except Exception as exc:
        # Python exits 1 on an unhandled exception, and 1 is the code that says
        # the pointer is safe. Anything unexpected has to land on 2 instead, or
        # a crash reports success and leaves a stale id recorded.
        print(f"::error::{type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            if _bool(args.app_deployed):
                api = os.environ.get("RL_STUDIO_API", "").strip().rstrip("/")
                key = os.environ.get("RL_STUDIO_API_KEY", "").strip()
                if api and key:
                    cleared, _ = clear(
                        f"{api}{RECORD_PATH}",
                        {"X-API-Key": key},
                        args.modal_environment,
                    )
                    if cleared:
                        return EXIT_ON_THE_LANE
        except Exception as inner:
            print(f"::error::clear also failed: {inner}", file=sys.stderr)
        return EXIT_POINTER_MAY_BE_STALE


if __name__ == "__main__":
    sys.exit(main())
