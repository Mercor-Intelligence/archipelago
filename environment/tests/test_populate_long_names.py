"""An object whose name the filesystem cannot hold is renamed, never dropped.

A Gmail attachment is keyed `<message_id>:<attachment_id>`, one segment around
485 bytes, so populate writes it under a deterministically shortened name.
"""

from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from runner.data.populate import utils

# Verbatim from the run that failed (prun_b6d872a473b9), as in
# `test_zip_builder_long_names.py`: one real key, so a change to the limit or to
# the segment split is caught against the real shape, not a synthetic one.
_REAL_GMAIL_KEY = (
    "filesystem/slice/services/gmail/files/1902be8b5e9e4ffe:ANGjdJ9RAW5rUDo05Hz"
    "OYv2sRM0SCujgX0zQyeFq2lCGEC0QZLmJ0S0AklctETYoWf6URHBsLs6n6ZC1tYIj70Uxt_Jcq"
    "D6UHxi1ioMPecAZ52W8yRPC2qsGef8N0pEh9ygOssFOtixHvjh_RvIbNApmjebFbMTkkC2p8Cc"
    "dNErbWYwM-20p-F-pB9wyJqEmfh5IRN4_H4ogHi1viW2QxbIoVyCAhc-a102cb738f427712"
)

# `SHORT_NAME_VECTORS` IS DUPLICATED, ON PURPOSE, into the hydrate_worker and
# seed_convert suites: three deployables with no shared code, and one object
# shortened two ways is two files. As `RULE_VECTORS` is, for file subtraction.
SHORT_NAME_VECTORS: list[tuple[str, str, str]] = [
    (
        "a name under the limit is returned unchanged",
        "filesystem/slice/services/slack/processed/messages.parquet",
        "84f608a79a2904c5",
    ),
    (
        "the real gmail attachment key",
        _REAL_GMAIL_KEY,
        "08f7eab9ab2b32c0",
    ),
    (
        "a segment exactly at the limit is left alone",
        "filesystem/" + "a" * 255 + "/x.pdf",
        "f15fbecf2626209f",
    ),
    (
        "one byte over is cut, and the suffix survives",
        "filesystem/" + "a" * 256 + "/x.pdf",
        "d5d6060c2c92f571",
    ),
    (
        "the limit counts utf-8 bytes, not characters",
        "files/" + "中" * 128 + "/n.txt",
        "1b61f1c4b005abe8",
    ),
    (
        "a multi-byte name keeps its suffix",
        "F1/" + "é" * 200 + ".docx",
        "28ab499051ea5ab6",
    ),
    (
        "a suffix wider than the hash is dropped rather than kept",
        "files/" + "z" * 300 + ".verylongextension",
        "4f447a205a16a94f",
    ),
    (
        "every over-long segment of a path is cut, not just the last",
        "a/" + "b" * 300 + "/" + "c" * 300 + "/d.txt",
        "abe139fae479c01a",
    ),
    (
        "keys differing only inside the long segment stay apart",
        "filesystem/files/" + "A" * 300 + "-one",
        "cbf993a882acf586",
    ),
    (
        "...and so does the other one",
        "filesystem/files/" + "A" * 300 + "-two",
        "1576900450c63816",
    ),
]

_REAL_GMAIL_REL = _REAL_GMAIL_KEY.removeprefix("filesystem/")


class _Body:
    async def read(self) -> bytes:
        return b"sqlite"


class _S3Client:
    async def get_object(self, **_kwargs: object) -> dict[str, _Body]:
        return {"Body": _Body()}


class _Objects:
    def __init__(self, objs: list[SimpleNamespace]) -> None:
        self.objs = objs

    async def filter(self, **_kwargs: object) -> AsyncIterator[SimpleNamespace]:
        for obj in self.objs:
            yield obj


class _Bucket:
    def __init__(self, objs: list[SimpleNamespace]) -> None:
        self.objects = _Objects(objs)


class _S3Resource:
    def __init__(self, objs: list[SimpleNamespace]) -> None:
        self.objs = objs
        self.meta = SimpleNamespace(client=_S3Client())

    async def Bucket(self, _name: str) -> _Bucket:
        return _Bucket(self.objs)


class _S3Context:
    def __init__(self, objs: list[SimpleNamespace]) -> None:
        self.resource = _S3Resource(objs)

    async def __aenter__(self) -> _S3Resource:
        return self.resource

    async def __aexit__(self, *_args: object) -> None:
        return None


class _RecordingS5cmd:
    def __init__(self) -> None:
        self.calls = 0

    async def download_prefix(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1


@pytest.mark.parametrize(("pins", "rel_path", "expected"), SHORT_NAME_VECTORS)
def test_the_shortening_rule(pins: str, rel_path: str, expected: str) -> None:
    short = utils.short_rel_path(rel_path)
    assert sha256(short.encode()).hexdigest()[:16] == expected, pins
    assert all(
        len(segment.encode()) <= utils.NAME_MAX_BYTES for segment in short.split("/")
    )


def test_a_segment_at_the_limit_is_left_alone() -> None:
    at_limit = f"files/{'a' * utils.NAME_MAX_BYTES}/x.pdf"
    over_limit = f"files/{'a' * (utils.NAME_MAX_BYTES + 1)}/x.pdf"
    assert utils.short_rel_path(at_limit) == at_limit
    assert utils.short_rel_path(over_limit) != over_limit


@pytest.mark.asyncio
async def test_populate_writes_the_over_long_name_and_drops_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "filesystem"
    root.mkdir()
    monkeypatch.setattr(utils, "FILESYSTEM_ROOT", str(root))

    key = "pipelines/snap_123/filesystem"
    objs = [
        SimpleNamespace(key=f"{key}/slice/services/slack/t.csv", size=6),
        SimpleNamespace(key=f"{key}/{_REAL_GMAIL_REL}", size=6),
    ]
    monkeypatch.setattr(utils, "get_s3_client", lambda **_kwargs: _S3Context(objs))
    monkeypatch.setattr(utils, "get_s5cmd_downloader", lambda _backend: None)

    count = await utils.download_objects(
        bucket="snapshots", key=key, subsystem=str(root).lstrip("/")
    )

    assert count == 2
    assert (root / "slice/services/slack/t.csv").read_bytes() == b"sqlite"
    attachment = root / utils.short_rel_path(_REAL_GMAIL_REL)
    assert attachment.read_bytes() == b"sqlite"
    assert len(attachment.name.encode()) <= utils.NAME_MAX_BYTES


def test_the_real_path_is_recoverable_from_the_reported_mapping() -> None:
    key = "pipelines/snap_123/filesystem"
    objs = [
        SimpleNamespace(key=f"{key}/ok.csv"),
        SimpleNamespace(key=f"{key}/{_REAL_GMAIL_REL}"),
    ]

    renamed = utils._renamed_paths(objs, key)

    assert renamed == {_REAL_GMAIL_REL: utils.short_rel_path(_REAL_GMAIL_REL)}


def test_two_paths_shortening_onto_one_are_refused() -> None:
    # Overwriting would still be counted as two objects added, so the loss
    # would be invisible. Snapshot keys are user content.
    key = "pipelines/snap_123/filesystem"
    collision = utils.short_rel_path(_REAL_GMAIL_REL)
    objs = [
        SimpleNamespace(key=f"{key}/{_REAL_GMAIL_REL}"),
        SimpleNamespace(key=f"{key}/{collision}"),
    ]

    with pytest.raises(HTTPException) as caught:
        utils._renamed_paths(objs, key)

    assert "resolve to one path" in str(caught.value.detail)


@pytest.mark.asyncio
async def test_the_s5cmd_fast_path_is_declined_when_a_name_must_be_shortened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `cp prefix/*` takes no per-object destination, so it cannot honour a
    # rename — it would try the real name and fail on it.
    root = tmp_path / "filesystem"
    root.mkdir()
    monkeypatch.setattr(utils, "FILESYSTEM_ROOT", str(root))

    key = "pipelines/snap_123/filesystem"
    objs = [
        SimpleNamespace(key=f"{key}/ok.csv", size=6),
        SimpleNamespace(key=f"{key}/{_REAL_GMAIL_REL}", size=6),
    ]
    s5cmd = _RecordingS5cmd()
    monkeypatch.setattr(utils, "get_s3_client", lambda **_kwargs: _S3Context(objs))
    monkeypatch.setattr(utils, "get_s5cmd_downloader", lambda _backend: s5cmd)
    monkeypatch.setattr(utils, "key_is_eligible", lambda _key: True)

    count = await utils.download_objects(
        bucket="snapshots",
        key=key,
        subsystem=str(root).lstrip("/"),
        backend="s5cmd",
    )

    assert s5cmd.calls == 0
    assert count == 2
    assert (root / "ok.csv").read_bytes() == b"sqlite"
    assert (root / utils.short_rel_path(_REAL_GMAIL_REL)).read_bytes() == b"sqlite"


@pytest.mark.asyncio
async def test_the_s5cmd_fast_path_still_runs_when_every_name_is_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "filesystem"
    root.mkdir()
    monkeypatch.setattr(utils, "FILESYSTEM_ROOT", str(root))

    key = "pipelines/snap_123/filesystem"
    objs = [SimpleNamespace(key=f"{key}/ok.csv", size=6)]
    s5cmd = _RecordingS5cmd()
    monkeypatch.setattr(utils, "get_s3_client", lambda **_kwargs: _S3Context(objs))
    monkeypatch.setattr(utils, "get_s5cmd_downloader", lambda _backend: s5cmd)
    monkeypatch.setattr(utils, "key_is_eligible", lambda _key: True)
    monkeypatch.setattr(
        utils, "_make_shared_filesystem_path_writable", lambda *_a: None
    )

    count = await utils.download_objects(
        bucket="snapshots",
        key=key,
        subsystem=str(root).lstrip("/"),
        backend="s5cmd",
    )

    assert s5cmd.calls == 1
    assert count == 1
