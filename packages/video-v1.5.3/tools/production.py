"""Explicit fixed-selection production and review export; never starts the business queue."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "doubao-jianying-orchestrator"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "preview"):
        p = sub.add_parser(command, help="从固定选镜和已准备配音生成独立时间线" if command == "prepare" else "准备时间线并渲染工作预览")
        p.add_argument("--input", required=True, type=Path)
        p.add_argument("--report-dir", required=True, type=Path)
        p.add_argument("--revision", type=int, default=1)
        if command == "preview":
            p.add_argument("--width", type=int)
    r = sub.add_parser("render", help="直接渲染 timeline.json，不需要草稿")
    r.add_argument("--timeline", type=Path, required=True)
    r.add_argument("--output", type=Path, required=True)
    r.add_argument("--width", type=int)
    q = sub.add_parser("check", help="检查当前渲染；内容及观看效果保持待审")
    q.add_argument("--timeline", type=Path, required=True)
    q.add_argument("--render-dir", type=Path, required=True)
    e = sub.add_parser("headless-plan", help="转换为无头剪映待审计划，不授予正式交付许可")
    e.add_argument("--timeline", type=Path, required=True)
    e.add_argument("--render-dir", type=Path, required=True)
    e.add_argument("--name", required=True)
    e.add_argument("--report-dir", type=Path, required=True)
    e.add_argument("--font", type=Path, required=True)
    e.add_argument("--task-json", type=Path, required=True, help="业务任务：先按worker空BGM自动选曲规则补齐音乐")
    e.add_argument("--bgm-state-dir", type=Path, required=True, help="任务选曲复用/批次去重目录")
    e.add_argument("--review-only", action="store_true", required=True)
    for child in sub.choices.values():
        child.add_argument("--ffmpeg")
        child.add_argument("--ffprobe")
    args = parser.parse_args(argv)
    from orchestrator.application.production import prepare_timeline
    from orchestrator.domain.timeline import EditTimeline
    from orchestrator.media.timeline_render import render_timeline
    from orchestrator.quality.production_qc import check_production
    from orchestrator.runtime.production_env import production_environment
    try:
        env = production_environment(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
        if not env["ok"]:
            raise ValueError("PRODUCTION_ENVIRONMENT_BLOCKED:" + ",".join(env["issues"]))
        tools = env["tools"]
        if args.command in {"prepare", "preview"}:
            manifest = json.loads(args.input.read_text(encoding="utf-8-sig"))
            timeline = prepare_timeline(manifest, args.input.resolve().parent, args.report_dir,
                                        revision=args.revision, **tools)
            result = {"status": "planned", "timeline": str((args.report_dir / "timeline.json").resolve()),
                      "formal_delivery_allowed": False}
        else:
            timeline = EditTimeline.load(args.timeline)
        if args.command in {"render", "preview"}:
            output = args.output if args.command == "render" else args.report_dir / "render"
            receipt = render_timeline(timeline, output, width=args.width, **tools)
            result = {"status": "review_rendered", "video": receipt["video"], "formal_delivery_allowed": False}
            result["qc"] = check_production(timeline, output, **tools)
        elif args.command == "check":
            result = check_production(timeline, args.render_dir, **tools)
        elif args.command == "headless-plan":
            from orchestrator.delivery.jianying_export import prepare_headless_export, headless_plan
            headless_plan(timeline, args.name, args.font, review_only=args.review_only)
            export_report, render_dir = args.report_dir, args.render_dir
            if args.task_json:
                if not args.bgm_state_dir:
                    raise ValueError("BUSINESS_BGM_STATE_DIRECTORY_REQUIRED")
                from tools.queueing.contracts import meeting_bgm_spec
                from orchestrator.application.production import prepare_headless_task
                task = json.loads(args.task_json.read_text(encoding="utf-8-sig"))
                task["bgm"] = meeting_bgm_spec(task.get("bgm"))
                timeline, render_dir = prepare_headless_task(timeline, task,
                    render_dir=args.render_dir, report_dir=args.report_dir,
                    state_dir=args.bgm_state_dir, **tools)
                export_report = args.report_dir / "export"
            result = prepare_headless_export(timeline, render_dir=render_dir,
                name=args.name, report_dir=export_report, font_path=args.font,
                review_only=args.review_only, **tools)
            result["prepared_timeline"] = str(render_dir.parent / "timeline.json") if args.task_json else str(args.timeline)
            result["bgm_completion"] = "TASK_BGM_PREPARED" if args.task_json else "PREPARED_TIMELINE_ONLY"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "BLOCKED" or result.get("qc", {}).get("status") == "BLOCKED" else 0
    except (OSError, ValueError, TypeError, KeyError, ZeroDivisionError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc), "formal_delivery_allowed": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
