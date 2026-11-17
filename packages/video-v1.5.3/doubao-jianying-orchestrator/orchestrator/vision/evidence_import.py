"""Admit existing reports after checking their source and quality bindings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import ai_editor_contract as contract
from .. import visual_qc_contract as qc
from . import evidence_store as store
from . import vision_analyzer as vision


def read(path):
    return Path(path).read_text(encoding="utf-8-sig")


@store.optional(0)
def import_qc(manifest, report, path):
    segments = report.get("selection_segments", [])
    if (not store.scope(manifest) or qc.report_status(report) != "PASS" or
            not qc.report_matches_draft(report, Path(report["draft"])) or
            contract.digest(segments) != report.get("selection_sha256") or
            contract.rules_identity(manifest) != report.get("editorial_rules")):
        return 0
    reviews = {r["index"]:r for r in report.get("model_review", [])}
    matched_path = Path(path).with_name("shot_match_report.json")
    if not matched_path.is_file():
        return 0
    matched = json.loads(read(matched_path))
    owner = matched.get("manifest") or {}
    if (contract.digest(matched.get("segments")) != report["selection_sha256"]
            or store.scope(owner) != store.scope(manifest)):
        return 0
    allowed = {str(Path(p).resolve()) for p in owner.get("material_sources", [])}
    headless = Path(report["draft"]) / "headless-production.json"
    if not headless.is_file():
        return 0
    assets = {a.get("source"):a for a in json.loads(read(headless)).get("record",{}).get("assets", [])}
    count = 0
    for seg in segments:
        r = reviews.get(seg.get("index"), {})
        selection = seg.get("ai_selection", {})
        parts = seg.get("span_parts") or []
        if (len(parts)>1 or not selection or not seg.get("selling_points") or
                r.get("match_level") != "DIRECT" or r.get("visual_ok") is not True or
                r.get("claim") != seg.get("text") or not r.get("observed") or
                selection.get("judgment") != "matched" or
                selection.get("policy_version") != contract.POLICY_VERSION):
            continue
        source = seg["video"]
        if str(Path(source).resolve()) not in allowed:
            continue
        if contract.source_identity(source) != selection.get("source_identity"):
            continue
        win = selection["window_us"]
        if [win["start"], win["end"]] != [round(seg[k]*1e6) for k in ("source_start","source_end")]:
            continue
        points = [p for p in selection["selling_points"] if p["id"] in selection["supported_point_ids"]]
        if not points:
            continue
        facts = {k:selection.get(k) for k in ("basis","time_sensitive","scene_ids")}
        facts.update(observation=r["observed"], claims=points, claim_context=r["claim"],
                     confidence=r.get("confidence"), same_product=True, verdict="matched",
                     evidence_intervals_us=[win], report_sha256=store.identity(path),
                     historical_identity=selection.get("identity"), segment_index=seg["index"],
                     scene_protocol=store.scene_protocol(manifest, selection.get("scene_ids")))
        asset = assets.get(source, {})
        sha = store.identity(source)
        if sha != asset.get("sha256"):
            continue
        historical = selection.get("identity") or {}
        expected_model = (vision._qwen_settings() if vision._vision_transport()=="qwen"
                          else vision._text_settings() or vision._volc_settings())["model"]
        if historical.get("model") != expected_model or historical.get("editorial_rules") != report["editorial_rules"]:
            continue
        proof_path, _ = store.archive(path)
        with store.connect() as db:
            store.put(db, store.scope(manifest), sha, source, win["start"], win["end"],
                      "qc", store.protocol(manifest), facts, proof_path)
        count += 1
    return count


def import_product(manifest, analysis, path, frozen):
    sources = {str(Path(s["path"]).resolve()):s for s in analysis["material_cache"]["source_inventory"]}
    for shot in analysis.get("shots", []):
        source = str(Path(shot["video"]).resolve())
        if (contract.source_identity(source) != sources.get(source) or
                vision._vision_cache_key(manifest, shot) != shot.get("analysis_cache_key")):
            raise ValueError("HISTORICAL_OBSERVATION_IDENTITY_MISMATCH")
    from .evidence_media import seed
    seed(manifest, analysis, vision._vision_cache_path(Path(path).parent).parent, frozen)
    store.index_analysis(manifest, analysis, path)


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("kind",choices=["product","qc","review","experiment"])
    cli.add_argument("--manifest", type=Path, required=True)
    cli.add_argument("--report", type=Path, required=True)
    cli.add_argument("--contract", type=Path, help="Frozen input SHA-256 contract for historical product or review")
    args = cli.parse_args()
    if args.kind in ("product","review","experiment") and not args.contract:
        cli.error("--contract is required for historical product, review or experiment")
    manifest, report = (json.loads(read(p)) for p in (args.manifest,args.report))
    if args.kind == "qc":
        count = import_qc(manifest,report,args.report)
    elif args.kind == "product":
        import_product(manifest, report, args.report, json.loads(read(args.contract)))
        count = len(report["shots"])
    elif args.kind == "review":
        count = import_reviews(manifest,report,args.report,json.loads(read(args.contract)))
    else:
        count = import_experiment(manifest, report, args.report, json.loads(read(args.contract)))
    print(json.dumps({"kind":args.kind,"admitted":count},ensure_ascii=False))


def import_reviews(manifest, plan, path, frozen):
    frozen = dict(frozen)
    if frozen.pop("contract_hash", "") != "sha256:" + contract.digest(frozen):
        raise ValueError("FROZEN_MATERIAL_CONTRACT_INVALID")
    hashes = {str(Path(x["path"]).resolve()):x["sha256"] for x in frozen["inputs"]}
    info = plan.get("identity") or {}
    cfg = vision._qwen_settings() if vision._vision_transport() == "qwen" else vision._volc_settings()
    if (info.get("model") != cfg["model"] or info.get("policy_version") != contract.POLICY_VERSION
            or info.get("editorial_rules") != contract.rules_identity(manifest)):
        return 0
    task_file = next((parent/"task.json" for parent in path.parents if (parent/"task.json").is_file()), None)
    task = json.loads(read(task_file)) if task_file else {}
    if (contract.digest(task) != frozen.get("task_sha256") or task.get("record_id") != frozen.get("task_id")
            or not task.get("product") or task.get("product") != manifest.get("product")):
        return 0
    allowed = {str(Path(p).resolve()) for p in manifest.get("material_sources", [])}
    count = 0
    for sentence in plan.get("sentences") or []:
        for review in plan.get("reviews", {}).get(str(sentence.get("index")), []):
            sig = review.get("source_identity") or {}
            source = sig.get("path")
            if (source in allowed and Path(source).is_file() and contract.source_identity(source) == sig
                    and Path(review.get("sheet") or "").is_file() and review.get("review_window_us")
                    and review.get("observation") and hashes.get(source) == store.identity(source)):
                # Historical plans with no bound manifest digest are retrieval
                # hints. They never establish support for a new spoken claim.
                reviewed = {**review,"source_sha256":hashes[source],"completed_dense_review":False}
                store.remember_review(manifest,sentence,{"video":source},reviewed)
                count += 1
    return count


def import_experiment(manifest, pair, path, frozen):
    """Admit a historical joint review only with its actual request and frozen inputs."""
    from ..selection import experiment_contract
    from . import experiment_evidence, purpose_review

    frozen = dict(frozen)
    if frozen.pop("contract_hash", "") != "sha256:" + contract.digest(frozen):
        raise ValueError("FROZEN_MATERIAL_CONTRACT_INVALID")
    task_file = next((parent / "task.json" for parent in Path(path).parents
                      if (parent / "task.json").is_file()), None)
    task = json.loads(read(task_file)) if task_file else {}
    if (contract.digest(task) != frozen.get("task_sha256")
            or task.get("record_id") != frozen.get("task_id")
            or task.get("record_id") != pair.get("task_id")
            or task.get("product") != manifest.get("product")):
        raise ValueError("HISTORICAL_EXPERIMENT_TASK_CHANGED")
    experiment_contract.validate_pair_evidence(manifest, pair, require_disjoint_windows=False)
    inputs = {str(Path(row["path"]).resolve()): row["sha256"] for row in frozen["inputs"]}
    samples = [member["samples"] for member in pair["members"]]
    if any(inputs.get(s["original"]["path"]) != store.identity(s["original"]["path"]) for s in samples):
        raise ValueError("HISTORICAL_EXPERIMENT_SOURCE_CHANGED")
    base = Path(path).with_name("pair")
    request = json.loads(read(base.with_suffix(".request.json")))
    prompt = read(base.with_suffix(".prompt.txt"))
    observation = json.loads(read(base.with_suffix(".json")))
    binding = json.loads(read(pair["input_binding"]["file"]["path"]))
    cfg = vision._qwen_settings() if vision._vision_transport() == "qwen" else vision._text_settings() or vision._volc_settings()
    # The deployed paper-specific mirror equivalence overlay is unrelated to
    # hygiene absorption. No other changed policy is accepted as equivalent.
    mirror_overlay = ("唯一对象等效例外：口播卖点为擦镜子/镜面时，清晰展示擦拭光滑反光玻璃面"
        "（窗玻璃、玻璃面板、烤箱/油烟机玻璃、镜面类表面）且可见擦拭后清洁效果的镜头可支持该卖点；"
        "看不到清洁效果、非光滑反光表面或其他卖点不适用此等效。\n")
    comparable_prompt = prompt.replace(mirror_overlay, "", 1)
    if (request.get("model") != cfg["model"] or request.get("endpoint", "").rstrip("/") != cfg["base_url"].rstrip("/")
            or request.get("prompt_sha256") != contract.digest(prompt)
            or request.get("image_sha256") != store.identity(pair["sheet"]["path"])
            or not comparable_prompt.startswith(purpose_review.policy(manifest))
            or contract.digest(binding) not in prompt or observation != pair["observation"]):
        raise ValueError("HISTORICAL_EXPERIMENT_REQUEST_CHANGED")
    key = experiment_evidence.pair_key(manifest, samples, [m["window_frame_ids"] for m in pair["members"]])
    if not key:
        return 0
    experiment_evidence.remember_pair(manifest, key, pair)
    return 1


if __name__ == "__main__":
    main()
