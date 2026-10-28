# SYNTHETIC fixture comparison — not measured performance

Benchmark valid: **True**

Conditions verified. Interpret speedups alongside completion and content differences.

| Metric | B0 | A | B |
|---|---:|---:|---:|
| run_id | B0 | A | B |
| run_status | completed | completed | completed |
| cache_mode | cold | cold | cold |
| wall_seconds | 12 | 8 | 6 |
| wall_seconds_per_input_paper | 6 | 4 | 3 |
| mean_paper_processing_seconds | 6 | 4 | 3 |
| rss_peak_bytes | 4096 | 4096 | 4096 |
| rss_mean_bytes | 2048 | 2048 | 2048 |
| disk_peak_bytes | 8192 | 8192 | 8192 |
| final_output_bytes | 1886 | 1886 | 1886 |
| complete_papers | 2 | 2 | 2 |
| failed_papers | 0 | 0 | 0 |
| unresolved_papers | 0 | 0 | 0 |
| paper_completion_rate | 1 | 1 | 1 |
| expected_versions | 2 | 2 | 2 |
| processed_versions | 2 | 2 | 2 |
| version_coverage | 1 | 1 | 1 |
| reference_retrieval_success_rate | 1 | 1 | 1 |
| valid_references | 0 | 0 | 0 |
| observed_http_429 | 2 | 2 | 2 |
| observed_retries | 2 | 2 | 2 |
| schema_status | ok | ok | ok |
| speedup_vs_B0 | N/A | 1.5 | 2 |

Stage durations may overlap; never sum them to infer pipeline wall time.
Mean paper processing time has variant-specific boundaries (A excludes batch discovery).
429/retry counts cover observed callbacks only; arxiv.Client internal attempts are unobserved.
N/A means unavailable, not zero. Final output bytes cover papers/ only.

## Benchmark issues

None.

## Output differences

- A expected_versions: 2 equal; changed=[]; unobserved=[]
- A metadata_sha256: 2 equal; changed=[]; unobserved=[]
- A source_sha256: 2 equal; changed=[]; unobserved=[]
- A references_sha256: 2 equal; changed=[]; unobserved=[]
- B expected_versions: 2 equal; changed=[]; unobserved=[]
- B metadata_sha256: 2 equal; changed=[]; unobserved=[]
- B source_sha256: 2 equal; changed=[]; unobserved=[]
- B references_sha256: 2 equal; changed=[]; unobserved=[]
