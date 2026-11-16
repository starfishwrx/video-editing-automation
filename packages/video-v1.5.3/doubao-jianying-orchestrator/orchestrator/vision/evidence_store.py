"""Local, content-addressed index of observations. Media stays on the filesystem."""
from __future__ import annotations

from contextvars import ContextVar
import contextlib
import functools
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import time
import logging

from .. import ai_editor_contract as contract


request_kind = ContextVar("evidence_request_kind", default="visual_analysis")


def optional(default=None):
    """Keep an unavailable accelerator out of the editor and QC success contracts."""
    def decorator(operation):
        @functools.wraps(operation)
        def wrapped(*args, **kwargs):
            try:
                return operation(*args, **kwargs)
            except (sqlite3.Error, OSError) as exc:
                logging.getLogger(__name__).warning("Evidence %s unavailable: %s", operation.__name__, exc)
                return default
        return wrapped
    return decorator


def database():
    value = os.environ.get("JY_MATERIAL_EVIDENCE_DB", "").strip()
    return Path(value) if value and value.casefold() != "off" else None


def scope(manifest):
    fields = [str(manifest.get(k) or "").strip().casefold() for k in
              ("category", "product_id", "product", "sku", "brand", "product_key")]
    return contract.digest(fields) if any(fields[i] for i in (1, 2, 3, 5)) else None


def protocol(manifest):
    from . import vision_analyzer as vision
    route = vision._vision_transport()
    cfg = vision._qwen_settings() if route == "qwen" else vision._volc_settings()
    editor = vision._text_settings() or cfg if route != "qwen" else cfg
    return contract.digest(["material-evidence-v1", route, editor["model"], editor["base_url"],
                            contract.POLICY_VERSION, contract.policy_prompt({**manifest, "revision_feedback":""})])


def scene_protocol(manifest, scene_ids):
    """Bind a factual cut observation to the visual requirements for its named scenes."""
    if not scene_ids:
        return None
    from . import vision_analyzer as vision
    context = contract.editorial_context(manifest)
    rules = {item["id"]:item for item in context["rules"].get("semantic_contracts", [])}
    if not set(scene_ids) <= rules.keys():
        return None
    route = vision._vision_transport()
    cfg = vision._qwen_settings() if route == "qwen" else vision._text_settings() or vision._volc_settings()
    return contract.digest(["qc-visible-scene-v1", context["category"], context["versions"].get("global"),
                            contract.POLICY_VERSION, route, cfg["model"], cfg["base_url"],
                            [rules[s] for s in sorted(scene_ids)]])


def _version(stream):
    stat = os.fstat(stream.fileno())
    changed = stat.st_ctime_ns
    if os.name == "nt":
        import ctypes
        import msvcrt
        # Python 3.11 st_ctime is creation time on Windows. SMB and NTFS
        # expose the actual change time in FILE_BASIC_INFO.ChangeTime.
        basic = (ctypes.c_int64 * 5)()
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
        if not ctypes.windll.kernel32.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic),
                                                                   ctypes.sizeof(basic)) or not basic[3]:
            return None
        changed = basic[3]
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, changed


def identity(path):
    with open(path, "rb") as stream:
        version = _version(stream)
        if version is None or not version[1]:
            return hashlib.file_digest(stream, "sha256").hexdigest()
        return _hash_version(str(Path(path).resolve()), version)


@functools.lru_cache(maxsize=8192)
def _hash_version(path, version):
    with open(path, "rb") as stream:
        if _version(stream) != version:
            raise ValueError("MATERIAL_CHANGED_DURING_EVIDENCE_HASH")
        # A new Windows process can reuse a digest only for the same volume,
        # file ID and change-time. Recheck the open handle before returning it.
        db_path = database()
        key = json.dumps([path, *version], ensure_ascii=False)
        if db_path:
            try:
                with contextlib.closing(sqlite3.connect(db_path.with_suffix(".hashes.sqlite3"), timeout=10)) as db, db:
                    db.execute("CREATE TABLE IF NOT EXISTS fingerprints(version TEXT PRIMARY KEY, sha TEXT NOT NULL)")
                    hit = db.execute("SELECT sha FROM fingerprints WHERE version=?", (key,)).fetchone()
                    if hit and _version(stream) == version:
                        return hit[0]
            except sqlite3.Error:
                pass
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
        if _version(stream) != version:
            raise ValueError("MATERIAL_CHANGED_DURING_EVIDENCE_HASH")
        if db_path:
            try:
                with contextlib.closing(sqlite3.connect(db_path.with_suffix(".hashes.sqlite3"), timeout=10)) as db, db:
                    db.execute("INSERT OR IGNORE INTO fingerprints VALUES(?,?)", (key,sha))
            except sqlite3.Error:
                pass
    return sha


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    sha TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(sha,path));
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY, product TEXT NOT NULL, sha TEXT NOT NULL,
    start_us INTEGER NOT NULL, end_us INTEGER NOT NULL CHECK(end_us>start_us),
    grade TEXT NOT NULL, protocol TEXT NOT NULL, payload TEXT NOT NULL,
    origin TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS source_window ON observations(product,sha,protocol,start_us,end_us);
CREATE TABLE IF NOT EXISTS usage (
    event_id INTEGER PRIMARY KEY, recorded REAL NOT NULL, run TEXT NOT NULL,
    stage TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS usage_run ON usage(run,stage);
CREATE TABLE IF NOT EXISTS preparations (
    sha TEXT NOT NULL, protocol TEXT NOT NULL, record_path TEXT NOT NULL,
    PRIMARY KEY(sha,protocol));
"""


@functools.lru_cache(maxsize=16)
def _init(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    from ..runtime.file_safety import file_lock
    with file_lock(path.with_suffix(".schema"), timeout=60), contextlib.closing(sqlite3.connect(path, timeout=60)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)


@contextlib.contextmanager
def connect():
    path = database()
    if path is None:
        raise ValueError("MATERIAL_EVIDENCE_DB_UNCONFIGURED")
    _init(path)
    with contextlib.closing(sqlite3.connect(path, timeout=60)) as db, db:
        db.row_factory = sqlite3.Row
        yield db


@optional()
def record(manifest, stage, detail):
    if database():
        with connect() as db:
            run = str(os.environ.get("JY_EVIDENCE_BATCH_ID") or manifest.get("parent_run_id") or manifest.get("record_id") or
                      manifest.get("task_id") or manifest.get("draft_name") or "")
            db.execute("INSERT INTO usage(recorded,run,stage,detail) VALUES(?,?,?,?)",
                       (time.time(), run, stage, json.dumps(detail, ensure_ascii=False)))


def put(db, product, sha, path, start, end, grade, version, facts, origin):
    if not product or end <= start or start < 0 or not facts:
        return
    db.execute("INSERT OR IGNORE INTO sources VALUES(?,?)", (sha, str(path)))
    key = contract.digest([product, sha, start, end, grade, version, facts])
    db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?)",
               (key, product, sha, start, end, grade, version,
                json.dumps(facts, ensure_ascii=False, allow_nan=False), str(origin)))


@optional()
def index_analysis(manifest, analysis, origin):
    product = scope(manifest)
    if not database() or not product:
        return
    version = protocol(manifest)
    from . import vision_analyzer as vision
    for shot in analysis.get("shots", []):
        path = shot.get("video")
        if not path or not shot.get("analysis_cache_key") or not shot.get("analysis_source"):
            continue
        sha = identity(path)
        if sha != shot.get("source_sha256"):
            continue
        bounds = [round(shot[k] * 1e6) for k in ("source_start", "source_end")]
        with connect() as db:
            if vision._vision_cache_key(manifest, shot) == shot["analysis_cache_key"]:
                projection = vision._cache_projection(shot)
                if projection:
                    put(db, product, sha, path, *bounds, "visual", visual_protocol(manifest),
                        projection, origin)
            for phase in shot.get("action_segments") or []:
                start, end = (round(float(phase[k]) * 1e6) for k in ("start", "end"))
                if bounds[0] <= start < end <= bounds[1]:
                    put(db, product, sha, path, start, end, "first_look", version,
                        {"object": shot.get("object"), "action": phase.get("action"),
                         "result": phase.get("result"), "frames": shot.get("frame_paths", [])},
                        origin)
            if shot.get("text_pass") == "hires" and shot.get("text_agreed"):
                put(db, product, sha, path, *bounds, "label", version,
                    {"text": shot["text_agreed"], "frames": shot.get("text_frames", []),
                     "ocr_protocol": shot.get("ocr_protocol")}, origin)


@optional()
def remember_review(manifest, sentence, shot, review):
    if not database() or not scope(manifest) or not review.get("observation"):
        return
    if review.get("source_identity") != contract.source_identity(shot["video"]):
        raise ValueError("MATERIAL_SOURCE_CHANGED_DURING_REVIEW")
    bounds = review["review_window_us"]
    point_ids = set(review.get("supported_point_ids") or [])
    points = [p for p in sentence.get("selling_points", []) if p["id"] in point_ids]
    proof = {k: review[k] for k in ("basis","observation","evidence_intervals_us","claim_checks",
             "time_sensitive","scene_ids","frame_times_us","same_product","verdict","sheet",
             "completed_dense_review","presentation_observation","playback_observation","presentation_status",
             "confidence","preferred_speed","speed_range_override") if k in review}
    proof["reason"] = review.get("reason")
    proof["claims"] = points
    proof["claim_context"] = sentence.get("text")
    sheet, proof["sheet_sha256"] = archive(review["sheet"])
    proof["sheet"] = str(sheet)
    with connect() as db:
        sha = identity(shot["video"])
        if sha != review.get("source_sha256"):
            return
        put(db, scope(manifest), sha, shot["video"], bounds["start"], bounds["end"],
            "review", protocol(manifest), proof, sheet)


@optional(())
def windows(manifest, shot):
    if not database() or not scope(manifest) or manifest.get("material_index_sha256"):
        return []
    sha = identity(shot["video"])
    with connect() as db:
        version = protocol(manifest)
        rows = db.execute("""SELECT * FROM observations WHERE product=? AND sha=?
            AND (protocol=? OR grade='qc') AND grade != 'visual' AND start_us>=? AND end_us<=?
            ORDER BY CASE grade WHEN 'qc' THEN 0 WHEN 'review' THEN 1 WHEN 'label' THEN 2 ELSE 3 END, start_us, id""",
            (scope(manifest), sha, version, round(shot["source_start"]*1e6), round(shot["source_end"]*1e6)))
        found = []
        for row in rows:
            facts = json.loads(row["payload"])
            if row["protocol"] != version:
                bound = facts.get("scene_protocol")
                if not bound or bound != scene_protocol(manifest, facts.get("scene_ids")):
                    continue
            found.append({**dict(row), "facts":facts})
        return found



def archive(path):
    """Copy a checked proof once, independent of the task directory lifecycle."""
    from ..runtime.file_safety import file_lock
    path = Path(path)
    sha = identity(path)
    target = database().parent / "evidence-proofs" / sha[:2] / (sha + path.suffix)
    with file_lock(target):
        if not target.is_file():
            temp = target.with_suffix(target.suffix + ".tmp")
            shutil.copyfile(path,temp)
            if identity(temp) != sha:
                raise ValueError("EVIDENCE_PROOF_CHANGED_DURING_COPY")
            os.replace(temp,target)
        if identity(target) != sha:
            raise ValueError("EVIDENCE_PROOF_HASH_MISMATCH")
    return target, sha


def proof_available(row):
    f = row["facts"]
    path = row["origin"] if row["grade"] == "qc" else f.get("sheet")
    expected = f.get("report_sha256") if row["grade"] == "qc" else f.get("sheet_sha256")
    try:
        return bool(path and expected and identity(path) == expected)
    except (OSError, ValueError):
        return False


def visual_protocol(manifest):
    from . import vision_analyzer as vision
    from ..media import frame_quality
    transport = vision._vision_transport()
    cfg = vision._qwen_settings() if transport == "qwen" else vision._volc_settings()
    return contract.digest([vision.VISION_ANALYSIS_VERSION, transport, cfg["model"], cfg["base_url"],
                            vision._keyed_rules(manifest), vision.SAMPLE_STEP_S,
                            vision.env_int("JY_VISION_SAMPLE_MAX", vision.SAMPLE_MAX, minimum=2),
                            frame_quality.FRAME_QUALITY_VERSION if frame_quality.enabled() else "off"])


@optional()
def portable_visual(manifest, shot):
    if not database() or not scope(manifest):
        return None
    with connect() as db:
        rows = db.execute("""SELECT payload FROM observations WHERE grade='visual' AND
            product=? AND sha=? AND protocol=? AND start_us=? AND end_us=?""",
            (scope(manifest),identity(shot["video"]),visual_protocol(manifest),
             round(shot["source_start"]*1e6),round(shot["source_end"]*1e6))).fetchall()
    # Divergent historical observations need another look.
    return json.loads(rows[0][0]) if len(rows)==1 else None


@optional()
def preparation(sha, policy, save=None):
    with connect() as db:
        if save:
            db.execute("INSERT OR REPLACE INTO preparations VALUES(?,?,?)",(sha,policy,str(save)))
            return
        row = db.execute("SELECT record_path FROM preparations WHERE sha=? AND protocol=?",
                         (sha,policy)).fetchone()
        return Path(row[0]) if row else None
