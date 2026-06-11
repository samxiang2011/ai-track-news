from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    tier: int
    url: str
    access_method: str
    lang: str
    topics: list[str]
    tos_risk: str
    m1_action: str
    notes: str = ""


def load_sources(path: Path) -> list[Source]:
    data = _load_yaml(path)
    raw_sources = data.get("sources", [])
    return [Source(**source) for source in raw_sources]


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        return _load_limited_sources_yaml(path)

    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return loaded


def _load_limited_sources_yaml(path: Path) -> dict[str, Any]:
    """Parse the narrow config/sources.yml shape without requiring PyYAML."""

    sources: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_sources = False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "sources:":
            in_sources = True
            continue
        if not in_sources:
            continue
        if line.startswith("  - "):
            if current:
                sources.append(current)
            current = {}
            key, value = _split_key_value(line[4:])
            current[key] = _parse_scalar(value)
            continue
        if current is not None and line.startswith("    "):
            key, value = _split_key_value(line[4:])
            current[key] = _parse_scalar(value)

    if current:
        sources.append(current)

    return {"sources": sources}


def _split_key_value(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ValueError(f"Cannot parse YAML line: {text}")
    key, value = text.split(":", 1)
    return key.strip(), value.strip()


def _parse_scalar(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value
