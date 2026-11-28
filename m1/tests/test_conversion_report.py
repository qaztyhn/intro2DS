import json

from m1.src.tools.conversion_report import summarize


def _audit(run, key, records):
    d = run / "papers" / key / "versions" / "v1"
    d.mkdir(parents=True)
    (d / "source_assets_audit.json").write_text(json.dumps(
        {"arxiv_id": key.replace("-", "."), "version": 1, "records": records}))


def test_counts_conversions_and_dropped_tex(tmp_path):
    _audit(tmp_path, "2501-00001", [
        {"original": "main.bbl", "conversion": "biber_to_bib"},
        {"original": "a.tex", "error": "binary/NUL content"},
        {"original": "x.txt", "error": "plain txt not identified as TeX source"},
    ])
    result = summarize(tmp_path)
    assert result["versions_audited"] == 1
    assert result["conversions"] == {"biber_to_bib": 1}
    assert result["errors_by_suffix"] == {".tex": 1, ".txt": 1}
    assert result["dropped_tex_bib"] == ["2501.00001v1/a.tex"]