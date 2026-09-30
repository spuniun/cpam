#!/usr/bin/env bash
# deploy.sh — copy the kometa configs from this repo into the live config dir
# (the compose mount). Run on the server after a git pull.
#
# Secrets never pass through here: config.yml carries <<name>> Config Secret
# markers that Kometa resolves from KOMETA_* env vars at runtime.
#
#   ./deploy.sh            copy configs into place
#   ./deploy.sh --dry-run  show what would change without touching the live dir

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${KOMETA_CONFIG_DIR:-/var/lib/plexmediaserver/.config/plex-meta-manager/config}"
FILES=(config.yml collections.yml premovies.yml 3dmovies.yml)

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

[[ -d "$DEST" ]] || { log "ERROR: $DEST does not exist"; exit 1; }

if (( DRY_RUN )); then
    for f in "${FILES[@]}"; do
        if [[ -f "$DEST/$f" ]] && cmp -s "$SRC/$f" "$DEST/$f"; then
            log "unchanged: $f"
        else
            log "would update: $f"
        fi
    done
    exit 0
fi

for f in "${FILES[@]}"; do
    install -m 644 "$SRC/$f" "$DEST/$f"
done
log "deployed kometa config to $DEST (picked up on kometa's next scheduled run)"
