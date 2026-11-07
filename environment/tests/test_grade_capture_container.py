"""`POST /grade/capture` in the real environment image, checked against the lane's upload set."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import uuid
import zipfile

import httpx
import pytest
from testcontainers.core.container import DockerContainer

_EXCLUDE = ["filesystem/*/.cache/*"]
_LANE_UPLOAD = """
import hashlib, json, sys
from runner.data.snapshot.main import _collect_subsystem_files
from runner.grade import _SNAPSHOT_SUBSYSTEMS
files = _collect_subsystem_files(_SNAPSHOT_SUBSYSTEMS, "snap", json.loads(sys.argv[1]))
print(json.dumps({
    key.removeprefix("snap/"): hashlib.sha256(open(path, "rb").read()).hexdigest()
    for path, key in files
}))
"""
_CAPTURE_PATH = """
import sys
from runner.grading_paths import capture_dir
print(capture_dir(sys.argv[1]) / "final.zip")
"""


def _exec(container: DockerContainer, *argv: str) -> str:
    result = container.exec(list(argv))
    output = result.output.decode()
    assert result.exit_code == 0, output
    return output.strip()


def _tar_gz(files: dict[str, bytes], links: dict[str, str]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        for name, target in links.items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return raw.getvalue()


async def _populate(
    client: httpx.AsyncClient,
    base_url: str,
    subsystem: str,
    archive: bytes,
) -> None:
    response = await client.post(
        f"{base_url}/data/populate",
        params={"subsystem": subsystem},
        files={"archive": ("seed.tar.gz", archive, "application/gzip")},
        timeout=60,
    )
    assert response.status_code == 200, response.text


def _under(root: str, entries: dict[str, str]) -> dict[str, str]:
    return {
        name: digest for name, digest in entries.items() if name.split("/")[1] == root
    }


def _read_capture(container: DockerContainer, capture_id: str) -> dict[str, str]:
    path = _exec(container, "python", "-c", _CAPTURE_PATH, capture_id)
    chunks, _ = container.get_wrapped_container().get_archive(path)
    with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as outer:
        member = outer.extractfile(outer.getmembers()[0])
        assert member is not None
        final_zip = member.read()
    with zipfile.ZipFile(io.BytesIO(final_zip)) as zf:
        return {
            name: hashlib.sha256(zf.read(name)).hexdigest() for name in zf.namelist()
        }


@pytest.mark.asyncio
async def test_capture_holds_exactly_what_the_lane_uploads(
    base_url: str, environment_container: DockerContainer
) -> None:
    root = f"capture-{uuid.uuid4().hex[:8]}"
    fs = f"/filesystem/{root}"
    try:
        async with httpx.AsyncClient() as client:
            await _populate(
                client,
                base_url,
                "filesystem",
                _tar_gz(
                    {
                        f"{root}/report.txt": b"report",
                        f"{root}/nested/data.bin": bytes(range(256)) * 64,
                        f"{root}/.cache/scratch": b"excluded",
                    },
                    {f"{root}/link_in": "report.txt"},
                ),
            )
            await _populate(
                client,
                base_url,
                ".apps_data",
                _tar_gz({f"{root}/state.json": b"{}"}, {}),
            )
            _exec(environment_container, "ln", "-s", "/etc/hostname", f"{fs}/link_out")
            _exec(environment_container, "ln", "-s", "/nonexistent", f"{fs}/broken")
            _exec(environment_container, "cp", f"{fs}/report.txt", f"{fs}/epoch.txt")
            _exec(environment_container, "touch", "-d", "@0", f"{fs}/epoch.txt")

            response = await client.post(
                f"{base_url}/grade/capture",
                json={"snapshot_exclude_globs": _EXCLUDE},
                timeout=120,
            )
            assert response.status_code == 200, response.text
            capture_id = response.json()["capture_id"]

        captured = _under(root, _read_capture(environment_container, capture_id))
        lane = _under(
            root,
            json.loads(
                _exec(
                    environment_container,
                    "python",
                    "-c",
                    _LANE_UPLOAD,
                    json.dumps(_EXCLUDE),
                )
            ),
        )

        assert captured == lane
        report = hashlib.sha256(b"report").hexdigest()
        assert captured[f"filesystem/{root}/link_in"] == report
        assert captured[f"filesystem/{root}/epoch.txt"] == report
        assert f"filesystem/{root}/link_out" in captured
        assert f"filesystem/{root}/broken" not in captured
        assert f"filesystem/{root}/.cache/scratch" not in captured
        assert f".apps_data/{root}/state.json" in captured
    finally:
        _exec(environment_container, "rm", "-rf", fs, f"/.apps_data/{root}")
