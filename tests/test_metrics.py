import pytest

from src.metrics import mean_or_none, safe_ratio
import src.metrics as metrics
from src.metrics import Timer
from src.metrics import directory_size_bytes
from types import SimpleNamespace
from src.metrics import process_tree_rss_bytes
from src.metrics import summarize_rss_samples
from src.metrics import ResourceSampler
from src.metrics import ResourceMonitor
import json
from datetime import datetime

from src.metrics import EventLogger
from contextlib import contextmanager
from uuid import uuid4

from src.metrics import measure_stage
from src.metrics import summarize_references
from src.metrics import summarize_versions
from copy import deepcopy

from src.metrics import summarize_papers
from src.metrics import summarize_source_sizes
from src.metrics import read_events
from src.metrics import summarize_stage_times
from src.metrics import log_resource_results
from src.metrics import make_request_hook

from src.metrics import collect_environment
from src.metrics import build_run_report

from src.metrics import write_run_report

from src.metrics import summarize_request_attempts

@pytest.mark.parametrize(
    "numerator, denominator, expected",
    [
        (3, 4, 0.75),
        (0, 4, 0.0),
        (4, 4, 1.0),
    ],
)
def test_safe_ratio_valid(numerator, denominator, expected):
    assert safe_ratio(numerator, denominator) == pytest.approx(expected)


def test_safe_ratio_zero_denominator():
    assert safe_ratio(0, 0) is None


@pytest.mark.parametrize(
    "numerator, denominator",
    [(-1, 4), (0, -1), (5, 4), (1, 0)],
)
def test_safe_ratio_invalid_counts(numerator, denominator):
    with pytest.raises(ValueError):
        safe_ratio(numerator, denominator)


@pytest.mark.parametrize(
    "numerator, denominator",
    [(1.0, 4), (1, 4.0), (True, 4), (1, False), ("1", 4)],
)
def test_safe_ratio_invalid_types(numerator, denominator):
    with pytest.raises(TypeError):
        safe_ratio(numerator, denominator)


@pytest.mark.parametrize(
    "values, expected",
    [
        ([1.0, 2.0, 3.0], 2.0),
        ([0, 0], 0.0),
        ([5], 5.0),
    ],
)
def test_mean_valid(values, expected):
    assert mean_or_none(values) == pytest.approx(expected)


def test_mean_empty():
    assert mean_or_none([]) is None


def test_mean_accepts_generator():
    assert mean_or_none(x for x in [1, 2, 3]) == pytest.approx(2.0)


@pytest.mark.parametrize(
    "value",
    [-1, float("nan"), float("inf"), float("-inf")],
)
def test_mean_invalid_values(value):
    with pytest.raises(ValueError):
        mean_or_none([value])


@pytest.mark.parametrize("value", [None, True, "2"])
def test_mean_invalid_types(value):
    with pytest.raises(TypeError):
        mean_or_none([value])

def mock_clock(monkeypatch, timestamps):
    ticks = iter(timestamps)
    monkeypatch.setattr(metrics, "perf_counter", lambda: next(ticks))


def test_timer_initial_duration():
    assert Timer().duration_s is None


def test_timer_measures_duration(monkeypatch):
    mock_clock(monkeypatch, [10.0, 12.5])

    with Timer() as timer:
        assert timer.duration_s is None

    assert timer.duration_s == pytest.approx(2.5)


def test_timer_records_duration_and_propagates_error(monkeypatch):
    mock_clock(monkeypatch, [10.0, 11.5])
    timer = Timer()

    with pytest.raises(ValueError, match="operation failed"):
        with timer:
            raise ValueError("operation failed")

    assert timer.duration_s == pytest.approx(1.5)


def test_timer_can_be_reused(monkeypatch):
    mock_clock(monkeypatch, [10.0, 12.0, 20.0, 23.0])
    timer = Timer()

    with timer:
        pass

    assert timer.duration_s == pytest.approx(2.0)

    with timer:
        assert timer.duration_s is None

    assert timer.duration_s == pytest.approx(3.0)


def test_timer_rejects_nested_use(monkeypatch):
    mock_clock(monkeypatch, [10.0, 11.0])
    timer = Timer()

    with timer:
        with pytest.raises(RuntimeError, match="already running"):
            with timer:
                pass

    assert timer.duration_s == pytest.approx(1.0)

def test_directory_size_empty(tmp_path):
    assert directory_size_bytes(tmp_path) == 0


def test_directory_size_includes_nested_files(tmp_path):
    (tmp_path / "main.tex").write_bytes(b"abc")  # 3 bytes

    nested = tmp_path / "sections"
    nested.mkdir()
    (nested / "intro.tex").write_bytes(b"hello")  # 5 bytes
    (nested / "empty.bib").write_bytes(b"")      # 0 bytes

    assert directory_size_bytes(tmp_path) == 8


def test_directory_size_accepts_string_path(tmp_path):
    (tmp_path / "sample.bin").write_bytes(b"1234")

    assert directory_size_bytes(str(tmp_path)) == 4


def test_directory_size_missing_directory(tmp_path):
    with pytest.raises(NotADirectoryError):
        directory_size_bytes(tmp_path / "missing")


def test_directory_size_rejects_file_path(tmp_path):
    file_path = tmp_path / "main.tex"
    file_path.write_bytes(b"abc")

    with pytest.raises(NotADirectoryError):
        directory_size_bytes(file_path)

class FakeProcess:
    def __init__(self, rss=0, children=None, error=None):
        self.rss = rss
        self.descendants = children or []
        self.error = error

    def children(self, recursive=False):
        assert recursive is True
        return self.descendants

    def memory_info(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(rss=self.rss)


def test_process_tree_rss_sums_root_and_children(monkeypatch):
    root = FakeProcess(
        rss=100,
        children=[FakeProcess(rss=200), FakeProcess(rss=300)],
    )
    monkeypatch.setattr(metrics.psutil, "Process", lambda pid: root)

    assert process_tree_rss_bytes() == 600


def test_process_tree_rss_passes_requested_pid(monkeypatch):
    received = []

    def fake_process(pid):
        received.append(pid)
        return FakeProcess(rss=100)

    monkeypatch.setattr(metrics.psutil, "Process", fake_process)

    assert process_tree_rss_bytes(123) == 100
    assert received == [123]


def test_process_tree_rss_skips_exited_child(monkeypatch):
    root = FakeProcess(
        rss=100,
        children=[
            FakeProcess(error=metrics.psutil.NoSuchProcess(pid=456)),
            FakeProcess(rss=200),
        ],
    )
    monkeypatch.setattr(metrics.psutil, "Process", lambda pid: root)

    assert process_tree_rss_bytes() == 300


def test_process_tree_rss_propagates_access_denied(monkeypatch):
    root = FakeProcess(
        rss=100,
        children=[
            FakeProcess(error=metrics.psutil.AccessDenied(pid=456))
        ],
    )
    monkeypatch.setattr(metrics.psutil, "Process", lambda pid: root)

    with pytest.raises(metrics.psutil.AccessDenied):
        process_tree_rss_bytes()


def test_process_tree_rss_rejects_missing_root(monkeypatch):
    def missing_process(pid):
        raise metrics.psutil.NoSuchProcess(pid=pid)

    monkeypatch.setattr(metrics.psutil, "Process", missing_process)

    with pytest.raises(metrics.psutil.NoSuchProcess):
        process_tree_rss_bytes(999)

def test_rss_summary_time_weighted_mean():
    result = summarize_rss_samples([
        (0.0, 100),
        (1.0, 200),
        (4.0, 200),
    ])

    assert result["sample_count"] == 3
    assert result["sampled_duration_s"] == pytest.approx(4.0)
    assert result["rss_peak_bytes"] == 200
    assert result["rss_mean_bytes"] == pytest.approx(175.0)


def test_rss_summary_empty():
    result = summarize_rss_samples([])

    assert result["sample_count"] == 0
    assert result["sampled_duration_s"] == 0.0
    assert result["rss_peak_bytes"] is None
    assert result["rss_mean_bytes"] is None


def test_rss_summary_single_sample():
    result = summarize_rss_samples([(2.0, 100)])

    assert result["sample_count"] == 1
    assert result["sampled_duration_s"] == 0.0
    assert result["rss_peak_bytes"] == 100
    assert result["rss_mean_bytes"] is None


def test_rss_summary_last_sample_affects_peak_only():
    result = summarize_rss_samples([(10.0, 100), (12.0, 900)])

    assert result["sampled_duration_s"] == pytest.approx(2.0)
    assert result["rss_peak_bytes"] == 900
    assert result["rss_mean_bytes"] == pytest.approx(100.0)


@pytest.mark.parametrize(
    "samples",
    [
        [(0.0, 100), (0.0, 200)],
        [(2.0, 100), (1.0, 200)],
        [(-1.0, 100)],
        [(float("nan"), 100)],
        [(float("inf"), 100)],
        [(0.0, -1)],
    ],
)
def test_rss_summary_invalid_values(samples):
    with pytest.raises(ValueError):
        summarize_rss_samples(samples)


@pytest.mark.parametrize(
    "samples",
    [
        [(True, 100)],
        [("0", 100)],
        [(0.0, True)],
        [(0.0, 1.5)],
    ],
)
def test_rss_summary_invalid_types(samples):
    with pytest.raises(TypeError):
        summarize_rss_samples(samples)

def test_resource_sampler_empty():
    sampler = ResourceSampler("unused", pid=123)

    assert sampler.summary() == {
        "sample_count": 0,
        "sampled_duration_s": 0.0,
        "rss_peak_bytes": None,
        "rss_mean_bytes": None,
        "disk_peak_bytes": None,
    }


def test_resource_sampler_collects_and_summarizes(monkeypatch):
    mock_clock(monkeypatch, [10.0, 11.0, 12.0, 15.0])

    rss_values = iter([100, 200, 150])
    disk_values = iter([10, 30, 20])
    received_pids = []
    received_directories = []

    def fake_rss(pid):
        received_pids.append(pid)
        return next(rss_values)

    def fake_disk(directory):
        received_directories.append(str(directory))
        return next(disk_values)

    monkeypatch.setattr(metrics, "process_tree_rss_bytes", fake_rss)
    monkeypatch.setattr(metrics, "directory_size_bytes", fake_disk)

    sampler = ResourceSampler("sample-data", pid=123)

    first = sampler.sample()
    sampler.sample()
    sampler.sample()

    assert first == {
        "elapsed_s": 1.0,
        "rss_bytes": 100,
        "disk_bytes": 10,
    }
    assert received_pids == [123, 123, 123]
    assert received_directories == ["sample-data"] * 3

    result = sampler.summary()

    assert result["sample_count"] == 3
    assert result["sampled_duration_s"] == pytest.approx(4.0)
    assert result["rss_peak_bytes"] == 200
    assert result["rss_mean_bytes"] == pytest.approx(175.0)
    assert result["disk_peak_bytes"] == 30


def test_resource_sampler_returns_copy(monkeypatch):
    mock_clock(monkeypatch, [10.0, 11.0])
    monkeypatch.setattr(metrics, "process_tree_rss_bytes", lambda pid: 100)
    monkeypatch.setattr(metrics, "directory_size_bytes", lambda path: 20)

    sampler = ResourceSampler("unused", pid=123)
    record = sampler.sample()
    record["rss_bytes"] = 999

    assert sampler.samples[0]["rss_bytes"] == 100


def test_resource_sampler_defaults_to_current_pid(monkeypatch):
    monkeypatch.setattr(metrics.os, "getpid", lambda: 321)

    sampler = ResourceSampler("unused")

    assert sampler.pid == 321


@pytest.mark.parametrize(
    "failing_function",
    ["process_tree_rss_bytes", "directory_size_bytes"],
)
def test_resource_sampler_does_not_store_failed_sample(
    monkeypatch, failing_function
):
    monkeypatch.setattr(metrics, "process_tree_rss_bytes", lambda pid: 100)
    monkeypatch.setattr(metrics, "directory_size_bytes", lambda path: 20)

    def fail(*args):
        raise PermissionError("measurement failed")

    monkeypatch.setattr(metrics, failing_function, fail)
    sampler = ResourceSampler("unused", pid=123)

    with pytest.raises(PermissionError, match="measurement failed"):
        sampler.sample()

    assert sampler.samples == []

class FakeThread:
    def __init__(self, target, daemon):
        self.target = target
        self.started = False
        self.joined = False

    def start(self):
        self.started = True

    def join(self):
        self.joined = True


@pytest.fixture
def monitor_dependencies(monkeypatch):
    class FakeSampler:
        def __init__(self, directory, pid):
            self.sample_calls = 0

        def sample(self):
            self.sample_calls += 1

    monkeypatch.setattr(metrics, "ResourceSampler", FakeSampler)
    monkeypatch.setattr(metrics, "Thread", FakeThread)


def test_monitor_samples_at_start_and_end(monitor_dependencies):
    with ResourceMonitor("unused") as monitor:
        assert monitor.sampler.sample_calls == 1
        assert monitor._thread.started is True

    assert monitor.sampler.sample_calls == 2
    assert monitor._thread.joined is True
    assert monitor._stop.is_set()


def test_monitor_preserves_pipeline_error(monitor_dependencies):
    with pytest.raises(ValueError, match="pipeline failed"):
        with ResourceMonitor("unused") as monitor:
            raise ValueError("pipeline failed")

    assert monitor.sampler.sample_calls == 2
    assert monitor._thread.joined is True


def test_monitor_reports_measurement_error(monitor_dependencies):
    error = PermissionError("measurement denied")

    with pytest.raises(RuntimeError, match="monitoring failed") as caught:
        with ResourceMonitor("unused") as monitor:
            monitor.error = error

    assert caught.value.__cause__ is error
    assert monitor.sampler.sample_calls == 1


def test_monitor_does_not_replace_pipeline_error(monitor_dependencies):
    error = PermissionError("measurement denied")

    with pytest.raises(ValueError, match="pipeline failed"):
        with ResourceMonitor("unused") as monitor:
            monitor.error = error
            raise ValueError("pipeline failed")

    assert monitor.error is error


def test_monitor_rejects_reuse(monitor_dependencies):
    monitor = ResourceMonitor("unused")

    with monitor:
        pass

    with pytest.raises(RuntimeError, match="only be used once"):
        with monitor:
            pass


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_monitor_invalid_interval_values(interval):
    with pytest.raises(ValueError):
        ResourceMonitor("unused", interval_s=interval)


@pytest.mark.parametrize("interval", [True, "1", None])
def test_monitor_invalid_interval_types(interval):
    with pytest.raises(TypeError):
        ResourceMonitor("unused", interval_s=interval)

class FakeStopEvent:
    def __init__(self, wait_results):
        self.wait_results = iter(wait_results)
        self.received_timeouts = []
        self.was_set = False

    def wait(self, timeout):
        self.received_timeouts.append(timeout)
        return next(self.wait_results)

    def set(self):
        self.was_set = True


def test_monitor_run_samples_until_stopped():
    monitor = ResourceMonitor("unused", interval_s=0.5)
    calls = []

    monitor.sampler = SimpleNamespace(
        sample=lambda: calls.append("sample")
    )
    monitor._stop = FakeStopEvent([False, False, True])

    monitor._run()

    assert calls == ["sample", "sample"]
    assert monitor._stop.received_timeouts == [0.5, 0.5, 0.5]
    assert monitor.error is None


def test_monitor_run_stops_without_sampling():
    monitor = ResourceMonitor("unused")
    calls = []

    monitor.sampler = SimpleNamespace(
        sample=lambda: calls.append("sample")
    )
    monitor._stop = FakeStopEvent([True])

    monitor._run()

    assert calls == []
    assert monitor.error is None


def test_monitor_run_records_sampling_error():
    monitor = ResourceMonitor("unused")
    error = PermissionError("sampling denied")

    def fail():
        raise error

    monitor.sampler = SimpleNamespace(sample=fail)
    monitor._stop = FakeStopEvent([False])

    monitor._run()

    assert monitor.error is error
    assert monitor._stop.was_set is True

def test_logger_creates_directory_and_preserves_fields(tmp_path):
    path = tmp_path / "nested" / "events.jsonl"
    logger = EventLogger(path, run_id="run-01")

    logger.log(
        "references",
        "finished",
        title="Tiếng Việt",
        status="ok",
        error_code=None,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["run_id"] == "run-01"
    assert record["stage"] == "references"
    assert record["event"] == "finished"
    assert record["title"] == "Tiếng Việt"
    assert record["error_code"] is None

    timestamp = datetime.fromisoformat(record["timestamp"])
    assert timestamp.utcoffset().total_seconds() == 0


def test_logger_appends_without_overwriting(tmp_path):
    path = tmp_path / "events.jsonl"
    first_logger = EventLogger(path, run_id="run-01")
    first_logger.log("pipeline", "started")

    second_logger = EventLogger(path, run_id="run-02")
    second_logger.log("pipeline", "finished")

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]

    assert [record["run_id"] for record in records] == [
        "run-01",
        "run-02",
    ]


@pytest.mark.parametrize("run_id", ["", "  ", None, 123])
def test_logger_rejects_invalid_run_id(tmp_path, run_id):
    with pytest.raises(ValueError):
        EventLogger(tmp_path / "events.jsonl", run_id=run_id)


@pytest.mark.parametrize(
    "stage, event",
    [("", "started"), ("pipeline", ""), (None, "started")],
)
def test_logger_rejects_invalid_event_names(tmp_path, stage, event):
    logger = EventLogger(tmp_path / "events.jsonl", run_id="run-01")

    with pytest.raises(ValueError):
        logger.log(stage, event)


@pytest.mark.parametrize("key", ["run_id", "timestamp"])
def test_logger_rejects_reserved_fields(tmp_path, key):
    logger = EventLogger(tmp_path / "events.jsonl", run_id="run-01")

    with pytest.raises(ValueError, match="reserved"):
        logger.log("pipeline", "started", **{key: "replacement"})


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
)
def test_logger_invalid_number_does_not_change_file(tmp_path, value):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, run_id="run-01")
    logger.log("pipeline", "started")
    before = path.read_bytes()

    with pytest.raises(ValueError):
        logger.log("pipeline", "finished", duration_s=value)

    assert path.read_bytes() == before


def test_logger_rejects_non_serializable_value(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, run_id="run-01")

    with pytest.raises(TypeError):
        logger.log("pipeline", "started", invalid_value=object())

    assert not path.exists()

@contextmanager
def measure_stage(logger: EventLogger, stage: str, **fields):
    """Measure an operation and log its start and final outcome."""
    reserved = {
        "operation_id",
        "status",
        "duration_s",
        "error_type",
        "error_message",
    }
    if reserved.intersection(fields):
        raise ValueError("Fields cannot overwrite measurement keys")

    operation_id = uuid4().hex

    logger.log(
        stage,
        "started",
        operation_id=operation_id,
        **fields,
    )

    timer = Timer()

    try:
        with timer:
            yield timer
    except BaseException as error:
        try:
            logger.log(
                stage,
                "finished",
                operation_id=operation_id,
                status="failed",
                duration_s=timer.duration_s,
                error_type=type(error).__name__,
                error_message=str(error),
                **fields,
            )
        except Exception as logging_error:
            error.add_note(
                f"Could not write failure log: {logging_error}"
            )
        raise
    else:
        logger.log(
            stage,
            "finished",
            operation_id=operation_id,
            status="ok",
            duration_s=timer.duration_s,
            **fields,
        )

def test_measure_stage_logs_success(tmp_path, monkeypatch):
    mock_clock(monkeypatch, [10.0, 12.5])
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, run_id="run-01")

    with measure_stage(
        logger, "extract", arxiv_id="2507.11659", version=1
    ) as timer:
        assert timer.duration_s is None

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 2
    assert [record["event"] for record in records] == [
        "started", "finished"
    ]
    assert records[0]["operation_id"] == records[1]["operation_id"]
    assert records[1]["arxiv_id"] == "2507.11659"
    assert records[1]["version"] == 1
    assert records[1]["status"] == "ok"
    assert records[1]["duration_s"] == pytest.approx(2.5)


def test_measure_stage_logs_failure_and_preserves_error(
    tmp_path, monkeypatch
):
    mock_clock(monkeypatch, [10.0, 11.0])
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, run_id="run-01")
    original_error = ValueError("extraction failed")

    with pytest.raises(ValueError) as caught:
        with measure_stage(logger, "extract"):
            raise original_error

    assert caught.value is original_error

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 2
    assert records[0]["operation_id"] == records[1]["operation_id"]
    assert records[1]["status"] == "failed"
    assert records[1]["duration_s"] == pytest.approx(1.0)
    assert records[1]["error_type"] == "ValueError"
    assert records[1]["error_message"] == "extraction failed"


def test_measure_stage_preserves_error_when_failure_log_fails(
    monkeypatch,
):
    mock_clock(monkeypatch, [10.0, 11.0])
    events = []

    def fake_log(stage, event, **fields):
        events.append(event)
        if event == "finished":
            raise OSError("disk full")

    logger = SimpleNamespace(log=fake_log)
    original_error = ValueError("pipeline failed")

    with pytest.raises(ValueError) as caught:
        with measure_stage(logger, "extract"):
            raise original_error

    assert caught.value is original_error
    assert events == ["started", "finished"]
    assert any("disk full" in note for note in caught.value.__notes__)


@pytest.mark.parametrize(
    "key",
    [
        "operation_id",
        "status",
        "duration_s",
        "error_type",
        "error_message",
    ],
)
def test_measure_stage_rejects_reserved_fields(tmp_path, key):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, run_id="run-01")

    with pytest.raises(ValueError, match="measurement keys"):
        with measure_stage(logger, "extract", **{key: "invalid"}):
            pytest.fail("Operation must not start")

    assert not path.exists()

def test_references_summary_mixed_results():
    result = summarize_references(
        ["a", "b", "c"],
        {
            "a": {
                "status": "ok",
                "references": [
                    {"ref_arxiv_id": "2507.11659"},
                    {"ref_arxiv_id": None},
                ],
            },
            "b": {"status": "empty", "references": []},
            "c": {"status": "failed"},
        },
    )

    assert result["input_paper_count"] == 3
    assert result["reference_response_success_rate"] == pytest.approx(2 / 3)
    assert result["reference_empty_paper_count"] == 1
    assert result["reference_count"] == 2
    assert result["references_per_valid_response"] == pytest.approx(1.0)
    assert result["reference_with_arxiv_id_count"] == 1
    assert result["reference_with_arxiv_id_rate"] == pytest.approx(0.5)


def test_references_summary_missing_result_counts_as_pending():
    result = summarize_references(
        ["a", "b"],
        {"a": {"status": "empty", "references": []}},
    )

    assert result["reference_status_counts"]["pending"] == 1
    assert result["reference_response_success_rate"] == pytest.approx(0.5)


def test_references_summary_empty_input():
    result = summarize_references([], {})

    assert result["input_paper_count"] == 0
    assert result["reference_response_success_rate"] is None
    assert result["references_per_valid_response"] is None
    assert result["reference_with_arxiv_id_rate"] is None


def test_references_summary_all_failed():
    result = summarize_references(
        ["a"], {"a": {"status": "failed"}}
    )

    assert result["reference_response_success_rate"] == 0.0
    assert result["references_per_valid_response"] is None


def test_references_summary_valid_empty_response():
    result = summarize_references(
        ["a"], {"a": {"status": "empty", "references": []}}
    )

    assert result["reference_response_success_rate"] == 1.0
    assert result["references_per_valid_response"] == 0.0
    assert result["reference_with_arxiv_id_rate"] is None


def test_references_summary_skipped_is_not_automatically_successful():
    result = summarize_references(
        ["a"], {"a": {"status": "skipped"}}
    )

    assert result["reference_status_counts"]["skipped"] == 1
    assert result["reference_response_success_rate"] == 0.0


def test_references_summary_counts_only_nonblank_arxiv_ids():
    result = summarize_references(
        ["a"],
        {
            "a": {
                "status": "ok",
                "references": [
                    {"ref_arxiv_id": "2507.11659"},
                    {"ref_arxiv_id": ""},
                    {"ref_arxiv_id": "  "},
                    {"ref_arxiv_id": None},
                    {},
                ],
            }
        },
    )

    assert result["reference_count"] == 5
    assert result["reference_with_arxiv_id_count"] == 1
    assert result["reference_with_arxiv_id_rate"] == pytest.approx(0.2)


@pytest.mark.parametrize("input_ids", [["a", "a"], [""], [None]])
def test_references_summary_rejects_invalid_input_ids(input_ids):
    with pytest.raises(ValueError):
        summarize_references(input_ids, {})


def test_references_summary_rejects_unexpected_paper():
    with pytest.raises(ValueError, match="outside"):
        summarize_references(["a"], {"b": {"status": "failed"}})


@pytest.mark.parametrize(
    "record",
    [
        {"status": "unknown"},
        {"status": "ok", "references": []},
        {"status": "empty", "references": [{}]},
    ],
)
def test_references_summary_rejects_inconsistent_status(record):
    with pytest.raises(ValueError):
        summarize_references(["a"], {"a": record})


@pytest.mark.parametrize("references", [None, {}, ["invalid"]])
def test_references_summary_rejects_invalid_reference_types(references):
    with pytest.raises(TypeError):
        summarize_references(
            ["a"],
            {"a": {"status": "ok", "references": references}},
        )

def test_versions_summary_mixed_results():
    result = summarize_versions(
        {"a": [1, 2, 3], "b": None},
        {
            "a": {
                1: {"extract_status": "ok"},
                2: {
                    "extract_status": "empty",
                    "error_code": "NO_TEX_SOURCE",
                },
                3: {"extract_status": "failed"},
            }
        },
    )

    assert result["input_paper_count"] == 2
    assert result["unknown_versions_paper_count"] == 1
    assert result["known_expected_version_count"] == 3
    assert result["processed_version_count"] == 2
    assert result["with_tex_version_count"] == 1
    assert result["no_tex_version_count"] == 1
    assert result["unresolved_version_count"] == 1
    assert result["known_version_processing_coverage"] == pytest.approx(2 / 3)


def test_versions_summary_missing_result_is_unresolved():
    result = summarize_versions({"a": [1, 2]}, {})

    assert result["processed_version_count"] == 0
    assert result["unresolved_version_count"] == 2
    assert result["known_version_processing_coverage"] == 0.0


def test_versions_summary_empty_input():
    result = summarize_versions({}, {})

    assert result["input_paper_count"] == 0
    assert result["known_expected_version_count"] == 0
    assert result["known_version_processing_coverage"] is None


def test_versions_summary_unknown_list_is_not_zero_versions():
    result = summarize_versions({"a": None}, {})

    assert result["unknown_versions_paper_count"] == 1
    assert result["known_version_processing_coverage"] is None


def test_versions_summary_complete():
    result = summarize_versions(
        {"a": [1, 2]},
        {
            "a": {
                1: {"extract_status": "ok"},
                2: {"extract_status": "ok"},
            }
        },
    )

    assert result["processed_version_count"] == 2
    assert result["with_tex_version_count"] == 2
    assert result["unresolved_version_count"] == 0
    assert result["known_version_processing_coverage"] == 1.0


@pytest.mark.parametrize(
    "status", ["pending", "running", "failed", "skipped"]
)
def test_versions_summary_unfinished_statuses(status):
    result = summarize_versions(
        {"a": [1]},
        {"a": {1: {"extract_status": status}}},
    )

    assert result["processed_version_count"] == 0
    assert result["unresolved_version_count"] == 1


@pytest.mark.parametrize(
    "versions",
    [[], [1, 1], [0], [-1], [True], ["1"], [1.5]],
)
def test_versions_summary_rejects_invalid_expected_versions(versions):
    with pytest.raises(ValueError):
        summarize_versions({"a": versions}, {})


def test_versions_summary_rejects_unexpected_paper():
    with pytest.raises(ValueError, match="unexpected paper"):
        summarize_versions({"a": [1]}, {"b": {}})


def test_versions_summary_rejects_unexpected_version():
    with pytest.raises(ValueError, match="unexpected versions"):
        summarize_versions(
            {"a": [1]},
            {"a": {2: {"extract_status": "ok"}}},
        )


def test_versions_summary_requires_known_list_for_results():
    with pytest.raises(ValueError, match="known expected"):
        summarize_versions(
            {"a": None},
            {"a": {1: {"extract_status": "ok"}}},
        )


def test_versions_summary_rejects_unknown_status():
    with pytest.raises(ValueError, match="Unknown extraction"):
        summarize_versions(
            {"a": [1]},
            {"a": {1: {"extract_status": "invalid"}}},
        )


@pytest.mark.parametrize(
    "error_code", [None, "UNSUPPORTED_FORMAT", "CORRUPT_SOURCE"]
)
def test_versions_summary_empty_requires_confirmed_no_tex(error_code):
    with pytest.raises(ValueError, match="NO_TEX_SOURCE"):
        summarize_versions(
            {"a": [1]},
            {
                "a": {
                    1: {
                        "extract_status": "empty",
                        "error_code": error_code,
                    }
                }
            },
        )

@pytest.fixture
def complete_paper_record():
    return {
        "discovery_status": "ok",
        "expected_versions": [1],
        "versions": {1: {"extract_status": "ok"}},
        "references": {"status": "empty", "references": []},
        "validation_status": "ok",
    }


def test_papers_summary_complete_and_missing(complete_paper_record):
    result = summarize_papers(
        ["a", "b"],
        {"a": complete_paper_record},
    )

    assert result["input_paper_count"] == 2
    assert result["complete_paper_count"] == 1
    assert result["incomplete_paper_count"] == 1
    assert result["paper_completion_rate"] == pytest.approx(0.5)
    assert result["discovery_ok_count"] == 1
    assert result["source_complete_count"] == 1
    assert result["references_ok_count"] == 1
    assert result["schema_ok_count"] == 1


@pytest.mark.parametrize(
    "field, value",
    [
        ("discovery_status", "failed"),
        ("validation_status", "failed"),
        ("references", {"status": "failed"}),
        ("versions", {1: {"extract_status": "failed"}}),
    ],
)
def test_papers_summary_requires_every_condition(
    complete_paper_record, field, value
):
    record = deepcopy(complete_paper_record)
    record[field] = value

    result = summarize_papers(["a"], {"a": record})

    assert result["complete_paper_count"] == 0
    assert result["paper_completion_rate"] == 0.0


def test_papers_summary_confirmed_no_tex_can_complete(
    complete_paper_record,
):
    record = deepcopy(complete_paper_record)
    record["versions"] = {
        1: {
            "extract_status": "empty",
            "error_code": "NO_TEX_SOURCE",
        }
    }

    result = summarize_papers(["a"], {"a": record})

    assert result["source_complete_count"] == 1
    assert result["complete_paper_count"] == 1


def test_papers_summary_requires_all_versions(complete_paper_record):
    record = deepcopy(complete_paper_record)
    record["expected_versions"] = [1, 2]

    result = summarize_papers(["a"], {"a": record})

    assert result["source_complete_count"] == 0
    assert result["complete_paper_count"] == 0


def test_papers_summary_unknown_versions_cannot_complete(
    complete_paper_record,
):
    record = deepcopy(complete_paper_record)
    record["expected_versions"] = None
    record["versions"] = {}

    result = summarize_papers(["a"], {"a": record})

    assert result["source_complete_count"] == 0
    assert result["complete_paper_count"] == 0


def test_papers_summary_empty_input():
    result = summarize_papers([], {})

    assert result["input_paper_count"] == 0
    assert result["complete_paper_count"] == 0
    assert result["paper_completion_rate"] is None


@pytest.mark.parametrize("input_ids", [["a", "a"], [""], [None]])
def test_papers_summary_rejects_invalid_ids(input_ids):
    with pytest.raises(ValueError):
        summarize_papers(input_ids, {})


def test_papers_summary_rejects_unexpected_paper():
    with pytest.raises(ValueError, match="unexpected paper"):
        summarize_papers(["a"], {"b": {}})


@pytest.mark.parametrize(
    "field", ["discovery_status", "validation_status"]
)
def test_papers_summary_rejects_unknown_status(
    complete_paper_record, field
):
    record = deepcopy(complete_paper_record)
    record[field] = "invalid"

    with pytest.raises(ValueError, match="Unknown"):
        summarize_papers(["a"], {"a": record})

def test_source_sizes_paired_measurements():
    result = summarize_source_sizes(
        ["a", "b", "c"],
        {
            "a": {
                "size_measurement_complete": True,
                "unpacked_total_bytes": 1000,
                "kept_source_bytes": 200,
            },
            "b": {
                "size_measurement_complete": True,
                "unpacked_total_bytes": 3000,
                "kept_source_bytes": 800,
            },
        },
    )

    assert result["size_measured_paper_count"] == 2
    assert result["size_unmeasured_paper_count"] == 1
    assert result["source_bytes_before_total"] == 4000
    assert result["source_bytes_after_total"] == 1000
    assert result["source_bytes_before_mean"] == pytest.approx(2000)
    assert result["source_bytes_after_mean"] == pytest.approx(500)
    assert result["source_reduction_rate"] == pytest.approx(0.75)


def test_source_sizes_excludes_partial_measurements():
    result = summarize_source_sizes(
        ["a"],
        {
            "a": {
                "size_measurement_complete": False,
                "unpacked_total_bytes": 1000,
                "kept_source_bytes": 200,
            }
        },
    )

    assert result["size_measured_paper_count"] == 0
    assert result["size_unmeasured_paper_count"] == 1
    assert result["source_bytes_before_mean"] is None
    assert result["source_bytes_after_mean"] is None
    assert result["source_reduction_rate"] is None


def test_source_sizes_empty_input():
    result = summarize_source_sizes([], {})

    assert result["input_paper_count"] == 0
    assert result["source_bytes_before_mean"] is None
    assert result["source_bytes_after_mean"] is None
    assert result["source_reduction_rate"] is None


def test_source_sizes_valid_zero_measurement():
    result = summarize_source_sizes(
        ["a"],
        {
            "a": {
                "size_measurement_complete": True,
                "unpacked_total_bytes": 0,
                "kept_source_bytes": 0,
            }
        },
    )

    assert result["size_measured_paper_count"] == 1
    assert result["source_bytes_before_mean"] == 0.0
    assert result["source_bytes_after_mean"] == 0.0
    assert result["source_reduction_rate"] is None


@pytest.mark.parametrize(
    "before, after", [(100, 100), (100, 0)]
)
def test_source_sizes_reduction_boundaries(before, after):
    result = summarize_source_sizes(
        ["a"],
        {
            "a": {
                "size_measurement_complete": True,
                "unpacked_total_bytes": before,
                "kept_source_bytes": after,
            }
        },
    )

    assert result["source_reduction_rate"] == pytest.approx(
        (before - after) / before
    )


@pytest.mark.parametrize("input_ids", [["a", "a"], [""], [None]])
def test_source_sizes_rejects_invalid_ids(input_ids):
    with pytest.raises(ValueError):
        summarize_source_sizes(input_ids, {})


def test_source_sizes_rejects_unexpected_paper():
    with pytest.raises(ValueError, match="unexpected paper"):
        summarize_source_sizes(["a"], {"b": {}})


@pytest.mark.parametrize("complete", [None, 1, "true"])
def test_source_sizes_rejects_invalid_complete_flag(complete):
    with pytest.raises(TypeError):
        summarize_source_sizes(
            ["a"], {"a": {"size_measurement_complete": complete}}
        )


@pytest.mark.parametrize(
    "before, after",
    [(None, 0), (100, None), (True, 0), (100, 1.5)],
)
def test_source_sizes_rejects_invalid_byte_types(before, after):
    with pytest.raises(TypeError):
        summarize_source_sizes(
            ["a"],
            {
                "a": {
                    "size_measurement_complete": True,
                    "unpacked_total_bytes": before,
                    "kept_source_bytes": after,
                }
            },
        )


@pytest.mark.parametrize(
    "before, after", [(-1, 0), (100, -1), (100, 101)]
)
def test_source_sizes_rejects_invalid_byte_values(before, after):
    with pytest.raises(ValueError):
        summarize_source_sizes(
            ["a"],
            {
                "a": {
                    "size_measurement_complete": True,
                    "unpacked_total_bytes": before,
                    "kept_source_bytes": after,
                }
            },
        )

def test_read_events_reads_logger_output(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, run_id="run-01")
    logger.log("pipeline", "started")
    logger.log("pipeline", "finished", duration_s=1.5)

    events = read_events(path)

    assert len(events) == 2
    assert [event["event"] for event in events] == [
        "started", "finished"
    ]
    assert events[1]["duration_s"] == pytest.approx(1.5)


def test_read_events_empty_file(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("", encoding="utf-8")

    assert read_events(path) == []


def test_read_events_ignores_blank_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, run_id="run-01")
    logger.log("pipeline", "started")

    text = path.read_text(encoding="utf-8")
    path.write_text("\n" + text + "\n  \n", encoding="utf-8")

    assert len(read_events(path)) == 1


def test_read_events_reports_invalid_json_line(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, run_id="run-01")
    logger.log("pipeline", "started")

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"incomplete":\n')

    with pytest.raises(ValueError, match="Invalid JSON at line 2"):
        read_events(path)


@pytest.mark.parametrize("record", [[], None, "text", 123])
def test_read_events_rejects_non_object_records(tmp_path, record):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="event object at line 1"):
        read_events(path)


@pytest.mark.parametrize(
    "missing_key", ["run_id", "timestamp", "stage", "event"]
)
def test_read_events_rejects_missing_fields(tmp_path, missing_key):
    record = {
        "run_id": "run-01",
        "timestamp": "2026-10-21T00:00:00+00:00",
        "stage": "pipeline",
        "event": "started",
    }
    del record[missing_key]

    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Missing fields at line 1"):
        read_events(path)


@pytest.mark.parametrize(
    "key, value",
    [
        ("run_id", ""),
        ("timestamp", None),
        ("stage", "  "),
        ("event", 123),
    ],
)
def test_read_events_rejects_invalid_required_values(
    tmp_path, key, value
):
    record = {
        "run_id": "run-01",
        "timestamp": "2026-10-21T00:00:00+00:00",
        "stage": "pipeline",
        "event": "started",
    }
    record[key] = value

    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=f"Invalid {key} at line 1"):
        read_events(path)


def test_read_events_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_events(tmp_path / "missing.jsonl")

def make_timed_event(operation_id, event, **fields):
    return {
        "run_id": "run-01",
        "stage": "extract",
        "event": event,
        "operation_id": operation_id,
        **fields,
    }


def test_stage_times_includes_failed_and_unfinished_operations():
    events = [
        make_timed_event("a", "started"),
        make_timed_event("a", "finished", status="ok", duration_s=2.0),
        make_timed_event("b", "started"),
        make_timed_event("b", "finished", status="failed", duration_s=4.0),
        make_timed_event("c", "started"),
    ]

    result = summarize_stage_times(events, "run-01", "extract")

    assert result["started_operation_count"] == 3
    assert result["finished_operation_count"] == 2
    assert result["unfinished_operation_count"] == 1
    assert result["successful_operation_count"] == 1
    assert result["failed_operation_count"] == 1
    assert result["duration_sum_s"] == pytest.approx(6.0)
    assert result["duration_mean_s"] == pytest.approx(3.0)
    assert result["duration_min_s"] == pytest.approx(2.0)
    assert result["duration_max_s"] == pytest.approx(4.0)


def test_stage_times_filters_run_and_stage():
    events = [
        make_timed_event("a", "started", run_id="other-run"),
        make_timed_event("b", "started", stage="download"),
        make_timed_event("c", "retry"),
    ]

    result = summarize_stage_times(events, "run-01", "extract")

    assert result["started_operation_count"] == 0
    assert result["duration_mean_s"] is None


def test_stage_times_empty_input():
    result = summarize_stage_times([], "run-01", "extract")

    assert result["duration_sum_s"] == 0
    assert result["duration_mean_s"] is None
    assert result["duration_min_s"] is None
    assert result["duration_max_s"] is None


@pytest.mark.parametrize(
    "events, message",
    [
        (
            [
                make_timed_event("a", "started"),
                make_timed_event("a", "started"),
            ],
            "Duplicate operation start",
        ),
        (
            [
                make_timed_event(
                    "a", "finished", status="ok", duration_s=1.0
                )
            ],
            "without a start",
        ),
        (
            [
                make_timed_event("a", "started"),
                make_timed_event(
                    "a", "finished", status="ok", duration_s=1.0
                ),
                make_timed_event(
                    "a", "finished", status="ok", duration_s=1.0
                ),
            ],
            "Duplicate operation finish",
        ),
    ],
)
def test_stage_times_rejects_invalid_event_sequence(events, message):
    with pytest.raises(ValueError, match=message):
        summarize_stage_times(events, "run-01", "extract")


@pytest.mark.parametrize("operation_id", [None, "", "  "])
def test_stage_times_requires_operation_id(operation_id):
    events = [make_timed_event(operation_id, "started")]

    with pytest.raises(ValueError, match="operation_id"):
        summarize_stage_times(events, "run-01", "extract")


@pytest.mark.parametrize(
    "key, first, second",
    [
        ("arxiv_id", "a", "b"),
        ("version", 1, 2),
    ],
)
def test_stage_times_rejects_mismatched_context(key, first, second):
    events = [
        make_timed_event("a", "started", **{key: first}),
        make_timed_event(
            "a",
            "finished",
            status="ok",
            duration_s=1.0,
            **{key: second},
        ),
    ]

    with pytest.raises(ValueError, match=f"Mismatched {key}"):
        summarize_stage_times(events, "run-01", "extract")


@pytest.mark.parametrize("duration", [None, True, "1"])
def test_stage_times_rejects_invalid_duration_types(duration):
    events = [
        make_timed_event("a", "started"),
        make_timed_event(
            "a", "finished", status="ok", duration_s=duration
        ),
    ]

    with pytest.raises(TypeError):
        summarize_stage_times(events, "run-01", "extract")


@pytest.mark.parametrize(
    "duration", [-1, float("nan"), float("inf")]
)
def test_stage_times_rejects_invalid_duration_values(duration):
    events = [
        make_timed_event("a", "started"),
        make_timed_event(
            "a", "finished", status="ok", duration_s=duration
        ),
    ]

    with pytest.raises(ValueError):
        summarize_stage_times(events, "run-01", "extract")


def test_stage_times_rejects_unknown_outcome():
    events = [
        make_timed_event("a", "started"),
        make_timed_event(
            "a", "finished", status="empty", duration_s=1.0
        ),
    ]

    with pytest.raises(ValueError, match="Timed outcome"):
        summarize_stage_times(events, "run-01", "extract")

def test_log_resource_results_writes_samples_and_summary(tmp_path):
    output = tmp_path / "corpus"
    output.mkdir()
    (output / "sample.tex").write_bytes(b"abc")

    samples = [
        {"elapsed_s": 0.0, "rss_bytes": 100, "disk_bytes": 10},
        {"elapsed_s": 2.0, "rss_bytes": 200, "disk_bytes": 30},
    ]
    sampler = SimpleNamespace(
        samples=samples,
        summary=lambda: {
            "sample_count": 2,
            "sampled_duration_s": 2.0,
            "rss_peak_bytes": 200,
            "rss_mean_bytes": 100.0,
            "disk_peak_bytes": 30,
        },
    )
    logger = EventLogger(tmp_path / "events.jsonl", run_id="run-01")

    result = log_resource_results(logger, sampler, output)
    events = read_events(logger.path)

    assert len(events) == 3
    assert [event["event"] for event in events] == [
        "sample", "sample", "summary"
    ]
    assert all(event["stage"] == "resources" for event in events)
    assert events[0]["elapsed_s"] == 0.0
    assert events[1]["rss_bytes"] == 200
    assert events[-1]["output_storage_bytes"] == 3
    assert result["disk_peak_bytes"] == 30
    assert result["output_storage_bytes"] == 3


def test_log_resource_results_measures_only_output_directory(tmp_path):
    output = tmp_path / "corpus"
    output.mkdir()
    (output / "main.tex").write_bytes(b"abc")
    (tmp_path / "temporary.bin").write_bytes(b"x" * 100)

    sampler = SimpleNamespace(
        samples=[],
        summary=lambda: {
            "sample_count": 0,
            "sampled_duration_s": 0.0,
            "rss_peak_bytes": None,
            "rss_mean_bytes": None,
            "disk_peak_bytes": None,
        },
    )
    logger = EventLogger(tmp_path / "events.jsonl", run_id="run-01")

    result = log_resource_results(logger, sampler, output)

    assert result["output_storage_bytes"] == 3
    assert result["rss_peak_bytes"] is None
    assert len(read_events(logger.path)) == 1


def test_log_resource_results_missing_output_does_not_write_log(tmp_path):
    sampler = SimpleNamespace(
        samples=[],
        summary=lambda: {"sample_count": 0},
    )
    logger = EventLogger(tmp_path / "events.jsonl", run_id="run-01")

    with pytest.raises(NotADirectoryError):
        log_resource_results(logger, sampler, tmp_path / "missing")

    assert not logger.path.exists()

def test_request_hook_logs_context_and_maps_duration(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, run_id="run-01")
    hook = make_request_hook(
        logger, "references", arxiv_id="2507.11659"
    )

    hook(
        attempt=1,
        http_status=429,
        error_code="HTTP_429",
        duration=0.8,
        error_message="Too many requests",
    )

    events = read_events(path)
    assert len(events) == 1

    event = events[0]
    assert event["run_id"] == "run-01"
    assert event["stage"] == "references"
    assert event["event"] == "request_attempt_finished"
    assert event["arxiv_id"] == "2507.11659"
    assert event["version"] is None
    assert event["attempt"] == 1
    assert event["http_status"] == 429
    assert event["error_code"] == "HTTP_429"
    assert event["error_message"] == "Too many requests"
    assert event["duration_s"] == pytest.approx(0.8)
    assert "duration" not in event


def test_request_hook_records_each_attempt(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, run_id="run-01")
    hook = make_request_hook(
        logger, "download", arxiv_id="2507.11659", version=2
    )

    hook(
        attempt=1,
        http_status=None,
        error_code="TIMEOUT",
        duration=2.0,
        error_message="Request timed out",
    )
    hook(
        attempt=2,
        http_status=200,
        error_code=None,
        duration=0.5,
        error_message=None,
    )

    events = read_events(path)

    assert [event["attempt"] for event in events] == [1, 2]
    assert all(event["version"] == 2 for event in events)
    assert events[0]["http_status"] is None
    assert events[1]["error_code"] is None
    assert events[1]["error_message"] is None


@pytest.mark.parametrize(
    "attempt, error_type",
    [
        (0, ValueError),
        (-1, ValueError),
        (True, TypeError),
        (1.5, TypeError),
    ],
)
def test_request_hook_rejects_invalid_attempt(
    tmp_path, attempt, error_type
):
    path = tmp_path / "events.jsonl"
    hook = make_request_hook(
        EventLogger(path, "run-01"), "references"
    )

    with pytest.raises(error_type):
        hook(
            attempt=attempt,
            http_status=200,
            error_code=None,
            duration=0.1,
            error_message=None,
        )

    assert not path.exists()


@pytest.mark.parametrize(
    "duration, error_type",
    [
        (-1, ValueError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        (True, TypeError),
        ("0.1", TypeError),
        (None, TypeError),
    ],
)
def test_request_hook_rejects_invalid_duration(
    tmp_path, duration, error_type
):
    path = tmp_path / "events.jsonl"
    hook = make_request_hook(
        EventLogger(path, "run-01"), "references"
    )

    with pytest.raises(error_type):
        hook(
            attempt=1,
            http_status=200,
            error_code=None,
            duration=duration,
            error_message=None,
        )

    assert not path.exists()

def test_collect_environment_returns_expected_values(monkeypatch):
    monkeypatch.setattr(
        metrics.platform, "python_version", lambda: "3.12.7"
    )
    monkeypatch.setattr(
        metrics.platform, "python_implementation", lambda: "CPython"
    )
    monkeypatch.setattr(metrics.platform, "system", lambda: "TestOS")
    monkeypatch.setattr(metrics.platform, "release", lambda: "1.0")
    monkeypatch.setattr(metrics.platform, "machine", lambda: "test64")
    monkeypatch.setattr(metrics.sys, "platform", "test-platform")

    monkeypatch.setattr(
        metrics.psutil,
        "cpu_count",
        lambda logical=True: 12 if logical else 8,
    )
    monkeypatch.setattr(
        metrics.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=16 * 1024**3),
    )
    monkeypatch.setattr(
        metrics, "package_version", lambda name: "test-version"
    )

    result = collect_environment()

    assert result == {
        "python_version": "3.12.7",
        "python_implementation": "CPython",
        "system": "TestOS",
        "system_release": "1.0",
        "machine": "test64",
        "logical_cpu_count": 12,
        "physical_cpu_count": 8,
        "total_ram_bytes": 16 * 1024**3,
        "psutil_version": "test-version",
        "platform": "test-platform",
    }

    # Must be writable as JSON without custom conversion.
    json.dumps(result, allow_nan=False)


def test_collect_environment_preserves_unknown_cpu_counts(monkeypatch):
    monkeypatch.setattr(
        metrics.psutil, "cpu_count", lambda logical=True: None
    )

    result = collect_environment()

    assert result["logical_cpu_count"] is None
    assert result["physical_cpu_count"] is None

def test_build_run_report_combines_metrics(complete_paper_record):
    record = deepcopy(complete_paper_record)
    record.update({
        "size_measurement_complete": True,
        "unpacked_total_bytes": 100,
        "kept_source_bytes": 25,
    })
    environment = {"system": "TestOS"}
    resources = {"rss_peak_bytes": 1000}

    events = [
        make_timed_event("a", "started"),
        make_timed_event(
            "a", "finished", status="ok", duration_s=2.0
        ),
    ]

    report = build_run_report(
        run_id="run-01",
        variant="baseline",
        input_ids=["paper-a"],
        paper_results={"paper-a": record},
        events=events,
        resource_summary=resources,
        environment=environment,
    )

    assert report["report_schema_version"] == "1.0"
    assert report["run_id"] == "run-01"
    assert report["variant"] == "baseline"
    assert report["papers"]["paper_completion_rate"] == 1.0
    assert report["versions"]["processed_version_count"] == 1
    assert report["references"]["reference_empty_paper_count"] == 1
    assert report["source_sizes"]["source_reduction_rate"] == pytest.approx(0.75)
    assert report["timing"]["extract"]["duration_mean_s"] == pytest.approx(2.0)
    assert report["resources"] == resources
    assert report["environment"] == environment

    json.dumps(report, allow_nan=False)


def test_build_run_report_retains_unprocessed_papers():
    report = build_run_report(
        run_id="run-01",
        variant="baseline",
        input_ids=["a", "b"],
        paper_results={},
        events=[],
        resource_summary={},
        environment={},
    )

    assert report["papers"]["incomplete_paper_count"] == 2
    assert report["references"]["reference_status_counts"]["pending"] == 2
    assert report["versions"]["unknown_versions_paper_count"] == 2
    assert report["source_sizes"]["size_unmeasured_paper_count"] == 2
    assert report["timing"]["pipeline"]["duration_mean_s"] is None


def test_build_run_report_filters_timing_by_run():
    events = [
        make_timed_event("a", "started", run_id="other-run"),
        make_timed_event(
            "a",
            "finished",
            run_id="other-run",
            status="ok",
            duration_s=99.0,
        ),
    ]

    report = build_run_report(
        run_id="run-01",
        variant="baseline",
        input_ids=[],
        paper_results={},
        events=events,
        resource_summary={},
        environment={},
    )

    assert report["timing"]["extract"]["finished_operation_count"] == 0


def test_build_run_report_accepts_generators():
    report = build_run_report(
        run_id="run-01",
        variant="baseline",
        input_ids=(paper_id for paper_id in ["a"]),
        paper_results={},
        events=(event for event in []),
        resource_summary={},
        environment={},
    )

    assert report["papers"]["input_paper_count"] == 1
    assert report["references"]["input_paper_count"] == 1


def test_build_run_report_copies_environment_and_resources():
    environment = {"system": "TestOS"}
    resources = {"rss_peak_bytes": 100}

    report = build_run_report(
        run_id="run-01",
        variant="baseline",
        input_ids=[],
        paper_results={},
        events=[],
        resource_summary=resources,
        environment=environment,
    )

    report["environment"]["system"] = "Changed"
    report["resources"]["rss_peak_bytes"] = 999

    assert environment["system"] == "TestOS"
    assert resources["rss_peak_bytes"] == 100

def test_write_report_creates_directory_and_preserves_data(tmp_path):
    path = tmp_path / "nested" / "metrics.json"
    report = {
        "run_id": "run-01",
        "description": "Dữ liệu kiểm thử",
        "mean": None,
        "rate": 0.5,
    }

    write_run_report(path, report)

    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert list(path.parent.glob("*.tmp")) == []


def test_write_report_replaces_existing_report(tmp_path):
    path = tmp_path / "metrics.json"
    write_run_report(path, {"run_id": "old"})
    write_run_report(path, {"run_id": "new"})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "run_id": "new"
    }


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")]
)
def test_write_report_invalid_number_preserves_old_file(tmp_path, value):
    path = tmp_path / "metrics.json"
    write_run_report(path, {"run_id": "old"})
    before = path.read_bytes()

    with pytest.raises(ValueError):
        write_run_report(path, {"value": value})

    assert path.read_bytes() == before


def test_write_report_serialization_error_preserves_old_file(tmp_path):
    path = tmp_path / "metrics.json"
    write_run_report(path, {"run_id": "old"})
    before = path.read_bytes()

    with pytest.raises(TypeError):
        write_run_report(path, {"invalid": object()})

    assert path.read_bytes() == before


def test_write_report_replace_failure_preserves_old_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "metrics.json"
    write_run_report(path, {"run_id": "old"})
    before = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("replacement failed")

    monkeypatch.setattr(metrics.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        write_run_report(path, {"run_id": "new"})

    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("report", [None, [], "invalid"])
def test_write_report_rejects_non_dictionary(tmp_path, report):
    path = tmp_path / "metrics.json"

    with pytest.raises(TypeError, match="dictionary"):
        write_run_report(path, report)

    assert not path.exists()

def test_request_hook_keeps_id_across_attempts(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, "run-01")
    hook = make_request_hook(logger, "references")

    for attempt in (1, 2):
        hook(
            attempt=attempt,
            http_status=429,
            error_code="HTTP_429",
            duration=0.1,
            error_message="Too many requests",
        )

    events = read_events(path)

    assert events[0]["request_id"]
    assert events[0]["request_id"] == events[1]["request_id"]
    assert [event["attempt"] for event in events] == [1, 2]


def test_request_hooks_generate_distinct_ids(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, "run-01")

    for _ in range(2):
        hook = make_request_hook(logger, "references")
        hook(
            attempt=1,
            http_status=200,
            error_code=None,
            duration=0.1,
            error_message=None,
        )

    events = read_events(path)

    assert events[0]["request_id"] != events[1]["request_id"]


def test_request_hook_preserves_explicit_request_id(tmp_path):
    path = tmp_path / "events.jsonl"
    hook = make_request_hook(
        EventLogger(path, "run-01"),
        "references",
        request_id="request-001",
    )

    hook(
        attempt=1,
        http_status=200,
        error_code=None,
        duration=0.1,
        error_message=None,
    )

    assert read_events(path)[0]["request_id"] == "request-001"


@pytest.mark.parametrize("request_id", ["", "  ", 123, True])
def test_request_hook_rejects_invalid_request_id(tmp_path, request_id):
    logger = EventLogger(tmp_path / "events.jsonl", "run-01")

    with pytest.raises(ValueError, match="request_id"):
        make_request_hook(
            logger, "references", request_id=request_id
        )

def make_attempt_event(request_id, attempt, **fields):
    return {
        "run_id": "run-01",
        "stage": "references",
        "event": "request_attempt_finished",
        "request_id": request_id,
        "attempt": attempt,
        "duration_s": 0.1,
        "error_code": None,
        **fields,
    }


def test_request_summary_counts_retries_and_errors():
    events = [
        make_attempt_event("a", 1, error_code="HTTP_429", duration_s=0.2),
        make_attempt_event("a", 2, duration_s=0.4),
        make_attempt_event("b", 1, error_code="TIMEOUT", duration_s=0.3),
    ]

    result = summarize_request_attempts(events, "run-01")

    assert result["observed_request_count"] == 2
    assert result["observed_attempt_count"] == 3
    assert result["observed_retry_attempt_count"] == 1
    assert result["error_attempt_count"] == 2
    assert result["error_code_counts"] == {"HTTP_429": 1, "TIMEOUT": 1}
    assert result["requests_with_missing_attempts"] == 0
    assert result["attempt_duration_sum_s"] == pytest.approx(0.9)
    assert result["attempt_duration_mean_s"] == pytest.approx(0.3)


def test_request_summary_detects_missing_attempts():
    events = [
        make_attempt_event("a", 1),
        make_attempt_event("a", 3),
        make_attempt_event("b", 2),
    ]

    result = summarize_request_attempts(events, "run-01")

    assert result["requests_with_missing_attempts"] == 2
    assert result["observed_retry_attempt_count"] == 2


def test_request_summary_filters_run_and_event():
    events = [
        make_attempt_event("a", 1, run_id="other-run"),
        make_attempt_event("b", 1, event="started"),
    ]

    result = summarize_request_attempts(events, "run-01")

    assert result["observed_attempt_count"] == 0
    assert result["attempt_duration_mean_s"] is None


def test_request_summary_empty():
    result = summarize_request_attempts([], "run-01")

    assert result["observed_request_count"] == 0
    assert result["observed_attempt_count"] == 0
    assert result["attempt_duration_mean_s"] is None


def test_request_summary_rejects_duplicate_attempt():
    events = [
        make_attempt_event("a", 1),
        make_attempt_event("a", 1),
    ]

    with pytest.raises(ValueError, match="Duplicate request attempt"):
        summarize_request_attempts(events, "run-01")


@pytest.mark.parametrize(
    "changed_context",
    [
        {"stage": "download"},
        {"arxiv_id": "another-paper"},
        {"version": 2},
    ],
)
def test_request_summary_rejects_inconsistent_context(changed_context):
    events = [
        make_attempt_event("a", 1),
        make_attempt_event("a", 2, **changed_context),
    ]

    with pytest.raises(ValueError, match="inconsistent context"):
        summarize_request_attempts(events, "run-01")


@pytest.mark.parametrize("request_id", [None, "", "  "])
def test_request_summary_requires_request_id(request_id):
    with pytest.raises(ValueError, match="request_id"):
        summarize_request_attempts(
            [make_attempt_event(request_id, 1)], "run-01"
        )


@pytest.mark.parametrize("attempt", [0, -1])
def test_request_summary_rejects_invalid_attempt_values(attempt):
    with pytest.raises(ValueError):
        summarize_request_attempts(
            [make_attempt_event("a", attempt)], "run-01"
        )


@pytest.mark.parametrize("attempt", [True, 1.5])
def test_request_summary_rejects_invalid_attempt_types(attempt):
    with pytest.raises(TypeError):
        summarize_request_attempts(
            [make_attempt_event("a", attempt)], "run-01"
        )


@pytest.mark.parametrize("duration", [-1, float("nan"), float("inf")])
def test_request_summary_rejects_invalid_duration_values(duration):
    with pytest.raises(ValueError):
        summarize_request_attempts(
            [make_attempt_event("a", 1, duration_s=duration)], "run-01"
        )


@pytest.mark.parametrize("duration", [None, True, "1"])
def test_request_summary_rejects_invalid_duration_types(duration):
    with pytest.raises(TypeError):
        summarize_request_attempts(
            [make_attempt_event("a", 1, duration_s=duration)], "run-01"
        )


@pytest.mark.parametrize("error_code", ["", "  ", 123])
def test_request_summary_rejects_invalid_error_code(error_code):
    with pytest.raises(ValueError):
        summarize_request_attempts(
            [make_attempt_event("a", 1, error_code=error_code)], "run-01"
        )

def test_build_run_report_includes_request_summary():
    events = [
        make_attempt_event(
            "request-a",
            1,
            error_code="HTTP_429",
            duration_s=0.2,
        ),
        make_attempt_event(
            "request-a",
            2,
            duration_s=0.4,
        ),
        make_attempt_event(
            "request-b",
            1,
            run_id="other-run",
        ),
    ]

    report = build_run_report(
        run_id="run-01",
        variant="baseline",
        input_ids=[],
        paper_results={},
        events=events,
        resource_summary={},
        environment={},
    )

    requests = report["requests"]

    assert requests["observed_request_count"] == 1
    assert requests["observed_attempt_count"] == 2
    assert requests["observed_retry_attempt_count"] == 1
    assert requests["error_attempt_count"] == 1
    assert requests["error_code_counts"] == {"HTTP_429": 1}
    assert requests["attempt_duration_sum_s"] == pytest.approx(0.6)