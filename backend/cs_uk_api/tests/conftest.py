import os
import sys
from pathlib import Path

# Make the package importable when running pytest from backend/
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Disable the startup catalog warm (#204/#210) in tests: it would run
# real provider scrapes inside any TestClient lifespan, slowing the
# suite and hitting upstream sites.
os.environ.setdefault("CS_UK_CATALOG_WARM", "0")

# Keep the resume store memory-only in tests (ticket #248): the default
# suite must not write/read a real state file under the operator's home
# dir. Persistence is exercised by dedicated ResumeStore tests that
# construct stores over tmp paths. An explicit empty string disables the
# disk layer (see config._load_resume_path).
os.environ.setdefault("CS_UK_RESUME_PATH", "")

# Same for the user-state store (favorites/played, ticket #258): tests
# keep it memory-only; persistence is exercised by dedicated
# UserStateStore tests over tmp paths.
os.environ.setdefault("CS_UK_USER_STATE_PATH", "")
