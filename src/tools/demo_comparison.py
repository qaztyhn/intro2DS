"""Generate explicitly SYNTHETIC fixtures/reports, never benchmark measurements."""

import argparse
from pathlib import Path

from .. import metrics
from ..downloader.http import make_manifest
from ..utils.common import paper_key
from .benchmark_protocol import CONFIG_NAMES, DEFAULTS, POLICY_KEYS, PROTOCOL
from .compare_runs import compare_runs, write_comparison

DEMO_IDS = ("2507.01659", "2507.01660")
TIMESTAMP = "2026-10-28T00:00:00+00:00"


def create_synthetic_run(directory, variant, *, wall_seconds=12, ids=DEMO_IDS):
    """Write small schema-valid invented papers and invented timing observations."""
    run = Path(directory)
    run.mkdir(parents=True, exist_ok=False)
    write = metrics.write_run_report
    cfg = dict(DEFAULTS, config_name=CONFIG_NAMES[variant],
               max_workers=2 if variant == "B" else 1,
               discovery_batch_size=20 if variant == "A" else 1)
    environment = {"synthetic": True, "python": "synthetic-interpreter", "dependencies": {"fixture": "1"}}
    write(run / "run.json", dict(cfg, run_id=run.name, input_ids=list(ids), synthetic_demo=True,
        run_status="completed", cache_mode="cold", environment=environment, wall_seconds=wall_seconds))
    policy = {key: cfg[key] for key in POLICY_KEYS if key in cfg}
    policy.update(s2_credentials_present=False, s2_min_interval_seconds=3.0)
    write(run / "benchmark.json", dict(run_id=run.name, variant=variant, synthetic=True,
        input_ids=list(ids), run_status="completed", cache_mode="cold", environment=environment,
        measurement_protocol=PROTOCOL, revision={"branch": f"synthetic/{variant}",
        "commit": f"synthetic-{variant}", "dirty": False}, policy=policy, config=cfg, warm_seed=None,
        wall_seconds=wall_seconds, resources={"rss_peak_bytes": 4096, "rss_mean_bytes": 2048,
                                             "disk_peak_bytes": 8192}))
    events = metrics.EventLogger(run / "metrics_events.jsonl", run.name)
    for pid in ids:
        paper = run / "papers" / paper_key(pid)
        source = paper / "versions/v1/source/main.tex"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"% SYNTHETIC fixture; not an actual publication\n")
        write(paper / "metadata.json", dict(arxiv_id=pid, title="SYNTHETIC fixture",
            authors=["Invented Author"], submission_date="2025-07-01T00:00:00+00:00",
            revised_dates=[], venue=None, latest_version=1, versions=[1], status="ok", error_code=None))
        manifest = make_manifest(pid, 1)
        manifest.update(download_status="ok", extract_status="ok", source_format="tex",
                        source_files=["main.tex"], archive_bytes=source.stat().st_size,
                        unpacked_total_bytes=source.stat().st_size, kept_source_bytes=source.stat().st_size)
        write(paper / "versions/v1/manifest.json", manifest)
        write(paper / "references.json", dict(arxiv_id=pid, status="empty",
            error_code="NO_REFERENCES_RETURNED", source="semantic_scholar", fetched_at=TIMESTAMP, references=[]))
        write(paper / "validation.json", dict(arxiv_id=pid, status="ok", issues=[]))
        for stage in ("paper", "download", "extract", "references", "storage"):
            # Deliberately serialize finish before start to test unordered exports.
            events.log(stage, "finished", operation_id=f"{pid}-{stage}", arxiv_id=pid,
                       status="ok", duration_s=wall_seconds / len(ids))
            events.log(stage, "started", operation_id=f"{pid}-{stage}", arxiv_id=pid)
    hook = metrics.make_request_hook(events, "references", arxiv_id=ids[0])
    hook(attempt=1, http_status=429, error_code="HTTP_429", duration=0.1, error_message="synthetic rate limit")
    hook(attempt=2, http_status=429, error_code="HTTP_429", duration=0.2, error_message="synthetic rate limit")
    hook(attempt=3, http_status=200, error_code=None, duration=0.1, error_message=None)
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    paths = [create_synthetic_run(args.output / v, v, wall_seconds=t)
             for v, t in zip(("B0", "A", "B"), (12, 8, 6))]
    write_comparison(compare_runs(*paths), args.output / "report")
    print(f"SYNTHETIC example only: {args.output / 'report/comparison.md'}")


if __name__ == "__main__":
    main()
