"""
Project MUSUBI — datamanager.py v4
Guild-to-guild relay. Supabase REST via httpx.

Speed 1: in-memory (webhook cache only — sessions live in DB)
Speed 2: in-memory cache for guilds, users, sudo (loaded at startup)
Speed 3: Supabase REST (all writes + session reads)

Schema notes:
- Guilds.level column removed — XP is wiped monthly via Supabase cron
- Guilds.xp persists per cycle and is saved for leaderboard snapshot before wipe
- Sessions: single row per call (caller + target on same row)

Memory notes (v4):
- Single persistent httpx.AsyncClient shared across all requests instead of
  spawning a new client per call. Each new client allocates its own SSL context
  and connection pool — at high message rates this caused significant GC pressure
  and RAM spikes. The shared client reuses connections and keeps one SSL context.
- Call close() on shutdown to cleanly release the client.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Optional

import httpx

log = logging.getLogger("musubi.data")

XP_PER_MESSAGE        = 10
XP_PREMIUM_MULTIPLIER = 2   # guild premium doubles XP

# Max tombstone entries before pruning oldest half
_TOMBSTONE_MAX = 2_000


def _base_headers() -> dict:
    key = os.environ.get("SUPABASE_KEY", "")
    return {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


def _url(table: str) -> str:
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    return f"{base}/rest/v1/{table}"


class DataManager:

    def __init__(self) -> None:
        # Speed 1
        self.webhook_cache: dict[str, Any] = {}  # guild_id → discord.Webhook

        # Speed 2
        self.guilds: dict[str, dict] = {}   # guild_id (str) → guild data
        self.users:  dict[str, dict] = {}   # user_id  (str) → user data
        self.sudo:         set[str] = set()
        self.banned_users: set[str] = set()
        self.blocklist:    set[str] = set()

        # Fast-path tombstone — ended session IDs so bridge stops relay immediately
        # Pruned when it exceeds _TOMBSTONE_MAX to prevent unbounded growth
        self._ended_sessions: set[str] = set()

        # Expose XP constants so cogs don't need to import the module-level vars
        self.XP_PER_MESSAGE        = XP_PER_MESSAGE
        self.XP_PREMIUM_MULTIPLIER = XP_PREMIUM_MULTIPLIER

        # Single persistent HTTP client — reuses SSL context and connection pool.
        # Created here so it's available before load_all() is called.
        self._http = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        """Call on bot shutdown to cleanly close the HTTP client."""
        await self._http.aclose()

    # ── HTTP helpers ────────────────────────────────────────────────────

    async def _get(self, table: str, params: dict) -> list[dict]:
        r = await self._http.get(_url(table), params=params, headers=_base_headers())
        r.raise_for_status()
        return r.json()

    async def _upsert(self, table: str, payload: dict) -> list[dict]:
        headers = {**_base_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}
        r = await self._http.post(_url(table), json=payload, headers=headers)
        r.raise_for_status()
        return r.json()

    async def _insert(self, table: str, payload: dict) -> dict:
        r = await self._http.post(_url(table), json=payload, headers=_base_headers())
        if not r.is_success:
            log.error("_insert %s failed — %s: %s", table, r.status_code, r.text)
        r.raise_for_status()
        return r.json()[0]

    async def _patch(self, table: str, params: dict, payload: dict) -> list[dict]:
        headers = {**_base_headers(), "Prefer": "return=representation"}
        r = await self._http.patch(_url(table), params=params, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()

    async def _delete(self, table: str, params: dict) -> None:
        r = await self._http.delete(_url(table), params=params, headers=_base_headers())
        r.raise_for_status()

    # ── Startup ─────────────────────────────────────────────────────────

    async def load_all(self) -> None:
        await asyncio.gather(
            self._load_guilds(),
            self._load_users(),
            self._load_sudo(),
            self._load_banned_users(),
            self._load_blocklist(),
        )
        await self._clear_stale_sessions()
        log.info(
            "Cache warmed — guilds:%d users:%d sudo:%d banned:%d blocklist:%d",
            len(self.guilds), len(self.users), len(self.sudo),
            len(self.banned_users), len(self.blocklist),
        )

    async def _clear_stale_sessions(self) -> None:
        """On startup, end any sessions left open from a previous run."""
        try:
            rows = await self._get("Sessions", {
                "select": "id",
                "status": "in.(active,searching)",
            })
            if not rows:
                return
            for row in rows:
                await self._patch("Sessions", {"id": f"eq.{row['id']}"}, {"status": "ended"})
            log.info("Startup cleanup — ended %d stale session(s).", len(rows))
        except Exception as e:
            log.error("_clear_stale_sessions failed: %s", e)

    async def _load_guilds(self) -> None:
        try:
            rows = await self._get("Guilds", {"select": "*"})
            self.guilds = {
                r["guild_id"]: {
                    "booth_channel": r["booth_channel"],
                    "webhook":       r.get("webhook"),
                    "prefix":        r.get("prefix"),
                    "xp":            r.get("xp") or 0,
                    "is_banned":     r.get("is_banned") or False,
                }
                for r in rows
            }
        except Exception as e:
            log.error("Failed to load guilds: %s", e)

    async def _load_users(self) -> None:
        try:
            rows = await self._get("Users", {"select": "*"})
            self.users = {
                r["user_id"]: {
                    "is_anonymous": r.get("is_anonymous", False),
                    "is_banned":    r.get("is_banned", False),
                    "nickname":     r.get("nickname"),
                    "avatar":       r.get("avatar"),
                    "prefix":       r.get("prefix"),
                }
                for r in rows
            }
        except Exception as e:
            log.error("Failed to load users: %s", e)

    async def _load_sudo(self) -> None:
        try:
            rows = await self._get("Sudo", {"select": "user_id"})
            self.sudo = {r["user_id"] for r in rows}
        except Exception as e:
            log.error("Failed to load sudo: %s", e)

    async def _load_banned_users(self) -> None:
        try:
            rows = await self._get("Users", {"select": "user_id", "is_banned": "eq.true"})
            self.banned_users = {r["user_id"] for r in rows}
        except Exception as e:
            log.error("Failed to load banned users: %s", e)

    async def _load_blocklist(self) -> None:
        try:
            rows = await self._get("Blocklist", {"select": "phrase"})
            self.blocklist = {r["phrase"] for r in rows}
        except Exception as e:
            log.error("Failed to load blocklist: %s", e)

    # ── Guilds ──────────────────────────────────────────────────────────

    def get_guild(self, guild_id: int | str) -> Optional[dict]:
        return self.guilds.get(str(guild_id))

    def is_guild_registered(self, guild_id: int | str) -> bool:
        return str(guild_id) in self.guilds

    def is_guild_banned(self, guild_id: int | str) -> bool:
        g = self.get_guild(guild_id)
        return g["is_banned"] if g else False

    async def register_guild(
        self, guild_id: int | str, booth_channel: int | str, webhook_url: str
    ) -> None:
        gid = str(guild_id)
        self.guilds[gid] = {
            "booth_channel": str(booth_channel),
            "webhook":       webhook_url,
            "prefix":        None,
            "xp":            0,
            "is_banned":     False,
        }
        try:
            await self._upsert("Guilds", {
                "guild_id":      gid,
                "booth_channel": str(booth_channel),
                "webhook":       webhook_url,
                "xp":            0,
                "is_banned":     False,
            })
        except Exception as e:
            log.error("register_guild failed: %s", e)

    async def unregister_guild(self, guild_id: int | str) -> None:
        gid = str(guild_id)
        self.guilds.pop(gid, None)
        self.webhook_cache.pop(gid, None)
        try:
            await self._delete("Guilds", {"guild_id": f"eq.{gid}"})
        except Exception as e:
            log.error("unregister_guild failed: %s", e)

    async def set_guild_prefix(self, guild_id: int | str, prefix: str) -> None:
        gid = str(guild_id)
        if gid in self.guilds:
            self.guilds[gid]["prefix"] = prefix
        try:
            await self._patch("Guilds", {"guild_id": f"eq.{gid}"}, {"prefix": prefix})
        except Exception as e:
            log.error("set_guild_prefix failed: %s", e)

    async def ban_guild(self, guild_id: int | str, banned: bool = True) -> None:
        gid = str(guild_id)
        if gid in self.guilds:
            self.guilds[gid]["is_banned"] = banned
        try:
            await self._patch("Guilds", {"guild_id": f"eq.{gid}"}, {"is_banned": banned})
        except Exception as e:
            log.error("ban_guild failed: %s", e)

    async def add_xp(self, guild_id: int | str, amount: int = XP_PER_MESSAGE, premium_guild: bool = False) -> dict:
        """
        Add XP to a guild. Returns dict with updated xp value.
        Level is not tracked — XP is wiped monthly via Supabase cron.
        """
        gid = str(guild_id)
        g   = self.guilds.get(gid)
        if not g:
            return {}

        g["xp"] = g["xp"] or 0
        actual  = amount * XP_PREMIUM_MULTIPLIER if premium_guild else amount
        g["xp"] += actual

        try:
            await self._patch(
                "Guilds",
                {"guild_id": f"eq.{gid}"},
                {"xp": g["xp"]},
            )
        except Exception as e:
            log.error("add_xp failed: %s", e)

        return {"xp": g["xp"]}

    # ── Users ───────────────────────────────────────────────────────────

    def get_user(self, user_id: int | str) -> dict:
        uid = str(user_id)
        if uid not in self.users:
            self.users[uid] = {
                "is_anonymous": False,
                "is_banned":    False,
                "nickname":     None,
                "avatar":       None,
                "prefix":       None,
            }
        return self.users[uid]

    async def upsert_user(self, user_id: int | str) -> bool:
        """
        Ensure the user row exists in the DB. Returns True on success.
        Must succeed before any FK-dependent insert (e.g. Sessions, Premium).
        """
        uid = str(user_id)
        u   = self.get_user(uid)
        try:
            await self._upsert("Users", {
                "user_id":      uid,
                "is_anonymous": u.get("is_anonymous", False),
                "is_banned":    u.get("is_banned", False),
                "nickname":     u.get("nickname"),
                "avatar":       u.get("avatar"),
                "prefix":       u.get("prefix"),
            })
            return True
        except Exception as e:
            log.error("upsert_user failed for %s: %s", uid, e)
            return False

    def resolve_identity(self, user_id: int | str, display_name: str, bot_avatar: Optional[str] = None) -> tuple[str, Optional[str]]:
        """
        Returns (name, avatar_url) for webhook relay.
        Anonymous users get a generic name AND the bot avatar — never their real avatar.
        """
        u = self.get_user(user_id)
        if u.get("is_anonymous"):
            return "📞 Anonymous", bot_avatar or None
        name   = u.get("nickname") or display_name
        avatar = u.get("avatar")
        return name, avatar

    # ── User bans ───────────────────────────────────────────────────────

    def is_user_banned(self, user_id: int | str) -> bool:
        return str(user_id) in self.banned_users

    async def ban_user(self, user_id: int | str) -> None:
        uid = str(user_id)
        self.banned_users.add(uid)
        u = self.get_user(uid)
        u["is_banned"] = True
        try:
            await self._patch("Users", {"user_id": f"eq.{uid}"}, {"is_banned": True})
        except Exception as e:
            log.error("ban_user failed: %s", e)

    async def unban_user(self, user_id: int | str) -> None:
        uid = str(user_id)
        self.banned_users.discard(uid)
        if uid in self.users:
            self.users[uid]["is_banned"] = False
        try:
            await self._patch("Users", {"user_id": f"eq.{uid}"}, {"is_banned": False})
        except Exception as e:
            log.error("unban_user failed: %s", e)

    # ── Blocklist ────────────────────────────────────────────────────────

    async def blocklist_add(self, phrase: str) -> None:
        self.blocklist.add(phrase)
        try:
            await self._upsert("Blocklist", {"phrase": phrase})
        except Exception as e:
            log.error("blocklist_add failed: %s", e)

    async def blocklist_remove(self, phrase: str) -> None:
        self.blocklist.discard(phrase)
        try:
            await self._delete("Blocklist", {"phrase": f"eq.{phrase}"})
        except Exception as e:
            log.error("blocklist_remove failed: %s", e)

    async def blocklist_clear(self) -> None:
        """Clear all blocklist entries. Uses correct PostgREST not-null syntax."""
        self.blocklist.clear()
        try:
            await self._delete("Blocklist", {"phrase": "not.is.null"})
        except Exception as e:
            log.error("blocklist_clear failed: %s", e)

    # ── Sessions ────────────────────────────────────────────────────────

    async def create_session(
        self, caller_guild: int | str, caller_channel: int | str, caller_id: int | str
    ) -> dict:
        """
        Insert a new searching session row. Called only when no partner is waiting.
        Ensures the user row exists first to satisfy the FK constraint.
        Returns the session row or {} on failure.
        """
        ok = await self.upsert_user(caller_id)
        if not ok:
            log.error("create_session aborted — upsert_user failed for %s", caller_id)
            return {}
        try:
            row = await self._insert("Sessions", {
                "caller_guild":   str(caller_guild),
                "caller_channel": str(caller_channel),
                "caller_id":      str(caller_id),
                "status":         "searching",
            })
            return row
        except Exception as e:
            log.error("create_session failed: %s", e)
            return {}

    async def find_match(self, caller_guild: int | str, priority: bool = False) -> Optional[dict]:
        """
        Find a searching session from a different guild.
        If priority=True, try to match with a premium guild first before falling back.
        """
        try:
            rows = await self._get("Sessions", {
                "select":       "*",
                "status":       "eq.searching",
                "caller_guild": f"neq.{caller_guild}",
                "order":        "created_at.asc",
                "limit":        "10",
            })
            if not rows:
                return None

            if priority:
                for row in rows:
                    if await self.is_premium_guild(row["caller_guild"]):
                        return row

            return rows[0]
        except Exception as e:
            log.error("find_match failed: %s", e)
            return None

    async def connect_partner_session(
        self,
        waiting_session_id: str,
        caller_guild: str,
        caller_channel: str,
    ) -> dict:
        """
        Connect a call by patching the waiting guild's single session row.

        One row represents the entire call:
          - caller_guild / caller_channel  = the guild that entered the queue first
          - target_guild / target_channel  = the guild that matched and connected

        No second row is ever created for the connecting guild. Both sides query
        this single row via get_active_session (which checks caller OR target).

        Returns the updated row so phone.py can tombstone it if needed.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            rows = await self._patch(
                "Sessions",
                {"id": f"eq.{waiting_session_id}"},
                {
                    "target_guild":   caller_guild,
                    "target_channel": caller_channel,
                    "status":         "active",
                    "last_activity":  now,
                },
            )
            return rows[0] if rows else {}
        except Exception as e:
            log.error("connect_partner_session failed: %s", e)
            return {}

    async def get_active_session(self, guild_id: int | str) -> Optional[dict]:
        """
        Get the active session for a guild — the guild can be on either side
        of the call (caller_guild or target_guild on the single shared row).

        PostgREST requires the `or` filter to be passed as a query parameter
        with the column filters embedded in the value string. The status filter
        is applied inside the or() so it doesn't conflict.
        """
        gid = str(guild_id)
        try:
            rows = await self._get("Sessions", {
                "select": "*",
                "and":    f"(status.eq.active,or(caller_guild.eq.{gid},target_guild.eq.{gid}))",
                "limit":  "1",
            })
            if not rows:
                return None
            row = rows[0]
            if row["id"] in self._ended_sessions:
                return None
            return row
        except Exception as e:
            log.error("get_active_session failed: %s", e)
            return None

    async def get_searching_session(self, guild_id: int | str) -> Optional[dict]:
        """Get the searching session for a guild if one exists."""
        gid = str(guild_id)
        try:
            rows = await self._get("Sessions", {
                "select":       "*",
                "status":       "eq.searching",
                "caller_guild": f"eq.{gid}",
                "limit":        "1",
            })
            if not rows:
                return None
            row = rows[0]
            if row["id"] in self._ended_sessions:
                return None
            return row
        except Exception as e:
            log.error("get_searching_session failed: %s", e)
            return None

    def _tombstone(self, session_id: str) -> None:
        """
        Add a session ID to the in-memory tombstone set.
        Prunes the oldest half of entries if the set exceeds _TOMBSTONE_MAX.
        """
        self._ended_sessions.add(session_id)
        if len(self._ended_sessions) > _TOMBSTONE_MAX:
            # Discard oldest half — sets are unordered so we just drop half arbitrarily
            victims = list(self._ended_sessions)[: _TOMBSTONE_MAX // 2]
            for v in victims:
                self._ended_sessions.discard(v)
            log.debug("Tombstone pruned — removed %d entries.", len(victims))

    async def end_session(self, session_id: str, status: str = "ended") -> None:
        self._tombstone(session_id)
        try:
            await self._patch("Sessions", {"id": f"eq.{session_id}"}, {"status": status})
        except Exception as e:
            log.error("end_session failed: %s", e)

    async def bump_activity(self, session_id: str) -> None:
        try:
            await self._patch(
                "Sessions",
                {"id": f"eq.{session_id}"},
                {"last_activity": datetime.now(timezone.utc).isoformat()},
            )
        except Exception as e:
            log.error("bump_activity failed: %s", e)

    async def add_xp_bulk(self, guild_id: int | str, amount: int) -> None:
        """
        Write a pre-accumulated XP amount directly to DB.
        In-memory guild cache is already updated incrementally by bridge.py,
        so this is a pure DB flush — no cache update needed here.
        """
        gid = str(guild_id)
        g   = self.guilds.get(gid)
        if not g:
            return
        try:
            await self._patch(
                "Guilds",
                {"guild_id": f"eq.{gid}"},
                {"xp": g.get("xp") or 0},
            )
        except Exception as e:
            log.error("add_xp_bulk failed for %s: %s", gid, e)

    async def get_idle_sessions(self, idle_minutes: int = 30) -> list[dict]:
        """Return active sessions with no activity for idle_minutes."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)).isoformat()
        try:
            return await self._get("Sessions", {
                "select":        "*",
                "status":        "eq.active",
                "last_activity": f"lt.{cutoff}",
            })
        except Exception as e:
            log.error("get_idle_sessions failed: %s", e)
            return []

    async def get_leaderboard(self, limit: int = 7) -> list[dict]:
        """Return top guilds by XP for the current cycle."""
        try:
            return await self._get("Guilds", {
                "select": "guild_id,xp,xp_reset_at",
                "xp":     "gt.0",
                "order":  "xp.desc",
                "limit":  str(limit),
            })
        except Exception as e:
            log.error("get_leaderboard failed: %s", e)
            return []

    async def get_leaderboard_history(self, limit: int = 7) -> list[dict]:
        """Return the most recent completed leaderboard cycle snapshot."""
        try:
            rows = await self._get("Leaderboard", {
                "select":  "guild_id,xp,rank,cycle_end",
                "order":   "cycle_end.desc,rank.asc",
                "limit":   str(limit * 3),
            })
            if not rows:
                return []
            latest = rows[0]["cycle_end"][:10]
            return [r for r in rows if r["cycle_end"][:10] == latest][:limit]
        except Exception as e:
            log.error("get_leaderboard_history failed: %s", e)
            return []

    async def get_stale_searching_sessions(self, timeout_seconds: int = 40) -> list[dict]:
        """Return searching sessions waiting longer than timeout_seconds."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
        try:
            return await self._get("Sessions", {
                "select":     "*",
                "status":     "eq.searching",
                "created_at": f"lt.{cutoff}",
            })
        except Exception as e:
            log.error("get_stale_searching_sessions failed: %s", e)
            return []

    async def count_active_calls(self) -> int:
        """Count active calls — each session row with a target = 1 call."""
        try:
            rows = await self._get("Sessions", {
                "select":       "id",
                "status":       "eq.active",
                "target_guild": "not.is.null",
            })
            return len(rows)
        except Exception as e:
            log.error("count_active_calls failed: %s", e)
            return 0

    # ── Premium ─────────────────────────────────────────────────────────

    async def is_premium_user(self, user_id: int | str) -> bool:
        uid = str(user_id)
        now = datetime.now(timezone.utc).isoformat()
        try:
            rows = await self._get("Premium", {
                "select":     "id",
                "user_id":    f"eq.{uid}",
                "tier":       "eq.user",
                "expires_at": f"gt.{now}",
                "limit":      "1",
            })
            return len(rows) > 0
        except Exception as e:
            log.error("is_premium_user failed: %s", e)
            return False

    async def is_premium_guild(self, guild_id: int | str) -> bool:
        gid = str(guild_id)
        now = datetime.now(timezone.utc).isoformat()
        try:
            rows = await self._get("Premium", {
                "select":     "id",
                "guild_id":   f"eq.{gid}",
                "tier":       "eq.guild",
                "expires_at": f"gt.{now}",
                "limit":      "1",
            })
            return len(rows) > 0
        except Exception as e:
            log.error("is_premium_guild failed: %s", e)
            return False

    async def grant_premium(
        self,
        tier: str,
        expires_at: datetime,
        user_id: Optional[int | str] = None,
        guild_id: Optional[int | str] = None,
    ) -> bool:
        """
        Grant premium. Returns True on success.
        Ensures the user row exists before insert to satisfy FK constraint.
        A failed upsert_user aborts the grant so the key is NOT consumed silently.
        """
        payload: dict = {
            "tier":       tier,
            "expires_at": expires_at.isoformat(),
        }
        if user_id:
            uid = str(user_id)
            ok  = await self.upsert_user(uid)
            if not ok:
                log.error("grant_premium aborted — upsert_user failed for %s", uid)
                return False
            payload["user_id"] = uid
        if guild_id:
            payload["guild_id"] = str(guild_id)
        try:
            await self._insert("Premium", payload)
            return True
        except Exception as e:
            log.error("grant_premium failed: %s", e)
            return False

    # ── Premium Keys ────────────────────────────────────────────────────

    def _generate_key(self) -> str:
        import secrets
        import string
        chars = string.ascii_uppercase + string.digits
        parts = ["".join(secrets.choice(chars) for _ in range(4)) for _ in range(4)]
        return "MSBY-" + "-".join(parts)

    async def create_key(self, key_type: str, days: int, created_by: int | str) -> str:
        key = self._generate_key()
        try:
            await self._insert("PremiumKeys", {
                "key":        key,
                "type":       key_type,
                "days":       days,
                "created_by": str(created_by),
            })
        except Exception as e:
            log.error("create_key failed: %s", e)
        return key

    async def get_unused_keys(self) -> list[dict]:
        try:
            return await self._get("PremiumKeys", {
                "select":   "key,type,days,created_at",
                "redeemed": "eq.false",
                "order":    "created_at.desc",
            })
        except Exception as e:
            log.error("get_unused_keys failed: %s", e)
            return []

    async def revoke_key(self, key: str) -> bool:
        try:
            await self._delete("PremiumKeys", {"key": f"eq.{key.upper()}"})
            return True
        except Exception as e:
            log.error("revoke_key failed: %s", e)
            return False

    async def redeem_key(
        self,
        key: str,
        user_id: Optional[int | str] = None,
        guild_id: Optional[int | str] = None,
    ) -> tuple[bool, str]:
        """
        Attempt to redeem a key. Returns (success, message).
        grant_premium is called only after the key patch succeeds,
        preventing keys from being consumed without premium being granted.
        """
        key = key.upper()
        try:
            rows = await self._get("PremiumKeys", {
                "select": "*",
                "key":    f"eq.{key}",
            })
        except Exception as e:
            log.error("redeem_key fetch failed: %s", e)
            return False, "Failed to look up key. Please try again."

        if not rows:
            return False, "Invalid key."

        row = rows[0]
        if row["redeemed"]:
            return False, "This key has already been redeemed."

        key_type = row["type"]
        days     = row["days"]

        if key_type == "user" and not user_id:
            return False, "This is a user key — redeem it as yourself, not as a guild."
        if key_type == "guild" and not guild_id:
            return False, "This is a guild key — redeem it inside a server."

        redeemed_by = str(user_id or guild_id)
        try:
            await self._patch("PremiumKeys", {"key": f"eq.{key}"}, {
                "redeemed":    True,
                "redeemed_by": redeemed_by,
                "redeemed_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            log.error("redeem_key patch failed: %s", e)
            return False, "Failed to redeem key. Please try again."

        expires = datetime.now(timezone.utc) + timedelta(days=days)
        ok = await self.grant_premium(
            tier=key_type,
            expires_at=expires,
            user_id=user_id,
            guild_id=guild_id,
        )
        if not ok:
            # Attempt to roll back the key mark — best effort
            try:
                await self._patch("PremiumKeys", {"key": f"eq.{key}"}, {
                    "redeemed":    False,
                    "redeemed_by": None,
                    "redeemed_at": None,
                })
            except Exception:
                pass
            return False, "Failed to activate premium. The key has not been consumed — please try again."

        label = "personal" if key_type == "user" else "server"
        return True, f"Key redeemed! **{label.title()} premium** is now active for **{days} days**."

    # ── Sudo ────────────────────────────────────────────────────────────

    def is_sudo(self, user_id: int | str) -> bool:
        return str(user_id) in self.sudo

    async def add_sudo(self, user_id: int | str, granted_by: int | str) -> None:
        uid = str(user_id)
        self.sudo.add(uid)
        try:
            await self._upsert("Sudo", {
                "user_id":    uid,
                "granted_by": str(granted_by),
            })
        except Exception as e:
            log.error("add_sudo failed: %s", e)

    async def remove_sudo(self, user_id: int | str) -> None:
        uid = str(user_id)
        self.sudo.discard(uid)
        try:
            await self._delete("Sudo", {"user_id": f"eq.{uid}"})
        except Exception as e:
            log.error("remove_sudo failed: %s", e)