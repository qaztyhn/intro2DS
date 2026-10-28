"""Forty-paper mixed-outcome integration and HTTP coverage evidence, offline."""

from types import SimpleNamespace


from src import metrics
from src.stages import references
from src.stages.validation import validate_paper
from src.tools.compare_runs import compare_runs, write_comparison
from src.tools.demo_comparison import create_synthetic_run
from src.utils import http

IDS = [f"2507.{number:05d}" for number in range(10001, 10041)]


def response(status=200, payload=None):
    return SimpleNamespace(status_code=status, headers={}, json=lambda: payload)


def test_forty_paper_mixed_outcomes_with_real_paginated_request_code(tmp_path, monkeypatch):
    monkeypatch.delenv("S2_API_KEY", raising=False)
    monkeypatch.setattr(http.time, "sleep", lambda _: None)
    monkeypatch.setattr(references, "S2", http.Throttle(0))

    def get(**kwargs):
        pid = kwargs["url"].split("ARXIV:")[1].split("/")[0]
        offset = kwargs["params"]["offset"]
        if pid == IDS[1]:
            return response(404)
        if pid == IDS[2] and offset:
            return response(503)
        return response(payload={"offset": offset,
            "data": [{"citedPaper": {"paperId": f"SYNTHETIC-{offset}", "title": "Invented reference"}}],
            **({"next": 1} if offset == 0 else {})})

    monkeypatch.setattr(http.requests, "get", get)
    runs = []
    for variant in ("B0", "A", "B"):
        run = create_synthetic_run(tmp_path / variant, variant, ids=IDS)
        logger = metrics.EventLogger(run / "metrics_events.jsonl", run.name)
        for pid in IDS[:3]:
            result = references.crawl_references(pid, max_retries=1,
                on_attempt=metrics.make_request_hook(logger, "references", arxiv_id=pid))
            paper = run / "papers" / pid.replace(".", "-")
            metrics.write_run_report(paper / "references.json", result)
            cfg = {"output_root": run.parent, "run_id": run.name}
            metrics.write_run_report(paper / "validation.json", validate_paper(cfg, pid))
        runs.append(run)
    report = compare_runs(*runs)
    assert report["synthetic"] and report["benchmark_valid"]
    for run in report["runs"].values():
        assert run["papers"]["input_paper_count"] == 40
        assert run["papers"]["complete_paper_count"] == 38
        assert run["reliability"]["failed_paper_count"] == 2
        assert run["reliability"]["reference_retrieval_success_rate"] == 38 / 40
        assert run["reliability"]["valid_reference_count"] == 2
        assert run["schema"]["status_counts"] == {"ok": 38, "failed": 2}
        assert run["requests"]["identity_status"] == "observed"
        assert run["requests"]["total_http_retry_count"] is None
    write_comparison(report, tmp_path / "report")
    assert (tmp_path / "report/comparison.csv").is_file()
