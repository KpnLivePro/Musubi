"""
Project MUSUBI — cogs/help.py
Autocomplete help system.

/help                          — main overview embed
/help category:<cat>           — all commands in a category
/help command:<cmd>            — detailed info on a specific command
/help category:<cat> command:<cmd> — scoped lookup

Both parameters have live autocomplete:
  - category shows all 7 categories as the user types
  - command list is filtered by the currently selected category
    (or shows all commands if no category is chosen)
"""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from datamanager import DataManager
from botprotocol import MusubiBot
from embeds import Embeds

log = logging.getLogger("musubi.help")

SUPPORT_SERVER = "https://discord.gg/GF9xN7CHfz"
MADE_BY        = "†spector"
BRAND_COLOR    = 0xC084FC

# ── Command registry ───────────────────────────────────────────────────────────
# (name, description, syntax, category)

COMMANDS: list[tuple[str, str, str | None, str]] = [
    # Phone — standalone action commands
    ("call",               "Search for another server to connect with.",                "m.call  (alias: m.c)",                        "phone"),
    ("hangup",             "End your current call or cancel a search.",                 "m.hangup  (alias: m.h)",                      "phone"),
    ("anonymous",          "Toggle anonymous mode — hide your name and avatar.",        "m.anonymous  (alias: m.a)",                   "phone"),
    ("friendme",           "Send your Discord tag to the other server.",                "m.friendme  (alias: m.f)",                    "phone"),
    # Invite — standalone + helpers
    ("invite",             "Send your server's invite link to the other side.",         "m.invite  (aliases: m.inv  m.join)",          "invite"),
    ("invitestatus",       "Check this server's daily invite quota and XP balance.",    "/invitestatus  (alias: /invstatus)",           "invite"),
    ("invitebuy",          "Buy extra invite quota with server XP.",                    "/invitebuy <5|10|20>  (alias: /invbuy)",       "invite"),
    # Profile — /me group
    ("me status",          "View your personal profile and settings.",                  "/me status",                                  "profile"),
    ("me name",            "Set a custom display name during calls. ✨ Premium",        "/me name <nickname>",                         "profile"),
    ("me avatar",          "Set a custom avatar for calls. ✨ Premium",                 "/me avatar <url>",                            "profile"),
    ("me reset",           "Reset your name and avatar to Discord defaults.",           "/me reset",                                   "profile"),
    # Server
    ("setup",              "Register this server and set a booth channel.",             "/setup <#channel>",                           "server"),
    ("setbooth",           "Change the booth channel.",                                 "/setbooth <#channel>",                        "server"),
    ("unregister",         "Remove this server from Musubi.",                           "/unregister [confirm:True]",                  "server"),
    ("prefix server",      "Set a custom command prefix for this server.",              "/prefix server <prefix>",                     "server"),
    # Booth Filter — /boothfilter group
    ("boothfilter add",    "Block words or phrases from entering your booth.",          "/boothfilter add <phrase>",                   "filter"),
    ("boothfilter remove", "Remove a phrase from your booth filter.",                   "/boothfilter remove <phrase>",                "filter"),
    ("boothfilter list",   "Show all phrases blocked in your booth.",                   "/boothfilter list",                           "filter"),
    ("boothfilter clear",  "Clear your entire booth filter.",                           "/boothfilter clear",                          "filter"),
    # Premium
    ("prefix self",        "Set a personal command prefix. ✨ User Premium",            "/prefix self <prefix>",                       "premium"),
    ("premium status",     "Check active premium for yourself and this server.",        "/premium status",                             "premium"),
    ("redeem",             "Redeem a premium key for yourself or this server.",         "/redeem <key>",                               "premium"),
    # Callboard
    ("callboard",          "View the current monthly call activity leaderboard.",       "m.callboard  (aliases: m.cb  m.lb)",          "leaderboard"),
]

# Groups shown in the [groups] dropdown.
# Each entry: (group_name, description, category_key)
GROUPS: list[tuple[str, str, str]] = [
    ("me",          "Personal identity settings — name, avatar, anonymous mode, premium status.", "profile"),
    ("prefix",      "Set a server prefix or personal prefix (premium).",                           "server"),
    ("boothfilter", "Block specific words from entering your server's booth.",                     "filter"),
    ("premium",     "Check premium status and redeem keys.",                                       "premium"),
]

CATEGORY_LABELS: dict[str, str] = {
    "phone":       "📞 Phone",
    "invite":      "📬 Invites",
    "profile":     "👤 Profile",
    "server":      "⚙️ Server",
    "filter":      "🚫 Booth Filter",
    "premium":     "✨ Premium",
    "leaderboard": "🏆 Callboard",
}

# Pre-built lookup tables (module-level, built once)
_CMD_BY_NAME:  dict[str, tuple[str, str, str | None, str]] = {c[0]: c for c in COMMANDS}
_GROUP_BY_NAME: dict[str, tuple[str, str, str]]             = {g[0]: g for g in GROUPS}
_CMDS_BY_CAT:  dict[str, list[tuple[str, str, str | None, str]]] = {k: [] for k in CATEGORY_LABELS}
for _c in COMMANDS:
    if _c[3] in _CMDS_BY_CAT:
        _CMDS_BY_CAT[_c[3]].append(_c)


# ── Embed builders ─────────────────────────────────────────────────────────────

def _make_main_embed(bot_avatar: str | None = None) -> discord.Embed:
    embed = discord.Embed(title="Musubi Help", color=BRAND_COLOR)
    embed.set_author(name="Need help? We've got you covered.")
    if bot_avatar:
        embed.set_thumbnail(url=bot_avatar)
    cat_list  = "  ".join(f"`{label}`" for label in CATEGORY_LABELS.values())
    group_list = "  ".join(f"`/{g[0]}`" for g in GROUPS)
    embed.description = (
        "> *Use the **[commands]** dropdown to look up any command.*\n"
        "> *Use the **[groups]** dropdown to browse a command group.*\n\n"
        f"> **Categories:** {cat_list}\n\n"
        f"> **Groups:** {group_list}\n\n"
        "> *Join our support server for help and updates:*\n"
        f"> {SUPPORT_SERVER}"
    )
    embed.set_footer(text=f"❣️ Made by {MADE_BY}  •  Default prefix: m.")
    return embed


def _make_group_embed(group_name: str, bot_avatar: str | None = None) -> discord.Embed:
    entry = _GROUP_BY_NAME.get(group_name)
    if not entry:
        return Embeds.error(f"No group called `{group_name}` found.")
    gname, gdesc, cat_key = entry
    cat_label = CATEGORY_LABELS.get(cat_key, cat_key)
    cmds = _CMDS_BY_CAT.get(cat_key, [])
    # Only show commands that start with the group name
    relevant = [(n, d, s) for n, d, s, _ in cmds if n.startswith(gname)]
    embed = discord.Embed(title=f"Musubi Help — /{gname}", color=BRAND_COLOR)
    if bot_avatar:
        embed.set_thumbnail(url=bot_avatar)
    lines = [f"> *{gdesc}*", ""]
    for n, d, s in relevant:
        lines.append(f"> `/{n}` — *{d}*")
        if s:
            lines.append(f"> `{s}`")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"❣️ Made by {MADE_BY}  •  Category: {cat_label}")
    return embed


def _make_category_embed(cat_key: str, bot_avatar: str | None = None) -> discord.Embed:
    label   = CATEGORY_LABELS.get(cat_key, cat_key)
    entries = _CMDS_BY_CAT.get(cat_key, [])
    embed   = discord.Embed(title=f"Musubi Help — {label}", color=BRAND_COLOR)
    if bot_avatar:
        embed.set_thumbnail(url=bot_avatar)
    if entries:
        lines = "\n".join(
            f"> `/{n}` — *{d}*" + (f"\n> `Syntax: {s}`" if s else "")
            for n, d, s, _ in entries
        )
        embed.description = lines
    else:
        embed.description = "> *No commands in this category.*"
    embed.set_footer(text=f"❣️ Made by {MADE_BY}  •  Use /help command:<name> for full details")
    return embed


def _make_cmd_embed(
    name: str,
    description: str,
    syntax: str | None,
    category: str,
    bot_avatar: str | None = None,
) -> discord.Embed:
    cat_label = CATEGORY_LABELS.get(category, category)
    embed     = discord.Embed(title=f"Musubi Help — {cat_label}", color=BRAND_COLOR)
    if bot_avatar:
        embed.set_thumbnail(url=bot_avatar)
    desc = f"> `/{name}`\n> *{description}*"
    if syntax:
        desc += f"\n```\nSyntax: {syntax}\n```"
    embed.description = desc
    embed.set_footer(text=f"❣️ Made by {MADE_BY}")
    return embed


# ── Autocomplete callbacks ─────────────────────────────────────────────────────

async def _autocomplete_category(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=label, value=key)
        for key, label in CATEGORY_LABELS.items()
        if current.lower() in label.lower() or current.lower() in key.lower()
    ][:25]


async def _autocomplete_group(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=f"/{gname}  —  {gdesc[:55]}", value=gname)
        for gname, gdesc, _ in GROUPS
        if current.lower() in gname.lower() or current.lower() in gdesc.lower()
    ][:25]


async def _autocomplete_command(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    # Read the category the user already selected in this interaction, if any
    cat_value: str | None = None
    if interaction.data and "options" in interaction.data:
        for opt in interaction.data["options"]:  # type: ignore[index]
            if opt.get("name") == "category":
                v = opt.get("value")
                cat_value = str(v) if v is not None else None
                break

    pool = _CMDS_BY_CAT.get(cat_value, []) if cat_value else COMMANDS

    return [
        app_commands.Choice(name=f"/{n}  —  {d[:50]}", value=n)
        for n, d, _, _ in pool
        if current.lower() in n.lower() or current.lower() in d.lower()
    ][:25]


# ── Cog ───────────────────────────────────────────────────────────────────────

class Help(commands.Cog):

    def __init__(self, bot: MusubiBot) -> None:
        self.bot  = bot
        self.data: DataManager = bot.data

    @app_commands.command(
        name="help",
        description="Browse Musubi commands by category or search for a specific command.",
    )
    @app_commands.describe(
        category="Browse all commands in a category.",
        group="Browse a specific command group (e.g. /me, /boothfilter).",
        command="Get detailed help for a specific command.",
    )
    @app_commands.autocomplete(category=_autocomplete_category, group=_autocomplete_group, command=_autocomplete_command)
    async def help(
        self,
        interaction: discord.Interaction,
        category: str | None = None,
        group:    str | None = None,
        command:  str | None = None,
    ) -> None:
        avatar = str(self.bot.user.display_avatar.url) if self.bot.user else None

        # Group lookup takes priority if provided
        if group:
            await interaction.response.send_message(
                embed=_make_group_embed(group, avatar), ephemeral=True
            )
            return

        # Both category + command
        if category and command:
            entry = _CMD_BY_NAME.get(command.lower())
            if not entry or entry[3] != category:
                await interaction.response.send_message(
                    embed=_make_category_embed(category, avatar), ephemeral=True
                )
                return
            n, d, s, cat = entry
            await interaction.response.send_message(
                embed=_make_cmd_embed(n, d, s, cat, avatar), ephemeral=True
            )
            return

        # Command only
        if command:
            entry = _CMD_BY_NAME.get(command.lower())
            if not entry:
                await interaction.response.send_message(
                    embed=Embeds.error(
                        f"No command called `{command}` found.\n"
                        "Pick one from the dropdown or try a different name."
                    ),
                    ephemeral=True,
                )
                return
            n, d, s, cat = entry
            await interaction.response.send_message(
                embed=_make_cmd_embed(n, d, s, cat, avatar), ephemeral=True
            )
            return

        # Category only
        if category:
            if category not in CATEGORY_LABELS:
                await interaction.response.send_message(
                    embed=Embeds.error(
                        f"`{category}` isn't a valid category.\n"
                        "Pick one from the dropdown."
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                embed=_make_category_embed(category, avatar), ephemeral=True
            )
            return

        # Nothing — overview
        await interaction.response.send_message(
            embed=_make_main_embed(avatar), ephemeral=True
        )

    # ── @mention handler ──────────────────────────────────────────────────

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
            g            = self.data.get_guild(message.guild.id)
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