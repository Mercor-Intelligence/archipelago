import tarfile
from importlib import import_module
from io import BytesIO
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from runner.data.snapshot import main as snapshot_main

data_router = import_module("runner.data.router")


def test_workspace_snapshot_leaves_world_actions_running(monkeypatch, tmp_path):
    visible = tmp_path / "report.csv"
    visible.write_bytes(b"result\n")
    answer = tmp_path / "answer.txt"
    answer.write_bytes(b"reserved")
    link = tmp_path / "link"
    link.symlink_to(answer)
    drains = []

    async def finish_actions():
        drains.append(True)

    def files(root, prefix):
        assert root == f"/{prefix}"
        yield visible, f"{prefix}/nested/report.csv"
        yield answer, f"{prefix}/solution/answer.txt"
        yield link, f"{prefix}/link"

    monkeypatch.setattr(data_router, "iter_paths", files)
    monkeypatch.setattr(
        snapshot_main,
        "get_coordinator",
        lambda: SimpleNamespace(finish_actions=finish_actions),
    )

    app = FastAPI()
    app.include_router(data_router.router, prefix="/data")
    with TestClient(app) as client:
        response = client.post("/data/snapshot/workspace")
        assert response.status_code == 200
        assert drains == []
        with tarfile.open(fileobj=BytesIO(response.content), mode="r:gz") as archive:
            assert len(archive.getmembers()) == 1
            (member,) = archive.getmembers()
            assert member.name.endswith("/nested/report.csv")
            assert archive.extractfile(member).read() == b"result\n"
        assert client.post("/data/snapshot").status_code == 200
        assert drains == [True]
