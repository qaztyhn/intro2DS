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