#!/usr/bin/env python3
"""
fetch_feed.py — download the free episodes of a podcast RSS feed into the
Podcasts library, skipping anything already on disk.

Exists for feeds that only keep a rolling window of episodes free (Hardcore
History moves shows into the paid archive once they age out of the feed).
Run monthly from cron; each run grabs whatever is in the feed and missing
locally, so an episode only has to be seen once while it is still free.

The enclosure's `length` attribute is only used for the progress line — it is
routinely off from the real file (by 128 bytes up to a few MB on this feed),
so the completeness check is against the server's Content-Length instead.

Files are named by the enclosure URL's basename (dchha63_Supernova_in_the_
East_II.mp3), which is how the existing library is named from show 59 on and
is the dedupe key — same basename already present means skip. Downloads go to
<name>.part and are renamed only once the byte count matches the server's
Content-Length; an interrupted .part is resumed with a Range request on the
next run.

Usage: fetch_feed.py [--dry-run] [--list] [FEED ...]
  --dry-run   report what would be downloaded, fetch nothing
  --list      print every feed item with its have/missing status
  FEED        restrict to the named feed(s) in FEEDS (default: all)

Stdlib only; no pip deps. No secrets needed.
"""

import argparse
import fcntl
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

# Destination dir name → feed URL. Dirs are relative to DEST_BASE.
FEEDS = {
    "Hardcore History": "https://feeds.feedburner.com/dancarlin/history?format=xml",
    "Hardcore History Addendum": "https://dchhaddendum.libsyn.com/rss",
}

# The decrypted RW view of local media (encfs). MOUNT is checked before writing:
# with encfs down, DEST_BASE is a plain empty dir under the mountpoint and
# anything written there is plaintext that vanishes once encfs mounts again.
MOUNT = "/home/plex/local-sorted"
DEST_BASE = f"{MOUNT}/Podcasts"

LOCK_FILE = "/tmp/fetch_feed.lock"
# Curl/python-requests default UAs get blocked in enough places to be worth avoiding.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) cpam-fetch-feed/1.0"
CHUNK = 1 << 20
TIMEOUT = 60


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def http_get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def fetch_items(feed_url: str) -> list[dict]:
    """Return the feed's items as [{title, url, filename, length}], newest first."""
    with http_get(feed_url) as resp:
        root = ET.fromstring(resp.read())
    items = []
    for item in root.iter("item"):
        enc = item.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        url = enc.get("url")
        # Basename of the URL path, decoded; the query string (tracking) is dropped.
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(url).path))
        name = name.replace("/", "_").strip()
        if not name or name in (".", ".."):
            log(f"WARNING: no usable filename in enclosure url {url!r}, skipping")
            continue
        try:
            length = int(enc.get("length") or 0)
        except ValueError:
            length = 0
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "url": url,
            "filename": name,
            "length": length,
        })
    return items


def download(url: str, dest: str) -> bool:
    """Stream url to dest via dest.part, resuming a leftover .part. True on success."""
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    try:
        resp = http_get(url, headers)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:
            # Range not satisfiable: the .part is already the whole file.
            resp = None
        else:
            log(f"ERROR: {url}: HTTP {e.code}")
            return False
    except (urllib.error.URLError, OSError) as e:
        log(f"ERROR: {url}: {e}")
        return False

    if resp is not None:
        with resp:
            if resp.status == 206:
                mode, expected = "ab", have + int(resp.headers.get("Content-Length") or 0)
                log(f"  resuming from {have} bytes")
            else:
                # Server ignored the Range header; start over.
                mode, have = "wb", 0
                expected = int(resp.headers.get("Content-Length") or 0)
            written = have
            try:
                with open(part, mode) as fh:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        written += len(chunk)
            except OSError as e:
                # Socket timeout / reset mid-stream; keep the .part for next time.
                log(f"ERROR: {url}: {e} after {written} bytes")
                return False
        # http.client returns b'' rather than raising when the server closes
        # early, so the size check is what catches a truncated transfer.
        if expected and written != expected:
            log(f"ERROR: {url}: got {written} of {expected} bytes, keeping .part")
            return False

    os.replace(part, dest)
    return True


def process_feed(name: str, feed_url: str, dry_run: bool, list_only: bool) -> tuple[int, int]:
    """Returns (downloaded, failed)."""
    dest_dir = os.path.join(DEST_BASE, name)
    try:
        items = fetch_items(feed_url)
    except (urllib.error.URLError, OSError, ET.ParseError) as e:
        log(f"ERROR: {name}: could not fetch feed: {e}")
        return 0, 1
    if not items:
        # A soft error page still parses as XML; treat "no episodes" as broken.
        log(f"ERROR: {name}: feed has no enclosures")
        return 0, 1

    if list_only:
        for it in items:
            state = "have" if os.path.exists(os.path.join(dest_dir, it["filename"])) else "MISSING"
            print(f"{state:8} {it['filename']}  ({it['title']}, {it['length'] / 1e6:.0f} MB)")
        return 0, 0

    missing = [it for it in items if not os.path.exists(os.path.join(dest_dir, it["filename"]))]
    log(f"{name}: {len(items)} in feed, {len(items) - len(missing)} on disk, {len(missing)} to fetch")
    if not missing:
        return 0, 0
    if dry_run:
        for it in missing:
            log(f"  would fetch {it['filename']} ({it['length'] / 1e6:.0f} MB)")
        return 0, 0

    os.makedirs(dest_dir, exist_ok=True)
    ok = failed = 0
    for it in reversed(missing):  # oldest first, so a partial run leaves a contiguous set
        log(f"  fetching {it['filename']} ({it['length'] / 1e6:.0f} MB) — {it['title']}")
        started = time.monotonic()
        if download(it["url"], os.path.join(dest_dir, it["filename"])):
            size = os.path.getsize(os.path.join(dest_dir, it["filename"]))
            log(f"  done {it['filename']}: {size / 1e6:.0f} MB in {time.monotonic() - started:.0f}s")
            ok += 1
        else:
            failed += 1
    return ok, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("feeds", nargs="*", metavar="FEED")
    args = ap.parse_args()

    unknown = [f for f in args.feeds if f not in FEEDS]
    if unknown:
        print(f"ERROR: unknown feed(s) {unknown}; known: {list(FEEDS)}", file=sys.stderr)
        return 2
    selected = {k: v for k, v in FEEDS.items() if not args.feeds or k in args.feeds}

    if not os.path.ismount(MOUNT):
        log(f"ERROR: {MOUNT} is not mounted (encfs down?); refusing to write")
        return 1

    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run is still in progress, exiting")
        return 0

    total_ok = total_failed = 0
    for name, url in selected.items():
        ok, failed = process_feed(name, url, args.dry_run, args.list)
        total_ok += ok
        total_failed += failed
    if not args.list:
        log(f"finished: {total_ok} downloaded, {total_failed} failed")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
