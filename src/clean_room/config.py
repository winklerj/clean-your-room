from pathlib import Path
import os

CLEAN_ROOM_DIR = Path(os.getenv("CLEAN_ROOM_DIR", Path.home() / ".clean-room"))
REPOS_DIR = CLEAN_ROOM_DIR / "repos"
SPECS_MONOREPO_DIR = CLEAN_ROOM_DIR / "specs-monorepo"
DB_PATH = CLEAN_ROOM_DIR / "clean_room.db"
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-sonnet-4-20250514")
