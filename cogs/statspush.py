"""
cogs/statspush.py  — Musubi bot repo
──────────────────────────────────────────────────────────────────────────────
Pushes Discord-cache-only values to the KpnWorld website API.

PUSH TRIGGERS:
  1. Every 60s via push_loop (safety net / heartbeat)
  2. Immediately on on_member_join  (member count went up)
  3. Immediately on on_member_remove (member count went down)

WHY: Supabase has persistent data (registered_guilds, XP, Users table).
     Three things only exist in the bot's in-process state:
       1. guild_count   — how many servers Musubi is currently in
       2. user_count    — sum of member_count across all guilds (Discord's number)
       3. active_calls  — live session count from DataManager
       4. callboard     — top 7 entries with resolved guild names + icons

SETUP:
  Set on the Azure Container (musubi):
    WEBSITE_URL       = https://kww-api.azurewebsites.net
    MUSUBI_API_SECRET = <same secret set on kww-api App Service>
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import cast

import aiohttp
import discord
from discord.ext import commands, tasks

from botprotocol import MusubiBot

log = logging.getLogger("musubi.statspush")

PUSH_INTERVAL     = 60    # seconds between scheduled pushes
MEMBER_PUSH_DELAY = 2     # seconds to debounce rapid join/leave bursts
HTTP_TIMEOUT      = aiohttp.ClientTimeout(total=8)


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
        # Debounce handle — cancels a pending push if another event fires first
        self._pending_push: asyncio.Task | None = None
        self.push_loop.start()

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        self.push_loop.cancel()
        if self._pending_push and not self._pending_push.done():
            self._pending_push.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Payload builder ───────────────────────────────────────────────────

    async def _build_payload(self) -> dict:
        guild_count = len(self.bot.guilds)
        # member_count is Discord's cached value — always accurate without chunking
        user_count  = sum(g.member_count or 0 for g in self.bot.guilds)

        active_calls = 0
        try:
            active_calls = await self.bot.data.count_active_calls()
        except Exception as e:
            log.debug("count_active_calls failed: %s", e)

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

    # ── HTTP post ─────────────────────────────────────────────────────────

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

    # ── Push helpers ──────────────────────────────────────────────────────

    async def _push_now(self) -> None:
        """Build and send a push immediately."""
        if not _ready():
            return
        try:
            payload = await self._build_payload()
            ok = await self._post(payload)
            if ok:
                log.debug(
                    "statspush OK — guilds:%d users:%d calls:%d board:%d",
                    payload["guild_count"], payload["user_count"],
                    payload["active_calls"], len(payload["callboard"]),
                )
        except Exception as e:
            log.warning("statspush push error: %s", e)

    async def _debounced_push(self) -> None:
        """
        Wait MEMBER_PUSH_DELAY seconds then push.
        If another event fires before the wait ends, the previous task is
        cancelled and a fresh delay starts — prevents spamming the API
        when many members join/leave in quick succession (e.g. a raid).
        """
        await asyncio.sleep(MEMBER_PUSH_DELAY)
        await self._push_now()

    def _schedule_push(self) -> None:
        """Cancel any pending debounced push and schedule a new one."""
        if self._pending_push and not self._pending_push.done():
            self._pending_push.cancel()
        self._pending_push = asyncio.create_task(self._debounced_push())

    # ── Scheduled loop (60s heartbeat) ───────────────────────────────────

    @tasks.loop(seconds=PUSH_INTERVAL)
    async def push_loop(self) -> None:
        await self._push_now()

    @push_loop.before_loop
    async def before_push(self) -> None:
        await self.bot.wait_until_ready()

    # ── Event-driven pushes ───────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Push updated stats when any member joins any server Musubi is in."""
        self._schedule_push()

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Push updated stats when any member leaves any server Musubi is in."""
        self._schedule_push()


async def setup(bot: commands.Bot) -> None:
    if not _ready():
        log.warning(
            "StatsPush: WEBSITE_URL or MUSUBI_API_SECRET not set — cog loaded but inactive."
        )
    await bot.add_cog(StatsPush(bot))