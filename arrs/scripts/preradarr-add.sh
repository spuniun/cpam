#!/bin/bash
#
# preradarr-add.sh - Seed preradarr when Radarr gains a not-yet-released movie
#
# Usage: invoked by Radarr as a Custom Script connection (Settings -> Connect),
#        with only "On Movie Added" ticked. Also runnable by hand:
#          docker exec -u abc radarr /scripts/preradarr-add.sh --test
#          docker exec -u abc radarr /scripts/preradarr-add.sh --sync --dry-run
#          docker exec -u abc radarr env radarr_eventtype=MovieAdded \
#              radarr_movie_id=1234 /scripts/preradarr-add.sh --dry-run
#
#   --dry-run   Decide and report, but add nothing
#   --test      Same as Radarr's "Test" button: check config + connectivity
#   --sync      Backfill: walk every Radarr movie, not just one event
#   --list      Print what --sync would consider, one line per movie, then exit
#
# Why this exists: preradarr chases early/low-quality releases (TELESYNC and the
# like) for titles that cannot be had properly yet. Keeping it stocked was a
# manual chore — notice a request land in Radarr, decide whether the digital
# release is out, add it to preradarr by hand. Radarr's MovieAdded event knows
# the first part and its API knows the second, so the whole decision fits here.
# The reverse direction (drop the pre copy once Radarr imports the real file)
# is preradarr-cleanup.sh; the two together make the pair self-maintaining.
#
# Flow, on Radarr's MovieAdded event:
#   1. re-read the movie from Radarr's API — the event environment carries
#      inCinemas and physicalRelease but *not* digitalRelease, which is the
#      field this whole decision turns on
#   2. skip it if Radarr already has a file, or if the movie is already out
#      (see classify() for the exact rule)
#   3. skip it if preradarr already holds that tmdbId
#   4. look the movie up on preradarr and POST it with preradarr's own root
#      folder / quality profile, monitored, and an immediate search
#
# Nothing here touches the filesystem: adds go through preradarr's API, and the
# only thing that lands on /home/plex/sorted/Pre is whatever preradarr later
# downloads on its own. So unlike the arrs themselves this is safe to run while
# the union mount is down — it just queues work.
#
# Environment (Radarr passes the container env through to custom scripts, so
# these come from arrs/docker-compose.yml, which reads arrs/.env):
#   PRERADARR_API_KEY     required; preradarr's API key
#   RADARR_API_KEY        optional; defaults to reading /config/config.xml,
#                         which inside the radarr container *is* Radarr's own
#                         key — no new secret to provision
#   PRERADARR_URL         default http://preradarr:7879 (same compose network)
#   RADARR_URL            default http://localhost:7878 (we run inside radarr)
#   PRERADARR_ROOT        default /movies (preradarr's only root folder)
#   PRERADARR_PROFILE     default "Any"; a profile name or a numeric id
#   PRERADARR_MIN_AVAIL   default announced — the point of preradarr is to grab
#                         something the moment anything exists
#   PRERADARR_SEARCH      default true; run a search on add
#   SYNC_DELAY            default 3; seconds between adds in --sync, so a
#                         backfill does not fire 30 indexer searches at once
#
# Exit: 0 on success or nothing-to-do, 1 on misconfiguration or a failed add.
#

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

PRERADARR_URL="${PRERADARR_URL:-http://preradarr:7879}"
RADARR_URL="${RADARR_URL:-http://localhost:7878}"
PRERADARR_ROOT="${PRERADARR_ROOT:-/movies}"
PRERADARR_PROFILE="${PRERADARR_PROFILE:-Any}"
PRERADARR_MIN_AVAIL="${PRERADARR_MIN_AVAIL:-announced}"
PRERADARR_SEARCH="${PRERADARR_SEARCH:-true}"
SYNC_DELAY="${SYNC_DELAY:-3}"

# /config is the Radarr config dir (/var/lib/plexmediaserver/.config/Radarr on
# the host). Deliberately not /config/logs — Radarr's own log-cleanup task
# prunes that directory.
LOG_FILE="${LOG_FILE:-/config/preradarr-add.log}"
LOG_MAX_BYTES=1048576

RADARR_CONFIG_XML="${RADARR_CONFIG_XML:-/config/config.xml}"

# Radarr fires MovieAdded as it commits the row; the API can lag it by a beat.
FETCH_RETRIES=3
FETCH_DELAY=2

# common.include's scraper guard 403s curl's default UA. These calls go direct
# to container ports rather than through nginx, but a UA costs nothing and
# saves the next person the same hour of debugging.
UA="cpam-preradarr-add/1.0"

DRY_RUN=false
MODE="event"

TODAY="$(date -u +%Y-%m-%d)"

# =============================================================================
# Helpers
# =============================================================================

log() {
    printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

die() {
    log "ERROR: $*"
    exit 1
}

# Truncate rather than rotate: this fires on every add, and the only readers
# are humans tailing it after something looked wrong.
rotate_log() {
    [[ -f "$LOG_FILE" ]] || return 0
    local size
    size=$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)
    if (( size > LOG_MAX_BYTES )); then
        tail -c $(( LOG_MAX_BYTES / 2 )) "$LOG_FILE" > "${LOG_FILE}.tmp" 2>/dev/null \
            && mv "${LOG_FILE}.tmp" "$LOG_FILE"
    fi
}

# api <base> <key> <method> <path> [body] -> response on stdout; non-zero
# unless the HTTP code is 2xx.
api() {
    local base="$1" key="$2" method="$3" path="$4" body="${5:-}" out code
    local -a args=(-sS -A "$UA" -X "$method" -H "X-Api-Key: ${key}"
                   -w $'\n%{http_code}' --max-time 60)
    if [[ -n "$body" ]]; then
        args+=(-H 'Content-Type: application/json' --data-binary "$body")
    fi
    out=$(curl "${args[@]}" "${base}${path}" 2>&1) || return 1
    code="${out##*$'\n'}"
    out="${out%$'\n'*}"
    if [[ ! "$code" =~ ^2 ]]; then
        log "${base}${path} ${method} -> HTTP ${code}: $(head -c 300 <<<"$out")"
        return 1
    fi
    printf '%s' "$out"
}

pre_api() { api "$PRERADARR_URL" "$PRERADARR_API_KEY" "$@"; }
rad_api() { api "$RADARR_URL"    "$RADARR_API_KEY"    "$@"; }

require_config() {
    [[ -n "${PRERADARR_API_KEY:-}" ]] \
        || die "PRERADARR_API_KEY is unset — add it to arrs/.env and recreate the radarr container"

    if [[ -z "${RADARR_API_KEY:-}" ]]; then
        [[ -r "$RADARR_CONFIG_XML" ]] \
            || die "RADARR_API_KEY is unset and ${RADARR_CONFIG_XML} is unreadable"
        RADARR_API_KEY=$(sed -n 's|.*<ApiKey>\([^<]*\)</ApiKey>.*|\1|p' "$RADARR_CONFIG_XML")
        [[ -n "$RADARR_API_KEY" ]] \
            || die "no <ApiKey> found in ${RADARR_CONFIG_XML}"
    fi
}

# Resolve PRERADARR_PROFILE (a name or an id) against preradarr, and confirm
# PRERADARR_ROOT is a root folder it actually knows. Both are cheap, and both
# fail loudly here rather than as an opaque 400 from the POST.
resolve_targets() {
    local profiles roots
    profiles=$(pre_api GET /api/v3/qualityprofile) \
        || die "could not read preradarr quality profiles"
    if [[ "$PRERADARR_PROFILE" =~ ^[0-9]+$ ]]; then
        QUALITY_PROFILE_ID=$(jq -r --argjson i "$PRERADARR_PROFILE" \
            'map(select(.id == $i)) | .[0].id // empty' <<<"$profiles")
    else
        QUALITY_PROFILE_ID=$(jq -r --arg n "${PRERADARR_PROFILE,,}" \
            'map(select((.name | ascii_downcase) == $n)) | .[0].id // empty' <<<"$profiles")
    fi
    [[ -n "$QUALITY_PROFILE_ID" ]] || die \
        "preradarr has no quality profile '${PRERADARR_PROFILE}' (have: $(jq -r '[.[].name]|join(", ")' <<<"$profiles"))"

    roots=$(pre_api GET /api/v3/rootfolder) || die "could not read preradarr root folders"
    jq -e --arg p "$PRERADARR_ROOT" 'any(.[]; .path == $p)' <<<"$roots" >/dev/null || die \
        "preradarr has no root folder '${PRERADARR_ROOT}' (have: $(jq -r '[.[].path]|join(", ")' <<<"$roots"))"
}

# =============================================================================
# The decision
# =============================================================================

# CLASSIFY_JQ defines a jq `classify` function returning "add|skip<TAB>reason"
# for a single movie object. It is applied one movie at a time on the event
# path and to the whole library in one pass by the sweep — 3768 separate jq
# processes is a slow way to answer the same question.
#
# "Not yet available digitally" is not one field. digitalRelease is null for
# most of the library (every pre-streaming-era film, and plenty of new ones
# until a date is announced), so it can only ever be a positive signal. Radarr's
# own `status` carries the rest: skyhook sets it to released once a digital or
# physical date has passed, inCinemas during the theatrical window, announced
# before that. Checked against the live library, no movie with status != released
# had a digital date in the past, so the two never disagree — the explicit date
# tests are here to stay correct if skyhook ever lags, not to fix a known gap.
CLASSIFY_JQ='
    def day($x): if ($x // "") == "" then null else $x[0:10] end;
    def classify:
        day(.digitalRelease)    as $dig
        | day(.physicalRelease) as $phy
        | day(.inCinemas)       as $cin
        | if .hasFile then
              "skip\tradarr already has a file"
          elif $dig != null and $dig <= $today then
              "skip\tdigital release \($dig) has passed"
          elif $phy != null and $phy <= $today then
              "skip\tphysical release \($phy) has passed"
          elif .status == "released" then
              "skip\tradarr status=released (dates: digital=\($dig // "-") physical=\($phy // "-") cinemas=\($cin // "-"))"
          else
              "add\tstatus=\(.status), digital=\($dig // "unknown"), cinemas=\($cin // "-"), physical=\($phy // "-")"
          end;
'

# classify <movie-json on stdin> -> "add|skip<TAB>reason"
classify() {
    jq -r --arg today "$TODAY" "${CLASSIFY_JQ} classify"
}

# classify_all <movie-array on stdin> -> "tmdbId<TAB>label<TAB>verdict<TAB>reason"
classify_all() {
    jq -r --arg today "$TODAY" "${CLASSIFY_JQ}"'
        .[] | "\(.tmdbId)\t\(.title) (\(.year))\t" + classify'
}

# =============================================================================
# Actions
# =============================================================================

# add_to_pre <tmdbId> <label> -> 0 added, 1 failed, 2 nothing to do
add_to_pre() {
    local tmdb="$1" label="$2" lookup payload

    if jq -e --argjson t "$tmdb" 'any(.[]; .tmdbId == $t)' <<<"$PRE_MOVIES" >/dev/null; then
        log "skip: ${label} — already in preradarr"
        return 2
    fi

    if [[ "$DRY_RUN" == true ]]; then
        log "DRY-RUN: would add ${label} to preradarr (tmdbId ${tmdb})"
        return 2
    fi

    lookup=$(pre_api GET "/api/v3/movie/lookup/tmdb?tmdbId=${tmdb}") || {
        log "ERROR: preradarr could not look up tmdbId ${tmdb} (${label})"
        return 1
    }

    # Post the lookup result back verbatim with only the add-time fields
    # overridden, so preradarr keeps whatever title/images/slug it resolved
    # rather than whatever Radarr happened to be holding.
    payload=$(jq -n --argjson m "$lookup" \
        --arg root "$PRERADARR_ROOT" \
        --argjson qp "$QUALITY_PROFILE_ID" \
        --arg minav "$PRERADARR_MIN_AVAIL" \
        --argjson search "$PRERADARR_SEARCH" \
        '$m + {
            rootFolderPath: $root,
            qualityProfileId: $qp,
            monitored: true,
            minimumAvailability: $minav,
            addOptions: { searchForMovie: $search }
         }')

    if pre_api POST /api/v3/movie "$payload" >/dev/null; then
        log "added ${label} to preradarr (tmdbId ${tmdb}, profile ${QUALITY_PROFILE_ID}, search=${PRERADARR_SEARCH})"
        # Keep the in-memory list current so a --sync run cannot add a title twice.
        PRE_MOVIES=$(jq --argjson t "$tmdb" '. + [{tmdbId: $t}]' <<<"$PRE_MOVIES")
        return 0
    fi

    log "ERROR: failed to add ${label} to preradarr (tmdbId ${tmdb})"
    return 1
}

run_test() {
    require_config
    local failed=0

    if rad_api GET /api/v3/system/status >/dev/null; then
        log "OK: radarr reachable at ${RADARR_URL}"
    else
        log "FAIL: radarr unreachable or API key rejected at ${RADARR_URL}"
        failed=1
    fi

    if pre_api GET /api/v3/system/status >/dev/null; then
        log "OK: preradarr reachable at ${PRERADARR_URL}"
        if resolve_targets; then
            log "OK: preradarr root '${PRERADARR_ROOT}', quality profile id ${QUALITY_PROFILE_ID} (${PRERADARR_PROFILE})"
        else
            failed=1
        fi
    else
        log "FAIL: preradarr unreachable or API key rejected at ${PRERADARR_URL}"
        failed=1
    fi

    return "$failed"
}

# Walk the whole Radarr library. --list prints the verdict for everything;
# --sync acts on it.
run_sweep() {
    require_config
    local movies verdict reason label tmdb rc failed=0 added=0 considered=0

    movies=$(rad_api GET /api/v3/movie) || die "could not list radarr movies"

    if [[ "$MODE" == "sync" ]]; then
        resolve_targets
        PRE_MOVIES=$(pre_api GET /api/v3/movie) || die "could not list preradarr movies"
    fi

    while IFS=$'\t' read -r tmdb label verdict reason; do
        [[ -n "$tmdb" ]] || continue
        if [[ "$MODE" == "list" ]]; then
            printf '%s\t%s\t%s\n' "$verdict" "$label" "$reason"
            continue
        fi
        [[ "$verdict" == "add" ]] || continue
        considered=$(( considered + 1 ))
        log "candidate: ${label} — ${reason}"
        rc=0; add_to_pre "$tmdb" "$label" || rc=$?
        case "$rc" in
            0) added=$(( added + 1 ))
               # Space the adds out: each one kicks off an indexer search.
               sleep "$SYNC_DELAY" ;;
            2) ;;
            *) failed=1 ;;
        esac
    done < <(classify_all <<<"$movies")

    if [[ "$MODE" == "sync" ]]; then
        log "sync done: ${considered} candidate(s), ${added} added$([[ "$DRY_RUN" == true ]] && echo ' (dry run)')"
    fi
    return "$failed"
}

# =============================================================================
# Main
# =============================================================================

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --test)    MODE="test" ;;
        --sync)    MODE="sync" ;;
        --list)    MODE="list" ;;
        --help|-h) sed -n '2,60p' "$0"; exit 0 ;;
        *)         echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

rotate_log

case "$MODE" in
    test)       run_test;  exit $? ;;
    sync|list)  run_sweep; exit $? ;;
esac

EVENT="${radarr_eventtype:-}"

# Radarr's own Test button sets eventtype=Test rather than passing --test.
if [[ "$EVENT" == "Test" ]]; then
    run_test
    exit $?
fi

if [[ "$EVENT" != "MovieAdded" ]]; then
    exit 0
fi

require_config

MOVIE_ID="${radarr_movie_id:-}"
TMDB_ID="${radarr_movie_tmdbid:-}"
LABEL="${radarr_movie_title:-unknown} (${radarr_movie_year:-?})"

# The event environment has no digitalRelease, so the API read is not optional.
MOVIE=""
for (( try = 1; try <= FETCH_RETRIES; try++ )); do
    if [[ -n "$MOVIE_ID" ]]; then
        MOVIE=$(rad_api GET "/api/v3/movie/${MOVIE_ID}") && break || true
    elif [[ -n "$TMDB_ID" && "$TMDB_ID" != "0" ]]; then
        MOVIE=$(rad_api GET "/api/v3/movie?tmdbId=${TMDB_ID}" \
                | jq -c 'if type == "array" then .[0] else . end') && break || true
    else
        die "MovieAdded for '${LABEL}' carried neither a movie id nor a tmdbId"
    fi
    (( try < FETCH_RETRIES )) && sleep "$FETCH_DELAY"
done

[[ -n "$MOVIE" && "$MOVIE" != "null" ]] \
    || die "could not read '${LABEL}' back from radarr after ${FETCH_RETRIES} tries"

TMDB_ID=$(jq -r '.tmdbId // empty' <<<"$MOVIE")
[[ -n "$TMDB_ID" && "$TMDB_ID" != "0" ]] \
    || die "radarr has no tmdbId for '${LABEL}' — cannot match it in preradarr"

IFS=$'\t' read -r VERDICT REASON < <(classify <<<"$MOVIE")

if [[ "$VERDICT" != "add" ]]; then
    # By far the common case: most adds are catalogue titles that are long out.
    log "skip: ${LABEL} — ${REASON}"
    exit 0
fi

log "candidate: ${LABEL} — ${REASON} (added to radarr via ${radarr_movie_addmethod:-unknown})"

resolve_targets
PRE_MOVIES=$(pre_api GET /api/v3/movie) || die "could not list preradarr movies"

RC=0; add_to_pre "$TMDB_ID" "$LABEL" || RC=$?
[[ "$RC" == 2 ]] && RC=0
exit "$RC"
