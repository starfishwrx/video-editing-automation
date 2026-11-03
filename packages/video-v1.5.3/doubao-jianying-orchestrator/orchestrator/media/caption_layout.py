"""Load the editor's pure caption geometry without loading its draft runtime."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[3] / "jianying-editor/scripts/utils/subtitle_layout.py"
_spec = importlib.util.spec_from_file_location("_shared_subtitle_geometry", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
layout_subtitle = _module.layout_subtitle
