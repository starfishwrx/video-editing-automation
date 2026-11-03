"""Native path relocation for formal delivery and authorized version recovery.

This is transport only. The normal finalizer must pass its existing four gates
before calling ship_one. No QC result is created or promoted here.
"""
import os
from pathlib import Path
import uuid

from tools.delivery_identity import tree_identity
from tools import native_drafts


def verify_shared(root, reference_root):
    payload = native_drafts.documents(root)
    actual = Path(payload['reference_root']).resolve()
    if actual != Path(reference_root).resolve():
        raise RuntimeError('HEADLESS_SHARED_REFERENCE_ROOT_CHANGED')
    return {'status': 'SHARED_MEDIA_VERIFIED', 'reference_root': str(actual),
            'literal_references': len(payload['marker']['record']['assets']) + len(payload['marker']['record'].get('font_assets', [])),
            'desktop_playback_verified': False, 'visual_verified': False}


def archive_shared_copy(source, target):
    from tools.draft_delivery import publish_tree
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.exists():
        raise RuntimeError('DESTINATION_EXISTS')
    before = tree_identity(source)
    work = target.parent / ('.native-archive-' + uuid.uuid4().hex)
    native_drafts.contract().native_backend().relocate(source, work, target)
    expected = tree_identity(work)
    publish_tree(work, target, expected)
    check = verify_shared(target, target)
    if tree_identity(source) != before:
        raise RuntimeError('SOURCE_CHANGED_DURING_DELIVERY')
    return {'source_tree_sha256': before['sha256'], 'tree_identity': tree_identity(target), 'shared_check': check}


def ship(source, staging, *, share=None):
    from tools.draft_delivery import publish_tree
    source, staging = Path(source).resolve(), Path(staging).resolve()
    raw = str(share or os.environ.get('JY_NAS_SHARE') or '').strip()
    if not raw:
        raise RuntimeError('JY_NAS_SHARE_MISSING')
    share = Path(raw).resolve()
    if os.name == 'nt' and not str(share).startswith('\\\\'):
        raise RuntimeError('HEADLESS_DELIVERY_UNC_REQUIRED')
    if not share.is_dir():
        raise RuntimeError('NAS_SHARE_UNAVAILABLE')
    target = share / '测试产出' / source.name
    distribution = target.parent / '分发草稿' / source.name
    before = tree_identity(source)
    backend = native_drafts.contract().native_backend()
    existing = next((p for p in (target, distribution) if p.exists()), None)
    if existing is not None:
        backend.equivalent(source, existing, target)
        stage_source, expected = existing, tree_identity(existing)
    else:
        backend.relocate(source, staging, target)
        stage_source, expected = staging, tree_identity(staging)
    # Refuse either conflicting destination before publishing either tree.
    for destination in (target, distribution):
        if destination.exists() and tree_identity(destination) != expected:
            raise RuntimeError('DELIVERY_TARGET_IDENTITY_CONFLICT')
    if stage_source != target:
        publish_tree(stage_source, target, expected)
    verify_shared(target, target)
    if stage_source != distribution:
        publish_tree(stage_source, distribution, expected, reference_root=target)
    verify_shared(distribution, target)
    if tree_identity(source) != before:
        raise RuntimeError('SOURCE_CHANGED_DURING_DELIVERY')
    return {'status': 'OK', 'dist_target': str(distribution), 'nas_target': str(target),
            'staged_tree_sha256': expected['sha256'], 'staged_file_count': expected['file_count'],
            'nas_root_verified': True, 'distribution_verified': True,
            'shared_media_references_verified': True, 'shared_reference_root': str(target), 'draft_backend': 'headless'}
