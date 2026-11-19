"""TV4 — write the three M2 tables (plus an internal ordering table) as Parquet.

Schemas follow ``docs/m2/tv4_decisions.md`` D9. Output is deterministic: rows are
sorted, compression/row-group/format are fixed and no timestamp is written, so
the same rows give byte-identical files (for the same pyarrow version, which is
recorded by Parquet in ``created_by``).

Files written to ``output_dir``:

* ``elements.parquet``, ``hierarchy.parquet`` (four columns, official schema),
  ``bibtex.parquet``;
* ``hierarchy_internal.parquet``: hierarchy plus ``position`` (D6), internal only.

Writes are staged in a private sub-directory and moved into place only after
every file was written, so an error never leaves a half-written or overwritten
export behind. For large corpora write one part per publication with
:func:`write_part` and combine them with :func:`merge_parts`; only one part is
held in memory at a time.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from ..utils.common import paper_key

_DICT = pa.dictionary(pa.int32(), pa.string())

ELEMENTS_SCHEMA = pa.schema([
    pa.field("element_id", pa.string(), nullable=False),
    pa.field("arxiv_id", pa.string(), nullable=False),
    pa.field("kind", _DICT),
    pa.field("content", pa.string()),
])

HIERARCHY_SCHEMA = pa.schema([
    pa.field("arxiv_id", pa.string(), nullable=False),
    pa.field("version", pa.int32()),
    pa.field("element_id", pa.string(), nullable=False),
    pa.field("parent_id", pa.string(), nullable=False),
])

HIERARCHY_INTERNAL_SCHEMA = pa.schema(
    list(HIERARCHY_SCHEMA) + [pa.field("position", pa.int32(), nullable=False)])

BIBTEX_SCHEMA = pa.schema([
    pa.field("arxiv_id", pa.string(), nullable=False),
    pa.field("citation_key", pa.string(), nullable=False),
    pa.field("entry_type", _DICT),
    pa.field("title", pa.string()),
    pa.field("authors", pa.list_(pa.string())),
    pa.field("year", pa.int32()),
    pa.field("doi", pa.string()),
    pa.field("journal", pa.string()),
    pa.field("url", pa.string()),
])

SCHEMAS: dict[str, pa.Schema] = {
    "elements": ELEMENTS_SCHEMA,
    "hierarchy": HIERARCHY_SCHEMA,
    "hierarchy_internal": HIERARCHY_INTERNAL_SCHEMA,
    "bibtex": BIBTEX_SCHEMA,
}

# Sort keys that make the output independent of input order (rule 6).
SORT_KEYS: dict[str, tuple[str, ...]] = {
    "elements": ("element_id",),
    "hierarchy": ("arxiv_id", "version", "position"),
    "hierarchy_internal": ("arxiv_id", "version", "position"),
    "bibtex": ("arxiv_id", "citation_key"),
}

# Which input columns each table accepts. ``hierarchy`` rows must carry ``position``
# (it feeds hierarchy_internal and the row order) but it is not written to hierarchy.
ROW_COLUMNS: dict[str, tuple[str, ...]] = {
    "elements": tuple(ELEMENTS_SCHEMA.names),
    "hierarchy": tuple(HIERARCHY_INTERNAL_SCHEMA.names),
    "bibtex": tuple(BIBTEX_SCHEMA.names),
}

TABLE_FILES = {name: f"{name}.parquet" for name in SCHEMAS}

COMPRESSION = "snappy"
ROW_GROUP_SIZE = 1 << 20
PARQUET_VERSION = "2.6"
DATA_PAGE_VERSION = "1.0"
_WRITE_OPTIONS: dict[str, Any] = dict(
    compression=COMPRESSION, version=PARQUET_VERSION, data_page_version=DATA_PAGE_VERSION,
    write_statistics=True, use_dictionary=True)


def table_path(output_dir: Path | str, table: str) -> Path:
    """Path of one exported table inside ``output_dir``."""
    return Path(output_dir) / TABLE_FILES[table]


# ----------------------------------------------------------------- rows -----

# Columns that must be present and non-null in INPUT rows. This is stricter than the
# schema on purpose (D9): kind, content and version are declared nullable because the
# specification does not require them, but TV4 never writes a null there.
REQUIRED_INPUT: dict[str, tuple[str, ...]] = {
    "elements": ("element_id", "arxiv_id", "kind", "content"),
    "hierarchy": ("arxiv_id", "version", "element_id", "parent_id", "position"),
    "bibtex": ("arxiv_id", "citation_key"),
}


def _check_rows(rows: Sequence[dict[str, Any]], table: str) -> None:
    allowed = set(ROW_COLUMNS[table])
    for index, row in enumerate(rows):
        extra = set(row) - allowed
        if extra:
            raise ValueError(f"{table} row {index}: column(s) outside the schema: "
                             f"{sorted(extra)}")
        for name in REQUIRED_INPUT[table]:
            if row.get(name) is None:
                raise ValueError(f"{table} row {index}: required column {name!r} is missing "
                                 f"or null")


def _sorted(rows: Sequence[dict[str, Any]], table: str) -> list[dict[str, Any]]:
    keys = SORT_KEYS[table]
    return sorted(rows, key=lambda r: tuple(r[k] for k in keys))


def _type_ok(value: Any, arrow_type: pa.DataType) -> bool:
    """Exact Python types only: pyarrow would silently split 'Ada' into characters for
    list<string> and truncate 2020.5 to 2020, so it is not trusted to validate."""
    if pa.types.is_integer(arrow_type):
        return type(value) is int
    if pa.types.is_string(arrow_type):
        return isinstance(value, str)
    if pa.types.is_dictionary(arrow_type):
        return _type_ok(value, arrow_type.value_type)
    if pa.types.is_list(arrow_type):
        return (isinstance(value, (list, tuple))
                and all(v is not None and _type_ok(v, arrow_type.value_type) for v in value))
    return True


def _column(values: list[Any], field: pa.Field, table: str) -> pa.Array:
    for value in values:
        if value is not None and not _type_ok(value, field.type):
            raise ValueError(f"{table}.{field.name}: {value!r} is not a valid "
                             f"{field.type} value")
    try:
        return pa.array(values, type=field.type)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as error:
        raise ValueError(f"{table}.{field.name}: {error}") from error


def _build(rows: Sequence[dict[str, Any]], table: str) -> pa.Table:
    """Validate, sort and convert ``rows`` to an Arrow table with the exact schema."""
    schema = SCHEMAS[table]
    source = "hierarchy" if table == "hierarchy_internal" else table
    _check_rows(rows, source)
    ordered = _sorted(rows, source)
    arrays = [_column([row.get(f.name) for row in ordered], f, table) for f in schema]
    return pa.Table.from_arrays(arrays, schema=schema)


def build_tables(elements: Sequence[dict[str, Any]], hierarchy: Sequence[dict[str, Any]],
                 bibtex: Sequence[dict[str, Any]]) -> dict[str, pa.Table]:
    """All four Arrow tables, validated and sorted. Raises ``ValueError`` on bad rows."""
    return {"elements": _build(elements, "elements"),
            "hierarchy": _build(hierarchy, "hierarchy"),
            "hierarchy_internal": _build(hierarchy, "hierarchy_internal"),
            "bibtex": _build(bibtex, "bibtex")}


def bibtex_rows(arxiv_id: str, entries: Iterable[Any]) -> list[dict[str, Any]]:
    """Map TV3 ``BibEntry`` objects (canonical key in ``original_key``) to bibtex rows.

    Duck-typed on ``original_key``, ``entry_type`` and ``fields``. A missing
    ``authors`` field becomes ``[]`` (TV3 convention); other missing fields become null.
    """
    rows = []
    for entry in entries:
        fields = entry.fields
        authors = fields.get("authors")
        rows.append({
            "arxiv_id": arxiv_id,
            "citation_key": entry.original_key,
            "entry_type": entry.entry_type,
            "title": fields.get("title"),
            "authors": [] if authors is None else list(authors),
            "year": fields.get("year"),
            "doi": fields.get("doi"),
            "journal": fields.get("journal"),
            "url": fields.get("url"),
        })
    return rows


# ---------------------------------------------------------------- writing ---

def _stage(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".export-", dir=output_dir))


def _publish(stage: Path, output_dir: Path, names: Iterable[str]) -> None:
    """Move staged files into place (each move is an atomic rename)."""
    for name in names:
        os.replace(stage / TABLE_FILES[name], output_dir / TABLE_FILES[name])


def export_tables(elements: list[dict[str, Any]], hierarchy: list[dict[str, Any]],
                  bibtex: list[dict[str, Any]], output_dir: Path) -> None:
    """Contract §3.4: write the tables to ``output_dir`` (created if needed).

    Nothing in ``output_dir`` is touched unless every table was built and written.
    """
    tables = build_tables(elements, hierarchy, bibtex)
    output_dir = Path(output_dir)
    stage = _stage(output_dir)
    try:
        for name, table in tables.items():
            pq.write_table(table, stage / TABLE_FILES[name], row_group_size=ROW_GROUP_SIZE,
                           **_WRITE_OPTIONS)
        _publish(stage, output_dir, tables)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ------------------------------------------------------------------ parts ---

def part_path(parts_dir: Path | str, table: str, arxiv_id: str) -> Path:
    return Path(parts_dir) / table / f"{paper_key(arxiv_id)}.parquet"


def write_part(arxiv_id: str, elements: list[dict[str, Any]], hierarchy: list[dict[str, Any]],
               bibtex: list[dict[str, Any]], parts_dir: Path) -> None:
    """Write one publication's rows under ``parts_dir/<table>/<paper_key>.parquet``.

    Every row must belong to ``arxiv_id`` (parts are merged by publication).
    Rewriting a publication replaces its previous part atomically.
    """
    for table, rows in (("elements", elements), ("hierarchy", hierarchy), ("bibtex", bibtex)):
        wrong = {r.get("arxiv_id") for r in rows} - {arxiv_id}
        if wrong:
            raise ValueError(f"{table}: rows of other publications in part {arxiv_id}: "
                             f"{sorted(map(str, wrong))}")
    tables = build_tables(elements, hierarchy, bibtex)
    parts_dir = Path(parts_dir)
    stage = _stage(parts_dir)  # private directory, removed whatever happens
    try:
        for name, table in tables.items():
            pq.write_table(table, stage / TABLE_FILES[name], row_group_size=ROW_GROUP_SIZE,
                           **_WRITE_OPTIONS)
        for name in tables:
            target = part_path(parts_dir, name, arxiv_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / TABLE_FILES[name], target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _scan_part(path: Path, table: str) -> str | None:
    """Check one part cheaply (schema from metadata, one column) and return its arxiv_id.

    A part must hold a single publication whose ``paper_key`` is the file name.
    Returns ``None`` for an empty part.
    """
    if not pq.read_schema(path).equals(SCHEMAS[table], check_metadata=False):
        raise ValueError(f"{path}: schema differs from {table} schema")
    ids = set(pq.read_table(path, columns=["arxiv_id"]).column("arxiv_id").to_pylist())
    if len(ids) > 1:
        raise ValueError(f"{path}: part holds several publications: {sorted(ids)}")
    arxiv_id = next(iter(ids), None)
    if arxiv_id is not None and paper_key(arxiv_id) != path.stem:
        raise ValueError(f"{path}: file name does not match its publication {arxiv_id!r}")
    return arxiv_id


def merge_parts(parts_dir: Path, output_dir: Path) -> None:
    """Combine every part under ``parts_dir`` into the final tables in ``output_dir``.

    The result has the same rows in the same order as :func:`export_tables` would give
    for all publications at once (parts are ordered by their sort key, which is
    contiguous per publication). Row-group layout differs (one group per part), so
    bytes differ from a direct export, but they are stable for identical parts.
    """
    parts_dir, output_dir = Path(parts_dir), Path(output_dir)
    stage = _stage(output_dir)
    try:
        for name, schema in SCHEMAS.items():
            found = []
            for path in sorted((parts_dir / name).glob("*.parquet")):
                arxiv_id = _scan_part(path, name)
                if arxiv_id is not None:
                    # elements sort by element_id = "<paper_key>#...", so their parts order
                    # by "<paper_key>#"; the other tables order by arxiv_id.
                    key = paper_key(arxiv_id) + "#" if name == "elements" else arxiv_id
                    found.append((key, path))
            found.sort()
            with pq.ParquetWriter(stage / TABLE_FILES[name], schema, **_WRITE_OPTIONS) as writer:
                for _, path in found:  # one part in memory at a time
                    writer.write_table(pq.read_table(path), row_group_size=ROW_GROUP_SIZE)
        _publish(stage, output_dir, SCHEMAS)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
