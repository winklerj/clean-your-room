from __future__ import annotations

import pytest
from pytest_postgresql import factories

# Session-scoped PostgreSQL process (starts once per test session)
postgresql_my_proc = factories.postgresql_proc(port=None, dbname="test_byr")

# Per-test connection (creates a fresh DB from template each test)
postgresql_my = factories.postgresql("postgresql_my_proc")


@pytest.fixture
def pg_dsn(postgresql_my):
    """Build an async-compatible DSN from the pytest-postgresql connection info."""
    info = postgresql_my.info
    return (
        f"host={info.host} port={info.port} "
        f"dbname={info.dbname} user={info.user}"
    )


@pytest.fixture
async def initialized_db(pg_dsn):
    """Initialize the schema on a fresh test database and yield the DSN.

    Tears down the pool after the test.
    """
    from build_your_room.db import init_db, close_pool

    await init_db(pg_dsn)
    yield pg_dsn
    await close_pool()


@pytest.fixture
def tmp_build_room(tmp_path):
    """Provide an isolated build-your-room directory for tests."""
    return tmp_path / "build-your-room"
