"""Trajectory snapshots include the apps_data subsystem (VCA coordinator state)."""

import inspect

from runner.data.snapshot import main as snapshot_main
from runner.utils.settings import get_settings


def test_file_snapshot_uploads_apps_data_subsystem() -> None:
    src = inspect.getsource(snapshot_main.handle_snapshot_s3_files)
    assert "APPS_DATA_SUBSYSTEM_NAME" in src or ".apps_data" in src


def test_settings_apps_data_names_coordinator_parent() -> None:
    assert get_settings().APPS_DATA_SUBSYSTEM_NAME == ".apps_data"
