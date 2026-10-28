"""Generate the shared ID lists."""

import json
from pathlib import Path

from .benchmark import expand_range, select_benchmark

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs"

# Range -> owner's student ID (from the assignment table).
OWNERS = {
    "R1": "24120085",
    "R2": "24120046",
    "R3": "24120058",
    "R4": "24120027",
}


def write_ids(path: Path, ids: list[str]) -> None:
    """One id per line, no header, trailing newline (so wc -l == len(ids))."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(ids) + "\n", encoding="utf-8")
    print(f"{path.relative_to(ROOT)}: {len(ids)} ids")


def main() -> None:
    spec = json.loads((CONFIGS / "benchmark.json").read_text(encoding="utf-8"))
    month_last_ids = spec["month_last_ids"]

    all_ids = []
    for rng in spec["ranges"]:
        ids = expand_range(rng, month_last_ids)
        if len(ids) != 5000:
            raise ValueError(f"{rng['range_id']}: expected 5000 ids, got {len(ids)}")
        write_ids(CONFIGS / "team" / f"ids_{OWNERS[rng['range_id']]}.txt", ids)
        all_ids.extend(ids)

    if len(all_ids) != len(set(all_ids)):
        raise ValueError("ranges overlap")
    write_ids(CONFIGS / "solo" / "ids_all.txt", all_ids)

    selected = select_benchmark(spec, month_last_ids)
    benchmark_ids = [paper_id for ids in selected.values() for paper_id in ids]
    write_ids(CONFIGS / "benchmark_ids.txt", benchmark_ids)


if __name__ == "__main__":
    main()