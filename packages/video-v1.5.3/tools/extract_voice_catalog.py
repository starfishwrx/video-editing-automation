"""Write a new dated voice catalog from Jianying's local resource cache."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "doubao-jianying-orchestrator"))
from orchestrator.voice_cache import find_latest_panel, main, parse_panel, write_catalog  # noqa: E402,F401


if __name__ == "__main__":
    raise SystemExit(main())
