"""Offline Kaggle discovery tests with small JSONL snapshots and no downloads."""

import json
from unittest.mock import Mock

import pytest

from src.discovery import kaggle
from src.utils.common import FAILED, OK, SKIPPED, read_json, write_json_atomic
from src.utils.errors import INVALID_ID, INVALID_RESPONSE, NOT_FOUND


ARXIV_ID = "2507.01659"
CREATED = "Tue, 01 Jul 2025 10:00:00 GMT"
REVISED = "Wed, 02 Jul 2025 12:00:00 GMT"


@pytest.fixture(autouse=True)
def download(monkeypatch):
    # An unexpected download fails immediately instead of fetching the full dataset.
    mock = Mock(side_effect=AssertionError("Unexpected Kaggle download"))
    monkeypatch.setattr(kaggle.kagglehub, "dataset_download", mock)
    return mock


@pytest.fixture
def raw():
    return {
        "id": ARXIV_ID,
        "title": "  A sample\n paper  ",
        "authors_parsed": [["Doe", "Jane", ""], ["Smith", "John", "Jr."]],
        "journal-ref": "Example Journal",
        "versions": [
            {"version": "v2", "created": REVISED},
            {"version": "v1", "created": CREATED},
        ],
    }


@pytest.fixture
def cfg(tmp_path):
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text(f"# selected papers\n{ARXIV_ID}\n{ARXIV_ID}\n", encoding="utf-8")
    return {
        "output_root": tmp_path / "output",
        "run_id": "kaggle-test",
        "ids_file": ids_file,
        "arxiv_delay_seconds": 3,
        "user_agent": "Kaggle-tests/1.0",
        "timeout": 10,
        "max_retries": 2,
    }


@pytest.mark.parametrize("value,expected", [
    (CREATED, "2025-07-01T10:00:00+00:00"),
    ("Tue, 01 Jul 2025 12:00:00 +0200", "2025-07-01T10:00:00+00:00"),
    ("Tue, 01 Jul 2025 10:00:00", "2025-07-01T10:00:00+00:00"),
    (None, None), (123, None), ("", None), ("  ", None), ("bad date", None),
])
def test_to_iso_date(value, expected):
    assert kaggle.to_iso_date(value) == expected


@pytest.mark.parametrize("line", [
    '{"id": "2507.01659", "title": "sample"}',
    '  { "id" : "2507.01659" }',
    '{"title": "sample", "id": "2507.01659"}',
])
def test_line_id_accepts_different_field_orders(line):
    assert kaggle._line_id(line) == ARXIV_ID


def test_index_filters_ids_and_keeps_first_record(tmp_path, raw):
    snapshot = tmp_path / "snapshot.jsonl"
    records = [dict(raw, id="2507.99999"), raw, dict(raw, title="duplicate")]
    snapshot.write_text("\n\n" + "\n".join(map(json.dumps, records)), encoding="utf-8")
    assert kaggle.load_kaggle_index(snapshot, [ARXIV_ID, "2507.88888"]) == {ARXIV_ID: raw}


def test_index_stops_after_finding_all_ids(tmp_path, raw):
    snapshot = tmp_path / "snapshot.jsonl"
    snapshot.write_text(json.dumps(raw) + "\ninvalid trailing JSON\n", encoding="utf-8")
    assert kaggle.load_kaggle_index(snapshot, [ARXIV_ID, ARXIV_ID]) == {ARXIV_ID: raw}


def test_empty_id_list_does_not_open_snapshot(tmp_path):
    assert kaggle.load_kaggle_index(tmp_path / "missing.jsonl", []) == {}


def test_normalize_record_sorts_versions_and_normalizes_fields(raw):
    record = kaggle.normalize_kaggle_record(raw)
    assert record == {
        "arxiv_id": ARXIV_ID,
        "title": "A sample paper",
        "authors": ["Jane Doe", "John Smith Jr."],
        "submission_date": "2025-07-01T10:00:00+00:00",
        "revised_dates": ["2025-07-02T12:00:00+00:00"],
        "venue": "Example Journal",
        "latest_version": 2,
        "versions": [1, 2],
        "status": OK,
        "error_code": None,
    }
    assert kaggle.validate_metadata(record, arxiv_id=ARXIV_ID) == []


def test_normalize_ignores_empty_author_entries(raw):
    raw["authors_parsed"] = [[], ["Doe"], ["Smith", "Jane"]]
    assert kaggle.normalize_kaggle_record(raw)["authors"] == ["Doe", "Jane Smith"]


def test_normalize_single_version_and_optional_fields(raw):
    raw.update(versions=[{"version": "v1", "created": CREATED}],
               title=None, authors_parsed=None, **{"journal-ref": ""})
    record = kaggle.normalize_kaggle_record(raw)
    assert record["versions"] == [1]
    assert record["revised_dates"] == []
    assert record["title"] is None
    assert record["authors"] == []
    assert record["venue"] is None


@pytest.mark.parametrize("label", [None, 1, "1", "v", "v-1", "v0", "v1.0"])
def test_normalize_rejects_invalid_versions(raw, label):
    raw["versions"] = [{"version": label, "created": CREATED}]
    with pytest.raises(ValueError):
        kaggle.normalize_kaggle_record(raw)


@pytest.mark.parametrize("versions", [
    [],
    [{"version": "v1", "created": CREATED}] * 2,
    [{"version": "v2", "created": REVISED}],
    [{"version": "v1", "created": CREATED}, {"version": "v3", "created": REVISED}],
    [{"version": "v1", "created": "bad date"}],
], ids=["empty", "duplicate", "missing-first", "gap", "invalid-date"])
def test_normalize_rejects_incomplete_history(raw, versions):
    raw["versions"] = versions
    with pytest.raises(ValueError):
        kaggle.normalize_kaggle_record(raw)


def test_download_returns_nonempty_snapshot(tmp_path, monkeypatch, download):
    cache = tmp_path / "raw"
    snapshot = tmp_path / kaggle.SNAPSHOT_NAME
    snapshot.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(kaggle, "RAW_DIR", cache)
    download.side_effect = None
    download.return_value = str(snapshot)
    assert kaggle.download_kaggle_metadata() == snapshot
    assert cache.is_dir()
    download.assert_called_once_with("Cornell-University/arxiv", path=kaggle.SNAPSHOT_NAME,
                                     output_dir=str(cache))


@pytest.mark.parametrize("exists", [False, True], ids=["missing", "empty"])
def test_download_rejects_invalid_snapshot(tmp_path, monkeypatch, download, exists):
    snapshot = tmp_path / kaggle.SNAPSHOT_NAME
    if exists:
        snapshot.touch()
    monkeypatch.setattr(kaggle, "RAW_DIR", tmp_path)
    download.side_effect = None
    download.return_value = str(snapshot)
    with pytest.raises(RuntimeError, match="Invalid Kaggle snapshot"):
        kaggle.download_kaggle_metadata()


@pytest.mark.parametrize("cached", [False, True], ids=["download", "cache-hit"])
def test_make_clients_prepares_index_and_shared_options(tmp_path, monkeypatch, cfg, raw, download, cached):
    cache = tmp_path / "raw"
    monkeypatch.setattr(kaggle, "RAW_DIR", cache)
    snapshot = cache / kaggle.SNAPSHOT_NAME
    cache.mkdir()
    if cached:
        snapshot.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    else:
        def create_snapshot(*args, **kwargs):
            snapshot.write_text(json.dumps(raw) + "\n", encoding="utf-8")
            return str(snapshot)
        download.side_effect = create_snapshot
    throttle = kaggle.Throttle(7)
    index, opts, returned_throttle = kaggle.make_clients(cfg, throttle=throttle)
    assert index == {ARXIV_ID: raw}
    assert returned_throttle is throttle
    assert opts == kaggle.http_options(cfg, throttle)
    assert download.call_count == (0 if cached else 1)


def test_make_clients_rejects_empty_cache(tmp_path, monkeypatch, cfg, download):
    monkeypatch.setattr(kaggle, "RAW_DIR", tmp_path)
    (tmp_path / kaggle.SNAPSHOT_NAME).touch()
    with pytest.raises(RuntimeError, match="Invalid Kaggle snapshot"):
        kaggle.make_clients(cfg)
    download.assert_not_called()


def discover(cfg, identity, client, logger):
    return kaggle.discover_paper(cfg, identity, client, {}, kaggle.Throttle(0), logger)


def test_discover_writes_valid_metadata_and_logs_success(cfg, raw):
    logger = Mock()
    assert discover(cfg, ARXIV_ID, {ARXIV_ID: raw}, logger) == OK
    path = kaggle.paper_dir(cfg, ARXIV_ID) / "metadata.json"
    assert read_json(path) == kaggle.normalize_kaggle_record(raw)
    assert not path.with_suffix(".json.tmp").exists()
    assert logger.log.call_args.args == ("discovery", OK)
    assert logger.log.call_args.kwargs["error_code"] is None
    assert logger.log.call_args.kwargs["elapsed_seconds"] >= 0


@pytest.mark.parametrize("single_version", [False, True])
def test_discover_skips_successful_existing_metadata(cfg, raw, single_version):
    if single_version:
        raw["versions"] = [{"version": "v1", "created": CREATED}]
    path = kaggle.paper_dir(cfg, ARXIV_ID) / "metadata.json"
    write_json_atomic(path, kaggle.normalize_kaggle_record(raw))
    before = path.read_bytes()
    client, logger = Mock(), Mock()
    assert discover(cfg, ARXIV_ID, client, logger) == SKIPPED
    client.get.assert_not_called()
    assert path.read_bytes() == before
    assert logger.log.call_args.args == ("discovery", SKIPPED)


def test_discover_refreshes_missing_revision_dates(cfg, raw):
    path = kaggle.paper_dir(cfg, ARXIV_ID) / "metadata.json"
    existing = kaggle.normalize_kaggle_record(raw)
    existing["revised_dates"] = None
    write_json_atomic(path, existing)
    assert discover(cfg, ARXIV_ID, {ARXIV_ID: raw}, Mock()) == OK
    assert read_json(path)["revised_dates"] == ["2025-07-02T12:00:00+00:00"]


@pytest.mark.parametrize("case,expected_code", [
    ("versioned-id", INVALID_ID),
    ("not-found", NOT_FOUND),
    ("bad-history", INVALID_RESPONSE),
    ("mismatched-id", INVALID_RESPONSE),
])
def test_discover_records_failures_without_raising(cfg, raw, case, expected_code):
    identity = ARXIV_ID
    client = {identity: raw}
    if case == "versioned-id":
        identity += "v2"
    elif case == "not-found":
        client = {}
    elif case == "bad-history":
        raw["versions"] = []
    else:
        raw["id"] = "2507.99999"
    logger = Mock()
    assert discover(cfg, identity, client, logger) == FAILED
    record = read_json(kaggle.paper_dir(cfg, identity) / "metadata.json")
    assert record["arxiv_id"] == identity
    assert record["status"] == FAILED
    assert record["error_code"] == expected_code
    assert record["versions"] is None
    assert logger.log.call_args.args == ("discovery", FAILED)
    assert logger.log.call_args.kwargs["error_code"] == expected_code


def test_discover_accepts_validation_warnings(cfg, raw):
    raw["title"] = None
    raw["authors_parsed"] = []
    assert discover(cfg, ARXIV_ID, {ARXIV_ID: raw}, Mock()) == OK
    assert read_json(kaggle.paper_dir(cfg, ARXIV_ID) / "metadata.json")["error_code"] is None
