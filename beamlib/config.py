"""Configuration loading and directory setup for Beam."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

INBOX = ROOT / "inbox"
PROCESSING = ROOT / "processing"
SORTED = ROOT / "sorted"
COLLECTIONS = ROOT / "collections"
FAILED = ROOT / "failed"
LOGS = ROOT / "logs"
CACHE = ROOT / ".cache"


DEFAULT_CONFIG: dict[str, Any] = {
    "api": {
        "base_url": "http://127.0.0.1:11434",
        "timeout_seconds": 600,
    },
    "models": {
        "decision": "deepseek-v4.1-flash:1017-cloud",
        "premium": "deepseek-v4.1-flash:1017-cloud",
        "cheap": "glm-5.3-flash:cloud",
        "think": False,
        "decision_num_predict": 500,
        "extract_num_predict": 8000,
        "temperature": 0.2,
    },
    "categories": {
        "research": {"label": "Research", "accent": "#2563eb", "blurb": "Papers, studies, deep dives, and evidence"},
        "code": {"label": "Code", "accent": "#7c3aed", "blurb": "Repos, technical notes, and engineering"},
        "finance": {"label": "Finance", "accent": "#047857", "blurb": "Markets, money, and business models"},
        "writing": {"label": "Writing", "accent": "#c2410c", "blurb": "Essays, drafts, and storytelling"},
        "admin": {"label": "Admin", "accent": "#475569", "blurb": "Paperwork, logistics, and life maintenance"},
        "health": {"label": "Health", "accent": "#be123c", "blurb": "Medicine, fitness, and wellbeing"},
        "energy": {"label": "Energy", "accent": "#ca8a04", "blurb": "Power, climate, and infrastructure"},
        "content-creation": {"label": "Content Creation", "accent": "#db2777", "blurb": "Video, social, and publishing ideas"},
        "other": {"label": "Other", "accent": "#52525b", "blurb": "Everything that resists a box"},
    },
    "limits": {
        "max_source_chars": 160000,
        "preview_chars": 6000,
        "route_preview_chars": 4000,
        "max_fetch_bytes": 8000000,
        "max_image_bytes": 2500000,
        "max_quotes": 10,
    },
    "collections": {"model": "premium", "max_sources": 12, "per_source_chars": 45000},
    "watch": {"interval_seconds": 2.0, "settle_seconds": 1.2},
    "server": {"host": "127.0.0.1", "port": 8765},
    "ui": {"site_name": "Beam Library", "theme": "auto", "default_language": "en"},
}

DIRS = (INBOX, PROCESSING, SORTED, COLLECTIONS, FAILED, LOGS, CACHE)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load config.json, filling in any keys added by newer Beam versions."""
    path = path or CONFIG_PATH
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config.json is not valid JSON: {exc}") from exc
    if not isinstance(user, dict):
        raise SystemExit("config.json must contain a JSON object")
    return _deep_merge(DEFAULT_CONFIG, user)


def ensure_dirs() -> None:
    for directory in DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def category_keys(cfg: dict[str, Any]) -> list[str]:
    return list(cfg["categories"].keys())


def category_meta(cfg: dict[str, Any], name: str) -> dict[str, str]:
    return cfg["categories"].get(name, cfg["categories"]["other"])


def sorted_dir(cfg: dict[str, Any], category: str) -> Path:
    key = category if category in cfg["categories"] else "other"
    path = SORTED / key
    path.mkdir(parents=True, exist_ok=True)
    return path
