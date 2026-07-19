#!/usr/bin/env python3
"""
error_digest.py — push a one-message Pushover digest of the latest Kometa run.

Parses Kometa's meta.log (the current/most recent run — Kometa rotates it per
run), collects every [ERROR]/[CRITICAL] line, dedupes them with counts, and
sends a single Pushover notification. Always sends, even on a clean run, so
the daily message doubles as a heartbeat: no digest by breakfast means kometa
didn't run.

Intended to run from host cron daily, after the 05:00 kometa run, sourcing the
same .env as the compose stack (see CLAUDE.md):

  0 8 * * * cd /home/plex/cpam && bash -c 'set -a; . ./.env; set +a; ./infra/kometa/error_digest.py'

Stdlib only; no pip deps.

Environment:
  PUSHOVER_APP_TOKEN  required — application token (https://pushover.net/apps/build)
  PUSHOVER_USER_KEY   required — your Pushover user key
  KOMETA_LOG          default /var/lib/plexmediaserver/.config/plex-meta-manager/config/logs/meta.log
  DIGEST_TOP          unique errors to list in the message, default 8

Flags:
  --dry-run           print the digest instead of sending it
"""

import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DRY_RUN = "--dry-run" in sys.argv[1:]

# Pushover hard limits
MAX_MESSAGE = 1024
MAX_TITLE = 250

# meta.log line: "[2026-07-18 05:00:12,345] plex.py    [ERROR]    | message |"
# Continuation lines (tracebacks) carry no level tag and are skipped, which is
# what we want for a digest.
LOG_LINE = re.compile(
    r"^\[(?P<ts>[\d-]+ [\d:,]+)\] \S+\s+\[(?P<level>ERROR|CRITICAL)\]\s+(?P<msg>.*)$"
)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: required environment variable {name} is not set", file=sys.stderr)
        sys.exit(1)
    return value


APP_TOKEN = require_env("PUSHOVER_APP_TOKEN")
USER_KEY = require_env("PUSHOVER_USER_KEY")
LOG_PATH = os.environ.get(
    "KOMETA_LOG",
    "/var/lib/plexmediaserver/.config/plex-meta-manager/config/logs/meta.log",
)
TOP = int(os.environ.get("DIGEST_TOP", "8"))


def clean_message(raw: str) -> str:
    # Strip the box-drawing border padding: "|      text      |"
    return raw.strip().strip("|").strip()


def parse_log(path: str):
    """Return (first_ts, error_counts_in_order, finished)."""
    first_ts = None
    counts = {}  # message -> count, insertion-ordered
    finished = False
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if first_ts is None and line.startswith("["):
                first_ts = line[1 : line.find("]")]
            # Kometa prints "Finished: ..." / "Run Time: ..." in its end-of-run
            # summary; either marker means the run completed.
            if "Run Time:" in line or "Finished:" in line:
                finished = True
            match = LOG_LINE.match(line)
            if not match:
                continue
            msg = clean_message(match.group("msg"))
            if msg:
                counts[msg] = counts.get(msg, 0) + 1
    return first_ts, counts, finished


def build_digest():
    if not os.path.exists(LOG_PATH):
        return "Kometa: no log found", f"{LOG_PATH} does not exist — has kometa ever run?"

    age_hours = (time.time() - os.path.getmtime(LOG_PATH)) / 3600
    first_ts, counts, finished = parse_log(LOG_PATH)
    total = sum(counts.values())

    if age_hours > 26:
        title = "Kometa: no recent run ⚠️"
        header = f"meta.log last touched {age_hours:.0f}h ago — is the container running?"
    elif total == 0:
        title = "Kometa: run clean ✅"
        header = f"Run started {first_ts or 'unknown'}; no errors."
    else:
        title = f"Kometa: {total} error{'s' if total != 1 else ''} ({len(counts)} unique)"
        header = f"Run started {first_ts or 'unknown'}"

    if not finished and age_hours <= 26:
        header += " — no end-of-run marker; run still going or was interrupted."

    ranked = sorted(counts.items(), key=lambda item: -item[1])
    lines = [header]
    for msg, count in ranked[:TOP]:
        lines.append(f"{count}× {msg}" if count > 1 else msg)
    if len(ranked) > TOP:
        remainder = sum(count for _, count in ranked[TOP:])
        lines.append(f"…and {len(ranked) - TOP} more unique ({remainder} total)")

    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE:
        message = message[: MAX_MESSAGE - 1] + "…"
    return title[:MAX_TITLE], message


def send_pushover(title: str, message: str) -> None:
    data = urllib.parse.urlencode(
        {"token": APP_TOKEN, "user": USER_KEY, "title": title, "message": message}
    ).encode()
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=data,
        headers={"User-Agent": "cpam-kometa-digest/1.0 (+https://cpam.tv)"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        print(f"ERROR: Pushover returned {exc.code}: {exc.read()[:300]}", file=sys.stderr)
        raise


def main() -> None:
    title, message = build_digest()
    if DRY_RUN:
        print(f"--- {title} ---\n{message}")
        return
    send_pushover(title, message)
    print(f"Sent digest: {title}")


if __name__ == "__main__":
    main()
