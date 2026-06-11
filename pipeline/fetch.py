from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Source


USER_AGENT = "AITrackNewsBot/0.1 (+https://github.com/)"


@dataclass(frozen=True)
class FetchResult:
    source: Source
    status: str
    fetched_at: datetime | None
    content: bytes | None
    content_type: str | None
    error: str | None = None


def fetch_source(source: Source, timeout: float, dry_run: bool) -> FetchResult:
    if dry_run:
        now = datetime.now(timezone.utc)
        return FetchResult(
            source=source,
            status="success",
            fetched_at=now,
            content=_dry_run_feed(source, now),
            content_type="application/rss+xml",
        )

    if source.access_method != "rss":
        return FetchResult(
            source=source,
            status="skipped",
            fetched_at=datetime.now(timezone.utc),
            content=None,
            content_type=None,
            error=f"unsupported access_method for M1: {source.access_method}",
        )

    request = Request(
        source.url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310: public feeds only.
            content_type = response.headers.get("Content-Type")
            content = response.read()
        return FetchResult(
            source=source,
            status="success",
            fetched_at=datetime.now(timezone.utc),
            content=content,
            content_type=content_type,
        )
    except HTTPError as exc:
        return _error_result(source, f"HTTP {exc.code}: {exc.reason}")
    except URLError as exc:
        return _error_result(source, f"URL error: {exc.reason}")
    except TimeoutError as exc:
        return _error_result(source, f"timeout: {exc}")
    except Exception as exc:  # noqa: BLE001 - source failure must be isolated.
        return _error_result(source, f"{type(exc).__name__}: {exc}")


def _error_result(source: Source, error: str) -> FetchResult:
    return FetchResult(
        source=source,
        status="failed",
        fetched_at=datetime.now(timezone.utc),
        content=None,
        content_type=None,
        error=error,
    )


def _dry_run_feed(source: Source, now: datetime) -> bytes:
    pub_date = format_datetime(now)
    shared_url = "https://example.com/ai-track-news/shared-model-release"
    unique_url = f"https://example.com/ai-track-news/{source.id}/m1-dry-run"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{_escape(source.name)} dry run</title>
    <link>{_escape(source.url)}</link>
    <description>Dry run feed for {_escape(source.id)}</description>
    <item>
      <title>Shared model release appears across AI sources</title>
      <link>{shared_url}</link>
      <guid>{shared_url}</guid>
      <pubDate>{pub_date}</pubDate>
      <description>This synthetic item exercises cross-source URL dedupe.</description>
    </item>
    <item>
      <title>{_escape(source.name)} M1 dry-run collection item</title>
      <link>{unique_url}</link>
      <guid>{unique_url}</guid>
      <pubDate>{pub_date}</pubDate>
      <description>Fetch, normalize, dedupe, snapshot, and manifest smoke item.</description>
    </item>
  </channel>
</rss>
"""
    return xml.encode("utf-8")


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
