"""Tests for TV4 table validation (docs/m2/tv4_decisions.md; Contract §7.2).

Valid data comes from the real deduplication + export code. Broken data is made by
reading the exported Parquet back, damaging rows in Python and writing the files
again, so each test proves one check fires on exactly the damage it describes.
"""

import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import tv4_builders as fx  # noqa: E402

from src.parsing import export, validation  # noqa: E402
from src.parsing.deduplication import deduplicate_elements  # noqa: E402
from src.utils.common import paper_key  # noqa: E402

KEY = paper_key(fx.ID)
TABLES = ["elements", "hierarchy", "hierarchy_internal", "bibtex"]


# ----------------------------------------------------------------- helpers --

def bib(key, arxiv_id=fx.ID):
    return {"arxiv_id": arxiv_id, "citation_key": key, "entry_type": "article",
            "title": f"T {key}", "authors": ["A B"], "year": 2020, "doi": None,
            "journal": None, "url": None}


def cited_docs():
    """Three cited keys (one repeated); bibtex will only know two of them."""
    def tree():
        return fx.root(fx.section("Intro", fx.paragraph(
            "As shown in \\cite{vaswani2017}.", "Also \\citep[p.2]{bert, nokey}.")))
    return [fx.doc(1, tree()), fx.doc(2, tree())]


def write_export(directory, docs=None, bibtex=None):
    docs = docs if docs is not None else fx.tv4_two_versions_one_sentence_changed()
    elements, hierarchy = deduplicate_elements(docs)
    export.export_tables(elements, hierarchy,
                         bibtex if bibtex is not None else [], directory)
    return Path(directory)


def load(directory):
    return {t: pq.read_table(Path(directory) / f"{t}.parquet").to_pylist() for t in TABLES}


def save(directory, rows, *, schemas=None, only=None):
    """Write row dicts back as Parquet, bypassing every exporter check."""
    for table in (only or TABLES):
        schema = (schemas or {}).get(table, export.SCHEMAS[table])
        pq.write_table(pa.Table.from_pylist(rows[table], schema=schema),
                       Path(directory) / f"{table}.parquet")


def nullable(schema):
    return pa.schema([f.with_nullable(True) for f in schema])


def issues(result, check):
    return [i for i in result["errors"] + result["warnings"] if i["check"] == check]


def only_check(result, check):
    """The report contains ``check`` and its severity matches the documented one."""
    assert issues(result, check), result
    return validation.SEVERITY[check]


@pytest.fixture
def good(tmp_path):
    return write_export(tmp_path / "good", cited_docs(), [bib("vaswani2017"), bib("bert")])


# ------------------------------------------------------------- clean data ---

def test_valid_export_passes_without_errors_or_warnings(tmp_path):
    result = validation.validate_tables(write_export(tmp_path / "ok"))
    assert result["passed"] is True
    assert result["errors"] == [] and result["warnings"] == []
    assert result["counts"]["tables"] == {"elements": 8, "hierarchy": 8, "bibtex": 0,
                                          "hierarchy_internal": 8}
    assert result["counts"]["publications"] == 1
    assert result["counts"]["versions_with_hierarchy"] == 2


@pytest.mark.parametrize("name", sorted(fx.SINGLE_PUBLICATION_FIXTURES))
def test_every_valid_fixture_exports_to_a_clean_report(tmp_path, name):
    docs = fx.SINGLE_PUBLICATION_FIXTURES[name]()
    result = validation.validate_tables(write_export(tmp_path / name, docs))
    assert result["errors"] == [], result["errors"]


def test_merged_multi_publication_corpus_is_valid(tmp_path):
    for aid in (fx.ID, fx.OTHER_ID, fx.OLD_ID):
        docs = fx.tv4_two_versions_one_sentence_changed()
        for d in docs:
            d.arxiv_id = aid
        e, h = deduplicate_elements(docs)
        export.write_part(aid, e, h, [bib("k", aid)], tmp_path / "parts")
    export.merge_parts(tmp_path / "parts", tmp_path / "out")
    result = validation.validate_tables(tmp_path / "out")
    assert result["passed"] and result["counts"]["publications"] == 3


def test_version_without_tex_is_valid_and_counted(tmp_path):
    result = validation.validate_tables(
        write_export(tmp_path / "e", fx.tv4_no_tex_middle_version()))
    assert result["passed"]
    assert result["counts"]["roots_without_children"] == 1
    assert result["counts"]["versions_with_hierarchy"] == 2


def test_report_shape(good):
    result = validation.validate_tables(good)
    assert set(result) == {"passed", "errors", "warnings", "counts"}
    assert {"issues", "error_total", "warning_total"} <= set(result["counts"])


# --------------------------------------------------------------- files ------

@pytest.mark.parametrize("table", ["elements", "hierarchy", "bibtex"])
def test_missing_required_file_is_an_error(good, table):
    (good / f"{table}.parquet").unlink()
    result = validation.validate_tables(good)
    assert not result["passed"] and only_check(result, "missing_file") == "error"
    assert table in issues(result, "missing_file")[0]["message"]


def test_missing_internal_file_is_not_an_error(good):
    (good / "hierarchy_internal.parquet").unlink()
    assert validation.validate_tables(good)["passed"]


def test_unreadable_file_is_an_error(good):
    (good / "bibtex.parquet").write_bytes(b"not parquet")
    result = validation.validate_tables(good)
    assert not result["passed"] and only_check(result, "unreadable_file") == "error"


# -------------------------------------------------------------- schema ------

def test_wrong_column_type_is_a_schema_error(good):
    rows = load(good)
    wrong = pa.schema([export.HIERARCHY_SCHEMA.field(0), pa.field("version", pa.int64()),
                       *list(export.HIERARCHY_SCHEMA)[2:]])
    save(good, rows, schemas={"hierarchy": wrong}, only=["hierarchy"])
    result = validation.validate_tables(good)
    assert not result["passed"]
    assert any("version" in i["message"] and "int64" in i["message"]
               for i in issues(result, "schema"))


def test_plain_string_kind_is_not_dictionary_encoded(good):
    rows = load(good)
    plain = pa.schema([export.ELEMENTS_SCHEMA.field(0), export.ELEMENTS_SCHEMA.field(1),
                       pa.field("kind", pa.string()), export.ELEMENTS_SCHEMA.field(3)])
    save(good, rows, schemas={"elements": plain}, only=["elements"])
    assert any("kind" in i["message"] for i in issues(validation.validate_tables(good), "schema"))


def test_nullable_key_column_is_a_schema_error(good):
    rows = load(good)
    save(good, rows, schemas={"elements": nullable(export.ELEMENTS_SCHEMA)}, only=["elements"])
    assert any("element_id" in i["message"] for i in
               issues(validation.validate_tables(good), "schema"))


def test_extra_or_reordered_columns_are_schema_errors(good):
    rows = load(good)
    reordered = pa.schema(reversed(list(export.BIBTEX_SCHEMA)))
    save(good, rows, schemas={"bibtex": reordered}, only=["bibtex"])
    assert issues(validation.validate_tables(good), "schema")
    rows = load(good)
    save(good, rows, only=["bibtex"])
    extra = pa.schema(list(export.HIERARCHY_SCHEMA) + [pa.field("position", pa.int32())])
    plain = [dict(r) for r in rows["hierarchy_internal"]]
    save(good, {"hierarchy": plain}, schemas={"hierarchy": extra}, only=["hierarchy"])
    assert issues(validation.validate_tables(good), "schema")


# ------------------------------------------------- values / uniqueness ------

@pytest.mark.parametrize("table,column", [
    ("elements", "element_id"), ("elements", "arxiv_id"), ("elements", "kind"),
    ("hierarchy", "version"), ("hierarchy", "parent_id"), ("bibtex", "citation_key")])
def test_null_in_a_required_column_is_an_error(good, table, column):
    rows = load(good)
    if table == "bibtex":
        rows[table] = [bib("a"), bib("b")]
    rows[table][0][column] = None
    schemas = {t: nullable(export.SCHEMAS[t]) for t in TABLES}
    save(good, rows, schemas=schemas)
    result = validation.validate_tables(good)
    assert not result["passed"]
    assert any(column in i["message"] and table in i["message"]
               for i in issues(result, "null_value"))


def test_null_content_is_an_error(good):
    rows = load(good)
    rows["elements"][3]["content"] = None
    save(good, rows)
    result = validation.validate_tables(good)
    assert any("content" in i["message"] for i in issues(result, "null_value"))


def test_empty_string_in_a_key_column_is_an_error(good):
    rows = load(good)
    rows["elements"][0]["arxiv_id"] = ""
    save(good, rows)
    assert only_check(validation.validate_tables(good), "empty_value") == "error"


def test_non_positive_version_is_an_error(good):
    rows = load(good)
    rows["hierarchy"][0]["version"] = 0
    save(good, rows)
    assert only_check(validation.validate_tables(good), "invalid_version") == "error"


def test_duplicate_element_id_is_an_error(good):
    rows = load(good)
    rows["elements"].append(dict(rows["elements"][1]))
    save(good, rows)
    result = validation.validate_tables(good)
    assert not result["passed"]
    assert issues(result, "duplicate_element_id")[0]["element_id"] == rows["elements"][1]["element_id"]


def test_duplicate_citation_key_in_a_publication_is_an_error(good):
    rows = load(good)
    rows["bibtex"].append(dict(rows["bibtex"][0]))
    save(good, rows)
    assert only_check(validation.validate_tables(good), "duplicate_citation_key") == "error"


def test_same_citation_key_in_two_publications_is_fine(tmp_path):
    for aid in (fx.ID, fx.OTHER_ID):
        docs = fx.tv4_single_version()
        for d in docs:
            d.arxiv_id = aid
        e, h = deduplicate_elements(docs)
        export.write_part(aid, e, h, [bib("shared", aid)], tmp_path / "p")
    export.merge_parts(tmp_path / "p", tmp_path / "o")
    assert validation.validate_tables(tmp_path / "o")["passed"]


# ------------------------------------------------------ references ----------

def test_hierarchy_element_missing_from_elements_is_an_error(good):
    rows = load(good)
    rows["hierarchy"][1]["element_id"] = f"{KEY}#h99"
    save(good, rows)
    assert only_check(validation.validate_tables(good), "unknown_reference") == "error"


def test_hierarchy_parent_missing_from_elements_is_an_error(good):
    rows = load(good)
    victim = rows["hierarchy"][0]["parent_id"]
    rows["elements"] = [r for r in rows["elements"] if r["element_id"] != victim]
    save(good, rows)
    result = validation.validate_tables(good)
    assert any("parent_id" in i["message"] for i in issues(result, "unknown_reference"))


def test_id_with_another_publications_prefix_is_an_error(good):
    rows = load(good)
    rows["elements"][1]["element_id"] = f"{paper_key(fx.OTHER_ID)}#zz1"
    save(good, rows)
    result = validation.validate_tables(good)
    assert only_check(result, "publication_prefix") == "error"
    assert not result["passed"]


def test_cross_publication_parent_is_an_error(tmp_path):
    for aid in (fx.ID, fx.OTHER_ID):
        docs = fx.tv4_single_version()
        for d in docs:
            d.arxiv_id = aid
        e, h = deduplicate_elements(docs)
        export.write_part(aid, e, h, [], tmp_path / "p")
    export.merge_parts(tmp_path / "p", tmp_path / "o")
    rows = load(tmp_path / "o")
    other = f"{paper_key(fx.OTHER_ID)}#h1"
    row = next(r for r in rows["hierarchy"] if r["arxiv_id"] == fx.ID and r["parent_id"].endswith("#h1"))
    row["parent_id"] = other
    save(tmp_path / "o", rows, only=["hierarchy"])
    result = validation.validate_tables(tmp_path / "o")
    assert any("parent_id" in i["message"] for i in issues(result, "publication_prefix"))


# ---------------------------------------------------------- roots / graph ---

def test_root_with_a_non_document_kind_is_an_error(good):
    rows = load(good)
    for r in rows["elements"]:
        if r["element_id"] == f"{KEY}#root1":
            r["kind"] = "section"
    save(good, rows)
    result = validation.validate_tables(good)
    assert only_check(result, "root_not_document") == "error"
    assert issues(result, "root_not_document")[0]["version"] == 1


def test_document_listed_as_a_child_is_an_error(good):
    rows = load(good)
    rows["hierarchy"][0]["element_id"] = f"{KEY}#root2"
    save(good, rows)
    assert only_check(validation.validate_tables(good), "document_as_child") == "error"


def test_two_roots_in_one_version_is_an_error(good):
    rows = load(good)
    first = next(r for r in rows["hierarchy"] if r["version"] == 1 and r["parent_id"] == f"{KEY}#root1")
    first["parent_id"] = f"{KEY}#root2"  # v1 now hangs from root2 as well as nothing else
    extra = dict(first, element_id=f"{KEY}#p1", parent_id=f"{KEY}#root1")
    rows["hierarchy"].append(extra)
    save(good, rows, only=["hierarchy"])
    result = validation.validate_tables(good)
    assert only_check(result, "root_count") == "error"
    assert issues(result, "root_count")[0]["version"] == 1


def test_self_loop_is_an_error(good):
    rows = load(good)
    rows["hierarchy"][2]["parent_id"] = rows["hierarchy"][2]["element_id"]
    save(good, rows, only=["hierarchy"])
    result = validation.validate_tables(good)
    assert only_check(result, "self_loop") == "error" and not result["passed"]


def test_cycle_is_an_error(good):
    rows = load(good)
    by_id = {r["element_id"]: r for r in rows["hierarchy"] if r["version"] == 1}
    by_id[f"{KEY}#h1"]["parent_id"] = f"{KEY}#p1"  # h1 -> p1 -> h1
    save(good, rows, only=["hierarchy"])
    result = validation.validate_tables(good)
    assert only_check(result, "cycle") == "error"
    assert issues(result, "cycle")[0]["version"] == 1


def test_a_very_deep_hierarchy_is_validated_without_recursion(tmp_path):
    depth = 5000
    tree = fx.sentence("leaf")
    for i in range(depth):
        tree = fx.node("section", f"s{i}", tree)
    result = validation.validate_tables(
        write_export(tmp_path / "deep", [fx.doc(1, fx.root(tree))]))
    assert result["passed"] and result["counts"]["tables"]["hierarchy"] == depth + 1


def test_nonstandard_root_id_is_a_warning(good):
    rows = load(good)
    old, new = f"{KEY}#root1", f"{KEY}#rootX"
    for r in rows["elements"]:
        if r["element_id"] == old:
            r["element_id"] = new
    for r in rows["hierarchy"] + rows["hierarchy_internal"]:
        if r["parent_id"] == old:
            r["parent_id"] = new
    save(good, rows)
    result = validation.validate_tables(good)
    assert result["passed"] and only_check(result, "root_id_convention") == "warning"


# ----------------------------------------------------------- warnings -------

def test_element_with_several_parents_is_counted_as_a_warning(tmp_path):
    result = validation.validate_tables(
        write_export(tmp_path / "m", fx.tv4_sentence_two_parents()))
    assert result["passed"] and only_check(result, "multi_parent") == "warning"
    assert result["counts"]["issues"]["multi_parent"]["total"] == 1


def test_repeated_edge_is_counted_as_a_warning(tmp_path):
    result = validation.validate_tables(
        write_export(tmp_path / "r", fx.tv4_repeated_sentence_same_parent()))
    assert result["passed"] and only_check(result, "duplicate_edge") == "warning"


def test_unknown_cite_key_is_a_warning_with_coverage(good):
    result = validation.validate_tables(good)
    assert result["passed"]
    missing = issues(result, "cite_key_missing")
    assert len(missing) == 1 and "nokey" in missing[0]["message"]
    coverage = result["counts"]["citation_coverage"]
    assert (coverage["numerator"], coverage["denominator"]) == (2, 3)
    assert coverage["value"] == pytest.approx(2 / 3)
    assert coverage["denominator_definition"]


def test_cite_coverage_is_per_publication(tmp_path):
    for aid, keys in ((fx.ID, ["vaswani2017", "bert", "nokey"]), (fx.OTHER_ID, [])):
        docs = cited_docs()
        for d in docs:
            d.arxiv_id = aid
        e, h = deduplicate_elements(docs)
        export.write_part(aid, e, h, [bib(k, aid) for k in keys], tmp_path / "p")
    export.merge_parts(tmp_path / "p", tmp_path / "o")
    result = validation.validate_tables(tmp_path / "o")
    coverage = result["counts"]["citation_coverage"]
    assert (coverage["numerator"], coverage["denominator"]) == (3, 6)  # 3 of 3, then 0 of 3


def test_no_citations_gives_an_empty_ratio(tmp_path):
    coverage = validation.validate_tables(write_export(tmp_path / "n"))["counts"]["citation_coverage"]
    assert (coverage["numerator"], coverage["denominator"], coverage["value"]) == (0, 0, None)


@pytest.mark.parametrize("title", ["References", "Bibliography", "5. References", "REFERENCES:",
                                   "Works Cited"])
def test_leftover_reference_heading_is_a_warning(tmp_path, title):
    docs = [fx.doc(1, fx.root(fx.section("Intro", fx.paragraph("Hi.")),
                              fx.section(title, fx.paragraph("[1] Someone."))))]
    result = validation.validate_tables(write_export(tmp_path / "h", docs))
    assert result["passed"] and only_check(result, "reference_heading") == "warning"


def test_ordinary_headings_are_not_flagged(tmp_path):
    docs = [fx.doc(1, fx.root(fx.section("Related work on references", fx.paragraph("Hi."))))]
    result = validation.validate_tables(write_export(tmp_path / "h", docs))
    assert not issues(result, "reference_heading")


# ------------------------------------------------- internal file / limits ---

def test_internal_file_that_disagrees_with_hierarchy_is_an_error(good):
    rows = load(good)
    rows["hierarchy_internal"].pop()
    save(good, rows, only=["hierarchy_internal"])
    assert only_check(validation.validate_tables(good), "internal_mismatch") == "error"


def test_hierarchy_rows_not_in_position_order_are_an_error(good):
    rows = load(good)
    rows["hierarchy"].reverse()
    save(good, rows, only=["hierarchy"])
    result = validation.validate_tables(good)
    assert only_check(result, "internal_mismatch") == "error"
    assert not [i for i in result["errors"] if i["check"] not in ("internal_mismatch",)]


def test_examples_are_capped_but_totals_are_exact(good):
    rows = load(good)
    for i in range(120):
        rows["elements"].append({"element_id": f"{KEY}#dup", "arxiv_id": fx.ID,
                                 "kind": "sentence", "content": "x"})
    save(good, rows, only=["elements"])
    result = validation.validate_tables(good)
    assert len([i for i in result["errors"] if i["check"] == "duplicate_element_id"]) == 1
    rows["elements"] = [r for r in rows["elements"] if not r["element_id"].endswith("#dup")]
    for i in range(120):
        rows["elements"].append({"element_id": f"{KEY}#e{i}", "arxiv_id": "", "kind": "sentence",
                                 "content": "x"})
    save(good, rows, only=["elements"])
    result = validation.validate_tables(good)
    summary = result["counts"]["issues"]["empty_value"]
    assert summary["total"] == 120 and summary["examples"] == validation.MAX_EXAMPLES
    assert len(issues(result, "empty_value")) == validation.MAX_EXAMPLES


def test_every_issue_has_the_documented_fields(good):
    rows = load(good)
    rows["hierarchy"][1]["element_id"] = f"{KEY}#h99"
    save(good, rows)
    for issue in validation.validate_tables(good)["errors"]:
        assert set(issue) == {"check", "severity", "arxiv_id", "version", "element_id", "message"}
        assert issue["severity"] == "error" and issue["message"]


def test_validator_does_not_modify_the_files(good):
    before = {p.name: p.read_bytes() for p in good.iterdir()}
    validation.validate_tables(good)
    assert {p.name: p.read_bytes() for p in good.iterdir()} == before
