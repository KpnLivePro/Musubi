"""
Project MUSUBI — cogs/filter.py
Global message filtering before relay.
Checks: user ban, invite links, word/phrase blocklist, spam (caps, flood, repeat).
All filtering is silent — blocked messages are dropped without user notification.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque

import discord
from discord.ext import commands

from datamanager import DataManager
from botprotocol import MusubiBot
from embeds import Embeds

log = logging.getLogger("musubi.filter")

# ── Constants ─────────────────────────────────────────────────────────────────

INVITE_RE      = re.compile(r"(discord\.gg|discord\.com/invite|discordapp\.com/invite)/\S+", re.IGNORECASE)
CAPS_THRESHOLD = 0.7   # 70% uppercase triggers cap filter (min 10 alpha chars)
FLOOD_LIMIT    = 5     # max messages allowed within FLOOD_WINDOW
FLOOD_WINDOW   = 8.0   # seconds
REPEAT_LIMIT   = 3     # same message N times in a row triggers block


class FilterCog(commands.Cog, name="Filter"):

    def __init__(self, bot: MusubiBot) -> None:
        self.bot  = bot
        self.data: DataManager = bot.data

        # Per-user spam tracking
        self._flood_tracker:  defaultdict[int, deque[float]] = defaultdict(lambda: deque(maxlen=FLOOD_LIMIT))
        self._repeat_tracker: dict[int, tuple[str, int]]     = {}

    # ── Public filter method (called by bridge.py) ────────────────────────────

    def should_block(self, message: discord.Message) -> bool:
        """Returns True if the message should be silently dropped."""
        content   = message.content or ""
        author_id = message.author.id

        if self.data.is_user_banned(author_id):
            log.info("Filter:banned — user:%d", author_id)
            return True

        if self._is_invite(content):
            log.info("Filter:invite — user:%d", author_id)
            return True

        if self._is_blocklisted(content):
            log.info("Filter:blocklist — user:%d", author_id)
            return True

        if self._is_caps(content):
            log.info("Filter:caps — user:%d", author_id)
            return True

        if self._is_flood(author_id):
            log.info("Filter:flood — user:%d", author_id)
            return True

        if self._is_repeat(author_id, content):
            log.info("Filter:repeat — user:%d", author_id)
            return True

        return False

    # ── Filter checks ─────────────────────────────────────────────────────────

    def _is_invite(self, content: str) -> bool:
        return bool(INVITE_RE.search(content))

    def _is_blocklisted(self, content: str) -> bool:
        lower = content.lower()
        return any(word in lower for word in self.data.blocklist)

    def _is_caps(self, content: str) -> bool:
        letters = [c for c in content if c.isalpha()]
        if len(letters) < 10:
            return False
        return sum(1 for c in letters if c.isupper()) / len(letters) >= CAPS_THRESHOLD

    def _is_flood(self, user_id: int) -> bool:
        """
        Returns True if the user has sent FLOOD_LIMIT or more messages
        within FLOOD_WINDOW seconds. The timestamp is only appended after
        the check so the triggering message itself is correctly blocked.
        """
        now = time.monotonic()
        dq  = self._flood_tracker[user_id]

        # Check before appending so the FLOOD_LIMIT-th message is blocked,
        # not the (FLOOD_LIMIT + 1)-th
        if len(dq) >= FLOOD_LIMIT and (now - dq[0]) <= FLOOD_WINDOW:
            return True

        dq.append(now)
        return False

    def _is_repeat(self, user_id: int, content: str) -> bool:
        if not content:
            return False
        last_msg, count = self._repeat_tracker.get(user_id, ("", 0))
        if content.strip().lower() == last_msg:
            count += 1
        else:
            count = 1
        self._repeat_tracker[user_id] = (content.strip().lower(), count)
        return count >= REPEAT_LIMIT

    # ── /filter commands (sudo only) ──────────────────────────────────────────

    def _is_sudo(self, ctx: commands.Context[commands.Bot]) -> bool:
        return self.data.is_sudo(ctx.author.id)

    @commands.hybrid_group(name="filter", description="Manage the global message filter.")
    @discord.app_commands.default_permissions(administrator=True)
    async def filter_group(self, ctx: commands.Context[commands.Bot]) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(
                embed=Embeds.info("Available subcommands: `add`, `remove`, `list`, `clear`"),
                ephemeral=True,
            )

    @filter_group.command(name="add", description="Add one or more phrases to the global blocklist.")
    async def filter_add(
        self,
        ctx: commands.Context[commands.Bot],
        *,
        phrase: str,
    ) -> None:
        """
        Add one or more comma-separated words or phrases to the blocklist.

        Parameters
        ----------
        phrase: str
            Comma-separated words or phrases to block (e.g. badword, another phrase).
        """
        if not self._is_sudo(ctx):
            await ctx.send(embed=Embeds.error("You don't have permission to use this command."), ephemeral=True)
            return

        entries = [p.strip().lower() for p in phrase.split(",") if p.strip()]
        if not entries:
            await ctx.send(embed=Embeds.error("No valid phrases provided."), ephemeral=True)
            return

        for entry in entries:
            await self.data.blocklist_add(entry)

        added = ", ".join(f"`{e}`" for e in entries)
        log.info("Filter:add — %s by %d", entries, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"Added to blocklist: {added}", ctx.author),
            ephemeral=True,
        )

    @filter_group.command(name="remove", description="Remove a word or phrase from the blocklist.")
    async def filter_remove(
        self,
        ctx: commands.Context[commands.Bot],
        *,
        phrase: str,
    ) -> None:
        """
        Remove a word or phrase from the global blocklist.

        Parameters
        ----------
        phrase: str
            The exact word or phrase to remove.
        """
        if not self._is_sudo(ctx):
            await ctx.send(embed=Embeds.error("You don't have permission to use this command."), ephemeral=True)
            return

        phrase = phrase.lower().strip()
        if phrase not in self.data.blocklist:
            await ctx.send(embed=Embeds.error(f"`{phrase}` is not in the blocklist."), ephemeral=True)
            return

        await self.data.blocklist_remove(phrase)
        log.info("Filter:remove — '%s' by %d", phrase, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"`{phrase}` removed from the blocklist.", ctx.author),
            ephemeral=True,
        )

    @filter_group.command(name="list", description="Show all blocked words and phrases.")
    async def filter_list(self, ctx: commands.Context[commands.Bot]) -> None:
        """List all entries currently in the global blocklist."""
        if not self._is_sudo(ctx):
            await ctx.send(embed=Embeds.error("You don't have permission to use this command."), ephemeral=True)
            return

        if not self.data.blocklist:
            await ctx.send(embed=Embeds.info("The blocklist is empty."), ephemeral=True)
            return

        entries = "\n".join(f"> `{w}`" for w in sorted(self.data.blocklist))
        await ctx.send(embed=Embeds.blocklist(entries, len(self.data.blocklist)), ephemeral=True)

    @filter_group.command(name="clear", description="Clear the entire blocklist.")
    async def filter_clear(self, ctx: commands.Context[commands.Bot]) -> None:
        """Remove all entries from the global blocklist."""
        if not self._is_sudo(ctx):
            await ctx.send(embed=Embeds.error("You don't have permission to use this command."), ephemeral=True)
            return

        count = len(self.data.blocklist)
        if count == 0:
            await ctx.send(embed=Embeds.info("The blocklist is already empty."), ephemeral=True)
            return

        await self.data.blocklist_clear()
        log.info("Filter:clear — %d entries removed by %d", count, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(
                f"Blocklist cleared — {count} entr{'y' if count == 1 else 'ies'} removed.",
                ctx.author,
            ),
            ephemeral=True,
        )


async def setup(bot: MusubiBot) -> None:
    await bot.add_cog(FilterCog(bot))