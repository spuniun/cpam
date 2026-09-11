#!/usr/bin/env python3
"""
abs_match.py — bulk-match Audiobookshelf podcast episodes to their feed data.

ABS only fills in episode metadata for episodes it downloads itself. Files that
arrive from a folder scan get a title from the ID3 tag or filename and nothing
else, and its own "quick match" is a near-exact fuzzy title search (fuse.js at
0.1) that misses whenever the tag title carries a filename prefix like
`dchha64 Supernova in the East III`. This matches by **episode number** instead,
parsed from the filename on our side and from the title on the feed side, and
writes the same fields ABS's own matcher would (title, subtitle, description,
enclosure, episode, season, pubDate, publishedAt) through the episode PATCH
endpoint. Episodes that already carry an enclosure URL count as matched and are
left alone unless --override.

Feeds usually only list a rolling window, so episodes older than the feed can
be filled from a JSON file instead (--import): a list of {num, title,
description, pubDate|date, ...} records, e.g. scraped from the show's website or
an archived copy of the feed (see podcasts/metadata/). Imported records carry
no enclosure, so they stay eligible for a real feed match later.

Usage: abs_match.py [--dry-run] [--override] [--feed URL] [--import FILE] [PODCAST ...]
  --dry-run       report what would change, write nothing
  --override      also rewrite episodes that are already matched
  --feed URL      match against this feed instead of the podcast's own (e.g. a
                  Wayback Machine copy: http://web.archive.org/web/<ts>id_/<url>)
  --no-enclosure  don't write the enclosure (for archived feeds whose media
                  URLs are dead; the episodes then stay "unmatched" in ABS's
                  eyes and eligible for a live-feed match later)
  --import FILE   apply metadata records from FILE instead of a feed — a JSON
                  list of {num, title, description, pubDate|date, …} (date as
                  'July 27, 2006' or 'Jul 27, 2006'), or a saved RSS file (e.g. an archived copy of the feed
                  fetched from the Wayback Machine; ABS itself cannot reach
                  web.archive.org from the container). RSS imports never write
                  the enclosure.
  PODCAST         podcast title(s) as shown in ABS; default: every podcast with
                  a number pattern in PATTERNS

Environment: ABS_API_KEY (required), ABS_URL (default http://127.0.0.1:13378).
Stdlib only.
"""

import argparse
import difflib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime

ABS_URL = os.environ.get("ABS_URL", "http://127.0.0.1:13378").rstrip("/")

# Podcast title in ABS → (regex over the local filename, regex over the feed title).
# Group 1 of each is the episode number; leading zeros are ignored when comparing.
# The filename side may be a list of regexes, tried in order.
PATTERNS = {
    "Dan Carlin's Hardcore History": (r"^(?:dchha)?(\d+)", r"^Show (\d+)\b"),
    "Dan Carlin's Hardcore History: Addendum": (r"(?:addendum|ep|dchha?)[-_ ]?(\d+)", r"^EP ?(\d+)\b"),
    # Premium episodes are filed as <main ep>.<premium #> locally but numbered
    # "05 - PREMIUM - …" in the feed, a separate sequence from the regular shows.
    # A regex's optional group 2 marks that sequence: the key becomes "P5".
    "Weird Medicine: The Podcast": ([r"^\d+\.(\d+) - (PREMIUM)", r"^(\d+(?:\.\d+)?)"],
                                    r"^(\d+(?:\.\d+)?)\s*[-=]\s*(PREMIUM)?"),
}

# A number match must also look like the same episode: premium/bonus items reuse
# numbers ("10 - PREMIUM - Podcastus Interruptus" vs "010 - Weird Medicine vs Davey
# Mac"), so titles are compared with the number prefix stripped.
TITLE_SIMILARITY = 0.5

# What a feed match writes, in ABS's own order (Scanner.updateEpisodeWithMatch).
MATCH_KEYS = ("title", "subtitle", "description", "episode", "episodeType", "season", "pubDate", "publishedAt")


def log(msg: str) -> None:
    print(msg, flush=True)


def api(method: str, path: str, body=None):
    key = os.environ.get("ABS_API_KEY", "").strip()
    if not key:
        sys.exit("ERROR: ABS_API_KEY is not set")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{ABS_URL}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read() or b"null")


def norm(num: str) -> str:
    """'081' → '81', '358.12' → '358.12'."""
    return num.lstrip("0") or "0"


def key_of(m) -> str:
    """Episode key from a PATTERNS match: the number, prefixed 'P' if the premium group matched."""
    return ("P" if m.re.groups > 1 and m.group(2) else "") + norm(m.group(1))


def podcasts_in_library():
    """Every podcast library item, expanded (episodes included)."""
    items = []
    for lib in api("GET", "/api/libraries")["libraries"]:
        if lib["mediaType"] != "podcast":
            continue
        page = api("GET", f"/api/libraries/{lib['id']}/items?limit=0")
        for it in page["results"]:
            items.append(api("GET", f"/api/items/{it['id']}?expanded=1"))
    return items


def feed_records(feed_url: str, title_re: str) -> dict:
    """Feed episodes keyed by episode number, parsed by ABS itself so the fields match a UI match exactly."""
    feed = api("POST", "/api/podcasts/feed", {"rssFeed": feed_url})
    if not feed or not feed.get("podcast"):
        sys.exit(f"ERROR: ABS could not parse feed {feed_url}")
    recs = {}
    for ep in feed["podcast"]["episodes"]:
        m = re.match(title_re, ep["title"].strip(), re.I)
        if not m:
            continue
        num = key_of(m)
        if num in recs:
            log(f"  feed lists {num!r} twice, keeping the first ({recs[num]['title']!r})")
            continue
        rec = {k: ep.get(k) for k in MATCH_KEYS}
        rec["enclosure"] = ep.get("enclosure") or None
        recs[num] = rec
    return recs


ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


def rss_records(path: str) -> list:
    """Items of a saved RSS file as import records (no enclosure — the media URLs are usually dead)."""
    recs = []
    for item in ET.parse(path).getroot().iter("item"):
        title = (item.findtext("title") or "").strip()
        desc = item.findtext(CONTENT + "encoded") or item.findtext("description") or item.findtext(ITUNES + "summary") or ""
        recs.append({"num": None, "title": title, "description": desc.strip(),
                     "subtitle": (item.findtext(ITUNES + "subtitle") or "").strip(),
                     "pubDate": (item.findtext("pubDate") or "").strip(),
                     "episode": (item.findtext(ITUNES + "episode") or "").strip(),
                     "season": (item.findtext(ITUNES + "season") or "").strip(),
                     "episodeType": (item.findtext(ITUNES + "episodeType") or "full").strip()})
    return recs


def parse_date(text: str) -> datetime:
    """'July 27, 2006' or 'Jun 27, 2012' → noon UTC that day (no time of day is known)."""
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(hour=12, tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"unrecognised date {text!r}")


def import_records(path: str, title_re: str) -> dict:
    """JSON or RSS file → the same shape as feed_records. Accepts pubDate (RFC 2822) or date ('July 27, 2006')."""
    with open(path) as fh:
        is_rss = fh.read(200).lstrip().startswith("<")
    rows = rss_records(path) if is_rss else json.load(open(path))
    recs = {}
    for r in rows:
        if r.get("num") is None:
            m = re.match(title_re, r["title"], re.I)
            if not m:
                continue
            r["num"] = key_of(m)
        if norm(str(r["num"])) in recs:
            log(f"  {path} lists {r['num']!r} twice, keeping the first ({recs[norm(str(r['num']))]['title']!r})")
            continue
        num = norm(str(r["num"]))
        rec = {"title": r["title"], "subtitle": r.get("subtitle", ""), "description": r.get("description", ""),
               "episode": str(r.get("episode") or num.lstrip("P")), "episodeType": r.get("episodeType") or "full",
               "season": str(r.get("season", "")), "enclosure": None}
        when = None
        if r.get("pubDate"):
            when = parsedate_to_datetime(r["pubDate"])
        elif r.get("date"):
            when = parse_date(r["date"])
        if when:
            rec["pubDate"] = format_datetime(when)
            rec["publishedAt"] = int(when.timestamp() * 1000)
        else:
            rec["pubDate"] = rec["publishedAt"] = None
        recs[num] = rec
    return recs


def title_key(text: str) -> str:
    """Lowercased letters/digits with any leading numbering/prefix junk removed."""
    text = re.sub(r"^(?:show|ep|dchha?|dchh[-_ ]?addendum|addendum)?[-_ ]?\d+(?:\.\d+)?[a-z]?\s*[-=_:]*\s*", "", text.strip(), flags=re.I)
    return re.sub(r"[^a-z0-9]", "", text.lower())


def similar(a: str, b: str) -> float:
    ka, kb = title_key(a), title_key(b)
    if not ka or not kb:
        return 0.0
    if ka in kb or kb in ka:
        return 1.0
    return difflib.SequenceMatcher(None, ka, kb).ratio()


def episode_num(ep: dict, file_re) -> str | None:
    name = ep.get("audioFile", {}).get("metadata", {}).get("filename") or ""
    for pattern in [file_re] if isinstance(file_re, str) else file_re:
        m = re.search(pattern, name, re.I)
        if m:
            return key_of(m)
    return None


def title_num(ep: dict, title_re: str) -> str | None:
    m = re.match(title_re, ep.get("title") or "", re.I)
    return key_of(m) if m else None


def text_of(html_: str) -> str:
    """Tags stripped, entities decoded, whitespace collapsed — ABS's sanitizer rewrites markup on save, so compare content."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", html_ or "")).split())


def build_payload(ep: dict, rec: dict, clear_enclosure: bool = False) -> dict:
    """Only the fields that actually differ, so the log shows what changed."""
    payload = {}
    for k in MATCH_KEYS:
        v = rec.get(k)
        if v is None or v == "":
            continue  # nothing to say; don't blank what is there
        if k == "publishedAt":
            if ep.get("publishedAt") != v:
                payload[k] = v
        elif k in ("description", "subtitle"):
            if text_of(ep.get(k)) != text_of(v):
                payload[k] = v
        elif (ep.get(k) or "") != v:
            payload[k] = v
    enc = rec.get("enclosure")
    if enc and (ep.get("enclosure") or {}).get("url") != enc["url"]:
        payload["enclosure"] = {"url": enc["url"], "type": enc.get("type"), "length": enc.get("length")}
    elif not enc and clear_enclosure and (ep.get("enclosure") or {}).get("url"):
        payload["enclosure"] = None
    return payload


def process(item: dict, recs: dict, file_re, title_re: str, args) -> tuple[int, int, int]:
    """Returns (updated, skipped-already-matched, unmatched)."""
    updated = skipped = unmatched = 0
    for ep in item["media"]["episodes"]:
        num = episode_num(ep, file_re)
        rec = recs.get(num) if num else None
        if not rec:
            unmatched += 1
            continue
        filename = ep.get("audioFile", {}).get("metadata", {}).get("filename") or ""
        # A matched episode whose title carries a different number than its file is
        # a wrong match (ABS's fuzzy title search pairing "100 - Prepare to Be
        # Disappointed" with "300 - … Again"); redo it, dropping the bad enclosure.
        matched = bool((ep.get("enclosure") or {}).get("url"))
        wrong = matched and title_num(ep, title_re) not in (None, num)
        if wrong:
            log(f"  {num:>7}  WRONG MATCH: {filename!r} carries {ep['title']!r}; redoing")
        score = max(similar(ep["title"], rec["title"]), similar(filename, rec["title"]))
        if score < TITLE_SIMILARITY and not wrong:  # a wrong match's title is known junk; trust the file number
            log(f"  {num:>7}  REJECTED (title {score:.2f}): {filename!r} vs feed {rec['title']!r}")
            unmatched += 1
            continue
        if matched and not wrong and not args.override:
            skipped += 1
            continue
        payload = build_payload(ep, rec, clear_enclosure=wrong)
        if not payload:
            skipped += 1
            continue
        log(f"  {num:>7}  {ep['title']!r} -> {rec['title']!r}  [{', '.join(payload)}]")
        if not args.dry_run:
            api("PATCH", f"/api/podcasts/{item['id']}/episode/{ep['id']}", payload)
        updated += 1
    return updated, skipped, unmatched


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--override", action="store_true")
    ap.add_argument("--feed", metavar="URL")
    ap.add_argument("--no-enclosure", action="store_true")
    ap.add_argument("--import", dest="import_file", metavar="FILE")
    ap.add_argument("podcasts", nargs="*", metavar="PODCAST")
    args = ap.parse_args()
    if (args.feed or args.import_file) and len(args.podcasts) != 1:
        ap.error("--feed / --import apply to exactly one PODCAST")

    items = {it["media"]["metadata"]["title"]: it for it in podcasts_in_library()}
    wanted = args.podcasts or [t for t in items if t in PATTERNS]
    rc = 0
    for title in wanted:
        item = items.get(title)
        if not item:
            log(f"ERROR: no podcast titled {title!r} in ABS; have: {sorted(items)}")
            rc = 1
            continue
        if title not in PATTERNS:
            log(f"ERROR: no number pattern for {title!r}; add one to PATTERNS")
            rc = 1
            continue
        file_re, title_re = PATTERNS[title]
        if args.import_file:
            recs, source = import_records(args.import_file, title_re), args.import_file
        else:
            source = args.feed or item["media"]["metadata"].get("feedUrl")
            if not source:
                log(f"ERROR: {title!r} has no feed URL in ABS and no --feed given")
                rc = 1
                continue
            recs = feed_records(source, title_re)
            if args.no_enclosure:
                for rec in recs.values():
                    rec["enclosure"] = None
        log(f"{title}: {len(item['media']['episodes'])} episodes, {len(recs)} numbered records from {source}")
        u, s, n = process(item, recs, file_re, title_re, args)
        log(f"  {'would update' if args.dry_run else 'updated'} {u}, already matched/unchanged {s}, no record {n}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
