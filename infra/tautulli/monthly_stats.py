#!/usr/bin/env python3
"""
monthly_stats.py — post Tautulli's "most popular" home stats to Discord.

Pulls the last N days of popular movies and TV shows from Tautulli's
get_home_stats API (the same data as the homepage widget) and posts one
compact message to a Discord webhook: two embeds (Movies, TV Shows), each a
ranked list of titles with viewer/play counts.

Intended to run from cron on the 1st of each month. Stdlib only; no pip deps.

Environment:
  TAUTULLI_API_KEY   required
  STATS_WEBHOOK_URL  required — Discord webhook URL
  TAUTULLI_URL       default http://127.0.0.1:8181
  STATS_COUNT        titles per category (max 25), default 5
  STATS_DAYS         lookback window in days, default 30
  STATS_THREAD_ID    optional — post into this Discord thread
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: required environment variable {name} is not set", file=sys.stderr)
        sys.exit(1)
    return value


TAUTULLI_API_KEY = require_env("TAUTULLI_API_KEY")
WEBHOOK_URL = require_env("STATS_WEBHOOK_URL")
TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "http://127.0.0.1:8181").rstrip("/")
COUNT = min(int(os.environ.get("STATS_COUNT", "5")), 25)
DAYS = int(os.environ.get("STATS_DAYS", "30"))
THREAD_ID = os.environ.get("STATS_THREAD_ID", "").strip()

CATEGORIES = (
    # (stat_id, embed title, embed color)
    ("popular_movies", "🎬 Movies", 0xE5A00D),
    ("popular_tv", "📺 TV Shows", 0x2E86C1),
)


def tautulli(cmd: str, **params) -> object:
    query = urllib.parse.urlencode({"apikey": TAUTULLI_API_KEY, "cmd": cmd, **params})
    with urllib.request.urlopen(f"{TAUTULLI_URL}/api/v2?{query}", timeout=30) as resp:
        data = json.loads(resp.read())["response"]
    if data.get("result") != "success":
        raise RuntimeError(f"Tautulli {cmd} failed: {data.get('message')}")
    return data["data"]


def format_row(rank: int, row: dict) -> str:
    title = row.get("title", "?")
    year = row.get("year")
    viewers = row.get("users_watched")
    plays = row.get("total_plays")
    stats = " · ".join(
        part
        for part in (
            f"{viewers} viewer{'s' if str(viewers) != '1' else ''}" if viewers else None,
            f"{plays} play{'s' if str(plays) != '1' else ''}" if plays else None,
        )
        if part
    )
    line = f"`{rank:>2}.` **{title}**" + (f" ({year})" if year else "")
    return f"{line} — {stats}" if stats else line


def post_webhook(payload: dict) -> None:
    query = {"wait": "true"}
    if THREAD_ID:
        query["thread_id"] = THREAD_ID
    req = urllib.request.Request(
        f"{WEBHOOK_URL}?{urllib.parse.urlencode(query)}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
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

    embeds = []
    for stat_id, title, color in CATEGORIES:
        rows = stats.get(stat_id, [])[:COUNT]
        if not rows:
            print(f"WARN: no rows for {stat_id}; skipping", file=sys.stderr)
            continue
        embeds.append(
            {
                "title": title,
                "description": "\n".join(
                    format_row(rank, row) for rank, row in enumerate(rows, start=1)
                ),
                "color": color,
            }
        )

    if not embeds:
        print("ERROR: nothing to post — no home stats returned", file=sys.stderr)
        sys.exit(1)

    post_webhook(
        {
            "content": f"## 📊 Most popular on CPAM — last {DAYS} days",
            "embeds": embeds,
        }
    )
    print(f"Posted 1 message with {len(embeds)} embed(s)")


if __name__ == "__main__":
    main()
