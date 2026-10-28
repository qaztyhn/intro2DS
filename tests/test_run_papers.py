import threading
import time

import pytest

from src import main
from src.utils.common import FAILED, OK, SKIPPED


def _run(ids, workers):
    return main.run_papers({}, ids, None, {}, None, [], None, None, max_workers=workers)


@pytest.mark.parametrize("workers", [1, 4])
def test_every_paper_processed_once(monkeypatch, workers):
    seen, lock = [], threading.Lock()

    def fake(cfg, arxiv_id, *args, **kwargs):
        with lock:
            seen.append(arxiv_id)
        return OK

    monkeypatch.setattr(main, "process_paper", fake)
    ids = [f"id{n}" for n in range(12)]
    assert _run(ids, workers) == {OK: 12, FAILED: 0, SKIPPED: 0}
    assert sorted(seen) == sorted(ids)


def test_rejects_bad_worker_count():
    with pytest.raises(ValueError):
        _run(["a"], 0)


@pytest.mark.parametrize("error, workers", [(RuntimeError("boom"), 1), (KeyboardInterrupt(), 2)])
def test_error_or_interrupt_drops_queued_papers(monkeypatch, error, workers):
    started, lock = [], threading.Lock()

    def fake(cfg, arxiv_id, *args, **kwargs):
        with lock:
            started.append(arxiv_id)
        time.sleep(0.01)
        if arxiv_id == "id1":
            raise error
        return OK

    monkeypatch.setattr(main, "process_paper", fake)
    ids = [f"id{n}" for n in range(40)]
    with pytest.raises(type(error)):
        _run(ids, workers)
    assert len(started) < len(ids)      # papers still queued were cancelled