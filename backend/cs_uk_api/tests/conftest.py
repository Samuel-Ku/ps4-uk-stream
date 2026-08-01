import sys
from pathlib import Path

# Make the package importable when running pytest from backend/
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
