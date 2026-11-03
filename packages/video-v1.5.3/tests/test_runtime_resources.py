"""Exercise a clean checkout/release with no installed skill or private data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Executed in a separate interpreter so imported modules and lookup caches from
# the developer machine cannot satisfy this test. Network requests are forbidden.
COLD_LOOKUP = r'''
import contextlib, io, json, os, pathlib, runpy, socket, subprocess, sys, types
from unittest.mock import patch
root = pathlib.Path.cwd().resolve()
sys.path.insert(0, str(root / "doubao-jianying-orchestrator"))
def forbidden(*args, **kwargs):
    raise AssertionError("cold-start lookup must be offline and package-local")
socket.socket.connect = forbidden
from orchestrator.runtime import platform_env
platform_env.find_jianying_editor_root = forbidden
from orchestrator.audio import library_resources, voice_catalog, bgm_selector
assert library_resources.CATALOGS_ROOT.is_relative_to(root), (library_resources.CATALOGS_ROOT, root)
report = library_resources.catalog_report()
assert report["ready"], report
ordinary = voice_catalog.resolve_voice("TVB女声1.2")
professional = voice_catalog.resolve_voice("TVB女声Pro")
assert (ordinary.sami, ordinary.speed) == ("BV409_streaming", 1.2)
assert professional.sami == "zh_female_tvb_emo_v2_mars_bigtts"
for name in library_resources.CATALOG_FIELDS:
    assert all("://" not in value for row in library_resources.read_catalog(name) for value in row.values())
tracks = bgm_selector.select_bgm(script="宝宝柔软舒适，日常带货推荐", topk=3)
assert tracks, "automatic BGM returned an empty candidate pool"
sys.path.insert(0, str(root / "jianying-editor" / "scripts"))
import requests
requests.sessions.Session.request = forbidden
from cloud_manager import CloudManager
manager = CloudManager()
for track in tracks:
    asset = manager.find_asset(track["music_id"])
    assert asset and asset["id"] == track["music_id"], track
    assert not asset["url"], "signed URLs must not be shipped"
# Windows editors may add a BOM. Preflight and the real downloader must agree.
music = library_resources.CATALOGS_ROOT / "cloud_music_library.csv"
music.write_text(music.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
assert library_resources.catalog_report()["ready"]
assert CloudManager().find_asset(tracks[0]["music_id"])

# Exercise download_track -> packaged CLI -> real CloudManager, replacing only
# process transport and network responses. A poisoned host import must be ignored.
host = root.parent / "old-host" / "scripts"
host.mkdir(parents=True)
(host / "cloud_manager.py").write_text("raise AssertionError('old host loaded')", encoding="utf-8")
os.environ["JY_SKILL_ROOT"] = str(host.parent)
sys.path.insert(0, str(host))
sys.modules["cloud_manager"] = types.SimpleNamespace(CloudManager=forbidden)
calls = []
class Response:
    headers = {"Content-Type": "audio/mp4", "Content-Length": "2048"}
    def raise_for_status(self): pass
    def json(self): return {"url": "https://example.com/fixture.m4a"}
    def iter_content(self, chunk_size): yield b"x" * 2048
    def close(self): pass
def post(url, **kwargs):
    assert kwargs["json"]["items"][0]["id"] == tracks[0]["music_id"]
    calls.append("resolve")
    return Response()
def get(url, **kwargs):
    assert url == "https://example.com/fixture.m4a"
    calls.append("download")
    return Response()
requests.post, requests.get = post, get
def local_cli(command, **kwargs):
    assert pathlib.Path(command[3]).resolve() == root / "jianying-editor/scripts/cloud_manager.py"
    original_argv = sys.argv
    try:
        sys.argv = command[3:]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            runpy.run_path(command[3], run_name="__main__")
        return subprocess.CompletedProcess(command, 0, output.getvalue(), "")
    finally:
        sys.argv = original_argv
bgm_selector.subprocess.run = local_cli
downloaded = bgm_selector.download_track(tracks[0], root.parent / "task-cache")
assert downloaded and downloaded.stat().st_size == 2048, "bundled download route failed"
assert calls == ["resolve", "download"], calls
calls.clear()
requests.post = requests.get = forbidden
assert bgm_selector.download_track(tracks[0], root.parent / "task-cache") == downloaded
# Content-type detection may have saved a different suffix from the static index.
alternate = downloaded.with_suffix(".mp3")
downloaded.rename(alternate)
assert bgm_selector.download_track(tracks[0], root.parent / "task-cache") == alternate
assert not calls
with patch.object(CloudManager, "get_url_from_logs", return_value=None), \
        patch.object(CloudManager, "_resolve_url_by_id", return_value=None) as resolve:
    assert CloudManager().download_asset(tracks[0]["music_id"], force=True) is None
    resolve.assert_called_once()
print(json.dumps({"status": "COLD_LOOKUP_READY", "voice": ordinary.sami,
                  "tempo": ordinary.speed, "pro_voice": professional.sami,
                  "catalog_entries": {n: v["entries"] for n, v in report["catalogs"].items()},
                  "bgm_candidates": len(tracks), "cloud_ids_resolved": True,
                  "bom_index_verified": True, "bundled_download_route_verified": True,
                  "offline_cache_reused": True, "force_refresh_verified": True}))
'''


def source_files():
    git = shutil.which("git")
    if git:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        result = subprocess.run([git, "rev-parse", "--show-toplevel"], cwd=ROOT,
                                env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)
        if result.returncode == 0:
            repository = Path(result.stdout.strip()).resolve()
            if ROOT.is_relative_to(repository):
                prefix = ROOT.relative_to(repository)
                tracked = subprocess.run([git, "ls-files", "-z", "--", prefix.as_posix()], cwd=repository,
                                         env=env, capture_output=True, check=True, timeout=15)
                return "git-tracked-only", [Path(p).relative_to(prefix).as_posix()
                    for p in tracked.stdout.decode("utf-8").split("\0") if p]
    # An extracted source release has no .git; use its explicit delivery inventory.
    manifest = json.loads((ROOT / "release-manifest.json").read_text(encoding="utf-8"))
    return "release-inventory-only", [row["path"] for row in manifest["files"]] + [
        "build_manifest.py", "release-manifest.json"]


class ColdRuntimeResourcesTests(unittest.TestCase):
    def test_git_or_release_inventory_runs_without_host_catalogs_or_signed_urls(self):
        mode, files = source_files()
        required = {"jianying-editor/resources/catalogs/" + name for name in (
            "jianying_voice_catalog.csv", "tts_speakers.csv", "cloud_music_library.csv")}
        self.assertTrue(required.issubset(files), f"{mode} is missing {required - set(files)}")
        with tempfile.TemporaryDirectory(prefix="video-cold-") as raw:
            root = Path(raw) / "package"
            for relative in files:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            profile = Path(raw) / "empty-profile"
            profile.mkdir()
            allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT"}
            env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
            env.update({k: str(profile) for k in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA")})
            env.update(PYTHONUTF8="1", JY_CLOUD_CACHE_DIR=str(profile / "cloud-cache"))
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", COLD_LOOKUP], cwd=root,
                                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=40)
        self.assertEqual(result.returncode, 0, mode + "\n" + result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "COLD_LOOKUP_READY")
        self.assertTrue(report["cloud_ids_resolved"])
