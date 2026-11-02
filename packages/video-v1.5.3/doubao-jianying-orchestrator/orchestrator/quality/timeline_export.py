"""Exact readback of new headless documents; never certifies viewing quality."""
import json
import math
from pathlib import Path


def compare_headless_documents(timeline, plan, document, metadata, record):
    issues = []
    def require(ok, message):
        if not ok:
            issues.append(message)
    require(document.get("duration") == timeline.duration_us, "EXPORT_DURATION_MISMATCH")
    require(document.get("fps") == timeline.canvas.fps_num / timeline.canvas.fps_den, "EXPORT_FPS_MISMATCH")
    require(document.get("canvas_config") == {"width": timeline.canvas.width, "height": timeline.canvas.height},
            "EXPORT_CANVAS_MISMATCH")
    target = Path(record["target"])
    require(metadata.get("draft_fold_path") == str(target), "EXPORT_REGISTRATION_PATH_MISMATCH")
    require(metadata.get("draft_root_path") == str(target.parent), "EXPORT_REGISTRATION_ROOT_MISMATCH")
    materials = {m["id"]: m for group in document["materials"].values() if isinstance(group, list)
                 for m in group if isinstance(m, dict) and "id" in m}
    actual_tracks = document.get("tracks", [])
    require(len(actual_tracks) == len(plan["tracks"]), "EXPORT_TRACK_COUNT_MISMATCH")
    assets = {str(Path(a["source"]).resolve()): a for a in record["assets"]}
    for actual, expected in zip(actual_tracks, plan["tracks"]):
        require(actual.get("name") == expected["name"] and actual.get("type") == expected["type"], "EXPORT_TRACK_MISMATCH")
        segments = actual.get("segments", [])
        require(len(segments) == len(expected["segments"]), "EXPORT_SEGMENT_COUNT_MISMATCH")
        for index, (seg, spec) in enumerate(zip(segments, expected["segments"])):
            label = expected["name"] + ":" + str(index)
            require(seg.get("target_timerange") == {"start": spec["start_us"], "duration": spec["duration_us"]},
                    "EXPORT_TARGET_MISMATCH:" + label)
            mat = materials.get(seg.get("material_id"), {})
            if expected["type"] == "text":
                text = json.loads(mat.get("content", "{}"))
                require(text.get("text") == spec["text"], "EXPORT_SUBTITLE_TEXT_MISMATCH:" + label)
                require(seg.get("clip", {}).get("transform") == {"x": spec["x"], "y": spec["y"]},
                        "EXPORT_SUBTITLE_POSITION_MISMATCH:" + label)
                require(bool(text.get("styles")) and all(s.get("size") == spec["size"] for s in text["styles"]),
                        "EXPORT_SUBTITLE_SIZE_MISMATCH:" + label)
                require(bool(text.get("styles")) and all(s.get("bold") is spec["bold"] for s in text["styles"]),
                        "EXPORT_SUBTITLE_BOLD_MISMATCH:" + label)
                continue
            asset = assets.get(str(Path(spec["source"]).resolve()))
            require(asset is not None, "EXPORT_ASSET_MISSING:" + label)
            if asset:
                require(mat.get("path") == str(target / asset["relative"]), "EXPORT_MEDIA_PATH_MISMATCH:" + label)
            require(seg.get("source_timerange") == {"start": spec["source_start_us"], "duration": spec["source_duration_us"]},
                    "EXPORT_SOURCE_WINDOW_MISMATCH:" + label)
            require(math.isclose(seg.get("speed", -1), spec["speed"], abs_tol=1e-9), "EXPORT_SPEED_MISMATCH:" + label)
            require(math.isclose(seg.get("volume", -1), spec["volume"], abs_tol=1e-9), "EXPORT_GAIN_MISMATCH:" + label)
            speed_nodes = [materials.get(ref, {}) for ref in seg.get("extra_material_refs", [])
                           if materials.get(ref, {}).get("type") == "speed"]
            require(len(speed_nodes) == 1 and math.isclose(speed_nodes[0].get("speed", -1), spec["speed"], abs_tol=1e-9),
                    "EXPORT_SPEED_MATERIAL_MISMATCH:" + label)
    return {"status": "READBACK_FAILED" if issues else "PARAMETERS_VERIFIED", "issues": issues,
        "duration_us": timeline.duration_us, "video_events": len(timeline.videos),
        "audio_events": len(timeline.audios), "subtitle_events": len(timeline.subtitles),
        "formal_delivery_allowed": False, "native_visual_acceptance": "pending"}
