#!/bin/bash
#
# syncclouds.sh - Sync local encrypted media to Google Drive
#
# Usage: ./syncclouds.sh [--dry-run] [--verbose] [category]
#   --dry-run   Show what would be synced without actually syncing
#   --verbose   Show detailed rclone output
#   category    Optional: sync only a specific category (e.g., "Movies", "Seinfeld")
#

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

LOCAL_BASE="/home/plex/.local-sorted"
REMOTE_BASE="gdrive:/cpam"
TV_LOCAL="${LOCAL_BASE}/XHiIBA-,pmPm6r1We6DzwWh4"
TV_REMOTE="${REMOTE_BASE}/XHiIBA-,pmPm6r1We6DzwWh4"

# Rclone settings (easy to adjust in one place)
TRANSFERS=2
CHECKERS=16
RCLONE_OPTS=""

# Log file
LOG_FILE="/home/plex/syncclouds.log"

# =============================================================================
# Sync Mappings
# Format: "Label|LocalEncryptedPath|RemoteEncryptedPath"
#
# For TV shows, use relative paths from TV_LOCAL/TV_REMOTE
# For top-level items, use full paths from LOCAL_BASE/REMOTE_BASE
# =============================================================================

# Top-level directories (Movies, etc.)
TOP_LEVEL_SYNCS=(
    "Movies|qMLzDzD,YvkM3FNZAO3oQiAg"
    "3D Movies|Ag2UvQ353t8bmJoW1qBrJiSU"
    "Selfcare|L2To,vHuH,6QNeBb,ZwO0gKE"
    "Workout|kATSPu7gnoE-ZTT8ZEZluRi7"
)

# TV Shows (relative to TV_LOCAL/TV_REMOTE)
TV_SYNCS=(
    "1883|xNyIs4Wwls6xJ6tCKaVFFsWl"
    "1923|01hlIxaxurwmyuoknPaC0WsR"
    "Ali G Rezurection|2oGQBtvN8Pi7lMWwUoKZcWK2g2QqPpiZevHH0IMDuToNg1"
    "Andor|Lth0ilkbYPNS,nlfy8lCVVUv"
    "Band of Brothers|R,mKgAHwbyZh5TKjkdbE0Uuyjs,EHvIqr8AFqArphJ2Cu0"
    "Better Call Saul|9GPe3rsz3jaATF9WgUVQ0DpBP1rjFyXo-WAgdB6BboIVa,"
    "Blue Planet II|ajkNNPjBCyqTA0iwlSaylb2g"
    "Boardwalk Empire|7OOzwRsf4icgTOkL7LFtaWxGeZvtB,n1UKddjR-ChkT,h1"
    "Book of Boba Fett|--FMKb4vPf8Nm3k1sAsDDaihF5ocaq7SzhiYnBwS1X7po,"
    "Breaking Bad|1tbR-zDiBPMygRKbo8G4za0h"
    "Bullshit|383p09WvZ9oJ4DjcbT9jG0nBMqm7CS,z-ITWL6z-gurb41"
    "Business Doing Pleasure|L5U0Zr6TFJLijeCn4hlhN,4oOdGpEdLOuUpkReilftjzd,"
    "Carnivale|wwDUbKzItGe0JpjJT2QOu18g"
    "Chappelle's Show|k-fNDJ9mXbGarU23CyMOxTMl,qGicrBRiXwPLTL0zGZZc-"
    "Conquest of the Skies|7OfBJqVqgc145JQXSMRX6nE2onOPBLtX9seLcalTvd6ohPOPyCtJO8zf9TVNscn0Bs8"
    "Curb Your Enthusiasm|M49LDpj9xUEZxD-vpDtfkA9BtO3bNIgtd1Ht9OyJ00SC50"
    "Deadwood|k2YTifvfYPLzJGy703h,Hs7p"
    "Expanse|,D3Pp2jlZP7YvAYwHvMdS89K"
    "Fargo|lgW9idACPkulbmdy1sinRjwI"
    "Fireman Sam|,xrn,tywQdnqMV1Dh8a2bnc,"
    "Freaks and Geeks|LpfXVGobe,HP10iOyaN7Ijf6exnGG8iMIKtR,14epswwL0"
    "Friends|zy5up,ikxOFKRe7yQEcvUFYh"
    "Game of Thrones|A0oKg89-TMyURE5B2J4nJBHa"
    "Gangs of London|g9LYVIUOJJtmEzuStnnYNMt5"
    "Gilmore Girls|iihcV9FKuV3Syu0jPriq8V-i"
    "Gilmore Girls A Year in the Life|6uTO7RkLPWmqXhVeWCIoEGwncTRhDWXXB,DO,57lk596f2lkHiVapfQYFGM,gyQKhT3"
    "Golden Girls|hyatqGAGV2D2-QUFFDHCzByPg,rdIKFlTTZR-xuSp2ugA,"
    "Handmaid's Tale|R2nMjnCTqid8ZzsxiH,qKpLprAE1wWCd6b19WZ8hi1SP,1"
    "High Potential|CAnPzeRGlErERENGFMB2Mj,6"
    "Homeland|6RxuqwxSJHiVbNQh-ZTfPUCo"
    "House of the Dragon|X,T5ShY-HhmKHSVilJC0iSjMknpZneyEppyukuTtCKHWW,"
    "Law and Order|Uyq69BS0kx5w3ngQAvnU3inY"
    "LEGO Star Wars|ngQLHhKfY-6,eSydk2Xi9ZOm"
    "Leftovers|fyktQIzJG7,q2rXQXNF92Iwa"
    "Lone Gunmen|nhZeKLwyUfUIhUMc8FQmnIem"
    "Looney Tunes|ElB0e3rRpL6EWBhxfxZNn6St"
    "Mandalorian|HbU52jlHh0lnfzj7JXoUgrCC"
    "Masters of the Air|CZgBGIbIhTZmaU,ZQE,8UTtAqTN2lc23BYCQE95S-BwAp-"
    "Mayor of Kingstown|cLwgdXaOLdWUbAcPkQ70OKkfjkM-jf8yfovHUyxGrFQBg-"
    "Millennium|PrbWa,qNowr-BIqmHSSZpmMA"
    "Mister Rogers Neighborhood|NWnt2D5aNz-GD18UydJ9FRrGnnbDjW4mk4SqseRG6ncJZ,"
    "Our Planet|Jc6-Pr5yWvCc1NbYBGH-vp2T"
    "Penguin|XwH9naYh0rsj84DgFlhXkz-5"
    "Phineas and Ferb|12ImTq5s0rPYCzFY16swd-ao1IB-ygvy13F9aTdxi2ecq,"
    "Planet Earth II|NECeQzqSKnOihFtF9S0s4D3Z"
    "Planet Earth III|TOnBs8i1NwQE6,aV8gNz9l95ngWR9MaQhZQPAespPjy841"
    "Pluribus|iIS0wLgGA6UMAQ,nq,3EWGWj"
    "Rick and Morty|OTsEXcBwUPT-bGtm1I1I9jt3"
    "Schitt's Creek|WB2Ixh1Vsl,-dX57yIXA9q0c"
    "Seinfeld|m,UWYtVSWaa0IRClxXkOjDDX"
    "Severance|FAJ6MDMXKKGk1CNGE0VZ6qH4"
    "Sopranos|,re,jyB6sCw0BE5U93LBm3CU"
    "South Park|8gY7XmIU2lFjO89pt3z8GunQ"
    "Station Eleven|Paos0YcE5,uSvTln-Otx6dHB"
    "Hunt|a7qpIGt1uAmcEFvdPuWFpyLG"
    "True Detective|-R-Ptcn,SBdRNo38nNfhxMYi"
    "Twin Peaks|I4zZBX7QM38MdasQ2rVvPMEf"
    "Vietnam War|9H5DSOggN0uqyUDN3Wsiuf3dNT-BfG6KUKVAqjlCl5Pyn1"
    "Vikings|OnmQ4ISUBL3iEeR5EelEdgd1"
    "Americans|,vlZMqDABsRQfw9-2933-5mtQwmersE0kp5kY9wkOUmgF0"
    "Westworld|OFfT6DzGrEX-GXRDK5iAnp7c"
    "Who Is America|gimA8-lkwcP79fzFViOvOs1l"
    "X-Files|YjyXonJI02JOawZdIXpAoYFK"
)

# =============================================================================
# Functions
# =============================================================================

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

sync_directory() {
    local label="$1"
    local source="$2"
    local dest="$3"
    
    log "Syncing: $label"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "  [DRY-RUN] rclone copy $source $dest"
        return 0
    fi
    
    local cmd="/usr/bin/rclone --transfers=$TRANSFERS --checkers=$CHECKERS $RCLONE_OPTS copy \"$source\" \"$dest\""
    
    if [[ "$VERBOSE" == "true" ]]; then
        eval "$cmd" -v
    else
        eval "$cmd"
    fi
    
    local status=$?
    if [[ $status -ne 0 ]]; then
        log "  ERROR: Failed to sync $label (exit code: $status)"
        return $status
    fi
    
    log "  Completed: $label"
    return 0
}

show_usage() {
    echo "Usage: $0 [--dry-run] [--verbose] [--list] [category]"
    echo
    echo "Options:"
    echo "  --dry-run   Show what would be synced without actually syncing"
    echo "  --verbose   Show detailed rclone output"
    echo "  --list      List all available categories"
    echo "  category    Sync only a specific category (partial match supported)"
    echo
    echo "Examples:"
    echo "  $0                    # Sync everything"
    echo "  $0 --dry-run          # Preview all syncs"
    echo "  $0 Movies             # Sync only Movies"
    echo "  $0 --verbose Seinfeld # Sync Seinfeld with verbose output"
}

list_categories() {
    echo "Top-level categories:"
    for entry in "${TOP_LEVEL_SYNCS[@]}"; do
        echo "  - ${entry%%|*}"
    done
    echo
    echo "TV Shows:"
    for entry in "${TV_SYNCS[@]}"; do
        echo "  - ${entry%%|*}"
    done
}

# =============================================================================
# Main
# =============================================================================

DRY_RUN="false"
VERBOSE="false"
FILTER=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN="true"
            shift
            ;;
        --verbose|-v)
            VERBOSE="true"
            shift
            ;;
        --list|-l)
            list_categories
            exit 0
            ;;
        --help|-h)
            show_usage
            exit 0
            ;;
        *)
            FILTER="$1"
            shift
            ;;
    esac
done

log "=========================================="
log "Starting cloud sync"
[[ "$DRY_RUN" == "true" ]] && log "DRY RUN MODE - no changes will be made"
[[ -n "$FILTER" ]] && log "Filtering for: $FILTER"
log "=========================================="

SYNC_COUNT=0
ERROR_COUNT=0

# Sync top-level directories
for entry in "${TOP_LEVEL_SYNCS[@]}"; do
    label="${entry%%|*}"
    enc_path="${entry#*|}"
    
    # Apply filter if specified
    if [[ -n "$FILTER" ]] && [[ ! "$label" =~ $FILTER ]]; then
        continue
    fi
    
    if sync_directory "$label" "${LOCAL_BASE}/${enc_path}/" "${REMOTE_BASE}/${enc_path}/"; then
        SYNC_COUNT=$((SYNC_COUNT + 1))
    else
        ERROR_COUNT=$((ERROR_COUNT + 1))
    fi
done

# Sync TV shows
for entry in "${TV_SYNCS[@]}"; do
    label="${entry%%|*}"
    enc_path="${entry#*|}"
    
    # Apply filter if specified
    if [[ -n "$FILTER" ]] && [[ ! "$label" =~ $FILTER ]]; then
        continue
    fi
    
    if sync_directory "TV/$label" "${TV_LOCAL}/${enc_path}/" "${TV_REMOTE}/${enc_path}/"; then
        SYNC_COUNT=$((SYNC_COUNT + 1))
    else
        ERROR_COUNT=$((ERROR_COUNT + 1))
    fi
done

log "=========================================="
log "Sync complete: $SYNC_COUNT succeeded, $ERROR_COUNT failed"
log "=========================================="

exit $ERROR_COUNT
