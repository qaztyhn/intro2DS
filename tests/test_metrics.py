import pytest

from src.metrics import mean_or_none, safe_ratio
import src.metrics as metrics
from src.metrics import Timer
from src.metrics import directory_size_bytes
from types import SimpleNamespace
from src.metrics import process_tree_rss_bytes
from src.metrics import summarize_rss_samples
from src.metrics import ResourceSampler

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