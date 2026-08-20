#!/usr/bin/env python3
"""backdate-archive-added.py - point addedAt at the original air date for the Archive collection

Usage: backdate-archive-added.py [--apply] [--rollback FILE] [--section N] [--collection RK]
  (default is a dry run: prints the plan and writes nothing)
  --apply           perform the updates, writing a rollback CSV first
  --rollback FILE   restore addedAt from a rollback CSV and clear the locks

Sets addedAt on every show/season/episode in the Plex "Archive" collection to
that item's original air date, and LOCKS the field. The lock is the whole
point: a raw sqlite UPDATE against com.plexapp.plugins.library.db changes the
value but not the lock flag, so Plex treats it as its own to recompute and
reverts it on the next refresh. Editing through the API with addedAt.locked=1
is what survives.

Do NOT try to do this by rewriting file mtimes, which is the obvious-looking
alternative. Two reasons:

  1. It would not work. Plex derives addedAt from the time the item was first
     scanned, not the file's mtime. Spot-check: Looney Tunes files with mtime
     2016-01-18 carry addedAt 2017-02-18. The ones where the two agree are just
     files that were downloaded and scanned the same day.
  2. It would be destructive. ~96% of Archive media lives on the read-only
     gdrive branch of the unionfs mount (measured: 4196 of 4385 files, 3.9 TB,
     across only the first 20 of 84 shows). A utimes() there either fails or
     triggers unionfs copy-up, pulling terabytes back down from Google Drive
     onto local disk. And on the local branch it would make syncclouds.sh's
     `rclone copy` see every touched file as modified and re-upload it.

Air date fallback chain, so nothing is left behind as a recently-added outlier:
episode's own date -> its season's earliest episode date -> the show's earliest.
(176 episodes have no date of their own, 117 of them Game of Thrones extras.)

!! Before running this, confirm the Maintainerr Archive exclusion is intact --
   see the "Plex addedAt" section in CLAUDE.md. Backdating makes every Archive
   show satisfy rule group 1's `sw_lastEpisodeAddedAt BEFORE 76 days` test, so
   the `NOT_CONTAINS "Archive"` guard is the only thing preventing a 14-day
   deletion countdown on the whole archive.

Environment: PLEX_TOKEN (infra/.env exports it as KOMETA_PLEXTOKEN).
Exit: 0 on success or dry run, 1 on failure.
"""
import os, sys, csv, time, datetime, argparse, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

BASE = os.environ.get("PLEX_URL", "http://172.20.20.250:32400")
# common.include's scraper guard 403s curl/python default UAs. These calls go
# direct to the host port rather than through nginx, but the UA costs nothing.
UA = "cpam-backdate-archive/1.0"


def build(tok):
    def req(path, method="GET", **kw):
        kw["X-Plex-Token"] = tok
        r = urllib.request.Request(f"{BASE}{path}?{urllib.parse.urlencode(kw)}",
                                   headers={"User-Agent": UA}, method=method)
        return urllib.request.urlopen(r, timeout=180)
    return req


def log(msg):
    print(msg, flush=True)   # unbuffered: this runs for minutes, progress must show


def epoch(datestr):
    return int(datetime.datetime.strptime(datestr, "%Y-%m-%d").timestamp())


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", metavar="FILE")
    ap.add_argument("--section", default="4")
    ap.add_argument("--collection", default="30312")
    args = ap.parse_args()

    tok = os.environ.get("PLEX_TOKEN")
    if not tok:
        log("ERROR: PLEX_TOKEN is unset (infra/.env has it as KOMETA_PLEXTOKEN)"); return 1
    req = build(tok)
    get = lambda p, **kw: ET.fromstring(req(p, **kw).read())
    SEC = args.section

    if args.rollback:
        rows = list(csv.DictReader(open(args.rollback)))
        log(f"restoring {len(rows)} items from {args.rollback}")
        ok = bad = 0
        for r in rows:
            if not r["old_addedAt"]:
                continue
            try:
                req(f"/library/sections/{SEC}/all", "PUT", type=r["type"], id=r["ratingKey"],
                    **{"addedAt.value": r["old_addedAt"], "addedAt.locked": "0"})
                ok += 1
            except Exception as exc:
                bad += 1; log(f"  FAIL {r['ratingKey']}: {exc}")
            time.sleep(0.05)
        log(f"restored {ok}, failed {bad}")
        return 1 if bad else 0

    arch = {s.get("ratingKey"): s for s in get(f"/library/metadata/{args.collection}/children")}
    seasons = [s for s in get(f"/library/sections/{SEC}/all", type="3")
               if s.get("parentRatingKey") in arch]
    episodes = [e for e in get(f"/library/sections/{SEC}/all", type="4")
                if e.get("grandparentRatingKey") in arch]

    show_first, season_first = {}, {}
    for e in episodes:
        air = e.get("originallyAvailableAt")
        if not air:
            continue
        g, p = e.get("grandparentRatingKey"), e.get("parentRatingKey")
        if g and (g not in show_first or air < show_first[g]):
            show_first[g] = air
        if p and (p not in season_first or air < season_first[p]):
            season_first[p] = air

    plan = []
    for rk, s in arch.items():
        air = s.get("originallyAvailableAt") or show_first.get(rk)
        if air:
            plan.append(("2", rk, epoch(air), f"SHOW  {s.get('title')}", s.get("addedAt")))
    for s in seasons:
        rk, p = s.get("ratingKey"), s.get("parentRatingKey")
        air = (s.get("originallyAvailableAt") or season_first.get(rk) or show_first.get(p)
               or (arch[p].get("originallyAvailableAt") if p in arch else None))
        if air:
            plan.append(("3", rk, epoch(air), f"SEAS  {s.get('parentTitle')} S{s.get('index')}", s.get("addedAt")))
    for e in episodes:
        rk, p, g = e.get("ratingKey"), e.get("parentRatingKey"), e.get("grandparentRatingKey")
        air = (e.get("originallyAvailableAt") or season_first.get(p) or show_first.get(g)
               or (arch[g].get("originallyAvailableAt") if g in arch else None))
        if air:
            plan.append(("4", rk, epoch(air),
                         f"EP    {e.get('grandparentTitle')} S{e.get('parentIndex')}E{e.get('index')}",
                         e.get("addedAt")))

    counts = {t: sum(1 for p in plan if p[0] == t) for t in ("2", "3", "4")}
    log(f"plan: {len(plan)} items (shows={counts['2']} seasons={counts['3']} episodes={counts['4']})")
    unresolved = (len(arch) + len(seasons) + len(episodes)) - len(plan)
    log(f"unresolvable (no date anywhere in the chain): {unresolved}")
    for t, rk, ep, lbl, old in plan[:3]:
        o = datetime.datetime.fromtimestamp(int(old)).strftime("%Y-%m-%d") if old else "-"
        log(f"  {lbl[:52]:<52} {o} -> {datetime.datetime.fromtimestamp(ep):%Y-%m-%d}")

    if not args.apply:
        log("\nDRY RUN - nothing written. Re-run with --apply.")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rb = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      f"archive_addedat_rollback-{stamp}.csv")
    with open(rb, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["type", "ratingKey", "old_addedAt", "label"])
        for t, rk, ep, lbl, old in plan:
            w.writerow([t, rk, old or "", lbl])
    log(f"rollback written: {rb}")

    # Batch ids sharing a target date (Plex accepts id=a,b,c for one value).
    done = fail = 0
    for t in ("2", "3", "4"):
        groups = {}
        for _t, rk, ep, lbl, old in plan:
            if _t == t:
                groups.setdefault(ep, []).append(rk)
        for ep, rks in groups.items():
            for i in range(0, len(rks), 40):
                chunk = rks[i:i + 40]
                try:
                    req(f"/library/sections/{SEC}/all", "PUT", type=t, id=",".join(chunk),
                        **{"addedAt.value": str(ep), "addedAt.locked": "1"})
                    done += len(chunk)
                except Exception as exc:
                    fail += len(chunk); log(f"  FAIL type={t} ep={ep} n={len(chunk)}: {exc}")
                time.sleep(0.05)
        log(f"  type {t} complete: {done} written, {fail} failed")
    log(f"\ndone: {done} updated, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
