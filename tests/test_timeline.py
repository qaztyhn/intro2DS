"""Timeline analysis must keep unknown waits and clock discrepancies explicit."""

import json

import pytest

from src import metrics
from src.tools.analyze_timeline import analyze_timeline, interval_overlap


def test_interval_overlap_uses_unions_not_nested_stage_sums():
    assert interval_overlap([(0, 10), (2, 8)], [(3, 7), (4, 6)]) == 4
    assert interval_overlap([(0, 2), (4, 6)], [(2, 4)]) == 0
    assert interval_overlap([], [(0, 1)]) == 0
    with pytest.raises(ValueError):
        interval_overlap([(3, 2)], [])


def test_utc_span_is_not_used_as_monotonic_duration_or_lock_wait(tmp_path):
    metrics.write_run_report(tmp_path / "run.json", {"run_id": "run", "input_ids": ["2507.10001"]})
    rows = [dict(run_id="run", stage="download", arxiv_id="2507.10001", version=1,
                 event="finished", operation_id="op", timestamp="2026-10-28T00:00:12+00:00",
                 duration_s=10, status="ok"),
            dict(run_id="run", stage="download", arxiv_id="2507.10001", version=1,
                 event="started", operation_id="op", timestamp="2026-10-28T00:00:00+00:00")]
    (tmp_path / "metrics_events.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    result = analyze_timeline(tmp_path)
    operation = result["operations"][0]
    assert operation["duration_s"] == 10
    assert operation["utc_span_s"] == 12
    assert operation["utc_span_minus_duration_s"] == 2
    assert operation["unallocated_stage_duration_s"] is None
    assert result["lock_wait_s"] is None
    assert result["papers"][0]["lock_wait_s"] is None
