"""Source profiling (TV3).

Reads an M1 run directory (read-only) and reports preliminary statistics about
versions, source files, includes, root candidates and bibliography formats.
These regex-based counts are profiling only, not parser results.

Usage (from m2/):
    python -m src.parsing.profiling scan --run-dir <run_dir> \
        --ids-file <ids.txt> --label range1 --out profiling_range1.json
"""
from __future__ import annotations

import argparse
import json
import logging
import posixpath
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

MAX_READ_BYTES = 8_000_000
NO_TEX_SOURCE = "NO_TEX_SOURCE"

_COMMENT = re.compile(r"(?<!\\)%[^\n]*")
_INCLUDE = re.compile(r"\\(?:input|include|subfile)\s*\{([^{}]*)\}")
_INPUT_BARE = re.compile(r"\\input\s+([^\s{}\\]+)")
_DOCCLASS = re.compile(r"\\documentclass\b")
_BIBLIOGRAPHY = re.compile(r"\\bibliography\s*\{([^{}]*)\}")
_ADDBIB = re.compile(r"\\addbibresource(?:\[[^\]]*\])?\s*\{([^{}]*)\}")
_THEBIB = re.compile(r"\\begin\s*\{thebibliography\}")
_BIBITEM = re.compile(r"\\bibitem\b")


def paper_key(arxiv_id: str) -> str:
    return arxiv_id.replace(".", "-").replace("/", "__")


# --------------------------------------------------------------------------
# Safe readers
# --------------------------------------------------------------------------
def _read_json(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _safe_read(source_dir: Path, rel: str) -> tuple[str, int] | None:
    """Read a file strictly inside source_dir. Returns (text, size) or None."""
    try:
        pp = PurePosixPath(rel)
        if pp.is_absolute() or ".." in pp.parts or "\\" in rel:
            return None
        target = source_dir / pp
        if target.is_symlink() or not target.is_file():
            return None
        if not target.resolve().is_relative_to(source_dir.resolve()):
            return None
        size = target.stat().st_size
        with target.open("rb") as fh:
            data = fh.read(MAX_READ_BYTES)
        return data.decode("utf-8", errors="replace"), size
    except OSError:
        return None


# --------------------------------------------------------------------------
# Include graph
# --------------------------------------------------------------------------
def _resolve_include(target: str, current: str, tex_files: set[str],
                     lower_map: dict[str, str]) -> str | None:
    t = target.strip()
    if not t:
        return None
    base = PurePosixPath(current).parent
    for root in (PurePosixPath("."), base):
        for cand in (t, t + ".tex"):
            p = posixpath.normpath((root / cand).as_posix())
            if p in tex_files:
                return p
            if p.lower() in lower_map:
                return lower_map[p.lower()]
    return None


def _include_depth(roots: list[str], edges: dict[str, list[str]]) -> tuple[int, bool]:
    memo: dict[str, int] = {}
    visiting: set[str] = set()
    cycle = False

    def depth(node: str) -> int:
        nonlocal cycle
        if node in memo:
            return memo[node]
        if node in visiting:
            cycle = True
            return 0
        visiting.add(node)
        d = max((1 + depth(c) for c in edges.get(node, [])), default=0)
        visiting.discard(node)
        memo[node] = d
        return d

    return max((depth(r) for r in roots), default=0), cycle


# --------------------------------------------------------------------------
# Per-version / per-paper profiling
# --------------------------------------------------------------------------
def _blank_record(arxiv_id: str, version: int) -> dict:
    return {
        "arxiv_id": arxiv_id, "version": version, "category": "no_manifest",
        "download_status": None, "extract_status": None, "source_format": None,
        "error_code": None, "has_tex": False, "n_source_files": 0, "n_tex": 0,
        "n_bib": 0, "n_other": 0, "listed_files_missing": 0, "tex_bytes": 0,
        "bib_bytes": 0, "multi_file": False, "n_root_candidates": 0,
        "n_include_cmds": 0, "missing_includes": [], "max_include_depth": 0,
        "include_cycle": False, "n_bibitem": 0, "n_thebibliography": 0,
        "bibliography_cmd": False, "missing_bib_files": [],
        "bib_format": "none", "notes": [],
    }


def _category(manifest: dict) -> str:
    if manifest.get("download_status") != "ok":
        return "download_not_ok"
    status = manifest.get("extract_status")
    if status == "ok":
        return "has_tex"
    if status == "empty" and manifest.get("error_code") == NO_TEX_SOURCE:
        return "no_tex"
    return "extract_failed"


def profile_version(paper_dir: Path, arxiv_id: str, version: int) -> dict:
    rec = _blank_record(arxiv_id, version)
    vdir = paper_dir / "versions" / f"v{version}"
    manifest = _read_json(vdir / "manifest.json")
    if manifest is None:
        rec["notes"].append("manifest.json missing or unreadable")
        return rec

    for key in ("download_status", "extract_status", "source_format", "error_code"):
        rec[key] = manifest.get(key)
    rec["category"] = _category(manifest)

    listed = manifest.get("source_files")
    if not isinstance(listed, list):
        listed = []
        rec["notes"].append("source_files is not a list")
    rec["n_source_files"] = len(listed)

    source_dir = vdir / "source"
    tex: dict[str, str] = {}
    bib: dict[str, str] = {}
    for rel in listed:
        ext = PurePosixPath(rel).suffix.lower() if isinstance(rel, str) else ""
        if ext not in (".tex", ".bib"):
            if isinstance(rel, str):
                rec["n_other"] += 1
            else:
                rec["listed_files_missing"] += 1
            continue
        got = _safe_read(source_dir, rel)
        if got is None:
            rec["listed_files_missing"] += 1
            continue
        text, size = got
        if ext == ".tex":
            tex[rel] = _COMMENT.sub("", text)
            rec["tex_bytes"] += size
        else:
            bib[rel] = text
            rec["bib_bytes"] += size

    rec["n_tex"], rec["n_bib"] = len(tex), len(bib)
    rec["has_tex"] = rec["category"] == "has_tex" and bool(tex)
    if rec["category"] == "has_tex" and not tex:
        rec["notes"].append("extract_status ok but no readable .tex file")
    if rec["listed_files_missing"]:
        rec["notes"].append(f"{rec['listed_files_missing']} listed file(s) unreadable/unsafe")
    if not rec["has_tex"]:
        return rec

    rec["multi_file"] = len(tex) > 1
    tex_set = set(tex)
    lower_map = {p.lower(): p for p in sorted(tex_set)}
    edges: dict[str, list[str]] = {}
    missing: set[str] = set()
    roots = [p for p in sorted(tex) if _DOCCLASS.search(tex[p])]
    rec["n_root_candidates"] = len(roots)
    bib_names: list[str] = []

    for path in sorted(tex):
        text = tex[path]
        targets = _INCLUDE.findall(text) + _INPUT_BARE.findall(text)
        rec["n_include_cmds"] += len(targets)
        for t in targets:
            resolved = _resolve_include(t, path, tex_set, lower_map)
            if resolved is None:
                missing.add(t.strip())
            else:
                edges.setdefault(path, []).append(resolved)
        rec["n_bibitem"] += len(_BIBITEM.findall(text))
        rec["n_thebibliography"] += len(_THEBIB.findall(text))
        for group in _BIBLIOGRAPHY.findall(text) + _ADDBIB.findall(text):
            bib_names.extend(n.strip() for n in group.split(",") if n.strip())

    rec["missing_includes"] = sorted(missing)
    if not roots:
        targeted = {c for children in edges.values() for c in children}
        roots = [p for p in sorted(tex) if p not in targeted]
    rec["max_include_depth"], rec["include_cycle"] = _include_depth(roots, edges)

    rec["bibliography_cmd"] = bool(bib_names)
    kept = set(bib) | {PurePosixPath(p).name for p in bib}
    for name in sorted(set(bib_names)):
        cands = {name, name + ".bib"} if not name.lower().endswith(".bib") else {name}
        cands |= {PurePosixPath(c).name for c in cands}
        if not cands & kept:
            rec["missing_bib_files"].append(name)

    has_bib = rec["n_bib"] > 0
    has_item = rec["n_bibitem"] > 0 or rec["n_thebibliography"] > 0
    rec["bib_format"] = ("both" if has_bib and has_item else "bib" if has_bib
                         else "bibitem" if has_item else "none")
    return rec


def _discover_versions(paper_dir: Path) -> set[int]:
    found: set[int] = set()
    vroot = paper_dir / "versions"
    if vroot.is_dir():
        for child in vroot.iterdir():
            if child.name.startswith("v") and child.name[1:].isdigit() and int(child.name[1:]) > 0:
                found.add(int(child.name[1:]))
    return found


def profile_paper(run_dir: Path, arxiv_id: str) -> tuple[dict, list[dict]]:
    paper_dir = run_dir / "papers" / paper_key(arxiv_id)
    if not paper_dir.is_dir():
        return {"arxiv_id": arxiv_id, "status": "missing_paper_dir"}, []
    meta = _read_json(paper_dir / "metadata.json")
    on_disk = _discover_versions(paper_dir)
    declared: set[int] = set()
    if meta and isinstance(meta.get("versions"), list):
        declared = {v for v in meta["versions"] if type(v) is int and v > 0}
    versions = sorted(on_disk | declared)
    records = [profile_version(paper_dir, arxiv_id, v) for v in versions]
    paper = {
        "arxiv_id": arxiv_id, "status": "ok",
        "metadata_present": meta is not None,
        "n_versions": len(versions), "versions": versions,
        "version_mismatch": bool(meta) and declared != on_disk,
        "references_json_present": (paper_dir / "references.json").is_file(),
    }
    return paper, records


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
def _rate(n: int, d: int, denominator_desc: str) -> dict:
    return {"count": n, "denominator": d, "denominator_desc": denominator_desc,
            "rate": round(n / d, 4) if d else None}


def _stats(values: list[int]) -> dict | None:
    if not values:
        return None
    v = sorted(values)
    n = len(v)
    return {"n": n, "min": v[0], "median": statistics.median(v),
            "mean": round(sum(v) / n, 1), "p90": v[min(n - 1, int(0.9 * n))],
            "max": v[-1]}


def _dist(values) -> dict:
    return {str(k): c for k, c in sorted(Counter(values).items(), key=lambda kv: str(kv[0]))}


def summarize(papers: list[dict], versions: list[dict]) -> dict:
    ok_papers = [p for p in papers if p.get("status") == "ok"]
    nv, npub = len(versions), len(ok_papers)
    tex = [v for v in versions if v["has_tex"]]
    nt = len(tex)
    ALL, TEX, PUB = "all versions", "versions with TeX", "publications found"

    def cnt(rows, pred):
        return sum(1 for r in rows if pred(r))

    cat = Counter(v["category"] for v in versions)
    rates = {
        "has_tex": _rate(cat["has_tex"], nv, ALL),
        "no_tex": _rate(cat["no_tex"], nv, ALL),
        "download_not_ok": _rate(cat["download_not_ok"], nv, ALL),
        "extract_failed": _rate(cat["extract_failed"], nv, ALL),
        "no_manifest": _rate(cat["no_manifest"], nv, ALL),
        "multi_file": _rate(cnt(tex, lambda v: v["multi_file"]), nt, TEX),
        "uses_includes": _rate(cnt(tex, lambda v: v["n_include_cmds"] > 0), nt, TEX),
        "nested_includes_depth_ge2": _rate(cnt(tex, lambda v: v["max_include_depth"] >= 2), nt, TEX),
        "has_missing_includes": _rate(cnt(tex, lambda v: bool(v["missing_includes"])), nt, TEX),
        "include_cycle": _rate(cnt(tex, lambda v: v["include_cycle"]), nt, TEX),
        "root_unique": _rate(cnt(tex, lambda v: v["n_root_candidates"] == 1), nt, TEX),
        "root_none": _rate(cnt(tex, lambda v: v["n_root_candidates"] == 0), nt, TEX),
        "root_multiple": _rate(cnt(tex, lambda v: v["n_root_candidates"] > 1), nt, TEX),
        "bib_files_kept": _rate(cnt(tex, lambda v: v["n_bib"] > 0), nt, TEX),
        "bibitem_present": _rate(cnt(tex, lambda v: v["n_bibitem"] > 0 or v["n_thebibliography"] > 0), nt, TEX),
        "bib_cmd_without_bib_file": _rate(cnt(tex, lambda v: bool(v["missing_bib_files"])), nt, TEX),
        "no_bibliography_detected": _rate(cnt(tex, lambda v: v["bib_format"] == "none"), nt, TEX),
        "multi_version": _rate(cnt(ok_papers, lambda p: p["n_versions"] >= 2), npub, PUB),
        "version_mismatch": _rate(cnt(ok_papers, lambda p: p["version_mismatch"]), npub, PUB),
    }
    return {
        "counts": {
            "publications_requested": len(papers), "publications_found": npub,
            "publications_missing_dir": len(papers) - npub, "versions": nv,
            "versions_with_tex": nt, "categories": dict(sorted(cat.items())),
        },
        "rates": rates,
        "distributions": {
            "versions_per_paper": _dist(p["n_versions"] for p in ok_papers),
            "root_candidates": _dist(min(v["n_root_candidates"], 3) for v in tex),
            "bib_format": _dist(v["bib_format"] for v in tex),
            "source_format": _dist(v["source_format"] for v in versions),
            "include_depth": _dist(v["max_include_depth"] for v in tex),
        },
        "stats": {
            "tex_files_per_version": _stats([v["n_tex"] for v in tex]),
            "source_files_per_version": _stats([v["n_source_files"] for v in tex]),
            "source_bytes_per_version": _stats([v["tex_bytes"] + v["bib_bytes"] for v in tex]),
        },
    }


# --------------------------------------------------------------------------
# Representative samples
# --------------------------------------------------------------------------
_SAMPLE_RULES = [
    ("single_file", lambda r, p: r["has_tex"] and not r["multi_file"]),
    ("multi_file", lambda r, p: r["has_tex"] and r["multi_file"]),
    ("nested_include", lambda r, p: r["max_include_depth"] >= 2),
    ("multi_version", lambda r, p: p["n_versions"] >= 2),
    ("no_tex", lambda r, p: r["category"] == "no_tex"),
    ("bib_file", lambda r, p: r["n_bib"] > 0),
    ("bibitem", lambda r, p: r["n_bibitem"] > 0),
    ("root_ambiguity", lambda r, p: r["has_tex"] and r["n_root_candidates"] != 1),
    ("missing_include", lambda r, p: bool(r["missing_includes"])),
]


def select_samples(papers: list[dict], versions: list[dict], limit: int = 10) -> list[dict]:
    by_id = {p["arxiv_id"]: p for p in papers if p.get("status") == "ok"}
    ordered = sorted(versions, key=lambda r: (r["arxiv_id"], r["version"]))
    chosen: dict[tuple[str, int], list[str]] = {}
    for name, rule in _SAMPLE_RULES:
        matches = [r for r in ordered if rule(r, by_id[r["arxiv_id"]])]
        if not matches:
            continue
        taken = {k[0] for k in chosen}
        pick = next((r for r in matches if r["arxiv_id"] not in taken), matches[0])
        key = (pick["arxiv_id"], pick["version"])
        if key in chosen or len(chosen) < limit:
            chosen.setdefault(key, []).append(name)
    return [{"arxiv_id": a, "version": v, "reasons": reasons}
            for (a, v), reasons in chosen.items()]


# --------------------------------------------------------------------------
# Range scan + CLI
# --------------------------------------------------------------------------
def read_ids(path: Path) -> list[str]:
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in ids:
            ids.append(line)
    return ids


def discover_ids(run_dir: Path) -> list[str]:
    ids = []
    for meta in sorted((run_dir / "papers").glob("*/metadata.json")):
        data = _read_json(meta)
        if data and isinstance(data.get("arxiv_id"), str):
            ids.append(data["arxiv_id"])
    return ids


def scan_range(run_dir: Path, ids: list[str], label: str) -> dict:
    started = time.perf_counter()
    papers: list[dict] = []
    versions: list[dict] = []
    errors: list[dict] = []
    for arxiv_id in ids:
        try:
            paper, records = profile_paper(run_dir, arxiv_id)
        except Exception as exc:  # one bad paper must not stop the batch
            log.error("[%s] stage=profiling: %s", arxiv_id, exc)
            papers.append({"arxiv_id": arxiv_id, "status": "error"})
            errors.append({"arxiv_id": arxiv_id, "stage": "profiling", "error": repr(exc)})
            continue
        papers.append(paper)
        versions.extend(records)
        if paper["status"] != "ok":
            log.warning("[%s] stage=profiling: %s", arxiv_id, paper["status"])
            errors.append({"arxiv_id": arxiv_id, "stage": "profiling", "error": paper["status"]})
    return {
        "meta": {"label": label, "run_dir": str(run_dir), "n_ids": len(ids),
                 "elapsed_seconds": round(time.perf_counter() - started, 3)},
        "summary": summarize(papers, versions),
        "samples": select_samples(papers, versions),
        "errors": errors,
        "papers": papers,
        "versions": versions,
    }


def _cmd_scan(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    ids = read_ids(Path(args.ids_file)) if args.ids_file else discover_ids(run_dir)
    result = scan_range(run_dir, ids, args.label)
    out = Path(args.out)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    counts = result["summary"]["counts"]
    print(f"[{args.label}] publications={counts['publications_found']}/{len(ids)} "
          f"versions={counts['versions']} with_tex={counts['versions_with_tex']} "
          f"errors={len(result['errors'])} -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M2 source profiling (TV3)")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="profile one arXiv-ID range")
    scan.add_argument("--run-dir", required=True)
    scan.add_argument("--ids-file", help="one base arXiv id per line; default: all papers in run")
    scan.add_argument("--label", required=True)
    scan.add_argument("--out", required=True)
    scan.set_defaults(func=_cmd_scan)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())