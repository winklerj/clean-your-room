from pathlib import Path
import os

BUILD_YOUR_ROOM_DIR = Path(
    os.getenv("BUILD_YOUR_ROOM_DIR", str(Path.home() / ".build-your-room"))
)
CLONES_DIR = BUILD_YOUR_ROOM_DIR / "clones"
PIPELINES_DIR = BUILD_YOUR_ROOM_DIR / "pipelines"

DATABASE_URL = os.getenv("DATABASE_URL", "postgres:///build_your_room")

DEFAULT_PORT = int(os.getenv("BUILD_YOUR_ROOM_PORT", "8317"))
DEFAULT_CLAUDE_MODEL = os.getenv("DEFAULT_CLAUDE_MODEL", "claude-sonnet-4-6")
DEFAULT_CODEX_MODEL = os.getenv("DEFAULT_CODEX_MODEL", "gpt-5.1-codex")
SPEC_CLAUDE_MODEL = os.getenv("SPEC_CLAUDE_MODEL", "claude-opus-4-6")
CONTEXT_THRESHOLD_PCT = int(os.getenv("CONTEXT_THRESHOLD_PCT", "60"))
MAX_CONCURRENT_PIPELINES = int(os.getenv("MAX_CONCURRENT_PIPELINES", "10"))
PIPELINE_LEASE_TTL_SEC = int(os.getenv("PIPELINE_LEASE_TTL_SEC", "30"))
PIPELINE_HEARTBEAT_INTERVAL_SEC = int(os.getenv("PIPELINE_HEARTBEAT_INTERVAL_SEC", "10"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
