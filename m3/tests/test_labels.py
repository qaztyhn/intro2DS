import csv

import pyarrow as pa
import pytest

from m3.src.contracts import make_table, toy_design
from m3.src.labels import (
    write_labeling_packet,
    load_manual_labels,
    check_manual_quota,
    choose_auto_publications,
    label_automatically,
    evaluate_auto_rule_on_manual,
    coverage_by_paper,
)
from m3.src.splits import split_publications

P, R, S = "2507.00001", "2301.00001", "2301.00002"


def context():
    return dict(
        queries=make_table("queries", [dict(arxiv_id=P, citation_key="k")]),
        pairs=make_table(
            "pairs",
            [dict(arxiv_id=P, citation_key="k", ref_arxiv_id=R, arxiv_id_match=True)],
        ),
        crawl_status=make_table(
            "crawl_status", [dict(arxiv_id=P, status="ok", candidate_count=1)]
        ),
    )


def test_packet_and_blank_labels(tmp_path):
    packet = write_labeling_packet(
        bibtex=make_table("bibtex", [dict(arxiv_id=P, citation_key="k")]),
        references=make_table("references", [dict(arxiv_id=P, ref_arxiv_id=R)]),
        publication_ids=[P],
        output_dir=tmp_path / "packet",
    )
    rows = list(csv.DictReader(packet.entries_path.open()))
    assert rows[0]["groundtruth_arxiv_id"] == rows[0]["member_id"] == ""
    assert {"arxiv_id", "ref_arxiv_id", "ref_title", "ref_submission_date"} <= set(
        next(csv.DictReader(packet.candidates_path.open()))
    )
    assert len(load_manual_labels([packet.entries_path], **context())) == 0


def test_manual_import(tmp_path):
    path = tmp_path / "manual.csv"
    header = "arxiv_id,citation_key,groundtruth_arxiv_id,member_id\n"
    row = f"{P},k,arXiv:{R}v2,m1\n"
    path.write_text(header + row + row)
    assert len(load_manual_labels([path], **context())) == 1
    path.write_text(header + row + f"{P},k,{R},m2\n")
    with pytest.raises(ValueError, match="conflicting"):
        load_manual_labels([path], **context())
    path.write_text(header + f"{P},k,{S},m1\n")
    with pytest.raises(ValueError, match=P):
        load_manual_labels([path], **context())
    path.write_text(header + row)
    data = context()
    data["crawl_status"] = make_table(
        "crawl_status", [dict(arxiv_id=P, status="missing", candidate_count=0)]
    )
    with pytest.raises(ValueError, match="crawl"):
        load_manual_labels([path], **data)


def test_quota():
    owners = make_table(
        "assignments", [dict(arxiv_id=f"2507.{i:05}", member_id="m") for i in range(5)]
    )
    manual = make_table("manual_publications", owners.to_pylist())
    labels = make_table(
        "labels",
        [
            dict(
                **r, citation_key=f"k{j}", groundtruth_arxiv_id=R, label_source="manual"
            )
            for r in owners.to_pylist()
            for j in range(20)
        ],
    )
    assert check_manual_quota(labels, manual, owners)["m"]["total_pairs"] == 100
    with pytest.raises(ValueError, match="2507.00000"):
        check_manual_quota(labels.slice(1), manual, owners)
    with pytest.raises(ValueError, match="trùng"):
        check_manual_quota(
            pa.concat_tables([labels, labels.slice(0, 1)]), manual, owners
        )


def test_selection_registry_and_order():
    ids = [f"2507.{i:05}" for i in range(5000)]
    manual = make_table(
        "manual_publications", [dict(arxiv_id=a, member_id="m") for a in ids[:5]]
    )
    args = dict(
        manual_labels=make_table("labels", []),
        manual_publications=manual,
        queries=make_table(
            "queries", [dict(arxiv_id=a, citation_key="k") for a in ids]
        ),
        pairs=make_table(
            "pairs", [dict(arxiv_id=a, citation_key="k", ref_arxiv_id=R) for a in ids]
        ),
        crawl_status=make_table(
            "crawl_status",
            [dict(arxiv_id=a, status="ok", candidate_count=1) for a in ids],
        ),
        fraction=0.1,
        seed=2026,
    )
    selected = choose_auto_publications(assigned_publication_ids=ids, **args)
    assert len(selected) == 500 and not set(selected) & set(ids[:5])
    assert selected == choose_auto_publications(
        assigned_publication_ids=ids[::-1], **args
    )
    args["pairs"] = args["pairs"].slice(0, 20)
    with pytest.raises(ValueError, match="missing"):
        choose_auto_publications(assigned_publication_ids=ids, **args)


def auto_args():
    return dict(
        **context(),
        publication_ids=[P],
        assignments=make_table("assignments", [dict(arxiv_id=P, member_id="m")]),
        manual_publications=make_table("manual_publications", []),
    )


@pytest.mark.parametrize("matches,expected", [(0, 0), (1, 1), (2, 0)])
def test_rule_id_ambiguity(matches, expected):
    args = auto_args()
    args["pairs"] = make_table(
        "pairs",
        [
            dict(arxiv_id=P, citation_key="k", ref_arxiv_id=r, arxiv_id_match=True)
            for r in [R, S][:matches]
        ],
    )
    assert len(label_automatically(**args)) == expected


def test_rule_fallback_and_manual_guard():
    args = auto_args()
    args["pairs"] = make_table(
        "pairs",
        [
            dict(
                arxiv_id=P,
                citation_key="k",
                ref_arxiv_id=R,
                title_similarity=0.95,
                first_author_match=True,
                year_diff=None,
            )
        ],
    )
    assert len(label_automatically(**args)) == 1
    args["manual_publications"] = make_table(
        "manual_publications", [dict(arxiv_id=P, member_id="m")]
    )
    with pytest.raises(ValueError, match="gán tay"):
        label_automatically(**args)


def test_splits_and_audit():
    f = toy_design()
    labels = f["labels"].to_pylist()
    manual = make_table("labels", [r for r in labels if r["label_source"] == "manual"])
    auto = make_table("labels", [r for r in labels if r["label_source"] == "auto"])
    args = dict(
        manual_labels=manual,
        auto_labels=auto,
        owners=f["assignments"],
        manual_publications=f["manual_publications"],
        auto_publications=f["auto_publications"],
        seed=2026,
    )
    splits = split_publications(**args)
    counts = {
        p: splits["partition"].to_pylist().count(p) for p in ["test", "valid", "train"]
    }
    assert counts["test"] == counts["valid"] == 8
    args["manual_labels"] = make_table("labels", list(reversed(manual.to_pylist())))
    assert splits.equals(split_publications(**args))
    args["manual_labels"] = make_table("labels", [])
    with pytest.raises(ValueError, match="labeled papers"):
        split_publications(**args)
    report = evaluate_auto_rule_on_manual(
        auto_labels=manual.slice(0, 2), manual_labels=manual
    )
    assert report["accuracy"] == 1 and report["coverage"] == 2 / len(manual)
    queries = make_table(
        "queries", [{k: r[k] for k in ("arxiv_id", "citation_key")} for r in labels]
    )
    coverage = coverage_by_paper(
        queries=queries,
        labels=f["labels"],
        publication_ids=f["assignments"]["arxiv_id"].to_pylist(),
    )
    assert all(r["coverage"] == 1 for r in coverage)


def test_runner_complete_and_pending(tmp_path):
    import json
    import pyarrow.parquet as pq
    from m3.src.labeling import run_labeling

    data = toy_design(n_members=1, n_manual=5, n_auto=3)
    manual_rows = []
    for row in data["labels"].to_pylist():
        if row["label_source"] == "manual":
            manual_rows.extend({**row, "citation_key": f"k{i}"} for i in range(20))
    all_labels = manual_rows + [
        r for r in data["labels"].to_pylist() if r["label_source"] == "auto"
    ]
    inputs = tmp_path / "input"
    inputs.mkdir()
    tables = {
        n: data[n] for n in ("assignments", "manual_publications", "auto_publications")
    }
    tables["queries"] = make_table(
        "queries", [{k: r[k] for k in ("arxiv_id", "citation_key")} for r in all_labels]
    )
    tables["pairs"] = make_table(
        "pairs",
        [
            dict(
                arxiv_id=r["arxiv_id"],
                citation_key=r["citation_key"],
                ref_arxiv_id=r["groundtruth_arxiv_id"],
                arxiv_id_match=True,
            )
            for r in all_labels
        ],
    )
    tables["crawl_status"] = make_table(
        "crawl_status",
        [
            dict(arxiv_id=r["arxiv_id"], status="ok", candidate_count=1)
            for r in data["assignments"].to_pylist()
        ],
    )
    for name, table in tables.items():
        pq.write_table(table, inputs / f"{name}.parquet")
    path = tmp_path / "gold.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=manual_rows[0])
        writer.writeheader()
        writer.writerows(manual_rows)
    result = run_labeling(
        input_dir=inputs, manual_csvs=[path], output_dir=tmp_path / "complete"
    )
    assert result["complete"] is True and result["labels"] == 103
    assert len(pq.read_table(tmp_path / "complete/splits.parquet")) == 8
    assert (
        json.loads((tmp_path / "complete/auto_rule_report.json").read_text())[
            "accuracy"
        ]
        == 1
    )
    with pytest.raises(FileExistsError):
        run_labeling(
            input_dir=inputs, manual_csvs=[path], output_dir=tmp_path / "complete"
        )
    result = run_labeling(
        input_dir=inputs, manual_csvs=[], output_dir=tmp_path / "pending"
    )
    assert result["complete"] is False
    assert not (tmp_path / "pending/splits.parquet").exists()


def test_split_zero_label_registry_and_overlap():
    f = toy_design(n_members=1)
    rows = f["labels"].to_pylist()
    args = dict(
        manual_labels=make_table(
            "labels", [r for r in rows if r["label_source"] == "manual"]
        ),
        auto_labels=make_table(
            "labels", [r for r in rows if r["label_source"] == "auto"]
        ),
        owners=f["assignments"],
        manual_publications=f["manual_publications"],
        auto_publications=f["auto_publications"],
    )
    missing = args["auto_labels"].to_pylist()[-1]["arxiv_id"]
    args["auto_labels"] = args["auto_labels"].slice(0, len(args["auto_labels"]) - 1)
    splits = split_publications(**args)
    assert (
        next(r["partition"] for r in splits.to_pylist() if r["arxiv_id"] == missing)
        == "train"
    )
    args["auto_publications"] = make_table(
        "auto_publications",
        f["auto_publications"].to_pylist() + f["manual_publications"].to_pylist()[:1],
    )
    with pytest.raises(ValueError, match="gán tay"):
        split_publications(**args)


def test_normalization_collision_and_wrong_owner():
    from m3.src.labels import canonical_table

    with pytest.raises(ValueError, match="trùng"):
        canonical_table(
            "queries",
            make_table(
                "queries",
                [
                    dict(arxiv_id=P, citation_key="k"),
                    dict(arxiv_id=P + "v2", citation_key="k"),
                ],
            ),
        )
    args = auto_args()
    args["assignments"] = make_table("assignments", [])
    with pytest.raises(ValueError):
        label_automatically(**args)


def test_fixed_rule_accuracy_denominator():
    manual = make_table(
        "labels",
        [
            dict(
                arxiv_id=P,
                citation_key=k,
                groundtruth_arxiv_id=R,
                label_source="manual",
                member_id="m",
            )
            for k in ["a", "b", "c"]
        ],
    )
    auto = make_table(
        "labels",
        [
            dict(
                arxiv_id=P,
                citation_key=k,
                groundtruth_arxiv_id=r,
                label_source="auto",
                member_id="m",
            )
            for k, r in [("a", R), ("b", S), ("unreviewed", R)]
        ],
    )
    report = evaluate_auto_rule_on_manual(auto_labels=auto, manual_labels=manual)
    assert report["accuracy"] == 0.5 and report["coverage"] == 2 / 3
