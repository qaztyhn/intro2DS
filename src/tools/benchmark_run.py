"""Run an unchanged pipeline checkout with independent benchmark provenance.

Use --prepare-only to inspect the generated config without issuing API requests.
No checkout, merge, key persistence, or full benchmark is performed implicitly.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from time import perf_counter
from uuid import uuid4

import psutil
import yaml

from .. import metrics
from ..schema import valid_arxiv_id
from .benchmark_protocol import CONFIG_NAMES, DEFAULTS, POLICY_KEYS, PROTOCOL
from .compare_runs import read_object, summarize_run


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


def revision_info(repo):
    return {"branch": _git(repo, "symbolic-ref", "--short", "HEAD")
            if _git(repo, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD" else "detached",
            "commit": _git(repo, "rev-parse", "HEAD"),
            "refs_at_commit": _git(repo, "for-each-ref", "--points-at=HEAD", "--format=%(refname:short)",
                                   "refs/heads", "refs/remotes").splitlines(),
            "dirty": bool(_git(repo, "status", "--porcelain", "--untracked-files=normal"))}


def capture_environment(python, repo):
    """Collect from the interpreter/checkout executing the crawl, never the reader."""
    code = (
        "import json,platform,os; from importlib import metadata; from src.metrics import collect_environment; "
        "e=collect_environment(); e['cpu_model']=platform.processor(); "
        "e['dependencies']={d.metadata['Name'].lower():d.version for d in metadata.distributions()}; "
        "print(json.dumps(e,sort_keys=True))"
    )
    return json.loads(subprocess.check_output([str(python), "-c", code], cwd=repo, text=True))


def inspect_instrumentation(python, repo, timeout=30):
    """Probe target code capabilities without an HTTP call or target edits."""
    code = '''
import inspect, json, sys
from importlib import metadata
from src import metrics
from src.utils import http
from src.discovery.arxiv_api import make_clients
import arxiv
import requests
hook = metrics.make_request_hook(object(), "references")
scoped = (callable(vars(hook).get("for_request"))
          and callable(getattr(http, "_bind_request_hook", None))
          and all("_bind_request_hook(on_attempt)" in inspect.getsource(func)
                  for func in (http.request_with_retry, http.download_file)))
cfg = json.loads(sys.argv[1])
seen = []
class StopBeforeNetwork(Exception):
    pass
def stop_send(self, request, **kwargs):
    seen.append(kwargs.get("timeout"))
    raise StopBeforeNetwork()
# Intercept before adapter.send: no DNS/HTTP call, even for a timeout Session
# wrapper calling another requests.Session. Probe the actual make_clients code.
requests.Session.send = stop_send
client, _, _ = make_clients(cfg)
client.num_retries = 0
client.delay_seconds = 0
try:
    client._parse_feed("https://example.invalid/metrics-timeout-probe")
except StopBeforeNetwork:
    pass
actual = seen[0] if seen else None
def matches_timeout(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == cfg["timeout"]
matches = matches_timeout(actual) or isinstance(actual, (list, tuple)) and len(actual) == 2 and all(matches_timeout(v) for v in actual)
timeout_mode = "configured" if seen and matches else "not_passed_by_library" if seen and actual is None else "unverified"
print(json.dumps({"per_request_hook_scope": scoped, "arxiv_version": metadata.version("arxiv"),
                  "arxiv_client_timeout_mode": timeout_mode, "arxiv_client_timeout_seconds": actual,
                  "http_coverage": "partial"}))
'''
    cfg = dict(DEFAULTS, timeout=timeout)
    return json.loads(subprocess.check_output([str(python), "-c", code, json.dumps(cfg)], cwd=repo, text=True))


def verify_prepared_run(repo, run, sidecar, python):
    """Reject drift between config preparation and actual subprocess launch."""
    revision = revision_info(repo)
    if any(revision.get(key) != sidecar["revision"].get(key) for key in ("branch", "commit", "dirty")):
        raise ValueError("Target Git revision/cleanliness changed after preparation")
    if capture_environment(python, repo) != sidecar["environment"]:
        raise ValueError("Target Python environment changed after preparation")
    if bool(os.getenv("S2_API_KEY")) != sidecar["policy"]["s2_credentials_present"]:
        raise ValueError("Credential presence changed after preparation")
    if yaml.safe_load((run / "config.yaml").read_text(encoding="utf-8")) != sidecar["config"]:
        raise ValueError("Frozen configuration changed after preparation")
    if read_ids(run / "input_ids.txt") != sidecar["input_ids"]:
        raise ValueError("Frozen paper IDs changed after preparation")
    if inspect_instrumentation(python, repo, sidecar["config"]["timeout"]) != sidecar.get("instrumentation"):
        raise ValueError("Target instrumentation changed after preparation")


def read_ids(path):
    ids = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()
           if line.strip() and not line.lstrip().startswith("#")]
    if not ids or any(not valid_arxiv_id(pid) for pid in ids) or len(set(ids)) != len(ids):
        raise ValueError("IDs must be nonempty, unique, valid base arXiv IDs")
    return ids


def prepare_run(repo, ids_file, output_root, variant, *, python=sys.executable,
                template=None, workers=2, batch_size=None, mode="cold", warm_from=None,
                allow_large=False, sample_interval=0.2):
    """Freeze one ID list/config and verify warm seeds belong to this variant."""
    repo, output_root = Path(repo).resolve(), Path(output_root).resolve()
    ids = read_ids(ids_file)
    if len(ids) > 40 and not allow_large:
        raise ValueError("More than 40 papers requires explicit --allow-large; run smoke and pilot first")
    if variant not in CONFIG_NAMES or mode not in {"cold", "warm"}:
        raise ValueError("Unknown variant or cache mode")
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    batch_size = (20 if variant == "A" else 1) if batch_size is None else batch_size
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be in [1, 100]")
    if variant == "B0" and batch_size != 1:
        raise ValueError("B0 does not support batch discovery")
    if not isinstance(sample_interval, (int, float)) or isinstance(sample_interval, bool) or not 0 < sample_interval < float("inf"):
        raise ValueError("sample_interval must be finite and positive")
    main_source = (repo / "src/main.py").read_text(encoding="utf-8")
    if variant == "A" and "A_BATCH" not in main_source:
        raise ValueError("A requires the feature/a-batch-lanes checkout")
    if variant == "B" and "run_papers" not in main_source:
        raise ValueError("B requires the huong/feat/parallel-papers checkout")
    if variant == "B0" and ("run_papers" in main_source or "A_BATCH" in main_source):
        raise ValueError("B0 requires the sequential baseline checkout")
    cfg = dict(DEFAULTS)
    if template:
        loaded = yaml.safe_load(Path(template).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Config template must contain a mapping")
        # Only shared protocol settings are imported: IDs and algorithm controls
        # always come from the explicit common selection and CLI.
        for key in DEFAULTS:
            if key in loaded:
                cfg[key] = loaded[key]
    for key in ("timeout", "arxiv_delay_seconds", "backoff_base_seconds", "s2_backoff_base_seconds"):
        value = cfg[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value < float("inf"):
            raise ValueError(f"Invalid {key}")
    if cfg["timeout"] <= 0:
        raise ValueError("timeout must be positive")
    for key in ("max_retries", "s2_max_retries"):
        if type(cfg[key]) is not int or cfg[key] < 0:
            raise ValueError(f"Invalid {key}")
    if cfg["arxiv_delay_seconds"] < 3:
        raise ValueError("Benchmark arXiv pacing must be at least 3 seconds")
    revision = revision_info(repo)
    environment = capture_environment(python, repo)
    instrumentation = inspect_instrumentation(python, repo, cfg["timeout"])
    blockers = []
    if not instrumentation.get("per_request_hook_scope"):
        blockers.append("Target shared HTTP/hook code lacks per-request identity scope; apply the reviewed common fix first")
    if instrumentation.get("arxiv_client_timeout_mode") != "configured":
        blockers.append("arxiv.Client GET timeout is absent or unverified; establish and verify the shared timeout policy")
    if revision.get("dirty") is not False:
        blockers.append("Target Git tree is dirty or cleanliness is unknown")
    credentials = bool(os.getenv("S2_API_KEY"))
    policy = {key: cfg[key] for key in POLICY_KEYS if key in cfg}
    policy.update(s2_min_interval_seconds=1.0 if credentials else 3.0,
                  s2_credentials_present=credentials)
    seed = None
    if mode == "warm":
        if warm_from is None:
            raise ValueError("Warm mode requires --warm-from from this variant's own completed cold run")
        seed = read_object(Path(warm_from) / "benchmark.json", required=True)
        if (seed.get("variant") != variant or seed.get("cache_mode") != "cold"
                or seed.get("run_status") != "completed" or seed.get("input_ids") != ids
                or seed.get("revision") != revision or seed.get("policy") != policy
                or seed.get("environment") != environment
                or seed.get("measurement_protocol") != PROTOCOL):
            raise ValueError("Warm seed must be a completed cold run with the same variant/revision/IDs/environment/policy")
        seed_cfg = seed.get("config", {})
        if (seed_cfg.get("max_workers") != (workers if variant == "B" else 1)
                or seed_cfg.get("discovery_batch_size") != batch_size):
            raise ValueError("Warm seed algorithm controls differ")
        source = summarize_run(warm_from, variant)
        if source["papers"]["complete_paper_count"] != len(ids):
            raise ValueError("Warm seed must have a fully complete, validated corpus")
    elif warm_from is not None:
        raise ValueError("--warm-from is only valid with --mode warm")
    run_id = f"{variant}_{mode}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:10]}"
    run = output_root / run_id
    run.mkdir(parents=True, exist_ok=False)
    frozen_ids = run / "input_ids.txt"
    frozen_ids.write_text("\n".join(ids) + "\n", encoding="utf-8")
    cfg.update(run_id=run_id, config_name=CONFIG_NAMES[variant], ids_file=str(frozen_ids),
               output_root=str(output_root), max_workers=workers if variant == "B" else 1,
               discovery_batch_size=batch_size)
    if seed:
        shutil.copytree(Path(warm_from) / "papers", run / "papers")
        # Existing pipeline detects warm executions through run.json. Keep the
        # new run ID and new logs; never append a new execution to the old run.
        metrics.write_run_report(run / "run.json", {"run_id": run_id, "cache_mode": "warm"})
    config_path = run / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    sidecar = {"run_id": run_id, "variant": variant, "input_ids": ids, "cache_mode": mode,
               "run_status": "prepared", "revision": revision, "environment": environment,
               "policy": policy, "config": cfg, "measurement_protocol": PROTOCOL,
               "instrumentation": instrumentation,
               "pilot_preflight": {"ready_for_local_pilot": not blockers, "blockers": blockers,
                                   "input_paper_count": len(ids),
                                   "colab_cpu_only_verified": None,
                                   "notes": ["HTTP totals remain unknown: arxiv.Client attempts and redirect hops are uninstrumented",
                                             "Configured timeout covers shared helpers; inspect arxiv_client_timeout_mode separately",
                                             "Colab accelerator selection and fresh Colab smoke require verification in that runtime"]},
               "sample_interval_s": sample_interval,
               "warm_seed": {"run_id": seed["run_id"], "commit": seed["revision"]["commit"]} if seed else None}
    metrics.write_run_report(run / "benchmark.json", sidecar)
    return run, sidecar


def execute_run(repo, run, sidecar, *, python=sys.executable):
    """Measure subprocess wall time separately from sums of overlapping stages."""
    run, repo = Path(run), Path(repo)
    verify_prepared_run(repo, run, sidecar, python)
    if len(sidecar["input_ids"]) >= 40 and not sidecar.get("pilot_preflight", {}).get("ready_for_local_pilot"):
        raise ValueError("Pilot preflight blocked: " + "; ".join(sidecar.get("pilot_preflight", {}).get("blockers", [])))
    sidecar.update(run_status="running", started_at=datetime.now(timezone.utc).isoformat())
    metrics.write_run_report(run / "benchmark.json", sidecar)
    samples, sample_errors = [], []
    process, interrupted = None, False
    started = perf_counter()
    try:
        with (run / "console.log").open("w", encoding="utf-8") as console:
            process = subprocess.Popen([str(python), "-u", "-m", "src.main", str(run / "config.yaml")],
                                       cwd=repo, stdout=console, stderr=subprocess.STDOUT)
            sampler = metrics.ResourceSampler(run, process.pid)
            while process.poll() is None:
                try:
                    samples.append(sampler.sample())
                except psutil.NoSuchProcess:
                    pass  # Child can exit between poll() and RSS collection.
                except (OSError, psutil.Error) as exc:
                    sample_errors.append(type(exc).__name__)
                try:
                    process.wait(timeout=sidecar["sample_interval_s"])
                except subprocess.TimeoutExpired:
                    pass
            returncode = process.wait()
    except BaseException:
        interrupted = True
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    finally:
        elapsed = perf_counter() - started
        resources = metrics.summarize_rss_samples((s["elapsed_s"], s["rss_bytes"]) for s in samples)
        resources["disk_peak_bytes"] = max([s["disk_bytes"] for s in samples]
                                            + [metrics.directory_size_bytes(run)])
        resources["sample_errors"] = sample_errors
        resources["output_storage_bytes"] = metrics.directory_size_bytes(run / "papers") if (run / "papers").is_dir() else None
        try:
            record = read_object(run / "run.json") or {}
        except ValueError as exc:
            record = {"run_status": "crashed"}
            sidecar["pipeline_record_error"] = str(exc)
        sidecar.update(finished_at=datetime.now(timezone.utc).isoformat(), wall_seconds=elapsed,
                       returncode=process.returncode if process else None,
                       run_status="interrupted" if interrupted else record.get("run_status", "crashed"), resources=resources)
        if not interrupted and process is not None and process.returncode != 0:
            sidecar["run_status"] = "crashed"
        metrics.write_run_report(run / "benchmark.json", sidecar)
    return returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--variant", choices=CONFIG_NAMES, required=True)
    parser.add_argument("--ids", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--mode", choices=("cold", "warm"), default="cold")
    parser.add_argument("--warm-from", type=Path)
    parser.add_argument("--sample-interval", type=float, default=0.2)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--allow-large", action="store_true")
    args = parser.parse_args()
    try:
        run, sidecar = prepare_run(args.repo, args.ids, args.output_root, args.variant,
            python=args.python, template=args.template, workers=args.workers, batch_size=args.batch_size,
            mode=args.mode, warm_from=args.warm_from, allow_large=args.allow_large,
            sample_interval=args.sample_interval)
        print(f"Run directory: {run}", flush=True)
        if args.prepare_only:
            print(json.dumps(sidecar["pilot_preflight"], ensure_ascii=False))
            print("Prepared only; no API calls issued.")
            return 0
        result = execute_run(args.repo, run, sidecar, python=args.python)
        return result or (0 if sidecar["run_status"] == "completed" else 2)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Benchmark failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
