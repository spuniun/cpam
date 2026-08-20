#!/bin/bash
#
# preradarr-cleanup.sh - Drop a movie from preradarr once Radarr imports it
#
# Usage: invoked by Radarr as a Custom Script connection (Settings -> Connect).
#        Also runnable by hand for testing:
#          docker exec radarr /scripts/preradarr-cleanup.sh --test
#          docker exec radarr env radarr_eventtype=Download \
#              radarr_movie_tmdbid=1234 /scripts/preradarr-cleanup.sh --dry-run
#
#   --dry-run   Resolve the match and report it, but delete nothing
#   --test      Same as Radarr's "Test" button: check config + connectivity
#   --list      Print every preradarr movie (id/tmdbId/title/hasFile), then exit
#
# Why this exists: preradarr holds early/low-quality releases (TELESYNC and the
# like) plus monitored upcoming titles. When the real release lands in Radarr
# the preradarr entry is dead weight, but Maintainerr cannot clear it — its
# rules see the Plex "Movies" library, and the preradarr copy lives in the
# separate "Pre" library, so a Movies-side match never reaches the Pre item.
# Radarr's own import event does know, so the cleanup hangs off that instead.
#
# Flow, on Radarr's Download (import) event:
#   1. match the imported movie in preradarr by tmdbId (imdbId as a fallback)
#   2. DELETE it from preradarr with deleteFiles=true
#   3. refresh the Plex "Pre" library, wait for the scan, empty its trash
#      so the item actually leaves the view rather than lingering as unavailable
#
# Both arrs write to the same unionfs mount (/home/plex/sorted), so the guard
# against acting on an unmounted union is the trigger itself: no import event
# fires unless Radarr just wrote a file through that mount. Blast radius is one
# movie per event — the one just imported — and its Pre copy is by definition
# the disposable one.
#
# Environment (Radarr passes the container env through to custom scripts, so
# these come from arrs/docker-compose.yml, which reads arrs/.env):
#   PRERADARR_API_KEY   required; preradarr's API key
#   PLEX_TOKEN          required for the Plex refresh; unset = delete only
#   PRERADARR_URL       default http://preradarr:7879 (same compose network)
#   PLEX_URL            default http://172.20.20.250:32400 (host LAN IP)
#   PLEX_PRE_SECTION    default 2 (the "Pre" library section id)
#   PRERADARR_EXCLUDE   default false; true also adds an import exclusion
#
# Exit: 0 on success or nothing-to-do, 1 on misconfiguration or a failed step.
#

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

PRERADARR_URL="${PRERADARR_URL:-http://preradarr:7879}"
PLEX_URL="${PLEX_URL:-http://172.20.20.250:32400}"
PLEX_PRE_SECTION="${PLEX_PRE_SECTION:-2}"
PRERADARR_EXCLUDE="${PRERADARR_EXCLUDE:-false}"

# /config is the Radarr config dir (/var/lib/plexmediaserver/.config/Radarr on
# the host). Deliberately not /config/logs — Radarr's own log-cleanup task
# prunes that directory.
LOG_FILE="${LOG_FILE:-/config/preradarr-cleanup.log}"
LOG_MAX_BYTES=1048576

# How long to wait for the Plex scan to finish before emptying the trash.
SCAN_TIMEOUT=90
SCAN_POLL=2

# common.include's scraper guard 403s curl's default UA. These calls go direct
# to container/host ports rather than through nginx, but a UA costs nothing and
# saves the next person the same hour of debugging.
UA="cpam-preradarr-cleanup/1.0"

DRY_RUN=false
MODE="event"

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

# Truncate rather than rotate: this fires on every import, and the only readers
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

# pre_api <method> <path> -> body on stdout; non-zero if the HTTP code is not 2xx
pre_api() {
    local method="$1" path="$2" body code
    body=$(curl -sS -A "$UA" -X "$method" \
        -H "X-Api-Key: ${PRERADARR_API_KEY}" \
        -w $'\n%{http_code}' \
        --max-time 30 \
        "${PRERADARR_URL}${path}" 2>&1) || return 1
    code="${body##*$'\n'}"
    body="${body%$'\n'*}"
    [[ "$code" =~ ^2 ]] || { log "preradarr ${method} ${path} -> HTTP ${code}"; return 1; }
    printf '%s' "$body"
}

# plex_api <method> <path-with-query> -> non-zero if the HTTP code is not 2xx
# Method matters: /refresh answers GET, /emptyTrash only answers PUT (GET and
# POST both 404, which reads like a wrong URL rather than a wrong verb).
plex_api() {
    local method="$1" path="$2" sep code
    [[ "$path" == *\?* ]] && sep='&' || sep='?'
    code=$(curl -sS -A "$UA" -o /dev/null -w '%{http_code}' -X "$method" \
        --max-time 30 \
        "${PLEX_URL}${path}${sep}X-Plex-Token=${PLEX_TOKEN}") || return 1
    [[ "$code" =~ ^2 ]] || { log "plex ${method} ${path} -> HTTP ${code}"; return 1; }
}

require_config() {
    [[ -n "${PRERADARR_API_KEY:-}" ]] \
        || die "PRERADARR_API_KEY is unset — add it to arrs/.env and recreate the radarr container"
}

# =============================================================================
# Actions
# =============================================================================

# Refresh the Plex "Pre" library and empty its trash, so a deleted movie stops
# showing as an unavailable item. autoEmptyTrash is on server-wide today, which
# would cover this on its own; the explicit call keeps the behaviour correct if
# that preference is ever turned off.
plex_refresh_pre() {
    if [[ -z "${PLEX_TOKEN:-}" ]]; then
        log "PLEX_TOKEN unset — skipping the Plex refresh (item stays visible until the next scan)"
        return 1
    fi

    log "refreshing Plex section ${PLEX_PRE_SECTION} (Pre)"
    plex_api GET "/library/sections/${PLEX_PRE_SECTION}/refresh" || return 1

    local waited=0 refreshing
    while (( waited < SCAN_TIMEOUT )); do
        sleep "$SCAN_POLL"
        waited=$(( waited + SCAN_POLL ))
        # Split on '<' so each element lands on its own line, pick the Pre
        # section's, then read its refreshing flag. Plex does not guarantee
        # attribute order — it currently emits refreshing= before key=, so
        # anything that assumes one order silently never matches and this loop
        # burns the full timeout on every import.
        refreshing=$(curl -sS -A "$UA" --max-time 30 \
            "${PLEX_URL}/library/sections?X-Plex-Token=${PLEX_TOKEN}" \
            | tr '<' '\n' \
            | grep -E "^Directory .*(^|[[:space:]])key=\"${PLEX_PRE_SECTION}\"" \
            | grep -o 'refreshing="[01]"' | head -1 || true)
        [[ "$refreshing" == 'refreshing="0"' ]] && break
    done

    if [[ "$refreshing" != 'refreshing="0"' ]]; then
        log "WARN: Plex scan still running after ${SCAN_TIMEOUT}s — leaving the trash to autoEmptyTrash"
        return 0
    fi

    log "scan finished in ${waited}s; emptying section ${PLEX_PRE_SECTION} trash"
    plex_api PUT "/library/sections/${PLEX_PRE_SECTION}/emptyTrash" || return 1
}

run_test() {
    require_config
    local failed=0

    if pre_api GET /api/v3/system/status >/dev/null; then
        log "OK: preradarr reachable at ${PRERADARR_URL}"
    else
        log "FAIL: preradarr unreachable or API key rejected at ${PRERADARR_URL}"
        failed=1
    fi

    if [[ -z "${PLEX_TOKEN:-}" ]]; then
        log "WARN: PLEX_TOKEN unset — deletes will work, the Pre library will not be refreshed"
    elif plex_api GET "/library/sections/${PLEX_PRE_SECTION}"; then
        log "OK: Plex section ${PLEX_PRE_SECTION} reachable at ${PLEX_URL}"
    else
        log "FAIL: Plex section ${PLEX_PRE_SECTION} unreachable at ${PLEX_URL}"
        failed=1
    fi

    return "$failed"
}

run_list() {
    require_config
    pre_api GET /api/v3/movie \
        | jq -r '.[] | "\(.id)\t\(.tmdbId)\t\(.title) (\(.year))\thasFile=\(.hasFile)"' \
        | sort -k3
}

# =============================================================================
# Main
# =============================================================================

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --test)    MODE="test" ;;
        --list)    MODE="list" ;;
        --help|-h) sed -n '2,40p' "$0"; exit 0 ;;
        *)         echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

rotate_log

case "$MODE" in
    test) run_test; exit $? ;;
    list) run_list; exit $? ;;
esac

EVENT="${radarr_eventtype:-}"

# Radarr's own Test button sets eventtype=Test rather than passing --test.
if [[ "$EVENT" == "Test" ]]; then
    run_test
    exit $?
fi

# Download covers both a first import and an upgrade. On an upgrade the
# preradarr entry is normally long gone, and the lookup below just no-ops.
if [[ "$EVENT" != "Download" ]]; then
    exit 0
fi

require_config

TMDB_ID="${radarr_movie_tmdbid:-}"
IMDB_ID="${radarr_movie_imdbid:-}"
TITLE="${radarr_movie_title:-unknown} (${radarr_movie_year:-?})"

[[ -n "$TMDB_ID" || -n "$IMDB_ID" ]] \
    || die "import of '${TITLE}' carried neither a tmdbId nor an imdbId"

MOVIES="$(pre_api GET /api/v3/movie)" || die "could not list preradarr movies"

MATCHES=""
if [[ -n "$TMDB_ID" && "$TMDB_ID" != "0" ]]; then
    MATCHES=$(jq -r --argjson t "$TMDB_ID" '.[] | select(.tmdbId == $t) | .id' <<<"$MOVIES")
fi
if [[ -z "$MATCHES" && -n "$IMDB_ID" ]]; then
    MATCHES=$(jq -r --arg i "$IMDB_ID" '.[] | select(.imdbId == $i) | .id' <<<"$MOVIES")
    [[ -n "$MATCHES" ]] && log "matched '${TITLE}' by imdbId ${IMDB_ID} (no tmdbId hit)"
fi

if [[ -z "$MATCHES" ]]; then
    # The common case by a wide margin: most imports were never in preradarr.
    exit 0
fi

DELETED=0
FAILED=0
while read -r id; do
    [[ -n "$id" ]] || continue
    detail=$(jq -r --argjson i "$id" \
        '.[] | select(.id == $i) | "\(.title) (\(.year)) hasFile=\(.hasFile) path=\(.path)"' \
        <<<"$MOVIES")

    if [[ "$DRY_RUN" == true ]]; then
        log "DRY-RUN: would delete preradarr movie ${id}: ${detail}"
        continue
    fi

    if pre_api DELETE "/api/v3/movie/${id}?deleteFiles=true&addImportExclusion=${PRERADARR_EXCLUDE}" >/dev/null; then
        log "deleted preradarr movie ${id}: ${detail} (imported to Radarr as '${TITLE}')"
        DELETED=$(( DELETED + 1 ))
    else
        log "ERROR: failed to delete preradarr movie ${id}: ${detail}"
        FAILED=1
    fi
done <<<"$MATCHES"

if (( DELETED > 0 )); then
    plex_refresh_pre || FAILED=1
fi

exit "$FAILED"
