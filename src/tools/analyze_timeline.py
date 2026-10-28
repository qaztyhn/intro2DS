"""Offline timing evidence: distinguish measured durations, UTC spans and gaps."""

import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .. import metrics
from .compare_runs import _read_business_events, read_object


def _timestamp(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Timeline timestamps must include a timezone")
    return stamp.timestamp()


def interval_overlap(left, right):
    """Intersection of two unions, not a sum of overlapping stage pairs."""
    def union(rows):
        merged = []
        for start, end in sorted(rows):
            if end < start:
                raise ValueError("Negative timestamp interval")
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return merged
    a, b = union(left), union(right)
    i = j = 0
    overlap = 0.0
    while i < len(a) and j < len(b):
        overlap += max(0, min(a[i][1], b[j][1]) - max(a[i][0], b[j][0]))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return overlap


def analyze_timeline(directory):
    run = Path(directory)
    record = read_object(run / "run.json", required=True)
    sidecar = read_object(run / "benchmark.json") or {}
    run_id = record["run_id"]
    events = [e for e in metrics.read_events(run / "metrics_events.jsonl") if e["run_id"] == run_id]
    business = _read_business_events(run / "events.jsonl", run_id)
    http = metrics.summarize_http_observations(events, run_id)
    starts = {}
    for row in events:
        if row["event"] == "started":
            starts[row.get("operation_id")] = row
    operations = []
    for stage in {e["stage"] for e in events if e["event"] in {"started", "finished"}}:
        metrics.summarize_stage_times(events, run_id, stage)  # validates identity/context
    for row in events:
        if row["event"] != "finished":
            continue
        start = starts[row["operation_id"]]
        start_time, end_time = _timestamp(start["timestamp"]), _timestamp(row["timestamp"])
        matching = [r for r in events if r["event"] == "request_attempt_finished"
                    and r["stage"] == row["stage"] and r.get("arxiv_id") == row.get("arxiv_id")
                    and r.get("version") == row.get("version")
                    and start_time <= _timestamp(r["timestamp"]) <= end_time]
        attempt_sum = sum(r["duration_s"] for r in matching) if matching and not http["issues"] else None
        operations.append({"stage": row["stage"], "arxiv_id": row.get("arxiv_id"),
            "version": row.get("version"), "start_utc": start["timestamp"], "end_utc": row["timestamp"],
            "duration_s": row["duration_s"], "utc_span_s": end_time - start_time,
            "utc_span_minus_duration_s": end_time - start_time - row["duration_s"],
            "observed_attempt_duration_sum_s": attempt_sum,
            "unallocated_stage_duration_s": row["duration_s"] - attempt_sum if attempt_sum is not None else None})
    operations.sort(key=lambda op: op["start_utc"])
    durations, intervals = defaultdict(list), defaultdict(list)
    for op in operations:
        if op["arxiv_id"] and op["stage"] not in {"paper", "pipeline"}:
            durations[op["arxiv_id"]].append(op["duration_s"])
            intervals[op["arxiv_id"]].append((_timestamp(op["start_utc"]), _timestamp(op["end_utc"])))
    papers = []
    for pid in record["input_ids"]:
        outcomes = [r for r in business if r["stage"] == "paper" and r.get("arxiv_id") == pid
                    and r.get("elapsed_seconds") is not None]
        # Repeated/resumed paper outcomes cannot be matched to an execution here.
        duration = outcomes[0]["elapsed_seconds"] if len(outcomes) == 1 else None
        stage_sum = sum(durations[pid]) if durations[pid] else None
        papers.append({"arxiv_id": pid, "paper_duration_s": duration, "stage_duration_sum_s": stage_sum,
            "paper_minus_stage_sum_s": duration - stage_sum if duration is not None and stage_sum is not None else None,
            "lock_wait_s": None})
    ids = sorted(intervals)
    overlap = [{"left": left, "right": right,
                "utc_active_stage_overlap_s": interval_overlap(intervals[left], intervals[right])}
               for index, left in enumerate(ids) for right in ids[index + 1:]]
    return {"run_id": run_id, "wall_seconds": sidecar.get("wall_seconds", record.get("wall_seconds")),
            "operations": operations, "papers": papers, "worker_overlap": overlap,
            "http_observations": http, "lock_wait_s": None,
            "limits": ["UTC spans and monotonic operation durations have different boundaries/time bases; do not substitute one for the other.",
                       "Active-stage overlap uses UTC timestamps and excludes lock-waiting workers; it is diagnostic only.",
                       "Paper minus summed stage duration is an unallocated residual, not measured lock wait; overlapping lanes can make it negative.",
                       "Request durations include response/body/file work; no DNS/TLS/TTFB/stream/disk split is recorded.",
                       "No lock requested/acquired/released or worker-start events were recorded; lock wait remains null."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    metrics.write_run_report(args.output, analyze_timeline(args.run_dir))
    print(f"Timeline evidence: {args.output}")


if __name__ == "__main__":
    main()
