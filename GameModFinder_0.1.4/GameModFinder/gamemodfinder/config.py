from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"

ALIASES_PATH = DATA_DIR / "game_aliases.json"
DEFAULT_API_BASE = "https://pawchive.pw/api/v1"

DEFAULT_SETTINGS: dict[str, Any] = {
    "api_base": DEFAULT_API_BASE,
    "requests_per_second": 1.0,
    "results_per_query_pages": 1,
    "max_expanded_queries": 7,
    "max_posts_per_search": 250,
    "request_timeout": 20,
}


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def load_aliases() -> dict[str, Any]:
    return load_json(ALIASES_PATH, {})


def app_data_dir() -> Path:
    """Use a per-user writable directory for the SQLite database."""
    root = os.environ.get("APPDATA")
    if root:
        base = Path(root) / "GameModFinder"
    else:
        base = Path.home() / ".gamemodfinder"
    base.mkdir(parents=True, exist_ok=True)
    return base


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    settings.update(load_json(app_data_dir() / "settings.json", {}))
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    path = app_data_dir() / "settings.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2)
