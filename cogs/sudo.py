"""
Project MUSUBI — cogs/sudo.py
Bot administration commands. All nest under m.sudo (prefix) or /sudo (hybrid).

Structured after Denki's sudo cog — sudo users get access via cog_check,
owner-only commands are guarded separately.

Structure:
  m.sudo                              — help overview
  m.sudo list                         — list sudo users
  m.sudo add <user>                   — add sudo (owner only)
  m.sudo remove <user>                — remove sudo (owner only)

  m.ban user <@user>                  — ban a user from the network
  m.ban unban <@user>                 — unban a user
  m.ban guild [guild_id]              — ban a guild
  m.ban unguild [guild_id]            — unban a guild

  m.grant user <@user> [days]         — grant user premium
  m.grant guild [days]                — grant guild premium to this server

  m.key gen <user|guild> [days]       — generate a premium key
  m.key list                          — list unredeemed keys
  m.key revoke <key>                  — revoke a key

  m.session list                      — list active calls
  m.session terminate <id>            — force-end a session
  m.session broadcast <msg>           — broadcast to all booth channels

  m.website status                    — check live stats served to the website
  m.website ping                      — verify push connection + secret

  m.reload cog <name>                 — reload a cog (owner only)
  m.reload all                        — reload all cogs (owner only)
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import commands

from datamanager import DataManager
from embeds import Embeds
from botprotocol import MusubiBot

log = logging.getLogger("musubi.sudo")

_start_time: float = time.time()


def _fmt_uptime(seconds: int) -> str:
    days, rem  = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    return f"{days}d {hours}h {mins}m {secs}s"


# ── Sudo Cog ──────────────────────────────────────────────────────────────────

class Sudo(commands.Cog):
    """Owner/sudo-only prefix commands. All nest under m."""

    def __init__(self, bot: MusubiBot) -> None:
        self.bot  = bot
        self.data: DataManager = bot.data

    async def cog_check(self, ctx: commands.Context[Any]) -> bool:  # type: ignore[override]
        """Gate: must be sudo user (or owner). Owner is always sudo-seeded at startup."""
        if not self.data.is_sudo(ctx.author.id):
            await ctx.send(
                embed=Embeds.error("You don't have permission to use this command."),
                ephemeral=True,
            )
            return False
        return True

    def _is_owner(self, user_id: int) -> bool:
        owner = int(os.environ.get("OWNER_ID", "0"))
        return user_id == owner

    # ── m.sudo (help) ─────────────────────────────────────────────────────────

    @commands.group(name="sudo", invoke_without_command=True)
    async def sudo_help(self, ctx: commands.Context[Any]) -> None:
        embed = Embeds.info(
            "**Groups:** `ban`, `grant`, `key`, `session`, `website`, `reload`, `guilds`\n"
            "**Commands:** `list`, `add`, `remove`\n\n"
            "> *`guilds` is a top-level group — use `m.guilds list` / `m.guilds info <id>`*"
        )
        await ctx.send(embed=embed, ephemeral=True)

    # ── m.sudo list / add / remove ────────────────────────────────────────────

    @sudo_help.command(name="list")
    async def sudo_list(self, ctx: commands.Context[Any]) -> None:
        """List all users with sudo privileges."""
        if not self.data.sudo:
            await ctx.send(embed=Embeds.info("No sudo users are currently configured."), ephemeral=True)
            return
        lines = "\n".join(f"> <@{uid}>" for uid in self.data.sudo)
        await ctx.send(embed=Embeds.sudo_list(lines), ephemeral=True)

    @sudo_help.command(name="add")
    async def sudo_add(self, ctx: commands.Context[Any], user: discord.User) -> None:
        """Grant sudo to a user. Owner only."""
        if not self._is_owner(ctx.author.id):
            await ctx.send(embed=Embeds.error("This command is restricted to the bot owner."), ephemeral=True)
            return
        if self.data.is_sudo(user.id):
            await ctx.send(embed=Embeds.info(f"**{user}** already has sudo privileges."), ephemeral=True)
            return
        await self.data.add_sudo(user.id, ctx.author.id)
        log.info("Sudo granted — %d by %d", user.id, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"**{user}** has been granted sudo privileges.", ctx.author),
            ephemeral=True,
        )

    @sudo_help.command(name="remove")
    async def sudo_remove(self, ctx: commands.Context[Any], user: discord.User) -> None:
        """Revoke sudo from a user. Owner only."""
        if not self._is_owner(ctx.author.id):
            await ctx.send(embed=Embeds.error("This command is restricted to the bot owner."), ephemeral=True)
            return
        if not self.data.is_sudo(user.id):
            await ctx.send(embed=Embeds.info(f"**{user}** does not have sudo privileges."), ephemeral=True)
            return
        await self.data.remove_sudo(user.id)
        log.info("Sudo removed — %d by %d", user.id, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"**{user}**'s sudo privileges have been revoked.", ctx.author),
            ephemeral=True,
        )

    # ── m.ban ─────────────────────────────────────────────────────────────────

    @commands.group(name="ban", invoke_without_command=True)
    async def ban(self, ctx: commands.Context[Any]) -> None:
        await ctx.send(
            embed=Embeds.info(
                "Available subcommands: `user <@user>`, `unban <@user>`, "
                "`guild [guild_id]`, `unguild [guild_id]`"
            ),
            ephemeral=True,
        )

    @ban.command(name="user")
    async def ban_user(self, ctx: commands.Context[Any], user: discord.User) -> None:
        """Ban a user from the relay network."""
        if self.data.is_user_banned(user.id):
            await ctx.send(embed=Embeds.info(f"**{user}** is already banned from the network."), ephemeral=True)
            return
        await self.data.ban_user(user.id)
        log.info("User banned — %d by %d", user.id, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"**{user}** has been banned from the network.", ctx.author),
            ephemeral=True,
        )

    @ban.command(name="unban")
    async def ban_unban(self, ctx: commands.Context[Any], user: discord.User) -> None:
        """Unban a user from the relay network."""
        if not self.data.is_user_banned(user.id):
            await ctx.send(embed=Embeds.info(f"**{user}** is not currently banned."), ephemeral=True)
            return
        await self.data.unban_user(user.id)
        log.info("User unbanned — %d by %d", user.id, ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"**{user}** has been unbanned from the network.", ctx.author),
            ephemeral=True,
        )

    @ban.command(name="guild")
    async def ban_guild(self, ctx: commands.Context[Any], guild_id: str | None = None) -> None:
        """Ban a guild by ID, or the current server if no ID given."""
        gid = guild_id or (str(ctx.guild.id) if ctx.guild else None)
        if not gid:
            await ctx.send(
                embed=Embeds.error("Provide a guild ID or run this command inside the target server."),
                ephemeral=True,
            )
            return
        if not self.data.is_guild_registered(gid):
            await ctx.send(embed=Embeds.error(f"Guild `{gid}` is not registered on the network."), ephemeral=True)
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

    @ban.command(name="unguild")
    async def ban_unguild(self, ctx: commands.Context[Any], guild_id: str | None = None) -> None:
        """Unban a guild from the network."""
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

    # ── m.grant ───────────────────────────────────────────────────────────────

    @commands.group(name="grant", invoke_without_command=True)
    async def grant(self, ctx: commands.Context[Any]) -> None:
        await ctx.send(
            embed=Embeds.info(
                "`grant user <@user> [days]` — grant personal premium to a user\n"
                "`grant guild [days]` — grant server premium to this server"
            ),
            ephemeral=True,
        )

    @grant.command(name="user")
    async def grant_user(self, ctx: commands.Context[Any], user: discord.User, days: int = 30) -> None:
        """Grant personal premium directly to a user."""
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

    @grant.command(name="guild")
    async def grant_guild(self, ctx: commands.Context[Any], days: int = 30) -> None:
        """Grant server premium to the current server."""
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

    # ── m.key ─────────────────────────────────────────────────────────────────

    @commands.group(name="key", invoke_without_command=True)
    async def key(self, ctx: commands.Context[Any]) -> None:
        await ctx.send(
            embed=Embeds.info("Available subcommands: `gen <user|guild> [days]`, `list`, `revoke <key>`"),
            ephemeral=True,
        )

    @key.command(name="gen")
    async def key_gen(self, ctx: commands.Context[Any], key_type: str, days: int = 30) -> None:
        """Generate a redeemable premium key. Sent to your DMs."""
        if key_type not in ("user", "guild"):
            await ctx.send(
                embed=Embeds.error("Key type must be `user` (personal premium) or `guild` (server premium)."),
                ephemeral=True,
            )
            return
        if days < 1 or days > 365:
            await ctx.send(embed=Embeds.error("Days must be between 1 and 365."), ephemeral=True)
            return

        generated_key = await self.data.create_key(key_type, days, ctx.author.id)
        log.info("Key generated — type:%s days:%d key:%s by:%d", key_type, days, generated_key, ctx.author.id)

        try:
            await ctx.author.send(embed=Embeds.premium_key(generated_key, key_type))
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

    @key.command(name="list")
    async def key_list(self, ctx: commands.Context[Any]) -> None:
        """List all unredeemed premium keys."""
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

    @key.command(name="revoke")
    async def key_revoke(self, ctx: commands.Context[Any], key: str) -> None:
        """Permanently revoke an unredeemed key."""
        ok = await self.data.revoke_key(key)
        if ok:
            await ctx.send(
                embed=Embeds.action(f"Key `{key.upper()}` has been revoked.", ctx.author),
                ephemeral=True,
            )
        else:
            await ctx.send(embed=Embeds.error("Key not found or could not be revoked."), ephemeral=True)

    # ── m.session ─────────────────────────────────────────────────────────────

    @commands.group(name="session", invoke_without_command=True)
    async def session(self, ctx: commands.Context[Any]) -> None:
        await ctx.send(
            embed=Embeds.info("Available subcommands: `list`, `terminate <id>`, `broadcast <message>`"),
            ephemeral=True,
        )

    @session.command(name="list")
    async def session_list(self, ctx: commands.Context[Any]) -> None:
        """List all active calls across the network."""
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

    @session.command(name="terminate")
    async def session_terminate(self, ctx: commands.Context[Any], session_id: str) -> None:
        """Force-end an active call by session ID (or first 8 chars)."""
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

        from cogs.phone import Phone
        from typing import cast
        phone_cog = cast(Phone, self.bot.cogs.get("Phone"))
        if phone_cog:
            await phone_cog._notify_end(match, reason="terminate")

        await self.data.end_session(match["id"])

        if phone_cog:
            await phone_cog._update_status()

        log.info("Session terminated — %s by %d", match["id"], ctx.author.id)
        await ctx.send(
            embed=Embeds.action(f"Session `{match['id'][:8]}` has been terminated.", ctx.author),
            ephemeral=True,
        )

    @session.command(name="broadcast")
    async def session_broadcast(self, ctx: commands.Context[Any], *, message: str) -> None:
        """Send a network notice to every active booth channel."""
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
            embed=Embeds.action(
                f"Broadcast sent to {len(sent)} channel{'s' if len(sent) != 1 else ''}.",
                ctx.author,
            ),
            ephemeral=True,
        )

    # ── m.website ─────────────────────────────────────────────────────────────

    @commands.group(name="website", invoke_without_command=True)
    async def website(self, ctx: commands.Context[Any]) -> None:
        await ctx.send(
            embed=Embeds.info("Available subcommands: `status`, `ping`"),
            ephemeral=True,
        )

    @website.command(name="status")
    async def website_status(self, ctx: commands.Context[Any]) -> None:
        """Fetch live stats from the KpnWorld API and display connection health."""
        import httpx
        from datetime import datetime, timezone

        url = (os.environ.get("WEBSITE_URL") or "").rstrip("/") + "/api/musubi/stats"

        if not url.startswith("http"):
            await ctx.send(embed=Embeds.error("WEBSITE_URL is not configured on this bot."), ephemeral=True)
            return

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(url)
            latency = int((time.monotonic() - start) * 1000)

            if r.status_code != 200:
                await ctx.send(
                    embed=Embeds.error(f"API returned HTTP `{r.status_code}` — check the Flask service."),
                    ephemeral=True,
                )
                return

            data = r.json()

        except Exception as e:
            latency = int((time.monotonic() - start) * 1000)
            await ctx.send(
                embed=Embeds.error(f"Could not reach the API (`{latency}ms`):\n```{e}```"),
                ephemeral=True,
            )
            return

        # Format push age
        received_at = data.get("push_received_at")
        if received_at:
            try:
                pushed = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
                diff   = int((datetime.now(timezone.utc) - pushed).total_seconds())
                age    = f"{diff}s ago" if diff < 60 else f"{diff // 60}m ago" if diff < 3600 else f"{diff // 3600}h ago"
                pushed_str = f"`{age}`"
            except Exception:
                pushed_str = f"`{received_at[:19]}`"
        else:
            pushed_str = "`never — bot may not have pushed yet`"

        # Callboard preview
        callboard = data.get("callboard") or []
        MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣"]
        board_lines = "\n".join(
            f"> {MEDALS[i] if i < len(MEDALS) else f'`#{i+1}`'} **{row.get('guild_name', '—')}** — `{row.get('xp', 0):,} XP`"
            for i, row in enumerate(callboard[:7])
        ) or "> *No callboard data yet*"

        guild_count  = data.get("guild_count")
        user_count   = data.get("user_count")
        active_calls = data.get("active_calls")
        reg_guilds   = data.get("registered_guilds")
        total_users  = data.get("total_users")
        user_str     = f"`{user_count:,}`" if isinstance(user_count, int) else "`—`"

        embed = discord.Embed(
            description=(
                f"> `🌐` *KpnWorld API Status*\n\n"
                f"> `✅` *Connection:* `{latency}ms`\n"
                f"> `📡` *Last bot push:* {pushed_str}\n\n"
                f"> `🏠` *Guild count (Discord):* `{guild_count if guild_count is not None else '—'}`\n"
                f"> `👥` *User count (live):* {user_str}\n"
                f"> `📞` *Active calls:* `{active_calls if active_calls is not None else '—'}`\n"
                f"> `📋` *Registered guilds (DB):* `{reg_guilds if reg_guilds is not None else '—'}`\n"
                f"> `🧑\u200d🤝\u200d🧑` *Total users (DB):* `{total_users if total_users is not None else '—'}`\n\n"
                f"> `🏆` *Callboard ({len(callboard)} entries)*\n"
                f"{board_lines}"
            ),
            color=0xC084FC,
        )
        embed.set_footer(text=f"GET {url}")
        await ctx.send(embed=embed, ephemeral=True)

    @website.command(name="ping")
    async def website_ping(self, ctx: commands.Context[Any]) -> None:
        """Send a test push to verify the API secret and connection."""
        import httpx

        base   = (os.environ.get("WEBSITE_URL") or "").rstrip("/")
        secret = os.environ.get("MUSUBI_API_SECRET") or os.environ.get("API_SECRET") or ""

        if not base.startswith("http"):
            await ctx.send(embed=Embeds.error("WEBSITE_URL is not configured on this bot."), ephemeral=True)
            return

        if not secret:
            await ctx.send(embed=Embeds.error("MUSUBI_API_SECRET / API_SECRET is not set on this bot."), ephemeral=True)
            return

        url   = base + "/api/musubi/push"
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.post(
                    url,
                    json={"guild_count": 0, "user_count": 0, "active_calls": 0, "callboard": []},
                    headers={"Content-Type": "application/json", "X-API-Secret": secret},
                )
            latency = int((time.monotonic() - start) * 1000)

            if r.status_code == 200:
                await ctx.send(
                    embed=Embeds.action(
                        f"Connection successful — secret is valid. (`{latency}ms`)\n"
                        f"> Endpoint: `{url}`",
                        ctx.author,
                    ),
                    ephemeral=True,
                )
            elif r.status_code == 401:
                await ctx.send(
                    embed=Embeds.error(
                        f"API reachable but secret is **wrong** — got `401 Unauthorized`. (`{latency}ms`)\n"
                        f"> Check that `MUSUBI_API_SECRET` matches `API_SECRET` on the Flask service."
                    ),
                    ephemeral=True,
                )
            else:
                await ctx.send(
                    embed=Embeds.error(f"Unexpected HTTP `{r.status_code}` from API. (`{latency}ms`)"),
                    ephemeral=True,
                )
        except Exception as e:
            latency = int((time.monotonic() - start) * 1000)
            await ctx.send(
                embed=Embeds.error(f"Could not reach API (`{latency}ms`):\n```{e}```"),
                ephemeral=True,
            )

    # ── m.reload ──────────────────────────────────────────────────────────────

    @commands.group(name="reload", invoke_without_command=True)
    async def reload(self, ctx: commands.Context[Any]) -> None:
        await ctx.send(
            embed=Embeds.info("Available subcommands: `cog <name>`, `all`"),
            ephemeral=True,
        )

    @reload.command(name="cog")
    async def reload_cog(self, ctx: commands.Context[Any], name: str) -> None:
        """Reload a specific cog by name. Owner only."""
        if not self._is_owner(ctx.author.id):
            await ctx.send(embed=Embeds.error("This command is restricted to the bot owner."), ephemeral=True)
            return
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

    @reload.command(name="all")
    async def reload_all(self, ctx: commands.Context[Any]) -> None:
        """Reload all loaded cogs. Owner only."""
        if not self._is_owner(ctx.author.id):
            await ctx.send(embed=Embeds.error("This command is restricted to the bot owner."), ephemeral=True)
            return
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


    # ── m.guilds ──────────────────────────────────────────────────────────────

    @commands.group(name="guilds", invoke_without_command=True)
    async def guilds_group(self, ctx: commands.Context[Any]) -> None:
        await ctx.send(
            embed=Embeds.info("Available subcommands: `list [page]`, `info <guild_id>`"),
            ephemeral=True,
        )

    @guilds_group.command(name="list")
    async def guilds_list(self, ctx: commands.Context[Any], page: int = 1) -> None:
        """List all registered guilds with IDs, XP, and premium status. 10 per page."""
        guilds   = list(self.data.guilds.items())  # [(guild_id, data), ...]
        per_page = 10
        total    = len(guilds)
        pages    = max(1, -(-total // per_page))   # ceiling division
        page     = max(1, min(page, pages))
        start    = (page - 1) * per_page
        chunk    = guilds[start:start + per_page]

        if not chunk:
            await ctx.send(embed=Embeds.info("No guilds are currently registered."), ephemeral=True)
            return

        lines: list[str] = []
        for gid, g in chunk:
            discord_guild = self.bot.get_guild(int(gid))
            name          = discord_guild.name if discord_guild else f"Unknown `{gid[-4:]}`"
            xp            = g.get("xp") or 0
            is_banned     = g.get("is_banned", False)
            flag          = " `🔨`" if is_banned else ""
            lines.append(f"> `{gid}` **{name}**{flag} — `{xp:,} XP`")

        embed = discord.Embed(
            description=(
                f"> `🏠` *Registered Guilds — Page {page}/{pages} ({total} total)*\n\n"
                + "\n".join(lines)
            ),
            color=0xC084FC,
        )
        embed.set_footer(text=f"m.guilds list {page + 1}  —  next page" if page < pages else "Last page")
        await ctx.send(embed=embed, ephemeral=True)

    @guilds_group.command(name="info")
    async def guilds_info(self, ctx: commands.Context[Any], guild_id: str) -> None:
        """Show detailed info for a specific guild ID."""
        g = self.data.get_guild(guild_id)
        if not g:
            await ctx.send(
                embed=Embeds.error(f"Guild `{guild_id}` is not registered on the network."),
                ephemeral=True,
            )
            return

        discord_guild = self.bot.get_guild(int(guild_id))
        name          = discord_guild.name if discord_guild else f"Not in cache"
        members       = discord_guild.member_count if discord_guild else "—"
        icon          = discord_guild.icon.url if discord_guild and discord_guild.icon else None

        is_premium    = await self.data.is_premium_guild(guild_id)
        booth         = g.get("booth_channel", "—")
        xp            = g.get("xp") or 0
        is_banned     = g.get("is_banned", False)
        invite_url    = g.get("invite_url")
        invite_quota  = g.get("invite_quota") or 0
        prefix        = g.get("prefix") or "`Default (m.)`"

        embed = discord.Embed(
            description=(
                f"> `🏠` *Guild Info*\n\n"
                f"> `🆔` *ID:* `{guild_id}`\n"
                f"> `📛` *Name:* **{name}**\n"
                f"> `👥` *Members:* `{members}`\n"
                f"> `📺` *Booth:* <#{booth}>\n"
                f"> `🏆` *XP:* `{xp:,}`\n"
                f"> `✨` *Premium:* `{'Active' if is_premium else 'None'}`\n"
                f"> `🔨` *Banned:* `{'Yes' if is_banned else 'No'}`\n"
                f"> `📬` *Invite bank:* `{invite_quota}`\n"
                f"> `⌨️` *Prefix:* `{prefix}`\n"
                + (f"> `🔗` *Invite:* {invite_url}" if invite_url else "> `🔗` *Invite:* `Not set`")
            ),
            color=0xC084FC,
        )
        if icon:
            embed.set_thumbnail(url=str(icon))
        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        await ctx.send(embed=embed, ephemeral=True)


async def setup(bot: MusubiBot) -> None:
    await bot.add_cog(Sudo(bot))