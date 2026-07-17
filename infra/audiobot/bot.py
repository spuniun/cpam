#!/usr/bin/env python3
"""
cpam-audiobot — Discord slash command that issues Wizarr invitations for Audiobookshelf.

A member runs /audiobooks; the bot mints (or re-uses) a Wizarr invitation and replies
ephemerally with the join link. Wizarr owns account creation and the onboarding wizard;
this bot never touches Audiobookshelf and never handles a password.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("audiobot")
logging.getLogger("discord").setLevel(logging.WARNING)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        log.error("Required environment variable %s is not set", name)
        sys.exit(1)
    return value


DISCORD_TOKEN = require_env("DISCORD_TOKEN")
DISCORD_GUILD_ID = int(require_env("DISCORD_GUILD_ID"))
DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
WIZARR_URL = require_env("WIZARR_URL").rstrip("/")
WIZARR_API_KEY = require_env("WIZARR_API_KEY")
WIZARR_PUBLIC_URL = require_env("WIZARR_PUBLIC_URL").rstrip("/")
WIZARR_SERVER_IDS = [
    int(part) for part in require_env("WIZARR_SERVER_IDS").split(",") if part.strip()
]

STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/invites.json"))
EXPIRES_IN_DAYS = int(os.environ.get("EXPIRES_IN_DAYS", "7"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "15"))

# Wizarr only accepts 1, 7, 30, or null for expires_in_days.
if EXPIRES_IN_DAYS not in (1, 7, 30):
    log.error("EXPIRES_IN_DAYS must be one of 1, 7, 30 (got %s)", EXPIRES_IN_DAYS)
    sys.exit(1)


# ---------------------------------------------------------------------------
# State — maps discord_id to the invitation we issued them.
# Small enough that a JSON file under a lock is the right amount of machinery.
# ---------------------------------------------------------------------------


class InviteState:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] = {}

    def load(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            log.info("No state file at %s; starting empty", self._path)
            return
        try:
            content = self._path.read_text().strip()
            if not content:
                log.info("State file %s is empty; starting empty", self._path)
                return
            self._data = json.loads(content)
            log.info("Loaded %d invite record(s) from %s", len(self._data), self._path)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Could not read state file %s: %s", self._path, exc)
            sys.exit(1)

    def get(self, discord_id: int) -> Optional[dict[str, Any]]:
        return self._data.get(str(discord_id))

    async def put(self, discord_id: int, record: dict[str, Any]) -> None:
        async with self._lock:
            self._data[str(discord_id)] = record
            tmp = self._path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
                tmp.replace(self._path)
            except OSError as exc:
                log.error("Failed to persist state to %s: %s", self._path, exc)
                raise


state = InviteState(STATE_FILE)


# ---------------------------------------------------------------------------
# Wizarr API client
# ---------------------------------------------------------------------------


class WizarrError(Exception):
    pass


class Wizarr:
    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url
        self._headers = {
            "X-API-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        assert self._session is not None, "Wizarr client not started"
        url = f"{self._base}{path}"
        try:
            async with self._session.request(method, url, **kwargs) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise WizarrError(f"{method} {path} returned {resp.status}: {body[:300]}")
                return json.loads(body) if body else None
        except aiohttp.ClientError as exc:
            raise WizarrError(f"{method} {path} failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise WizarrError(f"{method} {path} returned non-JSON: {exc}") from exc

    async def list_invitations(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/invitations")
        return data.get("invitations", [])

    async def get_invitation(self, invitation_id: int) -> Optional[dict[str, Any]]:
        # No GET /invitations/{id} in the spec — only DELETE. Filter the list instead.
        for inv in await self.list_invitations():
            if inv.get("id") == invitation_id:
                return inv
        return None

    async def create_invitation(self) -> dict[str, Any]:
        payload = {
            "server_ids": WIZARR_SERVER_IDS,
            "expires_in_days": EXPIRES_IN_DAYS,
            "duration": "unlimited",
            "unlimited": True,
            "allow_downloads": True,
        }
        data = await self._request("POST", "/api/invitations", json=payload)
        invitation = data.get("invitation")
        if not invitation or not invitation.get("code"):
            raise WizarrError(f"Unexpected create response: {str(data)[:300]}")
        return invitation


wizarr = Wizarr(WIZARR_URL, WIZARR_API_KEY)


def invite_url(invitation: dict[str, Any]) -> str:
    """Wizarr returns a relative url ('/j/CODE'); make it absolute against the public host."""
    path = invitation.get("url") or f"/j/{invitation['code']}"
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{WIZARR_PUBLIC_URL}{path}"


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

GUILD = discord.Object(id=DISCORD_GUILD_ID)


class AudioBot(discord.Client):
    def __init__(self) -> None:
        # No privileged intents required: slash commands only.
        super().__init__(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await wizarr.start()
        self.tree.copy_global_to(guild=GUILD)
        synced = await self.tree.sync(guild=GUILD)
        log.info("Synced %d command(s) to guild %s", len(synced), DISCORD_GUILD_ID)

    async def close(self) -> None:
        await wizarr.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Connected as %s (id=%s)", self.user, self.user.id if self.user else "?")


client = AudioBot()


@client.tree.command(
    name="audiobooks",
    description="Get your personal invite link for the CPAM audiobook library",
)
async def audiobooks(interaction: discord.Interaction) -> None:
    user = interaction.user
    if DISCORD_CHANNEL_ID and interaction.channel_id != DISCORD_CHANNEL_ID:
        log.info(
            "/audiobooks rejected: %s used channel %s",
            interaction.user.id,
            interaction.channel_id,
        )
        await interaction.response.send_message(
            f"Run this in <#{DISCORD_CHANNEL_ID}>.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    log.info("/audiobooks invoked by %s (id=%s)", user, user.id)

    try:
        record = state.get(user.id)

        if record:
            existing = await wizarr.get_invitation(record["invitation_id"])

            if existing is None:
                log.info(
                    "Invite %s for %s no longer exists in Wizarr; issuing a new one",
                    record["invitation_id"],
                    user.id,
                )
            elif existing["status"] == "used":
                await interaction.followup.send(
                    "Looks like you've already set up your audiobook account — "
                    f"sign in at <{WIZARR_PUBLIC_URL}>.\n"
                    "If you can't get in, give Peter a shout.",
                    ephemeral=True,
                )
                return
            elif existing["status"] == "pending":
                log.info("Re-issuing pending invite %s to %s", existing["code"], user.id)
                await interaction.followup.send(
                    _invite_message(invite_url(existing), existing.get("expires")),
                    ephemeral=True,
                )
                return
            else:  # expired
                log.info("Invite %s for %s expired; issuing a new one", existing["code"], user.id)

        invitation = await wizarr.create_invitation()
        await state.put(
            user.id,
            {
                "invitation_id": invitation["id"],
                "code": invitation["code"],
                "discord_name": str(user),
                "issued_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        log.info("Issued invite %s to %s (id=%s)", invitation["code"], user, user.id)

        await interaction.followup.send(
            _invite_message(invite_url(invitation), invitation.get("expires")),
            ephemeral=True,
        )

    except WizarrError as exc:
        log.error("Wizarr call failed for %s: %s", user.id, exc)
        await interaction.followup.send(
            "Couldn't reach the invite service just now. Try again in a bit — "
            "if it keeps failing, let Peter know.",
            ephemeral=True,
        )
    except Exception:
        log.exception("Unhandled error servicing /audiobooks for %s", user.id)
        await interaction.followup.send(
            "Something went wrong on my end. Peter's been left a log entry to stare at.",
            ephemeral=True,
        )


def _invite_message(url: str, expires: Optional[str]) -> str:
    lines = [
        "Here's your personal invite to the CPAM® audiobook library:",
        "",
        url,
        "",
        "Open it, pick a username and password, and it'll walk you through the app setup.",
    ]
    if expires:
        try:
            when = datetime.fromisoformat(expires).replace(tzinfo=timezone.utc)
            lines.append(f"Link expires <t:{int(when.timestamp())}:R>.")
        except ValueError:
            pass
    lines.append("This link is yours alone — please don't forward it.")
    return "\n".join(lines)


def main() -> None:
    state.load()
    log.info(
        "Starting: wizarr=%s public=%s server_ids=%s expiry=%dd state=%s",
        WIZARR_URL,
        WIZARR_PUBLIC_URL,
        WIZARR_SERVER_IDS,
        EXPIRES_IN_DAYS,
        STATE_FILE,
    )
    try:
        client.run(DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected the bot token")
        sys.exit(1)


if __name__ == "__main__":
    main()
