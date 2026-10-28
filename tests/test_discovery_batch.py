"""Batched discovery: one API request fills a cache that discover_paper then uses.

Offline: arxiv_api._query is replaced by a fake, so no arXiv request is made.
"""

from collections import namedtuple
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.discovery import arxiv_api as discovery
from src.utils.common import FAILED, OK, EventLogger, read_json
from src.utils.errors import HTTP_429, CrawlError

Author = namedtuple("Author", "name")
IDS = ["2507.00001", "2507.00002", "2507.00003"]  # all have exactly one version


class FakeResult:
    def __init__(self, short_id):
        self._short_id = short_id
        self.title = "A  title"
        self.authors = [Author("An")]
        self.published = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.updated = datetime(2025, 1, 2, tzinfo=timezone.utc)
        self.journal_ref = None

    def get_short_id(self):
        return self._short_id


def make_env(tmp_path, monkeypatch):
    discovery._PREFETCHED.clear()
    env = SimpleNamespace(
        cfg={"output_root": tmp_path, "run_id": "offline"},
        logger=EventLogger(tmp_path / "offline" / "events.jsonl", "offline"),
        calls=[], missing=set(), fail_batch=False,
    )

    def fake_query(client, throttle, ids):
        env.calls.append(list(ids))
        if env.fail_batch and len(ids) > 1:
            raise CrawlError("busy", HTTP_429, 429)
        return [FakeResult(f"{item}v1") for item in ids if item not in env.missing]

    monkeypatch.setattr(discovery, "_query", fake_query)
    return env


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield make_env(tmp_path, monkeypatch)
    discovery._PREFETCHED.clear()


def discover_all(env, ids):
    return [discovery.discover_paper(env.cfg, i, None, {}, None, env.logger) for i in ids]


def test_one_request_for_the_whole_batch(env):
    assert discovery.prefetch_metadata(env.cfg, IDS, None, None, env.logger) == 3
    assert discover_all(env, IDS) == [OK, OK, OK]
    assert env.calls == [IDS]                      # one request, not three
    assert discovery._PREFETCHED == {}             # every entry was used exactly once
    record = read_json(discovery.paper_dir(env.cfg, IDS[0]) / "metadata.json")
    assert record["status"] == OK and record["title"] == "A title" and record["versions"] == [1]


def test_failed_batch_falls_back_to_single_requests(env):
    env.fail_batch = True
    assert discovery.prefetch_metadata(env.cfg, IDS, None, None, env.logger) == 0
    assert discover_all(env, IDS) == [OK, OK, OK]
    assert env.calls == [IDS] + [[i] for i in IDS]


def test_id_missing_from_the_answer_is_asked_alone(env):
    env.missing = {IDS[1]}
    assert discovery.prefetch_metadata(env.cfg, IDS, None, None, env.logger) == 2
    assert discover_all(env, IDS) == [OK, FAILED, OK]   # arXiv really has no such id
    assert env.calls == [IDS, [IDS[1]]]


def test_completed_papers_are_left_out_of_the_batch(env):
    assert discover_all(env, IDS[:1]) == [OK]
    env.calls.clear()
    assert discovery.prefetch_metadata(env.cfg, IDS, None, None, env.logger) == 2
    assert env.calls == [IDS[1:]]


def test_nothing_to_batch_makes_no_request(env):
    assert discovery.prefetch_metadata(env.cfg, [IDS[0], "2507.00002v2"], None, None, env.logger) == 0
    assert env.calls == []