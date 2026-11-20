"""Tests for the TV4 QA report (Contract §7.2-7.3). All expected figures are hand counts."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import tv4_builders as fx  # noqa: E402

from m2.src.parsing import benchmark, export, qa_report  # noqa: E402
from m2.src.parsing.deduplication import deduplicate_elements  # noqa: E402

RATE_KEYS = {"numerator", "denominator", "denominator_definition", "value"}


# ----------------------------------------------------------------- helpers --

def row(version, status, *, arxiv_id=fx.ID, has_tex=True, root=True, problems=0, **extra):
    return {"arxiv_id": arxiv_id, "version": version, "status": status, "has_tex": has_tex,
            "root_found": root if has_tex else None,
            "include_problems": problems if has_tex and status in ("ok", "partial") else None,
            **extra}


def export_fixture(directory, name, bibtex=()):
    elements, hierarchy = deduplicate_elements(fx.SINGLE_PUBLICATION_FIXTURES[name]())
    export.export_tables(elements, hierarchy, list(bibtex), directory)
    return Path(directory)


def bib(key):
    return {"arxiv_id": fx.ID, "citation_key": key, "entry_type": "article", "title": "T",
            "authors": ["A"], "year": 2020, "doi": None, "journal": None, "url": None}


def walk_rates(node, path=""):
    """Yield every dict that looks like a rate, with where it was found."""
    if isinstance(node, dict):
        if "numerator" in node and "denominator_definition" in node:
            yield path, node
        for key, value in node.items():
            yield from walk_rates(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk_rates(value, f"{path}[{index}]")


@pytest.fixture
def no_tex_report(tmp_path):
    """Example (e): v1 ok, v2 no_tex, v3 ok; 5 elements, 6 hierarchy rows."""
    directory = export_fixture(tmp_path / "e", "tv4_no_tex_middle_version")
    rows = [row(1, "ok"), row(2, "no_tex", has_tex=False, reason="NO_TEX_SOURCE"), row(3, "ok")]
    return qa_report.build_qa_report(directory, rows, scope="unit test"), directory, rows


# ----------------------------------------------------------- the numbers ----

def test_version_counts_and_statuses(no_tex_report):
    report, _, _ = no_tex_report
    counts = report["versions"]["counts"]
    assert counts["publications"] == 1 and counts["versions"] == 3
    assert counts["versions_with_tex"] == 2 and counts["versions_with_tex_unknown"] == 0
    assert counts["by_status"] == {"ok": 2, "partial": 0, "no_tex": 1, "failed": 0, "skipped": 0}


def test_every_rate_has_numerator_denominator_and_definition(no_tex_report):
    report, _, _ = no_tex_report
    rates = list(walk_rates(report))
    assert len(rates) >= 12
    for where, rate in rates:
        assert set(rate) == RATE_KEYS, where
        assert rate["denominator_definition"], where
        if rate["denominator"]:
            assert rate["value"] == pytest.approx(rate["numerator"] / rate["denominator"]), where
        else:
            assert rate["value"] is None, where


def test_status_rates_use_all_versions_as_denominator(no_tex_report):
    rates = no_tex_report[0]["versions"]["rates"]
    assert (rates["versions_ok"]["numerator"], rates["versions_ok"]["denominator"]) == (2, 3)
    assert (rates["versions_no_tex"]["numerator"], rates["versions_no_tex"]["denominator"]) == (1, 3)
    assert rates["versions_failed"]["numerator"] == 0
    assert "all versions" in rates["versions_ok"]["denominator_definition"]


def test_versions_with_tex_denominator_is_never_called_a_corpus_rate(no_tex_report):
    rates = no_tex_report[0]["versions"]["rates"]
    tex = rates["ok_among_versions_with_tex"]
    assert (tex["numerator"], tex["denominator"]) == (2, 2)
    assert "NOT a corpus-wide" in tex["denominator_definition"]
    assert rates["versions_ok"]["denominator"] != tex["denominator"]  # 3 versions vs 2 with TeX


def test_coverage_root_detection_and_includes(tmp_path):
    directory = export_fixture(tmp_path / "t", "tv4_three_versions_reordered")
    rows = [row(1, "ok"), row(2, "partial", problems=2), row(3, "ok")]
    rates = qa_report.build_qa_report(directory, rows)["versions"]["rates"]
    assert (rates["version_coverage_in_tables"]["numerator"],
            rates["version_coverage_in_tables"]["denominator"]) == (3, 3)
    assert (rates["root_detection"]["numerator"], rates["root_detection"]["denominator"]) == (3, 3)
    assert (rates["include_resolution"]["numerator"],
            rates["include_resolution"]["denominator"]) == (2, 3)  # v2 has 2 include problems


def test_unknown_gathering_facts_are_not_counted_as_success(tmp_path):
    directory = export_fixture(tmp_path / "t", "tv4_three_versions_reordered")
    rows = [dict(row(v, "ok"), root_found=None, include_problems=None, has_tex=None)
            for v in (1, 2, 3)]
    report = qa_report.build_qa_report(directory, rows)
    rates = report["versions"]["rates"]
    assert rates["root_detection"]["denominator"] == 0 and rates["root_detection"]["value"] is None
    assert rates["include_resolution"]["value"] is None
    assert report["versions"]["counts"]["versions_with_tex_unknown"] == 3
    assert rates["ok_among_versions_with_tex"]["value"] is None


def test_element_dedup_matches_the_hand_count_of_example_a(tmp_path):
    directory = export_fixture(tmp_path / "a", "tv4_two_versions_one_sentence_changed")
    report = qa_report.build_qa_report(directory, [row(1, "ok"), row(2, "ok")])
    tables = report["tables"]
    assert tables["elements_total"] == 8 and tables["hierarchy_relations"] == 8
    assert tables["elements_by_kind"] == {"document": 2, "paragraph": 2, "section": 1,
                                          "sentence": 3}
    dedup = tables["element_dedup"]
    assert (dedup["non_root_occurrences"], dedup["unique_non_root_elements"]) == (8, 6)
    assert (dedup["dedup_rate"]["numerator"], dedup["dedup_rate"]["denominator"]) == (2, 8)
    assert dedup["dedup_rate"]["value"] == 0.25
    kinds = dedup["by_kind"]
    assert (kinds["sentence"]["occurrences"], kinds["sentence"]["unique_elements"]) == (4, 3)
    assert (kinds["paragraph"]["occurrences"], kinds["paragraph"]["unique_elements"]) == (2, 2)
    assert (kinds["section"]["occurrences"], kinds["section"]["unique_elements"]) == (2, 1)
    assert kinds["sentence"]["dedup_rate"]["value"] == 0.25
    assert kinds["section"]["dedup_rate"]["value"] == 0.5
    assert "document" not in kinds  # roots are never deduplicated


def test_dedup_of_the_version_without_tex_example(no_tex_report):
    dedup = no_tex_report[0]["tables"]["element_dedup"]
    assert (dedup["non_root_occurrences"], dedup["unique_non_root_elements"]) == (6, 3)
    assert dedup["dedup_rate"]["value"] == 0.5


def test_bibtex_counts_before_and_after(tmp_path):
    directory = export_fixture(tmp_path / "b", "tv4_single_version",
                               bibtex=[bib("a"), bib("b")])
    report = qa_report.build_qa_report(directory, [row(1, "ok")],
                                       bib_entries_before={fx.ID: 5})
    bibtex = report["tables"]["bibtex"]
    assert bibtex["entries_after_dedup"] == 2 and bibtex["entries_before_dedup"] == 5
    assert (bibtex["dedup_rate"]["numerator"], bibtex["dedup_rate"]["denominator"]) == (3, 5)
    assert bibtex["dedup_rate"]["value"] == pytest.approx(0.6)


def test_missing_bibtex_before_count_is_reported_as_not_measured(tmp_path):
    directory = export_fixture(tmp_path / "b", "tv4_single_version", bibtex=[bib("a")])
    bibtex = qa_report.build_qa_report(directory, [row(1, "ok")])["tables"]["bibtex"]
    assert bibtex["entries_after_dedup"] == 1
    assert bibtex["entries_before_dedup"] is None and bibtex["dedup_rate"]["value"] is None
    assert "not measured" in bibtex["dedup_rate"]["denominator_definition"]
    assert "TODO" in bibtex["note"]


def test_citation_coverage_comes_from_the_validation(tmp_path):
    docs = [fx.doc(1, fx.root(fx.section("S", fx.paragraph("See \\cite{a}.", "And \\cite{zz}."))))]
    elements, hierarchy = deduplicate_elements(docs)
    export.export_tables(elements, hierarchy, [bib("a")], tmp_path)
    report = qa_report.build_qa_report(tmp_path, [row(1, "ok")])
    coverage = report["validation"]["citation_coverage"]
    assert (coverage["numerator"], coverage["denominator"]) == (1, 2)
    assert report["validation"]["warning_total"] >= 1 and report["validation"]["passed"]


# ------------------------------------------------- failures / consistency ---

def test_failed_and_skipped_versions_are_listed_with_stage_and_reason(tmp_path):
    directory = export_fixture(tmp_path / "s", "tv4_status_mix")
    rows = [row(1, "ok"), row(2, "partial", problems=1),
            row(3, "failed", stage="gather", reason="AMBIGUOUS_ROOT"),
            row(4, "skipped", stage="gather", reason="M1_STATUS_SKIPPED"),
            row(5, "failed", stage="parse", reason="boom")]
    failures = qa_report.build_qa_report(directory, rows)["versions"]["failures"]
    assert failures["total"] == 3 and failures["by_stage"] == {"gather": 2, "parse": 1}
    assert failures["examples"][0] == {"arxiv_id": fx.ID, "version": 3, "status": "failed",
                                       "stage": "gather", "reason": "AMBIGUOUS_ROOT"}
    assert [e["version"] for e in failures["examples"]] == [3, 4, 5]


def test_failure_examples_are_capped_but_the_total_is_exact(tmp_path):
    directory = export_fixture(tmp_path / "s", "tv4_single_version")
    rows = [row(1, "ok")] + [row(v, "failed", stage="gather", reason="X") for v in range(2, 302)]
    failures = qa_report.build_qa_report(directory, rows)["versions"]["failures"]
    assert failures["total"] == 300
    assert len(failures["examples"]) == qa_report.MAX_FAILURE_EXAMPLES


def test_status_and_tables_that_disagree_are_flagged(tmp_path):
    directory = export_fixture(tmp_path / "e", "tv4_no_tex_middle_version")  # roots 1, 2, 3
    rows = [row(1, "ok"), row(2, "no_tex", has_tex=False), row(3, "failed", stage="parse"),
            row(4, "ok")]
    consistency = qa_report.build_qa_report(directory, rows)["versions"]["consistency"]
    assert consistency["versions_expected_in_tables_but_missing"] == 1
    assert consistency["examples_missing"] == [[fx.ID, 4]]
    assert consistency["versions_in_tables_without_an_exported_status"] == 1
    assert consistency["examples_unexpected"] == [[fx.ID, 3]]


def test_consistent_run_reports_no_disagreement(no_tex_report):
    consistency = no_tex_report[0]["versions"]["consistency"]
    assert consistency["versions_expected_in_tables_but_missing"] == 0
    assert consistency["versions_in_tables_without_an_exported_status"] == 0


# ----------------------------------------------- validation / benchmark ------

def test_validation_errors_are_embedded_and_fail_the_report(tmp_path):
    directory = export_fixture(tmp_path / "v", "tv4_single_version")
    rows = pq.read_table(directory / "hierarchy.parquet").to_pylist()
    rows[0]["parent_id"] = rows[0]["element_id"]  # self-loop
    pq.write_table(pa.Table.from_pylist(rows, schema=export.HIERARCHY_SCHEMA),
                   directory / "hierarchy.parquet")
    report = qa_report.build_qa_report(directory, [row(1, "ok")])
    assert report["validation"]["passed"] is False and report["validation"]["error_total"] >= 1
    assert "self_loop" in report["validation"]["issues"]
    assert any(e["check"] == "self_loop" for e in report["validation"]["errors"])


def test_benchmark_summary_is_embedded(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "_load_pipeline", lambda: None)
    ids = tmp_path / "ids.txt"
    ids.write_text(fx.ID + "\n")
    result = benchmark.run_benchmark(
        tmp_path, ids, tmp_path / "bench", "qa", documents={fx.ID: fx.tv4_single_version()})
    directory = export_fixture(tmp_path / "t", "tv4_single_version")
    report = qa_report.build_qa_report(directory, [row(1, "ok")],
                                       tmp_path / "bench" / "benchmark_qa.json")
    section = report["benchmark"]
    assert section["available"] and section["label"] == "qa" and section["mode"] == "tv4_only"
    assert section["end_to_end_wall_seconds"] == result["end_to_end_wall_seconds"]
    assert section["peak_rss_bytes"] == result["peak_rss_bytes"] > 0
    assert section["output_total_bytes"] == result["output"]["total_bytes"]
    assert set(section["stage_wall_seconds"]) >= {"end_to_end", "merge"}
    assert section["environment"]["platform"] == result["environment"]["platform"]


def test_without_a_benchmark_nothing_is_invented(tmp_path):
    directory = export_fixture(tmp_path / "t", "tv4_single_version")
    section = qa_report.build_qa_report(directory, [row(1, "ok")])["benchmark"]
    assert section["available"] is False and "nothing was measured" in section["note"]


# ------------------------------------------------------- file / inputs -------

def test_report_file_equals_the_returned_dict_and_has_no_timestamp(no_tex_report):
    report, directory, _ = no_tex_report
    text = (directory / "qa_report.json").read_text(encoding="utf-8")
    assert json.loads(text) == json.loads(json.dumps(report))
    assert "utc" not in text.lower() and "generated" not in text.lower()
    assert report["scope"] == "unit test"


def test_report_is_deterministic_and_leaves_no_temporary_files(no_tex_report):
    report, directory, rows = no_tex_report
    first = (directory / "qa_report.json").read_bytes()
    qa_report.build_qa_report(directory, list(reversed(rows)), scope="unit test")
    assert (directory / "qa_report.json").read_bytes() == first
    assert not [p for p in directory.iterdir() if p.name.startswith(".qa-")]


def test_notes_are_kept(tmp_path):
    directory = export_fixture(tmp_path / "t", "tv4_single_version")
    report = qa_report.build_qa_report(directory, [row(1, "ok")], notes=["pilot run only"])
    assert "pilot run only" in report["notes"]


@pytest.mark.parametrize("bad", [
    [{"arxiv_id": fx.ID, "version": 1}],
    [row(1, "weird")], [row(0, "ok")], [row(True, "ok")], [row(1, "ok"), row(1, "failed")],
    [dict(row(1, "ok"), arxiv_id="")]])
def test_malformed_version_status_is_rejected_and_nothing_is_written(tmp_path, bad):
    directory = export_fixture(tmp_path / "t", "tv4_single_version")
    with pytest.raises(ValueError):
        qa_report.build_qa_report(directory, bad)
    assert not (directory / "qa_report.json").exists()


def test_empty_version_status_gives_empty_rates(tmp_path):
    directory = export_fixture(tmp_path / "t", "tv4_single_version")
    versions = qa_report.build_qa_report(directory, [])["versions"]
    assert versions["counts"]["versions"] == 0
    assert all(r["value"] is None for r in versions["rates"].values())
    assert versions["consistency"]["versions_in_tables_without_an_exported_status"] == 1


def test_unreadable_tables_still_produce_a_report(tmp_path):
    directory = export_fixture(tmp_path / "t", "tv4_single_version")
    (directory / "bibtex.parquet").unlink()
    report = qa_report.build_qa_report(directory, [row(1, "ok")])
    assert report["versions"] is None and report["tables"] is None
    assert report["validation"]["passed"] is False
    assert any("could not be read" in n for n in report["notes"])
    assert (directory / "qa_report.json").exists()


# --------------------------------------------------------------- status_row --

def gathered(status="ok", root="main.tex", warnings=(), version=1, arxiv_id=fx.ID):
    return SimpleNamespace(arxiv_id=arxiv_id, version=version, status=status,
                           root_file=root, warnings=list(warnings))


def warning(code, detail="x"):
    return f"{fx.ID}v1 stage=gather {code}: sec/a.tex {detail}"


def test_status_row_for_a_clean_version():
    out = qa_report.status_row(gathered(), SimpleNamespace(status="ok"))
    assert out == {"arxiv_id": fx.ID, "version": 1, "status": "ok", "has_tex": True,
                   "root_found": True, "include_problems": 0, "stage": None, "reason": None}


def test_status_row_counts_only_include_problem_codes():
    warnings = [warning("MISSING_DEPENDENCY"), warning("INCLUDE_CYCLE"),
                warning("ENCODING_LATIN1_FALLBACK"), warning("UNRESOLVED_CONDITIONAL")]
    out = qa_report.status_row(gathered("partial", warnings=warnings),
                               SimpleNamespace(status="partial"))
    assert out["status"] == "partial" and out["include_problems"] == 2
    assert (out["stage"], out["reason"]) == ("gather", "UNRESOLVED_CONDITIONAL")


def test_status_row_takes_the_parser_status_when_it_is_worse():
    out = qa_report.status_row(gathered("ok"), SimpleNamespace(status="partial"))
    assert out["status"] == "partial"


def test_status_row_for_no_tex_skipped_and_failed_gathering():
    no_tex = qa_report.status_row(gathered("no_tex", root=None))
    assert (no_tex["status"], no_tex["has_tex"], no_tex["root_found"]) == ("no_tex", False, None)
    assert no_tex["reason"] == "NO_TEX_SOURCE" and no_tex["stage"] is None
    skipped = qa_report.status_row(gathered("skipped", root=None,
                                            warnings=[warning("M1_STATUS_SKIPPED")]))
    assert (skipped["status"], skipped["stage"], skipped["reason"]) == (
        "skipped", "gather", "M1_STATUS_SKIPPED")
    failed = qa_report.status_row(gathered("failed", root=None, warnings=[
        warning("MISSING_DEPENDENCY"), warning("AMBIGUOUS_ROOT")]), has_tex=True)
    assert (failed["status"], failed["stage"], failed["reason"]) == ("failed", "gather",
                                                                     "AMBIGUOUS_ROOT")
    assert failed["root_found"] is False and failed["has_tex"] is True


def test_status_row_for_an_exception_in_a_later_stage():
    out = qa_report.status_row(gathered(), error=("dedup", "DedupError: cycle " + "x" * 400))
    assert (out["status"], out["stage"]) == ("failed", "dedup")
    assert len(out["reason"]) == 300
    assert out["include_problems"] is None


def test_rows_from_status_row_are_accepted_by_the_report(tmp_path):
    directory = export_fixture(tmp_path / "t", "tv4_single_version")
    rows = [qa_report.status_row(gathered(), SimpleNamespace(status="ok"))]
    assert qa_report.build_qa_report(directory, rows)["versions"]["counts"]["versions"] == 1
