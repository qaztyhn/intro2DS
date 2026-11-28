import json

from m1.src.tools.size_report import summarize


def _version(run, key, v, before, kept, inventory, m2_input, complete=True):
    d = run / "papers" / key / "versions" / f"v{v}"
    d.mkdir(parents=True)
    audit = {"archive_scan_complete": complete, "inventory": inventory,
             "extraction": {"unpacked_total_bytes": before, "kept_source_bytes": kept}}
    (d / "source_assets_audit.json").write_text(json.dumps(audit))
    (d / "manifest.json").write_text(json.dumps({"kept_source_bytes": m2_input}))


def _item(path, size, suffix, disposition="retained"):
    return {"path": path, "bytes": size, "suffix": suffix, "disposition": disposition}


def test_sums_versions_and_separates_rule_from_preserve(tmp_path):
    inv = [_item("a.tex", 10, ".tex"), _item("a.sty", 5, ".sty"), _item("f.png", 100, ".png", "filtered")]
    _version(tmp_path, "2501-00001", 1, 115, 15, inv, 20)
    _version(tmp_path, "2501-00001", 2, 115, 15, inv, 20)
    result = summarize(tmp_path)
    mean = result["mean_bytes_per_paper"]
    assert result["papers_measured"] == 1
    assert (mean["before"], mean["rule"], mean["preserve"], mean["m2_input"]) == (230, 20, 30, 40)


def test_incomplete_version_makes_paper_unmeasured(tmp_path):
    _version(tmp_path, "2501-00002", 1, 10, 10, [], 10)
    _version(tmp_path, "2501-00002", 2, 10, 10, [], 10, complete=False)
    result = summarize(tmp_path)
    assert result["papers_measured"] == 0
    assert result["papers_unmeasured"] == 1