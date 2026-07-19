#!/usr/bin/env python3
"""
monthly_stats.py — post Tautulli's "most popular" home stats to Discord.

Pulls the last N days of popular movies and TV shows from Tautulli's
get_home_stats API (the same data as the homepage widget), downloads each
title's poster through Tautulli's image proxy, and posts one embed per title
to a Discord webhook — posters ride along as attachments so the Tautulli API
key never appears in any URL Discord sees.

Intended to run from cron on the 1st of each month. Stdlib only; no pip deps.

Environment:
  TAUTULLI_API_KEY   required
  STATS_WEBHOOK_URL  required — Discord webhook URL
  TAUTULLI_URL       default http://127.0.0.1:8181
  STATS_COUNT        titles per category (max 10), default 5
  STATS_DAYS         lookback window in days, default 30
  STATS_THREAD_ID    optional — post into this Discord thread
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: required environment variable {name} is not set", file=sys.stderr)
        sys.exit(1)
    return value


TAUTULLI_API_KEY = require_env("TAUTULLI_API_KEY")
WEBHOOK_URL = require_env("STATS_WEBHOOK_URL")
TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "http://127.0.0.1:8181").rstrip("/")
COUNT = min(int(os.environ.get("STATS_COUNT", "5")), 10)
DAYS = int(os.environ.get("STATS_DAYS", "30"))
THREAD_ID = os.environ.get("STATS_THREAD_ID", "").strip()

CATEGORIES = (
    # (stat_id, emoji, embed color, heading)
    ("popular_movies", "🎬", 0xE5A00D, "Movies"),
    ("popular_tv", "📺", 0x2E86C1, "TV Shows"),
)


def tautulli(cmd: str, as_json: bool = True, **params) -> object:
    query = urllib.parse.urlencode({"apikey": TAUTULLI_API_KEY, "cmd": cmd, **params})
    with urllib.request.urlopen(f"{TAUTULLI_URL}/api/v2?{query}", timeout=30) as resp:
        body = resp.read()
    if not as_json:
        return body
    data = json.loads(body)["response"]
    if data.get("result") != "success":
        raise RuntimeError(f"Tautulli {cmd} failed: {data.get('message')}")
    return data["data"]


def fetch_poster(row: dict) -> bytes | None:
    thumb = row.get("thumb") or row.get("grandparent_thumb")
    if not thumb:
        return None
    try:
        return tautulli(
            "pms_image_proxy",
            as_json=False,
            img=thumb,
            width=300,
            height=450,
            fallback="poster",
        )
    except (urllib.error.URLError, OSError) as exc:
        print(f"WARN: no poster for {row.get('title')}: {exc}", file=sys.stderr)
        return None


def build_message(rows: list, emoji: str, color: int, heading: str, lead: str):
    """Return (payload dict, [(filename, bytes), ...]) for one webhook post."""
    embeds, files = [], []
    for rank, row in enumerate(rows, start=1):
        title = row.get("title", "?")
        year = row.get("year")
        viewers = row.get("users_watched")
        plays = row.get("total_plays")
        stats = " · ".join(
            part
            for part in (
                f"{viewers} unique viewer{'s' if str(viewers) != '1' else ''}" if viewers else None,
                f"{plays} play{'s' if str(plays) != '1' else ''}" if plays else None,
            )
            if part
        )
        embed = {
            "title": f"{emoji} #{rank} · {title}" + (f" ({year})" if year else ""),
            "description": stats,
            "color": color,
        }
        poster = fetch_poster(row)
        if poster:
            name = f"poster_{heading[:2].lower()}{rank}.jpg"
            files.append((name, poster))
            embed["thumbnail"] = {"url": f"attachment://{name}"}
        embeds.append(embed)

    payload = {"content": f"{lead}**{heading}**", "embeds": embeds}
    return payload, files


def post_webhook(payload: dict, files: list) -> None:
    boundary = uuid.uuid4().hex
    parts = []

    def add_part(headers: str, content: bytes) -> None:
        parts.append(f"--{boundary}\r\n{headers}\r\n\r\n".encode() + content + b"\r\n")

    add_part(
        'Content-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json',
        json.dumps(payload).encode(),
    )
    for i, (name, blob) in enumerate(files):
        add_part(
            f'Content-Disposition: form-data; name="files[{i}]"; filename="{name}"\r\n'
            "Content-Type: image/jpeg",
            blob,
        )
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()

    query = {"wait": "true"}
    if THREAD_ID:
        query["thread_id"] = THREAD_ID
    url = f"{WEBHOOK_URL}?{urllib.parse.urlencode(query)}"

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            # Cloudflare fronts discord.com and 403s (error 1010) the default
            # Python-urllib user agent
            "User-Agent": "cpam-monthly-stats/1.0 (+https://cpam.tv)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        print(f"ERROR: Discord returned {exc.code}: {exc.read()[:300]}", file=sys.stderr)
        raise


def main() -> None:
    stats = {
        block["stat_id"]: block.get("rows", [])
        for block in tautulli(
            "get_home_stats", time_range=DAYS, stats_count=COUNT, stats_type="plays"
        )
    }

    posted = 0
    for index, (stat_id, emoji, color, heading) in enumerate(CATEGORIES):
        rows = stats.get(stat_id, [])[:COUNT]
        if not rows:
            print(f"WARN: no rows for {stat_id}; skipping", file=sys.stderr)
            continue
        lead = f"## 📊 Most popular on CPAM — last {DAYS} days\n" if index == 0 else ""
        payload, files = build_message(rows, emoji, color, heading, lead)
        post_webhook(payload, files)
        posted += 1

    if posted == 0:
        print("ERROR: nothing to post — no home stats returned", file=sys.stderr)
        sys.exit(1)
    print(f"Posted {posted} message(s) to Discord")


if __name__ == "__main__":
    main()
