"""Reusable observations of the same hygiene experiment, bound to source pixels."""
from __future__ import annotations

import copy
import contextlib
import json
import os
from pathlib import Path

from .. import ai_editor_contract as identity
from ..media import temporal_samples
from ..selection import experiment_contract, experiment_events
from . import evidence_store


@contextlib.contextmanager
def pair_lock(key):
    """Coalesce concurrent lookups of the same experiment when the store is available."""
    if not key or not evidence_store.database():
        yield
        return
    from ..runtime.file_safety import file_lock
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(evidence_store.database().parent / "experiment-locks" / key, timeout=300))
        except (OSError, TimeoutError):
            pass
        yield


def _table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS experiment_observations (
        kind TEXT NOT NULL, cache_key TEXT NOT NULL, observation TEXT NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(kind,cache_key,observation))""")


def _context_key(manifest):
    if not evidence_store.database() or not evidence_store.scope(manifest):
        return None
    from . import experiment_pair_review, experiment_event_review, purpose_review
    from ..selection import experiment_contract as contract
    try:
        files = (experiment_pair_review.__file__, experiment_event_review.__file__,
                 contract.__file__, temporal_samples.__file__)
        return identity.digest({"schema": "weipin-pair-reuse-v1", "product": evidence_store.scope(manifest),
            "policy": evidence_store.protocol(manifest), "rules": identity.rules_identity(manifest),
            "pair_prompt_policy": identity.digest(purpose_review.policy(manifest)),
            "code": [identity.file_sha256(path) for path in files]})
    except (OSError, ValueError, KeyError, TypeError):
        return None


def pair_key(manifest, samples, windows):
    """Use content and numbered frame clocks, never task id or source path."""
    context = _context_key(manifest)
    if not context or len(samples) != 2 or len(windows) != 2:
        return None
    try:
        return identity.digest({"context": context,
            "members": [{"original": evidence_store.identity(s["original"]["path"]),
                         "normalized": evidence_store.identity(s["source"]["path"]),
                         "frames": s["frames"], "source_end_us": s["source_end_us"],
                         "window": list(window)} for s, window in zip(samples, windows)],
        })
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _proof(path):
    archived, sha = evidence_store.archive(path)
    return {"path": str(archived), "sha256": sha}


def _proof_valid(proof):
    try:
        return evidence_store.identity(proof["path"]) == proof["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return False


@evidence_store.optional()
def remember_pair(manifest, key, pair):
    if not key:
        return
    members = pair.get("members") or []
    if (len(members) != 2 or key != pair_key(manifest, [m["samples"] for m in members],
                                             [m["window_frame_ids"] for m in members])):
        return
    try:
        experiment_contract.validate_pair_evidence(manifest, pair, require_disjoint_windows=False)
    except ValueError as exc:
        if str(exc) != "EXPERIMENT_PAIR_NOT_CONFIRMED":
            return
    observation = pair["observation"]
    payload = {"observation": observation, "sheet": _proof(pair["sheet"]["path"]),
               "binding": _proof(pair["input_binding"]["file"]["path"]),
               "source_task": pair["task_id"],
               "context": _context_key(manifest),
               "members": [{k: m[k] for k in ("part", "window_frame_ids", "visible_frame_ids",
                           "focus_frame_ids") if k in m} for m in pair["members"]],
               "sources": [{"sha256": evidence_store.identity(m["samples"]["original"]["path"]),
                            "path": m["samples"]["original"]["path"],
                            "start_us": m["samples"]["frames"][m["window_frame_ids"][0]]["pts_us"],
                            "end_us": min(m["samples"]["source_end_us"],
                                m["samples"]["frames"][m["window_frame_ids"][1]]["pts_us"] + 16667)}
                           for m in pair["members"]]}
    with evidence_store.connect() as db:
        _table(db)
        db.execute("INSERT OR IGNORE INTO experiment_observations VALUES(?,?,?,?)",
                   ("pair", key, identity.digest(observation), json.dumps(payload, ensure_ascii=False)))


@evidence_store.optional(())
def preferred_pairs(manifest, shots):
    """Return in-scope retrieval hints; reuse_pair still checks every source and frame."""
    context = _context_key(manifest)
    if not context:
        return ()
    with evidence_store.connect() as db:
        _table(db)
        rows = db.execute("SELECT cache_key,payload FROM experiment_observations WHERE kind='pair'").fetchall()
    grouped = {}
    for row in rows:
        try:
            payload = json.loads(row[1])
            if payload.get("context") == context:
                grouped.setdefault(row[0], []).append(payload)
        except (ValueError, TypeError):
            continue
    # Ranking is only a retrieval hint. Avoid resolving every UNC path over the
    # network here; the later source and sample validation is authoritative.
    def path_key(path):
        return os.path.normcase(os.path.abspath(str(path)))

    allowed = manifest.get("material_sources")
    allowed = {path_key(p) for p in allowed} if allowed is not None else None
    allowed_resolved = None
    result = []
    for entries in grouped.values():
        if len(entries) != 1:
            continue
        entry = entries[0]
        if (entry.get("observation", {}).get("verdict") != "matched" or len(entry.get("sources", [])) != 2
                or not all(_proof_valid(entry.get(k, {})) for k in ("sheet", "binding"))):
            continue
        candidates = []
        for source in entry["sources"]:
            found = []
            for shot in shots.values():
                path = path_key(shot["video"])
                if (shot.get("source_sha256") != source["sha256"] and path != path_key(source["path"])):
                    continue
                if allowed is not None and path not in allowed:
                    # Only the matching candidate can need Windows 8.3 or link
                    # resolution; common UNC spellings remain purely lexical.
                    if allowed_resolved is None:
                        allowed_resolved = {path_key(Path(p).resolve()) for p in allowed}
                    if path_key(Path(shot["video"]).resolve()) not in allowed_resolved:
                        continue
                if (round(shot["source_start"] * 1e6) <= source["start_us"]
                        and round(shot["source_end"] * 1e6) >= source["end_us"]):
                    found.append(shot["shot_id"])
            candidates.append(found)
        for first in candidates[0]:
            for second in candidates[1]:
                if first != second and (first, second) not in result:
                    result.append((first, second))
    return result


@evidence_store.optional()
def reuse_pair(manifest, key, samples, directory):
    if not key:
        return None
    from .experiment_pair_review import input_binding, PROTOCOL
    from .purpose_review import save
    with evidence_store.connect() as db:
        _table(db)
        rows = db.execute("SELECT payload FROM experiment_observations WHERE kind='pair' AND cache_key=?",
                          (key,)).fetchall()
    try:
        payloads = [json.loads(row[0]) for row in rows]
        # Contradictory or independently different observations require another look.
        if len(payloads) != 1 or payloads[0]["observation"]["verdict"] != "matched":
            return None
        data = payloads[0]
        if not all(_proof_valid(data[k]) for k in ("sheet", "binding")):
            return None
        members = [{**copy.deepcopy(item), "samples": sample}
                   for item, sample in zip(data["members"], samples)]
        if len(members) != 2 or key != pair_key(manifest, samples, [m["window_frame_ids"] for m in members]):
            return None
        sheet = data["sheet"]["path"]
        bound = input_binding(manifest, members, sheet)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        binding_path = directory / "pair.binding.json"
        for sample in samples:
            temporal_samples.validate(sample)
            temporal_samples.guard_outputs(sample, directory / "pair.json", (".binding.json", ".pair.json"))
        save(binding_path, bound)
        pair = {"protocol": PROTOCOL, "task_id": manifest["task_id"],
                "rules": identity.rules_identity(manifest),
                "context": {"product": manifest["product"], "category": "weipin"},
                "members": members, "observation": data["observation"],
                "sheet": identity.source_identity(sheet),
                "input_binding": {"file": identity.source_identity(binding_path),
                                  "sha256": identity.file_sha256(binding_path)},
                "retrieval_hint": experiment_contract.adjacency_hint(
                    samples[0]["original"]["path"], samples[1]["original"]["path"]),
                "formal_delivery_allowed": False,
                "method": "reused jointly reviewed source frames; final film requires review",
                "reused_from_task": data["source_task"]}
        experiment_contract.validate_pair_evidence(manifest, pair, require_disjoint_windows=False)
        save(directory / "pair.pair.json", pair)
        evidence_store.record(manifest, "experiment_pair_reused", {"source_task": data["source_task"]})
        return pair
    except (KeyError, TypeError, ValueError, OSError, IndexError):
        return None


def _motion_key(key, focus, pair):
    return identity.digest([key, focus, pair["observation"]])


def _valid_events(pair, motion):
    from .experiment_event_review import unshown_boundaries
    facts, observations = motion.get("event_observations"), motion.get("observations")
    if (motion.get("method") != "actual_frame_events_with_dense_refinement"
            or not isinstance(facts, list) or not isinstance(observations, list)
            or len(facts) != 2 or len(observations) != 2):
        return False
    for member, fact, observation in zip(pair["members"], facts, observations):
        shown = fact["shown_frame_ids"]
        if (unshown_boundaries(fact["frame_refinement"], shown)
                or experiment_events.compile_events(fact["frame_refinement"], member) != observation):
            return False
    return True


@evidence_store.optional()
def remember_motion(manifest, key, focus, pair, motion, directory):
    if not key:
        return
    from .experiment_motion_review import validate
    try:
        members = pair["members"]
        if key != pair_key(manifest, [m["samples"] for m in members],
                           [m["window_frame_ids"] for m in members]):
            return
        validate(manifest, pair, motion)
        if (not _valid_events(pair, motion) or motion["observations"][0].get("coverage") != focus
                or motion["observations"][1].get("coverage") != "complete_verification"):
            return
        facts = copy.deepcopy(motion["event_observations"])
        proofs = []
        for part, fact in zip(("A", "B"), facts):
            detail = fact["sheet"]
            if identity.source_identity(detail["path"]) != detail:
                return
            proofs.append(_proof(detail["path"]))
            coarse = Path(directory) / f"events-{focus}-{part}.jpg"
            if not coarse.is_file():
                return
            proofs.append(_proof(coarse))
        payload = {"facts": facts, "observations": motion["observations"],
                   "proofs": proofs, "source_task": motion["task_id"]}
        with evidence_store.connect() as db:
            _table(db)
            db.execute("INSERT OR IGNORE INTO experiment_observations VALUES(?,?,?,?)",
                       ("motion", _motion_key(key, focus, pair), identity.digest(payload["observations"]),
                        json.dumps(payload, ensure_ascii=False)))
    except (ValueError, KeyError, TypeError, OSError, IndexError):
        return


@evidence_store.optional()
def reuse_motion(manifest, key, focus, pair, directory):
    if not key:
        return None
    from .experiment_pair_review import input_binding
    from .experiment_motion_review import PROTOCOL, validate
    from .purpose_review import save
    with evidence_store.connect() as db:
        _table(db)
        rows = db.execute("SELECT payload FROM experiment_observations WHERE kind='motion' AND cache_key=?",
                          (_motion_key(key, focus, pair),)).fetchall()
    try:
        members = pair["members"]
        if key != pair_key(manifest, [m["samples"] for m in members],
                           [m["window_frame_ids"] for m in members]):
            return None
        if len(rows) != 1:
            return None
        data = json.loads(rows[0][0])
        if len(data["proofs"]) != 4 or not all(_proof_valid(p) for p in data["proofs"]):
            return None
        facts = data["facts"]
        for i, fact in enumerate(facts):
            fact["sheet"] = identity.source_identity(data["proofs"][i * 2]["path"])
        motion = {"protocol": PROTOCOL, "task_id": manifest["task_id"],
                  "rules": identity.rules_identity(manifest),
                  "pair_input_sha256": identity.digest(input_binding(manifest, pair["members"], pair["sheet"]["path"])),
                  "method": "actual_frame_events_with_dense_refinement", "event_observations": facts,
                  "observations": data["observations"], "formal_delivery_allowed": False,
                  "reused_from_task": data["source_task"]}
        validate(manifest, pair, motion)
        if (not _valid_events(pair, motion) or motion["observations"][0].get("coverage") != focus
                or motion["observations"][1].get("coverage") != "complete_verification"):
            return None
        save(Path(directory) / f"events-{focus}.motion.json", motion)
        evidence_store.record(manifest, "experiment_events_reused", {"source_task": data["source_task"], "focus": focus})
        return motion
    except (KeyError, TypeError, ValueError, OSError, IndexError):
        return None
