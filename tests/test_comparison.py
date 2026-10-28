"""Offline quality/comparability regression tests using invented output folders."""

import csv
import json
import subprocess
import sys

import pytest

from src import metrics
from src.tools.compare_runs import compare_runs, summarize_run, write_comparison
from src.tools.demo_comparison import create_synthetic_run, DEMO_IDS
from src.utils.common import paper_key


@pytest.fixture
def runs(tmp_path):
    return [create_synthetic_run(tmp_path / v, v, wall_seconds=t)
            for v, t in zip(("B0", "A", "B"), (12, 8, 6))]


def change(path, **updates):
    value = json.loads(path.read_text())
    value.update(updates)
    metrics.write_run_report(path, value)


def test_completed_runs_and_unordered_parallel_events(runs):
    report = compare_runs(*runs)
    assert report["synthetic"] is True
    assert report["benchmark_valid"] is True
    assert report["comparisons"]["A"]["speedup_vs_B0"] == 1.5
    assert report["comparisons"]["B"]["speedup_vs_B0"] == 2
    baseline = report["runs"]["B0"]
    # Five overlapping stage sums must never replace the 12-second wall.
    assert baseline["performance"]["wall_seconds"] == 12
    assert baseline["performance"]["mean_paper_processing_seconds"] == 6
    assert baseline["papers"]["paper_completion_rate"] == 1
    assert baseline["versions"]["version_coverage"] == 1
    assert baseline["reliability"]["valid_reference_count"] == 0  # observed valid empty responses
    assert baseline["reliability"]["observed_http_429_attempt_count"] == 2
    assert baseline["reliability"]["observed_retry_count"] == 2
    assert baseline["performance"]["final_output_bytes"] > 0


@pytest.mark.parametrize("target, updates, issue", [
    ("run.json", {"run_status": "interrupted"}, "not completed"),
    ("benchmark.json", {"cache_mode": "warm"}, "mode differs"),
    ("benchmark.json", {"environment": {"python": "other"}}, "environment"),
    ("benchmark.json", {"policy": None}, "policy"),
    ("benchmark.json", {"wall_seconds": 0}, "wall-clock"),
    ("benchmark.json", {"revision": {"commit": "abc", "branch": "a", "dirty": True}}, "dirty"),
    ("benchmark.json", {"measurement_protocol": None}, "protocol"),
])
def test_unfair_or_interrupted_runs_suppress_speedups(runs, target, updates, issue):
    change(runs[1] / target, **updates)
    report = compare_runs(*runs)
    assert report["benchmark_valid"] is False
    assert report["comparisons"]["A"]["speedup_vs_B0"] is None
    assert any(issue in text for text in report["issues"])


def test_different_ids_are_not_silently_compared(runs):
    for name in ("run.json", "benchmark.json"):
        change(runs[2] / name, input_ids=[DEMO_IDS[0]])
    report = compare_runs(*runs)
    assert not report["benchmark_valid"]
    assert any("ID set" in issue for issue in report["issues"])


def test_failed_404_is_not_empty_success(runs):
    paper = runs[0] / "papers" / paper_key(DEMO_IDS[0])
    change(paper / "references.json", status="failed", error_code="NOT_FOUND")
    report = summarize_run(runs[0], "B0")
    assert report["papers"]["complete_paper_count"] == 1
    assert report["reliability"]["failed_paper_count"] == 1
    assert report["reliability"]["reference_retrieval_success_rate"] == 0.5
    assert report["references"]["reference_empty_paper_count"] == 1
    assert report["schema"]["status_counts"]["failed"] == 1


def test_missing_observations_are_null_and_papers_remain_unresolved(runs):
    run = runs[0]
    (run / "benchmark.json").unlink()
    (run / "metrics_events.jsonl").unlink()
    change(run / "run.json", wall_seconds=None)
    for pid in DEMO_IDS:
        paper = run / "papers" / paper_key(pid)
        (paper / "references.json").unlink()
        (paper / "metadata.json").unlink()
        (paper / "validation.json").unlink()
    report = summarize_run(run, "B0")
    assert report["performance"]["wall_seconds"] is None
    assert report["performance"]["rss_peak_bytes"] is None
    assert report["reliability"]["observed_retry_count"] is None
    assert report["reliability"]["valid_reference_count"] is None
    assert report["reliability"]["reference_retrieval_success_rate"] is None
    assert report["versions"]["expected_version_count"] is None
    assert report["versions"]["version_coverage"] is None
    assert report["reliability"]["unresolved_paper_count"] == 2


@pytest.mark.parametrize("filename", ["run.json", "benchmark.json", "metadata.json", "references.json", "manifest.json", "validation.json"])
def test_corrupt_json_is_reported_with_filename(runs, filename):
    run = runs[0]
    path = run / filename
    if filename in {"metadata.json", "references.json", "validation.json"}:
        path = run / "papers" / paper_key(DEMO_IDS[0]) / filename
    if filename == "manifest.json":
        path = run / "papers" / paper_key(DEMO_IDS[0]) / "versions/v1/manifest.json"
    path.write_text('{"truncated":')
    with pytest.raises(ValueError, match=filename):
        summarize_run(run, "B0")


@pytest.mark.parametrize("filename", ["events.jsonl", "metrics_events.jsonl"])
def test_corrupt_jsonl_is_not_silently_skipped(runs, filename):
    with (runs[0] / filename).open("a") as handle:
        handle.write('{"truncated":\n')
    with pytest.raises(ValueError, match="line|:1"):
        summarize_run(runs[0], "B0")


def test_legacy_business_times_and_no_resource_fabrication(runs):
    run = runs[0]
    (run / "benchmark.json").unlink()
    (run / "metrics_events.jsonl").unlink()
    (run / "events.jsonl").write_text(json.dumps(dict(run_id=run.name, stage="paper", status="ok", elapsed_seconds=3)) + "\n")
    summary = summarize_run(run, "B0")
    assert summary["performance"]["mean_paper_processing_seconds"] == 3
    assert summary["performance"]["wall_seconds"] == 12
    assert summary["performance"]["rss_mean_bytes"] is None
    assert not compare_runs(*runs)["benchmark_valid"]


def test_reference_multiset_comparison_preserves_duplicates(runs):
    ref = dict(arxiv_id=DEMO_IDS[0], ref_arxiv_id=None, ref_s2_id="fixture-id",
               ref_title="SYNTHETIC", ref_authors=[], ref_submission_date=None)
    other = dict(ref, ref_s2_id="other-fixture")
    for run, refs in zip(runs, ([ref, other, ref], [other, ref, ref], [ref, other])):
        path = run / "papers" / paper_key(DEMO_IDS[0]) / "references.json"
        change(path, status="ok", error_code=None, references=refs)
    report = compare_runs(*runs)
    assert report["comparisons"]["A"]["content"]["references_sha256"]["changed_ids"] == []
    assert report["comparisons"]["B"]["content"]["references_sha256"]["changed_ids"] == [DEMO_IDS[0]]


def test_ok_normalized_empty_is_success_not_confirmed_empty(runs):
    for pid in DEMO_IDS:
        path = runs[0] / "papers" / paper_key(pid) / "references.json"
        change(path, status="ok", error_code=None)
    summary = summarize_run(runs[0], "B0")
    assert summary["reliability"]["reference_retrieval_success_rate"] == 1
    assert summary["references"]["reference_empty_paper_count"] == 0


def test_missing_source_is_schema_failure_even_if_saved_validation_ok(runs):
    source = runs[0] / "papers" / paper_key(DEMO_IDS[0]) / "versions/v1/source/main.tex"
    source.unlink()
    report = summarize_run(runs[0], "B0")
    assert report["schema"]["status_counts"]["failed"] == 1
    assert report["papers"]["complete_paper_count"] == 1
    assert report["warnings"]


def test_resumed_legacy_logs_do_not_mix_execution_times(runs):
    run = runs[0]
    (run / "benchmark.json").unlink()
    change(run / "run.json", resumed=True, cache_mode="warm")
    summary = summarize_run(run, "B0")
    assert summary["performance"]["mean_paper_processing_seconds"] is None
    assert summary["performance"]["rss_peak_bytes"] is None
    assert "multiple executions" in summary["warnings"][0]


def test_exports_and_cli_integration(runs, tmp_path):
    output = tmp_path / "reports"
    result = subprocess.run([sys.executable, "-m", "src.tools.compare_runs", "--baseline", str(runs[0]),
        "--a", str(runs[1]), "--b", str(runs[2]), "--output", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((output / "comparison.json").read_text())["benchmark_valid"]
    with (output / "comparison.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [r["variant"] for r in rows] == ["B0", "A", "B"]
    assert rows[2]["speedup_vs_B0"] == "2.0"
    assert "SYNTHETIC" in (output / "comparison.md").read_text()


def test_unfair_cli_still_writes_reports_but_exits_two(runs, tmp_path):
    change(runs[1] / "benchmark.json", policy=None)
    result = subprocess.run([sys.executable, "-m", "src.tools.compare_runs", "--baseline", str(runs[0]),
        "--a", str(runs[1]), "--b", str(runs[2]), "--output", str(tmp_path / "invalid")], capture_output=True)
    assert result.returncode == 2
    assert (tmp_path / "invalid/comparison.json").is_file()




@pytest.mark.parametrize("status", ["pending", "running", "skipped"])
def test_unfinished_references_are_unresolved_not_failed(runs, status):
    for pid in DEMO_IDS:
        path = runs[0] / "papers" / paper_key(pid) / "references.json"
        change(path, status=status, error_code=None)
    report = summarize_run(runs[0], "B0")
    assert report["reliability"]["failed_paper_count"] == 0
    assert report["reliability"]["unresolved_paper_count"] == 2
    assert report["reliability"]["reference_retrieval_success_rate"] is None


def test_start_only_operation_has_unknown_duration(runs):
    path = runs[0] / "metrics_events.jsonl"
    logger = metrics.EventLogger(path, runs[0].name)
    logger.log("discovery_batch", "started", operation_id="unfinished-batch")
    report = summarize_run(runs[0], "B0")
    stage = report["timing"]["discovery_batch"]
    assert stage["unfinished_operation_count"] == 1
    assert stage["duration_sum_s"] is None
    assert stage["duration_mean_s"] is None


def test_input_order_mismatch_is_reported(runs):
    for name in ("run.json", "benchmark.json"):
        change(runs[1] / name, input_ids=list(reversed(DEMO_IDS)))
    report = compare_runs(*runs)
    assert any("ID order" in issue for issue in report["issues"])
    assert not report["benchmark_valid"]


def test_duplicate_request_attempts_remain_invalid(runs):
    path = runs[0] / "metrics_events.jsonl"
    row = json.loads(path.read_text().splitlines()[-1])
    with path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="Duplicate request attempt"):
        metrics.summarize_request_attempts(metrics.read_events(path), runs[0].name)
    summary = summarize_run(runs[0], "B0")
    assert summary["requests"]["identity_status"] == "ambiguous"
    assert summary["requests"]["observed_request_count"] is None
    assert summary["reliability"]["observed_retry_count"] is None
    assert summary["reliability"]["observed_http_429_attempt_count"] is None
    assert summary["papers"]["complete_paper_count"] == 2
    assert not compare_runs(*runs)["benchmark_valid"]


def test_nonfinite_jsonl_rejected_with_line_number(runs):
    path = runs[0] / "metrics_events.jsonl"
    with path.open("a") as handle:
        handle.write('{"run_id":"B0", "timestamp":"t", "stage":"resources", "event":"sample", "rss_bytes":NaN}\n')
    with pytest.raises(ValueError, match="Invalid JSON at line"):
        summarize_run(runs[0], "B0")


def test_unassigned_output_folders_invalidate_comparison(runs):
    (runs[1] / "papers" / "other-paper").mkdir()
    report = compare_runs(*runs)
    assert not report["benchmark_valid"]
    assert any("unexpected paper output" in issue for issue in report["issues"])


@pytest.mark.parametrize("key", ["config", "environment", "policy", "resources", "revision"])
def test_invalid_provenance_objects_are_actionable_errors(runs, key):
    change(runs[0] / "benchmark.json", **{key: []})
    with pytest.raises(ValueError, match=key):
        summarize_run(runs[0], "B0")
