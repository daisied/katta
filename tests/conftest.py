import sys
from pathlib import Path

# Ensure repository root is importable so `app` package imports work
# regardless of the runner's default PYTHONPATH behavior.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
