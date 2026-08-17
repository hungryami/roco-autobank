"""Read the root config.yaml and expose typed settings for the launcher."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILE = "config.yaml"

# Seed ids of the preferred crops to auto-sow, in priority order. The game
# protocol only reports seed ids (no names), so the values must be looked up
# out-of-band. 100728957 = 乖乖蘑菇, 100728955 = 小Q牛轧糖. When none of the
# preferred seeds is in the inventory the automation sows the most abundant
# seed found in the inventory.
DEFAULT_PREFERRED_SEED_ID: int | None = 100728957
DEFAULT_FALLBACK_SEED_IDS: tuple[int, ...] = (100728955,)


@dataclass(slots=True)
class AppConfig:
    login_mode: str = "qr"
    account: str = ""
    password: str = ""
    auto_start_hang: bool = True
    auto_exit_at_23: bool = True

    # Automation switches (headless password mode).
    auto_farm: bool = True
    auto_paradise: bool = True
    auto_log_interval: int = 5
    farm_interval: int = 60
    paradise_interval: int = 15
    hang_minutes: int = 30
    hang_cooldown_minutes: int = 5
    preferred_seed_id: int | None = DEFAULT_PREFERRED_SEED_ID
    fallback_seed_ids: tuple[int, ...] = DEFAULT_FALLBACK_SEED_IDS

    # Server binding used by the headless mode.
    host: str = "127.0.0.1"
    port: int = 8000
    log_file: str = "logs/roco-mini-service.log"

    @property
    def has_password_credentials(self) -> bool:
        return bool(self.account.strip()) and bool(self.password.strip())

    @property
    def password_login(self) -> bool:
        return self.login_mode.lower() == "password"


def project_root() -> Path:
    """Return the repository root (the directory that owns config.yaml)."""

    # Prefer the environment override so tests can point at a temp directory.
    override = os.environ.get("ROCO_CONFIG_DIR")
    if override:
        return Path(override).resolve()
    # gui.py / scripts run with the project root as the working directory.
    return Path.cwd().resolve()


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load config.yaml from the project root, tolerating missing fields."""

    config_path = (
        Path(path).expanduser()
        if path is not None
        else project_root() / DEFAULT_CONFIG_FILE
    )
    raw: dict[str, Any] = {}
    if config_path.is_file():
        try:
            text = config_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(text) or {}
            if isinstance(parsed, dict):
                raw = parsed
        except (OSError, yaml.YAMLError) as exc:
            # A broken config must not crash the GUI path; fall back to defaults.
            import logging

            logging.getLogger(__name__).warning(
                "config_file_unreadable path=%s error_type=%s",
                config_path,
                type(exc).__name__,
            )

    return _config_from_mapping(raw)


def _config_from_mapping(raw: dict[str, Any]) -> AppConfig:
    def text(key: str, default: str) -> str:
        value = raw.get(key, default)
        return str(value) if value is not None else default

    def boolean(key: str, default: bool) -> bool:
        value = raw.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def integer(key: str, default: int) -> int:
        value = raw.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    config = AppConfig(
        login_mode=text("login_mode", "qr").strip() or "qr",
        account=text("account", "").strip(),
        password=text("password", ""),
        auto_start_hang=boolean("auto_start_hang", True),
        auto_exit_at_23=boolean("auto_exit_at_23", True),
        auto_farm=boolean("auto_farm", True),
        auto_paradise=boolean("auto_paradise", True),
        auto_log_interval=integer("auto_log_interval", 5),
        farm_interval=integer("farm_interval", 60),
        paradise_interval=integer("paradise_interval", 15),
        hang_minutes=integer("hang_minutes", 30),
        hang_cooldown_minutes=integer("hang_cooldown_minutes", 5),
        preferred_seed_id=_nullable_seed_id(
            raw.get("preferred_seed_id"),
            DEFAULT_PREFERRED_SEED_ID,
        ),
        fallback_seed_ids=_seed_id_list(
            raw.get("fallback_seed_ids"),
            DEFAULT_FALLBACK_SEED_IDS,
        ),
        host=text("host", "127.0.0.1"),
        port=integer("port", 8000),
        log_file=text("log_file", "logs/roco-mini-service.log"),
    )
    if config.auto_log_interval < 1:
        config.auto_log_interval = 5
    return config


def _nullable_seed_id(value: Any, default: int | None) -> int | None:
    if value is None:
        return default
    if value in ("", "null", "none"):
        return None
    try:
        parsed = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _seed_id_list(
    value: Any,
    default: tuple[int, ...],
) -> tuple[int, ...]:
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        ids = [_nullable_seed_id(item, None) for item in value]
        return tuple(seed for seed in ids if seed is not None)
    parsed = _nullable_seed_id(value, None)
    return (parsed,) if parsed is not None else ()
