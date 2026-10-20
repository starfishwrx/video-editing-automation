"""Content-addressed experimental material facts, independent of selection rules.

SQLite is the coordination boundary between task processes.  A product scope and
an explicit input inventory are both required by callers; sharing file bytes
never grants permission to reuse a different SKU's annotations.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


OBSERVATION_PROTOCOL = "visual-observation-v1"
_DERIVED_KEYS = {"visual_tags", "evidence_tags", "frame_tags", "labels",
                 "rule_checks", "label_results", "certification", "human_review"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def product_identity(manifest: dict[str, Any]) -> str:
    """Do not use brand alone: two SKUs may share a brand."""
    value = str(manifest.get("product_id") or manifest.get("product") or "").strip()
    if not value:
        raise ValueError("indexed_contextual requires product_id or product")
    return value


class MaterialLeaseTimeout(TimeoutError):
    pass


class MaterialStore:
    def __init__(self, index_dir: str | Path):
        self.index_dir = Path(index_dir).expanduser().resolve()
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.index_dir / "materials.sqlite3"
        self._batch_local = threading.local()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS materials (
                    material_id TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS paths (
                    path TEXT PRIMARY KEY, material_id TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                    ctime_ns INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS product_materials (
                    product_id TEXT NOT NULL, material_id TEXT NOT NULL,
                    PRIMARY KEY(product_id, material_id));
                CREATE TABLE IF NOT EXISTS frames (
                    frame_id TEXT PRIMARY KEY, material_id TEXT NOT NULL,
                    timestamp_us INTEGER NOT NULL, protocol TEXT NOT NULL,
                    path TEXT NOT NULL, sha256 TEXT NOT NULL,
                    UNIQUE(material_id, timestamp_us, protocol));
                CREATE TABLE IF NOT EXISTS observations (
                    observation_id TEXT PRIMARY KEY, material_id TEXT NOT NULL,
                    product_id TEXT NOT NULL, protocol TEXT NOT NULL,
                    start_us INTEGER NOT NULL, end_us INTEGER NOT NULL,
                    payload TEXT NOT NULL, provenance TEXT NOT NULL,
                    requirements TEXT NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS observation_scope ON observations
                    (product_id, material_id, protocol, created_at);
                CREATE TABLE IF NOT EXISTS derived_labels (
                    observation_id TEXT NOT NULL, policy TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(observation_id, policy));
                CREATE TABLE IF NOT EXISTS human_certifications (
                    observation_id TEXT NOT NULL, ledger_ref TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(observation_id, ledger_ref));
                CREATE TABLE IF NOT EXISTS vectors (
                    frame_id TEXT NOT NULL, model_key TEXT NOT NULL,
                    path TEXT NOT NULL, metadata TEXT NOT NULL,
                    PRIMARY KEY(frame_id, model_key));
                CREATE TABLE IF NOT EXISTS leases (
                    lease_key TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS model_signatures (
                    request_key TEXT PRIMARY KEY, metadata TEXT NOT NULL);
            """)

    @contextlib.contextmanager
    def _db(self):
        shared = getattr(self._batch_local, "db", None)
        if shared is not None:
            yield shared
            return
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextlib.contextmanager
    def batch(self):
        """One material per transaction; preserve checkpoints between materials.

        Opening the final WAL connection for every frame forces repeated WAL
        checkpoints on Windows. Nest fact/frame writes within this connection.
        """
        if getattr(self._batch_local, "db", None) is not None:
            yield
            return
        with self._db() as db:
            self._batch_local.db = db
            try:
                yield
            finally:
                self._batch_local.db = None

    def ingest_sources(self, sources: Iterable[str | Path | dict[str, Any]],
                       product_id: str) -> list[dict[str, Any]]:
        if not str(product_id).strip():
            raise ValueError("product_id is required")
        result = []
        for source in sources:
            raw = (source.get("path") or source.get("video") or source.get("source")) \
                if isinstance(source, dict) else source
            if not raw:
                raise ValueError("source path is required")
            path = Path(raw).expanduser().resolve()
            stat = path.stat()
            if not path.is_file():
                raise ValueError(f"source is not a file: {path}")
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            key = _path_key(path)
            with self._db() as db:
                prior = db.execute("SELECT * FROM paths WHERE path=?", (key,)).fetchone()
            reused = bool(prior and (prior["size_bytes"], prior["mtime_ns"], prior["ctime_ns"]) == signature)
            if reused:
                material_id = prior["material_id"]
            else:
                content = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                        content.update(chunk)
                after = path.stat()
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != signature:
                    raise RuntimeError(f"source changed while hashing: {path}")
                material_id = content.hexdigest()
            with self._db() as db:
                db.execute("INSERT OR IGNORE INTO materials VALUES(?,?,?)",
                           (material_id, stat.st_size, time.time()))
                db.execute("INSERT OR REPLACE INTO paths VALUES(?,?,?,?,?)",
                           (key, material_id, *signature))
                db.execute("INSERT OR IGNORE INTO product_materials VALUES(?,?)",
                           (product_id, material_id))
            result.append({"material_id": material_id, "path": str(path),
                           "product_id": product_id, "size_bytes": stat.st_size,
                           "hash_reused": reused})
        return result

    def material_for_path(self, path: str | Path, *, product_id: str) -> str | None:
        """Recheck the source signature before using an old path association."""
        try:
            current = Path(path).expanduser().resolve().stat()
        except OSError:
            return None
        with self._db() as db:
            row = db.execute("""SELECT p.* FROM paths p JOIN product_materials s
                ON s.material_id=p.material_id WHERE p.path=? AND s.product_id=?""",
                             (_path_key(path), product_id)).fetchone()
        if row and (row["size_bytes"], row["mtime_ns"], row["ctime_ns"]) == (
                current.st_size, current.st_mtime_ns, current.st_ctime_ns):
            return row["material_id"]
        return None

    def paths_for(self, material_id: str) -> list[str]:
        with self._db() as db:
            rows = db.execute("SELECT path FROM paths WHERE material_id=? ORDER BY path",
                              (material_id,)).fetchall()
        return [row["path"] for row in rows]

    def sources(self, material_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        allowed = set(material_ids) if material_ids is not None else None
        with self._db() as db:
            rows = db.execute("SELECT p.*,m.created_at FROM paths p JOIN materials m ON m.material_id=p.material_id ORDER BY p.path").fetchall()
        return [dict(row) for row in rows if allowed is None or row["material_id"] in allowed]

    def frames(self, material_ids: Iterable[str]) -> list[dict[str, Any]]:
        return [row for material_id in sorted(set(material_ids)) for row in self.get_frames(material_id)]

    def scoped_ids(self, product_id: str, allowed_material_ids: Iterable[str]) -> list[str]:
        allowed = set(allowed_material_ids)
        if not allowed:
            return []
        with self._db() as db:
            rows = db.execute("SELECT material_id FROM product_materials WHERE product_id=?",
                              (product_id,)).fetchall()
        return sorted(allowed.intersection(row[0] for row in rows))

    def _require_scope(self, material_id: str, product_id: str) -> None:
        if material_id not in self.scoped_ids(product_id, [material_id]):
            raise ValueError("material is outside the product scope")

    def save_frame(self, material_id: str, timestamp_us: int, path: str | Path,
                   *, protocol: str = "representative-v1") -> dict[str, Any]:
        if not isinstance(timestamp_us, int) or timestamp_us < 0:
            raise ValueError("frame timestamp must be non-negative integer microseconds")
        with self._db() as db:
            if not db.execute("SELECT 1 FROM materials WHERE material_id=?", (material_id,)).fetchone():
                raise ValueError("unknown material identity")
        path = Path(path)
        content = path.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        frame_id = _digest([material_id, int(timestamp_us), protocol])
        # Own the cached frame; report directories may be removed after a task.
        output = self.index_dir / "frames" / material_id / f"{frame_id}{path.suffix.lower() or '.jpg'}"
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.exists() or hashlib.sha256(output.read_bytes()).hexdigest() != sha:
            temp = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
            temp.write_bytes(content)
            os.replace(temp, output)
        with self._db() as db:
            # Changed decoding at one identity invalidates that frame's embeddings.
            prior = db.execute("SELECT sha256 FROM frames WHERE frame_id=?", (frame_id,)).fetchone()
            if prior and prior[0] != sha:
                db.execute("DELETE FROM vectors WHERE frame_id=?", (frame_id,))
            db.execute("INSERT OR REPLACE INTO frames VALUES(?,?,?,?,?,?)",
                       (frame_id, material_id, int(timestamp_us), protocol, str(output), sha))
        return {"frame_id": frame_id, "material_id": material_id, "timestamp_us": int(timestamp_us),
                "protocol": protocol, "path": str(output), "sha256": sha}

    def get_frames(self, material_id: str, *, protocol: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM frames WHERE material_id=?"
        args: list[Any] = [material_id]
        if protocol:
            sql += " AND protocol=?"
            args.append(protocol)
        with self._db() as db:
            rows = db.execute(sql + " ORDER BY timestamp_us", args).fetchall()
        result = []
        for row in rows:
            path = Path(row["path"])
            try:
                if hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]:
                    result.append(dict(row))
            except OSError:
                continue
        return result

    def save_observation(self, material_id: str, observation: dict[str, Any], *,
                         product_id: str, protocol: str = OBSERVATION_PROTOCOL,
                         start_us: int = 0, end_us: int,
                         provenance: dict[str, Any] | None = None,
                         verified_requirements: Iterable[str] = (),
                         state: str = "observed") -> dict[str, Any]:
        self._require_scope(material_id, product_id)
        if not isinstance(start_us, int) or not isinstance(end_us, int) or start_us < 0 or end_us <= start_us:
            raise ValueError("observation window must be positive integer microseconds")
        if state not in {"pending", "observed", "verified", "unusable"}:
            raise ValueError("unknown observation state")
        provenance = dict(provenance or {})
        requirements = sorted(set(str(item) for item in verified_requirements))
        # A legacy stamped list never establishes that any rule was viewed.
        if requirements and (not provenance.get("evidence") or provenance.get("kind", "").startswith("legacy")):
            raise ValueError("verified requirements require non-legacy evidence provenance")
        if state == "verified" and not provenance.get("evidence"):
            raise ValueError("verified observation requires evidence provenance")
        raw = {key: value for key, value in observation.items() if key not in _DERIVED_KEYS}
        observation_id = _digest([material_id, product_id, protocol, start_us, end_us,
                                  raw, provenance, requirements, state])
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (observation_id, material_id, product_id, protocol, start_us, end_us,
                        _json(raw), _json(provenance), _json(requirements), state, time.time()))
        derived = {key: value for key, value in observation.items()
                   if key in _DERIVED_KEYS and key not in {"rule_checks", "human_review", "certification"}}
        if derived:
            self.save_derived_labels(observation_id, derived, policy="legacy-import" if provenance.get("kind", "").startswith("legacy") else "observation-labels")
        return self._observation_by_id(observation_id)

    def _observation_by_id(self, observation_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM observations WHERE observation_id=?", (observation_id,)).fetchone()
        result = dict(row)
        result["observation"] = json.loads(result.pop("payload"))
        result["provenance"] = json.loads(result["provenance"])
        result["verified_requirements"] = json.loads(result.pop("requirements"))
        return result

    def get_observations(self, material_id: str, *, product_id: str,
                         protocol: str | None = None,
                         requirements: Iterable[str] = ()) -> list[dict[str, Any]]:
        self._require_scope(material_id, product_id)
        sql = "SELECT observation_id FROM observations WHERE material_id=? AND product_id=?"
        args: list[Any] = [material_id, product_id]
        if protocol:
            sql += " AND protocol=?"
            args.append(protocol)
        with self._db() as db:
            rows = db.execute(sql + " ORDER BY created_at DESC", args).fetchall()
        required = set(requirements)
        result = []
        for row in rows:
            item = self._observation_by_id(row[0])
            item["missing_requirements"] = sorted(required - set(item["verified_requirements"]))
            item["requirements_satisfied"] = not item["missing_requirements"]
            result.append(item)
        return result

    def save_derived_labels(self, observation_id: str, labels: dict[str, Any], *, policy: str) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO derived_labels VALUES(?,?,?)",
                       (observation_id, policy, _json(labels)))

    def get_derived_labels(self, observation_id: str, *, policy: str | None = None) -> dict[str, Any]:
        with self._db() as db:
            if policy:
                rows = db.execute("SELECT policy,payload FROM derived_labels WHERE observation_id=? AND policy=?",
                                  (observation_id, policy)).fetchall()
            else:
                rows = db.execute("SELECT policy,payload FROM derived_labels WHERE observation_id=?",
                                  (observation_id,)).fetchall()
        return {row["policy"]: json.loads(row["payload"]) for row in rows}

    def save_human_certification(self, observation_id: str, certification: dict[str, Any], *, ledger_ref: str) -> None:
        if not ledger_ref:
            raise ValueError("human certification must refer to the existing review ledger")
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO human_certifications VALUES(?,?,?)",
                       (observation_id, ledger_ref, _json(certification)))

    def import_analysis(self, analysis: dict[str, Any] | list[Any], manifest: dict[str, Any]) -> dict[str, int]:
        """Read-only migration: source identity must be in this task's ingested inventory.

        Historical rule_checks and approval fields remain untrusted.  A copied
        observation still requires new evidence for any newly requested rule.
        """
        product_id = product_identity(manifest)
        rows = analysis.get("shots", []) if isinstance(analysis, dict) else analysis
        stats = {"imported": 0, "skipped": 0, "frames": 0}
        for row in rows or []:
            with self.batch():
                if not isinstance(row, dict):
                    stats["skipped"] += 1
                    continue
                source = str(row.get("video") or row.get("source_path") or "")
                material_id = self.material_for_path(source, product_id=product_id) if source else None
                if not material_id:
                    stats["skipped"] += 1
                    continue
                start_us = int(row.get("source_start_us", round(float(row.get("source_start") or 0) * 1_000_000)))
                end_us = int(row.get("source_end_us", round(float(row.get("source_end") or 0) * 1_000_000)))
                if end_us <= start_us:
                    stats["skipped"] += 1
                    continue
                frame_paths = list(row.get("frame_paths") or [])
                if row.get("frame_path") and row["frame_path"] not in frame_paths:
                    frame_paths.insert(0, row["frame_path"])
                cached_frames = []
                # vision_frames are the sequence actually sent to the old model;
                # frame_paths normally contains only deterministic mid/end frames.
                # Keep the complete viewed sequence instead of silently reducing
                # its provenance to two representative images.
                explicit_frames = [f for field in ("vision_frames", "sample_frames")
                                   for f in row.get(field) or [] if isinstance(f, dict)]
                specs: list[tuple[str, int]] = []
                explicit_paths = set()
                for frame in explicit_frames:
                    frame_path = str(frame.get("path") or frame.get("frame_path") or "")
                    timestamp = frame.get("timestamp", frame.get("time"))
                    time_us = frame.get("timestamp_us")
                    if type(time_us) is not int:
                        try:
                            seconds = float(timestamp)
                            time_us = round(seconds * 1_000_000) if math.isfinite(seconds) else None
                        except (TypeError, ValueError):
                            time_us = None
                    if frame_path and type(time_us) is int and start_us <= time_us <= end_us:
                        specs.append((frame_path, time_us))
                        explicit_paths.add(_path_key(frame_path))
                for index, frame_path in enumerate(frame_paths):
                    if not frame_path or _path_key(frame_path) in explicit_paths:
                        continue
                    timestamp = row.get("frame_time") if index == 0 else None
                    if timestamp is not None:
                        try:
                            seconds = float(timestamp)
                            time_us = round(seconds * 1_000_000) if math.isfinite(seconds) else None
                        except (TypeError, ValueError):
                            time_us = None
                    elif len(frame_paths) <= 2:
                        # The old analyze_manifest contract emits exactly mid/end.
                        time_us = (start_us + end_us) // 2 if index == 0 else max(start_us, end_us - 150_000)
                    else:
                        # A longer arbitrary list has no defensible timing map.
                        continue
                    if type(time_us) is int and start_us <= time_us <= end_us:
                        specs.append((str(frame_path), time_us))
                seen_times = set()
                for frame_path, time_us in specs:
                    if time_us in seen_times or not Path(frame_path).is_file():
                        continue
                    seen_times.add(time_us)
                    cached_frames.append(self.save_frame(material_id, time_us, frame_path, protocol="legacy-representative-v1"))
                    stats["frames"] += 1
                description = str(row.get("description") or row.get("visual_description") or "").strip()
                if not description:
                    stats["skipped"] += 1
                    continue
                payload = dict(row)
                payload["frame_paths"] = [f["path"] for f in cached_frames]
                if cached_frames:
                    payload["frame_path"] = cached_frames[0]["path"]
                    payload["frame_time"] = cached_frames[0]["timestamp_us"] / 1_000_000
                    payload["vision_frames"] = [{"path": f["path"], "timestamp_us": f["timestamp_us"]}
                                                 for f in cached_frames]
                self.save_observation(material_id, payload, product_id=product_id,
                                      protocol=str(row.get("observation_protocol") or OBSERVATION_PROTOCOL),
                                      start_us=start_us, end_us=end_us,
                                      state="observed" if cached_frames else "pending",
                                      provenance={"kind": "legacy-shot-analysis", "shot_id": row.get("shot_id"),
                                                  "evidence": [f["frame_id"] for f in cached_frames],
                                                  "legacy_rule_checks_ignored": list(row.get("rule_checks") or [])})
                stats["imported"] += 1
        return stats

    def import_legacy_cache(self, path: str | Path, manifest: dict[str, Any]) -> dict[str, int]:
        """Cache projections without an attributable source path are not reusable."""
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        rows = list(payload.values()) if isinstance(payload, dict) else payload
        return self.import_analysis(rows, manifest)

    def save_vector(self, frame_id: str, model_key: str, vector: Any, *, metadata: dict[str, Any]) -> None:
        import numpy as np
        value = np.asarray(vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(value))
        if not np.isfinite(value).all() or norm <= 0:
            raise ValueError("embedding must be finite and non-zero")
        value = value / norm
        folder = self.index_dir / "vectors" / _digest(model_key)
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / f"{frame_id}.npy"
        temp = output.with_name(f".{frame_id}.{uuid.uuid4().hex}.tmp")
        with temp.open("wb") as stream:
            np.save(stream, value, allow_pickle=False)
        os.replace(temp, output)
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)",
                       (frame_id, model_key, str(output), _json(metadata)))

    def get_model_signature(self, request_key: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT metadata FROM model_signatures WHERE request_key=?", (request_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_model_signature(self, request_key: str, metadata: dict[str, Any]) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO model_signatures VALUES(?,?)", (request_key, _json(metadata)))

    def get_vector(self, frame_id: str, model_key: str):
        import numpy as np
        with self._db() as db:
            row = db.execute("SELECT path FROM vectors WHERE frame_id=? AND model_key=?",
                             (frame_id, model_key)).fetchone()
        if row and Path(row[0]).is_file():
            try:
                value = np.load(row[0], allow_pickle=False)
                return value if value.ndim == 1 and np.isfinite(value).all() else None
            except (OSError, ValueError):
                return None
        return None

    @contextlib.contextmanager
    def lease(self, material_id: str, purpose: str = "visual-observation", *,
              timeout_s: float = 120, lease_s: float = 300):
        """Cross-process renewable lease; timeout never authorizes bypassing it."""
        if lease_s <= 0 or timeout_s < 0:
            raise ValueError("invalid lease timing")
        key = _digest([material_id, purpose])
        owner = f"{os.getpid()}:{uuid.uuid4().hex}"
        deadline = time.monotonic() + timeout_s
        while True:
            now = time.time()
            with self._db() as db:
                db.execute("INSERT INTO leases VALUES(?,?,?) ON CONFLICT(lease_key) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at WHERE leases.expires_at<=?",
                           (key, owner, now + lease_s, now))
                row = db.execute("SELECT owner FROM leases WHERE lease_key=?", (key,)).fetchone()
                acquired = row[0] == owner
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise MaterialLeaseTimeout(f"material lease busy: {material_id}, {purpose}")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        stop = threading.Event()
        lost: list[Exception] = []

        def heartbeat():
            while not stop.wait(max(0.01, min(10, lease_s / 3))):
                try:
                    with self._db() as db:
                        changed = db.execute("UPDATE leases SET expires_at=? WHERE lease_key=? AND owner=?",
                                             (time.time() + lease_s, key, owner)).rowcount
                    if not changed:
                        lost.append(RuntimeError("material lease ownership was lost"))
                        return
                except Exception as exc:
                    lost.append(exc)
                    return

        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        try:
            yield {"material_id": material_id, "purpose": purpose, "owner": owner}
            if lost:
                raise RuntimeError("material lease could not be renewed") from lost[0]
        finally:
            stop.set()
            worker.join(timeout=30)
            with self._db() as db:
                db.execute("DELETE FROM leases WHERE lease_key=? AND owner=?", (key, owner))
