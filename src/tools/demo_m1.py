"""Create and validate a tiny synthetic corpus twice, entirely offline.

    python -m src.demo_m1 --run-dir experiments/person2_demo
"""

import argparse
import gzip
import io
import pathlib
import tarfile

from ..utils.common import EventLogger, read_json, write_json_atomic
from ..downloader.http import make_manifest
from ..stages.extract import extract_paper
from ..schema import no_symlinks, paper_directory
from ..stages.storage import (load_manifest, load_paper, save_manifest, save_metadata, save_references, store_paper)
from ..stages.validation import validate_run

DEMO_ID = "2507.01659"  # The fixture is invented, not this real paper's content.


def run_demo(run_dir):
    run = no_symlinks(run_dir)
    cfg = {"output_root": run.parent, "run_id": run.name}
    record = read_json(run / "run.json")
    if run.exists() and any(run.iterdir()) and (
            not isinstance(record, dict) or record.get("synthetic_demo") is not True):
        raise ValueError("choose a separate demo run directory; this run contains other data")
    write_json_atomic(run / "run.json", {"run_id": run.name, "input_ids": [DEMO_ID],
                                        "synthetic_demo": True})
    save_metadata(cfg, DEMO_ID, dict(
        arxiv_id=DEMO_ID, title="SYNTHETIC offline fixture", authors=["Demo Student"],
        submission_date="2025-07-01T00:00:00+00:00",
        revised_dates=["2025-07-02T00:00:00+00:00"], venue=None,
        latest_version=2, versions=[1, 2], status="ok", error_code=None))
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w") as archive:
        for name, data in {
            "main.tex": b"\\documentclass{article}\n\\input{sections/intro}\n",
            "sections/intro.tex": b"\\section{Synthetic example}\r\nOriginal bytes.\r\n",
            "refs.bib": b"@article{demo, title={Synthetic example}}\n",
            "figures/example.png": b"synthetic image bytes, never kept in source/",
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    for version, data in [(1, gzip.compress(content.getvalue())),
                          (2, b"%PDF-1.4\nsynthetic PDF-only fixture")]:
        existing = load_manifest(cfg, DEMO_ID, version)
        if existing and existing.get("extract_status") in {"ok", "empty"}:
            continue
        archive = run / "tmp" / "demo" / f"v{version}" / "source.archive"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(data)
        manifest = make_manifest(DEMO_ID, version)
        manifest.update(download_status="ok", source_format="unknown",
                        archive_path=archive.relative_to(run).as_posix(), archive_bytes=len(data))
        save_manifest(cfg, DEMO_ID, version, manifest)
    save_references(cfg, DEMO_ID, dict(
        arxiv_id=DEMO_ID, status="ok", error_code=None, source="semantic_scholar",
        fetched_at="2026-10-23T00:00:00+00:00", references=[dict(
            arxiv_id=DEMO_ID, ref_arxiv_id=None, ref_title="Synthetic non-arXiv reference",
            ref_authors=[], ref_submission_date=None, ref_s2_id="synthetic-s2-id")]))
    logger = EventLogger(run / "events.jsonl", run.name)
    extract_paper(cfg, DEMO_ID, logger)
    store_paper(cfg, DEMO_ID, logger)
    before = load_paper(cfg, DEMO_ID)
    extract_paper(cfg, DEMO_ID, logger)
    store_paper(cfg, DEMO_ID, logger)
    if before != load_paper(cfg, DEMO_ID):
        raise RuntimeError("demo resume unexpectedly changed the corpus")
    report = validate_run(cfg)
    print(f"Synthetic offline demo: {run}")
    print(f"Validation: {report['status']}; v1=ok, v2=empty; resume unchanged")
    print(f"M2 source: {paper_directory(cfg, DEMO_ID) / 'versions/v1/source'}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=pathlib.Path, default=pathlib.Path("experiments/person2_demo"))
    args = parser.parse_args()
    return 0 if run_demo(args.run_dir)["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
