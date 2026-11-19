"""TV4 — validate exported M2 tables, independently of the code that wrote them.

Everything is read from the Parquet files in ``output_dir``. The expected schema is
declared here on purpose (m2/docs/decisions/dedup_export.md D9, Contract §2.4) and is NOT
imported from ``export.py``, so a mistake in the exporter cannot validate itself.

Memory: each table is read column-wise (``content`` is only streamed in batches),
checks that need per-version graphs run one publication at a time, and cycle
detection is an iterative DFS, so deep documents cannot overflow the stack.

Result of :func:`validate_tables`::

    {"passed": bool,                 # True when there is no error (warnings allowed)
     "errors": [issue, ...],         # at most MAX_EXAMPLES examples per check
     "warnings": [issue, ...],
     "counts": {...}}                # totals per check, table sizes, coverage ratios

``issue`` is ``{check, severity, arxiv_id, version, element_id, message}``.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from m1.src.utils.common import paper_key

MAX_EXAMPLES = 50
BATCH_ROWS = 65536

_DICT = pa.dictionary(pa.int32(), pa.string())

# (column, type, nullable) per file, in file order. Declared independently of export.py.
EXPECTED_SCHEMAS: dict[str, list[tuple[str, pa.DataType, bool]]] = {
    "elements": [("element_id", pa.string(), False), ("arxiv_id", pa.string(), False),
                 ("kind", _DICT, True), ("content", pa.string(), True)],
    "hierarchy": [("arxiv_id", pa.string(), False), ("version", pa.int32(), True),
                  ("element_id", pa.string(), False), ("parent_id", pa.string(), False)],
    "bibtex": [("arxiv_id", pa.string(), False), ("citation_key", pa.string(), False),
               ("entry_type", _DICT, True), ("title", pa.string(), True),
               ("authors", pa.list_(pa.string()), True), ("year", pa.int32(), True),
               ("doi", pa.string(), True), ("journal", pa.string(), True),
               ("url", pa.string(), True)],
    "hierarchy_internal": [("arxiv_id", pa.string(), False), ("version", pa.int32(), True),
                           ("element_id", pa.string(), False),
                           ("parent_id", pa.string(), False), ("position", pa.int32(), False)],
}
FILES = {name: f"{name}.parquet" for name in EXPECTED_SCHEMAS}
REQUIRED_TABLES = ("elements", "hierarchy", "bibtex")

# Columns loaded into memory per table (``content`` is streamed separately).
READ_COLUMNS = {
    "elements": ["element_id", "arxiv_id", "kind"],
    "hierarchy": ["arxiv_id", "version", "element_id", "parent_id"],
    "bibtex": ["arxiv_id", "citation_key"],
    "hierarchy_internal": ["arxiv_id", "version", "element_id", "parent_id", "position"],
}

# Columns that must hold a real value even where the schema allows null (D9);
# ``elements.content`` is checked while streaming.
MUST_HAVE_VALUE = {
    "elements": ("element_id", "arxiv_id", "kind"),
    "hierarchy": ("arxiv_id", "version", "element_id", "parent_id"),
    "bibtex": ("arxiv_id", "citation_key"),
    "hierarchy_internal": ("arxiv_id", "version", "element_id", "parent_id", "position"),
}
NOT_EMPTY = {"elements": ("element_id", "arxiv_id"),
             "hierarchy": ("arxiv_id", "element_id", "parent_id"),
             "bibtex": ("arxiv_id", "citation_key")}

HEADING_KINDS = ("part", "chapter", "section", "subsection", "subsubsection",
                 "paragraph_heading", "subparagraph_heading", "heading")
ROOT_KIND = "document"

_CITE = re.compile(r"\\[A-Za-z]*[Cc]ite[A-Za-z]*\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^{}]*)\}")
_REFERENCE_HEADING = re.compile(
    r"^\s*(?:[0-9]+(?:\.[0-9]+)*\.?|[ivxlcIVXLC]+\.)?\s*"
    r"(?:references?|bibliography|works\s+cited|literature\s+cited)\s*[.:]?\s*$", re.IGNORECASE)
_ROOT_ID = re.compile(r"#root(\d+)$")

SEVERITY = {
    # errors
    "missing_file": "error", "unreadable_file": "error", "schema": "error",
    "null_value": "error", "empty_value": "error", "invalid_version": "error",
    "duplicate_element_id": "error", "unknown_reference": "error",
    "root_not_document": "error", "document_as_child": "error", "self_loop": "error",
    "cycle": "error", "publication_prefix": "error", "duplicate_citation_key": "error",
    "root_count": "error", "internal_mismatch": "error",
    # warnings
    "multi_parent": "warning", "duplicate_edge": "warning", "cite_key_missing": "warning",
    "reference_heading": "warning", "root_id_convention": "warning",
}


# ------------------------------------------------------------------ report --

class _Report:
    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.totals: dict[str, dict[str, Any]] = {}
        self.counts: dict[str, Any] = {}

    def bulk(self, check: str, total: int, examples: Iterable[dict[str, Any]]) -> None:
        """Record ``total`` findings of ``check`` and keep up to MAX_EXAMPLES examples."""
        if total <= 0:
            return
        severity = SEVERITY[check]
        entry = self.totals.setdefault(check, {"severity": severity, "total": 0,
                                               "examples": 0})
        entry["total"] += total
        target = self.errors if severity == "error" else self.warnings
        for example in examples:
            if entry["examples"] >= MAX_EXAMPLES:
                break
            entry["examples"] += 1
            target.append({"check": check, "severity": severity,
                           "arxiv_id": example.get("arxiv_id"),
                           "version": example.get("version"),
                           "element_id": example.get("element_id"),
                           "message": example["message"]})

    def add(self, check: str, message: str, **where: Any) -> None:
        self.bulk(check, 1, [dict(where, message=message)])

    def result(self) -> dict[str, Any]:
        counts = dict(self.counts)
        counts["issues"] = {k: dict(v) for k, v in sorted(self.totals.items())}
        counts["error_total"] = sum(v["total"] for v in self.totals.values()
                                    if v["severity"] == "error")
        counts["warning_total"] = sum(v["total"] for v in self.totals.values()
                                      if v["severity"] == "warning")
        return {"passed": counts["error_total"] == 0, "errors": self.errors,
                "warnings": self.warnings, "counts": counts}


def _ratio(numerator: int, denominator: int, definition: str) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator,
            "denominator_definition": definition,
            "value": numerator / denominator if denominator else None}


# ------------------------------------------------------------ arrow helpers -

def _flag(report: _Report, table: pa.Table, mask: pa.Array, check: str,
          message: Callable[[dict[str, Any]], str]) -> int:
    """Record every row where ``mask`` is true (examples are taken from ``table``)."""
    mask = pc.fill_null(mask, False)
    total = pc.sum(pc.cast(mask, pa.int64())).as_py() or 0
    if total:
        indices = pc.indices_nonzero(mask)[:MAX_EXAMPLES]
        rows = table.take(indices).to_pylist()
        report.bulk(check, total, [dict(row, message=message(row)) for row in rows])
    return total


def _read(output_dir: Path, name: str, columns: list[str] | None = None) -> pa.Table:
    table = pq.read_table(output_dir / FILES[name], columns=columns).combine_chunks()
    for index, field in enumerate(table.schema):
        if pa.types.is_dictionary(field.type):
            table = table.set_column(index, field.name, pc.cast(table[field.name], pa.string()))
    return table


def _schema_issues(schema: pa.Schema,
                   expected: list[tuple[str, pa.DataType, bool]]) -> tuple[list[str], list[str]]:
    """``(structural, nullability)`` problems. Only structural ones make a table unreadable
    for the data checks; a wrongly declared nullability still lets them run."""
    names, want = schema.names, [e[0] for e in expected]
    if names != want:
        return [f"columns are {names}, expected {want} in this order"], []
    structural, nullability = [], []
    for field, (name, arrow_type, nullable) in zip(schema, expected):
        if not field.type.equals(arrow_type):
            structural.append(f"column {name!r} has type {field.type}, expected {arrow_type}")
        if field.nullable != nullable:
            nullability.append(f"column {name!r} is declared "
                               f"{'nullable' if field.nullable else 'non-nullable'}, "
                               f"expected {'nullable' if nullable else 'non-nullable'}")
    return structural, nullability


def _group_rows(table: pa.Table, key: str = "arxiv_id") -> dict[str, pa.Table]:
    """Split ``table`` by ``key`` into per-publication tables, preserving row order."""
    if table.num_rows == 0:
        return {}
    indexed = pa.table({"k": table[key], "i": pa.array(range(table.num_rows), pa.int64())})
    grouped = indexed.group_by("k", use_threads=False).aggregate([("i", "list")])
    out = {}
    for row in grouped.to_pylist():
        if row["k"] is not None:
            out[row["k"]] = table.take(pa.array(sorted(row["i_list"]), pa.int64()))
    return out


# --------------------------------------------------------------- checks -----

def _check_schemas(report: _Report, output_dir: Path) -> dict[str, bool]:
    """Missing/unreadable files and per-column schema. Returns {table: usable}."""
    usable = {}
    for name, expected in EXPECTED_SCHEMAS.items():
        path = output_dir / FILES[name]
        if not path.exists():
            if name in REQUIRED_TABLES:
                report.add("missing_file", f"{FILES[name]} is missing")
            usable[name] = False
            continue
        try:
            schema = pq.read_schema(path)
        except (OSError, pa.ArrowException) as error:
            report.add("unreadable_file", f"{FILES[name]} cannot be read: {error}")
            usable[name] = False
            continue
        structural, nullability = _schema_issues(schema, expected)
        for problem in structural + nullability:
            report.add("schema", f"{FILES[name]}: {problem}")
        usable[name] = not structural
    return usable


def _check_values(report: _Report, name: str, table: pa.Table) -> None:
    """Nulls, empty strings and impossible versions in one table."""
    for column in MUST_HAVE_VALUE[name]:
        _flag(report, table, pc.is_null(table[column]), "null_value",
              lambda r, c=column: f"{name}.{c} is null")
    for column in NOT_EMPTY.get(name, ()):
        _flag(report, table, pc.equal(table[column], ""), "empty_value",
              lambda r, c=column: f"{name}.{c} is an empty string")
    if "version" in table.column_names:
        _flag(report, table, pc.less(table["version"], 1), "invalid_version",
              lambda r: f"version {r['version']} is not a positive integer")


def _check_prefixes(report: _Report, name: str, table: pa.Table, column: str) -> None:
    """Every id in ``column`` must start with ``<paper_key(arxiv_id)>#`` of its own row."""
    ids = pc.unique(pc.drop_null(table["arxiv_id"]))
    if len(ids) == 0:
        return
    keys = pa.array([paper_key(i) for i in ids.to_pylist()], pa.string())
    expected = pc.take(keys, pc.index_in(table["arxiv_id"], value_set=ids))
    head = pc.list_element(pc.split_pattern(table[column], "#", max_splits=1), 0)
    ok = pc.and_(pc.match_substring(table[column], "#"), pc.equal(head, expected))
    bad = pc.and_(pc.is_valid(table[column]), pc.invert(ok))
    _flag(report, table, bad, "publication_prefix",
          lambda r: f"{name}.{column} {r[column]!r} does not start with "
                    f"'{paper_key(r['arxiv_id'])}#'")


def _duplicates(values: pa.Array) -> list[Any]:
    counts = pc.value_counts(values)
    return sorted(counts.field("values").filter(pc.greater(counts.field("counts"), 1)).to_pylist())


def _check_uniqueness(report: _Report, elements: pa.Table | None, bibtex: pa.Table | None) -> None:
    if elements is not None:
        duplicated = _duplicates(pc.drop_null(elements["element_id"]))
        report.bulk("duplicate_element_id", len(duplicated),
                    [{"element_id": e, "message": f"element_id {e!r} appears more than once"}
                     for e in duplicated])
    if bibtex is not None:
        keys = pc.binary_join_element_wise(
            pc.fill_null(bibtex["arxiv_id"], ""), pc.fill_null(bibtex["citation_key"], ""),
            "\x00")
        duplicated = _duplicates(keys)
        examples = []
        for key in duplicated:
            arxiv_id, citation_key = key.split("\x00", 1)
            examples.append({"arxiv_id": arxiv_id,
                             "message": f"citation_key {citation_key!r} is duplicated"})
        report.bulk("duplicate_citation_key", len(duplicated), examples)


def _check_references(report: _Report, elements: pa.Table, hierarchy: pa.Table) -> None:
    known = pc.drop_null(elements["element_id"])
    for column in ("element_id", "parent_id"):
        missing = pc.and_(pc.is_valid(hierarchy[column]),
                          pc.invert(pc.is_in(hierarchy[column], value_set=known)))
        _flag(report, hierarchy, missing, "unknown_reference",
              lambda r, c=column: f"hierarchy.{c} {r[c]!r} is not in elements")


def _find_cycle(edges: Iterable[tuple[str, str]]) -> str | None:
    """Element on a cycle among ``(child, parent)`` edges (self-loops excluded), if any.

    Iterative three-colour DFS. Elements may have several parents.
    """
    parents: dict[str, list[str]] = {}
    for child, parent in edges:
        if child != parent:
            parents.setdefault(child, []).append(parent)
    state: dict[str, int] = {}
    for start in sorted(parents):
        if state.get(start) == 2:
            continue
        state[start] = 1
        stack = [(start, iter(sorted(set(parents.get(start, ())))))]
        while stack:
            current, neighbours = stack[-1]
            for nxt in neighbours:
                if state.get(nxt) == 1:
                    return nxt
                if state.get(nxt) is None:
                    state[nxt] = 1
                    stack.append((nxt, iter(sorted(set(parents.get(nxt, ()))))))
                    break
            else:
                state[current] = 2
                stack.pop()
    return None


def _check_graphs(report: _Report, elements: pa.Table, hierarchy: pa.Table) -> None:
    """Per (arxiv_id, version): roots, document rules, multi-parent, edges, cycles."""
    kinds_by_pub = {a: dict(zip(t["element_id"].to_pylist(), t["kind"].to_pylist()))
                    for a, t in _group_rows(elements).items()}
    versions = 0
    for arxiv_id, rows in sorted(_group_rows(hierarchy).items()):
        kinds = kinds_by_pub.get(arxiv_id, {})
        by_version: dict[int, list[tuple[str, str]]] = {}
        for version, element_id, parent_id in zip(rows["version"].to_pylist(),
                                                  rows["element_id"].to_pylist(),
                                                  rows["parent_id"].to_pylist()):
            if version is None or element_id is None or parent_id is None:
                continue  # already reported as null_value
            by_version.setdefault(version, []).append((element_id, parent_id))
        for version, edges in sorted(by_version.items()):
            versions += 1
            where = {"arxiv_id": arxiv_id, "version": version}
            element_ids = {e for e, _ in edges}
            tops = sorted({p for _, p in edges if p not in element_ids})
            if len(tops) != 1:
                report.add("root_count", f"{len(tops)} roots found, expected exactly 1"
                           + (f": {tops[:5]}" if tops else ""), **where)
            for top in tops:
                if top in kinds and kinds[top] != ROOT_KIND:
                    report.add("root_not_document", f"root {top!r} has kind {kinds[top]!r}, "
                               f"expected {ROOT_KIND!r}", element_id=top, **where)
                expected_id = f"{paper_key(arxiv_id)}#root{version}"
                if kinds.get(top) == ROOT_KIND and top != expected_id:
                    report.add("root_id_convention", f"root id is {top!r}, convention is "
                               f"{expected_id!r}", element_id=top, **where)
            for element_id in sorted(element_ids):
                if kinds.get(element_id) == ROOT_KIND:
                    report.add("document_as_child", f"document {element_id!r} is listed as a "
                               f"child", element_id=element_id, **where)
            for child, parent in edges:
                if child == parent:
                    report.add("self_loop", f"{child!r} is its own parent", element_id=child,
                               **where)
            on_cycle = _find_cycle(edges)
            if on_cycle is not None:
                report.add("cycle", f"cycle through {on_cycle!r}", element_id=on_cycle, **where)
            parents: dict[str, set[str]] = {}
            for child, parent in edges:
                parents.setdefault(child, set()).add(parent)
            multi = sorted(c for c, p in parents.items() if len(p) > 1)
            report.bulk("multi_parent", len(multi),
                        [dict(where, element_id=c, message=f"{c!r} has {len(parents[c])} parents "
                              f"in this version") for c in multi])
            repeated = sorted(e for e, n in Counter(edges).items() if n > 1)
            report.bulk("duplicate_edge", len(repeated),
                        [dict(where, element_id=c, message=f"edge ({c!r}, {p!r}) is repeated")
                         for c, p in repeated])
    report.counts["versions_with_hierarchy"] = versions


def _check_content(report: _Report, output_dir: Path, bibtex: pa.Table,
                   hierarchy: pa.Table) -> None:
    """Stream ``content``: \\cite keys without a bibtex entry; leftover reference headings."""
    bib_keys: dict[str, set[str]] = {}
    for arxiv_id, key in zip(bibtex["arxiv_id"].to_pylist(), bibtex["citation_key"].to_pylist()):
        bib_keys.setdefault(arxiv_id, set()).add(key)
    cited: dict[tuple[str, str], str] = {}
    heading_candidates: dict[str, str] = {}
    reader = pq.ParquetFile(output_dir / FILES["elements"])
    columns = ["element_id", "arxiv_id", "kind", "content"]
    for batch in reader.iter_batches(batch_size=BATCH_ROWS, columns=columns):
        table = pa.Table.from_batches([batch])
        _flag(report, table, pc.is_null(table["content"]), "null_value",
              lambda r: "elements.content is null")
        kind = pc.cast(table["kind"], pa.string())
        has_cite = pc.match_substring_regex(pc.fill_null(table["content"], ""), r"[Cc]ite")
        for row in table.filter(has_cite).to_pylist():
            for group in _CITE.findall(row["content"] or ""):
                for key in (k.strip() for k in group.split(",")):
                    if key and key != "*":
                        cited.setdefault((row["arxiv_id"], key), row["element_id"])
        is_heading = pc.is_in(kind, value_set=pa.array(HEADING_KINDS, pa.string()))
        for row in table.filter(is_heading).to_pylist():
            if _REFERENCE_HEADING.match(row["content"] or ""):
                heading_candidates[row["element_id"]] = row["arxiv_id"]
    missing = sorted((a, k, e) for (a, k), e in cited.items() if k not in bib_keys.get(a, set()))
    report.bulk("cite_key_missing", len(missing),
                [{"arxiv_id": a, "element_id": e,
                  "message": f"\\cite key {k!r} has no entry in bibtex"} for a, k, e in missing])
    report.counts["citation_coverage"] = _ratio(
        len(cited) - len(missing), len(cited),
        "distinct (arxiv_id, key) pairs cited in elements.content that have a bibtex row; "
        "denominator is all distinct (arxiv_id, key) pairs cited in elements.content")
    if heading_candidates:
        present = pc.is_in(hierarchy["element_id"],
                           value_set=pa.array(sorted(heading_candidates), pa.string()))
        used = sorted(set(pc.filter(hierarchy["element_id"], present).to_pylist()))
        report.bulk("reference_heading", len(used),
                    [{"arxiv_id": heading_candidates[e], "element_id": e,
                      "message": "References/Bibliography heading is still in the hierarchy"}
                     for e in used])


def _check_internal(report: _Report, output_dir: Path, hierarchy: pa.Table) -> None:
    """hierarchy_internal must carry the same rows as hierarchy, ordered by position."""
    internal = _read(output_dir, "hierarchy_internal")
    keys = pc.binary_join_element_wise(
        pc.fill_null(internal["arxiv_id"], ""), pc.cast(pc.fill_null(internal["version"], 0),
                                                        pa.string()),
        pc.cast(pc.fill_null(internal["position"], 0), pa.string()), "\x00")
    repeated = _duplicates(keys)
    report.bulk("internal_mismatch", len(repeated),
                [{"arxiv_id": k.split("\x00")[0],
                  "message": "position repeated within one version of hierarchy_internal"}
                 for k in repeated])
    order = pc.sort_indices(internal, sort_keys=[("arxiv_id", "ascending"),
                                                 ("version", "ascending"),
                                                 ("position", "ascending")])
    ordered = internal.take(order).select(["arxiv_id", "version", "element_id", "parent_id"])
    if ordered.num_rows != hierarchy.num_rows:
        report.add("internal_mismatch", f"hierarchy_internal has {ordered.num_rows} rows, "
                   f"hierarchy has {hierarchy.num_rows}")
    elif not ordered.equals(hierarchy.select(ordered.column_names)):
        report.add("internal_mismatch", "rows of hierarchy.parquet differ from "
                   "hierarchy_internal.parquet ordered by position")


# ------------------------------------------------------------------ public --

def validate_tables(output_dir: Path) -> dict:
    """Contract §3.4: validate the tables in ``output_dir`` and return a report dict."""
    output_dir = Path(output_dir)
    report = _Report()
    usable = _check_schemas(report, output_dir)
    tables: dict[str, pa.Table] = {}
    for name in EXPECTED_SCHEMAS:
        if usable[name]:
            try:
                tables[name] = _read(output_dir, name, READ_COLUMNS[name])
            except (OSError, pa.ArrowException) as error:
                report.add("unreadable_file", f"{FILES[name]} cannot be read: {error}")
                usable[name] = False
    report.counts["tables"] = {n: (tables[n].num_rows if n in tables else None)
                               for n in EXPECTED_SCHEMAS}
    skipped = [n for n in REQUIRED_TABLES if not usable[n]]
    for name, table in tables.items():
        _check_values(report, name, table)
    elements, hierarchy, bibtex = (tables.get(n) for n in REQUIRED_TABLES)
    _check_uniqueness(report, elements, bibtex)
    if elements is not None:
        _check_prefixes(report, "elements", elements, "element_id")
        report.counts["publications"] = len(pc.unique(pc.drop_null(elements["arxiv_id"])))
    if hierarchy is not None:
        _check_prefixes(report, "hierarchy", hierarchy, "element_id")
        _check_prefixes(report, "hierarchy", hierarchy, "parent_id")
    if elements is not None and hierarchy is not None:
        _check_references(report, elements, hierarchy)
        _check_graphs(report, elements, hierarchy)
        roots = elements.filter(pc.equal(elements["kind"], ROOT_KIND))["element_id"]
        childless = pc.invert(pc.is_in(roots, value_set=pc.drop_null(hierarchy["parent_id"])))
        report.counts["roots_without_children"] = pc.sum(pc.cast(childless, pa.int64())).as_py() or 0
    if elements is not None and hierarchy is not None and bibtex is not None:
        _check_content(report, output_dir, bibtex, hierarchy)
    if hierarchy is not None and "hierarchy_internal" in tables:
        _check_internal(report, output_dir, hierarchy)
    report.counts["skipped_tables"] = skipped
    return report.result()

