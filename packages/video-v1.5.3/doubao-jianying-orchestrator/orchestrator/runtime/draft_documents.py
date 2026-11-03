"""Read the active draft format; never fall back from native to stale plaintext."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path

MARKER = 'headless-production.json'


def native_backend():
    # Also loaded by the acceptance contract as a standalone file.
    spec = importlib.util.spec_from_file_location('_video_headless_runtime', Path(__file__).with_name('headless.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.backend()


def is_native(root):
    return (Path(root) / MARKER).is_file()


def documents(root, *, require_literal=True):
    root = Path(root)
    if is_native(root):
        return native_backend().inspect(root, require_literal=require_literal)
    # Unknown migrated drafts remain unsupported; a plaintext root sidecar
    # cannot establish the content of an encrypted active timeline.
    active = root / 'Timelines' / 'project.json'
    if active.is_file():
        raise ValueError('UNOWNED_NATIVE_DRAFT_UNSUPPORTED')
    for name in ('draft_info.json', 'draft_content.json'):
        path = root / name
        if path.is_file():
            timeline = json.loads(path.read_text(encoding='utf-8-sig'))
            meta = root / 'draft_meta_info.json'
            return {'timeline': timeline, 'metadata': json.loads(meta.read_text(encoding='utf-8-sig')) if meta.is_file() else {},
                    'reference_root': str(root)}
    raise FileNotFoundError('DRAFT_TIMELINE_MISSING')


def timeline(root):
    return documents(root)['timeline']


def draft_id(root):
    result = documents(root)
    return result['metadata'].get('draft_id') if is_native(root) else result['timeline'].get('id')
