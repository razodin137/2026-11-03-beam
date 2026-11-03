"""Prompt loading and placeholder substitution."""

from __future__ import annotations

from pathlib import Path

from .config import ROOT


def load_prompt(name: str) -> str:
    path = ROOT / "prompts" / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"missing prompt file: {path}")
    return path.read_text(encoding="utf-8")


def render(name: str, **values: object) -> str:
    text = load_prompt(name)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text
