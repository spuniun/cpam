# CLAUDE.md

Config repo for **cpam.tv** — a personal Plex media server and its supporting stack.
This checkout (Windows/WSL) is a working copy; the live deployment lives on the Plex
server at `/home/plex/cpam` (compose files are invoked from that path by the mount
scripts). Nginx configs symlinked to `/etc/nginx/` on the server. Nothing in this
repo runs locally — changes take effect only after being pulled to the server.

## Layout

| Path | Purpose |
|---|---|
| `arrs/docker-compose.yml` | Media-management stack (`cpam-arrs`): sonarr, radarr, preradarr, lidarr, listenarr, audiobookshelf, maintainerr |
| `infra/docker-compose.yml` | Support stack (`cpam-infra`): sabnzbd, tautulli, wizarr, kometa, seerr, audiobot, doplarr, wrapperr, watchtower |
| `arrs/scripts/` | Custom scripts run by the arrs; mounted read-only at `/scripts` (`preradarr-cleanup.sh` — drop a movie from preradarr once Radarr imports it) |
| `infra/audiobot/` | Custom Discord bot (locally built image): `/audiobooks` mints Wizarr invites for the audiobook library |
| `infra/doplarr/config.toml` | Config for doplarr_rs, the Discord `/request` bot fronting Seerr |
| `infra/kometa/` | Kometa configs (fully committed — `config.yml` uses `<<name>>` Config Secret markers, secrets live in server `.env` as `KOMETA_*`), `deploy.sh` (copy to live config dir), `error_digest.py` (daily Pushover digest of run errors) |
| `infra/tautulli/monthly_stats.py` | Cron script: posts Tautulli's 30-day most-popular movies/TV to Discord as a compact ranked list (webhook) |
| `nginx/sites-available/cpam.tv` | All `*.cpam.tv` vhosts (one server block per app) |
| `nginx/conf-available/` | Shared includes: `common.include` (TLS/headers/AI-scraper guard), `ai-blocklist.conf`, `cloudflare.ips`, `theme-park.include`, `letsencrypt.include` |
| `nginx/update-cloudflare-ips.sh` | Weekly cron: refresh `cloudflare.ips` from upstream, validate, reload nginx |
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

- `.env` on the server is **not** committed (and is gitignored). It currently must
  provide: `PUID`, `PGID`, `TZ` (optional), `AUDIOBOT_DISCORD_TOKEN`, `CPAM_GUILD_ID`,
  `AUDIOBOT_CHANNEL_ID`, `WIZARR_API_KEY`, `DOPLARR_DISCORD_TOKEN`, `SEERR_API_KEY`,
  `TAUTULLI_API_KEY`, `STATS_WEBHOOK_URL` (Discord webhook — treat as a secret),
  `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY` (kometa error digest), and the kometa
  Config Secrets: `KOMETA_PLEXTOKEN`, `KOMETA_TMDBKEY`, `KOMETA_TAUTULLIKEY`,
  `KOMETA_OMDBKEY`, `KOMETA_MDBLISTKEY`, `KOMETA_RADARRKEY`, `KOMETA_PRERADARRKEY`,
  `KOMETA_SONARRKEY`. (No `KOMETA_TRAKT*` — Trakt runs in public mode, see below.)
  Don't hardcode UIDs or secrets into the compose files. Note there are **two**
  uncommitted env files, one per stack: the list above is `infra/.env`, while
  `arrs/.env` holds `PUID`/`PGID`/`TZ` plus `PRERADARR_API_KEY` and `PLEX_TOKEN`
  for the preradarr cleanup script below.
- **Host cron** (not compose): `infra/tautulli/monthly_stats.py` runs on the 1st of
  the month, sourcing the same `.env` (`set -a; . .env; set +a`). It fetches
  `get_home_stats` from Tautulli on localhost:8181 and posts one compact webhook
  message (two ranked-list embeds, no posters — a deliberate choice, twice over:
  poster-per-title made posts huge, and image URLs via Tautulli's proxy would leak
  the API key into embeds). Webhook calls need a non-default User-Agent or
  Cloudflare 403s them (error 1010).
- The infra stack also joins an **external** `cpam-shared` network (wizarr, audiobot);
  it must exist on the server before `up`. Wizarr's port is loopback-only
  (`127.0.0.1:5690`) — it's reached via nginx (`invite.cpam.tv`) and
  container-to-container.
- arrs stack pins subnet `172.28.0.0/16`. Host LAN IP is `172.20.20.250` — that's the
  address nginx proxies to and the compose port bindings serve on.
- **audiobookshelf**: config + metadata volumes MUST stay on plain local disk
  (SQLite over encfs/union mounts corrupts). Library dirs are mounted read-only —
  the arr apps own writes to media files. Its port is bound to loopback only
  (`127.0.0.1:13378`); it is reached exclusively via nginx (`listen.cpam.tv`).
- **kometa** runs as a daemon (`restart: unless-stopped`) and relies on its internal
  scheduler (daily at 05:00). A manual `docker exec kometa python kometa.py …`
  starts a *second* process against the same `config.cache` SQLite file — check the
  daemon is idle first (`docker stats --no-stream kometa`, and its stdout tail reads
  "N Hours until the next run"). Overlapping runs also split the morning across two
  log files, since `meta.log` only ever holds the most recent run. Restarting the
  container does not trigger a run; it waits for the schedule. Its config dir on the
  server is `/var/lib/plexmediaserver/.config/plex-meta-manager/config/` (legacy PMM
  name);
  the compose mount is that dir, not the repo — deploy config changes with
  `infra/kometa/deploy.sh` after a git pull. The committed `config.yml` contains no
  secrets: `<<name>>` markers are Kometa **Config Secrets**, resolved at runtime
  from the `KOMETA_*` env vars the compose file passes in from `.env` (secret names
  must not contain underscores). `deploy.sh` still carries `trakt.authorization`
  forward from the live copy, but it is empty and unused today. Never commit a
  config.yml with real tokens inlined.
- **kometa Trakt is deliberately in public mode.** Trakt deleted a swathe of
  existing API apps in late July 2026, ours included — the old client id now
  returns `401 invalid_client`, which broke even unauthenticated list reads, and
  creating a replacement needs a paid VIP account. `client_id`/`client_secret` are
  therefore blank, so Kometa falls back to its own public client id. That is
  sufficient: every Trakt builder here is a public `trakt_list`, which needs only
  an API key. Do **not** set `client_secret` — Kometa only attempts a token
  refresh when it is set, and that refresh is what produced the daily "Trakt
  authorization is invalid" error. Kometa 2.4.8 also removed the in-config `pin:`
  flow entirely; re-auth now means pasting a block from utilities.kometa.wiki.
- **Don't set `radarr.add_existing: true`** on the Movies library. It injects
  `add_existing` into every builder's `item_details`, which makes Kometa reload
  each item of every collection *and playlist*. Reloading a playlist item drops
  the `playlistItemID` that Plex only returns on the playlist endpoint; movies
  survive it (they're already in the per-library reload cache) but episodes do
  not, so every episode move becomes `PUT /playlists/<id>/items/None/move` → 404.
  That was 340 errors/run and left the `(Timeline Order)` playlists with movies
  ordered and episodes stuck where they were appended.
- **Known upstream failure: the TMDb episode cache wedges.** When TMDb renumbers
  a show's episodes, the stale row in `config.cache`'s `tmdb_episode_data2` keeps
  the old numbering while the rebuild claims the same `episode_id`, violating the
  unique index on `(episode_id, language)`. Kometa logs one CRITICAL and carries
  on, so it hides easily — but it aborts the whole library-operations phase, which
  silently stopped part-way through the TV library for days. Fix: stop the
  container, `DELETE FROM tmdb_episode_data2;` (pure cache, rebuilt on the next
  run), restart. Expect it to recur whenever TMDb renumbers again.
- **Host cron** (kometa): `infra/kometa/error_digest.py` runs daily after the 05:00
  kometa run, parses `logs/meta.log`, and sends one Pushover digest of the run's
  deduped errors — deliberately a single message per run (the per-error webhook
  would flood the phone), and it doubles as a heartbeat that kometa actually ran.
  CRITICALs are ranked first (they fire once, so frequency ranking buries them).
  Error classes that fire every run and that no config change can fix — TVDb-vs-
  TMDb episode numbering, MDBList gaps, resolution overlays finding no items — are
  listed in `BENIGN` and collapsed onto one `+N benign` tail line rather than
  hidden; `--show-benign` lists them individually. Only add a pattern to `BENIGN`
  after tracing it to its source. Needs `PUSHOVER_APP_TOKEN`/`PUSHOVER_USER_KEY`
  from `.env`; `--dry-run` prints instead of sending.
- **maintainerr** (`arrs` stack, :6246) applies retention rules and deletes through the
  Radarr/Sonarr/Plex/Tautulli **APIs**, never the filesystem — so it gets no
  `/home/plex/sorted` mount, and the optional leftover-folder cleanup (which would
  need one) stays off deliberately. Its data dir is
  `/var/lib/plexmediaserver/.config/Maintainerr` (SQLite → plain local disk). Unlike
  the lscr.io arrs it takes no `PUID`/`PGID` env; the image runs as
  `user: ${PUID}:${PGID}`. Radarr/Sonarr are reachable by container name in the same
  stack, but Plex (`172.20.20.250:32400`) and Seerr/Tautulli (infra stack) must be
  addressed by host IP + published port.
  It has **no login of its own** and `GET /api/settings/database/download` returns the
  whole database, plex token and arr keys included — so its port is loopback-only and
  `manage.cpam.tv` is the only route in. Auth is a **Cloudflare Access** policy on that
  hostname; the vhost additionally refuses any request lacking the
  `Cf-Access-Jwt-Assertion` header that Access adds, so it fails closed if the policy
  is ever removed (a bare 403 instead of a CF login page means Access is not in front).
  The vhost needs `proxy_buffering off` — the Logs and task-progress pages are SSE
  (`/api/logs/stream`, `/api/events/stream`) and look frozen without it. No websockets.
- **preradarr cleanup** (`arrs/scripts/preradarr-cleanup.sh`) is a Radarr **Custom
  Script** connection ("Preradarr Cleanup", on Download/Upgrade) that deletes the
  matching movie from preradarr once Radarr imports the real release, then refreshes
  the Plex **Pre** library (section 2) and empties its trash so the item actually
  leaves the view instead of lingering as unavailable. It exists because Maintainerr
  *cannot* do this: its rules evaluate the Plex **Movies** library, while the preradarr
  copy lives in the separate **Pre** library, so a Movies-side match never reaches the
  Pre item. Radarr's own import event is the only place that knows both.
  Matching is by `tmdbId`, falling back to `imdbId`. It deletes with
  `deleteFiles=true` and **no** import exclusion (`PRERADARR_EXCLUDE=true` opts in):
  preradarr has no import lists, so nothing re-adds a deleted title, and an exclusion
  is a global blocklist that would also block a future deliberate add.
  The script is mounted **read-only straight from the repo**
  (`/home/plex/cpam/arrs/scripts:/scripts:ro`), so a `git pull` is the whole deploy —
  no copy step like kometa's `deploy.sh`. Credentials come from the container env
  (`PRERADARR_API_KEY`, `PLEX_TOKEN` in `arrs/.env`), which Radarr passes through to
  custom scripts; changing them needs `docker compose up -d radarr`, not merely a
  restart of Radarr. It logs to `/config/preradarr-cleanup.log` (host:
  `/var/lib/plexmediaserver/.config/Radarr/`), self-truncating at 1 MB — deliberately
  *not* `/config/logs/`, which Radarr's own log-cleanup task prunes.
  Two Plex API details worth keeping: `/library/sections/N/refresh` answers **GET**
  but `/library/sections/N/emptyTrash` answers **PUT** only (GET and POST both 404,
  which reads like a wrong URL rather than a wrong verb); and Plex does **not**
  guarantee attribute order in the section list — `refreshing=` currently precedes
  `key=`, so the scan-completion poll must not assume an order, or it silently never
  matches and burns the full timeout on every single import.
  Test with `docker exec -u abc radarr /scripts/preradarr-cleanup.sh --test`;
  `--list` dumps preradarr's movies, and `--dry-run` with `radarr_*` env vars set
  resolves a match without deleting. Radarr's own Test button reaches the same check
  via `radarr_eventtype=Test`. (Unrelated: the older `Telegram` custom script
  connection is dead — every event is false and its `/usr/bin/python3.6` no longer
  exists in the image.)
- **Host cron** (nginx): `nginx/update-cloudflare-ips.sh` runs weekly (Mon 04:30) and
  refreshes `conf-available/cloudflare.ips` from `cloudflare.com/ips-v4`/`-v6`. Drift
  here fails *silently* — `set_real_ip_from` stops matching a new edge range and every
  log line and `X-Real-IP` from it shows a Cloudflare IP instead of the real client.
  It compares the ranges as a **set**, not byte-wise, so an upstream reordering is not
  treated as a change (that would mean a pointless reload and an alert listing nothing
  added or removed). It refuses to write unless both lists parse as CIDRs and clear a
  minimum count — a soft 301 or an error page that still returns 200 would otherwise
  blank the file and break real_ip on every vhost. On failure or a rejected
  `nginx -t` it rolls back and leaves the old file in place. It writes with `sudo tee`
  because the repo copy is root-owned, and it deliberately **does not git commit** —
  expect a dirty working tree after a real change. `--dry-run` previews, `--list`
  compares. Pushover keys are optional (missing = log only, never fatal).
- **watchtower** auto-updates all containers daily at 4am and prunes old images.
- **wrapperr** has a known TODO: its config volume mapping (`/opt/wrapperr:/app/config`)
  must exist before cutover (see inline `FIX` comment).
- Container configs live in a mix of `/opt/<app>` and
  `/var/lib/plexmediaserver/.config/<App>` on the server — match the existing pattern
  for the app family when adding services.

## Discord bots

Two bots, deliberately different in origin:

- **audiobot** (`infra/audiobot/`, our own Python/discord.py code, built via
  `build: ./audiobot`): `/audiobooks` mints or re-uses a Wizarr invitation and replies
  ephemerally. Wizarr owns accounts/onboarding; the bot never touches Audiobookshelf or
  passwords. Details that matter:
  - Channel guard: the command only works in `DISCORD_CHANNEL_ID`
    (`AUDIOBOT_CHANNEL_ID` in `.env`); unset/`0` **disables** the restriction entirely.
  - State: `/data/invites.json` (host: `/var/lib/plexmediaserver/.config/audiobot`)
    maps Discord user → issued invite. An empty/whitespace file is tolerated (starts
    fresh); corrupt JSON is a deliberate hard-fail, so don't "fix" that by starting
    empty. To reset for testing, `rm` the file or write `{}`.
  - Runs with `Intents.none()` — the "Guilds intent seems to be disabled" warning at
    startup is expected. Wizarr only accepts `EXPIRES_IN_DAYS` of 1, 7, or 30.
  - Deploys need `docker compose up -d --build audiobot` — it's a locally built image,
    so watchtower never updates it and a plain `up -d` keeps running stale code.
- **doplarr** (`ghcr.io/activexray/doplarr_rs`, third-party): `/request movie` and
  `/request series` slash commands that file requests through Seerr. Configured solely
  by `infra/doplarr/config.toml`, mounted read-only at `/config.toml`; secrets enter
  via `${VAR}` env substitution (an unset referenced var is a startup error, i.e.
  crash-loop until `.env` is complete). It talks to Seerr as `http://seerr:5055` on the
  compose default network. Requesters must have their Discord User ID linked in their
  Seerr profile, or `fallback_user_id` must be set.

Channel restriction convention: Discord-side per-command overrides (Server Settings →
Integrations → bot → Channels) control visibility/execution for both bots; audiobot
additionally enforces in code. doplarr has no in-config channel option — Discord-side
is the only layer, and that's fine.

## Nginx / edge

- Everything is fronted by **Cloudflare**: nginx listens only on `172.20.20.250:443`
  with a Cloudflare origin cert (`CF_cpam.tv.pem`), and `cloudflare.ips` restores real
  client IPs (`$http_cf_connecting_ip` is used as `X-Real-IP` in proxy blocks).
- Every server block starts with `include /etc/nginx/conf.d/common.include;`
  (TLS, security headers, error pages). Follow that pattern for new subdomains.
- Repo path is `conf-available/` but includes are referenced at
  `/etc/nginx/conf.d/` on the server — keep the include paths as written. The naming
  matters: `conf.d/*.conf` is auto-included at **http** level, while the `.include`
  files are pulled in explicitly by each server block. `ai-blocklist.conf` must keep
  its `.conf` extension (it defines an http-level `map`); the includes must not gain
  one, or they'd be loaded twice and fail.
- `common.include` ends every request through a **scraper guard**:
  `if ($ai_scraper) { return 403; }`, where `$ai_scraper` is a `map` over
  `$http_user_agent` in `ai-blocklist.conf` (AI crawlers, plus empty UAs and the
  default UAs of curl / python-requests / Go-http-client / axios / node-fetch).
  It applies to every vhost. When testing a vhost from the server, pass a browser
  UA (`curl -A 'Mozilla/5.0 ...'`) — otherwise you get a 403 from this guard and it
  looks like the block you just wrote is broken. The file also defines a
  `log_format ai_block` that nothing currently references.
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
| manage.cpam.tv | Maintainerr :6246 (loopback, fronted by Cloudflare Access) |
| pods.cpam.tv | dir2cast podcast feed (php7.4, basic auth) |

## Conventions

- Shell scripts are plain bash; `syncclouds.sh` is the style reference (strict mode,
  `log()` helper, `--dry-run`/`--list` flags). `autoclean.sh` is third-party
  (Louwrentius) — keep its structure when patching rather than rewriting.
- Secrets never go in this repo: no passphrases, tokens, `.env`, or encfs XML config.
  The encfs ciphertext directory names in `syncclouds.sh` are fine to commit.
- Commit messages are short and lower-case, matching existing history.
- Line endings are LF everywhere, enforced by `.gitattributes` (`* text=auto eol=lf`) —
  the server is Linux and CRLF breaks shell scripts. The Windows-side editor may
  CRLF the working tree; git normalizes on commit, so don't hand-convert.
