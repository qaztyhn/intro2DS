"""Shared B0/A/B measurement contract for comparison, fixtures and execution."""

VARIANTS = ("B0", "A", "B")
PROTOCOL = "external-process-wall-rss-tree-run-disk-v1"
CONFIG_NAMES = {"B0": "B", "A": "A_BATCH", "B": "B"}
POLICY_KEYS = ("timeout", "max_retries", "backoff_base_seconds",
               "arxiv_delay_seconds", "s2_max_retries", "s2_backoff_base_seconds",
               "s2_min_interval_seconds", "s2_credentials_present", "user_agent")
DEFAULTS = {"timeout": 30, "max_retries": 3, "backoff_base_seconds": 2.0,
            "arxiv_delay_seconds": 3, "s2_max_retries": 3, "s2_backoff_base_seconds": 5,
            "schema_version": 1,
            "user_agent": "Intro2DS-arXiv-crawler/1.0 (student project, HCMUS; contact: 24120058@student.hcmus.edu.vn)"}
