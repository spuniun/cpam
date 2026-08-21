#!/usr/bin/env python3
"""
error_digest.py — push a one-message Pushover digest of the latest Kometa run.

Parses Kometa's meta.log (the current/most recent run — Kometa rotates it per
run), collects every [ERROR]/[CRITICAL] line, dedupes them with counts, and
sends a single Pushover notification. Always sends, even on a clean run, so
the daily message doubles as a heartbeat: no digest by breakfast means kometa
didn't run.

Errors matching BENIGN below are counted but not listed individually — see the
comment there for why. They are still reported, on one tail line, so a sudden
jump in their volume is visible without them burying the actionable errors.

Intended to run from host cron daily, after the 05:00 kometa run, sourcing the
same .env as the compose stack (see CLAUDE.md):

  0 9 * * * cd /home/plex/cpam/infra && bash -c 'set -a; . ./.env; set +a; ./kometa/error_digest.py'

Stdlib only; no pip deps.

Environment:
  PUSHOVER_APP_TOKEN  required — application token (https://pushover.net/apps/build)
  PUSHOVER_USER_KEY   required — your Pushover user key
  KOMETA_LOG          default /var/lib/plexmediaserver/.config/plex-meta-manager/config/logs/meta.log
  DIGEST_TOP          unique errors to list in the message, default 8

Flags:
  --dry-run           print the digest instead of sending it
  --show-benign       list the benign errors individually too (debugging)
"""

import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DRY_RUN = "--dry-run" in sys.argv[1:]
SHOW_BENIGN = "--show-benign" in sys.argv[1:]

# Pushover hard limits
MAX_MESSAGE = 1024
MAX_TITLE = 250

# meta.log line: "[2026-07-18 05:00:12,345] plex.py    [ERROR]    | message |"
# Continuation lines (tracebacks) carry no level tag and are skipped, which is
# what we want for a digest.
LOG_LINE = re.compile(
    r"^\[(?P<ts>[\d-]+ [\d:,]+)\] \S+\s+\[(?P<level>ERROR|CRITICAL)\]\s+(?P<msg>.*)$"
)

# Errors that fire every single run and that no config or metadata change can
# remove. Each was traced to its source before being listed here; do not add a
# pattern just because it is noisy. Grouped onto one tail line so the errors
# worth acting on are not pushed off the end of a 1024-char Pushover message.
#
#   overlay searches - a "default: resolution" overlay defines a variant per
#       resolution and runs a plex_search for each. A library that holds no 4K
#       (or no 720p, ...) matches nothing, and Kometa logs that at ERROR: the
#       overlay path swallows FilterFailed but an empty plex_search raises
#       plain Failed, so ignore_blank_results does not suppress it. Self-
#       correcting — the moment such an item is added, the search matches.
#   TMDb episodes   - Plex numbers episodes TVDb-style, which for specials is
#       <season><n> (Game of Thrones season 0 runs 401-416, 501-515, ...)
#       while TMDb's season 0 for the same show runs 1-314 in a different
#       layout. Same cause for the two-part finales TVDb splits and TMDb
#       merges (Friends S4-S9 E24). Reconciling means renumbering the library
#       away from Sonarr's scheme.
#   MDBList items   - titles MDBList has no record for. Verified by querying
#       MDBList directly with the IDs Plex holds: all correctly matched on the
#       modern Plex agent, all 404. A third-party data gap, not a mismatch.
#   TMDb movies     - dead TMDb IDs carried by the third-party lists the
#       collections are built from, not by anything in the library: each one
#       shows up only in a "Missing Movies from Library" report, and every ID
#       seen so far 404s on TMDb today because the record was deleted or
#       merged upstream. Two were traced back to stale imdb_to_tmdb_map rows
#       (tt5362760, tt5362730, the Prometheus viral shorts), whose IMDb IDs
#       TMDb now resolves to nothing, so there is no id left to remap to.
#       The trade-off: a list entry that dies later is hidden too — but it is
#       just as unfixable from here, since we do not own the lists.
BENIGN = [
    ("overlay searches", re.compile(r"^Plex Error: \S+: No matches found with regex pattern ")),
    ("TMDb episodes", re.compile(r"^TMDb Error: No Episode found for TMDb ID ")),
    ("MDBList items", re.compile(r'^MDBList Error: 404 - \{"error":"Item not found"\}')),
    ("TMDb movies", re.compile(r"^TMDb Error: No Movie found for TMDb ID: ")),
]


def benign_label(msg: str):
    """Return the BENIGN group name for msg, or None if it is actionable."""
    for label, pattern in BENIGN:
        if pattern.match(msg):
            return label
    return None


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


HTML_BODY = re.compile(r"\s*<html>.*", re.IGNORECASE | re.DOTALL)


def clean_message(raw: str) -> str:
    # Strip the box-drawing border padding: "|      text      |"
    msg = raw.strip().strip("|").strip()
    # Plex surfaces HTTP failures with the server's whole HTML error page
    # appended. It carries nothing the status code does not already say and
    # crowds every other error out of a 1024-char message, so drop it.
    return HTML_BODY.sub("", msg).strip()


def parse_log(path: str):
    """Return (first_ts, error_counts_in_order, benign_counts_by_group, finished)."""
    first_ts = None
    counts = {}  # message -> count, insertion-ordered
    benign = {}  # BENIGN group label -> count, insertion-ordered
    criticals = set()  # messages seen at CRITICAL level
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
            if not msg:
                continue
            label = None if SHOW_BENIGN else benign_label(msg)
            if label:
                benign[label] = benign.get(label, 0) + 1
            else:
                counts[msg] = counts.get(msg, 0) + 1
                if match.group("level") == "CRITICAL":
                    criticals.add(msg)
    return first_ts, counts, benign, criticals, finished


def build_digest():
    if not os.path.exists(LOG_PATH):
        return "Kometa: no log found", f"{LOG_PATH} does not exist — has kometa ever run?"

    age_hours = (time.time() - os.path.getmtime(LOG_PATH)) / 3600
    first_ts, counts, benign, criticals, finished = parse_log(LOG_PATH)
    total = sum(counts.values())
    benign_total = sum(benign.values())

    # Counts in the title are actionable errors only; benign ones would swamp
    # the number and make every run look equally bad.
    if age_hours > 26:
        title = "Kometa: no recent run ⚠️"
        header = f"meta.log last touched {age_hours:.0f}h ago — is the container running?"
    elif total == 0:
        title = "Kometa: run clean ✅"
        header = f"Run started {first_ts or 'unknown'}; no actionable errors."
    else:
        title = f"Kometa: {total} error{'s' if total != 1 else ''} ({len(counts)} unique)"
        header = f"Run started {first_ts or 'unknown'}"

    if not finished and age_hours <= 26:
        header += " — no end-of-run marker; run still going or was interrupted."

    # CRITICALs first, then by frequency. A CRITICAL aborts the phase it happens
    # in — Kometa logs one and keeps going, so it is easy to miss — and it fires
    # once, which would otherwise rank it below every repeated ERROR and push it
    # off the end of the message.
    ranked = sorted(counts.items(), key=lambda item: (item[0] not in criticals, -item[1]))
    lines = [header]
    for msg, count in ranked[:TOP]:
        prefix = "⛔ " if msg in criticals else ""
        lines.append(f"{prefix}{count}× {msg}" if count > 1 else f"{prefix}{msg}")
    if len(ranked) > TOP:
        remainder = sum(count for _, count in ranked[TOP:])
        lines.append(f"…and {len(ranked) - TOP} more unique ({remainder} total)")

    tail = ""
    if benign_total:
        groups = ", ".join(f"{label} {count}" for label, count in sorted(benign.items(), key=lambda i: -i[1]))
        tail = f"+{benign_total} benign ({groups})"

    # Truncate the error list, never the benign tail — losing it would silently
    # turn a suppressed-but-counted class back into an unreported one.
    message = "\n".join(lines)
    budget = MAX_MESSAGE - (len(tail) + 1 if tail else 0)
    if len(message) > budget:
        message = message[: budget - 1] + "…"
    if tail:
        message = f"{message}\n{tail}"
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
