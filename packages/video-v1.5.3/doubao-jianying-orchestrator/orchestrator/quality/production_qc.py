"""Production checks have three independent outcomes; media validity is not content PASS."""
import json
from pathlib import Path
import subprocess
import unicodedata

from ..domain.timeline import EditTimeline, TimelineError
from ..media.action_frames import identity
from ..media.timeline_render import timeline_identity, verify_video, _wav_samples
from fractions import Fraction


def _caption(value):
    return "".join(c for c in value if not c.isspace() and not unicodedata.category(c).startswith("P"))


def check_production(timeline, render_dir, *, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    root, issues = Path(render_dir).resolve(), []
    try:
        timeline.validate()
        receipt = json.loads((root / "render_receipt.json").read_text(encoding="utf-8"))
        saved = EditTimeline.load(root / "timeline.json")
        if timeline_identity(saved) != timeline_identity(timeline) or receipt.get("timeline_identity") != timeline_identity(timeline):
            raise TimelineError("RENDER_REVISION_OR_INPUT_MISMATCH")
        if receipt.get("status") != "rendered":
            raise TimelineError("RENDER_NOT_COMPLETE")
        video = root / "preview.mp4"
        if receipt.get("video") != str(video) or receipt.get("output_identity") != identity(video):
            raise TimelineError("RENDER_OUTPUT_CHANGED_OR_MISSING")
        for asset in timeline.assets:
            if receipt.get("assets", {}).get(asset.asset_id) != identity(asset.path):
                raise TimelineError("SOURCE_CHANGED_AFTER_RENDER")
        if not receipt.get("audio_identities"):
            raise TimelineError("RENDER_AUDIO_EVIDENCE_MISSING")
        for path, expected in receipt["audio_identities"].items():
            if Path(path).resolve().parent != root or identity(path) != expected:
                raise TimelineError("RENDER_AUDIO_CHANGED_OR_MISSING")
            if _wav_samples(path) != receipt["audio"]["samples"]:
                raise TimelineError("RENDER_AUDIO_SAMPLES_CHANGED")
        verify_video(video, expected_frames=timeline.frame_count,
                     fps=Fraction(timeline.canvas.fps_num, timeline.canvas.fps_den), ffmpeg=ffmpeg, ffprobe=ffprobe)
        by_sentence = {}
        for cue in sorted(timeline.subtitles, key=lambda c: c.start_us):
            by_sentence.setdefault(cue.sentence_id, []).append(cue.text)
        for sid, parts in by_sentence.items():
            if _caption("".join(parts)) != _caption(timeline.sentence_map()[sid].text):
                raise TimelineError("SUBTITLE_NARRATION_TEXT_MISMATCH:" + sid)
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as exc:
        issues.append(str(exc))
    content_issues = list(timeline.unresolved) if isinstance(timeline.unresolved, (list, tuple)) else []
    report = {"schema": "production-qc-v1", "task_id": timeline.task_id, "revision": timeline.revision,
        "status": "BLOCKED" if issues or content_issues else "UNCERTIFIED", "formal_delivery_allowed": False,
        "structure_media": {"status": "FAIL" if issues else "PASS", "issues": issues},
        "content_semantics": {"status": "BLOCKED" if content_issues else "NOT_RUN",
            "reason": "UNRESOLVED_TIMELINE_REQUIREMENTS" if content_issues else "INDEPENDENT_CONTENT_REVIEW_REQUIRED",
            "issues": content_issues},
        "viewing_quality": {"status": "NOT_RUN", "reason": "ACTION_RHYTHM_SUBTITLE_AND_MIX_REVIEW_REQUIRED"},
        "export_fidelity": {"status": "NOT_RUN", "reason": "JIAN_YING_EXPORT_NOT_IMPLEMENTED"}}
    root.mkdir(parents=True, exist_ok=True)
    (root / "production_qc.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
