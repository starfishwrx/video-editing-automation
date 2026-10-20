"""Read saved drafts, render review copies, and export hash-bound local feedback.

This tool never edits Jianying JSON or writes an approval ledger. A render is
not desktop playback evidence and a feedback download is not certification.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import time

from tools.v154_pilot import canonical_hash, digest_file, read_json, write_json

US = 1_000_000
FPS = 30


def _frames(us: int) -> int:
    return round(int(us) * FPS / US)


def readback_plan(info: dict) -> dict:
    """Accept only the renderer's supported subset, reject gaps and transforms."""
    mats = info["materials"]
    videos = {m["id"]: m for m in mats.get("videos", [])}
    audios = {m["id"]: m for m in mats.get("audios", [])}
    texts = {m["id"]: m for m in mats.get("texts", [])}
    transitions = {m["id"] for m in mats.get("transitions", [])}
    tracks = [t for t in info["tracks"] if t.get("type") == "video"]
    if len(tracks) != 1:
        raise ValueError("Review render requires exactly one video track")
    plan = {"total_us": int(info["duration"]), "total_frames": _frames(info["duration"]),
            "video": [], "audio": [], "subtitles": [], "renderer": "saved_draft_readback"}
    cursor = 0
    for segment in sorted(tracks[0]["segments"], key=lambda s: s["target_timerange"]["start"]):
        target, source = segment["target_timerange"], segment["source_timerange"]
        f0, f1 = _frames(target["start"]), _frames(target["start"] + target["duration"])
        if f0 != cursor or f1 <= f0:
            raise ValueError("Video gap, overlap, or empty segment; no filler is allowed")
        cursor = f1
        material = videos[segment["material_id"]]
        clip = segment.get("clip") or {}
        scale, transform = clip.get("scale", {}), clip.get("transform", {})
        if (segment.get("reverse") or abs(float(clip.get("rotation", 0))) > 1e-6
                or abs(float(clip.get("alpha", 1)) - 1) > 1e-6
                or any(abs(float(scale.get(k, 1)) - 1) > 1e-6 for k in ("x", "y"))
                or any(abs(float(transform.get(k, 0))) > 1e-6 for k in ("x", "y"))
                or clip.get("flip", {}).get("vertical")
                or transitions.intersection(segment.get("extra_material_refs", []))):
            raise ValueError("Unsupported video transform/transition")
        expected_crop = {"upper_left_x": 0, "upper_left_y": 0, "upper_right_x": 1,
                         "upper_right_y": 0, "lower_left_x": 0, "lower_left_y": 1,
                         "lower_right_x": 1, "lower_right_y": 1}
        if any(abs(float(material.get("crop", {}).get(k, v)) - v) > 1e-6
               for k, v in expected_crop.items()):
            raise ValueError("Cropped video is unsupported by this review renderer")
        brightness = 0.0
        for keyframe in segment.get("common_keyframes", []):
            keys = keyframe.get("keyframe_list") or []
            if keyframe.get("property_type") != "KFTypeBrightness" or len(keys) != 1:
                raise ValueError("Animated or unsupported video keyframe")
            brightness = float(keys[0]["values"][0])
        speed = float(segment.get("speed", 1))
        if speed <= 0 or int(source["start"]) < 0:
            raise ValueError("Invalid source window/speed")
        if int(source["start"]) + int(source["duration"]) > int(material["duration"]) + 40000:
            raise ValueError("Source window exceeds material duration")
        plan["video"].append({"path": material["path"], "source_start_us": int(source["start"]),
                              "source_duration_us": int(source["duration"]),
                              "target_start_us": int(target["start"]),
                              "target_duration_us": int(target["duration"]),
                              "frames": f1 - f0, "speed": speed, "brightness": brightness,
                              "flip_h": bool(clip.get("flip", {}).get("horizontal"))})
    if cursor != plan["total_frames"]:
        raise ValueError("Video does not cover the actual draft duration")
    for track in info["tracks"]:
        for segment in track.get("segments", []):
            kind = track.get("type")
            if kind == "audio":
                source, target = segment["source_timerange"], segment["target_timerange"]
                if (int(target["start"]) < 0 or int(target["duration"]) <= 0
                        or int(target["start"]) + int(target["duration"]) > plan["total_us"] + 1):
                    raise ValueError("Audio is outside the saved video timeline")
                plan["audio"].append({"path": audios[segment["material_id"]]["path"],
                    "source_start_us": int(source["start"]),
                    "source_duration_us": int(source["duration"]),
                    "target_start_us": int(target["start"]),
                    "target_duration_us": int(target["duration"]),
                    "speed": float(segment.get("speed", 1)),
                    "volume": float(segment.get("volume", 1)), "track": track.get("name")})
            elif kind == "text":
                payload = texts[segment["material_id"]].get("content", "")
                content = json.loads(payload) if isinstance(payload, str) else payload
                target = segment["target_timerange"]
                if (int(target["start"]) < 0 or int(target["duration"]) <= 0
                        or int(target["start"]) + int(target["duration"]) > plan["total_us"] + 1):
                    raise ValueError("Subtitle is outside the saved video timeline")
                transform = (segment.get("clip") or {}).get("transform") or {}
                plan["subtitles"].append({"text": content.get("text", ""),
                    "start_us": int(target["start"]), "duration_us": int(target["duration"]),
                    "x": float(transform.get("x", 0)), "y": float(transform.get("y", -0.55))})
    return plan


def render_groups(plan: dict) -> list[dict]:
    """Resample a continuous shot once, independent of its subtitle boundaries.

    Saved sentence windows use rounded global frame boundaries. Resetting the
    FPS phase for every sentence can ask for 41 frames from a 40.368-frame
    duration even though the following sentence continues the same source.
    Joining only identical contiguous video settings removes that artificial
    decode boundary; saved subtitles and every original audio row stay intact.
    """
    groups = []
    for index, original in enumerate(plan["video"]):
        row = copy.deepcopy(original)
        row["video_indices"] = [index]
        previous = groups[-1] if groups else None
        if previous is not None:
            source_end = previous["source_start_us"] + previous["source_duration_us"]
            target_end = previous["target_start_us"] + previous["target_duration_us"]
            same = (row["path"] == previous["path"]
                    and abs(row["speed"] - previous["speed"]) <= 1e-12
                    and row["brightness"] == previous["brightness"]
                    and row["flip_h"] == previous["flip_h"]
                    and abs(row["source_start_us"] - source_end) <= 1
                    and abs(row["target_start_us"] - target_end) <= 1)
            if same:
                previous["source_duration_us"] = (row["source_start_us"] + row["source_duration_us"]
                                                   - previous["source_start_us"])
                previous["target_duration_us"] = (row["target_start_us"] + row["target_duration_us"]
                                                   - previous["target_start_us"])
                previous["frames"] += row["frames"]
                previous["video_indices"].append(index)
                continue
        groups.append(row)
    for row in groups:
        expected = (_frames(row["target_start_us"] + row["target_duration_us"])
                    - _frames(row["target_start_us"]))
        if row["frames"] != expected:
            raise ValueError("Merged review window changed global frame coverage")
    if sum(row["frames"] for row in groups) != plan["total_frames"]:
        raise ValueError("Merged review windows do not cover the actual draft duration")
    return groups


def _ass_time(us: int) -> str:
    cs = round(us / 10000)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def _ass(plan: dict, path: Path) -> None:
    lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280",
             "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, BorderStyle, Outline, Alignment, MarginL, MarginR, MarginV",
             "Style: Default,Microsoft YaHei,44,&H00FFFFFF,&H00000000,-1,1,2,5,25,25,0",
             "[Events]", "Format: Layer, Start, End, Style, Text"]
    for sub in plan["subtitles"]:
        text = sub["text"].replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")
        x, y = round(360 + sub["x"] * 360), round(640 - sub["y"] * 640)
        lines.append(f"Dialogue: 0,{_ass_time(sub['start_us'])},{_ass_time(sub['start_us'] + sub['duration_us'])},Default,{{\\pos({x},{y})}}{text}")
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def render_saved_draft(draft: Path, output: Path, package_root: Path) -> dict:
    if output.exists():
        raise FileExistsError(output)
    info_path = draft / "draft_info.json"
    info = read_json(info_path)
    content_path = draft / "draft_content.json"
    if content_path.is_file() and info != read_json(content_path):
        raise ValueError("Saved draft_info/draft_content differ")
    plan = readback_plan(info)
    for row in plan["video"] + plan["audio"]:
        if not Path(row["path"]).is_file():
            raise FileNotFoundError(row["path"])
    sys.path.insert(0, str(package_root / "doubao-jianying-orchestrator"))
    from orchestrator import video_decode, video_encode
    from orchestrator.platform_env import find_ffmpeg
    ffmpeg, ffprobe = find_ffmpeg()[:2]
    result = subprocess.run([str(ffmpeg), "-hide_banner", "-filters"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=20)
    if " subtitles " not in result.stdout:
        alternate = shutil.which("ffmpeg")
        if not alternate:
            raise RuntimeError("A libass-enabled FFmpeg is required")
        ffmpeg = alternate
    work = output.parent / (output.stem + "_render")
    attempt = 1
    while True:
        try:
            work.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            attempt += 1
            work = output.parent / f"{output.stem}_render_{attempt}"
    os.environ["JY_GPU_CALL_LOG"] = str(work / "gpu_calls.jsonl")
    write_json(work / "readback_timeline.json", plan)
    groups = render_groups(plan)
    write_json(work / "render_groups.json", {"groups": groups, "source_video_segments": len(plan["video"]),
               "total_frames": plan["total_frames"], "audio_rows_preserved": len(plan["audio"]),
               "subtitle_rows_preserved": len(plan["subtitles"]),
               "method": "one resample per identical contiguous source/speed/effects window"})
    _ass(plan, work / "captions.ass")
    started, clips = time.monotonic(), []
    for index, row in enumerate(groups):
        clip = work / f"video_{index:03}.mkv"
        clips.append(clip)
        filters = [f"setpts=(PTS-STARTPTS)/{row['speed']:.9f}", "fps=30",
                   "scale=720:1280:force_original_aspect_ratio=decrease",
                   "pad=720:1280:(ow-iw)/2:(oh-ih)/2:black", "setsar=1"]
        if row["flip_h"]:
            filters.append("hflip")
        if row["brightness"]:
            filters.append(f"eq=brightness={row['brightness'] * 0.5:.6f}")
        filters.append("format=yuv420p")
        command = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-ss",
                   str(row["source_start_us"] / US), "-t", str(row["source_duration_us"] / US),
                   "-i", row["path"], "-an", "-vf", ",".join(filters), "-frames:v",
                   str(row["frames"]), "-c:v", "ffv1", "-threads", "2", str(clip)]
        proc = video_decode.run_ffmpeg_decode(command, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=180)
        (work / f"decode_{index:03}.log").write_text(proc.stderr or "", encoding="utf-8")
        if proc.returncode:
            raise RuntimeError((proc.stderr or "")[-1000:])
        probe = subprocess.run([str(ffprobe), "-v", "error", "-count_frames", "-select_streams",
            "v:0", "-show_entries", "stream=nb_read_frames", "-of", "json", str(clip)],
            capture_output=True, text=True, timeout=30, check=True)
        if int(json.loads(probe.stdout)["streams"][0]["nb_read_frames"]) != row["frames"]:
            raise ValueError("Review source ended early; refuse tail freeze/filler")
    concat = work / "videos.txt"
    concat.write_text("\n".join("file '" + str(p).replace("\\", "/").replace("'", "'\\''") + "'"
                                 for p in clips), encoding="utf-8")
    leading = ["-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(concat)]
    filters, labels = [], []
    for index, row in enumerate(plan["audio"], 1):
        leading += ["-ss", str(row["source_start_us"] / US), "-t",
                    str(row["source_duration_us"] / US), "-i", row["path"]]
        chain = [f"[{index}:a]aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        if abs(row["speed"] - 1) > 1e-6:
            chain.append(f"atempo={row['speed']:.9f}")
        chain += [f"volume={row['volume']:.6f}", f"adelay={round(row['target_start_us'] / 1000)}:all=1"]
        label = f"a{index}"
        filters.append(",".join(chain) + f"[{label}]")
        labels.append(label)
    if not labels:
        raise ValueError("Review draft has no real audio")
    total = plan["total_frames"] / FPS
    filters.append("".join(f"[{label}]" for label in labels)
        + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,apad,atrim=end={total:.9f}[aout]")
    ass_path = str(work / "captions.ass").replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    filters.append(f"[0:v]subtitles=filename='{ass_path}',format=yuv420p[vout]")
    leading += ["-filter_complex_threads", "2", "-filter_complex", ";".join(filters),
                "-map", "[vout]", "-map", "[aout]"]
    encoder, proc = video_encode.run_h264_encode(str(ffmpeg), leading, output,
        trailing=["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-r", "30", "-frames:v",
                  str(plan["total_frames"]), "-t", str(total)], timeout=300)
    if proc.returncode:
        raise RuntimeError((proc.stderr or "")[-1000:])
    receipt = {"schema": "v154-review-render-v1", "draft_file": str(info_path),
               "draft_sha256": digest_file(info_path), "plan_sha256": canonical_hash(plan),
               "output": str(output), "output_sha256": digest_file(output),
               "encoder": encoder, "elapsed_s": time.monotonic() - started,
               "render_work_dir": str(work), "render_group_count": len(groups),
               "source_video_segments": len(plan["video"]),
               "resolution": [720, 1280], "duration_s": total,
               "scope": "Review rendering of saved draft tracks; not Jianying desktop acceptance",
               "human_accepted": False, "production_deployed": False}
    write_json(work / "render_receipt.json", receipt)
    return receipt


def review_document(manifest: dict, *, arms: list[dict], cases: list[dict] | None = None) -> dict:
    rows = []
    for arm in arms:
        plan = arm.get("plan", {})
        rows.append({**arm, "plan_sha256": canonical_hash(plan),
                     "source_sha256s": sorted(set(arm.get("source_sha256s", [])))})
    result = {"schema": "v154-review-document-v1", "title": manifest.get("title", ""),
              "script": manifest.get("full_script", ""), "arms": rows, "cases": cases or [],
              "human_review_status": "PENDING", "production_deployed": False}
    result["review_identity_sha256"] = canonical_hash(result)
    return result


def collect_review(manifest_path: Path, arm_roots: list[Path], *, cases_path: Path | None = None) -> dict:
    """Collect current arms; missing/blocked builds remain visibly incomplete."""
    arms = []
    for root in arm_roots:
        result_path = root / "report" / "last_result.json"
        result = read_json(result_path) if result_path.is_file() else {}
        if not result and (root / "execution_receipt.json").is_file():
            execution = read_json(root / "execution_receipt.json")
            result = {"status": "BUILD_FAILED_NO_TERMINAL_RESULT", "execution": execution}
            if (root / "operator_stop_receipt.json").is_file():
                result["operator_stop"] = read_json(root / "operator_stop_receipt.json")
        manifest = read_json(root / "manifest.json")
        draft = root / "drafts" / manifest["draft_name"]
        actual_plan, source_hashes = {}, []
        draft_file = draft / "draft_info.json"
        if draft_file.is_file():
            actual_plan["actual_saved_timeline"] = readback_plan(read_json(draft_file))
            actual_plan["draft_sha256"] = digest_file(draft_file)
            for row in actual_plan["actual_saved_timeline"]["video"]:
                if not Path(row["path"]).is_file():
                    raise FileNotFoundError(row["path"])
                source_hashes.append(digest_file(row["path"]))
        for name in ("contextual_plan.json", "contextual_plan_report.json", "shot_match_report.json"):
            path = root / "report" / name
            if path.is_file():
                actual_plan[name] = read_json(path)
        video = root / "review.mp4"
        arms.append({"name": root.name, "status": result.get("status", "NOT_RUN"),
                     "video": video.as_uri() if video.is_file() else None,
                     "plan": actual_plan, "source_sha256s": source_hashes,
                     "result": result, "human_accepted": False})
    cases = read_json(cases_path) if cases_path else []
    if isinstance(cases, dict):
        cases = cases.get("cases", [])
    return review_document(read_json(manifest_path), arms=arms, cases=cases)


def validate_feedback(document: dict, feedback: dict) -> None:
    if feedback.get("review_identity_sha256") != document["review_identity_sha256"]:
        raise ValueError("Feedback belongs to a different review")
    arms = {a["name"]: a for a in document["arms"]}
    for row in feedback.get("items", []):
        arm = arms.get(row.get("arm"))
        if not arm or row.get("plan_sha256") != arm["plan_sha256"]:
            raise ValueError("Feedback plan identity mismatch")
        if sorted(row.get("source_sha256s", [])) != arm["source_sha256s"]:
            raise ValueError("Feedback source identity mismatch")
        if row.get("verdict") not in ("pending", "accept", "reject"):
            raise ValueError("Unknown feedback verdict")
    cases = {c["case_id"]: c for c in document.get("cases", [])}
    for row in feedback.get("cases", []):
        case = cases.get(row.get("case_id"))
        if (not case or row.get("source_sha256") != case.get("source_sha256")
                or row.get("case_identity_sha256") != case.get("case_identity_sha256")):
            raise ValueError("Feedback case/source identity mismatch")
        if row.get("label") not in ("pending", "DIRECT", "REJECT", "UNCERTAIN"):
            raise ValueError("Unknown case feedback label")


def write_review(document: dict, target: Path) -> None:
    # Do not interpolate JSON into executable script text: user captions can
    # contain a closing script tag. JSON is escaped even in the inert data block.
    payload = json.dumps(document, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    title = html.escape(str(document.get("title", "")))
    page = r"""<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>本地画面审核</title><style>body{font:16px system-ui;background:#f5f6f8;color:#18202b;margin:24px}h1{font-size:23px}article,section{background:white;border-radius:12px;padding:18px;margin:16px 0}video{width:min(100%,400px);max-height:600px}pre{white-space:pre-wrap;overflow-wrap:anywhere}textarea{display:block;width:95%;min-height:70px}button,select{padding:8px;margin:8px 8px 8px 0}.arms{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}.muted{color:#667}details{margin:12px 0}</style>
<h1>__TITLE__</h1><p>本地测试审核。尚未人工接受，未部署生产。导出反馈不会修改正式认证台账。</p><section id="script"></section><div class="arms" id="arms"></div><section><h2>真实案例</h2><p>历史模型观察与人工真值分开；待确认案例不计入已验收准确率。</p><div id="cases"></div></section><button id="download">导出反馈 JSON</button>
<script type="application/json" id="data">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent);
const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n};
document.getElementById('script').append(el('h2','口播'),el('pre',data.script));
for(const arm of data.arms){const card=el('article');card.dataset.arm=arm.name;card.append(el('h2',arm.name),el('p','状态：'+(arm.status||'PENDING')));if(arm.video){const v=el('video');v.controls=true;v.preload='metadata';v.src=arm.video;card.append(v)}else card.append(el('p','尚无成片；检查构建结果，不能以历史片代替。'));
const timeline=arm.plan.actual_saved_timeline;if(timeline){for(const [i,c]of timeline.video.entries()){const item=el('details');const words=timeline.subtitles.filter(s=>s.start_us<c.target_start_us+c.target_duration_us&&s.start_us+s.duration_us>c.target_start_us).map(s=>s.text).join(' / ');item.append(el('summary',(i+1)+' · '+words));item.append(el('p','源区间 '+(c.source_start_us/1e6).toFixed(3)+'–'+((c.source_start_us+c.source_duration_us)/1e6).toFixed(3)+' 秒；画面 '+(c.target_start_us/1e6).toFixed(3)+'–'+((c.target_start_us+c.target_duration_us)/1e6).toFixed(3)+' 秒；倍速 '+c.speed));const raw=el('video');raw.controls=true;raw.preload='none';const uri=c.path.startsWith('\\\\')?'file:'+c.path.replaceAll('\\','/'):'file:///'+c.path.replaceAll('\\','/');raw.src=uri+'#t='+(c.source_start_us/1e6)+','+((c.source_start_us+c.source_duration_us)/1e6);item.append(raw,el('p',c.path));card.append(item)}}
const details=el('details');details.append(el('summary','实际切片、源区间和选镜理由'),el('pre',JSON.stringify(arm.plan,null,2)));card.append(details);const select=el('select');for(const [value,label]of [['pending','待审核'],['accept','接受'],['reject','需返修']]){const o=el('option',label);o.value=value;select.append(o)}card.append(select);const note=el('textarea');note.placeholder='具体句组、错配、动作截断或节奏问题';card.append(note);document.getElementById('arms').append(card)}
for(const item of data.cases){const d=el('details');d.dataset.caseId=item.case_id;d.append(el('summary',item.case_id+' · '+item.query+' · '+item.expected+' · '+item.truth_status));if(item.source){const v=el('video');v.controls=true;v.preload='none';v.src=(item.source.startsWith('\\\\')?'file:':'file:///')+item.source.replaceAll('\\','/');d.append(v)}d.append(el('pre',JSON.stringify(item,null,2)));const label=el('select');for(const [value,text]of [['pending','尚未判定'],['DIRECT','可直接支持此要求'],['REJECT','不支持此要求'],['UNCERTAIN','仍缺证据']]){const o=el('option',text);o.value=value;label.append(o)}const note=el('textarea');note.placeholder='记录实际动作起止、观察到的反例及证据';d.append(label,note);document.getElementById('cases').append(d)}
document.getElementById('download').onclick=()=>{const items=[...document.querySelectorAll('article')].map(n=>{const a=data.arms.find(a=>a.name===n.dataset.arm);return{arm:a.name,plan_sha256:a.plan_sha256,source_sha256s:a.source_sha256s,verdict:n.querySelector('select').value,note:n.querySelector('textarea').value}});const cases=[...document.querySelectorAll('[data-case-id]')].map(n=>{const c=data.cases.find(c=>c.case_id===n.dataset.caseId);return{case_id:c.case_id,source_sha256:c.source_sha256,case_identity_sha256:c.case_identity_sha256,label:n.querySelector('select').value,note:n.querySelector('textarea').value}});const f={schema:'v154-local-feedback-v1',review_identity_sha256:data.review_identity_sha256,created_at:new Date().toISOString(),items,cases,formal_certification:false};const u=URL.createObjectURL(new Blob([JSON.stringify(f,null,2)],{type:'application/json'}));const a=el('a');a.href=u;a.download='v154_feedback.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)};
</script></html>"""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page.replace("__TITLE__", title).replace("__DATA__", payload), encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    render = commands.add_parser("render")
    render.add_argument("--draft", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--package-root", type=Path, required=True)
    review = commands.add_parser("html")
    review.add_argument("--input", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    feedback = commands.add_parser("validate-feedback")
    feedback.add_argument("--review", type=Path, required=True)
    feedback.add_argument("--feedback", type=Path, required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--manifest", type=Path, required=True)
    collect.add_argument("--arm-root", type=Path, action="append", required=True)
    collect.add_argument("--cases", type=Path)
    collect.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == "render":
        print(json.dumps(render_saved_draft(args.draft, args.output, args.package_root), ensure_ascii=False))
    elif args.action == "html":
        write_review(read_json(args.input), args.output)
    elif args.action == "collect":
        document = collect_review(args.manifest, args.arm_root, cases_path=args.cases)
        write_json(args.output, document)
        write_review(document, args.output.with_suffix(".html"))
    else:
        validate_feedback(read_json(args.review), read_json(args.feedback))
        print("Feedback identity verified; no formal approval ledger was changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
