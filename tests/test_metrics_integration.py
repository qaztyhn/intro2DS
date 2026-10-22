import pytest
import json

from src.metrics import (
    EventLogger,
    ResourceMonitor,
    log_resource_results,
    measure_stage,
    read_events,
    summarize_papers,
    summarize_references,
    summarize_source_sizes,
    summarize_stage_times,
    summarize_versions,
    build_run_report,
    collect_environment,
    write_run_report,
    make_request_hook,
)


def test_metrics_pipeline_integration(tmp_path):
    run_id = "integration-01"
    paper_id = "sample-paper"

    work = tmp_path / "work"
    output = work / "corpus"
    output.mkdir(parents=True)

    # Keep logs outside the directory being measured.
    logger = EventLogger(tmp_path / "events.jsonl", run_id)

    with ResourceMonitor(work, interval_s=0.1) as monitor:
        with measure_stage(logger, "pipeline"):
            with measure_stage(logger, "discovery"):
                expected_versions = {paper_id: [1]}

            with measure_stage(logger, "paper", arxiv_id=paper_id):
                with measure_stage(
                    logger, "extract", arxiv_id=paper_id, version=1
                ):
                    (output / "main.tex").write_bytes(b"abc")

                with measure_stage(
                    logger, "references", arxiv_id=paper_id
                ):
                    hook = make_request_hook(
                        logger,
                        "references",
                        arxiv_id=paper_id,
                    )
                    hook(
                        attempt=1,
                        http_status=429,
                        error_code="HTTP_429",
                        duration=0.2,
                        error_message="Synthetic rate limit",
                    )
                    hook(
                        attempt=2,
                        http_status=200,
                        error_code=None,
                        duration=0.1,
                        error_message=None,
                    )
                    reference_result = {
                        "status": "ok",
                        "references": [
                            {"ref_arxiv_id": "2507.11659"}
                        ],
                    }

                paper_result = {
                    "discovery_status": "ok",
                    "expected_versions": [1],
                    "versions": {1: {"extract_status": "ok"}},
                    "references": reference_result,
                    "validation_status": "ok",
                    "size_measurement_complete": True,
                    "unpacked_total_bytes": 10,
                    "kept_source_bytes": 3,
                }

    resources = log_resource_results(
        logger, monitor.sampler, output
    )
    events = read_events(logger.path)

    for stage in ("pipeline", "discovery", "paper", "extract", "references"):
        timing = summarize_stage_times(events, run_id, stage)
        assert timing["started_operation_count"] == 1
        assert timing["finished_operation_count"] == 1
        assert timing["unfinished_operation_count"] == 0
        assert timing["successful_operation_count"] == 1
        assert timing["duration_mean_s"] >= 0

    papers = summarize_papers(
        [paper_id], {paper_id: paper_result}
    )
    versions = summarize_versions(
        expected_versions,
        {paper_id: paper_result["versions"]},
    )
    references = summarize_references(
        [paper_id], {paper_id: reference_result}
    )
    sizes = summarize_source_sizes(
        [paper_id], {paper_id: paper_result}
    )

    assert papers["paper_completion_rate"] == 1.0
    assert versions["known_version_processing_coverage"] == 1.0
    assert references["reference_count"] == 1
    assert sizes["source_reduction_rate"] == pytest.approx(0.7)

    assert resources["sample_count"] >= 2
    assert resources["rss_peak_bytes"] > 0
    assert resources["rss_mean_bytes"] is not None
    assert resources["output_storage_bytes"] == 3

    resource_summaries = [
        event
        for event in events
        if event["stage"] == "resources"
        and event["event"] == "summary"
    ]
    assert len(resource_summaries) == 1
    assert resource_summaries[0]["output_storage_bytes"] == 3
    report = build_run_report(
        run_id=run_id,
        variant="synthetic-test",
        input_ids=[paper_id],
        paper_results={paper_id: paper_result},
        events=events,
        resource_summary=resources,
        environment=collect_environment(),
    )

    report_path = tmp_path / "metrics.json"
    write_run_report(report_path, report)

    saved = json.loads(report_path.read_text(encoding="utf-8"))

    assert saved["run_id"] == run_id
    assert saved["variant"] == "synthetic-test"
    assert saved["papers"]["complete_paper_count"] == 1
    assert saved["versions"]["processed_version_count"] == 1
    assert saved["references"]["reference_count"] == 1
    assert saved["source_sizes"]["source_bytes_after_total"] == 3
    assert saved["resources"]["output_storage_bytes"] == 3
    assert saved["timing"]["pipeline"]["finished_operation_count"] == 1
    assert saved["timing"]["discovery"]["finished_operation_count"] == 1
    assert saved["timing"]["paper"]["finished_operation_count"] == 1
    assert saved["environment"]["python_version"]
    assert saved["requests"]["observed_request_count"] == 1
    assert saved["requests"]["observed_attempt_count"] == 2
    assert saved["requests"]["observed_retry_attempt_count"] == 1
    assert saved["requests"]["error_attempt_count"] == 1
    assert saved["requests"]["error_code_counts"] == {"HTTP_429": 1}
    assert saved["requests"]["attempt_duration_sum_s"] == pytest.approx(0.3)

    # A failed attempt followed by success does not make the paper fail.
    assert saved["papers"]["complete_paper_count"] == 1

def test_metrics_pipeline_failure_integration(tmp_path):
    run_id = "failed-run-01"
    paper_id = "sample-paper"

    work = tmp_path / "work"
    output = work / "corpus"
    output.mkdir(parents=True)

    logger = EventLogger(tmp_path / "events.jsonl", run_id)
    original_error = RuntimeError("Synthetic download failure")

    with pytest.raises(RuntimeError) as caught:
        with ResourceMonitor(work, interval_s=0.1) as monitor:
            with measure_stage(logger, "pipeline"):
                with measure_stage(logger, "discovery"):
                    pass

                with measure_stage(
                    logger, "paper", arxiv_id=paper_id
                ):
                    with measure_stage(
                        logger,
                        "download",
                        arxiv_id=paper_id,
                        version=1,
                    ):
                        raise original_error

    assert caught.value is original_error

    resources = log_resource_results(
        logger, monitor.sampler, output
    )
    events = read_events(logger.path)

    report = build_run_report(
        run_id=run_id,
        variant="synthetic-failure-test",
        input_ids=[paper_id],
        paper_results={
            paper_id: {
                "discovery_status": "ok",
                "expected_versions": [1],
                "versions": {
                    1: {"extract_status": "failed"}
                },
            }
        },
        events=events,
        resource_summary=resources,
        environment=collect_environment(),
    )

    path = tmp_path / "metrics.json"
    write_run_report(path, report)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["papers"]["complete_paper_count"] == 0
    assert saved["papers"]["paper_completion_rate"] == 0.0
    assert saved["versions"]["unresolved_version_count"] == 1
    assert saved["source_sizes"]["size_measured_paper_count"] == 0

    for stage in ("pipeline", "paper", "download"):
        timing = saved["timing"][stage]
        assert timing["failed_operation_count"] == 1
        assert timing["unfinished_operation_count"] == 0
        assert timing["duration_mean_s"] >= 0

    assert saved["resources"]["sample_count"] >= 2
    assert saved["resources"]["output_storage_bytes"] == 0