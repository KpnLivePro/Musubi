"""
Project MUSUBI — cogs/invite.py
Guild invite system — the ONLY way to share Discord invite links across the relay.

How it works:
  - Every registered guild gets a permanent booth-channel invite stored in Guilds.invite_url.
  - /invite (during a call) sends *your* server's invite to the other side.
  - Usage is tracked per guild per day in InviteUsage.
  - Free guilds: 10 invites/day   |   Premium guilds: 30 invites/day
  - Premium guilds also have a 10s cooldown instead of none (non-premium have no cooldown
    but hit the daily cap faster).
  - Admins (Manage Messages) can buy extra quota with server XP:
      150 XP → +5   |   200 XP → +10   |   350 XP → +20
    XP is subtracted from the guild total, demoting them on the callboard.
  - The bot auto-creates the booth invite on first /setup, or lazily on first /invite.

Checks on filter.py:
  - filter.py's INVITE_RE already silently blocks any raw discord.gg message from
    being relayed. /invite bypasses that by sending a system embed, not a raw message.
"""

from __future__ import annotations

import logging
import time
from typing import cast

import discord
from discord.ext import commands

from datamanager import DataManager
from botprotocol import MusubiBot
from embeds import Embeds

log = logging.getLogger("musubi.invite")

# Premium cooldown between /invite uses per user (seconds)
INVITE_COOLDOWN_PREMIUM = 10

# XP tier table — must match DataManager.XP_COSTS
XP_TIERS: list[tuple[int, int]] = [
    (5,  150),
    (10, 200),
    (20, 350),
]


def _is_manager(member: discord.Member) -> bool:
    return member.guild_permissions.manage_messages


class InviteCog(commands.Cog, name="Invite"):

    def __init__(self, bot: MusubiBot) -> None:
        self.bot  = bot
        self.data: DataManager = bot.data

        # user_id → last /invite wall-clock time (for premium cooldown)
        self._last_invite: dict[int, float] = {}

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _ensure_invite(self, guild: discord.Guild, booth_channel_id: str) -> str | None:
        """
        Return the stored invite URL for this guild, creating one if missing.
        Creates a permanent (max_age=0, max_uses=0) invite on the booth channel.
        """
        g = self.data.get_guild(guild.id)
        if g and g.get("invite_url"):
            return g["invite_url"]

        # Lazy creation
        channel = guild.get_channel(int(booth_channel_id))
        if not channel or not isinstance(channel, discord.TextChannel):
            return None
        try:
            invite = await channel.create_invite(max_age=0, max_uses=0, unique=False, reason="Musubi invite system")
            await self.data.set_guild_invite(guild.id, invite.url)
            log.info("Invite created — guild:%d url:%s", guild.id, invite.url)
            return invite.url
        except discord.Forbidden:
            log.warning("Missing Create Invite permission — guild:%d", guild.id)
            return None
        except discord.HTTPException as e:
            log.error("create_invite failed — guild:%d: %s", guild.id, e)
            return None

    # ── /invite ───────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="invite",
        aliases=["inv", "join"],
        description="Send your server's invite link to the other side of the current call.",
    )
    async def invite(self, ctx: commands.Context[MusubiBot]) -> None:
        """Send your server's permanent invite to the server you're on a call with."""
        await self._send_invite(ctx)

    async def _send_invite(self, ctx: commands.Context[MusubiBot]) -> None:
        assert ctx.guild is not None

        # ── Guild checks ──────────────────────────────────────────────────
        if not self.data.is_guild_registered(ctx.guild.id):
            await ctx.send(
                embed=Embeds.error("This server isn't set up yet. Run `/setup` first."),
                ephemeral=True,
            )
            return

        if self.data.is_guild_banned(ctx.guild.id):
            await ctx.send(
                embed=Embeds.error("This server has been banned from the Musubi network."),
                ephemeral=True,
            )
            return

        g = self.data.get_guild(ctx.guild.id)
        if not g or str(ctx.channel.id) != g["booth_channel"]:
            booth = g["booth_channel"] if g else None
            ref   = f"<#{booth}>" if booth else "your booth channel"
            await ctx.send(
                embed=Embeds.error(f"Use `/invite` from {ref}."),
                ephemeral=True,
            )
            return

        # ── Must be in an active call ─────────────────────────────────────
        session = await self.data.get_active_session(ctx.guild.id)
        if not session:
            await ctx.send(
                embed=Embeds.error("You can only send invites during an active call."),
                ephemeral=True,
            )
            return

        # ── Premium cooldown (per-user) ───────────────────────────────────
        is_premium_user  = await self.data.is_premium_user(ctx.author.id)
        is_premium_guild = await self.data.is_premium_guild(ctx.guild.id)
        is_premium       = is_premium_user or is_premium_guild

        if is_premium:
            now  = time.monotonic()
            last = self._last_invite.get(ctx.author.id, 0.0)
            remaining = INVITE_COOLDOWN_PREMIUM - (now - last)
            if remaining > 0:
                await ctx.send(
                    embed=Embeds.error(f"Cooldown — wait `{remaining:.1f}s` before sending another invite."),
                    ephemeral=True,
                )
                return

        # ── Daily quota check (per-guild) ─────────────────────────────────
        used, total, _bank = await self.data.get_invite_allowance(ctx.guild.id)
        if used >= total:
            await ctx.send(
                embed=Embeds.error(
                    f"This server's daily invite quota is full (`{used}/{total}`).\n"
                    "Admins can buy more with `/invite buy`, or wait until midnight UTC."
                ),
                ephemeral=True,
            )
            return

        # ── Get / create this guild's invite ──────────────────────────────
        invite_url = await self._ensure_invite(ctx.guild, g["booth_channel"])
        if not invite_url:
            await ctx.send(
                embed=Embeds.error(
                    "I couldn't create an invite for this server. "
                    "Make sure I have the **Create Invite** permission in the booth channel."
                ),
                ephemeral=True,
            )
            return

        # ── Send to target channel ────────────────────────────────────────
        gid = str(ctx.guild.id)
        target_channel_id = (
            session["target_channel"]
            if session["caller_guild"] == gid
            else session["caller_channel"]
        )

        target_channel = self.bot.get_channel(int(target_channel_id))
        if not target_channel or not isinstance(target_channel, discord.TextChannel):
            await ctx.send(
                embed=Embeds.error("Couldn't reach the other server right now."),
                ephemeral=True,
            )
            return

        try:
            await target_channel.send(
                embed=Embeds.invite_sent(ctx.guild.name, invite_url, used + 1, total)
            )
        except discord.Forbidden:
            await ctx.send(
                embed=Embeds.error("Couldn't send to the other server — they may have restricted the channel."),
                ephemeral=True,
            )
            return

        # ── Commit usage ──────────────────────────────────────────────────
        await self.data.increment_invite_usage(ctx.guild.id)
        if is_premium:
            self._last_invite[ctx.author.id] = time.monotonic()

        await ctx.send(
            embed=Embeds.invite_confirm(ctx.guild.name, used + 1, total),
            ephemeral=True,
        )
        log.info(
            "Invite sent — guild:%d session:%s user:%d quota:%d/%d",
            ctx.guild.id, session["id"], ctx.author.id, used + 1, total,
        )

    # ── /invite status ────────────────────────────────────────────────────

    @commands.hybrid_command(name="invitestatus", aliases=["invstatus"], description="Check this server's invite quota and XP balance.")
    async def invite_status(self, ctx: commands.Context[MusubiBot]) -> None:
        """View today's invite usage, purchased quota, and available XP tiers."""
        assert ctx.guild is not None

        if not self.data.is_guild_registered(ctx.guild.id):
            await ctx.send(
                embed=Embeds.error("This server isn't set up yet. Run `/setup` first."),
                ephemeral=True,
            )
            return

        g            = self.data.get_guild(ctx.guild.id)
        is_premium   = await self.data.is_premium_guild(ctx.guild.id)
        used, total, bank = await self.data.get_invite_allowance(ctx.guild.id)
        xp           = (g.get("xp") or 0) if g else 0

        await ctx.send(
            embed=Embeds.invite_status(used, total, bank, is_premium, xp),
            ephemeral=True,
        )

    # ── /invite buy ───────────────────────────────────────────────────────

    @commands.hybrid_command(name="invitebuy", aliases=["invbuy"], description="Spend server XP to buy extra invite quota. (Manage Messages required)")
    async def invite_buy(
        self,
        ctx: commands.Context[MusubiBot],
        amount: int,
    ) -> None:
        """
        Buy extra daily invite quota by spending server XP.

        Parameters
        ----------
        amount: int
            Invites to purchase: 5 (150 XP), 10 (200 XP), or 20 (350 XP).
        """
        assert ctx.guild is not None

        # Permission — Manage Messages
        if not isinstance(ctx.author, discord.Member) or not _is_manager(ctx.author):
            await ctx.send(
                embed=Embeds.error("You need the **Manage Messages** permission to buy invite quota."),
                ephemeral=True,
            )
            return

        if not self.data.is_guild_registered(ctx.guild.id):
            await ctx.send(
                embed=Embeds.error("This server isn't set up yet. Run `/setup` first."),
                ephemeral=True,
            )
            return

        # Validate tier
        tier = next(((amt, cost) for amt, cost in XP_TIERS if amt == amount), None)
        if not tier:
            valid = ", ".join(f"`{a}`" for a, _ in XP_TIERS)
            await ctx.send(
                embed=Embeds.error(
                    f"Invalid amount. Choose from {valid} invites.\n"
                    "`5` → 150 XP  •  `10` → 200 XP  •  `20` → 350 XP"
                ),
                ephemeral=True,
            )
            return

        inv_amount, xp_cost = tier
        g = self.data.get_guild(ctx.guild.id)
        current_xp = (g.get("xp") or 0) if g else 0

        if current_xp < xp_cost:
            await ctx.send(
                embed=Embeds.error(
                    f"Not enough XP. This server has `{current_xp:,} XP` but "
                    f"`{xp_cost} XP` is needed for **+{inv_amount} invites**.\n"
                    "Earn more XP by staying active on calls!"
                ),
                ephemeral=True,
            )
            return

        ok = await self.data.add_invite_quota(ctx.guild.id, inv_amount, xp_cost)
        if not ok:
            await ctx.send(
                embed=Embeds.error("Purchase failed. Please try again."),
                ephemeral=True,
            )
            return

        g          = self.data.get_guild(ctx.guild.id)  # refreshed
        new_xp     = (g.get("xp") or 0) if g else current_xp - xp_cost
        new_bank   = (g.get("invite_quota") or 0) if g else 0

        log.info(
            "Invite quota purchased — guild:%d amount:%d xp_cost:%d by:%d",
            ctx.guild.id, inv_amount, xp_cost, ctx.author.id,
        )
        await ctx.send(
            embed=Embeds.invite_bought(inv_amount, xp_cost, new_bank, new_xp),
            ephemeral=True,
        )


async def setup(bot: MusubiBot) -> None:
    await bot.add_cog(InviteCog(bot))