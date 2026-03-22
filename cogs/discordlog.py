"""
Project MUSUBI — cogs/discordlog.py
Runtime log shipping to a Discord channel via webhook.

Sends silent (@silent) messages so no one gets pinged.
Creates and caches its own webhook in the target channel.

Events shipped:
  - Bot startup / shutdown
  - Runtime errors (ERROR and above from any musubi.* logger)
  - All INFO+ from musubi.* loggers (phone, bridge, sudo, statspush, etc.)

Log level routing:
  CRITICAL  →  ‼️  red
  ERROR     →  ❌  red
  WARNING   →  ⚠️  yellow
  INFO      →  🔵  blue

WHY webhook setup is deferred to on_ready:
  cog_load runs inside setup_hook, BEFORE on_ready fires.
  At that point the guild cache is empty and bot.get_channel()
  returns None for every channel ID. We install the log handler
  immediately (so nothing is lost) but defer the actual webhook
  creation until on_ready when the cache is populated.

Channel: 1475292266925916270
"""

from __future__ import annotations

import asyncio
import logging
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
MAX_QUEUE       = 500
FLUSH_INTERVAL  = 2.0   # seconds between flush cycles

log = logging.getLogger("musubi.discordlog")


# ── Embed builders ────────────────────────────────────────────────────────────

def _level_icon(level: int) -> str:
    if level >= logging.CRITICAL: return "‼️"
    if level >= logging.ERROR:    return "❌"
    if level >= logging.WARNING:  return "⚠️"
    return "🔵"


def _level_color(level: int) -> int:
    if level >= logging.CRITICAL: return 0xFF0000
    if level >= logging.ERROR:    return 0xFF4444
    if level >= logging.WARNING:  return 0xFFAA00
    return BRAND_COLOR


def _record_to_embed(record: logging.LogRecord) -> discord.Embed:
    icon    = _level_icon(record.levelno)
    color   = _level_color(record.levelno)
    ts_str  = f"<t:{int(record.created)}:T>"

    msg = record.getMessage()
    if record.exc_info:
        tb  = "".join(traceback.format_exception(*record.exc_info))
        msg = f"{msg}\n{tb}"
    if len(msg) > 3800:
        msg = msg[:3800] + "\n… (truncated)"

    body = f"```\n{msg}\n```" if record.levelno >= logging.ERROR else f"> *{msg}*"

    return discord.Embed(
        description=f"> `{icon}` `[{record.name}]` {ts_str}\n{body}",
        color=color,
    )


# ── Queue handler ─────────────────────────────────────────────────────────────

class _DiscordQueueHandler(logging.Handler):
    """Thread-safe handler — puts records on asyncio queue for the cog to drain."""

    def __init__(self, queue: asyncio.Queue) -> None:  # type: ignore[type-arg]
        super().__init__()
        self._queue = queue

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("musubi.discordlog"):
            return  # never log ourselves — infinite recursion guard
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            pass


# ── Cog ───────────────────────────────────────────────────────────────────────

class DiscordLog(commands.Cog, name="DiscordLog"):

    def __init__(self, bot: MusubiBot) -> None:
        self.bot  = bot
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)  # type: ignore[type-arg]
        self._webhook_url: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._handler = _DiscordQueueHandler(self._queue)
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._ready  = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        """
        Install the log handler immediately so no records are lost.
        Webhook setup is deferred to on_ready because the guild cache
        isn't available yet at this point in startup.
        """
        self._session = aiohttp.ClientSession()
        self._install_handler()
        self._task = asyncio.create_task(self._flush_loop(), name="discordlog-flush")

    async def cog_unload(self) -> None:
        self._uninstall_handler()
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

    async def _ensure_webhook(self) -> bool:
        """
        Find or create the Musubi Logs webhook.
        Called from on_ready — guild cache is guaranteed to be populated.
        Returns True if webhook is ready.
        """
        if self._webhook_url:
            return True

        channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if not channel or not isinstance(channel, discord.TextChannel):
            # Try fetching directly in case it's not in cache (e.g. DM channel)
            try:
                channel = await self.bot.fetch_channel(LOG_CHANNEL_ID)
            except Exception:
                pass
        if not channel or not isinstance(channel, discord.TextChannel):
            log.error(
                "DiscordLog: channel %d not found. "
                "Make sure the bot is in the server and has access to that channel.",
                LOG_CHANNEL_ID,
            )
            return False

        try:
            webhooks = await channel.webhooks()
            existing = next((w for w in webhooks if w.name == WEBHOOK_NAME), None)
            if existing:
                self._webhook_url = existing.url
                log.info("DiscordLog: webhook ready (existing) — channel %d", LOG_CHANNEL_ID)
                return True

            wh = await channel.create_webhook(
                name=WEBHOOK_NAME,
                reason="Musubi runtime log shipping",
            )
            self._webhook_url = wh.url
            log.info("DiscordLog: webhook created — channel %d", LOG_CHANNEL_ID)
            return True

        except discord.Forbidden:
            log.error(
                "DiscordLog: missing Manage Webhooks permission in channel %d. "
                "Grant it and reload the cog.",
                LOG_CHANNEL_ID,
            )
            return False
        except discord.HTTPException as e:
            log.error("DiscordLog: webhook setup failed — %s", e)
            return False

    # ── Handler ───────────────────────────────────────────────────────────────

    def _install_handler(self) -> None:
        self._handler.setLevel(logging.INFO)
        self._handler.addFilter(
            lambda r: r.name.startswith("musubi") and r.levelno >= logging.INFO
        )
        logging.getLogger("musubi").addHandler(self._handler)

    def _uninstall_handler(self) -> None:
        logging.getLogger("musubi").removeHandler(self._handler)

    # ── Flush loop ────────────────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            await self._drain_remaining()

    async def _drain_remaining(self) -> None:
        if not self._webhook_url:
            return
        batch: list[logging.LogRecord] = []
        try:
            while True:
                record = self._queue.get_nowait()
                batch.append(record)
                if len(batch) >= 10:
                    await self._ship(batch)
                    batch = []
        except asyncio.QueueEmpty:
            pass
        if batch:
            await self._ship(batch)

    async def _ship(self, records: list[logging.LogRecord]) -> None:
        if not self._webhook_url or not self._session or self._session.closed:
            return
        embeds  = [_record_to_embed(r) for r in records]
        payload = {
            "embeds": [e.to_dict() for e in embeds],
            "flags":  4096,   # SUPPRESS_NOTIFICATIONS (@silent)
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
        except Exception:
            pass   # never crash the bot over a log message

    # ── Events ────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """
        on_ready is the earliest point where the guild cache is populated
        and bot.get_channel() works reliably. Set up the webhook here,
        then immediately flush any records that queued up during startup.
        """
        ok = await self._ensure_webhook()
        if not ok:
            return

        self._ready = True

        # Ship the startup notice
        assert self.bot.user is not None
        invite = discord.utils.oauth_url(
            self.bot.user.id,
            permissions=discord.Permissions(administrator=True),
        )
        count = len(self.bot.guilds)
        record = logging.LogRecord(
            name="musubi.main", level=logging.INFO,
            pathname="", lineno=0,
            msg=(
                f"✅ **Musubi online**\n"
                f"> `{self.bot.user}` · `{count}` guilds\n"
                f"> [Invite Link]({invite})"
            ),
            args=(), exc_info=None,
        )
        self._queue.put_nowait(record)

        # Flush startup queue immediately — don't wait 2s
        await self._drain_remaining()

    async def ship_shutdown(self) -> None:
        """Called by main.py close() to send a shutdown notice."""
        record = logging.LogRecord(
            name="musubi.main", level=logging.INFO,
            pathname="", lineno=0,
            msg="🔴 **Musubi shutting down.**",
            args=(), exc_info=None,
        )
        self._queue.put_nowait(record)
        await self._drain_remaining()

    # ── m.logtest ─────────────────────────────────────────────────────────────

    @commands.command(name="logtest")
    async def logtest(self, ctx: commands.Context) -> None:
        """
        Test the Discord log handler. Sudo only.
        Fires one record at each level so you can verify the channel is working.
        """
        if not self.bot.data.is_sudo(ctx.author.id):
            await ctx.send(embed=discord.Embed(
                description="> `❗` *No permission.*", color=BRAND_COLOR
            ), ephemeral=True)
            return

        if not self._webhook_url:
            await ctx.send(embed=discord.Embed(
                description=(
                    f"> `❗` *Webhook not initialised.*\n"
                    f"> Channel `{LOG_CHANNEL_ID}` — bot may not be in that server "
                    f"or lacks Manage Webhooks permission."
                ),
                color=0xFF4444,
            ), ephemeral=True)
            return

        # Fire test records at every level
        test_log = logging.getLogger("musubi.logtest")
        test_log.info("logtest INFO — handler is working ✅")
        test_log.warning("logtest WARNING — this is a test warning ⚠️")
        test_log.error("logtest ERROR — this is a test error ❌")

        await ctx.send(embed=discord.Embed(
            description=(
                f"> `✅` *3 test records sent to <#{LOG_CHANNEL_ID}>.*\n"
                f"> INFO · WARNING · ERROR — check the log channel."
            ),
            color=BRAND_COLOR,
        ), ephemeral=True)


async def setup(bot: MusubiBot) -> None:
    await bot.add_cog(DiscordLog(bot))