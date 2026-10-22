"""Prepare reproducible benchmark ID lists."""

from datetime import datetime
import random
import hashlib
import re
import xml.etree.cElementTree as ET
from pathlib import Path
from datetime import timezone
from uuid import uuid4

import json

from .metrics import write_run_report
import tarfile



def parse_month(value: str) -> tuple[int, int]:
    """Parse a month written exactly as YYYY-MM."""
    parsed = datetime.strptime(value, "%Y-%m")

    if parsed.strftime("%Y-%m") != value:
        raise ValueError("Month must use YYYY-MM")

    if parsed.year < 2015:
        raise ValueError("This helper supports arXiv IDs from 2015 onward")

    return parsed.year, parsed.month


def expand_range(
    specification: dict,
    month_last_ids: dict[str, int],
) -> list[str]:
    """List inclusive candidate IDs; existence is checked separately."""
    start = parse_month(specification["start_month"])
    end = parse_month(specification["end_month"])
    start_id = specification["start_id"]
    end_id = specification["end_id"]

    for number in (start_id, end_id):
        if isinstance(number, bool) or not isinstance(number, int):
            raise TypeError("ID sequence numbers must be integers")
        if not 1 <= number <= 99999:
            raise ValueError("ID sequence numbers must be in 1..99999")

    if start > end or (start == end and start_id > end_id):
        raise ValueError("Range start must not exceed its end")

    ids = []
    year, month = start

    while (year, month) <= end:
        month_key = f"{year:04d}-{month:02d}"
        first = start_id if (year, month) == start else 1

        if (year, month) == end:
            last = end_id
        else:
            if month_key not in month_last_ids:
                raise ValueError(
                    f"Verified last ID required for {month_key}"
                )

            last = month_last_ids[month_key]
            if isinstance(last, bool) or not isinstance(last, int):
                raise TypeError("Monthly last ID must be an integer")
            if not 1 <= last <= 99999:
                raise ValueError("Monthly last ID must be in 1..99999")

        if first > last:
            raise ValueError(f"Invalid sequence bounds for {month_key}")

        prefix = f"{year % 100:02d}{month:02d}"
        ids.extend(
            f"{prefix}.{number:05d}"
            for number in range(first, last + 1)
        )

        year, month = (
            (year + 1, 1) if month == 12 else (year, month + 1)
        )

    return ids

def select_sample(
    candidate_ids: list[str],
    sample_size: int,
    seed: int,
) -> list[str]:
    """Select unique IDs reproducibly from a fixed candidate list."""
    if any(
        not isinstance(paper_id, str) or not paper_id.strip()
        for paper_id in candidate_ids
    ):
        raise ValueError("Candidate IDs must be non-empty strings")

    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Candidate IDs must be unique")

    if isinstance(sample_size, bool) or not isinstance(sample_size, int):
        raise TypeError("Sample size must be an integer")

    if not 0 <= sample_size <= len(candidate_ids):
        raise ValueError("Sample size must be within candidate count")

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("Seed must be an integer")

    generator = random.Random(seed)
    selected = generator.sample(sorted(candidate_ids), sample_size)
    return sorted(selected)

def select_benchmark(
    config: dict,
    month_last_ids: dict[str, int],
) -> dict[str, list[str]]:
    """Select one reproducible sample per range."""
    if config.get("bounds_inclusive") is not True:
        raise ValueError("Only inclusive bounds are supported")

    seed = config["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("Seed must be an integer")

    benchmark_size = config["benchmark_size"]
    if (
        isinstance(benchmark_size, bool)
        or not isinstance(benchmark_size, int)
    ):
        raise TypeError("Benchmark size must be an integer")
    if benchmark_size < 1:
        raise ValueError("Benchmark size must be positive")

    selected = {}
    seen_candidates = set()

    for specification in config["ranges"]:
        range_id = specification["range_id"]

        if not isinstance(range_id, str) or not range_id.strip():
            raise ValueError("Range ID must be a non-empty string")
        if range_id in selected:
            raise ValueError("Range IDs must be unique")

        candidates = expand_range(specification, month_last_ids)

        if seen_candidates.intersection(candidates):
            raise ValueError("Candidate ranges must not overlap")
        seen_candidates.update(candidates)

        seed_material = f"{seed}:{range_id}".encode("utf-8")
        range_seed = int.from_bytes(
            hashlib.sha256(seed_material).digest(),
            byteorder="big",
        )

        selected[range_id] = select_sample(
            candidates,
            sample_size=specification["sample_size"],
            seed=range_seed,
        )

    actual_size = sum(len(ids) for ids in selected.values())
    if actual_size != benchmark_size:
        raise ValueError(
            f"Expected {benchmark_size} selected IDs, got {actual_size}"
        )

    return selected

def select_pilot(
    benchmark: dict[str, list[str]],
    pilot_size: int,
    seed: int,
) -> dict[str, list[str]]:
    """Select an equally allocated pilot from the benchmark."""
    if isinstance(pilot_size, bool) or not isinstance(pilot_size, int):
        raise TypeError("Pilot size must be an integer")

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("Seed must be an integer")

    if not benchmark:
        raise ValueError("Benchmark must contain at least one range")

    if pilot_size < 1 or pilot_size % len(benchmark) != 0:
        raise ValueError(
            "Pilot size must be positive and divisible by range count"
        )

    per_range = pilot_size // len(benchmark)
    selected = {}
    seen_ids = set()

    for range_id, candidate_ids in benchmark.items():
        if not isinstance(range_id, str) or not range_id.strip():
            raise ValueError("Range ID must be a non-empty string")

        if seen_ids.intersection(candidate_ids):
            raise ValueError("Benchmark ranges must not share IDs")
        seen_ids.update(candidate_ids)

        seed_material = f"{seed}:pilot:{range_id}".encode("utf-8")
        range_seed = int.from_bytes(
            hashlib.sha256(seed_material).digest(),
            byteorder="big",
        )

        selected[range_id] = select_sample(
            candidate_ids,
            sample_size=per_range,
            seed=range_seed,
        )

    return selected

def parse_discovery_response(
    xml_data: bytes,
    requested_ids: list[str],
) -> dict:
    """Parse an Atom response without treating absent IDs as nonexistent."""
    if len(requested_ids) != len(set(requested_ids)):
        raise ValueError("Requested IDs must be unique")

    root = ET.fromstring(xml_data)
    atom = "{http://www.w3.org/2005/Atom}"

    if root.tag != f"{atom}feed":
        raise ValueError("Expected an Atom feed")

    papers = {}
    unexpected_ids = []

    for entry in root.findall(f"{atom}entry"):
        identifier = entry.findtext(f"{atom}id", default="").strip()

        match = re.fullmatch(
            r"https?://arxiv\.org/abs/(\d{4}\.\d{5})v([1-9]\d*)",
            identifier,
        )
        if match is None:
            raise ValueError(f"Unexpected entry identifier: {identifier}")

        paper_id = match.group(1)
        latest_version = int(match.group(2))

        if paper_id not in requested_ids:
            unexpected_ids.append(paper_id)
            continue
        if paper_id in papers:
            raise ValueError(f"Duplicate returned ID: {paper_id}")

        title = " ".join(
            entry.findtext(f"{atom}title", default="").split()
        )
        published = entry.findtext(f"{atom}published", default="").strip()
        authors = [
            author.findtext(f"{atom}name", default="").strip()
            for author in entry.findall(f"{atom}author")
        ]
        authors = [name for name in authors if name]

        missing_fields = [
            name
            for name, value in {
                "title": title,
                "submission_date": published,
                "authors": authors,
            }.items()
            if not value
        ]

        papers[paper_id] = {
            "arxiv_id": paper_id,
            "title": title or None,
            "authors": authors,
            "submission_date": published or None,
            "latest_version": latest_version,
            "expected_versions": list(range(1, latest_version + 1)),
            "verification_status": (
                "metadata_incomplete" if missing_fields else "metadata_present"
            ),
            "missing_fields": missing_fields,
        }

    return {
        "papers": papers,
        "missing_ids": [
            paper_id for paper_id in requested_ids
            if paper_id not in papers
        ],
        "unexpected_ids": unexpected_ids,
    }

def save_discovery_batch(
    directory: str | Path,
    requested_ids: list[str],
    xml_data: bytes,
) -> Path:
    """Preserve one response and its parsed result in a unique directory."""
    if not isinstance(xml_data, bytes):
        raise TypeError("XML response must be bytes")

    ids = list(requested_ids)
    if any(not isinstance(pid, str) or not pid.strip() for pid in ids):
        raise ValueError("Requested IDs must be non-empty strings")
    if len(ids) != len(set(ids)):
        raise ValueError("Requested IDs must be unique")

    batch_dir = Path(directory) / f"batch-{uuid4().hex}"
    batch_dir.mkdir(parents=True, exist_ok=False)

    write_run_report(
        batch_dir / "request.json",
        {
            "requested_ids": ids,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    (batch_dir / "response.xml").write_bytes(xml_data)

    parsed = parse_discovery_response(xml_data, ids)
    write_run_report(batch_dir / "parsed.json", parsed)

    return batch_dir

def pending_discovery_ids(
    input_ids: list[str],
    batches_directory: str | Path,
) -> list[str]:
    """Return IDs without a saved, complete metadata record."""
    ids = list(input_ids)

    if any(not isinstance(pid, str) or not pid.strip() for pid in ids):
        raise ValueError("Input IDs must be non-empty strings")
    if len(ids) != len(set(ids)):
        raise ValueError("Input IDs must be unique")

    root = Path(batches_directory)
    if not root.exists():
        return ids
    if not root.is_dir():
        raise NotADirectoryError(str(root))

    completed = set()

    for batch_dir in sorted(root.glob("batch-*")):
        if not batch_dir.is_dir():
            continue

        required_files = [
            batch_dir / "request.json",
            batch_dir / "response.xml",
            batch_dir / "parsed.json",
        ]
        if not all(path.is_file() for path in required_files):
            continue

        request = json.loads(
            (batch_dir / "request.json").read_text(encoding="utf-8")
        )
        parsed = json.loads(
            (batch_dir / "parsed.json").read_text(encoding="utf-8")
        )

        requested = set(request["requested_ids"])

        for paper_id, record in parsed["papers"].items():
            if paper_id not in requested:
                raise ValueError(
                    f"Cached result was not requested: {paper_id}"
                )

            if record["verification_status"] == "metadata_present":
                completed.add(paper_id)

    return [paper_id for paper_id in ids if paper_id not in completed]

def inspect_tar_source(path: str | Path) -> dict:
    """Inspect a tar source without extracting files to disk."""
    path = Path(path)

    def file_sha256(handle):
        digest = hashlib.sha256()
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()

    with path.open("rb") as handle:
        archive_hash = file_sha256(handle)

    with tarfile.open(path, "r:*") as archive:
        members = archive.getmembers()
        regular_files = [member for member in members if member.isfile()]
        source_files = []

        for member in regular_files:
            extension = Path(member.name).suffix.lower()
            if extension not in {".tex", ".bib"}:
                continue

            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"Cannot read source member: {member.name}")

            with handle:
                content_hash = file_sha256(handle)

            source_files.append({
                "path": member.name,
                "bytes": member.size,
                "sha256": content_hash,
            })

    return {
        "inspection_method": "tar_regular_members",
        "archive_bytes": path.stat().st_size,
        "archive_sha256": archive_hash,
        "regular_file_count": len(regular_files),
        "unpacked_total_bytes": sum(m.size for m in regular_files),
        "kept_source_bytes": sum(item["bytes"] for item in source_files),
        "tex_count": sum(
            Path(item["path"]).suffix.lower() == ".tex"
            for item in source_files
        ),
        "bib_count": sum(
            Path(item["path"]).suffix.lower() == ".bib"
            for item in source_files
        ),
        "contains_links": any(
            member.issym() or member.islnk() for member in members
        ),
        "source_files": sorted(source_files, key=lambda item: item["path"]),
    }

def inspect_reference_page(payload: dict, expected_offset: int) -> dict:
    """Validate and count one raw Semantic Scholar reference page."""
    if not isinstance(payload, dict):
        raise ValueError("Reference page must be an object")

    offset = payload.get("offset")
    if type(offset) is not int or offset != expected_offset:
        raise ValueError("Unexpected reference page offset")

    entries = payload.get("data")
    if not isinstance(entries, list):
        raise ValueError("Reference page data must be a list")

    next_offset = payload.get("next")
    if next_offset is not None:
        if type(next_offset) is not int or next_offset <= offset:
            raise ValueError("Next offset must move forward")
        if not entries:
            raise ValueError("Empty page cannot have a next offset")

    resolved_count = 0
    with_arxiv_id_count = 0

    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Reference entries must be objects")

        paper = entry.get("citedPaper")
        if paper is None:
            continue
        if not isinstance(paper, dict):
            raise ValueError("citedPaper must be an object or null")

        resolved_count += 1
        external_ids = paper.get("externalIds")
        arxiv_id = (
            external_ids.get("ArXiv")
            if isinstance(external_ids, dict)
            else None
        )
        if isinstance(arxiv_id, str) and arxiv_id.strip():
            with_arxiv_id_count += 1

    return {
        "offset": offset,
        "next_offset": next_offset,
        "raw_entry_count": len(entries),
        "resolved_entry_count": resolved_count,
        "unresolved_entry_count": len(entries) - resolved_count,
        "with_arxiv_id_count": with_arxiv_id_count,
        "is_last_page": next_offset is None,
    }