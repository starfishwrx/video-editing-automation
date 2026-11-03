"""Load the separately installed, source-pinned native backend without UI use."""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sys


def backend():
    raw = os.environ.get('JIANYING_HEADLESS_ROOT', '').strip()
    if not raw or not Path(raw).is_absolute():
        raise RuntimeError('HEADLESS_ROOT_REQUIRED')
    root = Path(raw).resolve(strict=True)
    project = json.loads((root / 'project.json').read_text(encoding='utf-8'))
    if project.get('id') != 'jianying-headless':
        raise RuntimeError('HEADLESS_PROJECT_IDENTITY_INVALID')
    entry = root / 'skills/yichen-jianying-edit/scripts/headless_draft.py'
    pins = None
    for node in ast.parse(entry.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PINS' for t in node.targets):
            pins = ast.literal_eval(node.value)
    if not isinstance(pins, dict) or 'production_library.py' not in pins:
        raise RuntimeError('HEADLESS_PRODUCTION_API_MISSING')
    for name, expected in pins.items():
        path = root / 'engine' / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('HEADLESS_SOURCE_CHANGED:' + name)
    for name, expected in pins.items():
        if '/' in name or not name.endswith('.py'):
            continue
        current = sys.modules.get(Path(name).stem)
        if current is not None:
            if (Path(current.__file__).resolve() != root / 'engine' / name
                    or getattr(current, '_headless_source_sha256', None) != expected):
                raise RuntimeError('HEADLESS_RUNTIME_ALREADY_LOADED_WITH_DIFFERENT_IDENTITY:' + name)
    if str(root / 'engine') not in sys.path:
        sys.path.insert(0, str(root / 'engine'))
    result = importlib.import_module('production_library')
    if getattr(result, 'SCHEMA', None) != 'headless-production-library/v1':
        raise RuntimeError('HEADLESS_PRODUCTION_API_UNSUPPORTED')
    for name, expected in pins.items():
        if '/' not in name and name.endswith('.py'):
            module = sys.modules.get(Path(name).stem)
            if module is not None:
                module._headless_source_sha256 = expected
    return result


def preflight(manifest=None):
    manifest = manifest or {}
    mode = str(manifest.get('draft_backend') or os.environ.get('JY_DRAFT_BACKEND') or 'headless')
    if mode == 'legacy':
        return {'status': 'READY', 'backend': 'legacy'}
    if mode != 'headless':
        raise ValueError('DRAFT_BACKEND_INVALID')
    native = backend()
    library = os.environ.get('JY_DRAFTS_ROOT') or os.environ.get('JY_PROJECTS_ROOT')
    if not library or not Path(library).is_absolute():
        raise ValueError('HEADLESS_LIBRARY_ROOT_REQUIRED')
    if Path(library).resolve() == native.j.nd.DRAFT_ROOT or Path(library).resolve().is_relative_to(native.j.nd.DRAFT_ROOT):
        raise ValueError('HEADLESS_LIBRARY_MUST_BE_OUTSIDE_HOME_INDEX')
    font = manifest.get('subtitle_font_path') or os.environ.get('JY_HEADLESS_FONT')
    if not font:
        raise ValueError('HEADLESS_SUBTITLE_FONT_REQUIRED')
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            raise ValueError('HEADLESS_EXECUTABLE_MISSING:' + tool)
    native.j.fonts.collect_plan({'tracks': [{'type': 'text', 'segments': [{'font_path': font}]}]})
    runtime = native.j.nd.doctor()
    return {'status': 'READY', 'backend': 'headless', 'runtime_profile': runtime['runtime_profile'],
            'install_dir': runtime.get('install_dir'), 'font': str(Path(font).resolve()),
            'backend_root': str(Path(native.__file__).resolve().parent.parent)}


def production_environment_report(base_report, manifest=None):
    """Combine cached toolchain facts with a fresh check of the selected writer."""
    report = {**base_report, 'profile': 'production',
              'base_ready': bool(base_report.get('ready')),
              'hints': list(base_report.get('hints') or [])}
    try:
        check = preflight(manifest)
    except (OSError, ValueError, RuntimeError, ImportError, SyntaxError) as exc:
        check = {'status': 'ENV_ERROR', 'reason': str(exc)}
    report['draft_backend_check'] = check
    report['ready'] = report['base_ready'] and check.get('status') == 'READY'
    if check.get('status') != 'READY':
        report['hints'].append(check.get('reason') or 'DRAFT_BACKEND_NOT_READY')
    return report
