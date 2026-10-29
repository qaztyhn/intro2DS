"""Bounded three-lane A pipeline: arXiv producer, Extract and References consumers.

Only the main thread calls the arXiv API/downloads; the two consumers share no
arXiv client. Jobs carry paper IDs and small counters, never source bytes.
Storage/validation runs once after both consumers finish a paper.
"""
from dataclasses import dataclass, field
from queue import Queue
from threading import BoundedSemaphore, Lock, Thread

from .discovery.batch import chunks, discover_batches
from .utils.common import FAILED, OK, SKIPPED, Stopwatch


@dataclass
class PaperJob:
    arxiv_id: str
    discovery_status: str
    watch: Stopwatch = field(default_factory=Stopwatch)
    downloads: dict = field(default_factory=lambda: {OK: 0, FAILED: 0, SKIPPED: 0})
    errors: list = field(default_factory=list)
    extract_done: bool = False
    refs_done: bool = False
    finalizing: bool = False


def _positive_int(cfg, key, default, *, minimum=1, maximum=100):
    value = cfg.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer in [{minimum}, {maximum}]")
    return value


def run_bounded_pipeline(cfg, ids, client, opts, throttle, hooks, logger,
                         metrics_logger, root_error):
    """Process batches incrementally, with bounded concurrent paper lifetimes.

    A single arXiv producer performs both discovery and version downloads.
    Exactly one Extract and one References consumer work across different papers.
    Queues contain PaperJob handles; the in-flight semaphore bounds *all*
    incomplete papers, including ones waiting for the slower consumer.
    """
    batch_size = _positive_int(cfg, "discovery_batch_size", 20)
    max_inflight = _positive_int(cfg, "pipeline_max_inflight", 4)
    extract_qsize = _positive_int(cfg, "pipeline_extract_queue", 2)
    refs_qsize = _positive_int(cfg, "pipeline_references_queue", 4)
    prefetch_remaining = _positive_int(
        cfg, "pipeline_prefetch_remaining", 5, minimum=0, maximum=batch_size
    )
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate paper IDs")

    slots = BoundedSemaphore(max_inflight)
    extract_q = Queue(maxsize=extract_qsize)
    refs_q = Queue(maxsize=refs_qsize)
    state_lock = Lock()
    counts = {OK: 0, FAILED: 0, SKIPPED: 0}
    active = 0
    finished = 0
    stage_hooks = dict(hooks)

    def record_paper(job, status, code):
        nonlocal finished
        logger.log(
            "paper", status, arxiv_id=job.arxiv_id,
            elapsed_seconds=job.watch.elapsed, error_code=code,
            message=(f"discovery={job.discovery_status} versions ok/failed/skipped="
                     f"{job.downloads[OK]}/{job.downloads[FAILED]}/{job.downloads[SKIPPED]}"),
        )
        with state_lock:
            counts[status] += 1
            finished += 1
            index = finished
            print(f"[{index}/{len(ids)}] {job.arxiv_id}: {status}", flush=True)

    def run_stage(stage, job):
        func = stage_hooks.get(stage)
        if func is None:
            return None
        watch = Stopwatch()
        try:
            func(cfg, job.arxiv_id, logger)
        except Exception as exc:
            code = type(exc).__name__
            logger.log(
                stage, FAILED, arxiv_id=job.arxiv_id, error_code=code,
                message=str(exc)[:300], elapsed_seconds=watch.elapsed,
            )
            return code
        return None

    def complete(job):
        """A single completing worker validates/stores and releases the slot."""
        nonlocal active
        try:
            storage_error = run_stage("storage", job)
            if storage_error:
                job.errors.append(storage_error)
            status = FAILED if job.downloads[FAILED] or job.errors else OK
            code = (root_error(cfg, job.arxiv_id, job.discovery_status,
                               job.downloads, job.errors)
                    if status == FAILED else None)
            record_paper(job, status, code)
        except Exception as exc:
            # Even a coordinator/logging failure must not strand a permit.
            logger.log("pipeline_worker", FAILED, arxiv_id=job.arxiv_id,
                       error_code=type(exc).__name__, message=str(exc)[:300])
            record_paper(job, FAILED, type(exc).__name__)
        finally:
            with state_lock:
                active -= 1
            slots.release()

    def worker(q, stage, done_name):
        while True:
            job = q.get()
            try:
                if job is None:
                    return
                error = run_stage(stage, job)
                with state_lock:
                    if error:
                        job.errors.append(error)
                    setattr(job, done_name, True)
                    ready = job.extract_done and job.refs_done and not job.finalizing
                    if ready:
                        job.finalizing = True
                if ready:
                    complete(job)
            finally:
                q.task_done()

    workers = [
        Thread(target=worker, args=(extract_q, "extract", "extract_done"),
               name="A-extract"),
        Thread(target=worker, args=(refs_q, "references", "refs_done"),
               name="A-references"),
    ]
    for thread in workers:
        thread.start()

    batches = list(chunks(ids, batch_size))
    prefetched = None
    try:
        for batch_index, batch_ids in enumerate(batches):
            if prefetched is None:
                statuses = discover_batches(
                    cfg, batch_ids, client, opts, throttle, logger,
                    metrics_logger, batch_size=batch_size,
                )
            else:
                statuses, prefetched = prefetched, None
            for position, arxiv_id in enumerate(batch_ids):
                d_status = statuses[arxiv_id]
                if d_status == FAILED:
                    job = PaperJob(arxiv_id, d_status)
                    code = root_error(cfg, arxiv_id, d_status, job.downloads, [])
                    record_paper(job, FAILED, code)
                else:
                    slots.acquire()
                    with state_lock:
                        active += 1
                    job = PaperJob(arxiv_id, d_status)
                    try:
                        # References can start while this paper downloads.
                        refs_q.put(job)
                        try:
                            from .downloader.http import download_paper
                            job.downloads = download_paper(
                                cfg, arxiv_id, opts, logger, metrics_logger
                            )
                        except Exception as exc:
                            job.errors.append(type(exc).__name__)
                            logger.log("download", FAILED, arxiv_id=arxiv_id,
                                       error_code=type(exc).__name__,
                                       message=str(exc)[:300])
                        # All versions have finished downloading before Extract.
                        extract_q.put(job)
                    except BaseException:
                        # If interrupted between dispatches, complete both
                        # consumers' participation without leaking a permit.
                        # Normal errors inside download are handled above.
                        with state_lock:
                            job.errors.append("INTERRUPTED")
                            job.extract_done = True
                            job.refs_done = True
                            job.finalizing = True
                        complete(job)
                        raise

                remaining = len(batch_ids) - position - 1
                with state_lock:
                    saturated = active >= max_inflight
                if (batch_index + 1 < len(batches) and prefetched is None
                        and remaining <= prefetch_remaining and not saturated):
                    # No extra arXiv thread: discovery and downloads share the
                    # same sequential client/throttle. At most one future batch.
                    prefetched = discover_batches(
                        cfg, batches[batch_index + 1], client, opts, throttle,
                        logger, metrics_logger, batch_size=batch_size,
                    )
    finally:
        # Consumers drain their pending jobs even on producer exceptions.
        extract_q.put(None)
        refs_q.put(None)
        extract_q.join()
        refs_q.join()
        for thread in workers:
            thread.join()

    print("papers:", counts)
    return counts
