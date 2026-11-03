"""Observe complete experiments without narration-length pressure."""
import json
from pathlib import Path

from .. import ai_editor_contract as contract
from ..selection.experiment_contract import contract_for, validate_observation, validate_roles
from . import temporal_video
from .purpose_review import policy, save
from ..selection import ai_editor
from ..media.temporal_samples import validate as validate_samples, guard_outputs, guard_paths

PROTOCOL = "weipin-single-original-experiment-v1"


def classify_sentences(manifest, timeline, output):
    guard_paths([a.path for a in timeline.assets],output,(".prompt.txt",".request.json"))
    rules = contract_for(manifest)
    rows = [{"sentence_id": s.sentence_id, "text": s.text} for s in timeline.sentences]
    prompt = policy(manifest) + """只分类原文用途，不判断素材，不改变句长。为每句标一个role：
absorption=吸收/锁水；rewet=反渗；followup=同实验结果的干爽总结；bridge=无独立卖点的承接；
breathability=透气/不闷热；fluorescence=荧光；sku_change=商品切换；promotion=促销/数量；
other=其他独立主题，uncertain=不能确定。体验句仍按实际主题分类，不当作通用bridge。
输出JSON {"sentences":[{"sentence_id":"原ID","text":"原文","role":"上述枚举"}]}。
""" + json.dumps(rows, ensure_ascii=False)
    result = ai_editor._call(prompt, Path(output))
    return validate_roles(timeline, result.get("sentences"), rules)


def observe(manifest, original, samples, output):
    rules = contract_for(manifest)
    before = contract.source_identity(original)
    validate_samples(samples)
    if before != samples["original"]:
        raise ValueError("EXPERIMENT_ORIGINAL_PROXY_MISMATCH")
    output = Path(output)
    guard_outputs(samples,output,(".input.json",".call.json",".video",".evidence.json"))
    allowed = {str(Path(p).resolve()) for p in manifest["material_sources"]}
    if before["path"] not in allowed:
        raise ValueError("EXPERIMENT_SOURCE_OUTSIDE_TASK")
    if samples["source"] != contract.source_identity(samples["source"]["path"]) or samples["video"] != contract.source_identity(samples["video"]["path"]):
        raise ValueError("EXPERIMENT_SAMPLE_MEDIA_CHANGED")
    context = {"product": manifest["product"], "valid_frame_ids": [0, len(samples["frames"])-1],
               "required_phases": rules["phases"]}
    prompt = policy(manifest) + """完整观察本次视频。每帧左上角frame N是该真实采样帧的编号。
只使用实际看到的编号，不猜秒数。判断同一条素材、同一片巾体是否连续完成同一次实验。
独立列出倒液pour、实际吸收absorb、纸巾接触按压press、提起翻转lift、清楚纸巾结果result。
吸收可与倒液重叠；结果必须在提起之后。不能从已染蓝的巾体补全之前倒液，不能把硅胶球当纸巾。
看到白色纸巾仅描述有没有明显湿痕，不外推完全无水。不同实验或剪接无法确认时保留uncertain。
不存在的阶段不填；不要为凑齐5阶段编造。没有完整实验时incomplete，信息不足时uncertain。
只输出JSON {"verdict":"complete|incomplete|uncertain","same_product":true,
"continuous_specimen":false,"no_splice":true,"observed":"实际事实与缺失",
"phases":[{"kind":"pour|absorb|press|lift|result","first_frame":0,"last_frame":1,"observed":"实际证据"}]}。
""" + json.dumps(context, ensure_ascii=False)
    result = temporal_video.review(samples["video"]["path"], prompt, output, fps=10,
        validator=lambda row: bool(validate_observation(row, samples["frames"], rules["phases"])))
    validate_samples(samples)
    artifact = {"protocol": PROTOCOL, "task_id": manifest["task_id"], "rules": contract.rules_identity(manifest),
        "context": {"product":manifest["product"],"category":contract.rules_identity(manifest)["category"]},
        "original": before, "samples": samples, "observation": result,
        "missing_phases": [p for p in rules["phases"] if p not in {r["kind"] for r in result["phases"]}],
        "formal_delivery_allowed": False, "method": "requested10fps native video; local frame IDs, not full-frame acceptance"}
    save(Path(output).with_suffix(".evidence.json"), artifact)
    return artifact
