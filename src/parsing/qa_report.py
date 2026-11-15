"""TV4 — assemble ``qa_report.json`` for the Jupyter Book (Contract §7.2-7.3).

Inputs: the exported tables, per-version statuses supplied by the pipeline (TV1),
optionally TV3's pre-dedup bibliography counts and a ``benchmark_<label>.json``.
Everything that can be derived from the tables is derived from the tables.

Every rate is ``{numerator, denominator, denominator_definition, value}`` and says
what its denominator is. The number of versions that have TeX is never used as the
denominator of a rate presented as corpus-wide; where it is used (``ok_among_
versions_with_tex``) the definition says so. Figures that were not supplied or not
measured are ``null`` with an explanation, never invented.

``version_status`` rows (one per version the pipeline saw)::

    {"arxiv_id": str, "version": int >= 1,
     "status": "ok" | "partial" | "no_tex" | "failed" | "skipped",   # Contract §3.6
     "has_tex": bool | None,            # did the M1 source contain TeX
     "root_found": bool | None,         # gathering identified a root file
     "include_problems": int | None,    # unresolved/cyclic/unsafe include warnings
     "stage": str | None,               # where a failed/skipped version stopped
     "reason": str | None}              # cause, e.g. a gathering error code

:func:`status_row` builds such a row from the contract objects.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import validation

STATUSES = ("ok", "partial", "no_tex", "failed", "skipped")
EXPORTED_STATUSES = ("ok", "partial", "no_tex")  # statuses that leave a root in the tables
REPORT_NAME = "qa_report.json"
MAX_FAILURE_EXAMPLES = 200

# TV1 warning codes (``... stage=gather CODE: ...``) that mean an include was not resolved.
INCLUDE_PROBLEM_CODES = frozenset({
    "MISSING_DEPENDENCY", "UNSAFE_PATH", "AMBIGUOUS_PATH", "INCLUDE_CYCLE", "DYNAMIC_PATH",
    "DYNAMIC_INCLUDEONLY", "INCLUDE_DEPTH_LIMIT", "UNSUPPORTED_SUBFILE", "NON_TEX_INCLUDE",
    "SOURCE_READ_FAILED"})
_GATHER_CODE = re.compile(r"stage=gather ([A-Z][A-Z0-9_]*)")
_ROOT_ID = re.compile(r"#root(\d+)$")


# ------------------------------------------------------------------ helpers -

def ratio(numerator: int | float | None, denominator: int | float | None,
          definition: str) -> dict[str, Any]:
    """A rate with its numerator, denominator and what the denominator counts."""
    value = (numerator / denominator
             if numerator is not None and denominator else None)
    return {"numerator": numerator, "denominator": denominator,
            "denominator_definition": definition, "value": value}


def _gather_codes(warnings: Iterable[str]) -> list[str]:
    return [m.group(1) for w in warnings if (m := _GATHER_CODE.search(w))]


def status_row(gathered: Any, parsed: Any = None, *, has_tex: bool | None = None,
               error: tuple[str, str] | None = None) -> dict[str, Any]:
    """Build a ``version_status`` row from a ``GatheredDocument`` and ``ParsedDocument``.

    ``error`` is ``(stage, message)`` for an exception raised while processing this
    version; it makes the status ``failed``. A version TV1 could not gather keeps TV1's
    status (``failed``, ``no_tex``, ``skipped``) and its last gathering code as the reason.
    """
    codes = _gather_codes(gathered.warnings)
    row: dict[str, Any] = {
        "arxiv_id": gathered.arxiv_id, "version": gathered.version, "status": None,
        "has_tex": has_tex, "root_found": gathered.root_file is not None,
        "include_problems": None, "stage": None, "reason": None}
    if error is not None:
        row.update(status="failed", stage=error[0], reason=error[1][:300])
    elif gathered.status in ("no_tex", "skipped", "failed"):
        row.update(status=gathered.status,
                   stage=None if gathered.status == "no_tex" else "gather",
                   reason=codes[-1] if codes and gathered.status != "no_tex" else
                   ("NO_TEX_SOURCE" if gathered.status == "no_tex" else None))
        if gathered.status == "no_tex":
            row["has_tex"] = False
            row["root_found"] = None
    else:
        parsed_status = getattr(parsed, "status", None)
        row["status"] = parsed_status if parsed_status in STATUSES else gathered.status
        if row["status"] == "failed":
            row.update(stage="parse", reason="parser returned failed")
        row["include_problems"] = sum(c in INCLUDE_PROBLEM_CODES for c in codes)
        if row["has_tex"] is None:
            row["has_tex"] = True
    return row


def _check_rows(rows: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, int]] = set()
    for index, row in enumerate(rows):
        for key in ("arxiv_id", "version", "status"):
            if key not in row:
                raise ValueError(f"version_status row {index} lacks {key!r}")
        if not isinstance(row["arxiv_id"], str) or not row["arxiv_id"]:
            raise ValueError(f"version_status row {index}: arxiv_id must be a non-empty string")
        if type(row["version"]) is not int or row["version"] < 1:
            raise ValueError(f"version_status row {index}: version must be an integer >= 1")
        if row["status"] not in STATUSES:
            raise ValueError(f"version_status row {index}: unknown status {row['status']!r}")
        identity = (row["arxiv_id"], row["version"])
        if identity in seen:
            raise ValueError(f"version_status lists {identity} twice")
        seen.add(identity)


def _read(output_dir: Path, name: str, columns: list[str]) -> pa.Table:
    table = pq.read_table(output_dir / f"{name}.parquet", columns=columns).combine_chunks()
    if "kind" in table.column_names:
        table = table.set_column(table.column_names.index("kind"), "kind",
                                 pc.cast(table["kind"], pa.string()))
    return table


def _counts(values: pa.Array) -> dict[str, int]:
    counted = pc.value_counts(values)
    return dict(sorted(zip(counted.field("values").to_pylist(),
                           counted.field("counts").to_pylist())))


def _table_stats(output_dir: Path) -> dict[str, Any]:
    """Counts and per-kind dedup figures derived from the Parquet tables only."""
    elements = _read(output_dir, "elements", ["element_id", "arxiv_id", "kind"])
    hierarchy = _read(output_dir, "hierarchy", ["element_id"])
    bibtex = _read(output_dir, "bibtex", ["arxiv_id"])
    root_rows = elements.filter(pc.equal(elements["kind"], validation.ROOT_KIND))
    roots = {(a, int(m.group(1))) for a, i in zip(root_rows["arxiv_id"].to_pylist(),
                                                 root_rows["element_id"].to_pylist())
             if (m := _ROOT_ID.search(i))}
    # D10: occurrences = hierarchy rows; unique = distinct element ids in the hierarchy.
    occurrence_kinds = pc.take(elements["kind"], pc.drop_null(
        pc.index_in(hierarchy["element_id"], value_set=elements["element_id"])))
    distinct = pc.unique(hierarchy["element_id"])
    unique_kinds = pc.take(elements["kind"], pc.drop_null(
        pc.index_in(distinct, value_set=elements["element_id"])))
    return {
        "elements_total": elements.num_rows,
        "elements_by_kind": _counts(elements["kind"]),
        "unique_non_root_by_kind": _counts(unique_kinds),
        "occurrences_by_kind": _counts(occurrence_kinds),
        "hierarchy_relations": hierarchy.num_rows,
        "bibtex_entries": bibtex.num_rows,
        "publications": len(pc.unique(pc.drop_null(elements["arxiv_id"]))),
        "roots": roots,
        "unique_non_root": len(distinct),
    }


_RATE_NAMES = {
    "ok": "versions_ok", "partial": "versions_partial", "no_tex": "versions_no_tex",
    "failed": "versions_failed", "skipped": "versions_skipped"}


def _version_section(rows: list[dict[str, Any]], tables: dict[str, Any]) -> dict[str, Any]:
    total = len(rows)
    by_status = {s: sum(r["status"] == s for r in rows) for s in STATUSES}
    with_tex = [r for r in rows if r.get("has_tex") is True]
    tex_known = [r for r in rows if r.get("has_tex") is not None]
    root_known = [r for r in with_tex if r.get("root_found") is not None]
    include_known = [r for r in rows if r.get("include_problems") is not None]
    exported = {(r["arxiv_id"], r["version"]) for r in rows if r["status"] in EXPORTED_STATUSES}
    missing = sorted(exported - tables["roots"])
    unexpected = sorted(tables["roots"] - exported)
    all_versions = "all versions listed in version_status (with or without TeX)"
    rates = {_RATE_NAMES[s]: ratio(by_status[s], total, all_versions) for s in STATUSES}
    rates["ok_among_versions_with_tex"] = ratio(
        sum(r["status"] == "ok" for r in with_tex), len(with_tex),
        "versions with has_tex = true; NOT a corpus-wide success rate because versions "
        "without TeX are excluded from the denominator")
    rates["version_coverage_in_tables"] = ratio(
        len(tables["roots"] & exported), total,
        all_versions + "; numerator: versions that have a root element in the tables")
    rates["root_detection"] = ratio(
        sum(r["root_found"] is True for r in root_known), len(root_known),
        "versions with has_tex = true whose root_found is known")
    rates["include_resolution"] = ratio(
        sum(r["include_problems"] == 0 for r in include_known), len(include_known),
        "versions that reached include resolution (include_problems known); "
        "numerator: those with no unresolved/cyclic/unsafe include")
    failures = [r for r in rows if r["status"] in ("failed", "skipped")]
    by_stage: dict[str, int] = {}
    for r in failures:
        by_stage[str(r.get("stage"))] = by_stage.get(str(r.get("stage")), 0) + 1
    return {
        "counts": {"publications": len({r["arxiv_id"] for r in rows}), "versions": total,
                   "versions_with_tex": len(with_tex),
                   "versions_with_tex_unknown": total - len(tex_known), "by_status": by_status},
        "rates": rates,
        "failures": {
            "total": len(failures), "by_stage": dict(sorted(by_stage.items())),
            "examples": [{k: r.get(k) for k in ("arxiv_id", "version", "status", "stage",
                                                "reason")}
                         for r in sorted(failures, key=lambda r: (r["arxiv_id"], r["version"]))
                         ][:MAX_FAILURE_EXAMPLES]},
        "consistency": {
            "versions_expected_in_tables_but_missing": len(missing),
            "versions_in_tables_without_an_exported_status": len(unexpected),
            "examples_missing": [list(x) for x in missing[:20]],
            "examples_unexpected": [list(x) for x in unexpected[:20]],
            "definition": "tables hold a root element for every version whose status is "
                          "ok, partial or no_tex, and for no other version"},
    }


def _table_section(tables: dict[str, Any], bib_before: dict[str, int] | None) -> dict[str, Any]:
    occurrences = tables["hierarchy_relations"]
    unique = tables["unique_non_root"]
    by_kind = {}
    for kind, count in tables["occurrences_by_kind"].items():
        u = tables["unique_non_root_by_kind"].get(kind, 0)
        by_kind[kind] = {"occurrences": count, "unique_elements": u,
                         "dedup_rate": ratio(count - u, count,
                                             f"occurrences of kind {kind!r} in hierarchy; "
                                             "numerator: occurrences removed by dedup")}
    if bib_before is None:
        bib = {"entries_before_dedup": None,
               "dedup_rate": ratio(None, None, "entries before dedup were not supplied "
                                               "(TV3 count); not measured"),
               "note": "TODO: supply bib_entries_before from TV3"}
    else:
        before = sum(bib_before.values())
        bib = {"entries_before_dedup": before,
               "dedup_rate": ratio(before - tables["bibtex_entries"], before,
                                   "bibliography entries before cross-version dedup, summed "
                                   "over the supplied publications; numerator: entries "
                                   "removed (before - after)")}
    return {
        "elements_total": tables["elements_total"],
        "elements_by_kind": tables["elements_by_kind"],
        "hierarchy_relations": occurrences,
        "element_dedup": {
            "unique_non_root_elements": unique, "non_root_occurrences": occurrences,
            "dedup_rate": ratio(occurrences - unique, occurrences,
                                "occurrences of non-root nodes (hierarchy rows); numerator: "
                                "occurrences removed by dedup, i.e. occurrences minus unique "
                                "non-root elements; roots are never deduplicated"),
            "by_kind": by_kind},
        "bibtex": {"entries_after_dedup": tables["bibtex_entries"], **bib},
    }


def _benchmark_section(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"available": False, "note": "no benchmark file supplied; nothing was measured"}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    env = data["environment"]
    return {"available": True, "file_name": Path(path).name, "label": data["label"],
            "mode": data["mode"], "process_source": data["process_source"],
            "data_source": data["data_source"], "id_list": data["id_list"],
            "counts": data["counts"],
            "environment": {k: env.get(k) for k in ("platform", "python_version", "cpu_count",
                                                    "total_ram_bytes", "pyarrow_version",
                                                    "git_sha", "git_dirty")},
            "end_to_end_wall_seconds": data["end_to_end_wall_seconds"],
            "stage_wall_seconds": {n: s["wall_seconds"] for n, s in data["stages"].items()},
            "peak_rss_bytes": data["peak_rss_bytes"],
            "output_total_bytes": data["output"]["total_bytes"],
            "throughput": data["throughput"], "config": data["config"]}


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    handle, temp = tempfile.mkstemp(prefix=".qa-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------- public -

def build_qa_report(output_dir: Path, version_status: list[dict[str, Any]],
                    benchmark_path: Path | None = None, *,
                    bib_entries_before: dict[str, int] | None = None,
                    scope: str | None = None, notes: list[str] | None = None) -> dict[str, Any]:
    """Validate the tables, combine everything and write ``<output_dir>/qa_report.json``.

    ``bib_entries_before`` maps ``arxiv_id`` to TV3's entry count before cross-version
    dedup (omit if unknown: the figure is then reported as not measured). ``scope``
    states what the run covered (for example "pilot run, 40 of N publications") so a
    reader cannot mistake a sample for the full corpus. The report is a pure function
    of its inputs and contains no timestamp. Returns the report dict.
    """
    output_dir = Path(output_dir)
    rows = [dict(r) for r in version_status]
    _check_rows(rows)
    result = validation.validate_tables(output_dir)
    report: dict[str, Any] = {
        "schema_version": 1, "scope": scope, "notes": list(notes or []),
        "validation": {
            "passed": result["passed"], "error_total": result["counts"]["error_total"],
            "warning_total": result["counts"]["warning_total"],
            "issues": result["counts"]["issues"], "errors": result["errors"],
            "warnings": result["warnings"],
            "citation_coverage": result["counts"].get("citation_coverage")},
        "benchmark": _benchmark_section(benchmark_path),
    }
    readable = all(result["counts"]["tables"].get(t) is not None
                   for t in ("elements", "hierarchy", "bibtex"))
    if readable:
        tables = _table_stats(output_dir)
        report["versions"] = _version_section(rows, tables)
        report["tables"] = _table_section(tables, bib_entries_before)
    else:
        report["versions"] = None
        report["tables"] = None
        report["notes"].append("tables could not be read; see validation.errors")
    _write_atomic(output_dir / REPORT_NAME, report)
    return report
