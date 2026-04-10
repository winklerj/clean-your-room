import pytest


@pytest.fixture
def tmp_build_room(tmp_path):
    """Provide an isolated build-your-room directory for tests."""
    return tmp_path / "build-your-room"
