from runner.evals.playground_snapshot_verifier.main import _units_requiring_judgement


def test_file_path_units_are_always_judged():
    # A non-DB file whose path matches a system-table pattern (e.g. ends with
    # "_log", or has a ".version" extension) must still be judged, not skipped
    # as system/infrastructure noise.
    units = {
        "filesystem/exports/release_log": ([], []),
        "config/settings.version": ([], []),
    }

    assert _units_requiring_judgement(units, file_path_units=set(units)) == [
        "filesystem/exports/release_log",
        "config/settings.version",
    ]


def test_db_system_tables_are_skipped():
    # Genuine DB system table names are still treated as noise (not judged).
    units = {"auth_user": ([], []), "alembic_version": ([], [])}

    assert _units_requiring_judgement(units, file_path_units=set()) == []


def test_db_business_table_is_judged():
    units = {"invoices": ([], [])}

    assert _units_requiring_judgement(units, file_path_units=set()) == ["invoices"]
