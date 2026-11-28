# Pilot20 baseline

These measurements come from commit `57d27734a01862a88e0d30175595893b5a701d6c`.
The pilot covers the 20 publications in `m1/configs/ids.txt`
(`2507.01659` through `2507.01678`) and their 34 versions in
`experiments/B_test01`. The six files below were copied byte-for-byte from
`/private/tmp/m1m2-pilot.HQg3Ho/`.

| File | Contents |
| --- | --- |
| `compare.tsv` | One row per publication and version, comparing parse status, selected root, bibliography keys and diagnostics. The `archive_sha256` column identifies the input archive for that row. All 34 digests were checked against the archive at `archive_path` in the corresponding `B_test01` version manifest. |
| `preserve.json` | Detailed per-version parser results for preserved and converted source assets, including bibliography entries and cited keys. |
| `texbib.json` | The same measurements with extraction limited to original `.tex` and `.bib` files. |
| `without_bbl_keys.json` | Bibliography keys by publication and version after removing BBL inputs before conversion. This comparison isolates key gains attributable to BBL. |
| `audit.json` | M1 source audit for the preserve run, including per-version findings and retry candidates. |
| `scan.py` | Original inspection script. It requires the pilot's adjacent `preserve/` tree and writes `graphics.tsv`, `filtered.tsv` and `mentions.tsv` beside the script. |
