import json
from concurrent.futures import ThreadPoolExecutor

from src.utils.common import EventLogger


def test_event_logger_is_thread_safe(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, "t")

    def work(worker):
        for _ in range(200):
            logger.log("stage", "ok", arxiv_id=f"id{worker}", message="x" * 500)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(8)))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8 * 200
    for line in lines:
        json.loads(line)          # mỗi dòng phải là JSON hợp lệ