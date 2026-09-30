"""Raw Harbor answer files must never enter the model's filesystem."""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runner.data.populate import utils
from runner.data.populate.models import PopulateSource


@pytest.mark.asyncio
@pytest.mark.parametrize("withhold", [False, True])
@pytest.mark.parametrize("harbor", [False, True])
@pytest.mark.parametrize("backend", ["boto3", "s5cmd"])
async def test_population_withholds_only_reserved_harbor_answers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    withhold: bool,
    harbor: bool,
    backend: str,
) -> None:
    prefix = "tasks/example/filesystem"
    files = {
        "instruction.md": b"Fix the bug",
        "solution/solution.patch": b"golden patch",
        "solution/solve.sh": b"apply golden patch",
        "tests/test.patch": b"hidden tests",
        "environment/repo/tests/test_public.py": b"public tests",
        "solution_notes.md": b"public notes",
        "tests_extra/test_public.py": b"public tests",
        "golden.patch": b"root golden patch",
        "golden_patch": b"root golden patch",
        "steps/0/solution/solve.sh": b"step reference",
        "steps/0/tests/test_outputs.py": b"step verifier",
        "steps/1/tests/test_outputs.py": b"step verifier",
        "steps/0/instruction.md": b"step prompt",
        "steps/0/environment/tests/test_public.py": b"public tests",
        "environment/golden.patch": b"repo file",
    }
    reserved = {
        "solution/solution.patch",
        "solution/solve.sh",
        "tests/test.patch",
        "golden.patch",
        "golden_patch",
        "steps/0/solution/solve.sh",
        "steps/0/tests/test_outputs.py",
        "steps/1/tests/test_outputs.py",
    }
    if harbor:
        files["task.toml"] = b"version = '1.0'"
    downloaded = []

    class Objects:
        async def filter(self, **kwargs):
            assert kwargs == {"Prefix": prefix}
            for name, content in files.items():
                yield SimpleNamespace(key=f"{prefix}/{name}", size=len(content))

    async def bucket(_name):
        return SimpleNamespace(objects=Objects())

    async def get_object(*, Key, **kwargs):
        name = Key.removeprefix(prefix + "/")
        downloaded.append(name)
        return {"Body": SimpleNamespace(read=AsyncMock(return_value=files[name]))}

    @asynccontextmanager
    async def client(**kwargs):
        yield SimpleNamespace(
            Bucket=bucket,
            meta=SimpleNamespace(client=SimpleNamespace(get_object=get_object)),
        )

    fast = AsyncMock()
    monkeypatch.setattr(utils, "get_s3_client", client)
    monkeypatch.setattr(
        utils, "get_s5cmd_downloader", lambda _: fast if backend == "s5cmd" else None
    )
    monkeypatch.setattr(utils, "key_is_eligible", lambda _: True)
    count = await utils.download_objects(
        "snapshots",
        prefix,
        str(tmp_path).lstrip("/"),
        backend=backend,
        withhold_harbor_answers=withhold,
    )
    protected = withhold and harbor
    expected = set(files) - reserved if protected else set(files)
    assert count == len(expected)
    if backend == "s5cmd" and not protected:
        fast.download_prefix.assert_awaited_once()
    else:
        fast.download_prefix.assert_not_awaited()
        assert set(downloaded) == expected
        assert {
            str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()
        } == expected
        for name in expected:
            assert (tmp_path / name).read_bytes() == files[name]
    assert files["solution/solution.patch"] == b"golden patch"


@pytest.mark.asyncio
async def test_population_forwards_answer_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download = AsyncMock(return_value=2)
    monkeypatch.setattr(utils, "download_objects", download)
    await utils.populate_data(
        [
            PopulateSource(
                url="s3://snapshots/tasks/test/filesystem/",
                subsystem="filesystem",
                withhold_harbor_answers=True,
            ),
            PopulateSource(
                url="s3://snapshots/tasks/test/.apps_data/", subsystem=".apps_data"
            ),
        ]
    )
    assert [
        call.kwargs["withhold_harbor_answers"] for call in download.await_args_list
    ] == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("mounted", [False, True])
async def test_world_tests_survive_harbor_task_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mounted: bool
) -> None:
    world = {"tests/public.py": b"world test", "solution/readme": b"world docs"}
    task = {
        "task.toml": b"version = '1.0'",
        "instruction.md": b"Fix the bug",
        "tests/public.py": b"hidden overwrite",
        "tests/test.patch": b"hidden tests",
        "solution/solution.patch": b"golden",
        "repo/main.py": b"public code",
    }
    files = {f"worlds/world/filesystem/{name}": body for name, body in world.items()}
    files.update({f"tasks/task/filesystem/{name}": body for name, body in task.items()})

    class Objects:
        async def filter(self, *, Prefix):
            for key, body in files.items():
                if key.startswith(Prefix):
                    yield SimpleNamespace(key=key, size=len(body))

    async def bucket(_name):
        return SimpleNamespace(objects=Objects())

    async def get_object(*, Key, **kwargs):
        return {"Body": SimpleNamespace(read=AsyncMock(return_value=files[Key]))}

    @asynccontextmanager
    async def client(**kwargs):
        yield SimpleNamespace(
            Bucket=bucket,
            meta=SimpleNamespace(client=SimpleNamespace(get_object=get_object)),
        )

    monkeypatch.setattr(utils, "get_s3_client", client)
    if mounted:
        for name, body in world.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
    else:
        await utils.download_objects(
            "snapshots",
            "worlds/world/filesystem/",
            "filesystem",
            dest_root=str(tmp_path),
            withhold_harbor_answers=True,
        )
    await utils.download_objects(
        "snapshots",
        "tasks/task/filesystem/",
        "filesystem",
        dest_root=str(tmp_path),
        withhold_harbor_answers=True,
    )
    assert {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == {
        **world,
        **{
            name: body
            for name, body in task.items()
            if name.split("/")[0] not in {"tests", "solution"}
        },
    }
