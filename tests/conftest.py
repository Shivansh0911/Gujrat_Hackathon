import sys
from pathlib import Path

# Tests import `services.*` from the repo root without requiring an editable install,
# so `pytest` works identically on a dev machine and in CI.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
