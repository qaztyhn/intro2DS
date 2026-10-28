"""Batched discovery wired into the pipeline: chunking, the arXiv lane and the config checks."""

import threading

import pytest

from src import main
from src.discovery import arxiv_api as discovery
from src.utils.common import FAILED, OK, SKIPPED

IDS = ["2507.00001", "2507.00002", "2507.00003"]


class _Log:
    def log(self, *args, **kwargs):
        pass


def test_prefetcher_asks_once_per_chunk(monkeypatch):
    calls = []
    monkeypatch.setattr(discovery, "prefetch_metadata", lambda cfg, ids, *a: calls.append(list(ids)))
    prefetch = discovery.BatchPrefetcher({}, IDS, 2, None, None, _Log())
    for arxiv_id in IDS + IDS:               # asking again changes nothing
        prefetch(arxiv_id)
    prefetch("2599.99999")                   # an id outside the list is ignored
    assert calls == [IDS[:2], IDS[2:]]


def test_process_paper_prefetches_inside_the_arxiv_lane(monkeypatch):
    lock, order = threading.Lock(), []

    monkeypatch.setattr(main, "discover_paper", lambda *a: order.append("discover") or OK)
    monkeypatch.setattr(
        main, "download_paper", lambda *a: order.append("download") or {OK: 1, FAILED: 0, SKIPPED: 0}
    )

    def prefetch(arxiv_id):
        order.append(("prefetch", arxiv_id, lock.locked()))

    status = main.process_paper({}, IDS[0], None, {}, None, [], _Log(), None,
                                arxiv_lock=lock, prefetch=prefetch)
    assert status == OK
    assert order == [("prefetch", IDS[0], True), "discover", "download"]


def _capture_prefetch(monkeypatch):
    seen = []

    def fake(cfg, arxiv_id, *args, **kwargs):
        seen.append(args[-1])                # the last argument process_paper receives is `prefetch`
        return OK

    monkeypatch.setattr(main, "process_paper", fake)
    return seen


def test_run_papers_builds_a_prefetcher_only_when_asked(monkeypatch):
    seen = _capture_prefetch(monkeypatch)
    main.run_papers({}, IDS, None, {}, None, [], _Log(), None, max_workers=1, batch_size=1)
    assert seen == [None, None, None]

    seen.clear()
    main.run_papers({}, IDS, None, {}, None, [], _Log(), None, max_workers=1, batch_size=2)
    assert all(isinstance(item, discovery.BatchPrefetcher) for item in seen) and len(seen) == 3

    seen.clear()
    main.run_papers({"config_name": "D1"}, IDS, None, {}, None, [], _Log(), None, max_workers=1, batch_size=2)
    assert seen == [None, None, None]


@pytest.mark.parametrize("bad", [0, 101, -1, "5", 2.0, True])
def test_run_papers_rejects_a_bad_batch_size(bad):
    with pytest.raises(ValueError):
        main.run_papers({}, IDS, None, {}, None, [], _Log(), None, batch_size=bad)