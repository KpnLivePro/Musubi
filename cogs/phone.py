"""
Project MUSUBI — cogs/phone.py
Guild-to-guild call routing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

import discord
from discord.ext import commands, tasks

from datamanager import DataManager
from embeds import Embeds
from botprotocol import MusubiBot

log = logging.getLogger("musubi.phone")

SEARCH_TIMEOUT  = 30   # seconds before a searching session is cancelled
IDLE_MINUTES    = 30   # minutes before an active call is ended for inactivity
CONNECT_DELAY   = 6    # seconds to show tip+heart before confirming connection

# ── Tips ─────────────────────────────────────────────────────────────────────
# {prefix} is replaced at runtime with the guild's resolved prefix.

TIPS: list[str] = [
    "Try `{prefix}anonymous` to hide your name and avatar during calls.",
    "Use `{prefix}friendme` to share your Discord tag with the other server.",
    "Earn XP every time you send a message on a call — check the `/callboard`!",
    "Set a custom server prefix with `/prefix server <prefix>` — admins only.",
    "Anonymous mode? Toggle it anytime with `{prefix}anon` even mid-call.",
    "Use `{prefix}hangup` or just `{prefix}h` to end a call quickly.",
    "Premium users can set a custom nickname with `/me name <nickname>`.",
    "Premium users can set a custom avatar with `/me avatar <url>`.",
    "Calls auto-end after {idle} minutes of inactivity to keep the network clean.".replace("{idle}", str(IDLE_MINUTES)),
    "The faster you reply, the more XP your server earns — stay active!",
    "Use `{prefix}call` or just `{prefix}c` to dial into a new call.",
    "Check your profile and premium status anytime with `/me status`.",
    "Redeem a premium key with `/redeem <key>` — ask a network admin for one.",
    "Your server's booth channel is where all calls happen — set it with `/setbooth`.",
    "View the top 7 most active servers this month with `/callboard`.",
]


def _get_tip(prefix: str) -> str:
    """Return a random tip with the guild prefix substituted in."""
    import random
    tip = random.choice(TIPS)
    return tip.replace("{prefix}", f"`{prefix}`")


def _resolve_prefix(bot: MusubiBot, guild: discord.Guild | None) -> str:
    """Get the best display prefix for a guild — falls back to @Musubi."""
    from main import DEFAULT_PREFIX
    if guild is None:
        return "@Musubi"
    data = bot.data
    g = data.get_guild(guild.id)
    if g and g.get("prefix"):
        return g["prefix"]
    return DEFAULT_PREFIX


# ── Hearts ────────────────────────────────────────────────────────────────────
# Sent alongside the tip embed when a call connects (6s delay now).
# Also used for premature hangup nudges.

HEARTS: list[str] = [
    "Let's keep the Musubi phone community safe. 💜",
    "Be kind — share a heartfelt message with someone new.",
    "Every call is a chance to make someone's day better.",
    "You never know who needs to hear a friendly voice today.",
    "Real connections start with a simple hello.",
    "Spread good vibes — the network is as warm as you make it.",
    "Treat others the way you'd want to be treated on a call.",
    "Someone out there is hoping for a great conversation. Be that person.",
    "Small talk leads to big friendships. Give it a chance.",
    "The best calls are the ones you didn't expect to enjoy.",
    "Be the reason someone smiles today.",
    "Musubi means connection — bring that energy to every call.",
    "Every server on here is a community of real people. Make it count.",
    "Kindness costs nothing. Leave every call better than you found it.",
    "A little patience goes a long way — you might meet someone amazing.",
]

HEART_HANGUP: list[str] = [
    "Let's give this one some more time to answer. 💜",
    "Ah, come on — you're no fun! Give it a sec.",
    "Hey there, let the other server warm up first!",
    "Patience! Good things come to those who wait on the line.",
    "Don't hang up just yet — someone might be typing right now!",
]

PREMATURE_HANGUP_WINDOW = 12  # seconds after connect where hangup shows a heart nudge


def _get_heart() -> str:
    import random
    return random.choice(HEARTS)


def _get_hangup_heart() -> str:
    import random
    return random.choice(HEART_HANGUP)


class Phone(commands.Cog):

    def __init__(self, bot: MusubiBot) -> None:
        self.bot = bot
        self.data: DataManager = bot.data
        self.cleanup_loop.start()
        # guild_id → timestamp of when their call connected (for premature hangup detection)
        self._connected_at: dict[str, float] = {}

    async def cog_unload(self) -> None:
        self.cleanup_loop.cancel()

    # ── Status helper ────────────────────────────────────────────────────

    async def _update_status(self) -> None:
        """Update bot presence — called on every connect/disconnect."""
        count = await self.data.count_active_calls()
        if count == 0:
            label = "No active calls"
        elif count == 1:
            label = "1 active call"
        else:
            label = f"{count} active calls"
        await self.bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=label,
            )
        )

    # ── Background cleanup ───────────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def cleanup_loop(self) -> None:
        """End idle sessions and cancel stale searching sessions."""
        idle = await self.data.get_idle_sessions(idle_minutes=IDLE_MINUTES)
        for session in idle:
            await self._notify_end(session, reason="idle")
            await self.data.end_session(session["id"])
            await self._end_session_cleanup(session)
            # Clear connect timestamps for both sides
            self._connected_at.pop(session.get("caller_guild", ""), None)
            self._connected_at.pop(session.get("target_guild", ""), None)
        if idle:
            log.info("Cleanup — ended %d idle session(s).", len(idle))

        stale = await self.data.get_stale_searching_sessions(timeout_seconds=SEARCH_TIMEOUT + 10)
        for session in stale:
            await self.data.end_session(session["id"], status="cancelled")
            self._connected_at.pop(session.get("caller_guild", ""), None)
            ch = self.bot.get_channel(int(session["caller_channel"]))
            if ch and isinstance(ch, discord.TextChannel):
                try:
                    await ch.send(embed=Embeds.no_answer())
                except discord.Forbidden:
                    pass
        if stale:
            log.info("Cleanup — cancelled %d stale search(es).", len(stale))

        if idle or stale:
            await self._update_status()

    @cleanup_loop.before_loop
    async def before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    # ── Notify helpers ───────────────────────────────────────────────────

    async def _notify_end(
        self,
        session: dict,
        reason: str = "ended",
        exclude_channel: int | None = None,
    ) -> None:
        """
        Notify both sides of a call that it has ended.
        Pass exclude_channel to skip one side (e.g. the guild that hung up).
        """
        embed_map = {
            "idle":      Embeds.ended_idle(),
            "hangup":    Embeds.ended_hangup(),
            "terminate": Embeds.ended_terminated(),
            "ended":     Embeds.ended(),
        }
        embed = embed_map.get(reason, Embeds.ended())

        for channel_id in [session.get("caller_channel"), session.get("target_channel")]:
            if not channel_id:
                continue
            if exclude_channel and int(channel_id) == exclude_channel:
                continue
            ch = self.bot.get_channel(int(channel_id))
            if ch and isinstance(ch, discord.TextChannel):
                try:
                    await ch.send(embed=embed)
                except discord.Forbidden:
                    pass

    async def _end_session_cleanup(self, session: dict) -> None:
        """
        Flush bridge XP buffer and clear session caches when any call ends.
        Keeps Supabase writes accurate without hammering on every message.
        """
        from cogs.bridge import Bridge
        bridge = cast(Bridge, self.bot.cogs.get("Bridge"))
        if bridge:
            for guild_id in [session.get("caller_guild"), session.get("target_guild")]:
                if guild_id:
                    await bridge.flush_xp(guild_id)
            bridge.clear_session_cache(session["id"])

    async def _check_guild(self, ctx: commands.Context[MusubiBot]) -> bool:
        assert ctx.guild is not None
        if not self.data.is_guild_registered(ctx.guild.id):
            await ctx.send(
                embed=Embeds.error("This server hasn't been set up yet. Run `/setup` first."),
                ephemeral=True,
            )
            return False
        if self.data.is_guild_banned(ctx.guild.id):
            await ctx.send(
                embed=Embeds.error("This server has been banned from the Musubi network."),
                ephemeral=True,
            )
            return False
        return True

    async def _check_booth(self, ctx: commands.Context[MusubiBot]) -> bool:
        assert ctx.guild is not None
        g = self.data.get_guild(ctx.guild.id)
        if not g or str(ctx.channel.id) != g["booth_channel"]:
            booth = g["booth_channel"] if g else None
            ref   = f"<#{booth}>" if booth else "your designated booth channel"
            await ctx.send(
                embed=Embeds.error(f"Calls can only be placed from {ref}."),
                ephemeral=True,
            )
            return False
        return True

    # ── /call ────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="call", aliases=["c"], description="Search for another server to connect with.")
    @commands.cooldown(rate=1, per=15, type=commands.BucketType.guild)
    async def call(self, ctx: commands.Context[MusubiBot]) -> None:
        """Place a call and search for an available server to connect with."""
        assert ctx.guild is not None
        if not await self._check_guild(ctx):
            return
        if not await self._check_booth(ctx):
            return

        if await self.data.get_active_session(ctx.guild.id):
            await ctx.send(
                embed=Embeds.error("You're already in a call. Use `/hangup` to end it first."),
                ephemeral=True,
            )
            return

        if await self.data.get_searching_session(ctx.guild.id):
            await ctx.send(
                embed=Embeds.error("Already searching for a connection. Use `/hangup` to cancel."),
                ephemeral=True,
            )
            return

        is_premium_guild = await self.data.is_premium_guild(ctx.guild.id)
        partner = await self.data.find_match(ctx.guild.id, priority=is_premium_guild)
        prefix  = _resolve_prefix(self.bot, ctx.guild)

        if partner:
            # ── Immediate connect path ────────────────────────────────────
            # Send one message and edit it through the flow so it feels like
            # a single natural progression, not a burst of separate messages.
            #
            # Step 1 — searching (feels like dialling)
            msg = await ctx.send(embed=Embeds.searching())
            await asyncio.sleep(1.5)

            # Step 2 — tip + heart (6s window, combined into one edit)
            await msg.edit(embed=Embeds.tip_and_heart(_get_tip(prefix), _get_heart()))
            await asyncio.sleep(CONNECT_DELAY)

            # Step 3 — actually connect
            session = await self.data.connect_partner_session(
                waiting_session_id=partner["id"],
                caller_guild=str(ctx.guild.id),
                caller_channel=str(ctx.channel.id),
            )
            if not session:
                await msg.edit(embed=Embeds.error("Failed to place call. Please try again in a moment."))
                return

            # Record connect time for premature hangup detection (both sides)
            now = asyncio.get_event_loop().time()
            self._connected_at[str(ctx.guild.id)]       = now
            self._connected_at[partner["caller_guild"]] = now

            # Step 4 — connected (edit our message, send full flow to waiting guild)
            await msg.edit(embed=Embeds.connected())
            await self._update_status()
            log.info("Call connected — %s ↔ %s", ctx.guild.id, partner["caller_guild"])

            # Give the waiting guild the same full experience via their searching message.
            # We look up their channel and send the tip+heart then edit to connected.
            partner_channel = self.bot.get_channel(int(partner["caller_channel"]))
            if partner_channel and isinstance(partner_channel, discord.TextChannel):
                try:
                    partner_prefix = _resolve_prefix(self.bot, partner_channel.guild)
                    partner_msg = await partner_channel.send(
                        embed=Embeds.tip_and_heart(_get_tip(partner_prefix), _get_heart())
                    )
                    await asyncio.sleep(2)
                    await partner_msg.edit(embed=Embeds.connected())
                except discord.Forbidden:
                    pass

        else:
            # ── Search queue path ─────────────────────────────────────────
            our_session = await self.data.create_session(ctx.guild.id, ctx.channel.id, ctx.author.id)
            if not our_session:
                await ctx.send(
                    embed=Embeds.error("Failed to place call. Please try again in a moment."),
                    ephemeral=True,
                )
                return

            msg = await ctx.send(embed=Embeds.searching())
            log.info("Searching — guild:%s session:%s", ctx.guild.id, our_session["id"])

            deadline = asyncio.get_event_loop().time() + SEARCH_TIMEOUT
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(3)

                current = await self.data.get_searching_session(ctx.guild.id)
                if not current or current["id"] != our_session["id"]:
                    # Matched by another caller — edit searching msg into tip+heart then connected
                    active = await self.data.get_active_session(ctx.guild.id)
                    if active and active["id"] not in self.data._ended_sessions:
                        await msg.edit(
                            embed=Embeds.tip_and_heart(_get_tip(prefix), _get_heart())
                        )
                        await asyncio.sleep(2)
                        await msg.edit(embed=Embeds.connected())
                        await self._update_status()
                        log.info("Call connected (polled) — guild:%s", ctx.guild.id)
                    return

            # Timed out
            still = await self.data.get_searching_session(ctx.guild.id)
            if still and still["id"] == our_session["id"]:
                await self.data.end_session(our_session["id"], status="cancelled")
                await msg.edit(embed=Embeds.no_answer())
                log.info("Search timeout — guild:%s", ctx.guild.id)

    # ── /hangup ──────────────────────────────────────────────────────────

    @commands.hybrid_command(name="hangup", aliases=["h"], description="End the current call or cancel a search.")
    async def hangup(self, ctx: commands.Context[MusubiBot]) -> None:
        """End your current call or cancel an active search."""
        assert ctx.guild is not None
        if not await self._check_guild(ctx):
            return
        if not await self._check_booth(ctx):
            return

        gid = str(ctx.guild.id)

        # Active call
        session = await self.data.get_active_session(ctx.guild.id)
        if session:
            # Premature hangup — call connected very recently, nudge them to stay
            connected_at = self._connected_at.get(gid)
            if connected_at:
                elapsed = asyncio.get_event_loop().time() - connected_at
                if elapsed < PREMATURE_HANGUP_WINDOW:
                    await ctx.send(
                        embed=Embeds.heart(_get_hangup_heart()),
                        ephemeral=True,
                    )
                    return  # Don't end the call — let them decide again

            # Clean up connect timestamp for both sides
            self._connected_at.pop(gid, None)
            partner_gid = (
                session["target_guild"]
                if session["caller_guild"] == gid
                else session["caller_guild"]
            )
            if partner_gid:
                self._connected_at.pop(str(partner_gid), None)

            await self.data.end_session(session["id"])
            await self._end_session_cleanup(session)
            await self._notify_end(session, reason="hangup", exclude_channel=ctx.channel.id)
            await ctx.send(embed=Embeds.ended())
            await self._update_status()
            log.info("Hangup — guild:%s session:%s", gid, session["id"])
            return

        # Searching — cancel
        session = await self.data.get_searching_session(ctx.guild.id)
        if session:
            await self.data.end_session(session["id"], status="cancelled")
            await ctx.send(embed=Embeds.ended())
            return

        await ctx.send(embed=Embeds.error("You're not in a call or search."), ephemeral=True)

    # ── /anonymous ───────────────────────────────────────────────────────

    @commands.hybrid_command(name="anonymous", aliases=["a"], description="Toggle anonymous mode for your messages during calls.")
    async def anonymous(self, ctx: commands.Context[MusubiBot]) -> None:
        """
        Toggle anonymous mode. When on, your name and avatar are hidden
        from the other server — you appear as 📞 Anonymous.
        """
        u   = self.data.get_user(ctx.author.id)
        new = not u.get("is_anonymous", False)
        u["is_anonymous"] = new
        await self.data.upsert_user(ctx.author.id)

        state = "**on** — you'll appear as `📞 Anonymous`" if new else "**off** — your name will be shown"
        await ctx.send(
            embed=Embeds.action(f"Anonymous mode is now {state}.", ctx.author),
            ephemeral=True,
        )

    # ── /friendme ────────────────────────────────────────────────────────

    @commands.hybrid_command(name="friendme", aliases=["f"], description="Send your Discord tag to the other server during a call.")
    async def friendme(self, ctx: commands.Context[MusubiBot]) -> None:
        """
        Share your Discord tag with the other server so they can add you.
        Only works during an active call. Respects anonymous mode — anonymous
        users cannot use this command.
        """
        assert ctx.guild is not None

        if not self.data.is_guild_registered(ctx.guild.id):
            await ctx.send(
                embed=Embeds.error("This server hasn't been set up yet. Run `/setup` first."),
                ephemeral=True,
            )
            return

        # Enforce booth channel consistency
        g = self.data.get_guild(ctx.guild.id)
        if not g or str(ctx.channel.id) != g["booth_channel"]:
            booth = g["booth_channel"] if g else None
            ref   = f"<#{booth}>" if booth else "your booth channel"
            await ctx.send(
                embed=Embeds.error(f"This command can only be used in {ref}."),
                ephemeral=True,
            )
            return

        # Block anonymous users — friendme would deanonymize them
        u = self.data.get_user(ctx.author.id)
        if u.get("is_anonymous"):
            await ctx.send(
                embed=Embeds.error("You can't use `/friendme` while anonymous mode is on."),
                ephemeral=True,
            )
            return

        session = await self.data.get_active_session(ctx.guild.id)
        if not session:
            await ctx.send(embed=Embeds.error("You're not in an active call."), ephemeral=True)
            return

        gid = str(ctx.guild.id)
        target_channel_id = (
            session["target_channel"]
            if session["caller_guild"] == gid
            else session["caller_channel"]
        )

        if not target_channel_id:
            await ctx.send(embed=Embeds.error("Couldn't determine the target channel."), ephemeral=True)
            return

        target_channel = self.bot.get_channel(int(target_channel_id))
        if not target_channel or not isinstance(target_channel, discord.TextChannel):
            await ctx.send(
                embed=Embeds.error("Couldn't reach the other server right now."),
                ephemeral=True,
            )
            return

        try:
            await target_channel.send(embed=Embeds.friendme(str(ctx.author)))
        except discord.Forbidden:
            await ctx.send(
                embed=Embeds.error("Couldn't send to the other server — they may have restricted the channel."),
                ephemeral=True,
            )
            return

        await ctx.send(
            embed=Embeds.action("Your Discord tag has been sent to the other server.", ctx.author),
            ephemeral=True,
        )
        log.info("Friendme — user:%d session:%s", ctx.author.id, session["id"])


async def setup(bot: MusubiBot) -> None:
    await bot.add_cog(Phone(bot))