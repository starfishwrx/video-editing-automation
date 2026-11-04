"""Load the separately installed, source-pinned native backend without UI use."""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
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


def _prepare_media_tools(media=None):
    """Use the same discovery as the engine and make it visible to the backend."""
    from . import platform_env

    for key in ('FFMPEG_BIN', 'FFPROBE_BIN'):
        configured = os.environ.get(key, '').strip()
        if configured and not Path(configured).expanduser().is_file():
            raise ValueError('HEADLESS_EXECUTABLE_CONFIG_INVALID:' + key)
    found = media if media and media.get('ffmpeg') and media.get('ffprobe') else platform_env.find_ffmpeg_detailed()
    paths = {}
    for tool in ('ffmpeg', 'ffprobe'):
        path = found.get(tool)
        if not path or not Path(path).is_file():
            raise ValueError('HEADLESS_EXECUTABLE_MISSING:' + tool)
        path = Path(path).resolve()
        try:
            probe = subprocess.run([str(path), '-version'], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('HEADLESS_EXECUTABLE_UNUSABLE:' + tool) from exc
        if probe.returncode:
            raise ValueError('HEADLESS_EXECUTABLE_UNUSABLE:' + tool)
        paths[tool] = path

    # Native backend invokes the command names; bind both to the binaries the
    # engine actually selected. Refuse conflicting directories or renamed tools
    # instead of claiming readiness for a different executable.
    current = os.environ.get('PATH', '')
    parents = list(dict.fromkeys(str(paths[name].parent) for name in ('ffmpeg', 'ffprobe')))
    for order in (parents, list(reversed(parents))):
        parts, seen = [], set()
        for part in [*order, *current.split(os.pathsep)]:
            key = os.path.normcase(os.path.normpath(part))
            if key not in seen:
                seen.add(key)
                parts.append(part)
        candidate = os.pathsep.join(parts)
        if all((resolved := shutil.which(name, path=candidate)) and
               Path(resolved).resolve() == paths[name] for name in paths):
            os.environ['PATH'] = candidate
            os.environ['FFMPEG_BIN'] = str(paths['ffmpeg'])
            os.environ['FFPROBE_BIN'] = str(paths['ffprobe'])
            return {name: str(path) for name, path in paths.items()}
    raise ValueError('HEADLESS_EXECUTABLE_PATH_CONFLICT: ffmpeg/ffprobe must be discoverable by name')


def preflight(manifest=None, *, media=None):
    manifest = manifest or {}
    mode = str(manifest.get('draft_backend') or os.environ.get('JY_DRAFT_BACKEND') or 'headless')
    if mode == 'legacy':
        return {'status': 'READY', 'backend': 'legacy', 'media_tools': _prepare_media_tools(media)}
    if mode != 'headless':
        raise ValueError('DRAFT_BACKEND_INVALID')
    native = backend()
    if getattr(native, 'TEXT_LAYOUT_SCHEMA', None) != 'centered-safe-subtitles/v1':
        raise RuntimeError('HEADLESS_TEXT_LAYOUT_API_MISSING: update the headless backend before production')
    library = os.environ.get('JY_DRAFTS_ROOT') or os.environ.get('JY_PROJECTS_ROOT')
    if not library or not Path(library).is_absolute():
        raise ValueError('HEADLESS_LIBRARY_ROOT_REQUIRED')
    if Path(library).resolve() == native.j.nd.DRAFT_ROOT or Path(library).resolve().is_relative_to(native.j.nd.DRAFT_ROOT):
        raise ValueError('HEADLESS_LIBRARY_MUST_BE_OUTSIDE_HOME_INDEX')
    font = manifest.get('subtitle_font_path') or os.environ.get('JY_HEADLESS_FONT')
    if not font:
        raise ValueError('HEADLESS_SUBTITLE_FONT_REQUIRED')
    media_tools = _prepare_media_tools(media)
    native.j.fonts.collect_plan({'tracks': [{'type': 'text', 'segments': [{'font_path': font}]}]})
    try:
        runtime = native.j.nd.doctor()
    except ValueError as exc:
        if str(exc) != '剪映默认本地草稿目录尚未初始化':
            raise
        raise ValueError(
            'HEADLESS_HOST_NOT_INITIALIZED: 剪映默认本地草稿目录尚未初始化；'
            '请使用运行流水线的同一 Windows 用户启动剪映并完成首次初始化，'
            '随后重新运行生产 env 检查。不要手工创建或覆盖 root_meta_info.json。'
        ) from exc
    return {'status': 'READY', 'backend': 'headless', 'runtime_profile': runtime['runtime_profile'],
            'install_dir': runtime.get('install_dir'), 'font': str(Path(font).resolve()),
            'backend_root': str(Path(native.__file__).resolve().parent.parent),
            'media_tools': media_tools}


def production_environment_report(base_report, manifest=None):
    """Combine cached toolchain facts with a fresh check of the selected writer."""
    report = {**base_report, 'profile': 'production',
              'base_ready': bool(base_report.get('ready')),
              'hints': list(base_report.get('hints') or [])}
    try:
        check = preflight(manifest, media=base_report)
    except (OSError, ValueError, RuntimeError, ImportError, SyntaxError) as exc:
        check = {'status': 'ENV_ERROR', 'reason': str(exc)}
    report['draft_backend_check'] = check
    report['ready'] = report['base_ready'] and check.get('status') == 'READY'
    if check.get('status') != 'READY':
        report['hints'].append(check.get('reason') or 'DRAFT_BACKEND_NOT_READY')
    return report
