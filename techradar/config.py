"""Configuration loading for the TechRadar pipeline.

All operational knobs live in ``sources.yaml`` so sources can be re-scoped,
re-scheduled, enabled, or disabled without touching application code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader (KEY=VALUE lines; environment wins over file)."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DATA_DIR = Path(os.environ.get("TECHRADAR_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = DATA_DIR / "techradar.db"
CHROMA_DIR = DATA_DIR / "chroma"
SOURCES_FILE = Path(os.environ.get("TECHRADAR_SOURCES", PROJECT_ROOT / "sources.yaml"))

BUCKETS = ("research", "regulatory", "practitioner")
STORAGE_MODES = ("full_text", "abstract_only", "link_only")


@dataclass(frozen=True)
class SourceConfig:
    """Static configuration for a single ingestion source."""

    name: str
    connector: str
    bucket: str
    doc_type: str
    cadence: str
    fetch_strategy: str
    storage_mode: str
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    request_timeout: float
    user_agent: str
    retry_max: int
    retry_backoff_seconds: float
    sources: dict[str, SourceConfig]

    def source(self, name: str) -> SourceConfig:
        try:
            return self.sources[name]
        except KeyError:
            known = ", ".join(sorted(self.sources))
            raise KeyError(f"unknown source {name!r}; configured sources: {known}") from None


def load_settings(path: Path | None = None) -> Settings:
    raw = yaml.safe_load((path or SOURCES_FILE).read_text())
    defaults = raw.get("defaults", {})
    sources: dict[str, SourceConfig] = {}
    for name, cfg in raw.get("sources", {}).items():
        if cfg.get("bucket") not in BUCKETS:
            raise ValueError(f"source {name!r}: bucket must be one of {BUCKETS}")
        if cfg.get("storage_mode") not in STORAGE_MODES:
            raise ValueError(f"source {name!r}: storage_mode must be one of {STORAGE_MODES}")
        sources[name] = SourceConfig(
            name=name,
            connector=cfg["connector"],
            bucket=cfg["bucket"],
            doc_type=cfg["doc_type"],
            cadence=cfg["cadence"],
            fetch_strategy=cfg.get("fetch_strategy", ""),
            storage_mode=cfg["storage_mode"],
            enabled=bool(cfg.get("enabled", True)),
            params=cfg.get("params", {}) or {},
        )
    return Settings(
        request_timeout=float(defaults.get("request_timeout", 30)),
        user_agent=str(defaults.get("user_agent", "TechRadarAgent/0.1")),
        retry_max=int(defaults.get("retry_max", 4)),
        retry_backoff_seconds=float(defaults.get("retry_backoff_seconds", 5)),
        sources=sources,
    )
