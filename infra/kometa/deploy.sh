#!/usr/bin/env bash
# deploy.sh — copy the kometa configs from this repo into the live config dir
# (the compose mount), preserving the trakt.authorization block that Kometa
# maintains in the live config.yml. Run on the server after a git pull.
#
# Secrets never pass through here: config.yml carries <<name>> Config Secret
# markers that Kometa resolves from KOMETA_* env vars at runtime.
#
#   ./deploy.sh            copy configs into place
#   ./deploy.sh --dry-run  show what would change without touching the live dir

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${KOMETA_CONFIG_DIR:-/var/lib/plexmediaserver/.config/plex-meta-manager/config}"
FILES=(collections.yml premovies.yml 3dmovies.yml)
TRAKT_KEYS=(access_token token_type expires_in refresh_token scope created_at)

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

[[ -d "$DEST" ]] || { log "ERROR: $DEST does not exist"; exit 1; }

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
cp "$SRC/config.yml" "$tmp"

# Carry the trakt.authorization values forward from the live config; Kometa
# rewrites them on every token refresh and losing them forces a PIN re-auth.
# The keys are unique in config.yml and always indented 4 spaces.
if [[ -f "$DEST/config.yml" ]]; then
    for key in "${TRAKT_KEYS[@]}"; do
        value="$(sed -n "s/^    $key: *//p" "$DEST/config.yml" | head -n1)"
        [[ -n "$value" ]] && sed -i "s|^    $key:.*|    $key: $value|" "$tmp"
    done
else
    log "WARN: no existing $DEST/config.yml — trakt will need a PIN auth on first run"
fi

if (( DRY_RUN )); then
    for f in config.yml "${FILES[@]}"; do
        src_file="$tmp"; [[ "$f" != config.yml ]] && src_file="$SRC/$f"
        if [[ -f "$DEST/$f" ]] && cmp -s "$src_file" "$DEST/$f"; then
            log "unchanged: $f"
        else
            log "would update: $f"
        fi
    done
    exit 0
fi

install -m 644 "$tmp" "$DEST/config.yml"
for f in "${FILES[@]}"; do
    install -m 644 "$SRC/$f" "$DEST/$f"
done
log "deployed kometa config to $DEST (picked up on kometa's next scheduled run)"
