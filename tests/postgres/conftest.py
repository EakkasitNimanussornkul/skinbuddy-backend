"""Fixtures for the SQL tests. The harness itself, and how to run these tests,
is described in tests/postgres/pg_harness.py."""

import os
from pathlib import Path

import pytest

from pg_harness import DB_NAME, MIGRATION_0013, SKIP_REASON, LocalPostgres, _find_bin_dir


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    if SKIP_REASON:
        pytest.skip(SKIP_REASON)
    configured = os.environ.get("SKINBUDDY_PG_DATA")
    data_dir = Path(configured) if configured else tmp_path_factory.mktemp("pg") / "data"
    port = int(os.environ.get("SKINBUDDY_PG_PORT", "54329"))
    server = LocalPostgres(_find_bin_dir(), data_dir, port)
    server.start()
    try:
        server.create_database(DB_NAME)
        # Twice: the second run proves the migration is safe to re-run.
        for _ in range(2):
            server.migration_runs.append(server.run_file(MIGRATION_0013))
        yield server
    finally:
        server.stop()
