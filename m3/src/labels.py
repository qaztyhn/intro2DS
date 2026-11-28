"""Offline TV2 labeling tools using the shared M3 v6 contract."""

from __future__ import annotations

import csv
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from .contracts import (
    KEYS,
    USABLE_CRAWL,
    auto_quota,
    make_table,
    normalize_arxiv_id,
    check_manual_quota as contract_quota,
    validate_registry,
    validate_table,
)


def canonical_table(name, table):
    """Normalize join IDs and validate after normalization to catch collisions."""
    validate_table(name, table)
    rows = table.to_pylist()
    for row in rows:
        if "member_id" in row and not str(row["member_id"] or "").strip():
            raise ValueError(f'[{name}] empty member_id for {row.get("arxiv_id")}')
        for column in ("arxiv_id", "ref_arxiv_id", "groundtruth_arxiv_id"):
            if column in row:
                value = normalize_arxiv_id(row[column])
                if value is None:
                    raise ValueError(f"[{name}] invalid {column}: {row[column]!r}")
                row[column] = value
    result = pa.Table.from_pylist(rows, schema=table.schema)
    validate_table(name, result)
    return result


def normalize_publications(values):
    ids = [normalize_arxiv_id(value) for value in values]
    if None in ids or len(ids) != len(set(ids)):
        raise ValueError("[publications] invalid or duplicate ID")
    return sorted(ids)


def _sorted_table(name, rows):
    return make_table(
        name, sorted(rows, key=lambda row: tuple(row[k] for k in KEYS[name]))
    )


def _context(queries, pairs, crawl_status):
    queries = canonical_table("queries", queries)
    pairs = canonical_table("pairs", pairs)
    crawl_status = canonical_table("crawl_status", crawl_status)
    keys = set(
        zip(queries["arxiv_id"].to_pylist(), queries["citation_key"].to_pylist())
    )
    for row in pairs.select(KEYS["pairs"]).to_pylist():
        key = row["arxiv_id"], row["citation_key"]
        if key not in keys:
            raise ValueError(f"[pairs] unknown query {key}")
    usable = {
        r["arxiv_id"] for r in crawl_status.to_pylist() if r["status"] in USABLE_CRAWL
    }
    return keys, pairs, usable


@dataclass(frozen=True)
class LabelingPacket:
    entries_path: Path
    candidates_path: Path


def write_labeling_packet(*, bibtex, references, publication_ids, output_dir):
    bibtex = canonical_table("bibtex", bibtex)
    references = canonical_table("references", references)
    selected = set(normalize_publications(publication_ids))
    known = set(bibtex["arxiv_id"].to_pylist())
    if selected - known:
        raise ValueError(
            f"[bibtex] missing selected publications: {sorted(selected - known)}"
        )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    entries = [
        "arxiv_id",
        "citation_key",
        "title",
        "authors",
        "year",
        "doi",
        "url",
        "groundtruth_arxiv_id",
        "member_id",
    ]
    candidates = [
        "arxiv_id",
        "ref_arxiv_id",
        "ref_title",
        "ref_authors",
        "ref_submission_date",
    ]
    for filename, table, columns, keys in (
        ("entries.csv", bibtex, entries, KEYS["bibtex"]),
        ("candidates.csv", references, candidates, KEYS["references"]),
    ):
        with (output / filename).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            rows = sorted(table.to_pylist(), key=lambda r: tuple(r[k] for k in keys))
            for row in rows:
                if row["arxiv_id"] in selected:
                    writer.writerow(row)
    return LabelingPacket(output / "entries.csv", output / "candidates.csv")


def load_manual_labels(
    csv_paths,
    *,
    queries,
    pairs,
    crawl_status,
    assignments=None,
    manual_publications=None,
):
    keys, pairs, usable = _context(queries, pairs, crawl_status)
    candidates = {tuple(r[k] for k in KEYS["pairs"]) for r in pairs.to_pylist()}
    rows = {}
    for path in csv_paths:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"arxiv_id", "citation_key", "groundtruth_arxiv_id", "member_id"}
            if not required <= set(reader.fieldnames or []):
                raise ValueError(f"[labels] {path}: missing columns {sorted(required)}")
            for raw in reader:
                truth = (raw["groundtruth_arxiv_id"] or "").strip()
                member = (raw["member_id"] or "").strip()
                if not truth and not member:
                    continue
                key = normalize_arxiv_id(raw["arxiv_id"]), raw["citation_key"]
                target = normalize_arxiv_id(truth)
                if not member or target is None:
                    raise ValueError(
                        f"[labels] {key}: both label fields must be filled"
                    )
                if key not in keys or (*key, target) not in candidates:
                    raise ValueError(
                        f"[labels] {key}: target {truth!r} is not a candidate"
                    )
                if key[0] not in usable:
                    raise ValueError(f"[labels] {key}: unusable crawl")
                row = dict(
                    arxiv_id=key[0],
                    citation_key=key[1],
                    groundtruth_arxiv_id=target,
                    label_source="manual",
                    member_id=member,
                )
                if key in rows and rows[key] != row:
                    raise ValueError(f"[labels] conflicting labels for {key}")
                rows[key] = row
    result = _sorted_table("labels", list(rows.values()))
    if (assignments is None) != (manual_publications is None):
        raise ValueError("[labels] supply assignments and manual_publications together")
    if assignments is not None:
        owners = canonical_table("assignments", assignments)
        manual = canonical_table("manual_publications", manual_publications)
        validate_registry(owners, manual, make_table("auto_publications", []))
        allowed = {(r["arxiv_id"], r["member_id"]) for r in manual.to_pylist()}
        for row in result.to_pylist():
            if (row["arxiv_id"], row["member_id"]) not in allowed:
                raise ValueError(
                    f"[labels] unregistered manual owner: {row['arxiv_id']}"
                )
    return result


def check_manual_quota(
    labels, manual, assignments, *, manual_papers=5, min_pairs=20, min_total=100
):
    labels = canonical_table("labels", labels)
    manual = canonical_table("manual_publications", manual)
    assignments = canonical_table("assignments", assignments)
    allowed = {(r["arxiv_id"], r["member_id"]) for r in manual.to_pylist()}
    for row in labels.to_pylist():
        if (
            row["label_source"] == "manual"
            and (row["arxiv_id"], row["member_id"]) not in allowed
        ):
            raise ValueError(f"[labels] wrong manual owner for {row['arxiv_id']}")
    return contract_quota(
        labels,
        manual,
        assignments,
        manual_papers=manual_papers,
        min_pairs=min_pairs,
        min_total=min_total,
    )


def choose_auto_publications(
    *,
    assigned_publication_ids,
    manual_labels,
    queries,
    crawl_status,
    pairs,
    fraction,
    seed,
    manual_publications,
):
    assigned = set(normalize_publications(assigned_publication_ids))
    manual = canonical_table("manual_publications", manual_publications)
    labels = canonical_table("labels", manual_labels)
    registered = set(manual["arxiv_id"].to_pylist())
    if any(r["label_source"] != "manual" for r in labels.to_pylist()):
        raise ValueError("[labels] manual_labels contains auto labels")
    if not set(labels["arxiv_id"].to_pylist()) <= registered:
        raise ValueError("[labels] manual labels outside registry")
    if not math.isfinite(fraction) or not 0 <= fraction <= 1:
        raise ValueError("[quota] fraction must be in [0, 1]")
    keys, pairs, usable = _context(queries, pairs, crawl_status)
    candidates = set(pairs["arxiv_id"].to_pylist())
    eligible = sorted(
        (assigned - registered) & {a for a, _ in keys} & usable & candidates
    )
    count = auto_quota(len(assigned), len(assigned & registered), fraction)
    if len(eligible) < count:
        raise ValueError(
            f"[quota] need {count}, eligible {len(eligible)}, missing {count-len(eligible)}"
        )
    return sorted(random.Random(seed).sample(eligible, count))


def _apply_rule(*, queries, pairs, crawl_status, selected, owners, title_threshold):
    keys, pairs, usable = _context(queries, pairs, crawl_status)
    if not math.isfinite(title_threshold) or not 0 <= title_threshold <= 1:
        raise ValueError("[labels] invalid title_threshold")
    required = {"arxiv_id_match", "title_similarity", "first_author_match", "year_diff"}
    if not required <= set(pairs.column_names):
        raise ValueError("[pairs] missing auto_rule_v1 features")
    if set(selected) - usable:
        raise ValueError(f"[labels] unusable crawl: {sorted(set(selected)-usable)}")
    if set(selected) - owners.keys():
        raise ValueError("[assignments] selected publication has no owner")
    groups = defaultdict(list)
    for row in pairs.to_pylist():
        if row["arxiv_id"] in selected:
            groups[row["arxiv_id"], row["citation_key"]].append(row)
    result = []
    for (paper, key), candidates in sorted(groups.items()):
        matches = [r for r in candidates if r["arxiv_id_match"] is True]
        # Multiple exact ID hits remain ambiguous, even if text favors one.
        if len(matches) > 1:
            continue
        if not matches:
            matches = [
                r
                for r in candidates
                if r["title_similarity"] is not None
                and r["title_similarity"] >= title_threshold
                and r["first_author_match"] is True
                and r["year_diff"] in (None, 0)
            ]
        if len(matches) == 1:
            result.append(
                dict(
                    arxiv_id=paper,
                    citation_key=key,
                    groundtruth_arxiv_id=matches[0]["ref_arxiv_id"],
                    label_source="auto",
                    member_id=owners[paper],
                )
            )
    return _sorted_table("labels", result)


def label_automatically(
    *,
    queries,
    pairs,
    crawl_status,
    publication_ids,
    title_threshold=0.95,
    assignments,
    manual_publications,
):
    owners = canonical_table("assignments", assignments)
    manual = canonical_table("manual_publications", manual_publications)
    selected = set(normalize_publications(publication_ids))
    owner_map = {r["arxiv_id"]: r["member_id"] for r in owners.to_pylist()}
    auto = make_table(
        "auto_publications",
        [dict(arxiv_id=a, member_id=owner_map.get(a)) for a in selected],
    )
    validate_registry(owners, manual, auto)
    return _apply_rule(
        queries=queries,
        pairs=pairs,
        crawl_status=crawl_status,
        selected=selected,
        owners=owner_map,
        title_threshold=title_threshold,
    )


def evaluate_auto_rule_on_manual(*, auto_labels, manual_labels):
    auto = canonical_table("labels", auto_labels)
    manual = canonical_table("labels", manual_labels)
    truth = {
        (r["arxiv_id"], r["citation_key"]): r["groundtruth_arxiv_id"]
        for r in manual.to_pylist()
    }
    guessed = {
        (r["arxiv_id"], r["citation_key"]): r["groundtruth_arxiv_id"]
        for r in auto.to_pylist()
    }
    shared = truth.keys() & guessed.keys()
    correct = sum(truth[k] == guessed[k] for k in shared)
    return dict(
        rule="auto_rule_v1",
        manual_entries=len(truth),
        predicted_entries=len(guessed),
        evaluated_entries=len(shared),
        correct=correct,
        accuracy=correct / len(shared) if shared else None,
        coverage=len(shared) / len(truth) if truth else None,
    )


def audit_auto_rule(*, queries, pairs, crawl_status, manual_labels, assignments):
    """Evaluate fixed rules on manual gold without registering these predictions as labels."""
    manual = canonical_table("labels", manual_labels)
    owners = canonical_table("assignments", assignments)
    predictions = _apply_rule(
        queries=queries,
        pairs=pairs,
        crawl_status=crawl_status,
        selected=set(manual["arxiv_id"].to_pylist()),
        owners={r["arxiv_id"]: r["member_id"] for r in owners.to_pylist()},
        title_threshold=0.95,
    )
    return evaluate_auto_rule_on_manual(auto_labels=predictions, manual_labels=manual)


def coverage_by_paper(*, queries, labels, publication_ids):
    queries = canonical_table("queries", queries)
    labels = canonical_table("labels", labels)
    keys = set(
        zip(queries["arxiv_id"].to_pylist(), queries["citation_key"].to_pylist())
    )
    for row in labels.to_pylist():
        if (row["arxiv_id"], row["citation_key"]) not in keys:
            raise ValueError("[labels] unknown entry in coverage")
    totals = Counter(queries["arxiv_id"].to_pylist())
    labeled = Counter(labels["arxiv_id"].to_pylist())
    return [
        dict(
            arxiv_id=a,
            entries=totals[a],
            labeled_entries=labeled[a],
            unlabeled_entries=totals[a] - labeled[a],
            coverage=labeled[a] / totals[a] if totals[a] else None,
        )
        for a in normalize_publications(publication_ids)
    ]
