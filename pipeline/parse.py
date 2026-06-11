from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import re
from xml.etree import ElementTree

from .config import Source


@dataclass(frozen=True)
class RawEntry:
    source: Source
    title: str
    url: str
    published_at: datetime | None
    excerpt: str | None


def parse_feed(source: Source, content: bytes) -> list[RawEntry]:
    root = ElementTree.fromstring(content)
    root_name = _local_name(root.tag)
    if root_name == "rss":
        return _parse_rss(source, root)
    if root_name == "feed":
        return _parse_atom(source, root)
    return _parse_generic(source, root)


def _parse_rss(source: Source, root: ElementTree.Element) -> list[RawEntry]:
    entries: list[RawEntry] = []
    for item in root.iter():
        if _local_name(item.tag) != "item":
            continue
        title = _child_text(item, "title")
        url = _child_text(item, "link") or _child_text(item, "guid")
        if not title or not url:
            continue
        entries.append(
            RawEntry(
                source=source,
                title=_clean_text(title),
                url=_clean_text(url),
                published_at=_parse_datetime(_child_text(item, "pubDate")),
                excerpt=_clean_excerpt(_child_text(item, "description")),
            )
        )
    return entries


def _parse_atom(source: Source, root: ElementTree.Element) -> list[RawEntry]:
    entries: list[RawEntry] = []
    for entry in root.iter():
        if _local_name(entry.tag) != "entry":
            continue
        title = _child_text(entry, "title")
        url = _atom_link(entry) or _child_text(entry, "id")
        if not title or not url:
            continue
        entries.append(
            RawEntry(
                source=source,
                title=_clean_text(title),
                url=_clean_text(url),
                published_at=_parse_datetime(
                    _child_text(entry, "published") or _child_text(entry, "updated")
                ),
                excerpt=_clean_excerpt(_child_text(entry, "summary") or _child_text(entry, "content")),
            )
        )
    return entries


def _parse_generic(source: Source, root: ElementTree.Element) -> list[RawEntry]:
    entries: list[RawEntry] = []
    for item in root.iter():
        if _local_name(item.tag) not in {"item", "entry"}:
            continue
        title = _child_text(item, "title")
        url = _child_text(item, "link") or _atom_link(item) or _child_text(item, "id")
        if title and url:
            entries.append(
                RawEntry(
                    source=source,
                    title=_clean_text(title),
                    url=_clean_text(url),
                    published_at=_parse_datetime(
                        _child_text(item, "pubDate")
                        or _child_text(item, "published")
                        or _child_text(item, "updated")
                    ),
                    excerpt=_clean_excerpt(
                        _child_text(item, "description")
                        or _child_text(item, "summary")
                        or _child_text(item, "content")
                    ),
                )
            )
    return entries


def _child_text(parent: ElementTree.Element, name: str) -> str | None:
    for child in parent:
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return None


def _atom_link(parent: ElementTree.Element) -> str | None:
    fallback: str | None = None
    for child in parent:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if not href:
            continue
        if child.attrib.get("rel", "alternate") == "alternate":
            return href
        fallback = fallback or href
    return fallback


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clean_excerpt(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _clean_text(value)
    return cleaned[:200] if cleaned else None


def _clean_text(value: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(no_tags)).strip()
