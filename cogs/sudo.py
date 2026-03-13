"""
Project MUSUBI — cogs/sudo.py
Bot administration commands. All commands live under /sudo.

Structure:
  /sudo list                          — list sudo users
  /sudo add/remove <user>             — manage sudo users (owner only)
  /sudo ban user <user>               — ban a user from the network
  /sudo ban unban <user>              — unban a user
  /sudo ban guild [guild_id]          — ban a guild from the network
  /sudo ban unguild [guild_id]        — unban a guild
  /sudo grant user <user> [days]      — grant user premium directly
  /sudo grant guild [days]            — grant guild premium to this server
  /sudo key gen <type> [days]         — generate a redeemable key
  /sudo key list                      — list unredeemed keys
  /sudo key revoke <key>              — revoke a key
  /sudo session list                  — list active calls
  /sudo session terminate <id>        — force-end a session
  /sudo session broadcast <msg>       — message all active booth channels
  /sudo reload cog <name>             — reload a specific cog (owner only)
  /sudo reload all                    — reload all cogs (owner only)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from datamanager import DataManager
from embeds import Embeds
from botprotocol import MusubiBot

log = logging.getLogger("musubi.sudo")


def is_sudo():
    async def predicate(ctx: commands.Context[MusubiBot]) -> bool:
        data: DataManager = ctx.bot.data
        if not data.is_sudo(ctx.author.id):
            await ctx.send(
                embed=Embeds.error("You don't have permission to use this command."),
                ephemeral=True,
            )
            return False
        return True
    return commands.check(predicate)


def is_owner():
    async def predicate(ctx: commands.Context[MusubiBot]) -> bool:
        owner_id = int(os.getenv("OWNER_ID", "0"))
        if ctx.author.id != owner_id:
            await ctx.send(embed=Embeds.error("This command is restricted to the bot owner."), ephemeral=True)
            return False
        return True
    return commands.check(predicate)


class Sudo(commands.Cog):

    def __init__(self, bot: MusubiBot) -> None:
        self.bot  = bot
        self.data: DataManager = bot.data

    # ── /sudo (root) ──────────────────────────────────────────────────────

    @commands.hybrid_group(name="sudo", description="Bot administration.")
    @discord.app_commands.default_permissions(administrator=True)
    @is_sudo()
    async def sudo(self, ctx: commands.Context[MusubiBot]) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(
                embed=Embeds.info(
                    "**Groups:** `ban`, `grant`, `key`, `session`, `reload`\n"
                    "**Commands:** `list`, `add`, `remove`"
                ),
                ephemeral=True,
            )

    # ── /sudo list/add/remove ─────────────────────────────────────────────

    @sudo.command(name="list", description="List all users with sudo privileges.")
    @is_sudo()
    async def sudo_list(self, ctx: commands.Context[MusubiBot]) -> None:
        """List all users currently granted sudo privileges."""
        if not self.data.sudo:
            await ctx.send(embed=Embeds.info("No sudo users are currently configured."), ephemeral=True)
            return
        lines = "\n".join(f"> <@{uid}>" for uid in self.data.sudo)
        await ctx.send(embed=Embeds.sudo_list(lines), ephemeral=True)

    @sudo.command(name="add", description="Grant sudo privileges to a user. (Owner only)")
    @is_owner()
    async def sudo_add(
        self,
        ctx: commands.Context[MusubiBot],
        user: discord.User,
    ) -> None:
        """
        Grant sudo privileges to a user.

        Parameters
        ----------
        user: discord.User
            The user to grant sudo to.
        """
        if self.data.is_sudo(user.id):
            await ctx.send(embed=Embeds.info(f"**{user}** already has sudo privileges."), ephemeral=True)
            return
        await self.data.add_sudo(user.id, ctx.author.id)
        log.info("Sudo granted — %d by %d", user.id, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"**{user}** has been granted sudo privileges.", ctx.author),
            ephemeral=True,
        )

    @sudo.command(name="remove", description="Revoke sudo privileges from a user. (Owner only)")
    @is_owner()
    async def sudo_remove(
        self,
        ctx: commands.Context[MusubiBot],
        user: discord.User,
    ) -> None:
        """
        Revoke sudo privileges from a user.

        Parameters
        ----------
        user: discord.User
            The user to revoke sudo from.
        """
        if not self.data.is_sudo(user.id):
            await ctx.send(embed=Embeds.info(f"**{user}** does not have sudo privileges."), ephemeral=True)
            return
        await self.data.remove_sudo(user.id)
        log.info("Sudo removed — %d by %d", user.id, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"**{user}**'s sudo privileges have been revoked.", ctx.author),
            ephemeral=True,
        )

    # ── /sudo ban ─────────────────────────────────────────────────────────

    @sudo.group(name="ban", description="Ban or unban users and guilds from the network.")
    @is_sudo()
    async def ban(self, ctx: commands.Context[MusubiBot]) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(
                embed=Embeds.info(
                    "Available subcommands: `user <@user>`, `unban <@user>`, "
                    "`guild [guild_id]`, `unguild [guild_id]`"
                ),
                ephemeral=True,
            )

    @ban.command(name="user", description="Ban a user from relaying messages on the network.")
    @is_sudo()
    async def ban_user(
        self,
        ctx: commands.Context[MusubiBot],
        user: discord.User,
    ) -> None:
        """
        Ban a user from sending messages across the relay network.

        Parameters
        ----------
        user: discord.User
            The user to ban.
        """
        if self.data.is_user_banned(user.id):
            await ctx.send(embed=Embeds.info(f"**{user}** is already banned from the network."), ephemeral=True)
            return
        await self.data.ban_user(user.id)
        log.info("User banned — %d by %d", user.id, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"**{user}** has been banned from the network.", ctx.author),
            ephemeral=True,
        )

    @ban.command(name="unban", description="Unban a user from the network.")
    @is_sudo()
    async def ban_unban(
        self,
        ctx: commands.Context[MusubiBot],
        user: discord.User,
    ) -> None:
        """
        Unban a user from the relay network.

        Parameters
        ----------
        user: discord.User
            The user to unban.
        """
        if not self.data.is_user_banned(user.id):
            await ctx.send(embed=Embeds.info(f"**{user}** is not currently banned."), ephemeral=True)
            return
        await self.data.unban_user(user.id)
        log.info("User unbanned — %d by %d", user.id, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"**{user}** has been unbanned from the network.", ctx.author),
            ephemeral=True,
        )

    @ban.command(name="guild", description="Ban a server from the network by ID, or this server if no ID given.")
    @is_sudo()
    async def ban_guild(
        self,
        ctx: commands.Context[MusubiBot],
        guild_id: str | None = None,
    ) -> None:
        """
        Ban a guild from the network. Provide a guild ID or run inside the target server.

        Parameters
        ----------
        guild_id: str
            The Discord guild ID to ban. Leave blank to ban the current server.
        """
        gid = guild_id or (str(ctx.guild.id) if ctx.guild else None)
        if not gid:
            await ctx.send(
                embed=Embeds.error("Provide a guild ID or run this command inside the target server."),
                ephemeral=True,
            )
            return
        if not self.data.is_guild_registered(gid):
            await ctx.send(
                embed=Embeds.error(f"Guild `{gid}` is not registered on the network."),
                ephemeral=True,
            )
            return
        if self.data.is_guild_banned(gid):
            await ctx.send(embed=Embeds.info(f"Guild `{gid}` is already banned."), ephemeral=True)
            return
        await self.data.ban_guild(gid, banned=True)
        log.info("Guild banned — %s by %d", gid, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"Guild `{gid}` has been banned from the network.", ctx.author),
            ephemeral=True,
        )

    @ban.command(name="unguild", description="Unban a server from the network.")
    @is_sudo()
    async def ban_unguild(
        self,
        ctx: commands.Context[MusubiBot],
        guild_id: str | None = None,
    ) -> None:
        """
        Unban a guild from the network.

        Parameters
        ----------
        guild_id: str
            The Discord guild ID to unban. Leave blank to unban the current server.
        """
        gid = guild_id or (str(ctx.guild.id) if ctx.guild else None)
        if not gid:
            await ctx.send(
                embed=Embeds.error("Provide a guild ID or run this command inside the target server."),
                ephemeral=True,
            )
            return
        if not self.data.is_guild_banned(gid):
            await ctx.send(embed=Embeds.info(f"Guild `{gid}` is not currently banned."), ephemeral=True)
            return
        await self.data.ban_guild(gid, banned=False)
        log.info("Guild unbanned — %s by %d", gid, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"Guild `{gid}` has been unbanned from the network.", ctx.author),
            ephemeral=True,
        )

    # ── /sudo grant ───────────────────────────────────────────────────────

    @sudo.group(name="grant", description="Grant premium to a user or server.")
    @is_sudo()
    async def grant(self, ctx: commands.Context[MusubiBot]) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(
                embed=Embeds.info(
                    "`grant user <@user> [days]` — grant personal premium to a user\n"
                    "`grant guild [days]` — grant server premium to this server"
                ),
                ephemeral=True,
            )

    @grant.command(name="user", description="Grant personal premium directly to a user.")
    @is_sudo()
    async def grant_user(
        self,
        ctx: commands.Context[MusubiBot],
        user: discord.User,
        days: int = 30,
    ) -> None:
        """
        Grant personal premium directly to a user without a key.

        Parameters
        ----------
        user: discord.User
            The user to grant premium to.
        days: int
            How many days the subscription lasts (default: 30, max: 365).
        """
        if days < 1 or days > 365:
            await ctx.send(embed=Embeds.error("Days must be between 1 and 365."), ephemeral=True)
            return
        expires = datetime.now(timezone.utc) + timedelta(days=days)
        ok = await self.data.grant_premium(tier="user", expires_at=expires, user_id=user.id)
        if not ok:
            await ctx.send(embed=Embeds.error("Failed to grant premium. Please try again."), ephemeral=True)
            return
        log.info("User premium granted — user:%d days:%d by:%d", user.id, days, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(
                f"**{user}** has been granted **Personal Premium** for **{days} day{'s' if days != 1 else ''}**.",
                ctx.author,
            ),
            ephemeral=True,
        )

    @grant.command(name="guild", description="Grant server premium to this server.")
    @is_sudo()
    async def grant_guild(
        self,
        ctx: commands.Context[MusubiBot],
        days: int = 30,
    ) -> None:
        """
        Grant server premium to the current server.
        Run this command inside the target server.

        Parameters
        ----------
        days: int
            How many days the subscription lasts (default: 30, max: 365).
        """
        if not ctx.guild:
            await ctx.send(
                embed=Embeds.error("Run this command inside the server you want to grant premium to."),
                ephemeral=True,
            )
            return
        if not self.data.is_guild_registered(ctx.guild.id):
            await ctx.send(
                embed=Embeds.error("This server isn't registered on the network. Run `/setup` first."),
                ephemeral=True,
            )
            return
        if days < 1 or days > 365:
            await ctx.send(embed=Embeds.error("Days must be between 1 and 365."), ephemeral=True)
            return
        expires = datetime.now(timezone.utc) + timedelta(days=days)
        ok = await self.data.grant_premium(tier="guild", expires_at=expires, guild_id=ctx.guild.id)
        if not ok:
            await ctx.send(embed=Embeds.error("Failed to grant premium. Please try again."), ephemeral=True)
            return
        log.info("Guild premium granted — guild:%d days:%d by:%d", ctx.guild.id, days, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(
                f"**{ctx.guild.name}** has been granted **Server Premium** for **{days} day{'s' if days != 1 else ''}**.",
                ctx.author,
            ),
            ephemeral=True,
        )

    # ── /sudo key ─────────────────────────────────────────────────────────

    @sudo.group(name="key", description="Manage premium keys.")
    @is_sudo()
    async def key(self, ctx: commands.Context[MusubiBot]) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(
                embed=Embeds.info("Available subcommands: `gen <user|guild> [days]`, `list`, `revoke <key>`"),
                ephemeral=True,
            )

    @key.command(name="gen", description="Generate a redeemable premium key.")
    @is_sudo()
    async def key_gen(
        self,
        ctx: commands.Context[MusubiBot],
        key_type: str,
        days: int = 30,
    ) -> None:
        """
        Generate a redeemable premium key. The key is sent to your DMs.

        Parameters
        ----------
        key_type: str
            The type of premium: `user` (personal) or `guild` (server).
        days: int
            How many days the key grants when redeemed (default: 30, max: 365).
        """
        if key_type not in ("user", "guild"):
            await ctx.send(
                embed=Embeds.error("Key type must be `user` (personal premium) or `guild` (server premium)."),
                ephemeral=True,
            )
            return
        if days < 1 or days > 365:
            await ctx.send(embed=Embeds.error("Days must be between 1 and 365."), ephemeral=True)
            return

        key = await self.data.create_key(key_type, days, ctx.author.id)
        log.info("Key generated — type:%s days:%d key:%s by:%d", key_type, days, key, ctx.author.id)

        try:
            await ctx.author.send(embed=Embeds.premium_key(key, key_type))
        except discord.Forbidden:
            pass

        label = "Personal" if key_type == "user" else "Server"
        await ctx.send(
            embed=Embeds.action(
                f"**{label} Premium** key generated for **{days} day{'s' if days != 1 else ''}** — sent to your DMs.",
                ctx.author,
            ),
            ephemeral=True,
        )

    @key.command(name="list", description="List all unredeemed premium keys.")
    @is_sudo()
    async def key_list(self, ctx: commands.Context[MusubiBot]) -> None:
        """Show all premium keys that have not yet been redeemed."""
        rows = await self.data.get_unused_keys()
        if not rows:
            await ctx.send(embed=Embeds.info("There are no unredeemed keys."), ephemeral=True)
            return

        lines = []
        for r in rows[:20]:
            try:
                ts = int(discord.utils.parse_time(r["created_at"]).timestamp())
                time_str = f"<t:{ts}:R>"
            except Exception:
                time_str = r["created_at"][:10]

            label = "Personal" if r["type"] == "user" else "Server"
            lines.append(f"> `{r['key']}` — `{label}` · **{r['days']}d** · {time_str}")

        embed = discord.Embed(
            description=f"> `🗝️` *Unredeemed Keys ({len(rows)})*\n\n" + "\n".join(lines),
            color=0xC084FC,
        )
        if len(rows) > 20:
            embed.set_footer(text=f"Showing 20 of {len(rows)}")
        await ctx.send(embed=embed, ephemeral=True)

    @key.command(name="revoke", description="Revoke an unredeemed premium key.")
    @is_sudo()
    async def key_revoke(
        self,
        ctx: commands.Context[MusubiBot],
        key: str,
    ) -> None:
        """
        Permanently revoke a key so it can no longer be redeemed.

        Parameters
        ----------
        key: str
            The key to revoke (format: MSBY-XXXX-XXXX-XXXX).
        """
        ok = await self.data.revoke_key(key)
        if ok:
            await ctx.send(
                embed=Embeds.action(f"Key `{key.upper()}` has been revoked.", ctx.author),
                ephemeral=True,
            )
        else:
            await ctx.send(embed=Embeds.error("Key not found or could not be revoked."), ephemeral=True)

    # ── /sudo session ─────────────────────────────────────────────────────

    @sudo.group(name="session", description="View and manage active calls.")
    @is_sudo()
    async def session(self, ctx: commands.Context[MusubiBot]) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(
                embed=Embeds.info("Available subcommands: `list`, `terminate <id>`, `broadcast <message>`"),
                ephemeral=True,
            )

    @session.command(name="list", description="List all active calls with details.")
    @is_sudo()
    async def session_list(self, ctx: commands.Context[MusubiBot]) -> None:
        """View all active calls across the network with timestamps."""
        try:
            rows = await self.data._get("Sessions", {
                "select": "id,caller_guild,target_guild,last_activity",
                "status": "eq.active",
            })
        except Exception as e:
            await ctx.send(embed=Embeds.error(f"Failed to fetch sessions: {e}"), ephemeral=True)
            return

        count = len(rows)
        if count == 0:
            await ctx.send(embed=Embeds.info("There are no active calls right now."), ephemeral=True)
            return

        lines = "\n\n".join(
            f"> `{s['id'][:8]}` — `{s['caller_guild']}` ↔ `{s.get('target_guild', '—')}`\n"
            f"> *Last activity: `{s.get('last_activity', '—')[:19].replace('T', ' ')} UTC`*"
            for s in rows[:10]
        )
        await ctx.send(embed=Embeds.session_active(count, lines, count), ephemeral=True)

    @session.command(name="terminate", description="Force-end an active call by session ID.")
    @is_sudo()
    async def session_terminate(
        self,
        ctx: commands.Context[MusubiBot],
        session_id: str,
    ) -> None:
        """
        Force-terminate an active call and notify both servers.

        Parameters
        ----------
        session_id: str
            The session ID or first 8 characters shown in /sudo session list.
        """
        try:
            rows = await self.data._get("Sessions", {"select": "*", "status": "eq.active"})
        except Exception as e:
            await ctx.send(embed=Embeds.error(f"Failed to fetch sessions: {e}"), ephemeral=True)
            return

        match = next((s for s in rows if s["id"].startswith(session_id)), None)
        if not match:
            await ctx.send(
                embed=Embeds.error(f"No active session matching `{session_id}` was found."),
                ephemeral=True,
            )
            return

        # Notify both sides before ending
        from cogs.phone import Phone
        from typing import cast
        phone_cog = cast(Phone, self.bot.cogs.get("Phone"))
        if phone_cog:
            await phone_cog._notify_end(match, reason="terminate")

        # End the single session row — this is the correct approach given the schema
        await self.data.end_session(match["id"])

        if phone_cog:
            await phone_cog._update_status()

        log.info("Session terminated — %s by %d", match["id"], ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"Session `{match['id'][:8]}` has been terminated.", ctx.author),
            ephemeral=True,
        )

    @session.command(name="broadcast", description="Send a message to all active booth channels.")
    @is_sudo()
    async def session_broadcast(
        self,
        ctx: commands.Context[MusubiBot],
        *,
        message: str,
    ) -> None:
        """
        Broadcast a system notice to every active booth channel on the network.

        Parameters
        ----------
        message: str
            The message to broadcast.
        """
        try:
            rows = await self.data._get("Sessions", {"select": "*", "status": "eq.active"})
        except Exception as e:
            await ctx.send(embed=Embeds.error(str(e)), ephemeral=True)
            return

        if not rows:
            await ctx.send(embed=Embeds.info("There are no active calls to broadcast to."), ephemeral=True)
            return

        sent: set[str] = set()
        for s in rows:
            for channel_id in [s.get("caller_channel"), s.get("target_channel")]:
                if channel_id and channel_id not in sent:
                    ch = self.bot.get_channel(int(channel_id))
                    if ch and isinstance(ch, discord.TextChannel):
                        try:
                            await ch.send(embed=Embeds.info(f"📢 **Network Notice:** {message}"))
                        except discord.Forbidden:
                            pass
                    sent.add(channel_id)

        await ctx.send(
            embed=Embeds.action(f"Broadcast sent to {len(sent)} channel{'s' if len(sent) != 1 else ''}.", ctx.author),
            ephemeral=True,
        )

    # ── /sudo reload ──────────────────────────────────────────────────────

    @sudo.group(name="reload", description="Reload bot extensions. (Owner only)")
    @is_owner()
    async def reload(self, ctx: commands.Context[MusubiBot]) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(
                embed=Embeds.info("Available subcommands: `cog <name>`, `all`"),
                ephemeral=True,
            )

    @reload.command(name="cog", description="Reload a specific cog by name.")
    @is_owner()
    async def reload_cog(
        self,
        ctx: commands.Context[MusubiBot],
        name: str,
    ) -> None:
        """
        Reload a specific cog by name.

        Parameters
        ----------
        name: str
            The cog name, e.g. phone, bridge, sudo, filter.
        """
        ext = f"cogs.{name}" if not name.startswith("cogs.") else name
        try:
            await self.bot.reload_extension(ext)
            log.info("Reloaded %s by %d", ext, ctx.author.id)
            await ctx.send(
                embed=Embeds.action(f"`{ext}` reloaded successfully.", ctx.author),
                ephemeral=True,
            )
        except commands.ExtensionNotLoaded:
            await ctx.send(embed=Embeds.error(f"`{ext}` is not currently loaded."), ephemeral=True)
        except commands.ExtensionNotFound:
            await ctx.send(embed=Embeds.error(f"`{ext}` was not found."), ephemeral=True)
        except Exception as e:
            log.exception("Failed to reload %s", ext)
            await ctx.send(embed=Embeds.critical(e), ephemeral=True)

    @reload.command(name="all", description="Reload all loaded cogs.")
    @is_owner()
    async def reload_all(self, ctx: commands.Context[MusubiBot]) -> None:
        """Reload every loaded extension."""
        from main import INITIAL_EXTENSIONS
        lines = []
        for ext in INITIAL_EXTENSIONS:
            try:
                await self.bot.reload_extension(ext)
                lines.append(f"> `✅` *`{ext}` reloaded*")
            except Exception as e:
                lines.append(f"> `❌` *`{ext}` — {e}*")
        log.info("Reload all by %d", ctx.author.id)
        await ctx.send(embed=Embeds.reload_all(lines), ephemeral=True)

    # ── /sudo filter ──────────────────────────────────────────────────────

    @sudo.group(name="filter", description="Manage the global network-wide message blocklist.")
    @is_sudo()
    async def filter_group(self, ctx: commands.Context[commands.Bot]) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(
                embed=Embeds.info("Available subcommands: `add`, `remove`, `list`, `clear`"),
                ephemeral=True,
            )

    @filter_group.command(name="add", description="Add phrases to the global blocklist.")
    @is_sudo()
    async def filter_add(
        self,
        ctx: commands.Context[commands.Bot],
        *,
        phrase: str,
    ) -> None:
        """
        Add one or more comma-separated words or phrases to the global network blocklist.

        Parameters
        ----------
        phrase: str
            Comma-separated words or phrases to block (e.g. badword, slur, spam phrase).
        """
        entries = [p.strip().lower() for p in phrase.split(",") if p.strip()]
        if not entries:
            await ctx.send(embed=Embeds.error("No valid phrases provided."), ephemeral=True)
            return
        for entry in entries:
            await self.data.blocklist_add(entry)
        added = ", ".join(f"`{e}`" for e in entries)
        log.info("Filter:add — %s by %d", entries, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"Added to global blocklist: {added}", ctx.author),
            ephemeral=True,
        )

    @filter_group.command(name="remove", description="Remove a phrase from the global blocklist.")
    @is_sudo()
    async def filter_remove(
        self,
        ctx: commands.Context[commands.Bot],
        *,
        phrase: str,
    ) -> None:
        """
        Remove a word or phrase from the global network blocklist.

        Parameters
        ----------
        phrase: str
            The exact word or phrase to remove.
        """
        phrase = phrase.lower().strip()
        if phrase not in self.data.blocklist:
            await ctx.send(embed=Embeds.error(f"`{phrase}` is not in the global blocklist."), ephemeral=True)
            return
        await self.data.blocklist_remove(phrase)
        log.info("Filter:remove — '%s' by %d", phrase, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"`{phrase}` removed from the global blocklist.", ctx.author),
            ephemeral=True,
        )

    @filter_group.command(name="list", description="List all entries in the global blocklist.")
    @is_sudo()
    async def filter_list(self, ctx: commands.Context[commands.Bot]) -> None:
        """Show every phrase currently on the global network-wide blocklist."""
        if not self.data.blocklist:
            await ctx.send(embed=Embeds.info("The global blocklist is empty."), ephemeral=True)
            return
        entries = "\n".join(f"> `{w}`" for w in sorted(self.data.blocklist))
        await ctx.send(embed=Embeds.blocklist(entries, len(self.data.blocklist)), ephemeral=True)

    @filter_group.command(name="clear", description="Clear the entire global blocklist.")
    @is_sudo()
    async def filter_clear(self, ctx: commands.Context[commands.Bot]) -> None:
        """Remove all entries from the global network-wide blocklist at once."""
        count = len(self.data.blocklist)
        if count == 0:
            await ctx.send(embed=Embeds.info("The global blocklist is already empty."), ephemeral=True)
            return
        await self.data.blocklist_clear()
        log.info("Filter:clear — %d entries removed by %d", count, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(
                f"Global blocklist cleared — {count} entr{'y' if count == 1 else 'ies'} removed.",
                ctx.author,
            ),
            ephemeral=True,
        )


async def setup(bot: MusubiBot) -> None:
    await bot.add_cog(Sudo(bot))