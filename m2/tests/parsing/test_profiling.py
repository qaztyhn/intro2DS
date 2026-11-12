import json
import pytest
from pathlib import Path

from src.parsing import profiling as P


def make_paper(run: Path, arxiv_id, versions, metadata_versions=None):
    pdir = run / "papers" / P.paper_key(arxiv_id)
    pdir.mkdir(parents=True)
    declared = sorted(versions) if metadata_versions is None else metadata_versions
    (pdir / "metadata.json").write_text(
        json.dumps({"arxiv_id": arxiv_id, "versions": declared}))
    for v, spec in versions.items():
        vdir = pdir / "versions" / f"v{v}"
        vdir.mkdir(parents=True)
        files = spec.get("files", {})
        for rel, text in files.items():
            target = vdir / "source" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        manifest = {
            "arxiv_id": arxiv_id, "version": v, "versioned_id": f"{arxiv_id}v{v}",
            "download_status": "ok", "extract_status": "ok",
            "source_format": "tar.gz", "source_files": sorted(files),
            "error_code": None,
        }
        manifest.update(spec.get("manifest", {}))
        (vdir / "manifest.json").write_text(json.dumps(manifest))
    return pdir


SINGLE = {"files": {
    "main.tex": r"\documentclass{article}\begin{document}x\bibliography{refs}\end{document}",
    "refs.bib": "@misc{a,title={A}}",
}}
NESTED = {"files": {
    "main.tex": "\\documentclass{article}\n\\input{sec/a}\n\\include{missing}\n% \\input{ghost}\n",
    "sec/a.tex": "\\input{sec/b}",
    "sec/b.tex": r"\begin{thebibliography}{1}\bibitem{k} Foo 2020.\end{thebibliography}",
}}
NO_TEX = {"files": {}, "manifest": {
    "extract_status": "empty", "error_code": "NO_TEX_SOURCE", "source_files": []}}


def test_single_file_with_bib(tmp_path):
    make_paper(tmp_path, "2501.00001", {1: SINGLE})
    _, recs = P.profile_paper(tmp_path, "2501.00001")
    r = recs[0]
    assert r["category"] == "has_tex" and r["has_tex"]
    assert (r["n_tex"], r["n_bib"], r["multi_file"]) == (1, 1, False)
    assert r["n_root_candidates"] == 1 and r["max_include_depth"] == 0
    assert r["bib_format"] == "bib" and r["missing_bib_files"] == []


def test_nested_includes_missing_and_bibitem(tmp_path):
    make_paper(tmp_path, "2501.00002", {1: NESTED})
    r = P.profile_paper(tmp_path, "2501.00002")[1][0]
    assert r["multi_file"] and r["n_root_candidates"] == 1
    assert r["n_include_cmds"] == 3          # commented \input{ghost} ignored
    assert r["missing_includes"] == ["missing"]
    assert r["max_include_depth"] == 2
    assert r["bib_format"] == "bibitem" and r["n_bibitem"] == 1


def test_no_tex_and_download_not_ok(tmp_path):
    make_paper(tmp_path, "2501.00003", {1: NO_TEX, 2: {"files": {}, "manifest": {
        "download_status": "failed", "extract_status": "pending", "source_files": []}}})
    recs = P.profile_paper(tmp_path, "2501.00003")[1]
    assert [r["category"] for r in recs] == ["no_tex", "download_not_ok"]
    assert not any(r["has_tex"] for r in recs)


def test_bibliography_cmd_without_bib_file(tmp_path):
    make_paper(tmp_path, "2501.00004", {1: {"files": {
        "main.tex": r"\documentclass{article}\bibliography{missing_refs}"}}})
    r = P.profile_paper(tmp_path, "2501.00004")[1][0]
    assert r["missing_bib_files"] == ["missing_refs"]
    assert r["bib_format"] == "none"


def test_version_mismatch_and_no_manifest(tmp_path):
    make_paper(tmp_path, "2501.00005", {1: SINGLE}, metadata_versions=[1, 2])
    paper, recs = P.profile_paper(tmp_path, "2501.00005")
    assert paper["version_mismatch"] is True
    assert [(r["version"], r["category"]) for r in recs] == [(1, "has_tex"), (2, "no_manifest")]


def test_unsafe_listed_paths_are_ignored(tmp_path):
    make_paper(tmp_path, "2501.00006", {1: {
        "files": {"main.tex": r"\documentclass{article}"},
        "manifest": {"source_files": ["main.tex", "../evil.tex", "/abs.tex"]}}})
    r = P.profile_paper(tmp_path, "2501.00006")[1][0]
    assert r["n_source_files"] == 3 and r["n_tex"] == 1
    assert r["listed_files_missing"] == 2


def test_missing_paper_dir_recorded(tmp_path):
    (tmp_path / "papers").mkdir()
    result = P.scan_range(tmp_path, ["2501.99999"], "t")
    assert result["papers"][0]["status"] == "missing_paper_dir"
    assert result["summary"]["counts"]["publications_missing_dir"] == 1
    assert len(result["errors"]) == 1


def test_rates_state_denominators(tmp_path):
    make_paper(tmp_path, "2501.00010", {1: NESTED, 2: NO_TEX})
    make_paper(tmp_path, "2501.00011", {1: SINGLE})
    s = P.scan_range(tmp_path, ["2501.00010", "2501.00011"], "t")["summary"]
    assert s["counts"]["versions"] == 3 and s["counts"]["versions_with_tex"] == 2
    assert s["rates"]["no_tex"]["count"] == 1
    assert s["rates"]["no_tex"]["denominator"] == 3
    assert s["rates"]["multi_file"]["count"] == 1
    assert s["rates"]["multi_file"]["denominator"] == 2
    assert s["rates"]["multi_version"]["denominator"] == 2
    assert s["rates"]["multi_version"]["count"] == 1


def test_samples_cover_categories(tmp_path):
    make_paper(tmp_path, "2501.00010", {1: NESTED, 2: NO_TEX})
    make_paper(tmp_path, "2501.00011", {1: SINGLE})
    samples = P.scan_range(tmp_path, ["2501.00010", "2501.00011"], "t")["samples"]
    reasons = {r for s in samples for r in s["reasons"]}
    assert {"single_file", "multi_file", "nested_include", "multi_version",
            "no_tex", "bib_file", "bibitem", "missing_include"} <= reasons


def test_cli_scan_is_deterministic(tmp_path):
    make_paper(tmp_path, "2501.00010", {1: NESTED, 2: NO_TEX})
    ids = tmp_path / "ids.txt"
    ids.write_text("# comment\n2501.00010\n\n2501.00010\n")
    outs = []
    for name in ("a.json", "b.json"):
        out = tmp_path / name
        assert P.main(["scan", "--run-dir", str(tmp_path), "--ids-file", str(ids),
                       "--label", "t", "--out", str(out)]) == 0
        data = json.loads(out.read_text())
        data.pop("meta")
        outs.append(data)
    assert outs[0] == outs[1]
    assert outs[0]["summary"]["counts"]["publications_requested"] == 1


def test_bbl_counted_and_biblatex_detected(tmp_path):
    make_paper(tmp_path, "2501.00020", {
        1: {"files": {
            "main.tex": r"\documentclass{article}\bibliography{refs}",
            "refs.bbl": r"\begin{thebibliography}{1}\bibitem{k} Foo 2020.\end{thebibliography}",
        }},
        2: {"files": {
            "main.tex": r"\documentclass{article}\bibliography{refs}",
            "refs.bbl": r"\entry{k}{article}{}",
        }},
    })
    r1, r2 = P.profile_paper(tmp_path, "2501.00020")[1]
    assert (r1["n_bbl"], r1["n_bib"], r1["bib_format"]) == (1, 0, "bbl")
    assert r1["n_bbl_biblatex"] == 0
    assert r2["n_bbl_biblatex"] == 1 and r2["n_other"] == 0


def test_unrecoverable_bibliography_rate(tmp_path):
    make_paper(tmp_path, "2501.00021", {1: {"files": {
        "main.tex": r"\documentclass{article}\bibliography{refs}"}}})
    make_paper(tmp_path, "2501.00022", {1: SINGLE})
    s = P.scan_range(tmp_path, ["2501.00021", "2501.00022"], "t")["summary"]
    rate = s["rates"]["bib_cmd_no_recoverable_source"]
    assert (rate["count"], rate["denominator"]) == (1, 2)
    assert s["rates"]["bbl_present"]["count"] == 0


def _two_ranges(tmp_path):
    r1, r2 = tmp_path / "run1", tmp_path / "run2"
    make_paper(r1, "2501.00010", {1: NESTED, 2: NO_TEX})
    make_paper(r1, "2501.00011", {1: SINGLE})
    make_paper(r2, "2501.00011", {1: SINGLE})   # duplicate id across ranges
    make_paper(r2, "2501.00012", {1: SINGLE})
    return [P.scan_range(r1, ["2501.00010", "2501.00011"], "r1"),
            P.scan_range(r2, ["2501.00011", "2501.00012"], "r2")]


def test_merge_dedups_and_recomputes(tmp_path):
    merged = P.merge_scans(_two_ranges(tmp_path))
    assert merged["duplicates"] == [
        {"arxiv_id": "2501.00011", "kept_in": "r1", "dropped_from": "r2"}]
    o = merged["overall"]["counts"]
    assert o["publications_found"] == 3 and o["versions"] == 4
    r2 = merged["ranges"]["r2"]["summary"]["counts"]
    assert r2["publications_found"] == 1 and r2["versions"] == 1
    r1 = merged["ranges"]["r1"]["summary"]["counts"]
    assert r1["versions"] + r2["versions"] == o["versions"]


def test_merge_rejects_duplicate_labels(tmp_path):
    scans = _two_ranges(tmp_path)
    scans[1]["meta"]["label"] = "r1"
    with pytest.raises(ValueError):
        P.merge_scans(scans)


def test_render_report_sections(tmp_path):
    md = P.render_report(P.merge_scans(_two_ranges(tmp_path)))
    for needle in ("# Báo cáo profiling M2", "r1", "r2", "## 2. Tỷ lệ",
                   "2501.00010", "Publication trùng giữa các range", "/4 ("):
        assert needle in md


def test_cli_merge_writes_files_and_is_deterministic(tmp_path):
    paths = []
    for scan in _two_ranges(tmp_path):
        p = tmp_path / f"{scan['meta']['label']}.json"
        p.write_text(json.dumps(scan))
        paths.append(str(p))
    outputs = []
    for tag in ("x", "y"):
        out_json, out_md = tmp_path / f"{tag}.json", tmp_path / f"{tag}.md"
        assert P.main(["merge", "--inputs", *paths, "--out-json", str(out_json),
                       "--out-md", str(out_md)]) == 0
        outputs.append((out_json.read_text(), out_md.read_text()))
    assert outputs[0] == outputs[1]


def test_cli_merge_bad_input_returns_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert P.main(["merge", "--inputs", str(bad), "--out-json", str(tmp_path / "o.json"),
                   "--out-md", str(tmp_path / "o.md")]) == 2