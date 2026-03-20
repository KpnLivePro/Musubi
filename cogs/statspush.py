"""
cogs/statspush.py  — Musubi bot repo
──────────────────────────────────────────────────────────────────────────────
Pushes Discord-cache-only values to the KpnWorld website API every 60s.

User count strategy:
  - Seeded on ready() by summing guild.member_count across all guilds.
  - Maintained in real time via on_member_join / on_member_remove listeners.
  - This avoids recalculating the full sum every 60s and stays accurate
    between pushes without any DB reads.
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
        self._user_count: int = 0
        self.push_loop.start()

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        self.push_loop.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    # ── User count listeners ──────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Seed the user count once the bot is ready and all guilds are loaded."""
        self._user_count = sum(g.member_count or 0 for g in self.bot.guilds)
        log.debug("statspush: user_count seeded at %d", self._user_count)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        self._user_count += 1

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        self._user_count = max(0, self._user_count - 1)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """When the bot joins a new server, add that server's member count."""
        self._user_count += guild.member_count or 0
        log.debug("statspush: guild joined (%d members), user_count now %d", guild.member_count or 0, self._user_count)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """When the bot leaves a server, subtract that server's member count."""
        self._user_count = max(0, self._user_count - (guild.member_count or 0))
        log.debug("statspush: guild removed (%d members), user_count now %d", guild.member_count or 0, self._user_count)

    # ── Payload builder ───────────────────────────────────────────────────────

    async def _build_payload(self) -> dict:
        guild_count  = len(self.bot.guilds)
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
            "user_count":   self._user_count,
            "active_calls": active_calls,
            "callboard":    callboard,
        }

    # ── HTTP push ─────────────────────────────────────────────────────────────

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