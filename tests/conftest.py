"""Synthetic multi-version corpus shared by Person 2 tests."""

import pytest

from src.downloader.http import make_manifest
from src.stages.extract import extract_version
from src.schema import paper_directory
from src.stages.storage import save_manifest, save_metadata, save_references


@pytest.fixture
def corpus(tmp_path):
    cfg = {"output_root": tmp_path, "run_id": "offline"}
    identity = "2507.01659"
    paper = paper_directory(cfg, identity)
    save_metadata(cfg, identity, dict(
        arxiv_id=identity, title="Synthetic paper", authors=["An"],
        submission_date="2025-07-01T00:00:00+00:00",
        revised_dates=["2025-07-02T00:00:00+00:00"], venue=None,
        latest_version=2, versions=[1, 2], status="ok", error_code=None))
    run = tmp_path / "offline"
    for version, content in [(1, b"\\documentclass{article}\n\\begin{document}demo\\end{document}"),
                             (2, b"%PDF-1.4\nsynthetic PDF-only source")]:
        archive = run / "tmp" / f"v{version}" / "source.archive"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(content)
        manifest = make_manifest(identity, version)
        manifest.update(download_status="ok", archive_path=archive.relative_to(run).as_posix(),
                        archive_bytes=len(content), source_format="unknown")
        save_manifest(cfg, identity, version, manifest)
        extract_version(cfg, identity, version)
    save_references(cfg, identity, dict(
        arxiv_id=identity, status="ok", error_code=None, source="semantic_scholar",
        fetched_at="2026-10-23T00:00:00+00:00", references=[dict(
            arxiv_id=identity, ref_arxiv_id=None, ref_title=None, ref_authors=[],
            ref_submission_date=None, ref_s2_id="synthetic-s2")]))
    return cfg, identity, paper
