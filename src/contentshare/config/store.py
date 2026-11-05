"""Loading and saving the YAML config, and where app data lives."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from contentshare.config.models import AppConfig


class ConfigError(Exception):
    """The config file could not be read or is invalid. The message says what to fix."""


def data_dir() -> Path:
    """``%APPDATA%\\ContentShare`` (override with CONTENTSHARE_HOME, used by tests)."""
    override = os.environ.get("CONTENTSHARE_HOME")
    base = (
        Path(override)
        if override
        else Path(os.environ.get("APPDATA", Path.home())) / "ContentShareAI"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_config_path() -> Path:
    return data_dir() / "config.yaml"


def load_config(path: Path | None = None) -> AppConfig:
    path = path or default_config_path()
    if not path.exists():
        return AppConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"Invalid settings in {path}: {problems}") from exc


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """Atomic save: write a temp file, fsync, keep the old file as ``.bak``, then replace."""
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            path.replace(path.with_suffix(path.suffix + ".bak"))
        Path(tmp_name).replace(path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    return path
