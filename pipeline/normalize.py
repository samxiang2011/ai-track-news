from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import re

from .parse import RawEntry


TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    "spm",
}


@dataclass(frozen=True)
class Item:
    id: str
    source_id: str
    url: str
    title: str
    published_at: str | None
    fetched_at: str
    lang: str
    excerpt: str | None
    topics: list[str]
    cluster_id: str | None
    origin_url: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_entry(entry: RawEntry, fetched_at: datetime) -> Item:
    canonical_url = canonicalize_url(entry.url)
    return Item(
        id=sha256(canonical_url.encode("utf-8")).hexdigest()[:24],
        source_id=entry.source.id,
        url=canonical_url,
        title=_clean_title(entry.title),
        published_at=_iso(entry.published_at) if entry.published_at else None,
        fetched_at=_iso(fetched_at),
        lang=entry.source.lang,
        excerpt=entry.excerpt[:200] if entry.excerpt else None,
        topics=list(entry.source.topics),
        cluster_id=None,
        origin_url=None,
    )


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_PARAMS:
            continue
        query_items.append((key, value))
    query = urlencode(query_items, doseq=True)
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((scheme, netloc, path, query, ""))


def dedupe_items(items: list[Item]) -> list[Item]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[Item] = []
    for item in items:
        title_key = _title_key(item.title)
        if item.url in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(item.url)
        seen_titles.add(title_key)
        deduped.append(item)
    return deduped


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", title.lower())


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
