"""
Project MUSUBI — main.py
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys

import discord
from aiohttp import ClientSession
from discord.ext import commands
from discord.ext.commands._types import BotT
from dotenv import load_dotenv

from botprotocol import MusubiBot
from datamanager import DataManager
from embeds import Embeds

load_dotenv()

TOKEN        = os.environ.get("DISCORD_TOKEN")
_owner       = os.environ.get("OWNER_ID")
OWNER_ID:int = int(_owner) if _owner else 0
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not TOKEN:
    sys.exit("DISCORD_TOKEN not set")
if not OWNER_ID:
    sys.exit("OWNER_ID not set")
if not SUPABASE_URL:
    sys.exit("SUPABASE_URL not set")
if not SUPABASE_KEY:
    sys.exit("SUPABASE_KEY not set")

DEFAULT_PREFIX = "m."

INITIAL_EXTENSIONS = [
    "cogs.phone",
    "cogs.bridge",
    "cogs.premium",
    "cogs.sudo",
    "cogs.config",
    "cogs.help",
    "cogs.filter",
    "cogs.leaderboard",
    "cogs.statspush",
    "cogs.discordlog",
]


class Musubi(MusubiBot):
    def __init__(
        self,
        *,
        web_session: ClientSession,
        data: DataManager,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix=resolve_prefix,
            help_command=None,
            intents=intents,
            owner_id=OWNER_ID,
            max_messages=1_000,
            chunk_guilds_at_startup=False,
        )

        self.session = web_session
        self.data    = data

    async def setup_hook(self) -> None:
        await self.data.load_all()
        log.info("DataManager cache warmed.")

        if OWNER_ID and not self.data.is_sudo(OWNER_ID):
            await self.data.add_sudo(OWNER_ID, granted_by=OWNER_ID)
            log.info("Owner %d bootstrapped into sudo.", OWNER_ID)

        results = await asyncio.gather(
            *[self.load_extension(ext) for ext in INITIAL_EXTENSIONS],
            return_exceptions=True,
        )
        for ext, result in zip(INITIAL_EXTENSIONS, results):
            if isinstance(result, Exception):
                log.error("Failed to load %s: %s", ext, result)
            else:
                log.info("Loaded: %s", ext)

        synced = await self.tree.sync()
        log.info("Global commands synced: %d commands.", len(synced))

    async def on_ready(self) -> None:
        assert self.user is not None
        invite = discord.utils.oauth_url(
            self.user.id,
            permissions=discord.Permissions(administrator=True),
        )
        log.info("Musubi online | %s (ID: %d) | Guilds: %d", self.user, self.user.id, len(self.guilds))
        log.info("Invite: %s", invite)
        count = await self.data.count_active_calls()
        if count == 0:
            label = "No active calls"
        elif count == 1:
            label = "1 active call"
        else:
            label = f"{count} active calls"
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=label,
            )
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """DM the server owner a welcome embed when Musubi joins."""
        owner = guild.owner
        if not owner:
            if not guild.owner_id:
                return
            try:
                owner = await self.fetch_user(guild.owner_id)
            except Exception:
                return

        bot_avatar = str(self.user.display_avatar.url) if self.user else None
        embed = Embeds.welcome(bot_avatar)

        try:
            await owner.send(embed=embed)
            log.info("Welcome DM sent — guild:%d owner:%d", guild.id, owner.id)
        except (discord.Forbidden, discord.HTTPException):
            log.info("Welcome DM blocked — owner:%d, falling back to first channel", owner.id)
            channel = next(
                (
                    c for c in guild.text_channels
                    if c.permissions_for(guild.me).send_messages
                ),
                None,
            )
            if channel:
                try:
                    await channel.send(embed=embed)
                    log.info("Welcome embed sent to channel:%d guild:%d", channel.id, guild.id)
                except discord.HTTPException as e:
                    log.warning("Welcome fallback failed — guild:%d: %s", guild.id, e)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """
        Fired when Musubi is kicked, banned, or the server is deleted.

        Design decision — DB row is KEPT, cache is flushed:
          - XP history, premium, and invite quota are preserved.
          - If the guild re-adds the bot, /setup works again and their
            callboard ranking and premium carry over intact.
          - The in-memory cache is dropped so bridge.py stops trying
            to relay messages for a guild that's no longer reachable.

        The only thing we actively clean up is open sessions — leaving
        an active or searching session open would strand the partner
        guild on the other end of the call with no hangup notification.
        """
        gid = str(guild.id)

        # ── End any open session so the partner isn't left hanging ────────
        try:
            session = await self.data.get_active_session(gid)
            if session:
                from cogs.phone import Phone
                from typing import cast as _cast
                phone_cog = _cast(Phone, self.cogs.get("Phone"))
                if phone_cog:
                    await phone_cog._notify_end(session, reason="hangup")
                    await phone_cog._end_session_cleanup(session)
                await self.data.end_session(session["id"])
                log.info("on_guild_remove: ended active session %s", session["id"])
            else:
                searching = await self.data.get_searching_session(gid)
                if searching:
                    await self.data.end_session(searching["id"], status="cancelled")
                    log.info("on_guild_remove: cancelled searching session %s", searching["id"])
        except Exception as e:
            log.warning("on_guild_remove: session cleanup failed guild:%s — %s", gid, e)

        # ── Flush in-memory cache — DB row intentionally preserved ────────
        self.data.guilds.pop(gid, None)
        self.data.webhook_cache.pop(gid, None)

        log.info("Guild removed — id:%d name:%r", guild.id, guild.name)

    async def on_command_error(self, ctx: commands.Context[BotT], exception: commands.CommandError, /) -> None:
        error = exception

        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.CheckFailure):
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                embed=Embeds.error(f"Missing required argument: `{error.param.name}`."),
                ephemeral=True,
            )
            return

        if isinstance(error, commands.BadArgument):
            await ctx.send(
                embed=Embeds.error(f"Invalid argument: {error}"),
                ephemeral=True,
            )
            return

        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                embed=Embeds.error(f"Slow down! Try again in `{error.retry_after:.1f}s`."),
                ephemeral=True,
            )
            return

        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send(
                embed=Embeds.error("This command can only be used inside a server."),
                ephemeral=True,
            )
            return

        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                embed=Embeds.error("You don't have permission to use this command."),
                ephemeral=True,
            )
            return

        if isinstance(error, commands.BotMissingPermissions):
            missing = ", ".join(f"`{p}`" for p in error.missing_permissions)
            await ctx.send(
                embed=Embeds.error(f"I'm missing the following permissions: {missing}."),
                ephemeral=True,
            )
            return

        if isinstance(error, commands.DisabledCommand):
            await ctx.send(
                embed=Embeds.error("This command is currently disabled."),
                ephemeral=True,
            )
            return

        original = getattr(error, "original", error)
        log.exception(
            "Unhandled error in command '%s' by user %d: %s",
            ctx.command.qualified_name if ctx.command else "unknown",
            ctx.author.id,
            original,
            exc_info=original,
        )
        await ctx.send(
            embed=Embeds.error("Something went wrong. Please try again later."),
            ephemeral=True,
        )

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        log.exception("Unhandled error in '%s'", event_method)

    async def close(self) -> None:
        log.info("Shutting down.")
        # Ship a shutdown notice to Discord before the loop dies
        from cogs.discordlog import DiscordLog
        from typing import cast as _cast
        dl = _cast(DiscordLog, self.cogs.get("DiscordLog"))
        if dl:
            try:
                await dl.ship_shutdown()
            except Exception:
                pass
        await self.data.close()
        await super().close()


def resolve_prefix(bot: Musubi, message: discord.Message) -> list[str]:
    """
    Prefix resolution order (highest to lowest priority):
      1. @mention  — always works
      2. User personal prefix — premium users, works everywhere including DMs
      3. Guild prefix — server-specific, guild messages only
      4. Default prefix — m.
    """
    mention_prefixes = list(commands.when_mentioned(bot, message))
    data: DataManager = bot.data

    extra: list[str] = []

    u = data.get_user(message.author.id)
    if u.get("prefix"):
        extra.append(u["prefix"])

    if message.guild:
        g = data.get_guild(message.guild.id)
        if g and g.get("prefix"):
            guild_pfx = g["prefix"]
            if guild_pfx not in extra:
                extra.append(guild_pfx)

    if DEFAULT_PREFIX not in extra:
        extra.append(DEFAULT_PREFIX)

    return mention_prefixes + extra


def setup_logging() -> None:
    os.makedirs("logs", exist_ok=True)
    fmt = logging.Formatter("[{asctime}] [{levelname:<8}] {name}: {message}", "%Y-%m-%d %H:%M:%S", style="{")

    file_handler = logging.handlers.RotatingFileHandler(
        filename="logs/musubi.log", encoding="utf-8", maxBytes=32 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


log = logging.getLogger("musubi.main")


async def main() -> None:
    setup_logging()
    log.info("Starting Project MUSUBI...")

    async with ClientSession() as web_session:
        data = DataManager()
        async with Musubi(
            web_session=web_session,
            data=data,
        ) as bot:
            assert TOKEN
            await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrupted.")