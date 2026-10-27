"""Compare completed production runs without issuing model requests."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path


def summarize(path: Path) -> dict:
    run = json.loads(path.read_text(encoding="utf-8-sig"))
    policy = run["policy"]
    elapsed = float(run["finished_at"]) - float(run["started_at"])
    if not math.isfinite(elapsed) or elapsed <= 0:
        raise ValueError("run must have a positive completed duration")
    rows = run.get("results", [])
    deliveries = sum(max(0, int(row.get("formal_deliveries", 0))) for row in rows)
    tasks = [row for row in rows if row.get("status") != "ALREADY_DELIVERED" and
             (row.get("stage") != "material" or row.get("status") in {"ERROR", "BLOCKED"})]
    failures = sum(row.get("status") in {"ERROR", "BLOCKED", "PARTIAL", "UNCERTIFIED",
                   "PREVIEW_READY", "QC_READY_FOR_DELIVERY", "UNCERTIFIED_TIMEOUT"}
                   or row.get("rc", 0) != 0 for row in tasks)
    # Logs from real requests are optional. A missing token count stays unknown.
    requests, reasons, tokens, peak = 0, Counter(), Counter(), None
    calls = []
    if run.get("request_events"):
        for event_file in run["request_events"]:
            for line in Path(event_file).read_text(encoding="utf-8").splitlines():
                try:
                    call = json.loads(line)
                except ValueError:
                    continue
                started = call.get("metadata", {}).get("started_at", 0)
                if run["started_at"] <= started <= run["finished_at"]:
                    calls.append(call)
    else:
        for ledger_file in run.get("request_ledgers", []):
            ledger = json.loads(Path(ledger_file).read_text(encoding="utf-8"))
            calls.extend(ledger.get("history", []))
    for call in calls:
        requests += 1
        reasons[call.get("error_code") or call.get("finish_reason") or "success"] += 1
        for key, value in call.get("usage", {}).items():
            if isinstance(value, (int, float)):
                tokens[key] += value
        inflight = call.get("metadata", {}).get("inflight")
        if isinstance(inflight, int):
            peak = max(peak or 0, inflight)
    return {"run": str(path), "vision_slots": policy["vision_slots"],
            "prep_slots": policy["prep_slots"], "elapsed_s": round(elapsed, 3),
            "formal_deliveries": deliveries,
            "qualified_per_hour": round(deliveries * 3600 / elapsed, 4),
            "task_failures": failures, "task_count": len(tasks),
            "failure_rate": failures / len(tasks) if tasks else None,
            "requests": requests if run.get("request_ledgers") or run.get("request_events") else None,
            "finish_reasons": dict(reasons), "usage": dict(tokens), "peak_inflight": peak}


def compare(paths: list[Path], *, confirmed_account_capacity: int = 20) -> dict:
    if confirmed_account_capacity < 20:
        raise ValueError("calibration requires the starting account capacity of 20")
    rows = [summarize(path) for path in paths]
    eligible = [row for row in rows if row["vision_slots"] <= confirmed_account_capacity]
    if not eligible:
        raise ValueError("no run within confirmed account capacity")
    # Higher concurrency qualifies only when its error rate does not worsen
    # against the lower-concurrency observations supplied in this comparison.
    candidates = []
    for row in eligible:
        lower = [other for other in eligible if other["vision_slots"] <= row["vision_slots"]
                 and other["prep_slots"] <= row["prep_slots"] and other is not row]
        if all(other["failure_rate"] is None or row["failure_rate"] is not None
               and row["failure_rate"] <= other["failure_rate"] for other in lower):
            candidates.append(row)
    selected = min(candidates or eligible, key=lambda row: (
        -row["qualified_per_hour"], row["vision_slots"], row["prep_slots"]))
    return {"runs": rows, "selected_observation": selected["run"],
            "selected_policy": {"vision_slots": selected["vision_slots"],
                                "prep_slots": selected["prep_slots"]},
            "confirmed_account_capacity": confirmed_account_capacity,
            "next_api_step": next((step for step in (20, 40, 80)
                                   if selected["vision_slots"] < step <= confirmed_account_capacity), None),
            "note": "仅比较真实生产观察。素材、文案与缓存冷热不同会影响结果；本工具不发请求、不改配置。"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument("--confirmed-account-capacity", type=int, default=20)
    args = parser.parse_args(argv)
    print(json.dumps(compare(args.run, confirmed_account_capacity=args.confirmed_account_capacity),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
