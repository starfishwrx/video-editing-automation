"""Lazy access to the bundled draft contract from package-level tools."""
import importlib.util
import hashlib
import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / 'doubao-jianying-orchestrator/orchestrator/runtime/draft_documents.py'


def contract():
    spec = importlib.util.spec_from_file_location('_tool_draft_documents', PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_native(root):
    return (Path(root) / 'headless-production.json').is_file()


def documents(root, **kwargs):
    return contract().documents(root, **kwargs)


def timeline(root):
    return contract().timeline(root)


def draft_id(root):
    return contract().draft_id(root)


def bgm_title(root):
    identity = Path(root) / 'audio_identity.json'
    if not is_native(root) or not identity.is_file():
        return None
    receipt = json.loads(identity.read_text(encoding='utf-8'))
    info = timeline(root)
    tracks = [t for t in info['tracks'] if t.get('name') == 'BGM']
    if len(tracks) != 1 or len(tracks[0]['segments']) != 1:
        raise ValueError('HEADLESS_BGM_TRACK_MISSING')
    segment = tracks[0]['segments'][0]
    materials = {m['id']: m for m in info['materials']['audios']}
    audio = Path(materials[segment['material_id']]['path'])
    if (hashlib.sha256(audio.read_bytes()).hexdigest() != receipt['bgm_asset_sha256']
            or segment['target_timerange']['duration'] != receipt['event_duration_us']):
        raise ValueError('HEADLESS_BGM_IDENTITY_CHANGED')
    return str((receipt.get('bgm_choice') or {}).get('title') or '').strip() or None
