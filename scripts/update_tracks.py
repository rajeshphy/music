#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "searches.json"
OUTPUT_PATH = ROOT / "data" / "tracks.json"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v={video_id}"
YOUTUBE_THUMB = "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


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


def run_search(query: str, max_results: int, timeout: int) -> list[dict]:
    target = f"ytsearchdate{max_results}:{query}"
    command = [
        "yt-dlp",
        "--dump-single-json",
        "--flat-playlist",
        "--ignore-errors",
        "--no-warnings",
        "--quiet",
        target,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    payload = json.loads(completed.stdout or "{}")
    entries = payload.get("entries") or []
    return [entry for entry in entries if isinstance(entry, dict)]


def term_score(text: str, terms: list[str], value: float) -> float:
    score = 0.0
    padded = f" {text} "
    for term in terms:
        normalized = normalize_key(term)
        if normalized and normalized in padded:
            score += value
    return score


def duration_score(duration: object, text: str) -> float:
    try:
        seconds = int(float(duration))
    except (TypeError, ValueError):
        return 0.0

    if seconds < 120:
        return -3.0
    if seconds <= 900:
        return 1.5
    if seconds <= 5400 and " mix " in f" {text} ":
        return 1.0
    if seconds <= 7200:
        return -0.5
    return -2.0


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


def item_from_entry(entry: dict, search: dict, config: dict, rank: int) -> dict | None:
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
    blocked_channels = {normalize_key(channel) for channel in config.get("blocked_channels", [])}
    if normalize_key(channel) in blocked_channels:
        return None

    duration = entry.get("duration")
    score = score_entry(entry, search, config, rank)
    return {
        "id": video_id,
        "title": title,
        "channel": channel,
        "url": YOUTUBE_WATCH.format(video_id=video_id),
        "embed_url": f"https://www.youtube.com/embed/{video_id}",
        "thumbnail": YOUTUBE_THUMB.format(video_id=video_id),
        "duration": duration,
        "duration_text": seconds_to_clock(duration),
        "published": clean_text(entry.get("upload_date") or entry.get("timestamp")),
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
    previous = load_json(OUTPUT_PATH, {})

    tracks_by_id: dict[str, dict] = {}
    errors = []

    for search in config.get("searches", []):
        query = clean_text(search.get("query"))
        if not query:
            continue
        try:
            entries = run_search(query, max_results, timeout)
        except Exception as exc:
            errors.append(f"{query}: {type(exc).__name__}: {exc}")
            continue

        for rank, entry in enumerate(entries, start=1):
            item = item_from_entry(entry, search, config, rank)
            if not item:
                continue
            existing = tracks_by_id.get(item["id"])
            tracks_by_id[item["id"]] = merge_track(existing, item) if existing else {
                **item,
                "categories": [item["category"]],
                "category_labels": [item["category_label"]],
                "queries": [item["query"]],
            }

    tracks = sorted(tracks_by_id.values(), key=lambda item: item.get("score", 0), reverse=True)[:max_tracks]

    if not tracks and previous.get("tracks"):
        output = dict(previous)
        output["generated_at"] = datetime.now(ZoneInfo(timezone)).isoformat()
        output["stale"] = True
        output["error"] = "Refresh failed; kept previous tracks. " + "; ".join(errors)
    else:
        output = {
            "generated_at": datetime.now(ZoneInfo(timezone)).isoformat(),
            "tracks": tracks,
            "searches": config.get("searches", []),
            "error": "; ".join(errors) if errors else None,
            "stale": False,
        }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(output.get('tracks') or [])} tracks to {OUTPUT_PATH.relative_to(ROOT)}")
    if errors:
        print("Warnings:")
        for error in errors:
            print(f"- {error}")
    return 0 if output.get("tracks") else 1


if __name__ == "__main__":
    raise SystemExit(main())
