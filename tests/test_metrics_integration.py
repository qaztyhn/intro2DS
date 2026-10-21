import pytest

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