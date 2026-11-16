"""Use stored evidence before requesting another pixel review."""
import json
from pathlib import Path

from .. import ai_editor_contract as contract
from ..runtime.file_safety import file_lock
from ..vision import evidence_store as store
from . import evidence_recall as recall


def lookup(manifest, sentence, shot, directory, call):
    def judge(row, observed, request):
        facts = row["facts"]
        prompt = ("只核对文字，不重新描述画面。判断这段已经逐帧核实的事实能否完整支持本句。"
                  "每个对象、动作、结果、程度、环境条件和数值必须都有明确依据。"
                  "旧口播只用来关联证据，不能作为新的视觉事实。区分包装标称和实际演示。"
                  "任何条件不明则列入missing，entire_sentence_supported=false。"
                  "返回JSON {entire_sentence_supported:bool,missing:[缺口],points:[{id,prior_point,"
                  "quote:逐字摘录观察,basis:原证据类型}]}。\n" +
                  json.dumps({"observed":observed,"basis":facts["basis"],"prior":facts.get("claims"),
                              "text":request["text"],"points":request["selling_points"]},ensure_ascii=False))
        try:
            return call(prompt, directory / "evidence_fit" / (contract.digest(prompt) + ".json"))
        except (OSError, ValueError):
            return {}
    result, hint = recall.inspect(manifest, sentence, shot, judge=judge)
    if result:
        store.record(manifest, "evidence_hit", {"shot":shot["shot_id"],"id":result["evidence_id"],
                     "basis":result["basis"], "points":result["supported_point_ids"]})
    return result, hint


def first_look(manifest, sentence, shot, directory, call, original, review, media):
    if not recall._eligible(manifest, sentence):
        return original(manifest, sentence, shot, directory)
    directory = Path(directory)
    key = contract.digest([store.scope(manifest),store.identity(shot["video"]),
                           shot["source_start"],shot["source_end"],store.protocol(manifest)])
    with file_lock(store.database().parent / "evidence-locks" / key, timeout=7200):
        found, hint = lookup(manifest, sentence, shot, directory, call)
        if found:
            return found
        if hint:
            row, missing = hint
            # Check the exact prior interval first. A negative applies only to
            # this window; a broader search remains available.
            a,b = row["start_us"]/1e6,row["end_us"]/1e6
            store.record(manifest, "window_gap", {"id":row["id"],"missing":missing or ["claim_fit"],
                                                  "start":a,"end":b})
            crop = {**shot,"source_start":a,"source_end":b, "_evidence_gap":{
                "window_us":[row["start_us"],row["end_us"]], "missing":missing or ["claim_fit"],
                "previous_observation":row["facts"].get("observation") or row["facts"].get("action")}}
            pixels = media(crop,directory,dense=True,fit=True)
            candidate = review(manifest,sentence,crop,directory,dense=True,prepared_media=pixels)
            if candidate.get("verdict") == "matched":
                return candidate
        return original(manifest,sentence,shot,directory)

