"""Load and validate config.yaml. Every setting of a run comes from this file."""

import pathlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import yaml

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "baseline.yaml"

REQUIRED_KEYS = (
    "run_id",
    "config_name",
    "schema_version",
    "ids_file",
    "output_root",
    "arxiv_delay_seconds",
    "user_agent",
    "timeout",
    "max_retries",
)

# run_id may be literally "auto" or contain "{timestamp}" -> replaced at load time.
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class ConfigError(ValueError):
    pass


def _resolve_run_id(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError("run_id must be a non-empty string")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = stamp if raw.strip() == "auto" else raw.strip().replace("{timestamp}", stamp)
    if not _SAFE_RUN_ID.match(run_id):
        raise ConfigError(f"run_id {run_id!r} may only contain letters, digits, '.', '_' and '-'")
    return run_id


def load_config(path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Return the config as a dict with run_id resolved and paths made absolute.

    Relative ``ids_file`` / ``output_root`` are resolved against the repository
    root, so results do not depend on the directory the program is started from.
    """
    config_path = pathlib.Path(path) if path else DEFAULT_CONFIG_PATH
    with config_path.open("r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}

    missing = [key for key in REQUIRED_KEYS if cfg.get(key) is None]
    if missing:
        raise ConfigError(f"{config_path}: missing keys: {', '.join(missing)}")

    for key in ("arxiv_delay_seconds", "timeout"):
        if not isinstance(cfg[key], (int, float)) or cfg[key] < 0:
            raise ConfigError(f"{key} must be a non-negative number")
    if not isinstance(cfg["max_retries"], int) or cfg["max_retries"] < 0:
        raise ConfigError("max_retries must be a non-negative integer")

    base = PROJECT_ROOT
    cfg["run_id"] = _resolve_run_id(cfg["run_id"])
    cfg["ids_file"] = (base / cfg["ids_file"]).resolve()
    cfg["output_root"] = (base / cfg["output_root"]).resolve()
    return cfg
