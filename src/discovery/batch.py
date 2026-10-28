"""Batch arXiv discovery for config_name=A_BATCH.

Preserves baseline per-paper metadata JSON and error/status contracts. Searches
for base IDs in batches, then batches intermediate version-date lookups. A
missing entry or failed batch is retried through the baseline single-ID path.
"""
from contextlib import nullcontext

from .. import metrics
from ..schema import valid_arxiv_id
from ..utils.common import (
    FAILED, OK, SKIPPED, Stopwatch, read_json, split_versioned_id,
    write_json_atomic,
)
from . import arxiv_api as baseline


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _completed(record):
    return (
        isinstance(record, dict)
        and record.get("status") == OK
        and (record.get("latest_version") == 1 or record.get("revised_dates") is not None)
        and isinstance(record.get("versions"), list)
        and bool(record["versions"])
    )


def _batch_query(client, throttle, ids, batch_no, phase, logger, metrics_logger):
    """One throttled arXiv API search; record one batch operation (not per-paper HTTP timings)."""
    watch = Stopwatch()
    measurement = (
        metrics.measure_stage(
            metrics_logger, "discovery_batch", batch_number=batch_no,
            phase=phase, ids_count=len(ids),
        )
        if metrics_logger is not None else nullcontext()
    )
    try:
        with measurement:
            results = baseline._query(client, throttle, ids)
    except Exception as exc:
        error = baseline._classify_arxiv_error(exc)
        logger.log(
            "discovery_batch", FAILED, error_code=error.error_code,
            http_status=error.status_code, elapsed_seconds=watch.elapsed,
            message=f"batch={batch_no} phase={phase} ids={len(ids)}",
        )
        raise
    logger.log(
        "discovery_batch", OK, elapsed_seconds=watch.elapsed,
        message=f"batch={batch_no} phase={phase} requested={len(ids)} returned={len(results)}",
    )
    return results


def _map_results(results, expected):
    """Never assume the arXiv API preserves the request order."""
    expected = set(expected)
    mapped = {}
    for item in results:
        try:
            returned_id, version = split_versioned_id(item.get_short_id())
        except (TypeError, ValueError, AttributeError):
            continue
        if version < 1:
            continue
        # For explicit version IDs, keep only a matching version.
        full_id = f"{returned_id}v{version}"
        if full_id in expected:
            mapped[full_id] = item
        if returned_id in expected:
            previous = mapped.get(returned_id)
            if previous is None or split_versioned_id(previous.get_short_id())[1] < version:
                mapped[returned_id] = item
    return mapped


def _fallback(cfg, arxiv_id, client, opts, throttle, logger, metrics_logger):
    """Retry single ID with baseline discovery to preserve reliable per-ID failures."""
    return baseline.discover_paper(
        cfg, arxiv_id, client, opts, throttle, logger, metrics_logger
    )


def discover_batches(cfg, ids, client, opts, throttle, logger, metrics_logger=None,
                     batch_size=20):
    """Return {arxiv_id: status}, writing the same metadata.json files as baseline.

    Query errors or missing entries fall back to single-paper discovery.
    Intermediate revised-date queries are batched for all papers in each chunk;
    per-paper fallback handles malformed/absent history and old-style IDs.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("discovery_batch_size must be an integer between 1 and 100")
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate arXiv IDs in batch discovery")

    statuses = {}
    pending = []
    for arxiv_id in ids:
        # Invalid IDs belong to the baseline failure pathway, not a batch request.
        if not valid_arxiv_id(arxiv_id):
            statuses[arxiv_id] = _fallback(
                cfg, arxiv_id, client, opts, throttle, logger, metrics_logger
            )
            continue
        existing = read_json(baseline.paper_dir(cfg, arxiv_id) / "metadata.json")
        if _completed(existing):
            statuses[arxiv_id] = SKIPPED
            logger.log("discovery", SKIPPED, arxiv_id=arxiv_id,
                       message="metadata already ok")
        else:
            pending.append(arxiv_id)

    for batch_no, chunk in enumerate(chunks(pending, batch_size), start=1):
        try:
            latest_results = _batch_query(
                client, throttle, chunk, batch_no, "metadata", logger, metrics_logger
            )
        except Exception:
            for arxiv_id in chunk:
                statuses[arxiv_id] = _fallback(
                    cfg, arxiv_id, client, opts, throttle, logger, metrics_logger
                )
            continue

        latest_by_id = _map_results(latest_results, chunk)
        # The version index must come from the returned arXiv record, not ID order.
        middle_ids = []
        for arxiv_id, result in latest_by_id.items():
            _, latest_version = split_versioned_id(result.get_short_id())
            if "/" not in arxiv_id:
                middle_ids.extend(
                    f"{arxiv_id}v{version}" for version in range(2, latest_version)
                )
        middle_results = {}
        failed_revision_query = False
        for middle_chunk in chunks(middle_ids, batch_size):
            try:
                found = _batch_query(
                    client, throttle, middle_chunk, batch_no, "revisions",
                    logger, metrics_logger,
                )
                middle_results.update(_map_results(found, middle_chunk))
            except Exception:
                failed_revision_query = True
                break

        for arxiv_id in chunk:
            latest = latest_by_id.get(arxiv_id)
            if latest is None:
                # A batch omission is not evidence of NOT_FOUND.
                statuses[arxiv_id] = _fallback(
                    cfg, arxiv_id, client, opts, throttle, logger, metrics_logger
                )
                continue

            watch = Stopwatch()
            record = baseline.make_record(arxiv_id)
            measurement = (
                metrics.measure_stage(metrics_logger, "discovery", arxiv_id=arxiv_id)
                if metrics_logger is not None else nullcontext()
            )
            try:
                with measurement:
                    returned_id, latest_version = split_versioned_id(latest.get_short_id())
                    if returned_id != arxiv_id or latest_version < 1:
                        raise ValueError("arXiv result ID/version mismatch")
                    record["title"] = " ".join(latest.title.split())
                    record["authors"] = [author.name for author in latest.authors]
                    record["submission_date"] = baseline._iso(latest.published)
                    record["venue"] = latest.journal_ref or None
                    record["latest_version"] = latest_version
                    record["versions"] = list(range(1, latest_version + 1))

                    if latest_version == 1:
                        revised, warning = [], None
                    elif "/" in arxiv_id:
                        revised, warning = baseline._revised_dates(
                            client, opts, throttle, arxiv_id, latest
                        )
                    else:
                        needed = range(2, latest_version + 1)
                        dates = {latest_version: baseline._iso(latest.updated)}
                        for version in range(2, latest_version):
                            entry = middle_results.get(f"{arxiv_id}v{version}")
                            if entry is not None:
                                dates[version] = baseline._iso(entry.updated)
                        if failed_revision_query or any(not dates.get(n) for n in needed):
                            revised, warning = baseline._revised_dates(
                                client, opts, throttle, arxiv_id, latest
                            )
                        else:
                            revised, warning = [dates[n] for n in needed], None

                    record["revised_dates"] = revised
                    record["status"] = OK
                    record["error_code"] = None
                status, error = OK, None
            except Exception as exc:
                error = baseline._classify_arxiv_error(exc)
                record["status"] = FAILED
                record["error_code"] = error.error_code
                status, warning = FAILED, None

            write_json_atomic(
                baseline.paper_dir(cfg, arxiv_id) / "metadata.json", record
            )
            statuses[arxiv_id] = status
            if status == FAILED:
                logger.log(
                    "discovery", FAILED, arxiv_id=arxiv_id,
                    error_code=error.error_code, http_status=error.status_code,
                    elapsed_seconds=watch.elapsed, message=str(error)[:300],
                )
            elif warning:
                logger.log(
                    "discovery", OK, arxiv_id=arxiv_id,
                    error_code=warning.error_code, http_status=warning.status_code,
                    elapsed_seconds=watch.elapsed,
                    message=f"batch={batch_no}; revised_dates unavailable: {str(warning)[:200]}",
                )
            else:
                logger.log(
                    "discovery", OK, arxiv_id=arxiv_id,
                    elapsed_seconds=watch.elapsed, message=f"batch={batch_no}",
                )
    return statuses
