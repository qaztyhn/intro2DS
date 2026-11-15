"""Tests for the TV4 benchmark harness.

No test hard-codes a benchmark figure: they check that measured values exist, are
positive, are consistent with each other, and that the harness refuses to overwrite
or reuse results.
"""

import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import tv4_builders as fx  # noqa: E402

from src.parsing import benchmark  # noqa: E402
from src.parsing.deduplication import deduplicate_elements  # noqa: E402

IDS = ["2310.12345", "2310.12346", "2401.00001"]

REQUIRED_KEYS = {
    "schema_version", "label", "mode", "process_source", "data_source", "started_at_utc",
    "run_dir_name", "id_list", "config", "environment", "counts", "failures", "stages",
    "end_to_end_wall_seconds", "peak_rss_bytes", "peak_rss_bytes_whole_run",
    "process_lifetime_max_rss_bytes", "output", "throughput", "validation", "notes"}
ENV_KEYS = {"platform", "system", "python_version", "cpu_count", "cpu_count_available",
            "total_ram_bytes", "psutil_version", "pyarrow_version", "git_sha", "git_dirty"}


# ----------------------------------------------------------------- helpers --

def make_documents(ids=IDS):
    out = {}
    for aid in ids:
        docs = fx.tv4_two_versions_one_sentence_changed()
        for d in docs:
            d.arxiv_id = aid
        out[aid] = docs
    return out


def ids_file(tmp_path, ids=IDS, name="ids.txt"):
    path = tmp_path / name
    path.write_text("# header\n" + "\n".join(ids) + "\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_pipeline(monkeypatch):
    """Keep tests independent of whether TV1's pipeline module has been merged."""
    monkeypatch.setattr(benchmark, "_load_pipeline", lambda: None)


def run(tmp_path, label="trial", **kwargs):
    kwargs.setdefault("documents", make_documents())
    return benchmark.run_benchmark(tmp_path / "m1_run", ids_file(tmp_path), tmp_path / "out",
                                   label, **kwargs)


# ------------------------------------------------------- stage measuring ----

def test_stage_accumulates_time_calls_and_peak():
    with benchmark.Benchmark(0.01) as bench:
        for _ in range(3):
            with bench.stage("work"):
                time.sleep(0.02)
    report = bench.stage_report()["work"]
    assert report["calls"] == 3
    assert report["wall_seconds"] >= 0.05
    assert report["peak_rss_bytes"] >= report["rss_start_bytes"] > 0
    assert bench.run_wall_seconds >= report["wall_seconds"]
    assert bench.peak_rss_bytes >= report["peak_rss_bytes"]


def test_stage_works_as_a_decorator_and_keeps_time_when_the_function_raises():
    with benchmark.Benchmark(0.01) as bench:
        @bench.stage("decorated")
        def work(fail):
            time.sleep(0.01)
            if fail:
                raise KeyError("boom")
            return 42

        assert work(False) == 42
        with pytest.raises(KeyError):
            work(True)
    report = bench.stage_report()["decorated"]
    assert report["calls"] == 2 and report["wall_seconds"] > 0


def test_nested_stages_measure_independently():
    with benchmark.Benchmark(0.01) as bench:
        with bench.stage("outer"):
            time.sleep(0.02)
            with bench.stage("inner"):
                time.sleep(0.02)
    report = bench.stage_report()
    assert report["outer"]["wall_seconds"] > report["inner"]["wall_seconds"] > 0


def test_sampler_sees_memory_that_is_held_for_a_while():
    with benchmark.Benchmark(0.01) as bench:
        with bench.stage("quiet"):
            time.sleep(0.05)
        with bench.stage("big"):
            block = bytes(range(256)) * (200 * 1024)  # ~50 MB, all pages touched
            time.sleep(0.15)
            assert len(block) > 0
            del block
    report = bench.stage_report()
    gain = report["big"]["peak_rss_bytes"] - report["big"]["rss_start_bytes"]
    assert gain > 20 * 1024 * 1024
    assert report["big"]["peak_rss_bytes"] > report["quiet"]["peak_rss_bytes"] - 1024 * 1024


def test_count_helper_adds_up():
    with benchmark.Benchmark(0.05) as bench:
        bench.count("versions", 2)
        bench.count("versions")
    assert bench.counters == {"versions": 3}


def test_without_psutil_the_lifetime_high_water_mark_is_used(monkeypatch):
    monkeypatch.setattr(benchmark, "_tree_rss", None)
    with benchmark.Benchmark(0.01) as bench:
        with bench.stage("work"):
            time.sleep(0.02)
    report = bench.stage_report()["work"]
    assert report["peak_rss_source"] == "getrusage_process_lifetime"
    assert report["peak_rss_bytes"] > 0 and bench.peak_rss_bytes > 0


def test_lifetime_peak_is_positive_and_in_bytes():
    assert benchmark.lifetime_peak_rss_bytes() > 1024 * 1024


# ------------------------------------------------------------ environment ---

def test_environment_has_the_documented_fields():
    env = benchmark.collect_environment()
    assert ENV_KEYS <= set(env)
    assert env["cpu_count"] >= 1 and env["total_ram_bytes"] > 0
    assert env["python_version"].count(".") == 2
    assert env["pyarrow_version"]
    assert env["git_dirty"] in (True, False, None)


def test_environment_does_not_leak_user_names_or_paths():
    text = json.dumps(benchmark.collect_environment())
    assert str(Path.home()) not in text and Path.home().name not in text


def test_platform_detection(monkeypatch):
    monkeypatch.delenv("COLAB_RELEASE_TAG", raising=False)
    monkeypatch.setattr(benchmark.platform, "release", lambda: "6.1.0-microsoft-standard-WSL2")
    assert benchmark.detect_platform() == "wsl"
    monkeypatch.setattr(benchmark.platform, "release", lambda: "6.1.0-generic")
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Linux")
    assert benchmark.detect_platform() == "linux"
    monkeypatch.setenv("COLAB_RELEASE_TAG", "release-colab-123")
    assert benchmark.detect_platform() == "colab"


def test_id_digest_ignores_order_and_duplicates():
    a = benchmark.id_list_digest(["b", "a", "c"])
    assert a == benchmark.id_list_digest(["c", "a", "b", "a"])
    assert a == hashlib.sha256(b"a\nb\nc").hexdigest()
    assert a != benchmark.id_list_digest(["a", "b"])


# --------------------------------------------------------------- the run ----

def test_tv4_only_run_reports_every_documented_field(tmp_path):
    result = run(tmp_path, config={"workers": 1})
    assert REQUIRED_KEYS <= set(result)
    assert ENV_KEYS <= set(result["environment"])
    assert result["mode"] == "tv4_only" and result["data_source"] == "caller_documents"
    assert result["config"] == {"workers": 1} and result["label"] == "trial"
    counts = result["counts"]
    assert counts["publications_attempted"] == 3 and counts["publications_ok"] == 3
    assert counts["publications_failed"] == 0
    assert counts["versions_exported"] == 6          # 3 publications x 2 versions
    assert counts["elements"] == 24 and counts["hierarchy_rows"] == 24  # example (a) x 3


def test_measured_values_exist_and_are_positive(tmp_path):
    result = run(tmp_path)
    assert result["end_to_end_wall_seconds"] > 0
    assert result["peak_rss_bytes"] > 0 and result["peak_rss_bytes_whole_run"] > 0
    assert result["process_lifetime_max_rss_bytes"] > 0
    assert result["output"]["total_bytes"] > 0
    for name in ("end_to_end", "deduplicate", "export_parts", "merge", "validate", "checksum"):
        stage = result["stages"][name]
        assert stage["wall_seconds"] > 0 and stage["calls"] >= 1, name
        assert stage["peak_rss_bytes"] > 0, name
    assert result["stages"]["deduplicate"]["calls"] == 3
    assert result["stages"]["export_parts"]["calls"] == 3
    assert result["stages"]["merge"]["calls"] == 1


def test_end_to_end_excludes_validation_and_checksums(tmp_path):
    stages = run(tmp_path)["stages"]
    parts = sum(stages[n]["wall_seconds"] for n in ("deduplicate", "export_parts", "merge"))
    assert stages["end_to_end"]["wall_seconds"] >= parts
    assert stages["validate"]["wall_seconds"] > 0 and stages["checksum"]["wall_seconds"] > 0


def test_ratios_state_numerator_denominator_and_definition(tmp_path):
    result = run(tmp_path)
    for name, ratio in result["throughput"].items():
        assert set(ratio) == {"numerator", "denominator", "denominator_definition", "value"}
        assert ratio["denominator_definition"], name
        assert ratio["value"] == pytest.approx(ratio["numerator"] / ratio["denominator"])
    vps = result["throughput"]["versions_per_second"]
    assert vps["numerator"] == result["counts"]["versions_exported"]
    assert vps["denominator"] == result["end_to_end_wall_seconds"]
    mean = result["throughput"]["mean_seconds_per_publication"]
    assert mean["denominator"] == result["counts"]["publications_attempted"] == 3


def test_id_list_fingerprint_matches_the_ids(tmp_path):
    info = run(tmp_path)["id_list"]
    assert info["count"] == 3
    assert info["sha256"] == hashlib.sha256("\n".join(sorted(IDS)).encode()).hexdigest()
    assert info["file_name"] == "ids.txt"


def test_output_sizes_and_checksums_match_the_files_on_disk(tmp_path):
    result = run(tmp_path)
    tables = tmp_path / "out" / "trial" / "tables"
    files = result["output"]["files"]
    assert set(files) == {"elements.parquet", "hierarchy.parquet", "hierarchy_internal.parquet",
                          "bibtex.parquet"}
    for name, info in files.items():
        data = (tables / name).read_bytes()
        assert info["bytes"] == len(data) and info["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["output"]["total_bytes"] == sum(f["bytes"] for f in files.values())


def test_validation_summary_is_recorded_and_can_be_skipped(tmp_path):
    assert run(tmp_path)["validation"] == {"passed": True, "error_total": 0, "warning_total": 0}
    skipped = run(tmp_path, label="novalidate", validate=False)
    assert skipped["validation"] is None and "validate" not in skipped["stages"]


def test_result_file_is_written_and_equals_the_returned_dict(tmp_path):
    result = run(tmp_path)
    written = json.loads((tmp_path / "out" / "benchmark_trial.json").read_text())
    assert written == json.loads(json.dumps(result, default=str))


def test_parts_are_removed_unless_kept(tmp_path):
    result = run(tmp_path)
    assert not (tmp_path / "out" / "trial" / "parts").exists()
    assert result["output"]["intermediate_parts_bytes"] > 0 and not result["output"]["parts_kept"]
    kept = run(tmp_path, label="kept", keep_parts=True)
    assert (tmp_path / "out" / "kept" / "parts").is_dir() and kept["output"]["parts_kept"]


def test_no_absolute_paths_or_user_names_in_the_result(tmp_path):
    text = json.dumps(run(tmp_path), default=str)
    assert str(tmp_path) not in text and Path.home().name not in text


# ------------------------------------------- no overwrite / no caching ------

def test_same_label_is_refused_and_the_old_result_is_untouched(tmp_path):
    run(tmp_path)
    path = tmp_path / "out" / "benchmark_trial.json"
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        run(tmp_path)
    assert path.read_bytes() == before


def test_a_new_label_does_a_fresh_run_in_its_own_directory(tmp_path):
    first, second = run(tmp_path, "one"), run(tmp_path, "two")
    assert (tmp_path / "out" / "one" / "tables").is_dir()
    assert (tmp_path / "out" / "two" / "tables").is_dir()
    assert second["stages"]["deduplicate"]["calls"] == first["stages"]["deduplicate"]["calls"] == 3
    assert second["started_at_utc"] >= first["started_at_utc"]
    assert first["output"]["files"] == second["output"]["files"]  # same input, same bytes


def test_existing_work_directory_is_never_reused(tmp_path):
    (tmp_path / "out" / "trial").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        run(tmp_path)


@pytest.mark.parametrize("label", ["", "../escape", "a/b", ".hidden", "with space", "x" * 101])
def test_unsafe_labels_are_rejected(tmp_path, label):
    with pytest.raises(ValueError):
        run(tmp_path, label)
    assert not (tmp_path / "out").exists()


def test_empty_id_list_is_rejected(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("# nothing\n\n")
    with pytest.raises(ValueError):
        benchmark.run_benchmark(tmp_path, path, tmp_path / "out", "x", documents={})


# ---------------------------------------------- modes / failures ------------

def test_without_a_pipeline_or_documents_the_run_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="pipeline"):
        benchmark.run_benchmark(tmp_path, ids_file(tmp_path), tmp_path / "out", "x")
    assert not (tmp_path / "out").exists()


def fake_process(run_dir, arxiv_id):
    docs = make_documents([arxiv_id])[arxiv_id]
    elements, hierarchy = deduplicate_elements(docs)
    entry = type("Entry", (), {"original_key": "k1", "entry_type": "article",
                               "fields": {"title": "T", "authors": ["A"], "year": 2020}})()
    return elements, hierarchy, [entry]


def test_a_given_process_runs_in_pipeline_mode_and_converts_bib_entries(tmp_path):
    result = benchmark.run_benchmark(tmp_path / "m1", ids_file(tmp_path), tmp_path / "out",
                                     "p", process=fake_process)
    assert result["mode"] == "pipeline" and result["data_source"] == "m1_run_dir"
    assert result["process_source"].endswith("fake_process")
    assert result["counts"]["bibtex_rows"] == 3
    assert "pipeline" in result["stages"] and "deduplicate" not in result["stages"]
    assert result["validation"]["passed"]


def test_an_importable_pipeline_is_discovered(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "_load_pipeline", lambda: fake_process)
    result = benchmark.run_benchmark(tmp_path / "m1", ids_file(tmp_path), tmp_path / "out", "d")
    assert result["mode"] == "pipeline"


def test_one_failing_publication_does_not_stop_the_batch(tmp_path):
    def flaky(run_dir, arxiv_id):
        if arxiv_id == IDS[1]:
            raise ValueError("cannot parse")
        return fake_process(run_dir, arxiv_id)

    result = benchmark.run_benchmark(tmp_path / "m1", ids_file(tmp_path), tmp_path / "out",
                                     "f", process=flaky)
    assert result["counts"]["publications_ok"] == 2
    assert result["counts"]["publications_failed"] == 1
    assert result["failures"] == [{"arxiv_id": IDS[1], "stage": "pipeline",
                                   "error_type": "ValueError", "message": "cannot parse"}]
    assert result["throughput"]["mean_seconds_per_publication"]["denominator"] == 3


def test_a_publication_whose_rows_are_invalid_fails_at_export(tmp_path):
    def bad_rows(run_dir, arxiv_id):
        elements, hierarchy, bibtex = fake_process(run_dir, arxiv_id)
        elements[0]["surprise"] = 1
        return elements, hierarchy, bibtex

    result = benchmark.run_benchmark(tmp_path / "m1", ids_file(tmp_path), tmp_path / "out",
                                     "b", process=bad_rows)
    assert result["counts"]["publications_ok"] == 0
    assert {f["stage"] for f in result["failures"]} == {"export_parts"}
    assert result["output"]["total_bytes"] > 0  # empty tables are still written


def test_missing_documents_are_recorded_as_failures(tmp_path):
    result = run(tmp_path, documents={IDS[0]: make_documents()[IDS[0]]})
    assert result["counts"]["publications_ok"] == 1
    assert result["counts"]["publications_failed"] == 2


# --------------------------------------------------------- reading results --

def test_load_benchmarks_returns_one_row_per_result_sorted_by_label(tmp_path):
    run(tmp_path, "b_run")
    run(tmp_path, "a_run")
    frame = benchmark.load_benchmarks(tmp_path / "out")
    assert list(frame["label"]) == ["a_run", "b_run"]
    for column in ("mode", "platform", "id_list_sha256", "end_to_end_wall_seconds",
                   "peak_rss_bytes", "output_total_bytes", "versions_per_second",
                   "stage_end_to_end_seconds", "stage_merge_seconds"):
        assert column in frame.columns, column
    assert (frame["end_to_end_wall_seconds"] > 0).all() and (frame["peak_rss_bytes"] > 0).all()
    assert list(frame["file"]) == ["benchmark_a_run.json", "benchmark_b_run.json"]


def test_load_benchmarks_on_an_empty_directory_keeps_the_columns(tmp_path):
    frame = benchmark.load_benchmarks(tmp_path)
    assert len(frame) == 0 and "label" in frame.columns and "peak_rss_bytes" in frame.columns


def test_comparable_runs_have_no_reasons(tmp_path):
    first, second = run(tmp_path, "one"), run(tmp_path, "two")
    assert benchmark.check_comparable(first, second) == []


def test_runs_on_different_id_lists_or_outputs_are_not_comparable(tmp_path):
    first = run(tmp_path, "one")
    other = benchmark.run_benchmark(
        tmp_path / "m1", ids_file(tmp_path, IDS[:2], "two.txt"), tmp_path / "out", "two",
        documents=make_documents())
    reasons = benchmark.check_comparable(first, other)
    assert any("ID lists" in r for r in reasons) and any("byte-identical" in r for r in reasons)


def test_runs_in_different_environments_are_not_comparable(tmp_path):
    first, second = run(tmp_path, "one"), run(tmp_path, "two")
    second["environment"] = dict(second["environment"], platform="colab", cpu_count=2)
    reasons = benchmark.check_comparable(first, second)
    assert "different environment: platform" in reasons
    assert "different environment: cpu_count" in reasons


# ------------------------------------- file systems without hard links ------

def test_result_is_written_without_hard_link_support(tmp_path, monkeypatch):
    def no_links(src, dst, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(benchmark.os, "link", no_links)
    result = run(tmp_path)
    path = tmp_path / "out" / "benchmark_trial.json"
    assert json.loads(path.read_text())["label"] == result["label"]
    assert [p.name for p in (tmp_path / "out").iterdir() if p.name.startswith(".bench-")] == []
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        run(tmp_path)
    assert path.read_bytes() == before
