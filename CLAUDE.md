# CLAUDE.md

Config repo for **cpam.tv** — a personal Plex media server and its supporting stack.
This checkout (Windows/WSL) is a working copy; the live deployment lives on the Plex
server at `/home/plex/cpam` (compose files are invoked from that path by the mount
scripts). Nginx configs symlinked to `/etc/nginx/` on the server. Nothing in this
repo runs locally — changes take effect only after being pulled to the server.

## Layout

| Path | Purpose |
|---|---|
| `arrs/docker-compose.yml` | Media-management stack (`cpam-arrs`): sonarr, radarr, preradarr, lidarr, listenarr, audiobookshelf |
| `infra/docker-compose.yml` | Support stack (`cpam-infra`): sabnzbd, tautulli, wizarr, kometa, seerr, wrapperr, watchtower |
| `nginx/sites-available/cpam.tv` | All `*.cpam.tv` vhosts (one server block per app) |
| `nginx/conf-available/` | Shared includes: `common.include` (TLS/headers), `cloudflare.ips`, `theme-park.include`, `letsencrypt.include` |
| `mnt_plex.sh` / `umnt_plex.sh` | Bring the storage + arrs + Plex up / down (see boot order below) |
| `syncclouds.sh` | rclone-copy local encrypted media → Google Drive (`gdrive:/cpam`) |
| `autoclean.sh` | Delete oldest local media files when disk usage exceeds a threshold |

## Storage architecture (the part that's easy to break)

Media lives in **encfs-encrypted** directories, merged with **unionfs**:

- `/home/plex/.local-sorted` — encrypted local media (ciphertext)
- `/home/plex/local-sorted` — decrypted local view (encfs mount, **RW**)
- `gdrive:/cpam` → rclone systemd service → `/home/plex/.gdrive-sorted` ciphertext → `/home/plex/gdrive-sorted` decrypted view (**RO**)
- `/home/plex/sorted` — unionfs copy-on-write merge (`local=RW : gdrive=RO`) — **this is what Plex and every container mounts**

`mnt_plex.sh` order matters: rclone service → encfs local → encfs gdrive → unionfs →
`docker compose up` (arrs) → 30s sleep → plexmediaserver. `umnt_plex.sh` is the exact
reverse. Containers started while `/home/plex/sorted` is unmounted will see an empty
dir and can wreak havoc (e.g. arrs mass-deleting "missing" media) — never reorder.

### Encrypted path mappings in `syncclouds.sh`

`TOP_LEVEL_SYNCS` and `TV_SYNCS` map plaintext labels to **encfs ciphertext directory
names** (the gibberish like `m,UWYtVSWaa0IRClxXkOjDDX` = Seinfeld). These are correct;
do not "fix" or normalize them. To add a new show, get its ciphertext name with:

```sh
ENCFS6_CONFIG=/home/plex/encfs6.xml encfsctl encode /home/plex/.local-sorted "TV/Show Name"
```

then add a `"Label|ciphertext"` entry. TV entries are relative to the TV dir
(`XHiIBA-,pmPm6r1We6DzwWh4`); top-level entries are relative to `LOCAL_BASE`.
`autoclean.sh` has its own separate plaintext `SEARCH_DIRS` list — the two lists are
maintained independently and are intentionally not identical (autoclean only lists
what's safe to evict locally, i.e. content already synced to gdrive).

## Docker stacks

- `.env` on the server provides `PUID`/`PGID` (and optionally `TZ`); it is **not**
  committed. Don't hardcode UIDs into the compose files.
- arrs stack pins subnet `172.28.0.0/16`. Host LAN IP is `172.20.20.250` — that's the
  address nginx proxies to and the compose port bindings serve on.
- **audiobookshelf**: config + metadata volumes MUST stay on plain local disk
  (SQLite over encfs/union mounts corrupts). Library dirs are mounted read-only —
  the arr apps own writes to media files. Its port is bound to loopback only
  (`127.0.0.1:13378`); it is reached exclusively via nginx (`listen.cpam.tv`).
- **kometa** has `restart: "no"` on purpose — it's run on demand/scheduled, not a daemon.
- **watchtower** auto-updates all containers daily at 4am and prunes old images.
- **wrapperr** has a known TODO: its config volume mapping (`/opt/wrapperr:/app/config`)
  must exist before cutover (see inline `FIX` comment).
- Container configs live in a mix of `/opt/<app>` and
  `/var/lib/plexmediaserver/.config/<App>` on the server — match the existing pattern
  for the app family when adding services.

## Nginx / edge

- Everything is fronted by **Cloudflare**: nginx listens only on `172.20.20.250:443`
  with a Cloudflare origin cert (`CF_cpam.tv.pem`), and `cloudflare.ips` restores real
  client IPs (`$http_cf_connecting_ip` is used as `X-Real-IP` in proxy blocks).
- Every server block starts with `include /etc/nginx/conf.d/common.include;`
  (TLS, security headers, error pages). Follow that pattern for new subdomains.
- Repo path is `conf-available/` but includes are referenced at
  `/etc/nginx/conf.d/` on the server — keep the include paths as written.
- `theme-park.include` applies theme-park.dev CSS via `sub_filter` using the `$app`
  variable; currently commented out in most blocks but the `set $app ...` lines are
  kept so it can be re-enabled.

### Subdomain map

| Host | Backend |
|---|---|
| plex.cpam.tv | redirect to app.plex.tv |
| dash.cpam.tv | Organizr (php-fpm) + `/plex/` proxy to :32400 |
| sonarr / radarr / pre / lidarr | :8989 / :7878 / :7879 / :8686 |
| audio.cpam.tv | Listenarr :4545 |
| listen.cpam.tv | Audiobookshelf :13378 (loopback) |
| requests.cpam.tv | Seerr :5055 |
| sabnzbd / tautulli / wrapped | :8080 / :8181 / :8282 |
| invite.cpam.tv | Wizarr :5690 |
| status.cpam.tv | Grafana :3000 (localhost) |
| pods.cpam.tv | dir2cast podcast feed (php7.4, basic auth) |

## Conventions

- Shell scripts are plain bash; `syncclouds.sh` is the style reference (strict mode,
  `log()` helper, `--dry-run`/`--list` flags). `autoclean.sh` is third-party
  (Louwrentius) — keep its structure when patching rather than rewriting.
- Secrets never go in this repo: no passphrases, tokens, `.env`, or encfs XML config.
  The encfs ciphertext directory names in `syncclouds.sh` are fine to commit.
- Commit messages are short and lower-case, matching existing history.
