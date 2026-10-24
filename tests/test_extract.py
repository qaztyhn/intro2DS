"""Small offline source fixtures; no arXiv requests."""

import gzip
import io
import tarfile

import pytest

from src.common import EventLogger, read_json, write_json_atomic
from src.downloader.http import make_manifest
from src.extract import ExtractionLimits, extract_paper, extract_source, extract_version
from src.schema import paper_directory
from src.utils.errors import CrawlError
from src import metrics

TEX = b"\\documentclass{article}\r\n% original bytes \xff\r\n\\includegraphics{a}\n"


def tar_bytes(files, *, fmt=tarfile.PAX_FORMAT):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=fmt) as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


def extract(tmp_path, content, **kwargs):
    archive = tmp_path / "source.archive"
    archive.write_bytes(content)
    output = tmp_path / "source"
    return extract_source(archive, output, **kwargs), output


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("fmt", [tarfile.PAX_FORMAT, tarfile.USTAR_FORMAT, tarfile.GNU_FORMAT])
def test_tar_preserves_bytes_paths_and_measures_all_files(tmp_path, compressed, fmt):
    files = {"main.tex": TEX, "sections/a.tex": b"a", "bib/refs.bib": b"@article{x}",
             "fig/a.png": b"image", "style.sty": b"style"}
    payload = tar_bytes(files, fmt=fmt)
    if compressed:
        payload = gzip.compress(payload)
    result, output = extract(tmp_path, payload)
    assert result["extract_status"] == "ok"
    assert result["source_format"] == ("tar.gz" if compressed else "tar")
    assert result["archive_bytes"] == len(payload)
    assert result["unpacked_total_bytes"] == sum(map(len, files.values()))
    assert result["kept_source_bytes"] == len(TEX) + 1 + len(b"@article{x}")
    assert result["source_files"] == ["bib/refs.bib", "main.tex", "sections/a.tex"]
    for name in result["source_files"]:
        assert (output / name).read_bytes() == files[name]
    assert not (output / "fig").exists()


@pytest.mark.parametrize("compressed", [False, True])
def test_single_tex_detected_by_content(tmp_path, compressed):
    result, output = extract(tmp_path, gzip.compress(TEX) if compressed else TEX)
    assert result["extract_status"] == "ok"
    assert result["source_format"] == ("gz" if compressed else "tex")
    assert (output / "main.tex").read_bytes() == TEX


@pytest.mark.parametrize("content", [tar_bytes({"x.png": b"abc"}),
                                    tar_bytes({"ref.bib": b"@article{x}"}),
                                    tar_bytes({}), b"%PDF-1.4\nfixture",
                                    gzip.compress(b"%PDF-1.4\nfixture")])
def test_valid_without_tex_is_empty(tmp_path, content):
    result, output = extract(tmp_path, content)
    assert result["extract_status"] == "empty"
    assert result["error_code"] == "NO_TEX_SOURCE"
    assert output.is_dir()


@pytest.mark.parametrize("content,code", [
    (b"\x1f\x8bbroken", "CORRUPT_SOURCE"),
    (gzip.compress(TEX)[:-5], "CORRUPT_SOURCE"),
    (tar_bytes({"main.tex": TEX})[:520], "CORRUPT_SOURCE"),
    (b"PK\x03\x04zip", "UNSUPPORTED_FORMAT"),
    (b"<html>\\begin{document}error</html>", "UNSUPPORTED_FORMAT"),
    (b"arbitrary text", "UNSUPPORTED_FORMAT"),
])
def test_bad_input_never_publishes_partial_source(tmp_path, content, code):
    result, output = extract(tmp_path, content)
    assert result["extract_status"] == "failed"
    assert result["error_code"] == code
    assert result["kept_source_bytes"] is None
    assert not output.exists()
    assert not list(tmp_path.glob(".extract-*"))


@pytest.mark.parametrize("name", ["../escape.tex", "/escape.tex", "a/../../x.tex",
                                 "C:/x.tex", "a\\x.tex"])
def test_rejects_unsafe_names_even_on_filtered_files(tmp_path, name):
    result, output = extract(tmp_path, tar_bytes({"main.tex": TEX, name: b"bad"}))
    assert result["extract_status"] == "failed"
    assert result["error_code"] == "CORRUPT_SOURCE"
    assert not output.exists()


@pytest.mark.parametrize("entry_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE,
                                       tarfile.CHRTYPE, tarfile.BLKTYPE])
def test_rejects_links_and_special_entries(tmp_path, entry_type):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        entry = tarfile.TarInfo("unsafe.tex")
        entry.type, entry.linkname = entry_type, "../../outside"
        archive.addfile(entry)
    result, output = extract(tmp_path, stream.getvalue())
    assert result["extract_status"] == "failed"
    assert not output.exists()


@pytest.mark.parametrize("limits", [ExtractionLimits(max_archive_bytes=5),
                                   ExtractionLimits(max_file_bytes=5),
                                   ExtractionLimits(max_unpacked_bytes=5),
                                   ExtractionLimits(max_members=1)])
def test_resource_limits(tmp_path, limits):
    result, output = extract(tmp_path, tar_bytes({"main.tex": TEX, "x.bib": b"x"}), limits=limits)
    assert result["error_code"] == "CORRUPT_SOURCE"
    assert not output.exists()


def test_gzip_expansion_limit(tmp_path):
    limits = ExtractionLimits(max_unpacked_bytes=10, max_members=1)
    result, output = extract(tmp_path, gzip.compress(b"x" * (2 * 1024 * 1024)), limits=limits)
    assert result["extract_status"] == "failed"
    assert not output.exists()


def test_v7_tar_without_ustar_magic(tmp_path):
    # V7 uses the same basic header/checksum but has no ustar marker.
    data = bytearray(tar_bytes({"main.tex": TEX}))
    data[257:512] = b"\0" * 255
    data[148:156] = b" " * 8
    data[148:156] = f"{sum(data[:512]):06o}\0 ".encode("ascii")
    result, output = extract(tmp_path, bytes(data))
    assert result["extract_status"] == "ok"
    assert (output / "main.tex").read_bytes() == TEX


def test_duplicate_normalized_tar_paths(tmp_path):
    result, output = extract(tmp_path, tar_bytes({"main.tex": TEX, "./main.tex": TEX}))
    assert result["extract_status"] == "failed"
    assert not output.exists()


def test_large_pax_header_is_bounded_before_member_is_loaded(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo("main.tex")
        member.size = len(TEX)
        member.pax_headers = {"comment": "x" * (1024 * 1024 + 1)}
        archive.addfile(member, io.BytesIO(TEX))
    result, output = extract(tmp_path, stream.getvalue())
    assert result["error_code"] == "CORRUPT_SOURCE"
    assert not output.exists()


def test_existing_output_and_symlink_are_preserved(tmp_path):
    output = tmp_path / "source"
    output.mkdir()
    (output / "old.tex").write_bytes(TEX)
    result, _ = extract(tmp_path, TEX)
    assert result["extract_status"] == "failed"
    assert (output / "old.tex").read_bytes() == TEX
    (tmp_path / "link").symlink_to(output, target_is_directory=True)
    assert extract_source(tmp_path / "source.archive", tmp_path / "link")["extract_status"] == "failed"


def seed_version(tmp_path, arxiv_id="2507.01659", version=1, content=TEX):
    cfg = {"output_root": tmp_path, "run_id": "offline"}
    paper = paper_directory(cfg, arxiv_id)
    archive = tmp_path / "offline" / "tmp" / f"v{version}" / "source.archive"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(content)
    manifest = make_manifest(arxiv_id, version)
    manifest.update(download_status="ok", archive_path=archive.relative_to(tmp_path / "offline").as_posix(),
                    archive_bytes=len(content), source_format="unknown")
    write_json_atomic(paper / "versions" / f"v{version}" / "manifest.json", manifest)
    return cfg, paper, archive


def test_manifest_resume_without_archive_and_old_id(tmp_path):
    cfg, paper, archive = seed_version(tmp_path, "hep-th/9901001")
    result = extract_version(cfg, "hep-th/9901001", 1)
    assert result["arxiv_id"] == "hep-th/9901001"
    assert result["parse_status"] == "pending"
    archive.unlink()
    assert extract_version(cfg, "hep-th/9901001", 1) == result
    path = paper / "versions/v1/manifest.json"
    before = path.read_bytes()
    (paper / "versions/v1/source/main.tex").unlink()
    with pytest.raises(CrawlError):
        extract_version(cfg, "hep-th/9901001", 1)
    assert path.read_bytes() == before


def test_resume_after_source_published_but_manifest_write_interrupted(tmp_path, monkeypatch):
    cfg, paper, _ = seed_version(tmp_path)
    import src.extract as module
    original = module.write_json_atomic
    def interrupted(*args):
        raise OSError("interrupted manifest write")
    monkeypatch.setattr(module, "write_json_atomic", interrupted)
    with pytest.raises(OSError):
        extract_version(cfg, "2507.01659", 1)
    source = paper / "versions/v1/source/main.tex"
    inode = source.stat().st_ino
    monkeypatch.setattr(module, "write_json_atomic", original)
    assert extract_version(cfg, "2507.01659", 1)["extract_status"] == "ok"
    assert source.stat().st_ino == inode
    assert source.read_bytes() == TEX


def test_retry_never_overwrites_different_existing_source(tmp_path):
    cfg, paper, _ = seed_version(tmp_path)
    source = paper / "versions/v1/source/main.tex"
    source.parent.mkdir()
    source.write_bytes(b"existing different data")
    result = extract_version(cfg, "2507.01659", 1)
    assert result["extract_status"] == "failed"
    assert source.read_bytes() == b"existing different data"


def test_missing_archive_keeps_previously_measured_size(tmp_path):
    cfg, _, archive = seed_version(tmp_path)
    archive.unlink()
    result = extract_version(cfg, "2507.01659", 1)
    assert result["extract_status"] == "failed"
    assert result["archive_bytes"] == len(TEX)


def test_hook_finishes_other_versions_before_reporting_failure(tmp_path):
    cfg, paper, _ = seed_version(tmp_path, version=1, content=b"bad")
    seed_version(tmp_path, version=2)
    write_json_atomic(paper / "metadata.json", {"status": "ok", "versions": [1, 2]})
    logger = EventLogger(tmp_path / "events.jsonl", "offline")
    with pytest.raises(CrawlError):
        extract_paper(cfg, "2507.01659", logger)
    assert read_json(paper / "versions/v1/manifest.json")["extract_status"] == "failed"
    assert read_json(paper / "versions/v2/manifest.json")["extract_status"] == "ok"

def test_hook_measures_each_version_when_metrics_logger_is_given(tmp_path):
    cfg, paper, _ = seed_version(tmp_path, version=1)
    seed_version(tmp_path, version=2)
    write_json_atomic(paper / "metadata.json", {"status": "ok", "versions": [1, 2]})
    logger = EventLogger(tmp_path / "events.jsonl", "offline")
    metrics_logger = metrics.EventLogger(tmp_path / "metrics_events.jsonl", "offline")
    extract_paper(cfg, "2507.01659", logger, metrics_logger=metrics_logger)
    events = metrics.read_events(tmp_path / "metrics_events.jsonl")
    finished = [e for e in events if e["stage"] == "extract" and e["event"] == "finished"]
    assert [e["version"] for e in finished] == [1, 2]
    assert {e["status"] for e in finished} == {"ok"}
    assert {e["arxiv_id"] for e in finished} == {"2507.01659"}

def test_header_budget_is_per_member_not_per_archive(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for index in range(6):
            data = bytes([index]) * (300 * 1024)  # 6 members x 300 KiB, well over 1 MiB in total
            member = tarfile.TarInfo(f"figure{index}.pdf")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        archive.addfile(tarfile.TarInfo("main.tex"), io.BytesIO(b""))
        tex = tarfile.TarInfo("paper.tex")
        tex.size = len(TEX)
        archive.addfile(tex, io.BytesIO(TEX))
    result, output = extract(tmp_path, stream.getvalue())
    assert result["error_code"] is None
    assert result["extract_status"] == "ok"