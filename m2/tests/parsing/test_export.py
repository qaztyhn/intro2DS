"""Tests for TV4 Parquet export (docs/m2/tv4_decisions.md D6, D9)."""

import hashlib
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import tv4_builders as fx  # noqa: E402

from m2.src.parsing import export  # noqa: E402
from m2.src.parsing.deduplication import deduplicate_elements  # noqa: E402
from m1.src.utils.common import paper_key  # noqa: E402

ALL_FILES = ["elements.parquet", "hierarchy.parquet", "hierarchy_internal.parquet",
             "bibtex.parquet"]


# ----------------------------------------------------------------- helpers --

def bib(arxiv_id, key, **over):
    row = {"arxiv_id": arxiv_id, "citation_key": key, "entry_type": "article",
           "title": f"Title {key}", "authors": ["Ada Lovelace", "Alan Turing"], "year": 2020,
           "doi": f"10.1/{key}", "journal": "J. Data", "url": None}
    row.update(over)
    return row


def publication(name="tv4_two_versions_one_sentence_changed", arxiv_id=fx.ID):
    elements, hierarchy = deduplicate_elements(fx.SINGLE_PUBLICATION_FIXTURES[name]())
    return elements, hierarchy, [bib(arxiv_id, "vaswani2017"), bib(arxiv_id, "bert", year=None)]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digests(directory):
    return {name: digest(Path(directory) / name) for name in ALL_FILES}


def read(directory, table):
    return pq.read_table(Path(directory) / export.TABLE_FILES[table])


def leftovers(directory):
    return sorted(p.name for p in Path(directory).iterdir() if p.name not in ALL_FILES)


@pytest.fixture
def exported(tmp_path):
    elements, hierarchy, bibtex = publication()
    export.export_tables(elements, hierarchy, bibtex, tmp_path / "out")
    return tmp_path / "out", (elements, hierarchy, bibtex)


# ------------------------------------------------------------------ schema --

def test_schemas_match_decision_d9_exactly():
    d = pa.dictionary(pa.int32(), pa.string())
    assert [(f.name, f.type, f.nullable) for f in export.ELEMENTS_SCHEMA] == [
        ("element_id", pa.string(), False), ("arxiv_id", pa.string(), False),
        ("kind", d, True), ("content", pa.string(), True)]
    assert [(f.name, f.type, f.nullable) for f in export.HIERARCHY_SCHEMA] == [
        ("arxiv_id", pa.string(), False), ("version", pa.int32(), True),
        ("element_id", pa.string(), False), ("parent_id", pa.string(), False)]
    assert [(f.name, f.type, f.nullable) for f in export.BIBTEX_SCHEMA] == [
        ("arxiv_id", pa.string(), False), ("citation_key", pa.string(), False),
        ("entry_type", d, True), ("title", pa.string(), True),
        ("authors", pa.list_(pa.string()), True), ("year", pa.int32(), True),
        ("doi", pa.string(), True), ("journal", pa.string(), True),
        ("url", pa.string(), True)]
    assert export.HIERARCHY_INTERNAL_SCHEMA.names == export.HIERARCHY_SCHEMA.names + ["position"]
    assert not export.HIERARCHY_INTERNAL_SCHEMA.field("position").nullable


def test_files_are_written_with_exact_schemas_and_row_counts(exported):
    out, (elements, hierarchy, bibtex) = exported
    for table, rows in (("elements", elements), ("hierarchy", hierarchy),
                        ("hierarchy_internal", hierarchy), ("bibtex", bibtex)):
        data = read(out, table)
        assert data.schema.equals(export.SCHEMAS[table], check_metadata=False)
        assert data.num_rows == len(rows)
    assert (len(elements), len(hierarchy)) == (8, 8)  # hand count of example (a)


def test_position_is_only_in_the_internal_hierarchy(exported):
    out, _ = exported
    assert "position" not in read(out, "hierarchy").column_names
    assert "position" in read(out, "hierarchy_internal").column_names


def test_kind_and_entry_type_are_dictionary_encoded_after_reading(exported):
    out, _ = exported
    assert pa.types.is_dictionary(read(out, "elements").schema.field("kind").type)
    assert pa.types.is_dictionary(read(out, "bibtex").schema.field("entry_type").type)


def test_integer_widths_and_year_null(exported):
    out, _ = exported
    bibtex = read(out, "bibtex").to_pylist()
    assert {r["citation_key"]: r["year"] for r in bibtex} == {"vaswani2017": 2020, "bert": None}
    assert read(out, "hierarchy").schema.field("version").type == pa.int32()
    assert read(out, "bibtex").schema.field("year").type == pa.int32()


def test_authors_round_trip_as_lists_and_none(tmp_path):
    elements, hierarchy, _ = publication()
    rows = [bib(fx.ID, "a"), bib(fx.ID, "b", authors=[]), bib(fx.ID, "c", authors=None)]
    export.export_tables(elements, hierarchy, rows, tmp_path)
    got = {r["citation_key"]: r["authors"] for r in read(tmp_path, "bibtex").to_pylist()}
    assert got == {"a": ["Ada Lovelace", "Alan Turing"], "b": [], "c": None}


def test_pandas_can_read_the_tables(exported):
    pd = pytest.importorskip("pandas")
    out, _ = exported
    frame = pd.read_parquet(out / "hierarchy.parquet")
    assert list(frame.columns) == ["arxiv_id", "version", "element_id", "parent_id"]
    assert len(frame) == 8


# ------------------------------------------------------ strict row checks ---

@pytest.mark.parametrize("table", ["elements", "hierarchy", "bibtex"])
def test_column_outside_the_schema_is_rejected(tmp_path, table):
    elements, hierarchy, bibtex = publication()
    rows = {"elements": elements, "hierarchy": hierarchy, "bibtex": bibtex}
    rows[table] = [dict(rows[table][0], surprise=1)] + rows[table][1:]
    with pytest.raises(ValueError, match="outside the schema"):
        export.export_tables(rows["elements"], rows["hierarchy"], rows["bibtex"], tmp_path)
    assert not tmp_path.exists() or leftovers(tmp_path) == [] and not list(tmp_path.glob("*.parquet"))


def test_position_is_not_accepted_in_elements(tmp_path):
    elements, hierarchy, bibtex = publication()
    elements[0]["position"] = 1
    with pytest.raises(ValueError, match="outside the schema"):
        export.export_tables(elements, hierarchy, bibtex, tmp_path)


@pytest.mark.parametrize("table,column", [
    ("elements", "element_id"), ("elements", "arxiv_id"), ("elements", "kind"),
    ("elements", "content"), ("hierarchy", "version"), ("hierarchy", "element_id"),
    ("hierarchy", "parent_id"), ("hierarchy", "position"), ("bibtex", "citation_key"),
    ("bibtex", "arxiv_id")])
def test_missing_or_null_required_values_are_rejected(tmp_path, table, column):
    elements, hierarchy, bibtex = publication()
    rows = {"elements": elements, "hierarchy": hierarchy, "bibtex": bibtex}
    rows[table][0][column] = None
    with pytest.raises(ValueError, match="required"):
        export.export_tables(rows["elements"], rows["hierarchy"], rows["bibtex"], tmp_path)
    rows[table][0].pop(column)
    with pytest.raises(ValueError, match="required"):
        export.export_tables(rows["elements"], rows["hierarchy"], rows["bibtex"], tmp_path)


def test_wrong_value_types_are_rejected(tmp_path):
    elements, hierarchy, bibtex = publication()
    for bad in ({"year": "2020"}, {"year": True}, {"year": 2 ** 40}, {"year": 2020.5},
                {"authors": "Ada"}, {"authors": ["Ada", None]}, {"authors": [1]},
                {"title": 5}, {"entry_type": 3}):
        with pytest.raises(ValueError):
            export.export_tables(elements, hierarchy, [bib(fx.ID, "x", **bad)], tmp_path)
    hierarchy[0]["version"] = True
    with pytest.raises(ValueError):
        export.export_tables(elements, hierarchy, bibtex, tmp_path)


def test_empty_tables_are_valid(tmp_path):
    export.export_tables([], [], [], tmp_path)
    for table in export.SCHEMAS:
        data = read(tmp_path, table)
        assert data.num_rows == 0
        assert data.schema.equals(export.SCHEMAS[table], check_metadata=False)


# ------------------------------------------------------------- determinism --

def test_two_exports_give_identical_bytes(tmp_path):
    elements, hierarchy, bibtex = publication()
    export.export_tables(elements, hierarchy, bibtex, tmp_path / "a")
    export.export_tables(elements, hierarchy, bibtex, tmp_path / "b")
    assert digests(tmp_path / "a") == digests(tmp_path / "b")


def test_input_row_order_does_not_change_the_bytes(tmp_path):
    elements, hierarchy, bibtex = publication("tv4_three_versions_reordered")
    export.export_tables(elements, hierarchy, bibtex, tmp_path / "a")
    rng = random.Random(7)
    for rows in (elements, hierarchy, bibtex):
        rng.shuffle(rows)
    export.export_tables(elements, hierarchy, bibtex, tmp_path / "b")
    assert digests(tmp_path / "a") == digests(tmp_path / "b")


def test_rows_are_written_in_documented_sort_order(exported):
    out, _ = exported
    ids = read(out, "elements").column("element_id").to_pylist()
    assert ids == sorted(ids)
    keys = [(r["arxiv_id"], r["citation_key"]) for r in read(out, "bibtex").to_pylist()]
    assert keys == sorted(keys)
    rows = read(out, "hierarchy_internal").to_pylist()
    assert [(r["arxiv_id"], r["version"], r["position"]) for r in rows] == sorted(
        (r["arxiv_id"], r["version"], r["position"]) for r in rows)


def test_no_timestamp_or_pandas_metadata_in_files(exported):
    out, _ = exported
    for name in ALL_FILES:
        metadata = pq.ParquetFile(out / name).metadata
        assert metadata.created_by.startswith("parquet-cpp-arrow")
        assert b"pandas" not in (metadata.metadata or {})


# ---------------------------------------------------- order recovery (D6) ---

def test_hierarchy_row_order_is_the_dfs_preorder_of_each_version(exported):
    out, _ = exported
    plain = [(r["version"], r["element_id"], r["parent_id"])
             for r in read(out, "hierarchy").to_pylist()]
    internal = sorted(read(out, "hierarchy_internal").to_pylist(),
                      key=lambda r: (r["arxiv_id"], r["version"], r["position"]))
    assert plain == [(r["version"], r["element_id"], r["parent_id"]) for r in internal]
    for version in (1, 2):
        positions = [r["position"] for r in internal if r["version"] == version]
        assert positions == [1, 2, 3, 4]


# ----------------------------------------------------------- safe writing ---

def test_success_leaves_no_temporary_files(exported):
    out, _ = exported
    assert leftovers(out) == []


def test_failed_export_keeps_previous_files_untouched(tmp_path):
    elements, hierarchy, bibtex = publication()
    out = tmp_path / "out"
    export.export_tables(elements, hierarchy, bibtex, out)
    before = digests(out)
    changed = [dict(r, content="changed") for r in elements]
    with pytest.raises(ValueError):
        export.export_tables(changed, hierarchy, [bib(fx.ID, "x", year="bad")], out)
    assert digests(out) == before
    assert leftovers(out) == []


def test_failure_while_writing_cleans_up_and_keeps_old_output(tmp_path, monkeypatch):
    elements, hierarchy, bibtex = publication()
    out = tmp_path / "out"
    export.export_tables(elements, hierarchy, bibtex, out)
    before = digests(out)
    real = pq.write_table
    calls = []

    def flaky(table, where, *args, **kwargs):
        calls.append(where)
        if len(calls) == 3:
            raise OSError("disk full")
        return real(table, where, *args, **kwargs)

    monkeypatch.setattr(export.pq, "write_table", flaky)
    with pytest.raises(OSError):
        export.export_tables([dict(r, content="new") for r in elements], hierarchy, bibtex, out)
    assert digests(out) == before
    assert leftovers(out) == []


# ------------------------------------------------------------------ parts ---

def corpus():
    """Two publications whose keys are prefixes of each other plus an old-style id."""
    specs = [("tv4_two_versions_one_sentence_changed", "2310.1234"),
             ("tv4_three_versions_reordered", "2310.12345"),
             ("tv4_old_style_id", fx.OLD_ID)]
    out = {}
    for name, arxiv_id in specs:
        docs = fx.SINGLE_PUBLICATION_FIXTURES[name]()
        for d in docs:
            d.arxiv_id = arxiv_id
        elements, hierarchy = deduplicate_elements(docs)
        out[arxiv_id] = (elements, hierarchy, [bib(arxiv_id, "k1"), bib(arxiv_id, "k0")])
    return out


def test_merge_parts_equals_a_direct_export_of_everything(tmp_path):
    data = corpus()
    for arxiv_id, (e, h, b) in data.items():
        export.write_part(arxiv_id, e, h, b, tmp_path / "parts")
    export.merge_parts(tmp_path / "parts", tmp_path / "merged")
    everything = [sum((data[a][i] for a in data), []) for i in range(3)]
    export.export_tables(*everything, tmp_path / "direct")
    for table in export.SCHEMAS:
        assert read(tmp_path / "merged", table).equals(read(tmp_path / "direct", table)), table


def test_merge_is_independent_of_the_order_parts_were_written(tmp_path):
    data = corpus()
    for order, name in ((list(data), "a"), (list(reversed(list(data))), "b")):
        for arxiv_id in order:
            export.write_part(arxiv_id, *data[arxiv_id], tmp_path / f"parts_{name}")
        export.merge_parts(tmp_path / f"parts_{name}", tmp_path / f"out_{name}")
    assert digests(tmp_path / "out_a") == digests(tmp_path / "out_b")


def test_merge_with_no_parts_writes_empty_tables(tmp_path):
    (tmp_path / "parts").mkdir()
    export.merge_parts(tmp_path / "parts", tmp_path / "out")
    for table in export.SCHEMAS:
        assert read(tmp_path / "out", table).num_rows == 0


def test_rewriting_a_part_replaces_it(tmp_path):
    e, h, b = publication()
    export.write_part(fx.ID, e, h, b, tmp_path / "parts")
    export.write_part(fx.ID, e, h, b[:1], tmp_path / "parts")
    export.merge_parts(tmp_path / "parts", tmp_path / "out")
    assert read(tmp_path / "out", "bibtex").num_rows == 1
    assert leftovers(tmp_path / "parts") == ["bibtex", "elements", "hierarchy",
                                             "hierarchy_internal"]


@pytest.mark.parametrize('rewrite', [False, True])
@pytest.mark.parametrize('failed_call', [2, 4])
def test_part_publish_failure_rolls_back_all_tables(tmp_path, monkeypatch, rewrite, failed_call):
    parts = tmp_path / 'parts'
    e, h, b = publication()
    if rewrite:
        export.write_part(fx.ID, e, h, b[:1], parts)
    before = {p.relative_to(parts): p.read_bytes() for p in parts.rglob('*.parquet')}
    replace = export.os.replace
    calls = 0

    def fail_once(source, target):
        nonlocal calls
        calls += 1
        if calls == failed_call:
            raise PermissionError('injected publish failure')
        return replace(source, target)

    monkeypatch.setattr(export.os, 'replace', fail_once)
    with pytest.raises(PermissionError, match='injected'):
        export.write_part(fx.ID, e, h, b, parts)
    assert {p.relative_to(parts): p.read_bytes() for p in parts.rglob('*.parquet')} == before
    export.merge_parts(parts, tmp_path / 'merged')
    assert read(tmp_path / 'merged', 'bibtex').num_rows == (1 if rewrite else 0)


def test_write_part_rejects_rows_of_another_publication(tmp_path):
    e, h, b = publication()
    with pytest.raises(ValueError, match="other publications"):
        export.write_part(fx.OTHER_ID, e, h, b, tmp_path / "parts")
    assert not (tmp_path / "parts" / "elements").exists()


def test_failed_rollback_retains_previous_part_for_recovery(tmp_path, monkeypatch):
    parts = tmp_path / 'parts'
    e, h, b = publication()
    export.write_part(fx.ID, e, h, b[:1], parts)
    replace = export.os.replace
    calls = 0

    def fail_publish_and_restore(source, target):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise PermissionError('persistent lock')
        return replace(source, target)

    monkeypatch.setattr(export.os, 'replace', fail_publish_and_restore)
    with pytest.raises(export.PartRollbackError, match='recovery files retained'):
        export.write_part(fx.ID, e, h, b, parts)
    assert list(parts.glob('.export-*/*.previous'))


def test_merge_rejects_a_part_with_a_mismatched_file_name(tmp_path):
    e, h, b = publication()
    export.write_part(fx.ID, e, h, b, tmp_path / "parts")
    for table in export.SCHEMAS:
        (tmp_path / "parts" / table / f"{paper_key(fx.ID)}.parquet").rename(
            tmp_path / "parts" / table / "9999-99999.parquet")
    with pytest.raises(ValueError, match="does not match"):
        export.merge_parts(tmp_path / "parts", tmp_path / "out")
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.parquet"))


def test_merge_rejects_a_part_with_the_wrong_schema(tmp_path):
    e, h, b = publication()
    export.write_part(fx.ID, e, h, b, tmp_path / "parts")
    path = tmp_path / "parts" / "bibtex" / f"{paper_key(fx.ID)}.parquet"
    pq.write_table(pa.table({"arxiv_id": [fx.ID], "citation_key": ["k"]}), path)
    with pytest.raises(ValueError, match="schema differs"):
        export.merge_parts(tmp_path / "parts", tmp_path / "out")


# ------------------------------------------------------ bibtex conversion ---

def entry(key="key1", **fields):
    return SimpleNamespace(original_key=key, entry_type="article", fields=fields)


def test_bibtex_rows_map_tv3_entries_to_table_rows():
    rows = export.bibtex_rows(fx.ID, [
        entry("k1", title="T", authors=["A B"], year=2019, doi="10.1/x", journal="J",
              url="http://u", raw="ignored", source_keys={1: ["old"]}),
        entry("k2")])
    assert rows[0] == {"arxiv_id": fx.ID, "citation_key": "k1", "entry_type": "article",
                       "title": "T", "authors": ["A B"], "year": 2019, "doi": "10.1/x",
                       "journal": "J", "url": "http://u"}
    assert rows[1]["authors"] == [] and rows[1]["year"] is None and rows[1]["title"] is None
    assert set(rows[0]) == set(export.BIBTEX_SCHEMA.names)


def test_bibtex_rows_export_cleanly(tmp_path):
    e, h, _ = publication()
    export.export_tables(e, h, export.bibtex_rows(fx.ID, [entry("k1", year=2019), entry("k2")]),
                         tmp_path)
    assert read(tmp_path, "bibtex").num_rows == 2


# ------------------------------------------------ end to end with dedup -----

def test_dedup_output_exports_with_the_hand_computed_sizes(tmp_path):
    expected = {"tv4_two_versions_one_sentence_changed": (8, 8),
                "tv4_three_versions_reordered": (10, 15), "tv4_no_tex_middle_version": (5, 6)}
    for name, (n_elements, n_rows) in expected.items():
        elements, hierarchy = deduplicate_elements(fx.SINGLE_PUBLICATION_FIXTURES[name]())
        out = tmp_path / name
        export.export_tables(elements, hierarchy, [], out)
        assert read(out, "elements").num_rows == n_elements
        assert read(out, "hierarchy").num_rows == n_rows
