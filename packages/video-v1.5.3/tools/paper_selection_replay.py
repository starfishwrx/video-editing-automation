"""Reselect paper shots from existing caches without running a production build."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "doubao-jianying-orchestrator"
if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))

from orchestrator.selection import paper_selection


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "analysis", "baseline-table", "requests", "report-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--round", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args(argv)
    def read(path):
        return json.loads(path.read_text(encoding="utf-8-sig"))
    manifest, analysis, baseline, requests = (read(path) for path in (
        args.manifest, args.analysis, args.baseline_table, args.requests))
    if not isinstance(baseline, list) or not isinstance(requests, list):
        parser.error("baseline-table and requests must be JSON lists")
    by_index = {row["index"]: row for row in baseline}
    enriched = []
    for request in requests:
        row = by_index.get(request.get("index"))
        if row is None:
            parser.error("request index is not in the fixed baseline")
        text = row.get("text")
        text = "，".join(text) if isinstance(text, list) else str(text or "")
        enriched.append(dict(request, text=text, segment_us=row["segment_us"]))
    args.report_dir.mkdir(parents=True, exist_ok=True)
    result = paper_selection.reselect(manifest, analysis, enriched, args.report_dir)
    result["round"] = args.round
    result["inputs"] = {"analysis": str(args.analysis.resolve()),
                        "baseline_table": str(args.baseline_table.resolve()),
                        "requests": str(args.requests.resolve())}
    paper_selection._write(args.report_dir / "selection.json", result)
    print(json.dumps({"status": result["status"], "selected": len(result["overrides"]),
        "unresolved": result["unresolved"], "round": args.round,
        "report": str(args.report_dir / "selection.json")}, ensure_ascii=False))
    return 0 if result["status"] == "SELECTION_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
