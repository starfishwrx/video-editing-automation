"""Validated, non-overwriting transport of readable Jianying draft packages.

Encrypted active documents are deliberately unsupported. An old draft_info.json
must never substitute for an unreadable current timeline. No key stores or
private encryption data are interpreted or exported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

VERSION = "4.0.0"
MANIFEST = "nas-package.json"
MEDIA_GROUPS = {"videos", "audios", "images", "gifs"}
EXTENSIONS = {".mov", ".mp4", ".mkv", ".avi", ".webm", ".ogg", ".mp3", ".m4a", ".wav", ".aac", ".flac", ".jpg", ".jpeg", ".png", ".webp", ".gif"}
SKIP_DIRS = {".backup", ".git", ".nas-link-backups", "__pycache__"}


class TransportError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def fail(code, detail):
    raise TransportError(code, detail)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path):
    try:
        raw = path.read_text(encoding="utf-8-sig")
        if not raw.lstrip().startswith(("{", "[")):
            fail("ENCRYPTED_DOCUMENT", f"不能验证加密文件：{path.name}；不会使用旧记录代替实际时间轴。")
        return json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        fail("UNREADABLE_DOCUMENT", f"无法读取 {path.name}: {error}")


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".nas-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def safe_relative(root: Path, name: str) -> Path:
    parts = PurePosixPath(name.replace("\\", "/"))
    if parts.is_absolute() or not parts.parts or any(p in {".", ".."} or ":" in p for p in parts.parts):
        fail("UNSAFE_PACKAGE_PATH", "包内含越界文件路径。")
    path = root.joinpath(*parts.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        fail("UNSAFE_PACKAGE_PATH", "包内文件指向包外。")
    return path


def project_files(root: Path):
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".nas-")]
        for name in files:
            if name.endswith((".bak", ".tmp")) or name in {".locked", MANIFEST, "nas-import-receipt.json", "crypto_key_store.dat"}:
                continue
            yield Path(parent) / name


def documents(root: Path):
    if not root.is_dir():
        fail("SOURCE_NOT_FOUND", "草稿目录不存在。")
    docs = {}
    for path in project_files(root):
        relative = path.relative_to(root)
        if path.suffix.lower() != ".json" or relative.parts[0] == "media":
            continue
        if path.stat().st_size == 0 and path.name not in {"draft_content.json", "draft_info.json", "draft_meta_info.json", "project.json"}:
            continue
        docs[relative.as_posix()] = read_json(path)
    roots = [docs.get(n) for n in ("draft_content.json", "draft_info.json") if n in docs]
    if not roots or not any(isinstance(d, dict) and isinstance(d.get("tracks"), list) and isinstance(d.get("materials"), dict) for d in roots):
        fail("NO_EDITABLE_DRAFT", "包里没有可验证的实际草稿。")
    if "draft_meta_info.json" not in docs:
        fail("DRAFT_METADATA_MISSING", "缺少草稿登记信息。")
    project = docs.get("Timelines/project.json")
    if project is not None:
        if not isinstance(project, dict):
            fail("INVALID_TIMELINE_INDEX", "时间轴索引格式错误。")
        active = project.get("main_timeline_id")
        if not isinstance(active, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", active):
            fail("INVALID_TIMELINE_INDEX", "无法定位实际主时间轴。")
        key = f"Timelines/{active}/draft_content.json"
        if key not in docs:
            fail("ACTIVE_TIMELINE_MISSING", "缺少实际主时间轴，不能只检查根目录旧草稿。")
        if not isinstance(docs[key], dict) or not isinstance(docs[key].get("tracks"), list):
            fail("ACTIVE_TIMELINE_INVALID", "实际时间轴无法验证。")
    return docs


def media_references(value):
    if isinstance(value, dict):
        materials = value.get("materials")
        if isinstance(materials, dict):
            for group in MEDIA_GROUPS:
                for material in materials.get(group, []) or []:
                    if not isinstance(material, dict):
                        fail("INVALID_MEDIA_RECORD", "素材记录格式错误。")
                    if not material.get("path"):
                        fail("MEDIA_PATH_EMPTY", "视频或音频没有源文件路径。")
                    for field in ("path", "media_path"):
                        path = material.get(field)
                        if path:
                            yield path
        for key, child in value.items():
            if isinstance(child, str) and (key.lower().endswith("path") or key.lower() in {"file_path", "file_uri"}):
                if PureWindowsPath(child).suffix.lower() in EXTENSIONS:
                    yield child
            elif isinstance(child, (dict, list)):
                yield from media_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from media_references(child)


def normalized(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").casefold()


def resolve_reference(root: Path, reference: str, reference_root: Path | None = None):
    if not isinstance(reference, str) or re.match(r"^[a-zA-Z]+://", reference):
        fail("REMOTE_MEDIA_UNCOLLECTED", "素材尚未收集为本地文件。")
    candidate = Path(reference)
    if reference_root and candidate.is_absolute() and candidate.is_relative_to(reference_root):
        candidate = root / candidate.relative_to(reference_root)
    elif not candidate.is_absolute():
        candidate = safe_relative(root, reference)
    if candidate.is_file():
        return candidate
    basename = PureWindowsPath(reference).name
    matches = [p for p in (root / "media").rglob(basename) if p.is_file()] if (root / "media").is_dir() else []
    if len(matches) != 1:
        fail("MEDIA_MISSING_OR_AMBIGUOUS", f"素材缺失或同名不明确：{basename}")
    return matches[0]


def inspect(root: Path, reference_root: Path | None = None, require_local=False):
    root = Path(root).resolve()
    docs = documents(root)
    refs = sorted({ref for document in docs.values() for ref in media_references(document)})
    if not refs:
        fail("NO_MEDIA_REFERENCES", "草稿没有可验证的画面或音频引用。")
    resolved = {}
    for ref in refs:
        if require_local:
            intended = Path(ref)
            boundary = reference_root or root
            if not intended.is_absolute() or not intended.is_relative_to(boundary):
                fail("NONLOCAL_MEDIA_REFERENCE", "本地草稿还依赖包外或 NAS 素材。")
        resolved[ref] = resolve_reference(root, ref, reference_root)
    return docs, resolved


def map_values(value, mapping):
    if isinstance(value, str):
        return mapping.get(normalized(value), value)
    if isinstance(value, list):
        return [map_values(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: map_values(v, mapping) for k, v in value.items()}
    return value


def inventory(root: Path):
    return {p.relative_to(root).as_posix(): {"bytes": p.stat().st_size, "sha256": digest(p)} for p in project_files(root)}


def verify_package(root: Path):
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            attributes = getattr(path.lstat(), 'st_file_attributes', 0)
            if path.is_symlink() or attributes & 0x400:
                fail('PACKAGE_LINK_REJECTED', '包内含目录映射或符号链接，不能保证素材已经完整复制。')
    manifest = read_json(root / MANIFEST)
    if manifest.get("schema_version") != 1 or manifest.get("transport_version") != VERSION or manifest.get("status") != "PORTABLE_PACKAGE_READY":
        fail("PACKAGE_NOT_CERTIFIED", "包版本或状态不能验证。")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        fail("PACKAGE_MANIFEST_EMPTY", "缺少包内文件清单。")
    for name, expected in files.items():
        path = safe_relative(root, name)
        if not path.is_file() or path.stat().st_size != expected.get("bytes") or digest(path) != expected.get("sha256"):
            fail("PACKAGE_HASH_MISMATCH", f"文件不完整或已变化：{name}")
    if set(inventory(root)) != set(files):
        fail("PACKAGE_FILE_LIST_CHANGED", "包内容在上传后发生变化，请重新打包。")
    docs, refs = inspect(root)
    for ref in refs:
        if ref.startswith(("/", "\\")) or PureWindowsPath(ref).is_absolute():
            fail("PACKAGE_PATH_NOT_PORTABLE", "包中仍然包含绝对素材路径。")
        if not safe_relative(root, ref).is_file():
            fail("PACKAGE_MEDIA_MISSING", "包中缺少引用的素材。")
    return manifest, docs, refs


def check_destination(source: Path, destination: Path):
    if destination.exists():
        fail("DESTINATION_EXISTS", "目标已存在；保留本地修改，请使用新的版本名。")
    if destination.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(destination.resolve()):
        fail("NESTED_DESTINATION", "源目录与目标目录不能互相包含。")


def cleanup_staging(parent: Path, expected_parent: Path):
    if parent.parent.resolve() != expected_parent.resolve() or not parent.name.startswith(('.nas-package-', 'import-')) or parent.is_symlink():
        fail('UNSAFE_STAGING_CLEANUP', '临时目录位置异常，保留现场，不执行清理。')
    shutil.rmtree(parent)


def package_draft(source: Path, destination: Path):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    check_destination(source, destination)
    snapshots = {p: digest(p) for p in project_files(source) if p.suffix.lower() == ".json"}
    docs, references = inspect(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix=".nas-package-", dir=destination.parent))
    stage = parent / "payload"
    stage.mkdir()
    try:
        mapping, assets = {}, {}
        for ref, original in references.items():
            hash_before = digest(original)
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", original.name)
            relative = "media/" + hash_before[:16] + "_" + name
            target = safe_relative(stage, relative)
            target.parent.mkdir(exist_ok=True)
            if not target.exists():
                shutil.copy2(original, target)
            if digest(target) != hash_before or digest(original) != hash_before:
                fail("SOURCE_CHANGED", "复制期间素材变化，未发布不完整包。")
            mapping[normalized(ref)] = relative
            assets[relative] = {"bytes": target.stat().st_size, "sha256": hash_before}
        for relative, document in docs.items():
            write_json(safe_relative(stage, relative), map_values(document, mapping))
        for path in project_files(source):
            relative = path.relative_to(source)
            if relative.parts[0] == "media" or path.suffix.lower() == ".json" or path.name.endswith((".ps1", ".bat", ".cmd", ".py", ".db")):
                continue
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        helpers = (Path(__file__).resolve().parent.parent / "nas-tools", Path(__file__).resolve().parent)
        helper_root = next((p for p in helpers if (p / "nas-tool.ps1").is_file()), None)
        if helper_root:
            for name in ("nas-tool.ps1", "fix_local.ps1", "素材本地化修复.bat", "一键取稿.bat", "上传草稿.bat"):
                shutil.copy2(helper_root / name, stage / name)
            shutil.copy2(Path(__file__), stage / "nas_portable.py")
        current_json = {p for p in project_files(source) if p.suffix.lower() == ".json"}
        if current_json != set(snapshots) or any(not p.exists() or digest(p) != old_hash for p, old_hash in snapshots.items()):
            fail("SOURCE_CHANGED", "复制期间草稿变化，请保存退出后重试。")
        inspect(stage)
        manifest = {"schema_version": 1, "transport_version": VERSION, "status": "PORTABLE_PACKAGE_READY", "draft_name": source.name, "files": inventory(stage), "media_files": len(assets), "media_references": len(references), "visual_verified": False}
        write_json(stage / MANIFEST, manifest)
        verify_package(stage)
        os.rename(stage, destination)
        verify_package(destination)
        return {"status": "PORTABLE_PACKAGE_READY", "package": str(destination), "media_files": len(assets), "media_references": len(references), "visual_verified": False}
    finally:
        cleanup_staging(parent, destination.parent)


def set_registration(docs, destination: Path):
    meta = docs["draft_meta_info.json"]
    primary = "draft_info.json" if "draft_info.json" in docs else "draft_content.json"
    meta.update({"draft_name": destination.name, "draft_fold_path": str(destination), "draft_root_path": str(destination.parent), "draft_json_file": str(destination / primary)})
    for name in ("draft_info.json", "draft_content.json"):
        if isinstance(docs.get(name), dict) and "name" in docs[name]:
            docs[name]["name"] = destination.name


def import_package(source: Path, destination: Path):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    check_destination(source, destination)
    _, docs, references = verify_package(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    hidden = destination.parent / ".nas-staging"
    hidden.mkdir(exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix="import-", dir=hidden))
    stage = parent / "payload"
    try:
        shutil.copytree(source, stage)
        verify_package(stage)
        mapping = {normalized(ref): str(destination / PurePosixPath(ref)) for ref in references}
        docs = {name: map_values(document, mapping) for name, document in docs.items()}
        set_registration(docs, destination)
        for name, document in docs.items():
            write_json(safe_relative(stage, name), document)
        inspect(stage, reference_root=destination, require_local=True)
        receipt = {"status": "LOCAL_MEDIA_VERIFIED", "transport_version": VERSION, "source_manifest_sha256": digest(source / MANIFEST), "media_references": len(references), "visual_verified": False}
        # The NAS manifest remains evidence, not a certificate for edited drafts.
        write_json(stage / "nas-import-receipt.json", receipt)
        os.rename(stage, destination)
        inspect(destination, require_local=True)
        return {**receipt, "draft": str(destination)}
    finally:
        cleanup_staging(parent, hidden)


def editor_running():
    if os.name != "nt":
        return False
    result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, timeout=10)
    return any(name in result.stdout.lower() for name in (b'"jianyingpro.exe"', b'"capcut.exe"', b'"videofusion.exe"'))


def localize_draft(root: Path):
    root = Path(root).resolve()
    if str(root).startswith("\\\\"):
        fail("SHARED_DRAFT_READ_ONLY", "请先取回本地，不在共享盘原稿上改路径。")
    if editor_running():
        fail("EDITOR_RUNNING", "请保存并退出剪映后执行；不会覆盖正在编辑的草稿。")
    docs, references = inspect(root)
    mapping = {}
    for ref in references:
        resolved = resolve_reference(root, ref)
        if not resolved.resolve().is_relative_to(root):
            fail("MEDIA_NOT_COLLECTED", "素材尚未完整收集到草稿文件夹，请使用安全打包与取稿。")
        mapping[normalized(ref)] = str(resolved.resolve())
    updated = {name: map_values(document, mapping) for name, document in docs.items()}
    set_registration(updated, root)
    backup = root / ".nas-link-backups" / time.strftime("%Y%m%d_%H%M%S")
    backup.mkdir(parents=True, exist_ok=False)
    for relative in docs:
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_relative(root, relative), target)
    try:
        for relative, document in updated.items():
            write_json(safe_relative(root, relative), document)
        inspect(root, require_local=True)
    except Exception:
        for relative in docs:
            shutil.copy2(backup / relative, safe_relative(root, relative))
        raise
    return {"status": "LOCAL_MEDIA_VERIFIED", "media_references": len(references), "backup": str(backup), "visual_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("export", "import"):
        command = sub.add_parser(name)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("--destination", required=True, type=Path)
    for name in ("inspect", "verify", "localize"):
        sub.add_parser(name).add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "export":
            result = package_draft(args.source, args.destination)
        elif args.action == "import":
            result = import_package(args.source, args.destination)
        elif args.action == "localize":
            result = localize_draft(args.source)
        elif args.action == "verify":
            manifest, _, _ = verify_package(args.source)
            result = {"status": "PACKAGE_FILES_VERIFIED", "files": len(manifest["files"]), "visual_verified": False}
        else:
            _, refs = inspect(args.source)
            result = {"status": "MEDIA_FILES_PRESENT", "media_references": len(refs), "visual_verified": False}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (TransportError, OSError, ValueError) as error:
        print(json.dumps({"status": "BLOCKED", "code": getattr(error, "code", "FILESYSTEM_ERROR"), "reason": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
