"""Media-only preflight; draft/export installation is a later boundary."""
from pathlib import Path
import os
import shutil
import subprocess


def production_environment(*, ffmpeg=None, ffprobe=None):
    tools, issues = {}, []
    for name, requested in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        value = requested or os.environ.get(name.upper() + "_BIN") or shutil.which(name)
        if not value:
            issues.append(name.upper() + "_MISSING")
            continue
        path = shutil.which(str(value)) or str(value)
        try:
            proc = subprocess.run([path, "-version"], capture_output=True, timeout=15)
            if proc.returncode:
                raise ValueError("version probe failed")
            tools[name] = str(Path(path).resolve())
        except (OSError, ValueError, subprocess.SubprocessError):
            issues.append(name.upper() + "_UNUSABLE")
    return {"ok": not issues, "tools": tools, "issues": issues, "scope": "production_media"}
