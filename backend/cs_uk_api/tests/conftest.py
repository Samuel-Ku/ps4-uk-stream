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
