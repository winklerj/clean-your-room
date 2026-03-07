import pytest


@pytest.fixture
def tmp_clean_room(tmp_path):
    """Provide an isolated clean room directory for tests."""
    return tmp_path / "clean-room"
