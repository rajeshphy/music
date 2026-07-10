#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "searches.json"
OUTPUT_PATH = ROOT / "data" / "tracks.json"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v={video_id}"
YOUTUBE_THUMB = "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def clean_text(value: object) -> str:
    text = str(value or "")
    return " ".join(text.split())


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def seconds_to_clock(seconds: object) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def seconds_value(seconds: object) -> int | None:
    try:
        return int(float(seconds))
    except (TypeError, ValueError):
        return None


def parse_timestamp(value: object, timezone: str):
    text = clean_text(value)
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    try:
        return datetime.fromtimestamp(float(text), tz=ZoneInfo(timezone))
    except (OverflowError, OSError, ValueError):
        return None


def parse_feed_datetime(value: object, timezone: str):
    text = clean_text(value)
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(ZoneInfo(timezone))
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
        return parsed.astimezone(ZoneInfo(timezone))
    except ValueError:
        try:
            return parsedate_to_datetime(text).astimezone(ZoneInfo(timezone))
        except (TypeError, ValueError, AttributeError):
            return None


def run_search(query: str, max_results: int, timeout: int, sort_mode: str, cutoff_time: datetime) -> list[dict]:
    if sort_mode == "date":
        query = f"{query} after:{cutoff_time.date().isoformat()}"
    target = f"ytsearch{max_results}:{query}"
    fields = "%(id)s\t%(title)s\t%(channel)s\t%(duration)s\t%(upload_date)s\t%(timestamp)s\t%(webpage_url)s"
    command = [
        "yt-dlp",
        "--extractor-args", "youtube:player_client=android",
        "--skip-download",
        "--ignore-errors",
        "--no-warnings",
        "--quiet",
        "--playlist-end", str(max_results),
        "--print", fields,
        target,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    entries = []
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 6)
        if len(parts) != 7:
            continue
        video_id, title, channel, duration, upload_date, timestamp, webpage_url = parts
        entries.append({
            "id": clean_text(video_id),
            "title": clean_text(title),
            "channel": clean_text(channel),
            "duration": None if duration == "NA" else duration,
            "upload_date": "" if upload_date == "NA" else clean_text(upload_date),
            "timestamp": "" if timestamp == "NA" else clean_text(timestamp),
            "webpage_url": clean_text(webpage_url),
        })

    if not entries and completed.returncode:
        raise RuntimeError((completed.stderr or "yt-dlp returned no entries").strip())
    return entries


def fetch_feed(feed: dict, timeout: int) -> ET.Element:
    req = Request(str(feed["url"]), headers={"User-Agent": "music-radar/1.0"})
    with urlopen(req, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        data = response.read(500000)
    root = ET.fromstring(data)
    entries = root.findall(f"{ATOM}entry")
    if not entries:
        raise RuntimeError("RSS feed has no entries")
    return root


def feed_link(entry: ET.Element) -> str:
    for link in entry.findall(f"{ATOM}link"):
        rel = link.attrib.get("rel", "alternate")
        href = link.attrib.get("href", "")
        if rel == "alternate" and href:
            return href
    video_id = clean_text(entry.findtext(f"{YT}videoId"))
    return YOUTUBE_WATCH.format(video_id=video_id) if video_id else ""


def fetch_video_duration(video_id: str, timeout: int, cache: dict[str, int | None]) -> int | None:
    if video_id in cache:
        return cache[video_id]
    if str(os.environ.get("MUSIC_SKIP_DURATION_FETCH", "")).lower() in {"1", "true", "yes"}:
        cache[video_id] = None
        return None
    url = f"{YOUTUBE_WATCH.format(video_id=video_id)}&bpctr=9999999999&has_verified=1"
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urlopen(req, timeout=timeout) as response:
            html = response.read(2500000).decode("utf-8", "ignore")
    except Exception:
        cache[video_id] = None
        return None

    start = html.find("ytInitialPlayerResponse")
    if start == -1:
        cache[video_id] = None
        return None
    match = re.search(r'"lengthSeconds"\s*:\s*"(\d+)"', html[start:start + 1000000])
    cache[video_id] = int(match.group(1)) if match else None
    return cache[video_id]


def term_score(text: str, terms: list[str], value: float) -> float:
    score = 0.0
    padded = f" {text} "
    for term in terms:
        normalized = normalize_key(term)
        if normalized and normalized in padded:
            score += value
    return score


def valid_music_text(text: str, config: dict) -> bool:
    if has_any_term(text, config.get("hard_block_terms", [])):
        return False
    required_terms = config.get("required_terms", [])
    return not required_terms or has_any_term(text, required_terms)


def has_any_term(text: str, terms: list[str]) -> bool:
    padded = f" {text} "
    return any((normalized in padded) for term in terms if (normalized := normalize_key(term)))


def duration_score(duration: object, text: str) -> float:
    try:
        seconds = int(float(duration))
    except (TypeError, ValueError):
        return 0.0

    if seconds < 600:
        return -8.0
    if seconds <= 1800:
        return 2.5
    if seconds <= 5400 and " mix " in f" {text} ":
        return 2.0
    if seconds <= 10800:
        return 1.2
    return -0.6


def score_entry(entry: dict, search: dict, config: dict, rank: int) -> float:
    title = normalize_key(clean_text(entry.get("title")))
    channel = normalize_key(clean_text(entry.get("channel") or entry.get("uploader")))
    text = f"{title} {channel}"

    score = float(search.get("weight", 1.0)) * 8.0
    score += max(0, 12 - rank) * 0.45
    score += term_score(text, config.get("positive_terms", []), 1.1)
    score += term_score(text, config.get("negative_terms", []), -4.0)
    score += duration_score(entry.get("duration"), text)

    view_count = entry.get("view_count")
    if isinstance(view_count, (int, float)) and view_count > 0:
        score += min(2.5, view_count / 1_000_000)

    return round(score, 3)


def item_from_feed_entry(
    entry: ET.Element,
    feed: dict,
    config: dict,
    rank: int,
    cutoff_time,
    timezone: str,
    duration_cache: dict[str, int | None],
    duration_timeout: int,
) -> dict | None:
    title = clean_text(entry.findtext(f"{ATOM}title"))
    video_id = clean_text(entry.findtext(f"{YT}videoId"))
    url = feed_link(entry)
    published_at = parse_feed_datetime(entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated"), timezone)
    if not title or not video_id or not url or not published_at or published_at < cutoff_time:
        return None

    channel = clean_text(feed.get("label")) or clean_text(entry.findtext(f"{ATOM}author/{ATOM}name"))
    text = normalize_key(f"{title} {channel}")
    if not valid_music_text(text, config):
        return None

    match_terms = [normalize_key(term) for term in feed.get("match_terms", [])]
    if match_terms and not any(term in text for term in match_terms):
        return None

    duration = fetch_video_duration(video_id, duration_timeout, duration_cache)
    duration_source = "youtube"
    if duration is None and feed.get("fallback_duration"):
        duration = seconds_value(feed.get("fallback_duration"))
        duration_source = "feed_fallback"
    min_duration = int(config.get("portal", {}).get("min_duration_seconds") or 600)
    if duration is None or duration < min_duration:
        return None

    search = {
        "id": feed.get("category"),
        "label": feed.get("category_label") or feed.get("label"),
        "query": feed.get("label"),
        "weight": feed.get("weight", 1.0),
    }
    raw = {"title": title, "channel": channel, "duration": duration}
    score = score_entry(raw, search, config, rank)
    if score < float(config.get("minimum_score", 8.0)):
        return None

    return {
        "id": video_id,
        "title": title,
        "channel": channel,
        "url": url,
        "embed_url": f"https://www.youtube.com/embed/{video_id}",
        "thumbnail": YOUTUBE_THUMB.format(video_id=video_id),
        "duration": duration,
        "duration_text": seconds_to_clock(duration),
        "published": published_at.strftime("%Y%m%d"),
        "published_at": published_at.isoformat(),
        "published_ts": int(published_at.timestamp()),
        "latest_rank": rank,
        "category": feed.get("category"),
        "category_label": feed.get("category_label") or feed.get("label"),
        "query": feed.get("label"),
        "score": score,
        "method": "rss",
        "duration_source": duration_source,
        "feed_id": feed.get("id"),
        "feed_url": feed.get("url"),
    }


def feed_items(
    feed: dict,
    config: dict,
    cutoff_time,
    timezone: str,
    timeout: int,
    duration_cache: dict[str, int | None],
    duration_timeout: int,
) -> list[dict]:
    root = fetch_feed(feed, timeout)
    max_per_feed = int(feed.get("max_items") or config.get("portal", {}).get("max_items_per_feed") or 4)
    items = []
    for rank, entry in enumerate(root.findall(f"{ATOM}entry"), start=1):
        item = item_from_feed_entry(entry, feed, config, rank, cutoff_time, timezone, duration_cache, duration_timeout)
        if item:
            items.append(item)
        if len(items) >= max_per_feed:
            break
    return items


def item_from_classic(track: dict, config: dict, rank: int, timezone: str) -> dict | None:
    video_id = clean_text(track.get("id"))
    title = clean_text(track.get("title"))
    channel = clean_text(track.get("channel"))
    category = clean_text(track.get("category"))
    category_label = clean_text(track.get("category_label"))
    duration = seconds_value(track.get("duration"))
    min_duration = int(config.get("portal", {}).get("min_duration_seconds") or 600)
    if not video_id or not title or not category or duration is None or duration < min_duration:
        return None

    text = normalize_key(f"{title} {channel}")
    if not valid_music_text(text, config):
        return None

    score = score_entry({"title": title, "channel": channel, "duration": duration}, {
        "id": category,
        "label": category_label,
        "query": "classic",
        "weight": float(track.get("weight", 1.0)),
    }, config, rank) + 5.0

    return {
        "id": video_id,
        "title": title,
        "channel": channel,
        "url": track.get("url") or YOUTUBE_WATCH.format(video_id=video_id),
        "embed_url": f"https://www.youtube.com/embed/{video_id}",
        "thumbnail": YOUTUBE_THUMB.format(video_id=video_id),
        "duration": duration,
        "duration_text": seconds_to_clock(duration),
        "published": "classic",
        "published_at": None,
        "published_ts": 0,
        "latest_rank": rank,
        "category": category,
        "category_label": category_label,
        "query": "Classic",
        "score": round(score, 3),
        "method": "classic",
        "classic": True,
    }


def item_from_audio_stream(stream: dict, rank: int, now: datetime) -> dict | None:
    stream_id = clean_text(stream.get("id"))
    title = clean_text(stream.get("title") or stream.get("label"))
    audio_url = clean_text(stream.get("audio_url"))
    category = clean_text(stream.get("category"))
    if not stream_id or not title or not audio_url or not category:
        return None

    category_label = clean_text(stream.get("category_label") or stream.get("label") or category.title())
    channel = clean_text(stream.get("channel") or stream.get("source") or "Background Audio")
    return {
        "id": f"stream-{stream_id}",
        "title": title,
        "channel": channel,
        "url": clean_text(stream.get("url")) or audio_url,
        "audio_url": audio_url,
        "thumbnail": clean_text(stream.get("thumbnail")),
        "duration": None,
        "duration_text": clean_text(stream.get("duration_text")) or "Live",
        "published": "stream",
        "published_at": now.isoformat(),
        "published_ts": int(now.timestamp()) - rank,
        "latest_rank": rank,
        "category": category,
        "category_label": category_label,
        "query": "Background Audio",
        "score": round(float(stream.get("weight", 1.0)) * 20.0, 3),
        "method": "audio_stream",
        "stream": True,
        "background_audio": True,
    }


def item_from_audio_track(track: dict, rank: int, now: datetime, min_duration: int) -> dict | None:
    track_id = clean_text(track.get("id"))
    title = clean_text(track.get("title"))
    audio_url = clean_text(track.get("audio_url"))
    category = clean_text(track.get("category"))
    duration = seconds_value(track.get("duration"))
    if not track_id or not title or not audio_url or not category or duration is None:
        return None
    if duration < min_duration:
        return None

    category_label = clean_text(track.get("category_label") or category.title())
    channel = clean_text(track.get("channel") or track.get("source") or "Background Audio")
    return {
        "id": f"audio-{track_id}",
        "title": title,
        "channel": channel,
        "url": clean_text(track.get("url")) or audio_url,
        "audio_url": audio_url,
        "thumbnail": clean_text(track.get("thumbnail")),
        "duration": duration,
        "duration_text": seconds_to_clock(duration),
        "published": "fixed",
        "published_at": now.isoformat(),
        "published_ts": int(now.timestamp()) - rank,
        "latest_rank": rank,
        "category": category,
        "category_label": category_label,
        "query": "Fixed Background Audio",
        "score": round(float(track.get("weight", 1.0)) * 20.0, 3),
        "method": "audio_track",
        "background_audio": True,
    }


def item_from_entry(entry: dict, search: dict, config: dict, rank: int, cutoff_time, timezone: str) -> dict | None:
    video_id = clean_text(entry.get("id"))
    if not video_id:
        url = clean_text(entry.get("url") or entry.get("webpage_url"))
        match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{8,})", url)
        video_id = match.group(1) if match else ""
    if not video_id:
        return None

    title = clean_text(entry.get("title"))
    if not title:
        return None

    channel = clean_text(entry.get("channel") or entry.get("uploader"))
    text = normalize_key(f"{title} {channel}")
    blocked_channels = {normalize_key(channel) for channel in config.get("blocked_channels", [])}
    if normalize_key(channel) in blocked_channels:
        return None
    if not valid_music_text(text, config):
        return None

    published_at = parse_timestamp(entry.get("timestamp"), timezone)
    if not published_at or published_at < cutoff_time:
        return None

    duration = entry.get("duration")
    min_duration = int(config.get("portal", {}).get("min_duration_seconds") or 600)
    if (seconds := seconds_value(duration)) is None or seconds < min_duration:
        return None
    score = score_entry(entry, search, config, rank)
    if score < float(config.get("minimum_score", 8.0)):
        return None
    return {
        "id": video_id,
        "title": title,
        "channel": channel,
        "url": YOUTUBE_WATCH.format(video_id=video_id),
        "embed_url": f"https://www.youtube.com/embed/{video_id}",
        "thumbnail": YOUTUBE_THUMB.format(video_id=video_id),
        "duration": duration,
        "duration_text": seconds_to_clock(duration),
        "published": clean_text(entry.get("upload_date")),
        "published_at": published_at.isoformat(),
        "published_ts": int(published_at.timestamp()),
        "latest_rank": rank,
        "category": search.get("id"),
        "category_label": search.get("label"),
        "query": search.get("query"),
        "score": score,
    }


def merge_track(existing: dict, incoming: dict) -> dict:
    if incoming["score"] > existing["score"]:
        merged = {**existing, **incoming}
    else:
        merged = dict(existing)

    categories = set(existing.get("categories") or [existing.get("category")])
    categories.add(incoming.get("category"))
    labels = set(existing.get("category_labels") or [existing.get("category_label")])
    labels.add(incoming.get("category_label"))
    queries = set(existing.get("queries") or [existing.get("query")])
    queries.add(incoming.get("query"))

    merged["categories"] = sorted(item for item in categories if item)
    merged["category_labels"] = sorted(item for item in labels if item)
    merged["queries"] = sorted(item for item in queries if item)
    return merged


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    portal = config.get("portal", {})
    timezone = portal.get("timezone", "Asia/Kolkata")
    max_results = int(os.environ.get("MUSIC_MAX_RESULTS_PER_QUERY") or portal.get("max_results_per_query") or 12)
    max_tracks = int(os.environ.get("MUSIC_MAX_TRACKS") or portal.get("max_tracks") or 64)
    timeout = int(os.environ.get("MUSIC_SEARCH_TIMEOUT") or 90)
    feed_timeout = int(os.environ.get("MUSIC_FEED_TIMEOUT") or 20)
    duration_timeout = int(os.environ.get("MUSIC_DURATION_TIMEOUT") or 15)
    sort_mode = str(os.environ.get("MUSIC_SEARCH_SORT") or portal.get("search_sort") or "date").lower()
    recent_hours = int(os.environ.get("MUSIC_RECENT_HOURS") or portal.get("recent_hours") or 24)
    youtube_search_enabled = str(os.environ.get("MUSIC_YOUTUBE_SEARCH_ENABLED", "")).lower()
    if youtube_search_enabled in {"1", "true", "yes"}:
        use_youtube_search = True
    elif youtube_search_enabled in {"0", "false", "no"}:
        use_youtube_search = False
    else:
        use_youtube_search = bool(portal.get("youtube_search_enabled", not config.get("rss_feeds")))
    now = datetime.now(ZoneInfo(timezone))
    cutoff_time = now - timedelta(hours=recent_hours)

    tracks_by_id: dict[str, dict] = {}
    duration_cache: dict[str, int | None] = {}
    errors = []
    min_duration = int(portal.get("min_duration_seconds") or 1200)

    for rank, track in enumerate(config.get("audio_tracks", []), start=1):
        item = item_from_audio_track(track, rank, now, min_duration)
        if not item:
            continue
        tracks_by_id[item["id"]] = {
            **item,
            "categories": [item["category"]],
            "category_labels": [item["category_label"]],
            "queries": [item["query"]],
        }

    streams_enabled = str(os.environ.get("MUSIC_LIVE_STREAMS_ENABLED", portal.get("live_streams_enabled", False))).lower()
    if streams_enabled in {"1", "true", "yes", "on"}:
        for rank, stream in enumerate(config.get("audio_streams", []), start=1):
            item = item_from_audio_stream(stream, rank, now)
            if not item:
                continue
            tracks_by_id[item["id"]] = {
                **item,
                "categories": [item["category"]],
                "category_labels": [item["category_label"]],
                "queries": [item["query"]],
            }

    feeds_enabled = str(os.environ.get("MUSIC_YOUTUBE_FEEDS_ENABLED", portal.get("youtube_feeds_enabled", False))).lower()
    if feeds_enabled in {"1", "true", "yes", "on"}:
        for feed in config.get("rss_feeds", []):
            try:
                items = feed_items(feed, config, cutoff_time, timezone, feed_timeout, duration_cache, duration_timeout)
            except Exception as exc:
                errors.append(f"{feed.get('id') or feed.get('url')}: {type(exc).__name__}: {exc}")
                continue
            for item in items:
                existing = tracks_by_id.get(item["id"])
                tracks_by_id[item["id"]] = merge_track(existing, item) if existing else {
                    **item,
                    "categories": [item["category"]],
                    "category_labels": [item["category_label"]],
                    "queries": [item["query"]],
                }

    if use_youtube_search:
        for search in config.get("searches", []):
            query = clean_text(search.get("query"))
            if not query:
                continue
            try:
                entries = run_search(query, max_results, timeout, sort_mode, cutoff_time)
            except Exception as exc:
                errors.append(f"{query}: {type(exc).__name__}: {exc}")
                continue

            for rank, entry in enumerate(entries, start=1):
                item = item_from_entry(entry, search, config, rank, cutoff_time, timezone)
                if not item:
                    continue
                existing = tracks_by_id.get(item["id"])
                tracks_by_id[item["id"]] = merge_track(existing, item) if existing else {
                    **item,
                    "categories": [item["category"]],
                    "category_labels": [item["category_label"]],
                    "queries": [item["query"]],
                }

    classic_limit = int(portal.get("classic_items_per_category", 2))
    classic_counts: dict[str, int] = {}
    for rank, track in enumerate(config.get("classic_tracks", []), start=1):
        category = clean_text(track.get("category"))
        if not category or classic_counts.get(category, 0) >= classic_limit:
            continue
        item = item_from_classic(track, config, rank, timezone)
        if not item:
            continue
        classic_counts[category] = classic_counts.get(category, 0) + 1
        existing = tracks_by_id.get(item["id"])
        tracks_by_id[item["id"]] = merge_track(existing, item) if existing else {
            **item,
            "categories": [item["category"]],
            "category_labels": [item["category_label"]],
            "queries": [item["query"]],
        }

    tracks = sorted(
        tracks_by_id.values(),
        key=lambda item: (item.get("published_ts", 0), item.get("score", 0)),
        reverse=True,
    )[:max_tracks]

    output = {
        "generated_at": now.isoformat(),
        "mode": "latest",
        "search_sort": sort_mode,
        "recent_hours": recent_hours,
        "min_duration_seconds": min_duration,
        "classic_items_per_category": classic_limit,
        "cutoff_time": cutoff_time.isoformat(),
        "youtube_search_enabled": use_youtube_search,
        "tracks": tracks,
        "searches": config.get("searches", []),
        "audio_tracks": config.get("audio_tracks", []),
        "audio_streams": config.get("audio_streams", []),
        "rss_feeds": config.get("rss_feeds", []),
        "warning": "; ".join(errors) if errors and tracks else None,
        "error": "; ".join(errors) if errors and not tracks else None,
        "stale": False,
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(output.get('tracks') or [])} tracks to {OUTPUT_PATH.relative_to(ROOT)}")
    if errors:
        print("Warnings:")
        for error in errors:
            print(f"- {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
