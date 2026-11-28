"""Load M1 tables and prepare the reusable catalog.

Run directories are supplied oldest -> newest, not sorted by folder name.
"""
from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from m1.src.utils.common import paper_key
from .contracts import KEYS, SCHEMAS, make_table, normalize_arxiv_id, validate_table

LOG = logging.getLogger(__name__)
_M1_STATUS = {
    "ok": "ok", 
    "empty": "empty", 
    "failed": "failed",
    "pending": "incomplete",
    "running": "incomplete", 
    "skipped": "incomplete",
}


@dataclass(frozen=True)
class M1Tables:
    papers: pa.Table
    references: pa.Table
    crawl_status: pa.Table


def _id(value, where):
    result = normalize_arxiv_id(value) \
        if isinstance(value, str) else None
    if result is None:
        raise ValueError(f"{where}: invalid arXiv ID {value!r}")
    return result


def _ids(publication_ids):
    if isinstance(publication_ids, (str, bytes, Path)):
        raise TypeError("publication_ids must be an iterable of IDs, not a filename")
    seen = set()
    for raw in publication_ids:
        arxiv_id = _id(raw, "[publication_ids]")
        if arxiv_id in seen:
            raise ValueError(f"[publication_ids] duplicate normalized ID: {arxiv_id}")
        seen.add(arxiv_id)
    return sorted(seen)


def _read_json(path):
    """Return (object, state). A missing per-paper file is not a missing run."""
    try:
        with path.open(encoding="utf-8") as stream:
            obj = json.load(stream)
        if not isinstance(obj, dict):
            raise ValueError("expected a JSON object")
        return obj, "ok"
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeError, ValueError) as exc:
        LOG.warning("Invalid JSON %s: %s", path, exc)
        return None, "failed"


def _check_identity(obj, arxiv_id, where):
    raw = obj.get("arxiv_id")
    actual = normalize_arxiv_id(raw) if isinstance(raw, str) else None
    if actual is None:
        LOG.warning("%s %s: missing or invalid citing ID", where, arxiv_id)
        return False
    if actual != arxiv_id:
        raise ValueError(f"{where}: expected {arxiv_id}, got {actual}")
    return True


def _project(name, obj, arxiv_id):
    """Check M3 field types rather than silently coerce malformed JSON."""
    row = {"arxiv_id": arxiv_id}
    for field in SCHEMAS[name]:
        if field.name == "arxiv_id":
            continue
        value = obj.get(field.name)
        if value is not None:
            if pa.types.is_string(field.type):
                good = isinstance(value, str)
            elif pa.types.is_list(field.type):
                good = isinstance(value, list) and all(isinstance(x, str) for x in value)
            else:
                raise ValueError(f"[{name}] unsupported field: {field.name}")
            if not good:
                raise ValueError(f"[{name}] {arxiv_id}: invalid {field.name}")
        row[field.name] = value
    return row


def _runs_and_plan(m1_runs, consolidation_plan):
    if isinstance(m1_runs, (str, Path)):
        m1_runs = [m1_runs]
    runs = [Path(run).resolve() for run in m1_runs]
    if not runs or len(set(runs)) != len(runs):
        raise ValueError("m1_runs must contain unique run directories, oldest -> newest")
    for run in runs:
        if not (run / "papers").is_dir():
            raise FileNotFoundError(run / "papers")
    if isinstance(consolidation_plan, (str, Path)):
        with Path(consolidation_plan).open(encoding="utf-8") as f:
            consolidation_plan = json.load(f)
    if consolidation_plan is None:
        return runs, {}
    if not isinstance(consolidation_plan, Mapping):
        raise ValueError("consolidation_plan must be a mapping arxiv_id -> run directory")
    plan = {}
    for raw, path in consolidation_plan.items():
        arxiv_id = _id(raw, "[consolidation_plan]")
        if arxiv_id in plan:
            raise ValueError(f"[consolidation_plan] duplicate ID: {arxiv_id}")
        run = Path(path).resolve()
        if run not in runs:
            raise ValueError(f"[consolidation_plan] {arxiv_id}: unknown run {run}")
        plan[arxiv_id] = run
    return runs, plan


def _select_paper(runs, arxiv_id, plan):
    """Prefer the newest validated run; otherwise use newest existing for diagnosis."""
    fallback = None
    chosen = None
    for run in reversed(runs):
        folder = run / "papers" / paper_key(arxiv_id)
        if not folder.is_dir():
            continue
        report, state = _read_json(folder / "validation.json")
        if report is not None and (not _check_identity(report, arxiv_id, "[validation]")
                                   or report.get("status") not in ("ok", "failed", "pending", "running")):
            state = "failed"
        validated = state == "ok" and report.get("status") == "ok"
        if fallback is None:
            fallback = (run, folder, False, state, report)
        if validated:
            chosen = (run, folder, True, state, report)
            break
    chosen = chosen or fallback
    if arxiv_id in plan and (chosen is None or chosen[0] != plan[arxiv_id]):
        raise ValueError(f"[consolidation_plan] {arxiv_id}: expected {plan[arxiv_id]}, "
                         f"selected {chosen[0] if chosen else None}")
    return chosen


def _read_paper(folder, arxiv_id):
    # A row for every assigned publication, even if metadata is missing.
    record, state = _read_json(folder / "metadata.json")
    if record is None:
        return _project("papers", {}, arxiv_id), state
    if not _check_identity(record, arxiv_id, "[papers]"):
        return _project("papers", {}, arxiv_id), "failed"
    try:
        if not isinstance(record.get("status"), str) or record.get("status") not in _M1_STATUS:
            raise ValueError("invalid metadata status")
        return _project("papers", record, arxiv_id), "ok"
    except ValueError as exc:
        LOG.warning("[papers] %s: %s", arxiv_id, exc)
        return _project("papers", {}, arxiv_id), "failed"


def _has_value(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_has_value(v) for v in value)
    return value is not None


def _deduplicate(rows):
    fields = [f.name for f in SCHEMAS["references"] if f.name not in KEYS["references"]]
    kept = {}
    for row in rows:
        key = (row["arxiv_id"], row["ref_arxiv_id"])
        old = kept.get(key)
        if old is None:
            kept[key] = row
            continue
        conflicts = [f for f in fields if _has_value(old[f]) and _has_value(row[f]) and old[f] != row[f]]
        if conflicts:
            LOG.warning("[references] conflicting metadata %s fields=%s", key, conflicts)
        if sum(_has_value(row[f]) for f in fields) > sum(_has_value(old[f]) for f in fields):
            kept[key] = row
    return [kept[key] for key in sorted(kept)]


def _read_references(folder, arxiv_id):
    record, state = _read_json(folder / "references.json")
    if record is None:
        return [], state, None
    if not _check_identity(record, arxiv_id, "[references]"):
        return [], "failed", None
    raw = record.get("references")
    status = record.get("status")
    if not isinstance(raw, list) or not isinstance(status, str) or status not in _M1_STATUS:
        LOG.warning("[references] %s: invalid status or references array", arxiv_id)
        return [], "failed", None
    count = len(raw)  # saved M1 rows BEFORE ID filtering/dedup, not S2 API total
    if status == "empty" and raw:
        LOG.warning("[references] %s: empty status with nonempty references", arxiv_id)
        return [], "failed", count
    rows = []
    for idx, ref in enumerate(raw):
        if not isinstance(ref, dict):
            LOG.warning("[references] %s: invalid row %s", arxiv_id, idx)
            return [], "failed", count
        # A foreign citing ID is a contract violation, not a missing reference.
        if not _check_identity(ref, arxiv_id, f"[references] row {idx}"):
            return [], "failed", count
        candidate = ref.get("ref_arxiv_id")
        if candidate is not None and not isinstance(candidate, str):
            LOG.warning("[references] %s: invalid ref_arxiv_id type", arxiv_id)
            return [], "failed", count
        try:
            row = _project("references", ref, arxiv_id)
        except ValueError as exc:
            LOG.warning("%s", exc)
            return [], "failed", count
        normalized = normalize_arxiv_id(candidate)
        if normalized is None:
            continue
        row["ref_arxiv_id"] = normalized
        rows.append(row)
    if status not in {"ok", "empty"}:
        # Do not expose partial/failed crawl results as candidates.
        return [], _M1_STATUS[status], count
    return _deduplicate(rows), _M1_STATUS[status], count


def _table(name, rows):
    table = make_table(name, rows)
    validate_table(name, table)
    return table.sort_by([(key, "ascending") for key in KEYS[name]]) if table.num_rows else table


def load_m1_tables(m1_runs, publication_ids, *, consolidation_plan=None) -> M1Tables:
    """Read M1 JSON into three validated Arrow tables, without writing files.

    m1_runs: list of run roots containing papers/, oldest -> newest.
    publication_ids: iterable of assigned base IDs (versioned IDs normalized).
    consolidation_plan: optional mapping or JSON file (see _runs_and_plan).
    """
    ids = _ids(publication_ids)
    runs, plan = _runs_and_plan(m1_runs, consolidation_plan)
    papers, references, statuses = [], [], []
    for arxiv_id in ids:
        selected = _select_paper(runs, arxiv_id, plan)
        if selected is None:
            paper = _project("papers", {}, arxiv_id)
            candidates, status, count = [], "missing", None
        else:
            run, folder, validated, report_state, report = selected
            paper, metadata_state = _read_paper(folder, arxiv_id)
            candidates, status, count = _read_references(folder, arxiv_id)
            if status in {"ok", "empty"}:
                if (report_state == "failed" or metadata_state == "failed"
                        or (report and report.get("status") == "failed")):
                    status = "failed"
                elif not validated or metadata_state == "missing":
                    status = "incomplete"
            if not validated:
                LOG.warning("[crawl_status] %s: no validation=ok; fallback %s, status=%s", arxiv_id, run, status)
            # Valid reference rows may be retained for export/diagnostics, but
            # fallback papers with nonusable status cannot enter labeling.
        papers.append(paper)
        references.extend(candidates)
        statuses.append({"arxiv_id": arxiv_id, "status": status,
                         "source_reference_count": count, "candidate_count": len(candidates)})
    return M1Tables(_table("papers", papers), _table("references", references),
                    _table("crawl_status", statuses))


def _read_queries(bibtex_path, ids):
    """Read M2's existing Parquet (single file or shard directory)."""
    path = Path(bibtex_path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_dir() and not any(path.rglob("*.parquet")):
        raise FileNotFoundError(f"No parquet files under {path}")
    # Validate full schema, then project only the columns needed by queries.
    # ParquetFile for files avoids inference of hive partition columns.
    if path.is_file():
        schema = pq.ParquetFile(path).schema_arrow
    else:
        import pyarrow.dataset as ds
        dataset = ds.dataset(path, format="parquet", exclude_invalid_files=True)
        schema = dataset.schema
        for fragment in dataset.get_fragments():
            if not fragment.physical_schema.equals(SCHEMAS["bibtex"], check_metadata=False):
                raise ValueError(f"[bibtex] invalid schema in {fragment.path}")
    expected = SCHEMAS["bibtex"]
    if not schema.equals(expected, check_metadata=False):
        raise ValueError(f"[bibtex] invalid schema at {path}: {schema}")
    keys = ["arxiv_id", "citation_key"]
    table = pq.ParquetFile(path).read(columns=keys) if path.is_file() else dataset.to_table(columns=keys)
    wanted = set(ids)
    rows = []
    for row in table.to_pylist():
        arxiv_id = _id(row["arxiv_id"], "[bibtex]")
        if arxiv_id in wanted:
            rows.append({"arxiv_id": arxiv_id, "citation_key": row["citation_key"]})
    # validate_table rejects blank and duplicate keys after ID normalization.
    return _table("queries", rows)


def prepare_catalog(*, m1_runs, consolidation_plan, bibtex_path,
                    publication_ids, output_dir) -> Path:
    """Write four catalog Parquet tables, never overwriting existing files.

    Caller owns run_id/seed/commit manifest and path configuration.
    Pass all assigned IDs here; selected matching IDs are for prepare_pairs later.
    """
    ids = _ids(publication_ids)
    output_dir = Path(output_dir).resolve()
    # Materialize run iterable once; reject writing inside input datasets.
    runs = [m1_runs] if isinstance(m1_runs, (str, Path)) else list(m1_runs)
    for source in [*runs, bibtex_path]:
        source = Path(source).resolve()
        if output_dir == source or source in output_dir.parents or output_dir in source.parents:
            raise ValueError(f"output_dir overlaps input: {source}")
    names = ("papers", "references", "crawl_status", "queries")
    if output_dir.exists() and not output_dir.is_dir():
        raise NotADirectoryError(output_dir)
    for name in names:
        target = output_dir / f"{name}.parquet"
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Catalog file already exists: {target}")
    m1 = load_m1_tables(runs, ids, consolidation_plan=consolidation_plan)
    queries = _read_queries(bibtex_path, ids)
    tables = {"papers": m1.papers, "references": m1.references,
              "crawl_status": m1.crawl_status, "queries": queries}
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    # Write all files to staging before publishing the catalog; no partial result
    # if writing a Parquet file fails. A new output directory is recommended.
    with tempfile.TemporaryDirectory(prefix=".catalog-", dir=output_dir.parent) as temp:
        stage = Path(temp) / "catalog"
        stage.mkdir()
        for name, table in tables.items():
            pq.write_table(table, stage / f"{name}.parquet")
        if not output_dir.exists():
            stage.rename(output_dir)
        else:
            published = []
            try:
                for name in tables:
                    target = output_dir / f"{name}.parquet"
                    if target.exists() or target.is_symlink():
                        raise FileExistsError(target)
                    (stage / target.name).rename(target)
                    published.append(target)
            except OSError:
                for target in published:
                    target.unlink()
                raise
    return output_dir
