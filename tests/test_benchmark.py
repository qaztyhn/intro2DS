import pytest

from src.benchmark import expand_range
from src.benchmark import select_sample
from copy import deepcopy

from src.benchmark import select_benchmark
from src.benchmark import select_pilot
import xml.etree.ElementTree as ET

from src.benchmark import parse_discovery_response

def make_range(start_month, start_id, end_month, end_id):
    return {
        "start_month": start_month,
        "start_id": start_id,
        "end_month": end_month,
        "end_id": end_id,
    }


def test_expand_same_month_includes_both_bounds():
    specification = make_range("2025-07", 1659, "2025-07", 1661)

    assert expand_range(specification, {}) == [
        "2507.01659",
        "2507.01660",
        "2507.01661",
    ]


def test_expand_crosses_month():
    specification = make_range("2025-07", 3, "2025-08", 2)

    assert expand_range(specification, {"2025-07": 4}) == [
        "2507.00003",
        "2507.00004",
        "2508.00001",
        "2508.00002",
    ]


def test_expand_crosses_year():
    specification = make_range("2025-12", 2, "2026-01", 1)

    assert expand_range(specification, {"2025-12": 3}) == [
        "2512.00002",
        "2512.00003",
        "2601.00001",
    ]


def test_expand_requires_month_boundary():
    specification = make_range("2025-07", 3, "2025-08", 2)

    with pytest.raises(ValueError, match="Verified last ID"):
        expand_range(specification, {})


@pytest.mark.parametrize(
    "specification",
    [
        make_range("2025-08", 1, "2025-07", 2),
        make_range("2025-07", 3, "2025-07", 2),
        make_range("2025-07", 0, "2025-07", 2),
        make_range("2025-07", 1, "2025-07", 100000),
    ],
)
def test_expand_rejects_invalid_bounds(specification):
    with pytest.raises(ValueError):
        expand_range(specification, {})


def test_expand_rejects_start_above_month_boundary():
    specification = make_range("2025-07", 5, "2025-08", 2)

    with pytest.raises(ValueError, match="Invalid sequence bounds"):
        expand_range(specification, {"2025-07": 4})

def test_select_sample_is_reproducible_and_order_independent():
    candidates = [f"2507.{number:05d}" for number in range(1, 101)]

    first = select_sample(candidates, 10, seed=42)
    second = select_sample(list(reversed(candidates)), 10, seed=42)

    assert first == second
    assert len(first) == 10
    assert len(set(first)) == 10
    assert set(first).issubset(candidates)
    assert first == sorted(first)


def test_select_sample_does_not_modify_input():
    candidates = ["2507.00003", "2507.00001", "2507.00002"]
    original = candidates.copy()

    select_sample(candidates, 2, seed=42)

    assert candidates == original


def test_select_sample_does_not_change_global_random_state():
    import random

    before = random.getstate()
    select_sample(["a", "b", "c"], 2, seed=42)

    assert random.getstate() == before


@pytest.mark.parametrize(
    "candidates, size, expected",
    [
        ([], 0, []),
        (["b", "a"], 0, []),
        (["b", "a"], 2, ["a", "b"]),
    ],
)
def test_select_sample_boundaries(candidates, size, expected):
    assert select_sample(candidates, size, seed=42) == expected


@pytest.mark.parametrize(
    "candidates",
    [["a", "a"], [""], ["  "], [None]],
)
def test_select_sample_rejects_invalid_candidates(candidates):
    with pytest.raises(ValueError):
        select_sample(candidates, 1, seed=42)


@pytest.mark.parametrize("size", [-1, 3])
def test_select_sample_rejects_invalid_size_values(size):
    with pytest.raises(ValueError):
        select_sample(["a", "b"], size, seed=42)


@pytest.mark.parametrize("size", [True, 1.5])
def test_select_sample_rejects_invalid_size_types(size):
    with pytest.raises(TypeError):
        select_sample(["a", "b"], size, seed=42)


@pytest.mark.parametrize("seed", [True, "42"])
def test_select_sample_rejects_invalid_seed_types(seed):
    with pytest.raises(TypeError):
        select_sample(["a", "b"], 1, seed=seed)

@pytest.fixture
def sample_benchmark_config():
    return {
        "seed": 42,
        "bounds_inclusive": True,
        "benchmark_size": 4,
        "ranges": [
            {
                "range_id": "A",
                **make_range("2025-07", 1, "2025-07", 10),
                "sample_size": 2,
            },
            {
                "range_id": "B",
                **make_range("2025-08", 1, "2025-08", 10),
                "sample_size": 2,
            },
        ],
    }


def test_select_benchmark_sizes_and_membership(sample_benchmark_config):
    selected = select_benchmark(sample_benchmark_config, {})

    assert set(selected) == {"A", "B"}
    assert sum(map(len, selected.values())) == 4

    for specification in sample_benchmark_config["ranges"]:
        range_id = specification["range_id"]
        candidates = expand_range(specification, {})

        assert len(selected[range_id]) == 2
        assert len(set(selected[range_id])) == 2
        assert set(selected[range_id]).issubset(candidates)


def test_select_benchmark_reproducible_and_order_independent(
    sample_benchmark_config,
):
    original = deepcopy(sample_benchmark_config)
    first = select_benchmark(sample_benchmark_config, {})

    reversed_config = deepcopy(sample_benchmark_config)
    reversed_config["ranges"].reverse()
    second = select_benchmark(reversed_config, {})

    assert first == second
    assert sample_benchmark_config == original


def test_select_benchmark_rejects_duplicate_range_ids(
    sample_benchmark_config,
):
    sample_benchmark_config["ranges"][1]["range_id"] = "A"

    with pytest.raises(ValueError, match="Range IDs must be unique"):
        select_benchmark(sample_benchmark_config, {})


def test_select_benchmark_rejects_overlapping_ranges(
    sample_benchmark_config,
):
    second = sample_benchmark_config["ranges"][1]
    second.update(make_range("2025-07", 5, "2025-07", 15))

    with pytest.raises(ValueError, match="must not overlap"):
        select_benchmark(sample_benchmark_config, {})


def test_select_benchmark_rejects_incorrect_total(
    sample_benchmark_config,
):
    sample_benchmark_config["benchmark_size"] = 5

    with pytest.raises(ValueError, match="Expected 5 selected IDs, got 4"):
        select_benchmark(sample_benchmark_config, {})


def test_select_benchmark_requires_cross_month_boundary(
    sample_benchmark_config,
):
    second = sample_benchmark_config["ranges"][1]
    second.update(make_range("2025-08", 1, "2025-09", 2))

    with pytest.raises(ValueError, match="Verified last ID"):
        select_benchmark(sample_benchmark_config, {})


def test_select_benchmark_uses_supplied_month_boundary(
    sample_benchmark_config,
):
    second = sample_benchmark_config["ranges"][1]
    second.update(make_range("2025-08", 9, "2025-09", 2))

    selected = select_benchmark(
        sample_benchmark_config, {"2025-08": 10}
    )

    assert set(selected["B"]).issubset({
        "2508.00009",
        "2508.00010",
        "2509.00001",
        "2509.00002",
    })


def test_select_benchmark_requires_inclusive_bounds(
    sample_benchmark_config,
):
    sample_benchmark_config["bounds_inclusive"] = False

    with pytest.raises(ValueError, match="inclusive"):
        select_benchmark(sample_benchmark_config, {})


@pytest.mark.parametrize("seed", [True, "42"])
def test_select_benchmark_rejects_invalid_seed(
    sample_benchmark_config, seed
):
    sample_benchmark_config["seed"] = seed

    with pytest.raises(TypeError):
        select_benchmark(sample_benchmark_config, {})


@pytest.mark.parametrize("size", [True, 4.5])
def test_select_benchmark_rejects_invalid_total_type(
    sample_benchmark_config, size
):
    sample_benchmark_config["benchmark_size"] = size

    with pytest.raises(TypeError):
        select_benchmark(sample_benchmark_config, {})

@pytest.fixture
def synthetic_benchmark():
    return {
        f"R{group}": [
            f"synthetic-{group}-{number:03d}"
            for number in range(250)
        ]
        for group in range(1, 5)
    }


def test_pilot_has_equal_allocation_and_is_subset(synthetic_benchmark):
    pilot = select_pilot(synthetic_benchmark, 40, seed=42)

    assert sum(map(len, pilot.values())) == 40

    for range_id, ids in pilot.items():
        assert len(ids) == 10
        assert len(set(ids)) == 10
        assert set(ids).issubset(synthetic_benchmark[range_id])


def test_pilot_is_reproducible_and_order_independent(synthetic_benchmark):
    original = deepcopy(synthetic_benchmark)
    first = select_pilot(synthetic_benchmark, 40, seed=42)

    reordered = {
        key: list(reversed(ids))
        for key, ids in reversed(list(synthetic_benchmark.items()))
    }
    second = select_pilot(reordered, 40, seed=42)

    assert first == second
    assert synthetic_benchmark == original


def test_pilot_rejects_shared_ids():
    with pytest.raises(ValueError, match="must not share IDs"):
        select_pilot(
            {"A": ["a", "b"], "B": ["b", "c"]},
            2,
            seed=42,
        )


def test_pilot_rejects_insufficient_candidates():
    with pytest.raises(ValueError, match="candidate count"):
        select_pilot(
            {"A": ["a"], "B": ["b", "c"]},
            4,
            seed=42,
        )


def test_pilot_rejects_empty_benchmark():
    with pytest.raises(ValueError, match="at least one range"):
        select_pilot({}, 40, seed=42)


@pytest.mark.parametrize("size", [0, -4, 3])
def test_pilot_rejects_invalid_allocation(synthetic_benchmark, size):
    with pytest.raises(ValueError, match="divisible"):
        select_pilot(synthetic_benchmark, size, seed=42)


@pytest.mark.parametrize("size", [True, 40.0])
def test_pilot_rejects_invalid_size_type(synthetic_benchmark, size):
    with pytest.raises(TypeError):
        select_pilot(synthetic_benchmark, size, seed=42)


@pytest.mark.parametrize("seed", [True, "42"])
def test_pilot_rejects_invalid_seed_type(synthetic_benchmark, seed):
    with pytest.raises(TypeError):
        select_pilot(synthetic_benchmark, 40, seed=seed)

def make_atom_entry(
    identifier,
    title="Synthetic title",
    published="2025-07-15T12:00:00Z",
    author="Example Author",
):
    return f"""
    <entry>
      <id>{identifier}</id>
      <title>{title}</title>
      <published>{published}</published>
      <author><name>{author}</name></author>
    </entry>
    """


def make_atom_feed(*entries):
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + "".join(entries)
        + "</feed>"
    ).encode("utf-8")


def test_parse_discovery_metadata_and_missing_id():
    response = make_atom_feed(
        make_atom_entry("http://arxiv.org/abs/2507.11659v2")
    )

    result = parse_discovery_response(
        response, ["2507.11659", "2507.11660"]
    )

    paper = result["papers"]["2507.11659"]
    assert paper["expected_versions"] == [1, 2]
    assert paper["latest_version"] == 2
    assert paper["authors"] == ["Example Author"]
    assert paper["verification_status"] == "metadata_present"
    assert result["missing_ids"] == ["2507.11660"]
    assert result["unexpected_ids"] == []


def test_parse_discovery_matches_ids_not_response_order():
    response = make_atom_feed(
        make_atom_entry("https://arxiv.org/abs/2507.11660v1"),
        make_atom_entry("https://arxiv.org/abs/2507.11659v3"),
    )

    result = parse_discovery_response(
        response, ["2507.11659", "2507.11660"]
    )

    assert result["papers"]["2507.11659"]["latest_version"] == 3
    assert result["papers"]["2507.11660"]["latest_version"] == 1
    assert result["missing_ids"] == []


def test_parse_discovery_empty_feed_keeps_all_ids_missing():
    result = parse_discovery_response(
        make_atom_feed(), ["2507.11659"]
    )

    assert result["papers"] == {}
    assert result["missing_ids"] == ["2507.11659"]


def test_parse_discovery_records_unexpected_id():
    response = make_atom_feed(
        make_atom_entry("http://arxiv.org/abs/2507.11660v1")
    )

    result = parse_discovery_response(response, ["2507.11659"])

    assert result["papers"] == {}
    assert result["missing_ids"] == ["2507.11659"]
    assert result["unexpected_ids"] == ["2507.11660"]


@pytest.mark.parametrize(
    "field, expected_missing",
    [
        ("title", "title"),
        ("published", "submission_date"),
        ("author", "authors"),
    ],
)
def test_parse_discovery_marks_missing_metadata(field, expected_missing):
    response = make_atom_feed(
        make_atom_entry(
            "http://arxiv.org/abs/2507.11659v1",
            **{field: ""},
        )
    )

    result = parse_discovery_response(response, ["2507.11659"])
    paper = result["papers"]["2507.11659"]

    assert paper["verification_status"] == "metadata_incomplete"
    assert paper["missing_fields"] == [expected_missing]


def test_parse_discovery_rejects_duplicate_requested_ids():
    with pytest.raises(ValueError, match="Requested IDs must be unique"):
        parse_discovery_response(
            make_atom_feed(), ["2507.11659", "2507.11659"]
        )


def test_parse_discovery_rejects_duplicate_returned_ids():
    entry = make_atom_entry("http://arxiv.org/abs/2507.11659v1")

    with pytest.raises(ValueError, match="Duplicate returned ID"):
        parse_discovery_response(
            make_atom_feed(entry, entry), ["2507.11659"]
        )


@pytest.mark.parametrize(
    "identifier",
    [
        "http://arxiv.org/abs/2507.11659",
        "http://arxiv.org/abs/2507.11659v0",
        "http://arxiv.org/api/errors",
    ],
)
def test_parse_discovery_rejects_unusable_identifiers(identifier):
    with pytest.raises(ValueError, match="Unexpected entry identifier"):
        parse_discovery_response(
            make_atom_feed(make_atom_entry(identifier)),
            ["2507.11659"],
        )


def test_parse_discovery_rejects_non_feed_xml():
    with pytest.raises(ValueError, match="Expected an Atom feed"):
        parse_discovery_response(b"<html/>", ["2507.11659"])


def test_parse_discovery_rejects_malformed_xml():
    with pytest.raises(ET.ParseError):
        parse_discovery_response(b"<feed", ["2507.11659"])