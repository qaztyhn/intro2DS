"""D1 discovery using arXiv OAI-PMH (arXivRaw), compatible with Intro2DS baseline.

Only replaces metadata discovery. It uses the shared HTTP retry/throttle layer and
writes the EXACT metadata contract expected by downloader, storage and validation.
"""

from __future__ import annotations

import contextlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .. import metrics
from ..utils.common import FAILED, OK, SKIPPED, Stopwatch, paper_key, read_json, write_json_atomic
from ..schema import valid_arxiv_id
from ..stages.validation import validate_metadata
from ..utils.errors import CrawlError, INVALID_ID, INVALID_RESPONSE, NOT_FOUND
from ..utils.http import request_text

OAI_URL = "https://oaipmh.arxiv.org/oai"
OAI_NS = "http://www.openarchives.org/OAI/2.0/"
RAW_NS = "http://arxiv.org/OAI/arXivRaw/"
STAGE = "discovery"


def _text(parent: ET.Element, name: str) -> str | None:
    value = parent.findtext(f"{{{RAW_NS}}}{name}")
    return " ".join(value.split()) if value and value.strip() else None


def _date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        date = parsedate_to_datetime(value)
    except (ValueError, TypeError, IndexError):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CrawlError(f"Invalid OAI version date: {value}", INVALID_RESPONSE) from exc
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc).isoformat(timespec="seconds")


def _authors(value: str) -> list[str]:
    """Split common raw separators, keeping commas inside affiliations intact.

    arXivRaw is free text, not structured author names. Preserve authors_raw too;
    do not discard affiliations or guess missing names.
    """
    parts, start, depth = [], 0, 0
    for match in re.finditer(r"[(){}\[\]]|,|\s+and\s+", value, flags=re.I):
        token = match.group()
        if token in ("(", "{", "["):
            depth += 1
        elif token in (")", "}", "]"):
            depth = max(0, depth - 1)
        elif depth == 0:
            part = value[start:match.start()].strip()
            if part:
                parts.append(part)
            start = match.end()
    part = value[start:].strip()
    if part:
        parts.append(part)
    return parts


def parse_record(xml: str, arxiv_id: str) -> dict[str, Any]:
    """Parse a single GetRecord response into baseline metadata.json schema."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise CrawlError(f"OAI XML parse error: {exc}", INVALID_RESPONSE) from exc

    if root.tag != f"{{{OAI_NS}}}OAI-PMH":
        raise CrawlError("Missing OAI-PMH response envelope", INVALID_RESPONSE)

    error = root.find(f"{{{OAI_NS}}}error")
    if error is not None:
        code = error.attrib.get("code", "")
        kind = NOT_FOUND if code == "idDoesNotExist" else INVALID_RESPONSE
        raise CrawlError(f"OAI error {code}: {error.text or ''}", kind)

    records = root.findall(f"{{{OAI_NS}}}GetRecord/{{{OAI_NS}}}record")
    if len(records) != 1:
        raise CrawlError("Expected one OAI GetRecord record", INVALID_RESPONSE)
    envelope = records[0]
    header = envelope.find(f"{{{OAI_NS}}}header")
    if header is None:
        raise CrawlError("Missing OAI record header", INVALID_RESPONSE)
    if header.findtext(f"{{{OAI_NS}}}identifier") != f"oai:arXiv.org:{arxiv_id}":
        raise CrawlError("OAI header ID mismatch", INVALID_RESPONSE)
    if header.attrib.get("status") == "deleted":
        raise CrawlError("OAI record is deleted", NOT_FOUND)

    record = envelope.find(f"{{{OAI_NS}}}metadata/{{{RAW_NS}}}arXivRaw")
    if record is None:
        raise CrawlError("Missing OAI arXivRaw record", INVALID_RESPONSE)
    returned = _text(record, "id")
    if returned != arxiv_id:
        raise CrawlError(f"OAI ID mismatch: expected {arxiv_id}, got {returned}", INVALID_RESPONSE)

    version_dates: dict[int, str | None] = {}
    for node in record.findall(f"{{{RAW_NS}}}version"):
        match = re.fullmatch(r"v([1-9]\d*)", node.attrib.get("version", ""))
        if not match:
            raise CrawlError("Invalid OAI version label", INVALID_RESPONSE)
        number = int(match.group(1))
        if number in version_dates:
            raise CrawlError("Duplicate OAI version", INVALID_RESPONSE)
        version_dates[number] = _date(_text(node, "date"))

    versions = sorted(version_dates)
    if not versions or versions != list(range(1, versions[-1] + 1)):
        raise CrawlError("Incomplete OAI version history", INVALID_RESPONSE)
    if version_dates[1] is None:
        raise CrawlError("Missing v1 submission date", INVALID_RESPONSE)
    title = _text(record, "title")
    if title is None:
        raise CrawlError("Missing title", INVALID_RESPONSE)

    authors_raw = _text(record, "authors")
    authors = _authors(authors_raw or "")
    if not authors:
        raise CrawlError("Missing authors", INVALID_RESPONSE)
    revised = [version_dates[v] for v in versions[1:]]
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "submission_date": version_dates[1],
        "revised_dates": revised if all(revised) else None,
        "venue": _text(record, "journal-ref"),
        "latest_version": versions[-1],
        "versions": versions,
        "status": OK,
        "error_code": None,
        # Supplemental fields; canonical required fields above remain flat.
        "authors_raw": authors_raw,
        "discovery_source": "oai_pmh_arXivRaw",
        "version_history": [
            {"version": v, "versioned_id": f"{arxiv_id}v{v}", "submitted_at": version_dates[v]}
            for v in versions
        ],
    }


def _complete_metadata(record, arxiv_id) -> bool:
    """Only resume matching, schema-valid metadata with all required dates."""
    return (
        isinstance(record, dict)
        and record.get("status") == OK
        and record.get("error_code") is None
        and not validate_metadata(record, arxiv_id=arxiv_id)
        and all(isinstance(author, str) and author.strip() for author in record["authors"])
        and record["versions"] == list(range(1, record["latest_version"] + 1))
    )


def discover_paper(cfg, arxiv_id, client, opts, throttle, logger, metrics_logger=None) -> str:
    """Baseline-compatible discovery; one timed operation and one logical request.

    The shared HTTP layer owns per-attempt throttling, timeout and retry/backoff.
    As in B, a cache hit emits only a skipped run event, not another measurement.
    """
    path = cfg["output_root"] / cfg["run_id"] / "papers" / paper_key(arxiv_id) / "metadata.json"
    existing = read_json(path)
    if _complete_metadata(existing, arxiv_id):
        logger.log(STAGE, SKIPPED, arxiv_id=arxiv_id, message="metadata already ok")
        return SKIPPED

    watch = Stopwatch()
    result = {
        "arxiv_id": arxiv_id, "title": None, "authors": None,
        "submission_date": None, "revised_dates": None, "venue": None,
        "latest_version": None, "versions": None,
        "status": FAILED, "error_code": None,
        "discovery_source": "oai_pmh_arXivRaw",
    }
    stage_cm = contextlib.nullcontext()
    paper_opts = opts
    if metrics_logger is not None:
        stage_cm = metrics.measure_stage(metrics_logger, STAGE, arxiv_id=arxiv_id)
        # A fresh hook per GetRecord call: retries share its request_id, papers do not.
        paper_opts = {
            **opts,
            "on_attempt": metrics.make_request_hook(metrics_logger, STAGE, arxiv_id=arxiv_id),
        }
    try:
        with stage_cm:
            if not valid_arxiv_id(arxiv_id):
                raise CrawlError("Give a valid base arXiv ID without vN", INVALID_ID)
            content = request_text(
                OAI_URL,
                params={"verb": "GetRecord", "identifier": f"oai:arXiv.org:{arxiv_id}",
                        "metadataPrefix": "arXivRaw"},
                **paper_opts,
            )
            result = parse_record(content, arxiv_id)
        if result["revised_dates"] is None:
            logger.log(STAGE, OK, arxiv_id=arxiv_id,
                       error_code=INVALID_RESPONSE,
                       message="Some revision dates are missing",
                       elapsed_seconds=watch.elapsed)
        else:
            logger.log(STAGE, OK, arxiv_id=arxiv_id, elapsed_seconds=watch.elapsed)
        status = OK
    except CrawlError as exc:
        result["error_code"] = exc.error_code
        status = FAILED
        logger.log(STAGE, FAILED, arxiv_id=arxiv_id, error_code=exc.error_code,
                   http_status=exc.status_code, message=str(exc)[:300], elapsed_seconds=watch.elapsed)
    except Exception as exc:
        result["error_code"] = INVALID_RESPONSE
        status = FAILED
        logger.log(STAGE, FAILED, arxiv_id=arxiv_id, error_code=INVALID_RESPONSE,
                   message=f"{type(exc).__name__}: {exc}"[:300], elapsed_seconds=watch.elapsed)
    write_json_atomic(path, result)
    return status
