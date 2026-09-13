#!/usr/bin/env python3
"""Push a compact BirdNET-Go daily snapshot to a TRMNL webhook."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "0.1.0"
DEFAULT_API_URL = "http://127.0.0.1:8080"
DEFAULT_TIMEZONE = "UTC"
DEFAULT_STATION_NAME = "BirdNET-Go"
DEFAULT_MAX_PAYLOAD_BYTES = 2000
USER_AGENT = f"birdnet-trmnl/{VERSION}"
FALSE_POSITIVE_VALUES = {"false_positive", "false-positive", "false positive", "incorrect"}


class AdapterError(RuntimeError):
    """A concise, user-facing adapter error."""


class RateLimitError(AdapterError):
    def __init__(self, retry_after: str | None = None):
        message = "TRMNL rejected the update because its webhook rate limit was reached"
        if retry_after:
            message += f"; retry after {retry_after}"
        super().__init__(message)


@dataclass(frozen=True)
class Config:
    birdnet_url: str
    webhook_url: str | None
    station_name: str
    timezone: str
    timeout_seconds: float
    cache_dir: Path
    max_payload_bytes: int
    min_confidence: float
    images_enabled: bool

    @classmethod
    def from_env(cls) -> "Config":
        timeout = _float_env("BIRDNET_TRMNL_TIMEOUT_SECONDS", 10.0, minimum=1.0, maximum=60.0)
        max_bytes = _int_env("BIRDNET_TRMNL_MAX_PAYLOAD_BYTES", DEFAULT_MAX_PAYLOAD_BYTES, 900, 2048)
        minimum = _float_env("BIRDNET_TRMNL_MIN_CONFIDENCE", 0.0, minimum=0.0, maximum=1.0)
        cache_dir = Path(os.environ.get("BIRDNET_TRMNL_CACHE_DIR", "/var/cache/birdnet-trmnl"))
        return cls(
            birdnet_url=os.environ.get("BIRDNET_TRMNL_BIRDNET_URL", DEFAULT_API_URL).rstrip("/"),
            webhook_url=_optional_env("BIRDNET_TRMNL_WEBHOOK_URL"),
            station_name=os.environ.get("BIRDNET_TRMNL_STATION_NAME", DEFAULT_STATION_NAME).strip()
            or DEFAULT_STATION_NAME,
            timezone=os.environ.get("BIRDNET_TRMNL_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE,
            timeout_seconds=timeout,
            cache_dir=cache_dir,
            max_payload_bytes=max_bytes,
            min_confidence=minimum,
            images_enabled=_bool_env("BIRDNET_TRMNL_WIKIMEDIA_IMAGES", True),
        )


class HttpClient:
    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds

    def get_json(self, url: str) -> Any:
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise AdapterError(f"GET failed for {_safe_url(url)}: {error}") from error

    def post_json(self, url: str, body: bytes) -> None:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    raise AdapterError(f"TRMNL webhook returned HTTP {response.status}")
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise RateLimitError(error.headers.get("Retry-After")) from error
            raise AdapterError(f"TRMNL webhook returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise AdapterError(f"TRMNL webhook request failed: {error}") from error


class BirdNetClient:
    def __init__(self, base_url: str, http: HttpClient):
        self.base_url = base_url.rstrip("/")
        self.http = http

    def daily_species(self, date_key: str, min_confidence: float) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"date": date_key, "min_confidence": min_confidence})
        payload = self.http.get_json(f"{self.base_url}/api/v2/analytics/species/daily?{query}")
        if not isinstance(payload, list):
            raise AdapterError("BirdNET daily species response was not a list")
        return [item for item in payload if isinstance(item, dict)]

    def recent_detections(self, limit: int = 50) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"limit": limit})
        payload = self.http.get_json(f"{self.base_url}/api/v2/detections?{query}")
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise AdapterError("BirdNET detections response did not contain a data list")
        return [item for item in items if isinstance(item, dict)]


class WikimediaClient:
    API = "https://en.wikipedia.org/w/api.php"
    COMMONS_API = "https://commons.wikimedia.org/w/api.php"
    SUCCESS_TTL = 7 * 24 * 60 * 60
    NEGATIVE_TTL = 24 * 60 * 60

    def __init__(self, http: HttpClient, cache_dir: Path):
        self.http = http
        self.cache_dir = cache_dir / "images"

    def portrait(self, scientific_name: str) -> dict[str, str] | None:
        cached = self._read_cache(scientific_name)
        if cached is not None:
            return cached.get("portrait")

        portrait = self._fetch(scientific_name)
        self._write_cache(scientific_name, {"portrait": portrait, "cached_at": int(time.time())})
        return portrait

    def _fetch(self, scientific_name: str) -> dict[str, str] | None:
        first_query = urllib.parse.urlencode(
            {
                "action": "query",
                "prop": "pageimages",
                "titles": scientific_name,
                "piprop": "name|thumbnail",
                "pithumbsize": 1200,
                "pilicense": "free",
                "redirects": 1,
                "format": "json",
                "formatversion": 2,
            }
        )
        try:
            page_data = self.http.get_json(f"{self.API}?{first_query}")
            pages = page_data.get("query", {}).get("pages", [])
            page = pages[0] if pages and isinstance(pages[0], dict) else {}
            filename = page.get("pageimage")
            if not filename:
                return None

            second_query = urllib.parse.urlencode(
                {
                    "action": "query",
                    "prop": "imageinfo",
                    "titles": f"File:{filename}",
                    "iiprop": "url|extmetadata",
                    "iiurlwidth": 1200,
                    "format": "json",
                    "formatversion": 2,
                }
            )
            commons_data = self.http.get_json(f"{self.COMMONS_API}?{second_query}")
            commons_pages = commons_data.get("query", {}).get("pages", [])
            image_info = commons_pages[0].get("imageinfo", [{}])[0] if commons_pages else {}
            metadata = image_info.get("extmetadata", {})
            image_url = image_info.get("thumburl") or image_info.get("url") or page.get("thumbnail", {}).get("source")
            author = _metadata_text(metadata, "Artist") or _metadata_text(metadata, "Credit")
            license_name = _metadata_text(metadata, "LicenseShortName") or _metadata_text(metadata, "UsageTerms")
            if not image_url or not author or not license_name:
                return None
            return {
                "url": str(image_url),
                "credit": _truncate(f"Photo: {author} · {license_name}", 110),
                "source": f"https://commons.wikimedia.org/wiki/{urllib.parse.quote('File:' + filename, safe=':')}",
            }
        except AdapterError:
            return None

    def _cache_path(self, scientific_name: str) -> Path:
        digest = hashlib.sha256(scientific_name.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, scientific_name: str) -> dict[str, Any] | None:
        path = self._cache_path(scientific_name)
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - int(cached.get("cached_at", 0))
            ttl = self.SUCCESS_TTL if cached.get("portrait") else self.NEGATIVE_TTL
            return cached if age < ttl else None
        except (OSError, ValueError, TypeError):
            return None

    def _write_cache(self, scientific_name: str, value: dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            target = self._cache_path(scientific_name)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.cache_dir, delete=False) as handle:
                json.dump(value, handle, separators=(",", ":"), ensure_ascii=False)
                temp_name = handle.name
            os.replace(temp_name, target)
        except OSError:
            pass


def build_snapshot(
    daily: list[dict[str, Any]],
    detections: list[dict[str, Any]],
    config: Config,
    now: datetime,
    portrait: dict[str, str] | None = None,
) -> dict[str, Any]:
    zone = _timezone(config.timezone)
    local_now = now.astimezone(zone)
    date_key = local_now.date().isoformat()
    valid_daily = [item for item in daily if not _is_false_positive(item)]
    valid_detections = [
        item
        for item in detections
        if item.get("date") == date_key and not _is_false_positive(item)
    ]

    top_species = []
    activity = [0] * 24
    for item in valid_daily:
        hourly = item.get("hourly_counts")
        if isinstance(hourly, list):
            for index, value in enumerate(hourly[:24]):
                activity[index] += _safe_int(value)
        top_species.append(
            {
                "name": _truncate(str(item.get("common_name") or item.get("scientific_name") or "Unknown"), 38),
                "scientific": _truncate(str(item.get("scientific_name") or ""), 48),
                "count": _safe_int(item.get("count")),
                "last": _format_clock(item.get("latest_heard"), zone),
            }
        )
    top_species.sort(key=lambda item: (-item["count"], item["name"]))

    recent = []
    for item in valid_detections[:5]:
        recent.append(
            {
                "name": _truncate(str(item.get("commonName") or item.get("scientificName") or "Unknown"), 34),
                "confidence": round(_safe_float(item.get("confidence")) * 100),
                "heard": _format_clock(item.get("timestamp") or item.get("time"), zone),
            }
        )

    latest = valid_detections[0] if valid_detections else None
    featured_species = str(
        (latest or {}).get("scientificName")
        or (top_species[0]["scientific"] if top_species else "")
    )
    featured_name = str(
        (latest or {}).get("commonName")
        or (top_species[0]["name"] if top_species else "No detections yet")
    )

    max_activity = max(activity, default=0)
    activity_levels = [
        0 if max_activity == 0 or count == 0 else max(1, round((count / max_activity) * 10))
        for count in activity
    ]

    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "status": "online",
        "station": _truncate(config.station_name, 42),
        "date": date_key,
        "date_label": local_now.strftime("%A, %B %-d"),
        "checked_at": local_now.strftime("%-I:%M %p %Z"),
        "detections": sum(_safe_int(item.get("count")) for item in valid_daily),
        "species": len(valid_daily),
        "featured": {
            "name": _truncate(featured_name, 42),
            "scientific": _truncate(featured_species, 52),
            "confidence": round(_safe_float((latest or {}).get("confidence")) * 100) if latest else None,
            "heard": _format_clock((latest or {}).get("timestamp"), zone) if latest else "",
            "image_url": (portrait or {}).get("url", ""),
            "image_credit": (portrait or {}).get("credit", ""),
            "image_source": (portrait or {}).get("source", ""),
        },
        "top_species": top_species[:5],
        "recent": recent,
        "activity": activity,
        "activity_levels": activity_levels,
        "activity_max": max_activity,
    }
    return snapshot


def fit_webhook_body(snapshot: dict[str, Any], max_bytes: int) -> bytes:
    candidate = json.loads(json.dumps(snapshot))
    while True:
        body = json.dumps({"merge_variables": candidate}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) <= max_bytes:
            return body
        if candidate.get("recent"):
            candidate["recent"].pop()
            continue
        if len(candidate.get("top_species", [])) > 3:
            candidate["top_species"].pop()
            continue
        featured = candidate.get("featured", {})
        if featured.get("image_source"):
            featured["image_source"] = ""
            continue
        if featured.get("image_credit"):
            featured["image_credit"] = ""
            continue
        if featured.get("image_url"):
            featured["image_url"] = ""
            continue
        if candidate.get("activity") or candidate.get("activity_levels"):
            candidate["activity"] = []
            candidate["activity_levels"] = []
            continue
        raise AdapterError(f"Unable to fit required TRMNL data within {max_bytes} bytes")


def collect(config: Config, now: datetime | None = None, http: HttpClient | None = None) -> dict[str, Any]:
    zone = _timezone(config.timezone)
    instant = now or datetime.now(tz=zone)
    date_key = instant.astimezone(zone).date().isoformat()
    client_http = http or HttpClient(config.timeout_seconds)
    birdnet = BirdNetClient(config.birdnet_url, client_http)
    daily = birdnet.daily_species(date_key, config.min_confidence)
    detections = birdnet.recent_detections()

    portrait = None
    if config.images_enabled:
        latest = next(
            (
                item
                for item in detections
                if item.get("date") == date_key and not _is_false_positive(item) and item.get("scientificName")
            ),
            None,
        )
        scientific_name = str((latest or {}).get("scientificName") or (daily[0].get("scientific_name") if daily else ""))
        if scientific_name:
            portrait = WikimediaClient(client_http, config.cache_dir).portrait(scientific_name)
    return build_snapshot(daily, detections, config, instant, portrait)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the exact webhook body without sending it")
    parser.add_argument("--no-image", action="store_true", help="skip Wikimedia portrait lookup for this run")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
        if args.no_image:
            config = Config(**{**config.__dict__, "images_enabled": False})
        snapshot = collect(config)
        body = fit_webhook_body(snapshot, config.max_payload_bytes)
        if args.dry_run:
            print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
            print(f"payload_bytes={len(body)}", file=sys.stderr)
            return 0
        if not config.webhook_url:
            raise AdapterError("BIRDNET_TRMNL_WEBHOOK_URL is required unless --dry-run is used")
        HttpClient(config.timeout_seconds).post_json(config.webhook_url, body)
        print(
            f"pushed {snapshot['detections']} detections across {snapshot['species']} species "
            f"({len(body)} bytes)"
        )
        return 0
    except (AdapterError, ZoneInfoNotFoundError, ValueError) as error:
        print(f"birdnet-trmnl: {error}", file=sys.stderr)
        return 1


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    value = float(os.environ.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.environ.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _timezone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _safe_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _is_false_positive(item: dict[str, Any]) -> bool:
    value = str(item.get("verified") or item.get("verification") or "").strip().lower()
    return value in FALSE_POSITIVE_VALUES


def _format_clock(value: Any, zone: ZoneInfo) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(zone)
            return parsed.strftime("%-I:%M %p")
        parsed_time = datetime.strptime(text[:8], "%H:%M:%S")
        return parsed_time.strftime("%-I:%M %p")
    except ValueError:
        return _truncate(text, 12)


def _metadata_text(metadata: dict[str, Any], key: str) -> str:
    raw = metadata.get(key, {}) if isinstance(metadata, dict) else {}
    value = raw.get("value", "") if isinstance(raw, dict) else ""
    return _clean_html(str(value))


def _clean_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", html.unescape(value))
    return re.sub(r"\s+", " ", value).strip()


def _truncate(value: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    return compact if len(compact) <= limit else compact[: max(0, limit - 1)].rstrip() + "…"


if __name__ == "__main__":
    raise SystemExit(main())
