import threading
import time
from concurrent.futures import ThreadPoolExecutor

from src import main
from src.utils.common import FAILED, OK, SKIPPED


class _Log:
    def log(self, *args, **kwargs):
        pass


class _Gauge:
    """Đếm số luồng đang ở trong một lane cùng lúc."""
    def __init__(self):
        self._m = threading.Lock()
        self.now = 0
        self.peak = 0

    def enter(self):
        with self._m:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def leave(self):
        with self._m:
            self.now -= 1


def test_each_lane_runs_one_at_a_time(monkeypatch):
    arxiv, s2 = _Gauge(), _Gauge()

    def fake_discover(cfg, arxiv_id, client, opts, throttle, logger, metrics_logger):
        arxiv.enter(); time.sleep(0.01); arxiv.leave()
        return OK

    def fake_download(cfg, arxiv_id, opts, logger, metrics_logger):
        arxiv.enter(); time.sleep(0.01); arxiv.leave()
        return {OK: 1, FAILED: 0, SKIPPED: 0}

    def fake_refs(cfg, arxiv_id, logger):
        s2.enter(); time.sleep(0.01); s2.leave()

    monkeypatch.setattr(main, "discover_paper", fake_discover)
    monkeypatch.setattr(main, "download_paper", fake_download)
    hooks = [("extract", lambda *a: None), ("references", fake_refs), ("storage", lambda *a: None)]
    arxiv_lock, s2_lock = threading.Lock(), threading.Lock()

    def run(i):
        return main.process_paper({}, f"2507.{i:05d}", None, {}, None, hooks, _Log(), None,
                                  arxiv_lock=arxiv_lock, s2_lock=s2_lock)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(8)))

    assert results == [OK] * 8
    assert arxiv.peak == 1
    assert s2.peak == 1