# TV2 labeling, quotas and publication splits

TV2 maintains labeling tools, validates member quotas and builds the shared group split. Each member supplies their own reviewed labels for at least five publications, with at least 20 matching entry–reference pairs per publication and 100 pairs in total.

The implementation follows the supplied M3 contract v6 and the instructor's QA reply supplied on 28 November 2026. The instructor clarified that labels represent matching pairs, entries without a matching reference are excluded from evaluation, and a publication used for manual labeling cannot enter the automatic pool. These clarifications resolve the broader all-negative-row wording on the current lab page. Unmatched or ambiguous entries therefore have no row in `labels`. Automatic predictions on reviewed manual entries are used only for the rule audit.

Sources are the supplied `Intro2DS_Project (1).md`, `Intro2DS_Project (2).md`, the instructor QA reply, and the [matching lab](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/matching/).

## Dependencies and integration

This branch starts from PR #41 at `6588c2f`, which supplies the shared `m3/src/contracts.py` v6. TV2 adds `labels.py`, `splits.py`, a standalone `labeling.py` command, tests and this document. PR #42 supplies catalog input and PR #43 supplies pair features. `pipeline.py` stage wiring belongs to TV4 and can call `run_labeling` after integration. The TV2 command is usable independently.

Install the repository dependencies, including PyArrow. Tests require pytest. Pair generation in PR #43 additionally uses rapidfuzz. All labeling calculations run offline on CPU. Fetching an existing dataset for the pilot is a preparation step.

The proposed function signatures lack the registries needed to protect manually registered publications with zero labels. These explicit keyword arguments carry the existing contract tables without changing their schemas.

| Function | Additional inputs |
| --- | --- |
| `load_manual_labels` | Optional paired `assignments`, `manual_publications`, always supplied by the CLI |
| `choose_auto_publications` | Required `manual_publications` |
| `label_automatically` | Required `assignments`, `manual_publications` |
| `split_publications` | Required `manual_publications`, `auto_publications`; `owners` is the assignments Arrow table |

## Input and output

The run input directory contains `assignments.parquet`, `manual_publications.parquet`, `auto_publications.parquet`, `queries.parquet`, `pairs.parquet` and `crawl_status.parquet`. Use the entire assigned range in assignments when computing quotas. Queries, pairs and crawl status come from TV1. Shared contract validation checks schemas, duplicate keys, ownership and disjoint registries. Join IDs are normalized, including version suffixes and legacy arXiv IDs.

`pairs` must be the unmasked baseline TV1 output, with `title_similarity`, `first_author_match`, `year_diff` and `arxiv_id_match`. Validate the full candidate Cartesian product with TV1's `validate_pairs` before handoff. The HF publishing format omits `crawl_status`. Supply the authoritative TV1 catalog or M1 manifests for production labeling.

Create a packet for the IDs registered to manual labeling.

```bash
python -m m3.src.labeling packet \
  --bibtex /data/hf/data/bibtex \
  --references /data/hf/data/references \
  --ids-file /data/id/24120046/manual_ids.txt \
  --output-dir /data/m3/manual-packet
```

`entries.csv` contains entry metadata and blank `groundtruth_arxiv_id` and `member_id` fields. `candidates.csv` contains reference metadata grouped by citing publication. Each person reviews their own packet and fills both label fields for confirmed matches. Keep both blank for unreviewed or unmatched entries. Candidate IDs must belong to that entry's publication. Identical repeated labels are deduplicated, contradictory labels fail with the entry key. Invalid or partially filled labels fail explicitly.

```bash
python -m m3.src.labeling run \
  --input-dir /data/m3/catalog-and-pairs \
  --manual-csv /data/reviewed/member1.csv /data/reviewed/member2.csv \
  --output-dir /data/m3/tv2-run-001 --seed 2026
```

The command writes `labels.parquet`, `auto_rule_report.json`, `coverage_by_paper.csv`, `quota_report.json` and `manifest.json`. Complete runs also write `splits.parquet`. Incomplete quotas produce a diagnostic run with exit status 2 and a specific quota report. Invalid labels or input schemas fail before publication of a run. A new output directory is required. Inputs are hashed before and after processing. Output files have hashes, seed and commit provenance in the manifest.

## Automatic rule and quota

`auto_rule_v1` uses one exact arXiv-ID hit when available. Multiple ID hits cause abstention. With no ID hit, it accepts exactly one candidate having title similarity at least 0.95, the same first-author surname, and equal or missing year. All other cases abstain. The rule shares title/author/year/ID signals with the model features. Report this dependence alongside manual-gold precision and coverage.

`choose_auto_publications` excludes the complete manual registry, intersects query availability, usable crawl and candidate availability, and samples sorted IDs using the supplied seed. The quota uses `ceil(0.10 * (assigned - manual))`, giving 500 for 5000 assigned and five manual publications. Insufficient eligible papers raise an error with the shortfall. The full run also checks a minimum of two automatic papers per member to supply valid and test. Preselected IDs can be supplied via `auto_publications`, with eligibility and quota checked by the runner.

Rule precision is correct predictions divided by predictions on reviewed gold entries. Coverage is predictions on reviewed gold entries divided by all reviewed gold entries. Missing denominators are reported as null. Predictions on unreviewed entries are excluded from precision, and the report retains their count. `audit_auto_rule` is the explicit diagnostic entry point for manual publications. Its predictions do not enter the training-label registry.

## Splits

The split is performed jointly at publication level. For each member and each pool, one labeled publication goes to test and one to valid, using seed 2026 by default. Remaining registered publications go to train, including automatic publications where the rule abstained on every entry. Valid/test require at least one matching label. The complete manual registry remains separate from the auto registry, including manually selected papers with zero labels. Labels, pool and owner are validated before splitting. Quota validation is performed by the run command before final split publication.

## Verification and pilot on 28 November 2026

The TV2 tests cover T2-1 through T2-8, normalized-key collisions, owner rejection, ambiguous automatic matches, zero-label registry handling, quota shortfall, deterministic splits, report denominators and the complete command workflow. Synthetic fixtures are used for reviewed labels. All 13 TV2 tests and all 33 tests in the combined PR #41–#43 checkout passed, including HF streaming with offline mode enabled. The shared contract self-test also checks MRR@5 = 0.75. Real manual accuracy remains unavailable until members supply reviewed labels.

The pilot used the user-confirmed HF dataset `minhh-45/intro2ds-24120046` at revision `c7e62fb85cf45b055bdc2d6003e5442c48f2fd5a`, with original selections from `id.zip`. It reused `build_pair_batch` from PR #43. The original ZIP exceeds the tool's 32 MiB download limit, so the confirmed HF copy was used.

| Measurement | Result |
| --- | --- |
| Selected papers processed | 505 / 505, 100% |
| Runtime on this CPU environment | 32.40 seconds |
| Processing exceptions | 0 |
| Auto entries inspected | 27,907 |
| Provisional matching pairs | 4,912, 17.60% of auto entries |
| Auto papers with at least one provisional match | 397 / 500, 79.40% |
| Auto papers without candidates | 85 / 500, 17.00% |
| Auto papers with candidates but no rule match | 18 / 500, 3.60% |
| Gold manual labels supplied | 0 |
| Measurable real-data rule precision | Unavailable |

HF has no authoritative crawl status. The sensitivity pilot assumed `ok` for nonempty reference lists and wrote explicitly provisional auto matches. These assumptions require confirmation from the TV1 catalog before production use. The 415 auto publications with candidates represent 83% of the planned 500 eligible publications. The 100 auto reserve IDs include 88 with entries and candidates, which is enough to propose replacements for the 85 missing-candidate selections after checking crawl status. Original registries are preserved for review.

| Manual publication | Entries | Candidates |
| --- | --- | --- |
| 2507.11865 | 49 | 12 |
| 2507.13803 | 57 | 10 |
| 2507.14500 | 75 | 0 |
| 2507.15761 | 68 | 31 |
| 2507.15901 | 58 | 24 |

`2507.14500` cannot supply a matching pair from this snapshot. The other four publications still require manual review to establish the minimum 20 pairs each. Candidate counts alone do not establish match counts, and several distinct entries may map to one reference. The manual reserve IDs with at least 20 candidates include `2507.15264`, `2507.14129`, `2507.13040`, `2507.12998` and `2507.14677`. Selection must be based on confirmed matching pairs.

The supplied four-member ID lists each have five manual and 500 automatic papers, disjoint manual/auto selections, consistent selected lists and no cross-member selected-ID overlap. This verifies those files structurally. It does not establish full-range ownership or other members' data readiness.

TV2 tooling is implemented and tested. The real-data G3 handoff remains pending authoritative crawl status, eligible replacements, reviewed manual labels and the final joint group run. `labels.parquet` and `splits.parquet` for the complete group require those inputs. Model fitting, MRR evaluation of a trained model and HF publication belong to the other assigned roles.
