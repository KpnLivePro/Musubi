"""
cogs/statspush.py  — Musubi bot repo
──────────────────────────────────────────────────────────────────────────────
Pushes Discord-cache-only values to the KpnWorld website API every 60s.

WHY: Supabase has persistent data (registered_guilds, XP, Users table).
     Three things only exist in the bot's in-process state:
       1. guild_count   — how many servers Musubi is currently in
       2. user_count    — sum of member.member_count across all guilds
       3. active_calls  — live session count from DataManager
       4. callboard     — top 7 entries with resolved guild names + icons
                          (Supabase has XP; only the bot can get Discord.Guild objects)

SETUP:
  1. Copy this file to cogs/statspush.py in the Musubi repo.
  2. Add "cogs.statspush" to INITIAL_EXTENSIONS in main.py.
  3. Set on the Azure Container (musubi):
       WEBSITE_URL       = https://kww-api.azurewebsites.net
       MUSUBI_API_SECRET = <same secret set on kww-api App Service>

Verified against:
  - datamanager.py:  bot.data.count_active_calls(), bot.data.get_leaderboard(limit=7)
  - main.py:         INITIAL_EXTENSIONS, bot.data attribute
  - Supabase schema: Guilds(guild_id, xp, xp_reset_at), Sessions(status, target_guild)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import cast

import aiohttp
from discord.ext import commands, tasks

from botprotocol import MusubiBot

log = logging.getLogger("musubi.statspush")

PUSH_INTERVAL = 60
HTTP_TIMEOUT  = aiohttp.ClientTimeout(total=8)


def _url() -> str:
    return os.environ.get("WEBSITE_URL", "").rstrip("/") + "/api/musubi/push"

def _secret() -> str:
    return os.environ.get("MUSUBI_API_SECRET", "") or os.environ.get("API_SECRET", "")

def _ready() -> bool:
    return bool(os.environ.get("WEBSITE_URL") and _secret())


class StatsPush(commands.Cog, name="StatsPush"):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot: MusubiBot = cast(MusubiBot, bot)
        self._session: aiohttp.ClientSession | None = None
        self.push_loop.start()

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        self.push_loop.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _build_payload(self) -> dict:
        # ── Discord cache values ──────────────────────────────────────────
        guild_count = len(self.bot.guilds)
        user_count  = sum(g.member_count or 0 for g in self.bot.guilds)

        # active_calls: DataManager.count_active_calls() queries Sessions table
        # for rows with status=active and target_guild not null
        active_calls = 0
        try:
            active_calls = await self.bot.data.count_active_calls()
        except Exception as e:
            log.debug("count_active_calls failed: %s", e)

        # callboard: DataManager.get_leaderboard() returns
        # [{guild_id, xp, xp_reset_at}, ...] — we resolve names + icons here
        callboard: list[dict] = []
        try:
            rows = await self.bot.data.get_leaderboard(limit=7)
            for i, row in enumerate(rows):
                guild = self.bot.get_guild(int(row["guild_id"]))
                callboard.append({
                    "rank":          i + 1,
                    "guild_id":      row["guild_id"],
                    "guild_name":    guild.name if guild else f"Server \u2026{row['guild_id'][-4:]}",
                    "xp":            row.get("xp") or 0,
                    "icon_url":      str(guild.icon.url) if guild and guild.icon else None,
                    "cycle_started": (row.get("xp_reset_at") or "")[:10],
                })
        except Exception as e:
            log.debug("callboard build failed: %s", e)

        return {
            "guild_count":  guild_count,
            "user_count":   user_count,
            "active_calls": active_calls,
            "callboard":    callboard,
        }

    async def _post(self, payload: dict) -> bool:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.post(
                _url(),
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Secret":  _secret(),
                },
                timeout=HTTP_TIMEOUT,
            ) as r:
                if r.status == 200:
                    return True
                log.warning("statspush HTTP %d — check MUSUBI_API_SECRET and WEBSITE_URL", r.status)
        except asyncio.TimeoutError:
            log.warning("statspush timeout")
        except Exception as e:
            log.warning("statspush error: %s", e)
        return False

    @tasks.loop(seconds=PUSH_INTERVAL)
    async def push_loop(self) -> None:
        if not _ready():
            return
        try:
            payload = await self._build_payload()
            ok      = await self._post(payload)
            if ok:
                log.debug(
                    "statspush OK — guilds:%d users:%d calls:%d board:%d",
                    payload["guild_count"], payload["user_count"],
                    payload["active_calls"], len(payload["callboard"]),
                )
        except Exception as e:
            log.warning("statspush loop error: %s", e)

    @push_loop.before_loop
    async def before_push(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    if not _ready():
        log.warning(
            "StatsPush: WEBSITE_URL or MUSUBI_API_SECRET not set — cog loaded but inactive."
        )
    await bot.add_cog(StatsPush(bot))