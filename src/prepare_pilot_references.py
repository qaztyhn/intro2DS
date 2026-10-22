"""Prepare raw reference snapshots for the fixed pilot."""

import hashlib
import json
import os
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import certifi

from .benchmark import inspect_reference_page
from .metrics import write_run_report
from email.utils import parsedate_to_datetime


ROOT = Path("experiments/benchmark-preparation")
FIELDS = "title,authors,externalIds,year,publicationDate,venue"
LIMIT = 100

def rate_limit_delay(headers) -> float:
    """Wait at least 30 seconds, respecting a longer Retry-After."""
    value = (headers.get("Retry-After") or "").strip()

    if value.isdecimal():
        return max(30.0, float(value))

    if value:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is not None:
                remaining = (
                    retry_at - datetime.now(timezone.utc)
                ).total_seconds()
                return max(30.0, remaining)
        except (ValueError, TypeError, OverflowError):
            pass

    return 30.0


def prepare_one(paper_id, key, context):
    paper_dir = ROOT / "reference-inspection" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)

    lookup_path = paper_dir / "lookup-status.json"
    if lookup_path.is_file():
        lookup = json.loads(lookup_path.read_text(encoding="utf-8"))
        if lookup.get("status") == "not_found":
            print("Previously not found; pending later investigation:", paper_id)
            return

    candidates = []
    for directory in paper_dir.glob("snapshot-*"):
        request_path = directory / "request.json"
        if not request_path.is_file():
            continue

        request_info = json.loads(request_path.read_text(encoding="utf-8"))
        params = request_info.get("params", {})
        if (
            request_info.get("arxiv_id") == paper_id
            and params.get("fields") == FIELDS
            and params.get("limit") == LIMIT
            and params.get("offset") == 0
        ):
            candidates.append(directory)

    # Prefer a completed snapshot, otherwise continue the latest partial one.
    completed = [
        directory for directory in candidates
        if (directory / "snapshot.json").is_file()
    ]
    if completed:
        summary = json.loads(
            (completed[0] / "snapshot.json").read_text(encoding="utf-8")
        )
        if (
            summary.get("arxiv_id") != paper_id
            or summary.get("status") != "pages_complete"
        ):
            raise ValueError(f"Invalid snapshot summary for {paper_id}")

        print("Existing complete snapshot:", paper_id)
        return

    if candidates:
        directory = max(candidates, key=lambda path: path.stat().st_mtime)
    else:
        directory = paper_dir / f"snapshot-{uuid4().hex}"
        directory.mkdir()
        write_run_report(
            directory / "request.json",
            {
                "arxiv_id": paper_id,
                "endpoint": (
                    "https://api.semanticscholar.org/graph/v1/paper/"
                    f"ARXIV:{paper_id}/references"
                ),
                "params": {"fields": FIELDS, "offset": 0, "limit": LIMIT},
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    offset = 0
    pages = []

    while True:
        page_path = directory / f"page-{offset:06d}.json"

        if page_path.is_file():
            body = page_path.read_bytes()
            print("  Cached page:", offset)
        else:
            params = {"fields": FIELDS, "offset": offset, "limit": LIMIT}
            url = (
                "https://api.semanticscholar.org/graph/v1/paper/"
                f"ARXIV:{paper_id}/references?"
                + urlencode(params)
            )
            request = Request(
                url,
                headers={
                    "x-api-key": key,
                    "User-Agent": "intro2DS-benchmark-check/1.0",
                },
            )

            time.sleep(30)
            with urlopen(request, timeout=60, context=context) as response:
                body = response.read()

            # Preserve the response before parsing it.
            temporary = page_path.with_suffix(".part")
            temporary.write_bytes(body)
            temporary.replace(page_path)
            print("  Downloaded page:", offset)

        summary = inspect_reference_page(json.loads(body), offset)
        pages.append({
            "file": page_path.name,
            "sha256": hashlib.sha256(body).hexdigest(),
            **summary,
        })

        if summary["is_last_page"]:
            break
        offset = summary["next_offset"]

    result = {
        "arxiv_id": paper_id,
        "status": "pages_complete",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "page_count": len(pages),
        "pages": pages,
    }
    for field in (
        "raw_entry_count",
        "resolved_entry_count",
        "unresolved_entry_count",
        "with_arxiv_id_count",
    ):
        result[field] = sum(page[field] for page in pages)

    write_run_report(directory / "snapshot.json", result)
    print("  Complete; raw references:", result["raw_entry_count"])

def main():
    key = os.getenv("S2_API_KEY", "").strip()
    if not key:
        raise ValueError("S2_API_KEY is not configured")

    manifest = json.loads(
        (ROOT / "selection-v1/pilot-source-manifest-v1.json").read_text(
            encoding="utf-8"
        )
    )
    pilot = [
        item["arxiv_id"]
        for item in manifest["records"]
        if item["is_pilot"]
    ]
    context = ssl.create_default_context(cafile=certifi.where())

    for index, paper_id in enumerate(pilot, start=1):
        print(f"\n{index}/{len(pilot)}: {paper_id}")
        rate_limit_retries = 0

        while True:
            try:
                prepare_one(paper_id, key, context)
                break

            except HTTPError as error:
                status = error.code
                delay = rate_limit_delay(error.headers)

                print("HTTP:", status)
                print(
                    "Retry-After:",
                    error.headers.get("Retry-After"),
                )

                try:
                    message = error.read(1000).decode(
                        "utf-8", errors="replace"
                    )
                finally:
                    error.close()

                print(message)

                if status == 404:
                    write_run_report(
                        ROOT / "reference-inspection"
                        / paper_id / "lookup-status.json",
                        {
                            "arxiv_id": paper_id,
                            "status": "not_found",
                            "http_status": 404,
                            "checked_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                            "message": message,
                        },
                    )
                    print(
                        "Recorded as not_found; "
                        "references remain unverified."
                    )
                    break

                if status != 429:
                    print("Stopped; saved pages are retained.")
                    return

                if rate_limit_retries >= 3:
                    print(
                        "Stopped after 3 additional retries "
                        "for this paper."
                    )
                    return

                rate_limit_retries += 1
                print(
                    f"Retry {rate_limit_retries}/3 "
                    f"after {delay:.1f} seconds."
                )
                time.sleep(delay)

            except Exception as error:
                print(type(error).__name__, str(error))
                print(
                    "Stopped; inspect saved data "
                    "before continuing."
                )
                return

    print("\nFinished checking all pilot papers.")

if __name__ == "__main__":
    main()