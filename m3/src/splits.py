"""Deterministic publication-level splits, balanced by member and registry pool."""

import random

import pyarrow as pa

from .contracts import (
    make_table,
    validate_registry,
    validate_splits,
    validate_split_design,
)
from .labels import canonical_table


def split_publications(
    *,
    manual_labels,
    auto_labels,
    owners,
    valid_per_pool_per_member=1,
    test_per_pool_per_member=1,
    seed=2026,
    manual_publications,
    auto_publications,
):
    assignments = canonical_table("assignments", owners)
    manual = canonical_table("manual_publications", manual_publications)
    auto = canonical_table("auto_publications", auto_publications)
    validate_registry(assignments, manual, auto)
    tables = []
    for source, table, registry in (
        ("manual", manual_labels, manual),
        ("auto", auto_labels, auto),
    ):
        table = canonical_table("labels", table)
        registered = {(r["arxiv_id"], r["member_id"]) for r in registry.to_pylist()}
        for row in table.to_pylist():
            if (
                row["label_source"] != source
                or (row["arxiv_id"], row["member_id"]) not in registered
            ):
                raise ValueError(
                    f"[labels] {row['arxiv_id']}: source/owner disagrees with {source} registry"
                )
        tables.append(table)
    labels = pa.concat_tables(tables)
    labeled = set(labels["arxiv_id"].to_pylist())
    quotas = (test_per_pool_per_member, valid_per_pool_per_member)
    if any(type(q) is not int or q < 0 for q in quotas):
        raise ValueError("[splits] quotas must be nonnegative integers")
    members = sorted(set(assignments["member_id"].to_pylist()))
    rng = random.Random(seed)
    partitions = {}
    for source, registry in (("manual", manual), ("auto", auto)):
        for member in members:
            papers = sorted(
                r["arxiv_id"] for r in registry.to_pylist() if r["member_id"] == member
            )
            eligible = sorted(set(papers) & labeled)
            required = sum(quotas)
            if len(eligible) < required:
                raise ValueError(
                    f"[splits] {member}/{source}: need {required} labeled papers, got {len(eligible)}"
                )
            rng.shuffle(eligible)
            partitions.update({a: "train" for a in papers})
            stop = test_per_pool_per_member
            partitions.update({a: "test" for a in eligible[:stop]})
            partitions.update({a: "valid" for a in eligible[stop:required]})
    result = make_table(
        "splits", [dict(arxiv_id=a, partition=p) for a, p in sorted(partitions.items())]
    )
    validate_splits(result, labels, manual, auto)
    validate_split_design(
        result,
        manual,
        auto,
        assignments,
        valid_per_pool_per_member=valid_per_pool_per_member,
        test_per_pool_per_member=test_per_pool_per_member,
    )
    return result
