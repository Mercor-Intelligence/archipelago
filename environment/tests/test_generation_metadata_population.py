"""World-generation metadata (`filesystem/.meta/`) never enters the solver's filesystem."""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runner.data.populate import utils

_FILES = {
    "01_Company/profile.docx": b"world doc",
    "notes/.meta/keep.txt": b"nested folder the world owns",
    ".metadata/keep.txt": b"not the metadata dir",
    ".meta/single_agent_export_runs/seeding_report.json": b"seed report",
    ".meta/single_agent_export_bundle/odoo_erp/seed.py": b"seed script",
}
_WITHHELD = {name for name in _FILES if name.startswith(".meta/")}


def _fake_s3(monkeypatch: pytest.MonkeyPatch, prefix: str, downloaded: list[str]):
    class Objects:
        async def filter(self, **kwargs):
            assert kwargs == {"Prefix": prefix}
            for name, body in _FILES.items():
                yield SimpleNamespace(key=f"{prefix}/{name}", size=len(body))

    async def bucket(_name):
        return SimpleNamespace(objects=Objects())

    async def get_object(*, Key, **kwargs):
        name = Key.removeprefix(prefix + "/")
        downloaded.append(name)
        return {"Body": SimpleNamespace(read=AsyncMock(return_value=_FILES[name]))}

    @asynccontextmanager
    async def client(**kwargs):
        yield SimpleNamespace(
            Bucket=bucket,
            meta=SimpleNamespace(client=SimpleNamespace(get_object=get_object)),
        )

    monkeypatch.setattr(utils, "get_s3_client", client)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["boto3", "s5cmd"])
async def test_filesystem_population_withholds_generation_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    prefix = "worlds/snap/filesystem"
    downloaded: list[str] = []
    _fake_s3(monkeypatch, prefix, downloaded)
    fast = AsyncMock()
    monkeypatch.setattr(
        utils, "get_s5cmd_downloader", lambda _: fast if backend == "s5cmd" else None
    )
    monkeypatch.setattr(utils, "key_is_eligible", lambda _: True)
    monkeypatch.setattr(utils, "FILESYSTEM_ROOT", str(tmp_path))

    count = await utils.download_objects(
        "snapshots", prefix, str(tmp_path).lstrip("/"), backend=backend
    )

    expected = set(_FILES) - _WITHHELD
    assert count == len(expected)
    # A prefix copy cannot leave keys out, so it is never used once one is withheld.
    fast.download_prefix.assert_not_awaited()
    assert set(downloaded) == expected
    assert {
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()
    } == expected


@pytest.mark.asyncio
async def test_other_subsystems_keep_generation_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = "worlds/snap/.apps_data"
    downloaded: list[str] = []
    _fake_s3(monkeypatch, prefix, downloaded)
    monkeypatch.setattr(utils, "get_s5cmd_downloader", lambda _: None)

    count = await utils.download_objects(
        "snapshots", prefix, ".apps_data", dest_root=str(tmp_path)
    )

    assert count == len(_FILES)
    assert set(downloaded) == set(_FILES)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (".meta/x.json", True),
        ("./.meta/x.json", True),
        ("/.meta/x.json", True),
        (".meta", True),
        ("a/.meta/x.json", False),
        (".metadata/x.json", False),
        ("meta/x.json", False),
    ],
)
def test_is_generation_metadata(key: str, expected: bool) -> None:
    assert utils.is_generation_metadata(key) is expected
