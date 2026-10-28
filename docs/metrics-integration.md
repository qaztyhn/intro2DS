# M1 Metrics Integration

## Responsibilities

- Pipeline: supplies input IDs, run ID, variant and business results.
- Metrics: measures time/resources, records events and builds the report.
- Request utilities: invoke the supplied callback after each attempt.
- Validation: supplies the actual schema-validation outcome.

## Measurement boundaries

| Stage | Measurement scope |
|---|---|
| pipeline | One operation covering discovery and all paper processing |
| discovery | One operation covering discovery of the entire input set |
| paper | One operation per paper, after discovery |
| download | One operation per version download |
| extract | One operation per version extraction |
| references | One operation per paper's reference retrieval |
| storage | Explicit storage operations when measured separately |

Use `measure_stage(logger, stage, ...)`.

Pass `arxiv_id` for paper-specific operations and `version` for
version-specific operations.

A timing status of `ok` only means that no exception escaped.
Business completion is calculated from the returned results.

## Paper-result contract

`paper_results` maps base arXiv IDs to records with this structure:

```python
{
    "2507.11659": {
        "discovery_status": "ok",
        "expected_versions": [1, 2],
        "versions": {
            1: {"extract_status": "ok"},
            2: {
                "extract_status": "empty",
                "error_code": "NO_TEX_SOURCE",
            },
        },
        "references": {
            "status": "ok",
            "references": [
                {
                    "ref_arxiv_id": "2501.00001",
                    "ref_s2_id": "example-id",
                }
            ],
        },
        "validation_status": "ok",
        "size_measurement_complete": True,
        "unpacked_total_bytes": 1000,
        "kept_source_bytes": 200,
    }
}
```

This is an illustrative record, not verified paper data.

Rules:

- Keep every assigned ID in `input_ids`, including failures.
- Unknown expected versions: `None`, not an empty list.
- `extract_status="ok"` means usable TeX was extracted.
- Confirmed absence of TeX: `empty` with `NO_TEX_SOURCE`.
- Unsupported/corrupt sources must not be marked as confirmed no-TeX.
- A valid empty reference response uses `empty` and `references=[]`.
- `ok` with no normalized rows is also possible when a nonempty raw response
  contains only unresolved cited papers; this does not establish raw emptiness.
- Set `validation_status="ok"` only after actual validation.
- Size fields are totals across all expected versions of the paper.
- Set `size_measurement_complete=True` only when both size totals
  are known for all expected versions.
- Missing measurements are not zero-byte measurements.
- Cached results marked `skipped` are not automatically counted as
  successful by the current summary functions.

## Request callback

Create the callback with fixed context:

```python
hook = make_request_hook(
    logger,
    "references",
    arxiv_id=paper_id,
)
```

The request utility calls it after each attempt:

```python
hook(
    attempt=1,
    http_status=429,
    error_code="HTTP_429",
    duration=0.8,
    error_message="Too many requests",
)
```

- Attempt numbering starts at 1.
- Duration is in seconds, for this attempt only.
- Backoff before the next attempt is excluded from attempt duration.
- Use `None` for unavailable HTTP status or absent error information.
- The hook logs `duration` as `duration_s`.
- Callback errors must not trigger another HTTP request.
- Return the final request result separately from attempt events.
- Create one hook per logical request, before entering its retry loop.
- Reuse that hook for every attempt of the same request.
- Create a new hook for another logical request.
- The hook generates a request_id, or accepts one explicitly.
- Group attempts by (run_id, request_id), not by attempt number alone.
- Metrics hooks expose `for_request()`. Shared HTTP helpers call it once before
  the retry loop: each page/GET gets a new ID, retries keep it, and
  `request_group_id` links the parent hook. Direct callbacks retain their supplied
  ID. A/B's old common code still needs the reviewed patch; see
  [pre-pilot review](metrics-pilot-readiness.md).
- Total wire HTTP requests/retries/429 remain unknown when library attempts or
  redirect hops are unobserved. Observed callback zero is not a total zero.

## Resource monitoring and report

- Create the monitored directory before entering `ResourceMonitor`.
- Monitor the working directory, including temporary source files.
- Supply the corpus-only directory to `log_resource_results`.
- Stop monitoring before reading its samples or summary.
- Capture `collect_environment()` on the machine executing the crawl.
- Supply results, resource summary and events from the same run to
  `build_run_report`.
- Save the report using `write_run_report`.
- Use a fresh run ID for each benchmark execution.

## Current verification

See [metrics-comparison.md](metrics-comparison.md) for the output adapter,
independent checkout runner, comparability rules and Colab execution cells.
Local real smoke results and actual test outcomes are recorded in
[metrics-verification.md](metrics-verification.md). Colab execution must still
be verified in that runtime.

Run tests from the repository root:

```powershell
python -m pytest tests/test_metrics.py tests/test_metrics_integration.py -q --basetemp=tmp/pytest-metrics --tb=short
```

Create the `tmp` parent directory first if it does not exist.
The `--basetemp` directory is disposable test data.
