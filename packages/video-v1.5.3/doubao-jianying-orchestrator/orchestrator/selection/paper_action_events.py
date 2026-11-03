"""Observe paper actions in source-frame coordinates before exposing timing goals."""
from __future__ import annotations

import json
from pathlib import Path

from .. import ai_editor_contract as contract
from ..media import action_frames

EVENTS = {"onset", "first_rub", "repeated_rubbing", "unfold_begin", "unfold_complete", "next_action"}
PROTOCOL = "paper-action-source-events-v1"


def validate_events(row: dict, frames: list[dict]) -> dict:
    if not isinstance(row, dict) or row.get("verdict") not in {"observed", "uncertain", "not_applicable"}:
        raise ValueError("ACTION_EVENT_VERDICT_INVALID")
    if not isinstance(row.get("observation"), str) or not row["observation"].strip():
        raise ValueError("ACTION_EVENT_FACTS_REQUIRED")
    if type(row.get("dry_rubbing")) is not bool:
        raise ValueError("ACTION_EVENT_MATERIAL_STATE_REQUIRED")
    events = row.get("events")
    if not isinstance(events, list):
        raise ValueError("ACTION_EVENT_LIST_REQUIRED")
    allowed = {r["index"]: r["pts_us"] for r in frames}
    if not allowed or len(allowed) != len(frames):
        raise ValueError("ACTION_REVIEW_FRAME_SET_INVALID")
    seen = set()
    for event in events:
        if (not isinstance(event, dict) or event.get("name") not in EVENTS or event["name"] in seen
                or type(event.get("frame_lower")) is not int or type(event.get("frame_upper")) is not int
                or event["frame_lower"] not in allowed or event["frame_upper"] not in allowed
                or event["frame_lower"] > event["frame_upper"]
                or not isinstance(event.get("fact"), str) or not event["fact"].strip()):
            raise ValueError("ACTION_EVENT_FRAME_REFERENCE_INVALID")
        seen.add(event["name"])
    order = ["onset", "first_rub", "repeated_rubbing", "unfold_begin", "unfold_complete", "next_action"]
    observed = sorted(events, key=lambda e: order.index(e["name"]))
    if any(a["frame_lower"] > b["frame_upper"] for a, b in zip(observed, observed[1:])):
        raise ValueError("ACTION_EVENT_ORDER_REVERSED")
    return row


def review(manifest: dict, shot: dict, metadata: dict, frames: list[dict], directory: Path,
           *, scope: str) -> dict:
    """One bounded review; no narration duration, desired speed, or prior verdict enters the prompt."""
    from . import ai_editor
    from ..vision import vision_analyzer as vision, material_cache
    from ..runtime.runtime_policy import media_slot
    if not frames or len(frames) > 32:
        raise ValueError("ACTION_REVIEW_FRAME_COUNT_INVALID")
    before = action_frames.identity(shot["video"])
    if metadata["source_identity"] != before:
        raise ValueError("ACTION_REVIEW_SOURCE_STALE")
    tiles = [{"path": f["path"], "label": "F%d t=%.4fs" % (f["index"], f["pts_us"] / 1e6)} for f in frames]
    params = {"columns": 4, "tile_width": 270, "tile_height": 514, "fit": True, "font_size": 20}
    def prepare(target):
        with media_slot(timeout=600) as handle:
            if not handle.acquired:
                raise ValueError("ACTION_REVIEW_MEDIA_SLOT_TIMEOUT")
            return vision._write_sheet(tiles, target, **params)
    sheet = material_cache.cached_sheet(tiles, directory, prefix="action_events", parameters=params, prepare=prepare)
    if sheet is None:
        raise ValueError("ACTION_REVIEW_SHEET_FAILED")
    context = {"product": manifest.get("product"), "review_scope": scope,
               "frames": [{"index": f["index"], "pts_us": f["pts_us"]} for f in frames]}
    prompt = """你是纸品素材动作观察员，只记录本次图片里真实可见的动作和源帧边界。
此阶段不剪辑，不决定速度、时长或是否能放入配音。不要参考历史通过结论或素材标签。
按左到右、上到下顺序观察。标签F是原视频帧编号，t是对应真实源时间。
严格区分干纸揉搓、单纯折叠/展开、表面抚摸、湿纸挤水和包装操作；不能把这些动作互相替代。
只为本次可见的事件填边界：onset首次开始施力使纸变形；first_rub首次可辨摩擦；
repeated_rubbing已进入连续重复摩擦；unfold_begin结束摩擦开始展开；unfold_complete完整展开；next_action下一次新动作开始。
frame_lower/upper必须是本次实际提供的F编号。事件在两张图之间则用前后两张界定不确定范围；
单张已可确认事件状态，仍不能编造未采到的精确起点。未看到的事件不要填，不凭常识补全。
不要把手仍握住或纸仍折叠的画面说成完整展开，也不要因未见微小碎屑就保证零掉屑。
返回JSON：{"verdict":"observed|uncertain|not_applicable","dry_rubbing":true,
"observation":"本次可见事实与缺失过程","events":[{"name":"onset","frame_lower":12,"frame_upper":14,"fact":"可见变化"}]}。
""" + json.dumps(context, ensure_ascii=False)
    key = contract.digest([PROTOCOL, context, before, str(shot["shot_id"])])[:24]
    raw = ai_editor._call(prompt, Path(directory) / "reviews" / (key + ".json"), Path(sheet))
    row = validate_events(raw, frames)
    if before != action_frames.identity(shot["video"]):
        raise ValueError("ACTION_SOURCE_CHANGED_DURING_REVIEW")
    pts = {frame["index"]: frame["pts_us"] for frame in frames}
    grounded = [dict(event, source_interval_us={"start": pts[event["frame_lower"]], "end": pts[event["frame_upper"]]},
                     left_censored=event["frame_lower"] == frames[0]["index"],
                     right_censored=event["frame_upper"] == frames[-1]["index"])
                for event in row["events"]]
    return dict(row, events=grounded, source_identity=before, sheet=str(sheet), scope=scope,
                reviewed_frames=context["frames"], protocol=PROTOCOL)
