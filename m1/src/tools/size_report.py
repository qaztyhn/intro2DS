"""Per-paper source size statistics read from source_assets_audit.json.

Usage: python -m m1.src.tools.size_report <run_dir>

No re-download. A paper is measured only if every one of its versions has a
complete audit scan and a manifest with kept_source_bytes; versions are summed
per paper before averaging.
"""
import json
import pathlib
import sys

RULE_SUFFIXES = {".tex", ".bib"}  # what the M1 statement allows to keep
FIELDS = ("before", "rule", "preserve", "m2_input")


def _read(path):
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def version_sizes(audit):
    """Return before/rule/preserve bytes of one version, or None if not measurable."""
    if not audit.get("archive_scan_complete"):
        return None
    extraction = audit.get("extraction") or {}
    before = extraction.get("unpacked_total_bytes")
    preserve = extraction.get("kept_source_bytes")
    if type(before) is not int or type(preserve) is not int:
        return None
    rule = sum(r["bytes"] for r in audit.get("inventory", [])
               if r.get("disposition") == "retained" and r.get("suffix") in RULE_SUFFIXES)
    return {"before": before, "rule": rule, "preserve": preserve}


def summarize(run_dir):
    totals, unmeasured = {}, set()
    for vdir in sorted(pathlib.Path(run_dir, "papers").glob("*/versions/v*")):
        key = vdir.parents[1].name
        audit = _read(vdir / "source_assets_audit.json")
        manifest = _read(vdir / "manifest.json")
        sizes = version_sizes(audit) if audit else None
        m2_input = manifest.get("kept_source_bytes") if manifest else None
        if sizes is None or type(m2_input) is not int:
            unmeasured.add(key)
            continue
        paper = totals.setdefault(key, dict.fromkeys(FIELDS, 0))
        for name, value in {**sizes, "m2_input": m2_input}.items():
            paper[name] += value
    measured = {k: v for k, v in totals.items() if k not in unmeasured}
    n = len(measured)
    mean = {f: (sum(p[f] for p in measured.values()) / n if n else None) for f in FIELDS}
    return {
        "papers_measured": n,
        "papers_unmeasured": len(unmeasured | set(totals)) - n,
        "mean_bytes_per_paper": mean,
        "mean_bytes_filtered_by_preserve_policy": (
            mean["before"] - mean["preserve"] if n else None),
    }


if __name__ == "__main__":
    print(json.dumps(summarize(sys.argv[1]), indent=2))