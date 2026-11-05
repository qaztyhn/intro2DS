# Pilot transport fix and Colab handoff — 2026-10-29

## Git state and scope

PR [#22](https://github.com/qaztyhn/intro2DS/pull/22) merged Metrics as
`1b2409d`. Its revert is `29b8aa3`; PR
[#23](https://github.com/qaztyhn/intro2DS/pull/23) subsequently merged that revert
into `main` at **`a69dd4210468d8ba2441d9469a168699abaf928e`**. After fetching and
the user's updated instruction, `fix/pilot-http-transport` was created from
that latest `origin/main`. The reverted Metrics framework was not restored into
this branch. Its tools/documents are still available at Metrics revision
`1bb87ed46d3a93f5bc3eee07d619f300f274f96f`.

The request-identity fix is committed as `e966ae5`; the transport timeout and
pilot dependency lock are committed as `3c130a3` on
`fix/pilot-http-transport`. No pushes, merges, resets or force-pushes were made.
The five existing untracked user files were preserved, with SHA-256 checks.
Private integration branches/worktrees are:

| Variant | Trial branch | Base revision | Worktree |
|---|---|---|---|
| B0 | `test/pilot-http-b0` | `a69dd4210468d8ba2441d9469a168699abaf928e` | `/tmp/intro2ds-pilot-validation/b0` |
| A | `test/pilot-http-a` | `0c7ffe0b5a40073d8c5f5d89d113adbf0f4f64b3` | `/tmp/intro2ds-pilot-validation/a` |
| B | `test/pilot-http-b` | `dc8239237cad2d086631bfa2b024df05178f8d59` | `/tmp/intro2ds-pilot-validation/b` |

Metrics controller: separate worktree `/tmp/intro2ds-pilot-validation/metrics`
at `1bb87ed`, with only the scoped diagnostic-probe change described below.
That patch is committed separately as `4fd663f` on
`fix/metrics-preflight-adapter`; its clean worktree is
`/tmp/intro2ds-pilot-validation/clean-metrics`.
Teammates' branch refs were not moved. `src/main.py`, `src/config.py` and
`src/stages/references.py` remain byte-identical to each target's base; discovery
changes are one import and one timeout-binding call in `make_clients`.

## Confirmed defects and implementation

**Request identity:** the existing Metrics request-scope patch was applied
unchanged to `src/metrics.py` and `src/utils/http.py`. A hook has a `for_request`
factory; a shared GET/download helper binds one child before its retry loop.
Every page/logical invocation gets a new ID, retries keep that ID, and the parent
is retained as `request_group_id`. Direct callbacks retain their existing
contract. Callback failures cannot cause extra HTTP requests. References
pagination and normalization were not modified.

**Timeout:** arxiv 4.0.2 constructs a Requests Session and calls its `get`
without a timeout. Previously YAML timeout reached project HTTP helpers but not
the SDK's transport. Twelve initial regression cases failed, including the
adapter observing `timeout=None` rather than 7.5 seconds.

`src/utils/arxiv_transport.py` now supplies a `TimeoutSession` **per client**.
Its `send` method replaces an absent/None timeout with the validated YAML scalar
before delegating to Requests. Explicit non-None request timeouts are preserved;
redirects retain the effective setting. `make_clients` binds it to the new
arxiv client's private `_session`, closes the unused original Session and fails
clearly if the SDK exposes an unexpected transport. This uses the inspected 4.x
private interface and should be regression-tested when upgrading arxiv.

Requests classes and other Session instances are not modified. SDK retry count,
backoff/pacing, page size, discovery flow, A's batch lanes, B's worker count,
batch setting and locks retain their existing behavior. In particular, arxiv
retries `ConnectionError` (including `ConnectTimeout`); a `ReadTimeout` still
escapes immediately. The fix deliberately preserves that distinction.

A scalar Requests timeout bounds connect and individual read waits. It is **not**
a deadline for the full response stream, retry sequence, paper or pipeline.
Slow transfers may exceed the scalar while continuing to deliver data.

**Preflight diagnostic:** the experimental Metrics controller now mocks only
the new client's adapters during its no-network probe. It observes timeout at
the real adapter boundary, after the per-client Session supplies the default;
it no longer replaces `requests.Session.send` globally, even in the probe child.

## Files for review

| File | Purpose |
|---|---|
| `src/metrics.py`, `src/utils/http.py` | Reuse the existing request-identity fix |
| `src/utils/arxiv_transport.py` | Per-client timeout transport and compatibility guard |
| `src/discovery/arxiv_api.py` | Bind configured timeout in the existing client factory |
| `configs/pilot-dependencies.txt` | Exact dependency versions tested for the pilot, including arxiv 4.0.2 |
| `tests/test_request_scope.py` | Reused Metrics pagination/retry/404/callback/concurrency regressions |
| `tests/test_arxiv_transport.py` | YAML-to-adapter timeout, SDK retry semantics, independent clients, explicit settings, redirects and invalid values |
| `tests/test_pilot_pipeline_transport.py` | Actual variant driver with mocked adapters: two papers, pagination, 429 retry and retained S2 404 failure |
| `docs/patches/pilot-http-transport.patch` | Combined production-only patch for B0/A/B, including the new module |
| `docs/patches/metrics-preflight-adapter.patch` | Small patch to the separate Metrics controller's probe |
| `docs/pilot-http-transport.md` | Findings, local validation and CPU-only Colab cells |

The common production patch passed `git apply --check` against clean snapshots
of all three bases. It was cherry-picked into **private verification branches**,
while the original A/B branch refs were left unchanged. The controller patch
passed its check against Metrics `1bb87ed` and was committed only to a separate
controller branch. The pilot dependency lock must be installed with `-c` for
each variant; all three clean trial trees contain identical lock bytes.

## Actual validation results

Environment: Python **3.11.16**, arxiv **4.0.2**, requests **2.34.2**, pytest
**9.1.1**, installed from the original requirements in
`/tmp/intro2ds-pilot-venv`. The old `.venv` interpreter was unavailable and was
left intact. The 18 installed dependency versions are recorded exactly in
`configs/pilot-dependencies.txt`; install each variant with that constraint file
for the pilot. All new integration requests use mocked adapters, never live APIs.

| Full pytest suite | Passed | Subtests passed |
|---|---:|---:|
| Root fix branch, including six tests in the user's existing untracked file | 678 | 121 |
| B0 private integration worktree | 672 | 121 |
| A private integration worktree | 677 | 121 |
| B private integration worktree | 693 | 121 |
| Metrics controller with scoped probe patch | 734 | 121 |

Commands actually run (replace `REPO` with each worktree above):

```bash
cd REPO
/tmp/intro2ds-pilot-venv/bin/python -m pytest -q --tb=short

# Also run individually on B0/A/B:
/tmp/intro2ds-pilot-venv/bin/python -m pytest tests/test_pilot_pipeline_transport.py -q --tb=short
```

Before the final driver integration was added, the targeted transport/request/
HTTP/reference/discovery/download tests also passed: **81 tests, 121 subtests**.
The driver integration passes on all three variants. It observes actual library
GETs at mocked adapters, confirms the configured timeout on each, and verifies
five instrumented logical requests / six attempts / one retry while additional
SDK GETs remain uninstrumented. The 404 paper stays in `input_ids`, has
`references.status=failed`, `error_code=NOT_FOUND`, and failed validation.

## Clean-checkout 40-ID preflight

Using the real Metrics runner from the clean controller checkout, the common
`configs/pilot_ids.txt`, and the baseline pilot request policy, preparation was
performed for each clean private target. The probe uses no network. No crawl
was launched.

| Check | B0 | A | B |
|---|---|---|---|
| Common unique paper IDs | 40 | 40 | 40 |
| Per-request identity scope | pass | pass | pass |
| Effective SDK transport timeout | 30 s | 30 s | 30 s |
| Paper workers / discovery batch size | 1 / 1 | 1 / 20 | 2 / 1 |
| Full local preflight eligibility | pass | pass | pass |

The initial pre-commit trial worktrees were correctly blocked for dirty trees;
the committed verification worktrees have `dirty=false` and no blockers. This
is a local preflight result, not a live crawl or Colab CPU verification. The
runner's launch guard remains enabled and unchanged.

Initial blocked evidence is retained in Git-ignored
`experiments/pilot-transport-final-preflight-20261029/preflight.json`.
Clean preparation evidence is outside Git at
`/tmp/intro2ds-pilot-validation/clean-preflight/summary.json`, with frozen
per-run configs and Git provenance in adjacent run directories.
The algorithm hash comparison is retained under
`/tmp/intro2ds-pilot-validation/integration-algorithm-hashes.json`.

**Remaining prerequisites:** review/publish the clean fixed B0/A/B revisions
and controller probe revision, verify Colab accelerator None, and complete a
fresh two-paper smoke there. Local unit/integration
success is not a Colab performance result. No real pilot or 1,000-paper run
was performed. Raw internal SDK attempts and HTTP redirect hops are still not
instrumented, so total HTTP requests/retries/429 remain **unknown/null**. A
valid zero in shared callbacks is only an observed zero, not a total zero.

## Colab CPU-only cells

Select **Runtime → Change runtime type → Hardware accelerator: None**. Use
local `/content` disk for all timed runs. Fill in reviewed, published **fixed
commit SHAs** below after owners approve the changes. Current old SHAs or dirty
local patches are not eligible; these cells do not apply patches, commit, push,
merge or disable preflight. The Metrics SHA must include the scoped probe patch.

Cell 1 — approved revisions, private-repository authentication and checkouts:

```python
import getpass, os, re, subprocess, sys
from pathlib import Path

METRICS_SHA = ""  # approved Metrics controller commit with scoped adapter probe
TARGET_SHA = {"B0": "", "A": "", "B": ""}  # fixed, published variant commits
assert all(re.fullmatch(r"[0-9a-f]{40}", s) for s in [METRICS_SHA, *TARGET_SHA.values()]), "Fill in reviewed fixed SHAs first"
assert sys.version_info >= (3, 11)
CTRL = Path("/content/intro2ds-controller")
CHECKOUTS = Path("/content/intro2ds-targets")
RESULTS = Path("/content/intro2ds-results")
assert not CTRL.exists(), "Use a fresh Colab runtime"
CHECKOUTS.mkdir(exist_ok=True)
RESULTS.mkdir(exist_ok=True)

# A read token is only passed through an ephemeral child environment, never a URL.
token = getpass.getpass("GitHub read token for this private repo: ")
assert token, "Provide a read token for the private repository"
git_env = os.environ.copy()
git_env["GIT_TERMINAL_PROMPT"] = "0"
askpass = Path("/content/intro2ds-git-askpass.py")
if token:
    askpass.write_text("#!/usr/bin/env python3\nimport os,sys\nprint('x-access-token' if 'username' in sys.argv[1].lower() else os.environ['INTRO2DS_READ_TOKEN'])\n")
    askpass.chmod(0o700)
    git_env.update(GIT_ASKPASS=str(askpass), INTRO2DS_READ_TOKEN=token)
try:
    def git(*args):
        subprocess.run(["git", "-c", "credential.helper=", *map(str, args)], env=git_env, check=True)
    git("clone", "--branch", "feature/metrics-comparison", "https://github.com/qaztyhn/intro2DS.git", CTRL)
    git("-C", CTRL, "fetch", "origin", METRICS_SHA)
    git("-C", CTRL, "switch", "--detach", METRICS_SHA)
    for variant, sha in TARGET_SHA.items():
        git("-C", CTRL, "fetch", "origin", sha)
        git("-C", CTRL, "worktree", "add", "--detach", CHECKOUTS / variant, sha)
finally:
    git_env.pop("INTRO2DS_READ_TOKEN", None)
    git_env.pop("GIT_ASKPASS", None)
    askpass.unlink(missing_ok=True)
    del token
```

Cell 2 — one dependency environment, CPU confirmation and common S2 credentials:

```python
import hashlib, inspect, json, shutil
CPU_ONLY_CONFIRMED = False  # set True only after checking accelerator None in Colab
assert CPU_ONLY_CONFIRMED, "Verify Colab accelerator None first"
if shutil.which("nvidia-smi"):
    gpu = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True)
    assert gpu.returncode != 0 or not gpu.stdout.strip(), "GPU runtime detected; choose accelerator None"
locks = [(CHECKOUTS / v / "configs/pilot-dependencies.txt").read_bytes() for v in ("B0", "A", "B")]
requirements = [(CHECKOUTS / v / "requirements.txt").read_bytes() for v in ("B0", "A", "B")]
assert len(set(locks)) == len(set(requirements)) == 1, "Variants must use identical dependencies"
subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(CHECKOUTS / "B0/requirements.txt"),
                "-c", str(CHECKOUTS / "B0/configs/pilot-dependencies.txt")], check=True)
from importlib import metadata
for row in locks[0].decode().splitlines():
    if row.strip() and not row.startswith("#"):
        name, expected = row.split("==", 1)
        assert metadata.version(name) == expected, (name, expected)
assert metadata.version("arxiv") == "4.0.2"
with (RESULTS / "pip-freeze.txt").open("w") as handle:
    subprocess.run([sys.executable, "-m", "pip", "freeze"], stdout=handle, check=True)
key = getpass.getpass("S2 API key, reused for every variant (blank for unauthenticated): ")
if key:
    os.environ["S2_API_KEY"] = key
else:
    os.environ.pop("S2_API_KEY", None)
del key
sys.path.insert(0, str(CTRL))
from src.tools.benchmark_run import prepare_run
from src.tools.compare_runs import compare_runs, write_comparison
import src.tools.benchmark_run as runner
assert "requests.Session.send =" not in inspect.getsource(runner.inspect_instrumentation), "Use the reviewed scoped-probe controller"
(RESULTS / "approved-revisions.json").write_text(json.dumps({"controller": METRICS_SHA, "targets": TARGET_SHA, "cpu_only_confirmed": True}, indent=2))
```

Cell 3 — tests and 40-ID preparation guard, without a live crawl:

```python
for variant in ("B0", "A", "B"):
    with (RESULTS / f"pytest-{variant}.log").open("w") as handle:
        subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short"],
                       cwd=CHECKOUTS / variant, stdout=handle, stderr=subprocess.STDOUT, check=True)
    _, prepared = prepare_run(CHECKOUTS / variant, CTRL / "configs/pilot_ids.txt", RESULTS,
                             variant, python=sys.executable, template=CTRL / "configs/baseline_pilot.yaml")
    assert len(prepared["input_ids"]) == 40
    assert prepared["pilot_preflight"]["ready_for_local_pilot"], prepared["pilot_preflight"]["blockers"]
    assert prepared["instrumentation"]["arxiv_client_timeout_seconds"] == 30
    print(variant, prepared["revision"]["commit"], "preflight passed")
```

Cell 4 — sequential, fresh cold smoke runs on the same two IDs:

```python
def run_one(variant, ids, template):
    command = [sys.executable, "-m", "src.tools.benchmark_run",
               "--repo", str(CHECKOUTS / variant), "--variant", variant,
               "--ids", str(CTRL / ids), "--template", str(CTRL / template),
               "--output-root", str(RESULTS), "--mode", "cold",
               "--workers", "2", "--batch-size", "20" if variant == "A" else "1"]
    result = subprocess.run(command, cwd=CTRL, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode:
        print(result.stderr)
    result.check_returncode()
    return Path(next(line.removeprefix("Run directory: ") for line in result.stdout.splitlines()
                     if line.startswith("Run directory: ")))

SMOKE_PASSED = False
smoke = {v: run_one(v, "configs/metrics_smoke_ids.txt", "configs/metrics_smoke.yaml") for v in ("B0", "A", "B")}
smoke_report = compare_runs(smoke["B0"], smoke["A"], smoke["B"])
write_comparison(smoke_report, RESULTS / "smoke-report")
assert smoke_report["benchmark_valid"], smoke_report["issues"]
assert all(r["papers"]["paper_completion_rate"] == 1 for r in smoke_report["runs"].values())
assert all(not value["changed_ids"] and not value["unobserved_ids"]
           for comparison in smoke_report["comparisons"].values() for value in comparison["content"].values())
SMOKE_PASSED = True
```

Cell 5 — enable the 40-paper pilot only after reviewing smoke results:

```python
RUN_PILOT = False  # set True only after all smoke/preflight checks pass
if RUN_PILOT:
    assert globals().get("SMOKE_PASSED", False), "Run and verify the fresh Colab smoke first"
    pilot = {v: run_one(v, "configs/pilot_ids.txt", "configs/baseline_pilot.yaml") for v in ("B0", "A", "B")}
    pilot_report = compare_runs(pilot["B0"], pilot["A"], pilot["B"])
    write_comparison(pilot_report, RESULTS / "pilot-report")
    for variant, run in pilot_report["runs"].items():
        print(variant, run["papers"], run["reliability"], run["requests"])
else:
    print("Pilot not enabled; smoke artifacts retained.")
```

The pilot retains HTTP 404 papers as failures; do not filter/reselect them or
interpret failed/unknown references as an empty successful response. A run may
be `completed` while some papers fail. Interpret wall time together with quality;
stage durations can overlap and are not substitutes for total wall time.

Cell 6 — retain artifacts after timing, outside Git:

```python
archive = shutil.make_archive("/content/intro2ds-results", "gztar", root_dir=RESULTS)
print("Saved artifacts:", archive)
# Optional manual download:
# from google.colab import files
# files.download(archive)
```

Each measured invocation gets its own ID, frozen IDs/config, code revision,
environment and logs. No warm/cache seed is shared. Keep dependencies, S2 key
and runtime unchanged between variants, and copy to Drive only after timing.
These cells contain no full-benchmark invocation and do not claim that a local
test or configuration-only preflight has already verified Colab execution.
