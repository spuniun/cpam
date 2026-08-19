#!/bin/bash
#
# update-cloudflare-ips.sh - Refresh nginx's Cloudflare real-IP ranges
#
# Usage: ./update-cloudflare-ips.sh [--dry-run] [--list] [--help]
#   --dry-run   Show what would change without writing or reloading
#   --list      Print the current and upstream ranges, then exit
#
# Fetches Cloudflare's published edge ranges and rewrites
# nginx/conf-available/cloudflare.ips (symlinked to /etc/nginx/conf.d/) so
# set_real_ip_from keeps matching every edge IP. When it drifts, nginx stops
# resolving $http_cf_connecting_ip for traffic on the new ranges and silently
# logs the Cloudflare edge as the client — nothing breaks visibly, which is
# why this runs on a schedule rather than being noticed.
#
# Writes the repo file in place (root-owned, so via sudo tee, which keeps the
# existing owner and mode) and reloads nginx. It deliberately does NOT git
# commit: the repo is the deployment, and an unattended push to master is a
# surprise waiting to happen. Expect a dirty working tree after a real change.
#
# Cron (weekly, sourcing infra/.env for the Pushover keys):
#   30 4 * * 1 cd /home/plex/cpam/infra && bash -c 'set -a; . ./.env; set +a; ../nginx/update-cloudflare-ips.sh' >/dev/null 2>&1
#
# Environment (optional):
#   PUSHOVER_APP_TOKEN / PUSHOVER_USER_KEY   notify on change or failure;
#                                            absent = log only, never fatal
#
# Exit: 0 on no-change or successful update, 1 on any failure.

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${SCRIPT_DIR}/conf-available/cloudflare.ips"

V4_URL="https://www.cloudflare.com/ips-v4"
V6_URL="https://www.cloudflare.com/ips-v6"

# nginx directive appended after the set_real_ip_from block
REAL_IP_HEADER="real_ip_header CF-Connecting-IP;"

# Sanity floors. A truncated fetch or an error page that still returns 200
# would otherwise blank the file and break real_ip on every vhost.
MIN_V4=10
MIN_V6=4

LOG_FILE="/home/plex/cloudflare-ips.log"

# =============================================================================
# Helpers
# =============================================================================

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

usage() {
    echo "Usage: $0 [--dry-run] [--list] [--help]"
    echo
    echo "Options:"
    echo "  --dry-run   Show what would change without writing or reloading"
    echo "  --list      Print current and upstream ranges, then exit"
    echo "  --help      Show this help"
    echo
    echo "Examples:"
    echo "  $0 --dry-run          # Preview the diff"
    echo "  $0                    # Update and reload nginx if anything changed"
}

# Pushover is optional: a missing key must not stop the update itself.
notify() {
    local title="$1" message="$2"

    if [[ -z "${PUSHOVER_APP_TOKEN:-}" || -z "${PUSHOVER_USER_KEY:-}" ]]; then
        log "pushover not configured (PUSHOVER_APP_TOKEN/PUSHOVER_USER_KEY unset) - not sending: ${title}"
        return 0
    fi

    if curl -fsS --max-time 15 \
        --form-string "token=${PUSHOVER_APP_TOKEN}" \
        --form-string "user=${PUSHOVER_USER_KEY}" \
        --form-string "title=${title}" \
        --form-string "message=${message}" \
        https://api.pushover.net/1/messages.json >/dev/null; then
        log "pushover sent: ${title}"
    else
        log "WARNING: pushover send failed"
    fi
}

# Fetch one list and reject anything that isn't a plain list of CIDRs.
# $1 url, $2 output file, $3 label, $4 line regex, $5 minimum count
fetch_ranges() {
    local url="$1" out="$2" label="$3" regex="$4" min="$5" count

    if ! curl -fsS --max-time 20 --retry 2 --retry-delay 3 "$url" -o "$out"; then
        log "ERROR: failed to fetch ${label} from ${url}"
        return 1
    fi

    # Upstream sends no trailing newline; normalise so the last range survives.
    sed -i -e '$a\' "$out"
    sed -i -e 's/\r$//' -e '/^[[:space:]]*$/d' "$out"

    if grep -qvE "$regex" "$out"; then
        log "ERROR: ${label} contains lines that are not CIDRs - refusing to use it:"
        grep -vE "$regex" "$out" | head -5 | while read -r bad; do log "  bad line: ${bad}"; done
        return 1
    fi

    count=$(wc -l < "$out")
    if (( count < min )); then
        log "ERROR: ${label} returned only ${count} ranges (expected at least ${min}) - refusing to use it"
        return 1
    fi

    log "fetched ${count} ${label} ranges"
    return 0
}

# =============================================================================
# Argument parsing
# =============================================================================

DRY_RUN="false"
LIST_ONLY="false"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN="true"
            shift
            ;;
        --list)
            LIST_ONLY="true"
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
done

# =============================================================================
# Main
# =============================================================================

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

V4_FILE="${TMP_DIR}/v4"
V6_FILE="${TMP_DIR}/v6"
NEW_FILE="${TMP_DIR}/cloudflare.ips"

[[ -f "$TARGET" ]] || { log "ERROR: target ${TARGET} does not exist"; exit 1; }

V4_RE='^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$'
V6_RE='^[0-9a-fA-F:]+/[0-9]{1,3}$'

if ! fetch_ranges "$V4_URL" "$V4_FILE" "IPv4" "$V4_RE" "$MIN_V4" \
   || ! fetch_ranges "$V6_URL" "$V6_FILE" "IPv6" "$V6_RE" "$MIN_V6"; then
    notify "nginx: Cloudflare IP update FAILED" \
           "Could not fetch or validate the range lists. ${TARGET} left untouched. See ${LOG_FILE}."
    exit 1
fi

# Build the replacement file: v4 then v6, upstream order, header line last.
{
    while read -r cidr; do echo "set_real_ip_from ${cidr};"; done < "$V4_FILE"
    while read -r cidr; do echo "set_real_ip_from ${cidr};"; done < "$V6_FILE"
    echo "$REAL_IP_HEADER"
} > "$NEW_FILE"

if [[ "$LIST_ONLY" == "true" ]]; then
    echo "--- current (${TARGET}) ---"
    cat "$TARGET"
    echo "--- upstream ---"
    cat "$NEW_FILE"
    echo "---"
    if diff -q "$TARGET" "$NEW_FILE" >/dev/null; then
        echo "in sync"
    else
        echo "DRIFTED - run without --list to update"
    fi
    exit 0
fi

# Compare the ranges as a SET, not byte-for-byte. Cloudflare has reordered its
# published lists before; reacting to that would mean a pointless reload and a
# "ranges updated" alert listing nothing added and nothing removed.
grep -oE '[0-9a-fA-F:.]+/[0-9]+' "$TARGET"   | sort -u > "${TMP_DIR}/have"
grep -oE '[0-9a-fA-F:.]+/[0-9]+' "$NEW_FILE" | sort -u > "${TMP_DIR}/want"

ADDED="$(comm -13 "${TMP_DIR}/have" "${TMP_DIR}/want" | tr '\n' ' ')"
REMOVED="$(comm -23 "${TMP_DIR}/have" "${TMP_DIR}/want" | tr '\n' ' ')"

if [[ -z "$ADDED" && -z "$REMOVED" ]]; then
    log "no change ($(wc -l < "${TMP_DIR}/want") ranges) - nginx not reloaded"
    exit 0
fi

log "CHANGED - added: ${ADDED:-none} | removed: ${REMOVED:-none}"

if [[ "$DRY_RUN" == "true" ]]; then
    log "DRY RUN MODE - not writing ${TARGET}, not reloading nginx"
    diff -u "$TARGET" "$NEW_FILE" || true
    exit 0
fi

# Keep the old copy outside the repo so a failed nginx -t can be rolled back
# without leaving stray files in the working tree.
BACKUP="${TMP_DIR}/cloudflare.ips.previous"
cp "$TARGET" "$BACKUP"

# sudo tee rather than a redirect: the repo file is root-owned, and tee writes
# through the existing inode, preserving its owner and mode.
if ! sudo tee "$TARGET" < "$NEW_FILE" >/dev/null; then
    log "ERROR: failed to write ${TARGET}"
    notify "nginx: Cloudflare IP update FAILED" "Could not write ${TARGET}. See ${LOG_FILE}."
    exit 1
fi
log "wrote ${TARGET}"

if ! sudo nginx -t >/dev/null 2>&1; then
    log "ERROR: nginx -t failed with the new ranges - rolling back"
    sudo tee "$TARGET" < "$BACKUP" >/dev/null
    sudo nginx -t >/dev/null 2>&1 && log "rollback restored a valid config" \
        || log "CRITICAL: config still invalid after rollback - do not reload nginx"
    notify "nginx: Cloudflare IP update FAILED" \
           "nginx -t rejected the new ranges; rolled back. See ${LOG_FILE}."
    exit 1
fi

if ! sudo systemctl reload nginx; then
    log "ERROR: nginx reload failed"
    notify "nginx: Cloudflare IP reload FAILED" \
           "New ranges written and valid, but the reload failed. See ${LOG_FILE}."
    exit 1
fi

log "nginx reloaded"
notify "nginx: Cloudflare IPs updated" \
       "Added: ${ADDED:-none}
Removed: ${REMOVED:-none}
Repo working tree is now dirty - commit ${TARGET#"${SCRIPT_DIR%/nginx}/"} when convenient."
