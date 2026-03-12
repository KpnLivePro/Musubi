"""
Project MUSUBI — cogs/help.py
Clean minimal help system.

/help          — main help embed
/help {cmd}    — individual command info
/help cmds     — all public commands grouped by category
"""

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from datamanager import DataManager
from botprotocol import MusubiBot
from embeds import Embeds

log = logging.getLogger("musubi.help")

SUPPORT_SERVER = "https://discord.gg/ZMEq3QbSCY"
MADE_BY        = "†spector"
BRAND_COLOR    = 0xC084FC

# ── Command registry ──────────────────────────────────────────────────────────
# (name, description, syntax, category)
# category: phone | profile | server | premium | leaderboard | filter

COMMANDS: list[tuple[str, str, str | None, str]] = [
    # Phone
    ("call",                "Search for another server to connect with.",               "/call",                          "phone"),
    ("hangup",              "End your current call or cancel a search.",                "/hangup",                        "phone"),
    ("anonymous",           "Toggle anonymous mode — hide your name and avatar.",       "/anonymous",                     "phone"),
    ("friendme",            "Send your Discord tag to the other server.",               "/friendme",                      "phone"),
    # Profile
    ("me status",           "View your personal profile and settings.",                 "/me status",                     "profile"),
    ("me name",             "Set a custom display name during calls. ✨ Premium",       "/me name <nickname>",            "profile"),
    ("me avatar",           "Set a custom avatar for calls. ✨ Premium",                "/me avatar <url>",               "profile"),
    ("me reset",            "Reset your name and avatar to Discord defaults.",          "/me reset",                      "profile"),
    # Server
    ("setup",               "Register this server and set a booth channel.",            "/setup <#channel>",              "server"),
    ("setbooth",            "Change the booth channel.",                                "/setbooth <#channel>",           "server"),
    ("unregister",          "Remove this server from Musubi.",                          "/unregister [confirm:True]",     "server"),
    ("prefix server",       "Set a custom command prefix for this server.",             "/prefix server <prefix>",        "server"),
    # Booth Filter
    ("boothfilter add",     "Block words or phrases from entering your booth.",         "/boothfilter add <phrase>",      "filter"),
    ("boothfilter remove",  "Remove a phrase from your booth filter.",                  "/boothfilter remove <phrase>",   "filter"),
    ("boothfilter list",    "Show all phrases blocked in your booth.",                  "/boothfilter list",              "filter"),
    ("boothfilter clear",   "Clear your entire booth filter.",                          "/boothfilter clear",             "filter"),
    # Premium
    ("prefix self",         "Set a personal command prefix. ✨ User Premium",           "/prefix self <prefix>",          "premium"),
    ("premium status",      "Check active premium for yourself and this server.",       "/premium status",                "premium"),
    ("redeem",              "Redeem a premium key for yourself or this server.",        "/redeem <key>",                  "premium"),
    # Callboard
    ("callboard",           "View the current monthly call activity leaderboard.",      "/callboard",                     "leaderboard"),
]

CATEGORY_LABELS = {
    "phone":       "📞 Phone",
    "profile":     "👤 Profile",
    "server":      "⚙️ Server",
    "filter":      "🚫 Booth Filter",
    "premium":     "✨ Premium",
    "leaderboard": "🏆 Callboard",
}


def _make_main_embed(bot_avatar: str | None = None) -> discord.Embed:
    embed = discord.Embed(title="Musubi Help", color=BRAND_COLOR)
    embed.set_author(name="Need help? We've got you covered.")
    if bot_avatar:
        embed.set_thumbnail(url=bot_avatar)
    embed.description = (
        "> `⚙️` *New here? Start with these two commands:*\n"
        "```\n@Musubi setup   — register your server and set a booth channel\n"
        "@Musubi call    — dial into the network and connect with another server\n```\n"
        "> *Use `/help cmds` to see all available commands*\n"
        "> *Use `/help <command>` for details on a specific command*\n"
        "> *Questions? Join our support server:*\n"
        f"> {SUPPORT_SERVER}"
    )
    embed.set_footer(text=f"❣️ Made by {MADE_BY}")
    return embed


def _make_cmd_embed(name: str, description: str, syntax: str | None, bot_avatar: str | None = None) -> discord.Embed:
    embed = discord.Embed(title="Musubi Help", color=BRAND_COLOR)
    if bot_avatar:
        embed.set_thumbnail(url=bot_avatar)
    desc = f"> `/{name}` — *{description}*"
    if syntax:
        desc += f"\n```\nSyntax: {syntax}\n```"
    embed.description = desc
    embed.set_footer(text=f"❣️ Made by {MADE_BY}")
    return embed


def _make_cmds_embed(bot_avatar: str | None = None) -> discord.Embed:
    embed = discord.Embed(title="Musubi — All Commands", color=BRAND_COLOR)
    if bot_avatar:
        embed.set_thumbnail(url=bot_avatar)

    # Group commands by category in defined order
    grouped: dict[str, list[tuple[str, str, str | None, str]]] = {k: [] for k in CATEGORY_LABELS}
    for entry in COMMANDS:
        cat = entry[3]
        if cat in grouped:
            grouped[cat].append(entry)

    for cat_key, label in CATEGORY_LABELS.items():
        entries = grouped[cat_key]
        if entries:
            lines = "\n".join(f"> `/{n}` — *{d}*" for n, d, _, _ in entries)
            embed.add_field(name=label, value=lines, inline=False)

    embed.set_footer(text=f"❣️ Made by {MADE_BY}  •  /help <command> for details")
    return embed


class Help(commands.Cog):

    def __init__(self, bot: MusubiBot) -> None:
        self.bot  = bot
        self.data: DataManager = bot.data

    @commands.hybrid_command(name="help", description="Get help with Musubi commands.")
    async def help(
        self,
        ctx: commands.Context[MusubiBot],
        cmd: str | None = None,
    ) -> None:
        """
        Get help with Musubi.

        Parameters
        ----------
        cmd: str
            A command name for detailed help, or 'cmds' to list all commands.
        """
        avatar = str(self.bot.user.display_avatar.url) if self.bot.user else None

        if cmd is None:
            await ctx.send(embed=_make_main_embed(avatar), ephemeral=True)
            return

        if cmd.lower() == "cmds":
            await ctx.send(embed=_make_cmds_embed(avatar), ephemeral=True)
            return

        query = cmd.lower().strip()
        match = next(
            ((n, d, s) for n, d, s, _ in COMMANDS if n.lower() == query),
            None,
        )

        if not match:
            await ctx.send(
                embed=Embeds.error(
                    f"No command called `{query}` found.\n"
                    "Use `/help cmds` to see all available commands."
                ),
                ephemeral=True,
            )
            return

        await ctx.send(embed=_make_cmd_embed(*match, bot_avatar=avatar), ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Reply with prefix info when the bot is @mentioned with no other content."""
        if message.author.bot:
            return
        assert self.bot.user is not None
        pattern = rf"^<@!?{self.bot.user.id}>\s*$"
        if not re.match(pattern, message.content.strip()):
            return

        from main import DEFAULT_PREFIX

        if message.guild:
            g = self.data.get_guild(message.guild.id)
            guild_prefix = f"`{g['prefix']}`" if g and g.get("prefix") else f"`{DEFAULT_PREFIX}`"
            guild_name   = message.guild.name
        else:
            guild_prefix = f"`{DEFAULT_PREFIX}`"
            guild_name   = "DM"

        u           = self.data.get_user(message.author.id)
        user_prefix = f"`{u['prefix']}`" if u.get("prefix") else "`Not set`"

        embed = discord.Embed(
            description=(
                f"> `✨` *Prefix for **{guild_name}**: {guild_prefix}*\n"
                f"> `✨` *Your personal prefix: {user_prefix}*"
            ),
            color=BRAND_COLOR,
        )
        await message.channel.send(embed=embed)


async def setup(bot: MusubiBot) -> None:
    await bot.add_cog(Help(bot))