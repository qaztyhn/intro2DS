"""Read canonical crawler outputs and compare independent B0/A/B runs offline.

    python -m src.tools.compare_runs --baseline RUN --a RUN --b RUN --output REPORT
"""

import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path

from .. import metrics
from ..schema import safe_path, valid_arxiv_id
from ..stages.validation import validate_manifest, validate_paper, validate_references
from ..utils.common import paper_key
from .benchmark_protocol import CONFIG_NAMES, POLICY_KEYS, VARIANTS


def read_object(path, *, required=False):
    """Missing is unknown; corruption is an actionable error with its path."""
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_bad_constant)
    except FileNotFoundError:
        if required:
            raise ValueError(f"Missing required record: {path}") from None
        return None
    except (ValueError, OSError) as exc:
        raise ValueError(f"Invalid JSON record {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _bad_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def _read_business_events(path, run_id):
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, parse_constant=_bad_constant)
            if not isinstance(row, dict) or not {"run_id", "stage", "status"} <= row.keys():
                raise ValueError("expected run_id, stage, status")
            if any(not isinstance(row[k], str) or not row[k].strip()
                   for k in ("run_id", "stage", "status")):
                raise ValueError("event keys must be nonempty strings")
        except ValueError as exc:
            raise ValueError(f"Invalid event {path}:{number}: {exc}") from exc
        if row["run_id"] == run_id:
            rows.append(row)
    return rows


def _paper_result(run, paper_id, warnings):
    paper = safe_path(run, f"papers/{paper_key(paper_id)}")
    metadata = read_object(safe_path(paper, "metadata.json"))
    refs = read_object(safe_path(paper, "references.json"))
    saved_validation = read_object(safe_path(paper, "validation.json"))
    # Read every manifest strictly, including unexpected versions. The existing
    # validator supplies the schema/file-integrity checks, without rewriting data.
    for path in paper.glob("versions/v*/manifest.json"):
        read_object(safe_path(paper, path.relative_to(paper).as_posix()), required=True)
    cfg = {"output_root": run.parent, "run_id": run.name}
    validation = validate_paper(cfg, paper_id)
    if saved_validation and saved_validation.get("status") != validation["status"]:
        warnings.append(f"{paper_id}: saved validation differs from current files")
    versions = metadata.get("versions") if metadata else None
    if (not isinstance(versions, list) or not versions
            or any(type(v) is not int or v < 1 for v in versions)
            or len(versions) != len(set(versions))):
        versions = None
    record = {"discovery_status": "pending", "expected_versions": versions,
              "versions": {}, "validation_status": validation["status"]}
    allowed = {"pending", "running", "ok", "empty", "failed", "skipped"}
    if metadata:
        record["discovery_status"] = metadata.get("status") if metadata.get("status") in allowed else "failed"
    source_rows, sizes = [], []
    sources_complete = versions is not None
    for version in versions or []:
        directory = safe_path(paper, f"versions/v{version}")
        manifest = read_object(safe_path(directory, "manifest.json"))
        if manifest is None:
            sources_complete = False
            continue
        status = manifest.get("extract_status")
        if any(i["severity"] == "error" for i in validate_manifest(
                manifest, arxiv_id=paper_id, version=version, source_dir=directory / "source")):
            sources_complete = False
        if status not in allowed or (status == "empty" and manifest.get("error_code") != "NO_TEX_SOURCE"):
            status = "failed"
        # A successful extraction requires a successful download as well.
        if status in {"ok", "empty"} and manifest.get("download_status") != "ok":
            status = "failed"
        record["versions"][version] = {"extract_status": status,
                                         "error_code": manifest.get("error_code")}
        pair = (manifest.get("unpacked_total_bytes"), manifest.get("kept_source_bytes"))
        if all(type(v) is int and v >= 0 for v in pair) and pair[1] <= pair[0]:
            sizes.append(pair)
        files = manifest.get("source_files")
        if status not in {"ok", "empty"} or not isinstance(files, list):
            sources_complete = False
            continue
        for name in files:
            try:
                source = safe_path(directory / "source", name)
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                source_rows.append((version, name, digest))
            except (ValueError, OSError):
                sources_complete = False
    if versions and len(sizes) == len(versions):
        record.update(size_measurement_complete=True,
                      unpacked_total_bytes=sum(pair[0] for pair in sizes),
                      kept_source_bytes=sum(pair[1] for pair in sizes))
    refs_valid = False
    if refs:
        issues = validate_references(refs, arxiv_id=paper_id)
        refs_malformed = any(i["severity"] == "error" for i in issues)
        refs_valid = (not refs_malformed
                      and refs.get("status") in {"ok", "empty"})
        record["references"] = (refs if not refs_malformed else {"status": "failed", "references": []})
    # Canonical multisets preserve duplicate occurrences and ignore API ordering.
    refs_hash = _hash(sorted(json.dumps(ref, sort_keys=True, ensure_ascii=False)
                             for ref in refs["references"])) if refs_valid else None
    snapshot = {
        "expected_versions": versions,
        "metadata_sha256": _hash(metadata) if metadata and metadata.get("status") == "ok" else None,
        "source_sha256": _hash(sorted(source_rows)) if sources_complete else None,
        "references_sha256": refs_hash,
        "reference_count": len(refs["references"]) if refs_valid else None,
        "schema_status": validation["status"],
        "schema_issue_counts": dict(Counter(i["code"] for i in validation["issues"])),
    }
    failed = (record["discovery_status"] == "failed"
              or any(v["extract_status"] == "failed" for v in record["versions"].values())
              or record.get("references", {}).get("status") == "failed"
              or validation["status"] == "failed")
    return record, snapshot, failed, refs_valid


def summarize_run(directory, variant):
    """Adapt both log formats and canonical paper files to shared metrics helpers."""
    run = Path(directory).absolute()
    record = read_object(run / "run.json", required=True)
    sidecar = read_object(run / "benchmark.json") or {}
    for key in ("config", "environment", "policy", "resources", "revision", "instrumentation", "pilot_preflight"):
        if sidecar.get(key) is not None and not isinstance(sidecar[key], dict):
            raise ValueError(f"Invalid {key} object in {run / 'benchmark.json'}")
    if record.get("environment") is not None and not isinstance(record["environment"], dict):
        raise ValueError(f"Invalid environment object in {run / 'run.json'}")
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError(f"Invalid run_id in {run / 'run.json'}")
    ids = record.get("input_ids")
    if (not isinstance(ids, list) or any(not valid_arxiv_id(pid) for pid in ids)
            or len(ids) != len(set(ids))):
        raise ValueError(f"Invalid/duplicate input_ids in {run / 'run.json'}")
    warnings = []
    expected_folders = {paper_key(pid) for pid in ids}
    for path in (run / "papers").glob("*"):
        if path.is_dir() and path.name not in expected_folders:
            warnings.append(f"unexpected paper output directory: {path.name}")
    if sidecar and (sidecar.get("run_id") != run_id or sidecar.get("input_ids") != ids
                    or sidecar.get("variant") != variant):
        raise ValueError(f"benchmark.json does not identify the same {variant} run: {run}")
    if sidecar and sidecar.get("run_status") != record.get("run_status"):
        warnings.append("runner and pipeline completion status differ")
    if sidecar and sidecar.get("cache_mode") != record.get("cache_mode"):
        warnings.append("runner and pipeline cold/warm mode differ")
    if sidecar:
        cfg = sidecar.get("config") or {}
        for key in (*POLICY_KEYS, "config_name", "max_workers", "discovery_batch_size"):
            if key in record and key in cfg and record[key] != cfg[key]:
                warnings.append(f"runner and pipeline config differ: {key}")
        if sidecar.get("returncode") not in {None, 0}:
            warnings.append("runner process exited unsuccessfully")
    if record.get("resumed") and not sidecar:
        warnings.append("legacy resumed run: append-only timing/resources span multiple executions")
    event_path = run / "metrics_events.jsonl"
    try:
        events = metrics.read_events(event_path) if event_path.exists() else []
    except ValueError as exc:
        raise ValueError(f"{event_path}: {exc}") from exc
    events = [row for row in events if row["run_id"] == run_id]
    business = _read_business_events(run / "events.jsonl", run_id)
    results, outputs, failures, refs_observed = {}, {}, 0, 0
    for pid in ids:
        results[pid], outputs[pid], failed, observed = _paper_result(run, pid, warnings)
        failures += failed
        refs_observed += observed
    try:
        summary = metrics.build_run_report(
            run_id=run_id, variant=variant, input_ids=ids, paper_results=results,
            events=[e for e in events if e["event"] != "request_attempt_finished"],
            resource_summary={}, environment=sidecar.get("environment") or record.get("environment") or {})
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"Invalid metrics in {run}: {exc}") from exc
    # Only observed stage durations are presented. An absent stream is not zero.
    timing = {}
    stages = set(summary["timing"]) | {r["stage"] for r in events} | {r["stage"] for r in business}
    for stage in sorted(stages - {"resources"}):
        measured = [r for r in events if r["stage"] == stage and r["event"] in {"started", "finished"}]
        if measured:
            value = metrics.summarize_stage_times(events, run_id, stage)
            if not value["finished_operation_count"]:
                value["duration_sum_s"] = None
            timing[stage] = dict(value, source="metrics_events.jsonl")
        else:
            values = [r["elapsed_seconds"] for r in business
                      if r["stage"] == stage and r["status"] not in {"running", "pending"}
                      and _number(r.get("elapsed_seconds"))]
            timing[stage] = {"duration_mean_s": metrics.mean_or_none(values),
                             "duration_sum_s": sum(values) if values else None,
                             "finished_operation_count": len(values), "source": "events.jsonl"}
    resources = sidecar.get("resources") or next(
        (r for r in reversed(events) if r["stage"] == "resources" and r["event"] == "summary"), {})
    for key in ("rss_peak_bytes", "rss_mean_bytes", "disk_peak_bytes"):
        if resources.get(key) is not None and not _number(resources[key]):
            raise ValueError(f"Invalid resource measurement {key} in {run}")
    if record.get("resumed") and not sidecar:
        timing, resources = {}, {}
    wall = sidecar.get("wall_seconds", record.get("wall_seconds"))
    wall = wall if _number(wall) else None
    final_size = metrics.directory_size_bytes(run / "papers") if (run / "papers").is_dir() else None
    attempts = [r for r in events if r["event"] == "request_attempt_finished"]
    requests = metrics.summarize_http_observations(events, run_id)
    observed_429 = requests["observed_http_429_attempt_count"]
    if requests["issues"]:
        warnings.append("HTTP request identities are ambiguous; request/retry totals remain unknown")
    # Business errors describe outcomes, not HTTP attempts; do not add them to 429 counts.
    errors = Counter(r["stage"] for r in business if r["status"] == "failed")
    schema = dict(Counter(o["schema_status"] for o in outputs.values()))
    reference_terminal_count = sum(r.get("references", {}).get("status") in {"ok", "empty", "failed"}
                                   for r in results.values())
    summary.update(
        report_schema_version="2.0", input_ids=ids, run_directory=str(run),
        run_status=record.get("run_status"), cache_mode=sidecar.get("cache_mode", record.get("cache_mode")),
        synthetic=record.get("synthetic_demo") is True or sidecar.get("synthetic") is True,
        provenance={"revision": sidecar.get("revision"), "config": sidecar.get("config", record),
                    "policy": sidecar.get("policy"), "protocol": sidecar.get("measurement_protocol"),
                    "instrumentation": sidecar.get("instrumentation"), "pilot_preflight": sidecar.get("pilot_preflight"),
                    "warm_seed": sidecar.get("warm_seed"), "runner_status": sidecar.get("run_status")},
        performance={"wall_seconds": wall,
                     "wall_seconds_per_input_paper": wall / len(ids) if wall is not None and ids else None,
                     "mean_paper_processing_seconds": timing.get("paper", {}).get("duration_mean_s"),
                     "rss_peak_bytes": resources.get("rss_peak_bytes"),
                     "rss_mean_bytes": resources.get("rss_mean_bytes"),
                     "disk_peak_bytes": resources.get("disk_peak_bytes"), "final_output_bytes": final_size},
        timing=timing, resources=resources, requests=requests, paper_outputs=outputs,
        schema={"status_counts": schema, "status": "ok" if ids and schema.get("ok") == len(ids) else "incomplete_or_failed"},
        reliability={"failed_paper_count": failures,
                     "unresolved_paper_count": len(ids) - failures - summary["papers"]["complete_paper_count"],
                     "references_observed_success_count": refs_observed,
                     "references_observed_terminal_count": reference_terminal_count,
                     "reference_retrieval_success_rate": metrics.safe_ratio(refs_observed, len(ids)) if reference_terminal_count else None,
                     "valid_reference_count": summary["references"]["reference_count"] if refs_observed else None,
                     "observed_http_429_attempt_count": observed_429,
                     "observed_retry_count": requests["observed_retry_attempt_count"],
                     "total_http_request_count": None, "total_http_429_count": None, "total_http_retry_count": None,
                     "failed_outcomes_by_stage": dict(errors) if business else None,
                     "failed_timed_operations_by_stage": {stage: value["failed_operation_count"]
                         for stage, value in timing.items() if "failed_operation_count" in value} or None,
                     "request_errors_by_stage": dict(Counter(r["stage"] for r in attempts
                         if r.get("error_code") is not None)) if attempts and not requests["issues"] else None,
                     "request_observation_scope": requests["coverage"]},
        warnings=warnings)
    if not refs_observed:
        summary["references"]["reference_count"] = None
    if not reference_terminal_count:
        summary["references"]["reference_response_success_rate"] = None
    if not summary["source_sizes"]["size_measured_paper_count"]:
        for key in ("source_bytes_before_total", "source_bytes_after_total"):
            summary["source_sizes"][key] = None
    # The known subtotal remains useful, but total/coverage cannot imply that
    # undiscovered papers had zero versions.
    version_summary = summary["versions"]
    version_summary["expected_version_count"] = (version_summary["known_expected_version_count"]
        if not version_summary["unknown_versions_paper_count"] else None)
    version_summary["version_coverage"] = (version_summary["known_version_processing_coverage"]
        if not version_summary["unknown_versions_paper_count"] else None)
    return summary


def _content_comparison(baseline, candidate):
    result = {}
    for field in ("expected_versions", "metadata_sha256", "source_sha256", "references_sha256"):
        equal, changed, unknown = [], [], []
        ids = sorted(set(baseline["input_ids"]) | set(candidate["input_ids"]))
        for pid in ids:
            left = baseline["paper_outputs"].get(pid, {}).get(field)
            right = candidate["paper_outputs"].get(pid, {}).get(field)
            (unknown if left is None or right is None else equal if left == right else changed).append(pid)
        result[field] = {"equal_paper_count": len(equal), "changed_ids": changed, "unobserved_ids": unknown}
    return result


def compare_runs(baseline, a, b):
    """Suppress official speedups unless all benchmark conditions are verified."""
    runs = {v: summarize_run(p, v) for v, p in zip(VARIANTS, (baseline, a, b))}
    issues = []

    def issue(message):
        if message not in issues:
            issues.append(message)

    first = runs["B0"]
    for variant, run in runs.items():
        if not run["input_ids"]:
            issue(f"{variant}: empty input set")
        if set(run["input_ids"]) != set(first["input_ids"]):
            issue(f"{variant}: paper ID set differs from B0")
        if run["input_ids"] != first["input_ids"]:
            issue(f"{variant}: paper ID order differs from B0")
        if run["run_status"] != "completed":
            issue(f"{variant}: run is not completed")
        if run["cache_mode"] not in {"cold", "warm"}:
            issue(f"{variant}: cold/warm mode is unknown")
        if run["cache_mode"] != first["cache_mode"]:
            issue(f"{variant}: cold/warm mode differs from B0")
        if not run["environment"] or run["environment"] != first["environment"]:
            issue(f"{variant}: environment missing or differs from B0")
        provenance = run["provenance"]
        config = provenance["config"] or {}
        expected_name = CONFIG_NAMES[variant]
        if config.get("config_name") != expected_name:
            issue(f"{variant}: unexpected/missing pipeline config_name")
        if variant in {"A", "B"} and (type(config.get("discovery_batch_size")) is not int
                                       or config["discovery_batch_size"] < 1):
            issue(f"{variant}: discovery_batch_size is unknown or invalid")
        if variant == "B" and (type(config.get("max_workers")) is not int or config["max_workers"] < 1):
            issue("B: max_workers is unknown or invalid")
        policy = provenance["policy"]
        if not policy or any(policy.get(k) is None for k in POLICY_KEYS):
            issue(f"{variant}: request/credential policy not fully observed")
        elif policy != first["provenance"]["policy"]:
            issue(f"{variant}: request/credential policy differs from B0")
        if not provenance["protocol"] or provenance["protocol"] != first["provenance"]["protocol"]:
            issue(f"{variant}: measurement protocol missing or differs from B0")
        if provenance["instrumentation"] != first["provenance"]["instrumentation"]:
            issue(f"{variant}: observed transport/instrumentation policy differs from B0")
        revision = provenance["revision"] or {}
        if not revision.get("commit") or not revision.get("branch"):
            issue(f"{variant}: code revision/branch is unknown")
        if revision.get("dirty") is not False:
            issue(f"{variant}: code tree is dirty or cleanliness is unknown")
        if run["cache_mode"] == "warm" and not provenance["warm_seed"]:
            issue(f"{variant}: warm cache provenance is unknown")
        if provenance["runner_status"] and provenance["runner_status"] != run["run_status"]:
            issue(f"{variant}: runner and pipeline completion disagree")
        if run["performance"]["wall_seconds"] is None or run["performance"]["wall_seconds"] <= 0:
            issue(f"{variant}: positive wall-clock measurement missing")
        for warning in run["warnings"]:
            issue(f"{variant}: {warning}")
    if len({run["run_id"] for run in runs.values()}) != 3:
        issue("run IDs are not unique")
    if len({run["synthetic"] for run in runs.values()}) != 1:
        issue("synthetic and real runs cannot be mixed")
    valid = not issues
    comparisons = {}
    for variant in ("A", "B"):
        run = runs[variant]
        left, right = first["performance"]["wall_seconds"], run["performance"]["wall_seconds"]
        ratio = left / right if left is not None and right is not None and left > 0 and right > 0 else None
        comparisons[variant] = {
            "speedup_vs_B0": ratio if valid else None,
            "diagnostic_wall_ratio": ratio,
            "paper_completion_rate_delta": (run["papers"]["paper_completion_rate"] - first["papers"]["paper_completion_rate"]
                if run["papers"]["paper_completion_rate"] is not None and first["papers"]["paper_completion_rate"] is not None else None),
            "content": _content_comparison(first, run)}
    return {"report_schema_version": "2.0", "synthetic": all(r["synthetic"] for r in runs.values()),
            "benchmark_valid": valid, "issues": issues, "runs": runs, "comparisons": comparisons,
            "conclusion": ("Conditions verified. Interpret speedups alongside completion and content differences."
                           if valid else "Benchmark conditions unverified or unequal. No performance ranking is valid.")}


def write_comparison(report, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    metrics.write_run_report(directory / "comparison.json", report)
    rows = []
    for variant, run in report["runs"].items():
        rows.append(dict(variant=variant, run_id=run["run_id"], synthetic=run["synthetic"],
            benchmark_valid=report["benchmark_valid"], run_status=run["run_status"], cache_mode=run["cache_mode"],
            **run["performance"], complete_papers=run["papers"]["complete_paper_count"],
            failed_papers=run["reliability"]["failed_paper_count"],
            unresolved_papers=run["reliability"]["unresolved_paper_count"],
            paper_completion_rate=run["papers"]["paper_completion_rate"],
            expected_versions=run["versions"]["expected_version_count"],
            processed_versions=run["versions"]["processed_version_count"],
            version_coverage=run["versions"]["version_coverage"],
            reference_retrieval_success_rate=run["reliability"]["reference_retrieval_success_rate"],
            valid_references=run["reliability"]["valid_reference_count"],
            observed_http_429=run["reliability"]["observed_http_429_attempt_count"],
            observed_retries=run["reliability"]["observed_retry_count"],
            observed_requests=run["requests"]["observed_request_count"],
            observed_attempts=run["requests"]["observed_attempt_count"],
            http_metrics_identity=run["requests"]["identity_status"],
            total_http_requests=run["requests"]["total_http_request_count"],
            total_http_429=run["requests"]["total_http_429_count"],
            total_http_retries=run["requests"]["total_http_retry_count"],
            schema_status=run["schema"]["status"],
            speedup_vs_B0=report["comparisons"].get(variant, {}).get("speedup_vs_B0")))
    with (directory / "comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    def display(value):
        return "N/A" if value is None else f"{value:.4g}" if isinstance(value, float) else str(value)
    title = "SYNTHETIC fixture comparison — not measured performance" if report["synthetic"] else "Crawler run comparison"
    lines = [f"# {title}", "", f"Benchmark valid: **{report['benchmark_valid']}**", "", report["conclusion"], "",
             "| Metric | B0 | A | B |", "|---|---:|---:|---:|"]
    for key in rows[0]:
        if key not in {"variant", "synthetic", "benchmark_valid"}:
            lines.append(f"| {key} | " + " | ".join(display(r[key]) for r in rows) + " |")
    lines += ["", "Stage durations may overlap; never sum them to infer pipeline wall time.",
              "Mean paper processing time has variant-specific boundaries (A excludes batch discovery).",
              "HTTP counts cover observed logical GET callbacks only; arxiv.Client attempts and redirect hops are unobserved. Total HTTP/429/retries are N/A.",
              "N/A means unavailable, not zero. Final output bytes cover papers/ only.", "", "## Benchmark issues", ""]
    lines += [f"- {issue}" for issue in report["issues"]] or ["None."]
    lines += ["", "## Output differences", ""]
    for variant, comparison in report["comparisons"].items():
        for field, values in comparison["content"].items():
            lines.append(f"- {variant} {field}: {values['equal_paper_count']} equal; "
                         f"changed={values['changed_ids']}; unobserved={values['unobserved_ids']}")
    (directory / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "a", "b", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = compare_runs(args.baseline, args.a, args.b)
        write_comparison(report, args.output)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Comparison failed: {exc}\n")
    print(f"Reports: {args.output}; benchmark_valid={report['benchmark_valid']}")
    return 0 if report["benchmark_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
