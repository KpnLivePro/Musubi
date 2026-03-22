"""
Project MUSUBI — cogs/discordlog.py
Runtime log shipping to a Discord channel via webhook.

Sends silent (@silent) messages so no one gets pinged.
Creates and caches its own webhook in the target channel so it
doesn't depend on any pre-existing webhook.

Events shipped:
  - Bot startup (on_ready)
  - Bot shutdown (close signal via atexit-style hook)
  - Runtime errors (ERROR and above from any musubi.* logger)
  - Website push success/failure (from statspush)
  - Session opens/closes (INFO from musubi.phone)
  - Guild joins/removes (INFO from musubi.main)

Log level routing:
  ERROR / CRITICAL  →  ❌  red codeblock
  WARNING           →  ⚠️  yellow
  INFO              →  🔵  plain
  (DEBUG is never shipped — too noisy)

Channel: 1475292266925916270
The cog creates a "Musubi Logs" webhook there on first load
and caches the URL in memory. No env var needed — channel ID
is hardcoded here since it's an internal ops channel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

from botprotocol import MusubiBot

LOG_CHANNEL_ID  = 1475292266925916270
WEBHOOK_NAME    = "Musubi Logs"
BRAND_COLOR     = 0xC084FC
MAX_QUEUE       = 500          # drop oldest if queue backs up beyond this
FLUSH_INTERVAL  = 2.0          # seconds between flushes (rate-limit friendly)

log = logging.getLogger("musubi.discordlog")


# ── Embed builders ────────────────────────────────────────────────────────────

def _level_icon(level: int) -> str:
    if level >= logging.CRITICAL:
        return "‼️"
    if level >= logging.ERROR:
        return "❌"
    if level >= logging.WARNING:
        return "⚠️"
    return "🔵"


def _level_color(level: int) -> int:
    if level >= logging.CRITICAL:
        return 0xFF0000
    if level >= logging.ERROR:
        return 0xFF4444
    if level >= logging.WARNING:
        return 0xFFAA00
    return BRAND_COLOR


def _record_to_embed(record: logging.LogRecord) -> discord.Embed:
    icon  = _level_icon(record.levelno)
    color = _level_color(record.levelno)
    ts    = datetime.fromtimestamp(record.created, tz=timezone.utc)
    ts_str = f"<t:{int(ts.timestamp())}:T>"

    description = f"> `{icon}` `[{record.name}]` {ts_str}\n"

    msg = record.getMessage()
    if record.exc_info:
        tb  = "".join(traceback.format_exception(*record.exc_info))
        msg = f"{msg}\n{tb}"

    # Truncate long messages so embed stays under Discord's 4096 char limit
    if len(msg) > 3800:
        msg = msg[:3800] + "\n… (truncated)"

    # Wrap ERROR+ in a codeblock so stack traces are readable
    if record.levelno >= logging.ERROR:
        description += f"```\n{msg}\n```"
    else:
        description += f"> *{msg}*"

    return discord.Embed(description=description, color=color)


# ── Queue-based handler ───────────────────────────────────────────────────────

class _DiscordQueueHandler(logging.Handler):
    """
    Puts log records onto an asyncio queue.
    The cog drains the queue and ships embeds to Discord.
    Thread-safe — logging can fire from any thread.
    """

    def __init__(self, queue: asyncio.Queue) -> None:  # type: ignore[type-arg]
        super().__init__()
        self._queue = queue

    def emit(self, record: logging.LogRecord) -> None:
        # Never log our own shipping — avoids infinite recursion
        if record.name.startswith("musubi.discordlog"):
            return
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            pass   # queue backed up — drop rather than crash


# ── Cog ───────────────────────────────────────────────────────────────────────

class DiscordLog(commands.Cog, name="DiscordLog"):
    """Ships log records to a Discord channel via webhook."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot: MusubiBot       = bot  # type: ignore[assignment]
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)  # type: ignore[type-arg]
        self._webhook_url: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._handler  = _DiscordQueueHandler(self._queue)
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()
        await self._ensure_webhook()
        self._install_handler()
        self._task = asyncio.create_task(self._flush_loop(), name="discordlog-flush")
        log.info("DiscordLog ready — shipping to channel %d", LOG_CHANNEL_ID)

    async def cog_unload(self) -> None:
        # Uninstall handler first so no new records enter the queue
        self._uninstall_handler()
        # Drain what's left
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._drain_remaining()
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Webhook setup ─────────────────────────────────────────────────────────

    async def _ensure_webhook(self) -> None:
        """Find or create the Musubi Logs webhook in the target channel."""
        channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if not channel or not isinstance(channel, discord.TextChannel):
            log.warning("DiscordLog: channel %d not found or not a text channel", LOG_CHANNEL_ID)
            return

        try:
            webhooks = await channel.webhooks()
            existing = next((w for w in webhooks if w.name == WEBHOOK_NAME), None)
            if existing:
                self._webhook_url = existing.url
                log.debug("DiscordLog: reusing existing webhook")
                return

            wh = await channel.create_webhook(
                name=WEBHOOK_NAME,
                reason="Musubi runtime log shipping",
            )
            self._webhook_url = wh.url
            log.debug("DiscordLog: created new webhook")
        except discord.Forbidden:
            log.warning("DiscordLog: missing Manage Webhooks permission in channel %d", LOG_CHANNEL_ID)
        except discord.HTTPException as e:
            log.warning("DiscordLog: webhook setup failed — %s", e)

    # ── Handler install/uninstall ──────────────────────────────────────────────

    def _install_handler(self) -> None:
        """Attach the queue handler to the root musubi logger."""
        self._handler.setLevel(logging.INFO)
        # Filter: only musubi.* loggers, skip DEBUG
        self._handler.addFilter(
            lambda r: r.name.startswith("musubi") and r.levelno >= logging.INFO
        )
        logging.getLogger("musubi").addHandler(self._handler)

    def _uninstall_handler(self) -> None:
        logging.getLogger("musubi").removeHandler(self._handler)

    # ── Flush loop ────────────────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        """Drain the queue every FLUSH_INTERVAL seconds and ship to Discord."""
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            await self._drain_remaining()

    async def _drain_remaining(self) -> None:
        """Ship all queued records in one pass (batched into embeds)."""
        if not self._webhook_url:
            return

        batch: list[logging.LogRecord] = []
        try:
            while True:
                record = self._queue.get_nowait()
                batch.append(record)
                if len(batch) >= 10:   # Discord allows up to 10 embeds per message
                    await self._ship(batch)
                    batch = []
        except asyncio.QueueEmpty:
            pass

        if batch:
            await self._ship(batch)

    async def _ship(self, records: list[logging.LogRecord]) -> None:
        """POST a webhook message with up to 10 embeds."""
        if not self._webhook_url or not self._session or self._session.closed:
            return

        embeds = [_record_to_embed(r) for r in records]
        payload = {
            "embeds":   [e.to_dict() for e in embeds],
            "flags":    4096,  # SUPPRESS_NOTIFICATIONS — @silent
        }

        try:
            async with self._session.post(
                self._webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 429:
                    retry_after = float((await resp.json()).get("retry_after", 2))
                    await asyncio.sleep(retry_after)
                elif resp.status not in (200, 204):
                    # Don't log this — would cause recursion
                    pass
        except Exception:
            pass  # Network failure — silently drop, never crash the bot

    # ── Startup/shutdown system events ────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Ship a startup event immediately on ready."""
        assert self.bot.user is not None
        invite = discord.utils.oauth_url(
            self.bot.user.id,
            permissions=discord.Permissions(administrator=True),
        )
        count = len(self.bot.guilds)
        record = logging.LogRecord(
            name="musubi.main", level=logging.INFO,
            pathname="", lineno=0,
            msg=f"✅ **Musubi online** — `{self.bot.user}` · `{count}` guilds · [Invite]({invite})",
            args=(), exc_info=None,
        )
        self._queue.put_nowait(record)
        # Force an immediate flush so startup shows up right away
        await self._drain_remaining()

    async def ship_shutdown(self) -> None:
        """
        Called by main.py close() before the event loop dies.
        Ships a shutdown notice and drains the queue one final time.
        """
        record = logging.LogRecord(
            name="musubi.main", level=logging.INFO,
            pathname="", lineno=0,
            msg="🔴 **Musubi shutting down.**",
            args=(), exc_info=None,
        )
        self._queue.put_nowait(record)
        await self._drain_remaining()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DiscordLog(bot))