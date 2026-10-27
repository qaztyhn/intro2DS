# Discover paper metadata from the Kaggle arXiv JSONL snapshot.
# Scan the snapshot once for the configured IDs, then use the in-memory index
# to write metadata.json and log a discovery result for each paper.
import json
import re
import kagglehub
from datetime import timezone
from contextlib import nullcontext
from .. import metrics
from email.utils import parsedate_to_datetime
from pathlib import Path
from ..stages.validation import validate_metadata
from ..utils.common import (
    FAILED, OK, SKIPPED,
    Stopwatch, load_ids, paper_key,
    read_json, write_json_atomic,
)
from ..config import PROJECT_ROOT, http_options
from ..utils.errors import INVALID_ID, INVALID_RESPONSE, NOT_FOUND, CrawlError
from ..utils.http import Throttle

STAGE = "discovery"
RAW_DIR = PROJECT_ROOT / "temp" / "data" / "raw"
SNAPSHOT_NAME = "arxiv-metadata-oai-snapshot.json"
# Fast path for snapshot lines whose first field is "id".
_ID_PREFIX = re.compile(r'^\s*\{\s*"id"\s*:\s*"([^"]+)"')


def download_kaggle_metadata():

    # Download only the metadata snapshot into the project's raw-data cache.
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(kagglehub.dataset_download(
        "Cornell-University/arxiv",
        path=SNAPSHOT_NAME,
        output_dir=str(RAW_DIR),
    ))
    return path


def to_iso_date(value):
    # Convert snapshot date strings to UTC ISO timestamps; invalid values stay None.
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            # Treat dates without an explicit timezone as UTC.
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, IndexError):
        return None


def _line_id(line):
    # Extract the leading ID without decoding the entire JSON record when possible.
    match = _ID_PREFIX.match(line)
    if match:
        return match.group(1)

    # Fall back to JSON parsing if whitespace or field order does not match the prefix.
    return json.loads(line).get("id")

def load_kaggle_index(metadata_path, target_ids):
    # Keep only requested papers in memory rather than loading the full snapshot.
    wanted = set(target_ids)
    index = {}
    if not wanted:
        return index

    # The snapshot is JSONL: stream one paper record at a time.
    with Path(metadata_path).open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            arxiv_id = _line_id(line)
            if arxiv_id in wanted and arxiv_id not in index:
                # Keep the first record for each ID and stop once all IDs are found.
                index[arxiv_id] = json.loads(line)
                if len(index) == len(wanted):
                    break 
    return index


def normalize_kaggle_record(raw):
    # Map Kaggle fields to the metadata schema shared by the discovery sources.
    arxiv_id = raw["id"]

    # Parse vN labels and dates, rejecting invalid or duplicate version numbers.
    version_dates = {}
    for item in raw.get("versions") or []:
        version_text = item.get("version", "")
        if (
            not isinstance(version_text, str)
            or not version_text.startswith("v")
            or not version_text[1:].isdigit()
        ):
            raise ValueError("Invalid Kaggle version")

        number = int(version_text[1:])
        if number < 1 or number in version_dates:
            raise ValueError("Invalid or duplicated version")

        date = to_iso_date(item.get("created"))
        version_dates[number] = date

    if not version_dates:
        raise ValueError("No versions available")

    latest_version = max(version_dates)
    # Build the expected consecutive version numbers from v1 to the latest version.
    versions = list(range(1, latest_version + 1))

    # Reject histories with a missing first version or gaps between versions.
    if sorted(version_dates) != versions:
        raise ValueError("Missing versions")

    # Collect revision dates in version order; v1 is the submission date.
    revised_dates = [version_dates[v] for v in versions[1:]]

    # Mark incomplete revision dates as unknown; keep [] for papers with only v1.
    if any(date is None for date in revised_dates):
        revised_dates = None

    # Kaggle stores names as [last, first, suffix]; output readable full names.
    authors = []
    for author in raw.get("authors_parsed") or []:
        if not isinstance(author, (list, tuple)):
            continue
        last, first, suffix = (list(author) + ["", ""])[:3]

        name = " ".join(
            str(p).strip()
            for p in (first, last, suffix)
            if p and str(p).strip()
        )
        if name:
            authors.append(name)

    # Collapse embedded newlines and repeated whitespace in snapshot titles.
    title = raw.get("title")
    title = " ".join(title.split()) if isinstance(title, str) else None

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "submission_date": version_dates[1],
        "revised_dates": revised_dates,
        "venue": raw.get("journal-ref") or None,
        "latest_version": latest_version,
        "versions": versions,
        "status": OK,
        "error_code": None,
    }


def paper_dir(cfg, arxiv_id):
    # Use the same run layout and filesystem-safe paper key as the arXiv source.
    return cfg["output_root"] / cfg["run_id"] / "papers" / paper_key(arxiv_id)


def make_clients(cfg, throttle=None):
    # Reuse an existing snapshot; cached files are checked for validity below.
    snapshot = RAW_DIR / SNAPSHOT_NAME
    if not snapshot.is_file():
        snapshot = download_kaggle_metadata()

    if not snapshot.is_file() or snapshot.stat().st_size == 0:
        raise RuntimeError(f"Invalid Kaggle snapshot: {snapshot}")

    # Build the lookup table once for all papers in this run.
    index = load_kaggle_index(
        snapshot,
        load_ids(cfg["ids_file"])
    )

    # Preserve the client/options/throttle interface used by the shared pipeline.
    throttle = throttle or Throttle(cfg["arxiv_delay_seconds"])

    return index, http_options(cfg, throttle), throttle

def discover_paper(cfg, arxiv_id, client, opts, throttle, logger, metrics_logger=None):
    
    path = paper_dir(cfg, arxiv_id) / "metadata.json"
    existing = read_json(path)
    # Reuse successful metadata when revision dates are present or unnecessary.
    if existing and existing.get("status") == OK:
        logger.log(
            STAGE, SKIPPED,
            arxiv_id=arxiv_id,
            message="metadata already ok"
        )
        return SKIPPED

    watch = Stopwatch()
    http_status = None
    stage_cm = (
        metrics.measure_stage(
            metrics_logger,
            STAGE,
            arxiv_id=arxiv_id
        )
        if metrics_logger is not None
        else nullcontext()
    )
    try:
        with stage_cm:
            # The snapshot index uses base IDs, not IDs with a specific vN suffix.
            if re.search(r"v\d+$", arxiv_id):
                raise CrawlError(f"{arxiv_id!r}: give the base id without a vN suffix", INVALID_ID)
            raw = client.get(arxiv_id)
            if raw is None:
                # A valid ID can be absent if the snapshot predates the paper.
                raise CrawlError(f"{arxiv_id} not in Kaggle snapshot (snapshot may predate it)", NOT_FOUND)
            record = normalize_kaggle_record(raw)

            # Only validation errors fail discovery; warnings do not reject the record.
            issues = validate_metadata(record, arxiv_id=arxiv_id)

            errors = [
                issue for issue in issues
                if issue["severity"] == "error"
            ]

            if errors:
                raise CrawlError(
                    f"Invalid metadata: {errors[:3]}",
                    INVALID_RESPONSE,
                )

            status, code, msg = OK, None, "source=kaggle"
    except Exception as exc:
        # Save a failure record for this paper so the pipeline can continue.
        code = exc.error_code if isinstance(exc, CrawlError) else INVALID_RESPONSE

        record = {
            k: None
            for k in (
                "title", "authors", "submission_date",
                "revised_dates", "venue",
                "latest_version", "versions"
            )
        }
        record.update(
            arxiv_id=arxiv_id,
            status=FAILED,
            error_code=code,
        )

        http_status = getattr(exc, "status_code", None)
        status = FAILED
        msg = f"{type(exc).__name__}: {exc}"[:300]

    # Persist either outcome atomically, then record its status and elapsed time.
    write_json_atomic(path, record)

    logger.log(
        STAGE, status,
        arxiv_id=arxiv_id,
        error_code=code,
        http_status=http_status,
        message=msg,
        elapsed_seconds=watch.elapsed,
    )

    return status
