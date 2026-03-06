import pytest
from pathlib import Path


@pytest.fixture
def tmp_clean_room(tmp_path):
    """Provide an isolated clean room directory for tests."""
    return tmp_path / "clean-room"
