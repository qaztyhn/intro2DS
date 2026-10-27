"""D1 contract and metrics tests; the real HTTP helper uses mocked responses."""

import json
from types import SimpleNamespace
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest
import requests

from src import main, metrics
from src.utils.common import EventLogger, load_ids, paper_key, read_json, write_json_atomic
from src.config import PROJECT_ROOT, http_options, load_config
from src.discovery import arxiv_api, oai_pmh as d1
from src.stages.storage import load_metadata, save_metadata
from src.stages.validation import validate_metadata
from src.tools.benchmark import select_benchmark, select_pilot
from src.utils import http
from src.utils.errors import CrawlError

ID = "2507.01659"
OLD_ID = "hep-th/9901001"


def response_xml(identity=ID, versions=(1, 2, 3)):
    """Synthetic arXivRaw record, never metadata from a real paper."""
    history = "".join(
        f'<version version="v{v}"><date>2025-07-{v:02d}T00:00:00Z</date></version>'
        for v in versions
    )
    return (
        f'<OAI-PMH xmlns="{d1.OAI_NS}"><GetRecord><record><header>'
        f'<identifier>oai:arXiv.org:{identity}</identifier>'
        '<datestamp>2026-10-01</datestamp></header><metadata>'
        f'<arXivRaw xmlns="{d1.RAW_NS}"><id>{identity}</id>{history}'
        '<title> A synthetic\n paper </title><authors>Alice, Bob and Carol</authors>'
        '<journal-ref>Synthetic Journal 1 (2025)</journal-ref>'
        '</arXivRaw></metadata></record></GetRecord></OAI-PMH>'
    )


def raw_node(root):
    return root.find(f".//{{{d1.RAW_NS}}}arXivRaw")


def changed_xml(change):
    root = ET.fromstring(response_xml())
    change(root)
    return ET.tostring(root, encoding="unicode")


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = dict(output_root=tmp_path, run_id="offline-d1", config_name="D1",
               timeout=17, max_retries=2, backoff_base_seconds=0.25,
               arxiv_delay_seconds=3, user_agent="Intro2DS-offline-test")
    run = tmp_path / cfg["run_id"]
    logger = EventLogger(run / "events.jsonl", cfg["run_id"])
    metrics_logger = metrics.EventLogger(run / "metrics_events.jsonl", cfg["run_id"])
    throttle = Mock(spec=http.Throttle)
    opts = http_options(cfg, throttle)

    def respond(**kwargs):
        identity = kwargs["params"]["identifier"].removeprefix("oai:arXiv.org:")
        return SimpleNamespace(status_code=200, text=response_xml(identity), headers={})

    get = Mock(side_effect=respond)
    sleep = Mock()
    monkeypatch.setattr(http.requests, "get", get)
    monkeypatch.setattr(http.time, "sleep", sleep)
    return SimpleNamespace(cfg=cfg, run=run, logger=logger, metrics_logger=metrics_logger,
                           throttle=throttle, opts=opts, get=get, sleep=sleep)


def discover(env, identity=ID, with_metrics=True):
    return d1.discover_paper(env.cfg, identity, None, env.opts, env.throttle, env.logger,
                             env.metrics_logger if with_metrics else None)


def events(env):
    return metrics.read_events(env.metrics_logger.path)


def metadata_path(env, identity=ID):
    return env.run / "papers" / paper_key(identity) / "metadata.json"


@pytest.mark.parametrize("identity", [ID, OLD_ID])
@pytest.mark.parametrize("versions", [(1,), (1, 2), (3, 1, 2)])
def test_parse_versions_dates_and_baseline_schema(identity, versions):
    record = d1.parse_record(response_xml(identity, versions), identity)
    expected = list(range(1, max(versions) + 1))
    assert record["arxiv_id"] == identity
    assert record["title"] == "A synthetic paper"
    assert record["authors"] == ["Alice", "Bob", "Carol"]
    assert record["submission_date"] == "2025-07-01T00:00:00+00:00"
    assert record["revised_dates"] == [f"2025-07-{v:02d}T00:00:00+00:00" for v in expected[1:]]
    assert record["versions"] == expected
    assert all(type(v) is int for v in record["versions"])
    assert record["latest_version"] == max(versions)
    assert record["venue"] == "Synthetic Journal 1 (2025)"
    assert record["status"] == "ok" and record["error_code"] is None
    assert set(arxiv_api.make_record(identity)) <= record.keys()
    assert validate_metadata(record, arxiv_id=identity) == []


@pytest.mark.parametrize("text, expected", [
    ("Alice", ["Alice"]),
    ("Alice and Bob", ["Alice", "Bob"]),
    ("Alice, Bob, and Carol", ["Alice", "Bob", "Carol"]),
    ("Alice (Inst, City), Bob and Carol", ["Alice (Inst, City)", "Bob", "Carol"]),
])
def test_authors_keep_raw_text_and_grouped_affiliations(text, expected):
    xml = changed_xml(lambda r: setattr(raw_node(r).find(f"{{{d1.RAW_NS}}}authors"), "text", text))
    record = d1.parse_record(xml, ID)
    assert record["authors"] == expected
    assert record["authors_raw"] == text


@pytest.mark.parametrize("date, expected", [
    ("Tue, 1 Jul 2025 01:00:00 +0100", "2025-07-01T00:00:00+00:00"),
    ("Tue, 1 Jul 2025 00:00:00 GMT", "2025-07-01T00:00:00+00:00"),
    ("2025-07-01", "2025-07-01T00:00:00+00:00"),
])
def test_raw_dates_normalized_to_utc(date, expected):
    xml = changed_xml(lambda r: setattr(raw_node(r).find(f"{{{d1.RAW_NS}}}version/{{{d1.RAW_NS}}}date"), "text", date))
    assert d1.parse_record(xml, ID)["submission_date"] == expected


def test_missing_venue_is_null_and_missing_revision_date_is_not_fabricated(env):
    def change(root):
        raw = raw_node(root)
        raw.remove(raw.find(f"{{{d1.RAW_NS}}}journal-ref"))
        version = raw.findall(f"{{{d1.RAW_NS}}}version")[1]
        version.remove(version.find(f"{{{d1.RAW_NS}}}date"))
    xml = changed_xml(change)
    env.get.side_effect = None
    env.get.return_value = SimpleNamespace(status_code=200, text=xml, headers={})
    assert discover(env) == "ok"
    record = read_json(metadata_path(env))
    assert record["venue"] is None
    assert record["revised_dates"] is None
    assert record["version_history"][1]["submitted_at"] is None
    assert record["versions"] == [1, 2, 3]
    assert record["error_code"] is None  # same partial-success convention as B
    assert discover(env) == "ok"  # incomplete dates are retried, not skipped
    assert env.get.call_count == 2


@pytest.mark.parametrize("versions", [(), (2,), (1, 3), (1, 1)])
def test_reject_missing_gapped_or_duplicate_version_history(versions):
    with pytest.raises(CrawlError) as exc:
        d1.parse_record(response_xml(versions=versions), ID)
    assert exc.value.error_code == "INVALID_RESPONSE"


@pytest.mark.parametrize("label", ["1", "v0", "v-1", "v1x", ""])
def test_reject_invalid_version_labels(label):
    xml = changed_xml(lambda r: raw_node(r).find(f"{{{d1.RAW_NS}}}version").set("version", label))
    with pytest.raises(CrawlError, match="version label"):
        d1.parse_record(xml, ID)


@pytest.mark.parametrize("field", ["title", "authors", "id"])
def test_missing_required_raw_fields_are_not_invented(field):
    xml = changed_xml(lambda r: raw_node(r).remove(raw_node(r).find(f"{{{d1.RAW_NS}}}{field}")))
    with pytest.raises(CrawlError) as exc:
        d1.parse_record(xml, ID)
    assert exc.value.error_code == "INVALID_RESPONSE"


@pytest.mark.parametrize("xml, code", [
    ("<broken", "INVALID_RESPONSE"),
    ("<html>unavailable</html>", "INVALID_RESPONSE"),
    (f'<OAI-PMH xmlns="{d1.OAI_NS}"/>', "INVALID_RESPONSE"),
    (f'<OAI-PMH xmlns="{d1.OAI_NS}"><error code="idDoesNotExist">absent</error></OAI-PMH>', "NOT_FOUND"),
    (f'<OAI-PMH xmlns="{d1.OAI_NS}"><error code="cannotDisseminateFormat">bad prefix</error></OAI-PMH>', "INVALID_RESPONSE"),
    (response_xml("2507.01660"), "INVALID_RESPONSE"),
])
def test_oai_errors_are_persisted_and_measured_as_failed(env, xml, code):
    env.get.side_effect = None
    env.get.return_value = SimpleNamespace(status_code=200, text=xml, headers={})
    assert discover(env) == "failed"
    record = read_json(metadata_path(env))
    assert record["status"] == "failed" and record["error_code"] == code
    assert record["title"] is None and record["versions"] is None
    finished = [e for e in events(env) if e["event"] == "finished"]
    assert len(finished) == 1 and finished[0]["status"] == "failed"
    # A successful HTTP transfer can still contain an invalid OAI document.
    attempt = [e for e in events(env) if e["event"] == "request_attempt_finished"][0]
    assert attempt["http_status"] == 200 and attempt["error_code"] is None


@pytest.mark.parametrize("change, code", [
    (lambda r: r.find(f".//{{{d1.OAI_NS}}}header").set("status", "deleted"), "NOT_FOUND"),
    (lambda r: r.find(f".//{{{d1.OAI_NS}}}record").remove(r.find(f".//{{{d1.OAI_NS}}}header")), "INVALID_RESPONSE"),
    (lambda r: setattr(raw_node(r).find(f"{{{d1.RAW_NS}}}id"), "text", "2507.01660"), "INVALID_RESPONSE"),
    (lambda r: setattr(raw_node(r).find(f"{{{d1.RAW_NS}}}version/{{{d1.RAW_NS}}}date"), "text", "not a date"), "INVALID_RESPONSE"),
    (lambda r: raw_node(r).find(f"{{{d1.RAW_NS}}}version").remove(raw_node(r).find(f"{{{d1.RAW_NS}}}version/{{{d1.RAW_NS}}}date")), "INVALID_RESPONSE"),
])
def test_deleted_mismatched_and_malformed_records(change, code):
    with pytest.raises(CrawlError) as exc:
        d1.parse_record(changed_xml(change), ID)
    assert exc.value.error_code == code


def test_shared_http_settings_success_metrics_storage_and_resume(env):
    original_opts = dict(env.opts)
    assert discover(env) == "ok"
    env.get.assert_called_once_with(
        url=d1.OAI_URL,
        params={"verb": "GetRecord", "identifier": f"oai:arXiv.org:{ID}", "metadataPrefix": "arXivRaw"},
        headers={"User-Agent": env.cfg["user_agent"]}, timeout=17,
    )
    env.throttle.wait.assert_called_once_with()
    env.throttle.mark.assert_not_called()  # the shared helper owns throttling
    assert env.opts == original_opts
    record = read_json(metadata_path(env))
    save_metadata(env.cfg, ID, record)
    assert load_metadata(env.cfg, ID) == record
    before = metadata_path(env).read_bytes(), metadata_path(env).stat().st_mtime_ns
    assert discover(env) == "skipped"
    assert env.get.call_count == 1
    assert (metadata_path(env).read_bytes(), metadata_path(env).stat().st_mtime_ns) == before
    timing = metrics.summarize_stage_times(events(env), env.cfg["run_id"], "discovery")
    assert timing["started_operation_count"] == timing["finished_operation_count"] == 1
    assert timing["successful_operation_count"] == 1
    attempts = metrics.summarize_request_attempts(events(env), env.cfg["run_id"])
    assert attempts["observed_attempt_count"] == 1
    assert attempts["observed_retry_attempt_count"] == 0
    assert all(e["arxiv_id"] == ID and e["stage"] == "discovery" for e in events(env))


@pytest.mark.parametrize("cached", [
    [], "invalid object", {"status": "ok", "latest_version": 1},
    {"status": "ok", "versions": ["1"], "latest_version": 1},
])
def test_resume_rejects_incomplete_or_wrongly_typed_cache(env, cached):
    write_json_atomic(metadata_path(env), cached)
    assert discover(env) == "ok"
    assert env.get.call_count == 1
    assert validate_metadata(read_json(metadata_path(env)), arxiv_id=ID) == []


@pytest.mark.parametrize("changes", [
    {"arxiv_id": "2507.01660"}, {"versions": [1, 3]}, {"revised_dates": []},
    {"authors": None}, {"authors": [""]}, {"submission_date": "bad"},
    {"status": "failed", "error_code": "TIMEOUT"},
])
def test_resume_retries_invalid_existing_metadata(env, changes):
    record = d1.parse_record(response_xml(), ID)
    write_json_atomic(metadata_path(env), {**record, **changes})
    assert discover(env) == "ok"
    assert env.get.call_count == 1


def test_resume_retries_corrupt_json_and_skips_valid_v1_with_no_venue(env):
    path = metadata_path(env)
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    assert discover(env) == "ok"
    record = d1.parse_record(response_xml(versions=(1,)), ID)
    record["venue"] = None
    write_json_atomic(path, record)
    assert discover(env) == "skipped"
    assert env.get.call_count == 1


@pytest.mark.parametrize("failure, code, status", [
    (429, "HTTP_429", 429), (503, "HTTP_5XX", 503), (404, "NOT_FOUND", 404),
    (requests.Timeout("timeout"), "TIMEOUT", None),
    (requests.ConnectionError("offline"), "NETWORK_ERROR", None),
])
def test_http_failures_use_shared_retry_and_attempt_metrics(env, failure, code, status):
    if isinstance(failure, int):
        env.get.side_effect = None
        env.get.return_value = SimpleNamespace(status_code=failure, headers={})
    else:
        env.get.side_effect = failure
    assert discover(env) == "failed"
    expected_attempts = 1 if failure == 404 else 3
    assert env.get.call_count == env.throttle.wait.call_count == expected_attempts
    assert env.sleep.call_count == expected_attempts - 1
    record = read_json(metadata_path(env))
    assert record["status"] == "failed" and record["error_code"] == code
    attempts = [e for e in events(env) if e["event"] == "request_attempt_finished"]
    assert [e["attempt"] for e in attempts] == list(range(1, expected_attempts + 1))
    assert all(e["error_code"] == code and e["http_status"] == status for e in attempts)
    assert all(e["duration_s"] >= 0 for e in attempts)
    assert len({e["request_id"] for e in attempts}) == 1
    timing = metrics.summarize_stage_times(events(env), env.cfg["run_id"], "discovery")
    assert timing["failed_operation_count"] == 1


def test_rate_limit_retry_after_then_success_and_new_request_id_per_paper(env):
    env.get.side_effect = [
        SimpleNamespace(status_code=429, headers={"Retry-After": "7"}),
        SimpleNamespace(status_code=200, headers={}, text=response_xml()),
        SimpleNamespace(status_code=200, headers={}, text=response_xml(OLD_ID)),
    ]
    assert discover(env) == discover(env, OLD_ID) == "ok"
    env.sleep.assert_called_once_with(7)
    summary = metrics.summarize_request_attempts(events(env), env.cfg["run_id"])
    assert summary["observed_request_count"] == 2
    assert summary["observed_attempt_count"] == 3
    assert summary["observed_retry_attempt_count"] == 1
    assert summary["error_code_counts"] == {"HTTP_429": 1}
    assert summary["requests_with_missing_attempts"] == 0
    timing = metrics.summarize_stage_times(events(env), env.cfg["run_id"], "discovery")
    assert timing["successful_operation_count"] == 2
    assert timing["started_operation_count"] == timing["finished_operation_count"] == 2


def test_exponential_backoff_from_config_and_unexpected_failure(env):
    env.get.side_effect = [requests.Timeout(), requests.Timeout(), RuntimeError("synthetic bug")]
    assert discover(env) == "failed"
    assert [c.args[0] for c in env.sleep.call_args_list] == [0.25, 0.5]
    assert read_json(metadata_path(env))["error_code"] == "INVALID_RESPONSE"
    timing = metrics.summarize_stage_times(events(env), env.cfg["run_id"], "discovery")
    assert timing["failed_operation_count"] == 1


@pytest.mark.parametrize("identity", [ID + "v2", "not-an-id", "2500.12345"])
def test_invalid_id_fails_before_any_request(env, identity):
    assert discover(env, identity) == "failed"
    env.get.assert_not_called()
    assert read_json(metadata_path(env, identity))["error_code"] == "INVALID_ID"
    assert metrics.summarize_stage_times(events(env), env.cfg["run_id"], "discovery")["failed_operation_count"] == 1


def test_optional_metrics_and_existing_callback(env):
    callback = Mock()
    env.opts["on_attempt"] = callback
    assert discover(env, with_metrics=False) == "ok"
    callback.assert_called_once()
    assert not env.metrics_logger.path.exists()


@pytest.mark.parametrize("config_name", [None, "B", "D1"])
def test_main_selects_discovery_and_preserves_baseline_monkeypatch(env, monkeypatch, config_name):
    if config_name is None:
        env.cfg.pop("config_name")
    else:
        env.cfg["config_name"] = config_name
    baseline, oai = Mock(return_value="ok"), Mock(return_value="ok")
    download = Mock(return_value={"ok": 1, "failed": 0, "skipped": 0})
    monkeypatch.setattr(main, "discover_paper", baseline)
    monkeypatch.setattr(main, "d1_discover_paper", oai)
    monkeypatch.setattr(main, "download_paper", download)
    assert main.process_paper(env.cfg, ID, None, env.opts, env.throttle, [], env.logger, env.metrics_logger) == "ok"
    selected, other = (oai, baseline) if config_name == "D1" else (baseline, oai)
    selected.assert_called_once_with(env.cfg, ID, None, env.opts, env.throttle, env.logger, env.metrics_logger)
    other.assert_not_called()
    download.assert_called_once_with(env.cfg, ID, env.opts, env.logger, env.metrics_logger)


@pytest.mark.parametrize("failed", [False, True])
def test_real_d1_in_main_is_measured_once_and_failure_stops_download(env, monkeypatch, failed):
    baseline = Mock(side_effect=AssertionError("D1 must use OAI"))
    download = Mock(return_value={"ok": 3, "failed": 0, "skipped": 0})
    monkeypatch.setattr(main, "discover_paper", baseline)
    monkeypatch.setattr(main, "download_paper", download)
    if failed:
        env.get.side_effect = requests.Timeout()
    status = main.process_paper(env.cfg, ID, None, env.opts, env.throttle, [], env.logger, env.metrics_logger)
    assert status == ("failed" if failed else "ok")
    assert download.call_count == (0 if failed else 1)
    timing = metrics.summarize_stage_times(events(env), env.cfg["run_id"], "discovery")
    assert timing["started_operation_count"] == timing["finished_operation_count"] == 1
    assert timing["failed_operation_count"] == int(failed)


def test_d1_config_uses_shared_40_pilot_ids_and_baseline_http_settings():
    cfg = load_config(PROJECT_ROOT / "configs/d1_pilot.yaml")
    baseline = load_config()
    spec = json.loads((PROJECT_ROOT / "configs/benchmark.json").read_text())
    selection = select_pilot(select_benchmark(spec, spec["month_last_ids"]), spec["pilot_size"], spec["seed"])
    expected = [identity for group in selection.values() for identity in group]
    assert cfg["config_name"] == "D1"
    assert cfg["run_id"].startswith("D1_") and cfg["run_id"] != baseline["run_id"]
    assert cfg["run_id"] == load_config(PROJECT_ROOT / "configs/d1_pilot.yaml")["run_id"]
    assert cfg["output_root"] == PROJECT_ROOT / "experiments"
    assert load_ids(cfg["ids_file"]) == expected and len(expected) == len(set(expected)) == 40
    assert http_options(cfg, None) == http_options(baseline, None)
    assert cfg["arxiv_delay_seconds"] == baseline["arxiv_delay_seconds"] >= 3
    assert "YOUR_EMAIL" not in cfg["user_agent"]
