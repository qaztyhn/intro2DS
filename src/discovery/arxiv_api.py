"""Baseline discovery: arXiv API -> metadata.json + version list for every id.

Output per paper: <output_root>/<run_id>/papers/<paper_key>/metadata.json
Run log:          <output_root>/<run_id>/events.jsonl

One paper failing is logged and never stops the run.

Dates of versions 2..N ("revised_dates"):
  * new-style ids (2310.12345): arXiv API, one batched request for versions 2..N-1
    (the latest version's date comes with the first request).
  * old-style ids (hep-th/9901001): the arXiv API answers HTTP 500 for any versioned
    old-style id, so the "Submission history" block of the abs page is parsed instead.
    The same page is also the fallback when the API request fails for a new-style id.
  * If the dates cannot be obtained at all, revised_dates stays null, the paper is still
    status "ok" (the version list is known, so download can proceed) and the reason is
    logged in events.jsonl as a warning.
"""

import pathlib
import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

# pyrefly: ignore[missing-import]
import arxiv
import requests

from ..common import (
    FAILED,
    HTTP_429,
    HTTP_5XX,
    INVALID_RESPONSE,
    NETWORK_ERROR,
    NOT_FOUND,
    OK,
    SKIPPED,
    TIMEOUT,
    EventLog,
    PipelineError,
    Stopwatch,
    Throttle,
    load_ids,
    paper_key,
    read_json,
    split_versioned_id,
    write_json_atomic,
)
from ..config import load_config
from ..request_utils import RequestClient

STAGE = "discovery"
INVALID_ID = "INVALID_ID"
ABS_URL = "https://export.arxiv.org/abs/{arxiv_id}"
_HAS_VERSION_SUFFIX = re.compile(r"v\d+$")
_HISTORY_BLOCK = re.compile(r'<div class="submission-history">(.*?)</div>', re.DOTALL)
_HISTORY_ENTRY = re.compile(
    r"\[v(\d+)\].*?</strong>\s*(\w{3}, \d{1,2} \w{3} \d{4} \d{2}:\d{2}:\d{2}) UTC", re.DOTALL
)


def paper_dir(cfg: Dict[str, Any], arxiv_id: str) -> pathlib.Path:
    return cfg["output_root"] / cfg["run_id"] / "papers" / paper_key(arxiv_id)


def make_record(arxiv_id: str) -> Dict[str, Any]:
    """Record for the `papers` table fields plus run-level fields. Missing = None."""
    return {
        "arxiv_id": arxiv_id,
        "title": None,
        "authors": None,
        "submission_date": None,
        "revised_dates": None,
        "venue": None,
        "latest_version": None,
        "versions": None,
        "status": FAILED,
        "error_code": None,
    }


def _iso(value) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def _classify_arxiv_error(exc: Exception) -> PipelineError:
    """Map exceptions raised by the arxiv package / requests onto our error codes."""
    if isinstance(exc, PipelineError):
        return exc
    if isinstance(exc, arxiv.HTTPError):
        status = exc.status
        if status == 429:
            return PipelineError(HTTP_429, str(exc), http_status=status)
        if 500 <= status < 600:
            return PipelineError(HTTP_5XX, str(exc), http_status=status)
        return PipelineError(INVALID_RESPONSE, str(exc), http_status=status)
    if isinstance(exc, requests.Timeout):
        return PipelineError(TIMEOUT, str(exc))
    if isinstance(exc, requests.RequestException):
        return PipelineError(NETWORK_ERROR, str(exc))
    # UnexpectedEmptyPageError, MissingFieldError, other ArxivError, parsing problems
    return PipelineError(INVALID_RESPONSE, f"{type(exc).__name__}: {exc}")


def _query(client: arxiv.Client, throttle: Throttle, id_list: List[str]) -> List[arxiv.Result]:
    throttle.wait()
    try:
        return list(client.results(arxiv.Search(id_list=id_list, max_results=len(id_list))))
    except Exception as exc:  # noqa: BLE001 - everything is classified below
        raise _classify_arxiv_error(exc) from exc
    finally:
        throttle.mark()


def parse_submission_history(html: str) -> Dict[int, str]:
    """{version: ISO-8601 UTC timestamp} from the abs page's "Submission history"."""
    block = _HISTORY_BLOCK.search(html)
    if not block:
        raise PipelineError(INVALID_RESPONSE, "no submission-history block on abs page")
    dates: Dict[int, str] = {}
    for version, text in _HISTORY_ENTRY.findall(block.group(1)):
        parsed = parsedate_to_datetime(f"{text} +0000").astimezone(timezone.utc)
        dates[int(version)] = parsed.isoformat(timespec="seconds")
    if not dates:
        raise PipelineError(INVALID_RESPONSE, "no versions found in submission history")
    return dates


def _dates_from_api(
    client: arxiv.Client, throttle: Throttle, arxiv_id: str, latest_version: int, latest_updated
) -> Dict[int, str]:
    dates = {latest_version: _iso(latest_updated)}
    middle = [f"{arxiv_id}v{n}" for n in range(2, latest_version)]
    if middle:
        for entry in _query(client, throttle, middle):
            _, version = split_versioned_id(entry.get_short_id())
            dates[version] = _iso(entry.updated)
    return dates


def _dates_from_abs(http: RequestClient, throttle: Throttle, arxiv_id: str) -> Dict[int, str]:
    throttle.wait()
    try:
        response = http.get(ABS_URL.format(arxiv_id=arxiv_id))
    finally:
        throttle.mark()
    return parse_submission_history(response.text)


def _revised_dates(
    client: arxiv.Client, http: RequestClient, throttle: Throttle, arxiv_id: str, latest
):
    """Return (revised_dates or None, warning or None)."""
    _, latest_version = split_versioned_id(latest.get_short_id())
    if latest_version == 1:
        return [], None
    needed = range(2, latest_version + 1)

    sources = []
    if "/" not in arxiv_id:  # old-style ids: API cannot serve versioned ids
        sources.append(lambda: _dates_from_api(client, throttle, arxiv_id, latest_version, latest.updated))
    sources.append(lambda: _dates_from_abs(http, throttle, arxiv_id))

    warning: Optional[PipelineError] = None
    for fetch in sources:
        try:
            dates = fetch()
            missing = [n for n in needed if not dates.get(n)]
            if missing:
                raise PipelineError(INVALID_RESPONSE, f"no date for versions {missing}")
            return [dates[n] for n in needed], None
        except Exception as exc:  # noqa: BLE001 - try the next source, remember why
            warning = _classify_arxiv_error(exc)
    return None, warning


def discover_one(
    arxiv_id: str, client: arxiv.Client, http: RequestClient, throttle: Throttle, record: Dict[str, Any]
) -> Optional[PipelineError]:
    """Fill ``record`` in place. Raises PipelineError if the paper itself cannot be
    discovered (record stays failed). Returns a warning (PipelineError) when only the
    revised dates are missing; the paper is then still ok.
    """
    if _HAS_VERSION_SUFFIX.search(arxiv_id):
        raise PipelineError(INVALID_ID, f"{arxiv_id!r}: give the base id without a vN suffix")

    results = _query(client, throttle, [arxiv_id])
    if not results:
        raise PipelineError(NOT_FOUND, f"arXiv returned no entry for {arxiv_id}")
    latest = results[0]
    _, latest_version = split_versioned_id(latest.get_short_id())

    record["title"] = " ".join(latest.title.split())
    record["authors"] = [author.name for author in latest.authors]
    record["submission_date"] = _iso(latest.published)  # published = date of v1
    record["venue"] = latest.journal_ref or None
    record["latest_version"] = latest_version
    record["versions"] = list(range(1, latest_version + 1))

    record["revised_dates"], warning = _revised_dates(client, http, throttle, arxiv_id, latest)
    record["status"] = OK
    record["error_code"] = None
    return warning


def make_clients(cfg: Dict[str, Any], throttle: Optional[Throttle] = None):
    """Build the arxiv client, the HTTP client and (unless given) a Throttle."""
    client = arxiv.Client(
        page_size=100,
        delay_seconds=cfg["arxiv_delay_seconds"],
        num_retries=cfg["max_retries"],
    )
    http = RequestClient(cfg["user_agent"], cfg["timeout"], cfg["max_retries"])
    return client, http, throttle or Throttle(cfg["arxiv_delay_seconds"])


def discover_paper(
    cfg: Dict[str, Any],
    arxiv_id: str,
    client: arxiv.Client,
    http: RequestClient,
    throttle: Throttle,
    events: EventLog,
) -> str:
    """Discover one paper. Returns OK, FAILED or SKIPPED. Never raises for a bad paper."""
    path = paper_dir(cfg, arxiv_id) / "metadata.json"
    existing = read_json(path)
    complete = (
        existing
        and existing.get("status") == OK
        and (existing.get("latest_version") == 1 or existing.get("revised_dates") is not None)
    )
    if complete:
        events.log(STAGE, SKIPPED, arxiv_id=arxiv_id, message="metadata already ok")
        return SKIPPED

    watch = Stopwatch()
    record = make_record(arxiv_id)
    try:
        warning = discover_one(arxiv_id, client, http, throttle, record)
    except Exception as exc:  # noqa: BLE001 - one paper must never stop the run
        error = _classify_arxiv_error(exc)
        record["status"] = FAILED
        record["error_code"] = error.error_code
        status = FAILED
        events.log(
            STAGE, FAILED, arxiv_id=arxiv_id, error_code=error.error_code,
            http_status=error.http_status, attempts=error.attempts,
            message=str(error)[:300], elapsed_seconds=watch.elapsed,
        )
    else:
        status = OK
        if warning:
            events.log(
                STAGE, OK, arxiv_id=arxiv_id, error_code=warning.error_code,
                http_status=warning.http_status, attempts=warning.attempts,
                message=f"revised_dates unavailable: {str(warning)[:250]}",
                elapsed_seconds=watch.elapsed,
            )
        else:
            events.log(STAGE, OK, arxiv_id=arxiv_id, elapsed_seconds=watch.elapsed)
    write_json_atomic(path, record)
    return status


def run_discovery(cfg: Dict[str, Any], events: Optional[EventLog] = None) -> Dict[str, int]:
    """Discover every id in ``ids_file``. Returns counts per status."""
    run_dir = cfg["output_root"] / cfg["run_id"]
    if events is None:
        events = EventLog(run_dir / "events.jsonl", cfg["run_id"])
    client, http, throttle = make_clients(cfg)

    counts = {OK: 0, FAILED: 0, SKIPPED: 0}
    for arxiv_id in load_ids(cfg["ids_file"]):
        counts[discover_paper(cfg, arxiv_id, client, http, throttle, events)] += 1
    return counts


def main() -> None:
    cfg = load_config()
    write_json_atomic(
        cfg["output_root"] / cfg["run_id"] / "run.json",
        {k: str(v) if isinstance(v, pathlib.Path) else v for k, v in cfg.items()},
    )
    counts = run_discovery(cfg)
    print(f"run_id={cfg['run_id']} discovery: {counts}")


if __name__ == "__main__":
    main()
