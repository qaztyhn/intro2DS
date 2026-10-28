"""Runner guardrails and subprocess integration, entirely offline."""

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from src.tools import benchmark_run as runner
from src.tools.demo_comparison import create_synthetic_run, DEMO_IDS


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    (repo / "src").mkdir(parents=True)
    (repo / "src/__init__.py").write_text("")
    (repo / "src/main.py").write_text("# sequential fixture\n")
    ids = tmp_path / "ids.txt"
    ids.write_text("\n".join(DEMO_IDS) + "\n")
    monkeypatch.setattr(runner, "revision_info", lambda _: {"branch": "fixture", "commit": "fixture", "dirty": False})
    monkeypatch.setattr(runner, "capture_environment", lambda *_: {"fixture": True})
    monkeypatch.setattr(runner, "inspect_instrumentation", lambda *_: {"per_request_hook_scope": True,
                                                                   "arxiv_client_timeout_mode": "configured"})
    monkeypatch.delenv("S2_API_KEY", raising=False)
    return repo, ids, tmp_path / "results"


def test_prepare_freezes_common_ids_and_retains_no_secret(setup, monkeypatch):
    repo, ids, output = setup
    monkeypatch.setenv("S2_API_KEY", "secret-value-do-not-persist")
    run, info = runner.prepare_run(repo, ids, output, "B0")
    cfg = yaml.safe_load((run / "config.yaml").read_text())
    assert cfg["ids_file"] == str(run / "input_ids.txt")
    assert cfg["max_workers"] == 1
    assert cfg["discovery_batch_size"] == 1
    assert info["policy"]["s2_credentials_present"] is True
    assert "secret-value-do-not-persist" not in (run / "benchmark.json").read_text()
    assert not (run / "run.json").exists()  # remains a cold run to the pipeline
    second, _ = runner.prepare_run(repo, ids, output, "B0")
    assert second != run


def test_a_and_b_record_distinct_algorithm_controls(setup):
    repo, ids, output = setup
    (repo / "src/main.py").write_text("# A_BATCH fixture\n")
    _, a = runner.prepare_run(repo, ids, output, "A")
    assert a["config"]["discovery_batch_size"] == 20
    (repo / "src/main.py").write_text("# run_papers fixture\n")
    _, b = runner.prepare_run(repo, ids, output, "B", workers=4, batch_size=10)
    assert b["config"]["max_workers"] == 4
    assert b["config"]["discovery_batch_size"] == 10


@pytest.mark.parametrize("variant", ["A", "B"])
def test_wrong_checkout_rejected_before_execution(setup, variant):
    with pytest.raises(ValueError, match="checkout"):
        runner.prepare_run(*setup, variant)


@pytest.mark.parametrize("content", ["2507.01659\n2507.01659\n", "bad-id\n", "\n"])
def test_invalid_ids_rejected(setup, content):
    repo, ids, output = setup
    ids.write_text(content)
    with pytest.raises(ValueError, match="IDs"):
        runner.prepare_run(repo, ids, output, "B0")


@pytest.mark.parametrize("option", [{"batch_size": 0}, {"workers": True}, {"sample_interval": 0},
                                    {"sample_interval": float("nan")}, {"batch_size": 20}])
def test_invalid_measurement_or_baseline_options(setup, option):
    with pytest.raises(ValueError):
        runner.prepare_run(*setup, "B0", **option)


def test_large_run_is_never_implicit(setup):
    repo, ids, output = setup
    ids.write_text("\n".join(f"2507.{i:05d}" for i in range(41)))
    with pytest.raises(ValueError, match="allow-large"):
        runner.prepare_run(repo, ids, output, "B0")


def test_template_cannot_select_other_ids_or_algorithm(setup, tmp_path):
    template = tmp_path / "template.yaml"
    template.write_text("ids_file: other.txt\nmax_workers: 99\nconfig_name: D1\ntimeout: 20\n")
    run, info = runner.prepare_run(*setup, "B0", template=template)
    assert info["config"]["timeout"] == 20
    assert info["config"]["max_workers"] == 1
    assert info["config"]["config_name"] == "B"
    assert info["input_ids"] == list(DEMO_IDS)


def test_warm_requires_provenance_and_matching_variant(setup, tmp_path):
    with pytest.raises(ValueError, match="Warm mode"):
        runner.prepare_run(*setup, "B0", mode="warm")
    seed = create_synthetic_run(tmp_path / "foreign-seed", "A")
    with pytest.raises(ValueError, match="Warm seed"):
        runner.prepare_run(*setup, "B0", mode="warm", warm_from=seed)


def test_warm_copies_own_corpus_without_old_logs(setup, tmp_path, monkeypatch):
    seed = create_synthetic_run(tmp_path / "own-seed", "B0")
    data = json.loads((seed / "benchmark.json").read_text())
    monkeypatch.setattr(runner, "revision_info", lambda _: data["revision"])
    monkeypatch.setattr(runner, "capture_environment", lambda *_: data["environment"])
    run, info = runner.prepare_run(*setup, "B0", mode="warm", warm_from=seed)
    assert (run / "papers").is_dir()
    assert not (run / "metrics_events.jsonl").exists()
    assert info["warm_seed"]["run_id"] == seed.name
    assert json.loads((run / "run.json").read_text())["run_id"] == run.name


def test_external_runner_measures_real_local_subprocess(setup):
    repo, ids, output = setup
    # Real subprocess, invented pipeline. No network or fabricated measurements.
    (repo / "src/main.py").write_text(
        "import json,sys,pathlib,yaml,time\n"
        "cfg=yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())\n"
        "root=pathlib.Path(cfg['output_root'])/cfg['run_id']\n"
        "time.sleep(0.05)\n"
        "(root/'run.json').write_text(json.dumps(dict(run_status='completed')))\n")
    run, sidecar = runner.prepare_run(repo, ids, output, "B0", sample_interval=0.01)
    assert runner.execute_run(repo, run, sidecar, python=sys.executable) == 0
    assert sidecar["run_status"] == "completed"
    assert sidecar["wall_seconds"] >= 0.05
    assert sidecar["resources"]["rss_peak_bytes"] > 0
    assert sidecar["resources"]["rss_mean_bytes"] > 0
    assert sidecar["resources"]["disk_peak_bytes"] > 0


def test_crashed_subprocess_retains_measurements(setup):
    repo, ids, output = setup
    (repo / "src/main.py").write_text("raise RuntimeError('offline failure')\n")
    run, sidecar = runner.prepare_run(repo, ids, output, "B0", sample_interval=0.01)
    assert runner.execute_run(repo, run, sidecar) != 0
    assert sidecar["run_status"] == "crashed"
    assert sidecar["wall_seconds"] > 0
    assert "offline failure" in (run / "console.log").read_text()


def test_forty_paper_preparation_and_unsafe_target_launch_guard(setup, monkeypatch):
    repo, ids, output = setup
    ids.write_text("\n".join(f"2507.{i:05d}" for i in range(40)))
    monkeypatch.setattr(runner, "inspect_instrumentation", lambda *_: {"per_request_hook_scope": False})
    run, sidecar = runner.prepare_run(repo, ids, output, "B0")
    assert len(sidecar["input_ids"]) == 40
    assert not sidecar["pilot_preflight"]["ready_for_local_pilot"]
    assert sidecar["pilot_preflight"]["colab_cpu_only_verified"] is None
    with pytest.raises(ValueError, match="Pilot preflight blocked"):
        runner.execute_run(repo, run, sidecar)
    assert not (run / "console.log").exists()


@pytest.mark.parametrize("drift", ["revision", "environment", "credentials", "config", "ids", "instrumentation"])
def test_prepare_launch_drift_rejected_before_start(setup, monkeypatch, drift):
    repo, ids, output = setup
    run, sidecar = runner.prepare_run(repo, ids, output, "B0")
    if drift == "revision":
        monkeypatch.setattr(runner, "revision_info", lambda _: {"commit": "different"})
    elif drift == "environment":
        monkeypatch.setattr(runner, "capture_environment", lambda *_: {"different": True})
    elif drift == "credentials":
        monkeypatch.setenv("S2_API_KEY", "fixture-secret")
    elif drift == "config":
        (run / "config.yaml").write_text("timeout: 99\n")
    elif drift == "ids":
        (run / "input_ids.txt").write_text("2507.12345\n")
    else:
        monkeypatch.setattr(runner, "inspect_instrumentation", lambda *_: {"per_request_hook_scope": False})
    with pytest.raises(ValueError, match="changed after preparation"):
        runner.execute_run(repo, run, sidecar)
    assert not (run / "console.log").exists()


def test_zero_timeout_rejected_during_preparation(setup, tmp_path):
    template = tmp_path / "zero.yaml"
    template.write_text("timeout: 0\n")
    with pytest.raises(ValueError, match="timeout must be positive"):
        runner.prepare_run(*setup, "B0", template=template)


def test_safe_forty_id_runner_executes_only_a_synthetic_local_process(setup):
    repo, ids, output = setup
    ids.write_text("\n".join(f"2507.{i:05d}" for i in range(40)))
    (repo / "src/main.py").write_text(
        "import json,pathlib,sys,yaml\n"
        "cfg=yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())\n"
        "ids=pathlib.Path(cfg['ids_file']).read_text().splitlines()\n"
        "root=pathlib.Path(cfg['output_root'])/cfg['run_id']\n"
        "(root/'run.json').write_text(json.dumps(dict(run_status='completed',input_ids=ids)))\n"
        "print('SYNTHETIC local process completed',len(ids))\n")
    run, sidecar = runner.prepare_run(repo, ids, output, "B0")
    assert sidecar["pilot_preflight"]["ready_for_local_pilot"] is True
    assert runner.execute_run(repo, run, sidecar) == 0
    assert len(json.loads((run / "run.json").read_text())["input_ids"]) == 40
    assert "SYNTHETIC local process completed 40" in (run / "console.log").read_text()


def test_timeout_preflight_probes_effective_transport_without_network(tmp_path):
    # Only a disposable synthetic source copy is changed; A/B stay untouched.
    source = Path(__file__).resolve().parents[1] / "src"
    shutil.copytree(source, tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__"))
    before = runner.inspect_instrumentation(sys.executable, tmp_path, 15)
    assert before["per_request_hook_scope"] is True
    assert before["arxiv_client_timeout_mode"] == "not_passed_by_library"
    target = tmp_path / "src/discovery/arxiv_api.py"
    with target.open("a") as handle:
        handle.write('''
_original_make_clients = make_clients
def make_clients(cfg, throttle=None):
    client, opts, throttle = _original_make_clients(cfg, throttle)
    class SyntheticTimeoutSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", cfg["timeout"])
            return super().request(*args, **kwargs)
    client._session = SyntheticTimeoutSession()
    return client, opts, throttle
''')
    after = runner.inspect_instrumentation(sys.executable, tmp_path, 15)
    assert after["arxiv_client_timeout_mode"] == "configured"
    assert after["arxiv_client_timeout_seconds"] == 15
