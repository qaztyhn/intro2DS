"""Count conversions and conversion failures recorded in source_assets_audit.json.

Usage: python -m m1.src.tools.conversion_report <run_dir>
"""
import json
import pathlib
import sys
from collections import Counter


def _read(path):
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def summarize(run_dir):
    conversions, reasons, suffixes, dropped = Counter(), Counter(), Counter(), []
    versions = 0
    for path in sorted(pathlib.Path(run_dir, "papers").glob("*/versions/v*/source_assets_audit.json")):
        audit = _read(path)
        if not audit:
            continue
        versions += 1
        for record in audit.get("records", []):
            if record.get("conversion"):
                conversions[record["conversion"]] += 1
            error = record.get("error")
            if not error:
                continue
            suffix = pathlib.PurePosixPath(record.get("original", "")).suffix.lower() or "<none>"
            reasons[error.split(":")[0]] += 1
            suffixes[suffix] += 1
            if suffix in {".tex", ".bib"}:  # present in the archive but missing from source/
                dropped.append(f"{audit.get('arxiv_id')}v{audit.get('version')}/{record['original']}")
    return {
        "versions_audited": versions,
        "conversions": dict(conversions.most_common()),
        "errors_by_reason": dict(reasons.most_common()),
        "errors_by_suffix": dict(suffixes.most_common()),
        "dropped_tex_bib_count": len(dropped),
        "dropped_tex_bib": dropped[:200],
    }


if __name__ == "__main__":
    print(json.dumps(summarize(sys.argv[1]), indent=2))