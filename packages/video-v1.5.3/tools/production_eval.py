"""Compatibility entrypoint; maintained implementation: tools.acceptance.evaluation."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.acceptance import evaluation as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
elif __name__ in sys.modules:
    sys.modules[__name__] = _implementation
else:
    __file__ = _implementation.__file__
    __package__ = "tools.acceptance"
    __spec__ = None
    exec(compile(Path(__file__).read_text(encoding="utf-8"), __file__, "exec"), globals())
