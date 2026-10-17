"""
db.py — Lightweight SQLite database layer for MusicBot.

Manages:
1. Long-lasting web session persistence (survives bot restarts for 30+ days).
2. Community / rotating Gemini API key pool with health metrics, cooldown tracking,
   and audit details (who contributed, when, last used, total calls).
"""

import json
import logging
import os
import sqlite3
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("MUSICBOT_DB_PATH", "musicbot.db")
DEFAULT_SESSION_DURATION = 30 * 86400  # 30 days in seconds


def get_connection() -> sqlite3.Connection:
    """Return a connection to the SQLite database with WAL mode and row factory."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


def init_db():
    """Create tables and indexes if they do not exist."""
    with get_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL,
            user_data TEXT NOT NULL,
            guilds_data TEXT NOT NULL,
            access_token TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            last_active REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

        CREATE TABLE IF NOT EXISTS gemini_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT UNIQUE NOT NULL,
            added_by_user_id TEXT NOT NULL,
            added_by_username TEXT NOT NULL,
            added_by_avatar TEXT,
            added_at REAL NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'healthy', -- 'healthy', 'quota_exceeded', 'invalid'
            status_message TEXT DEFAULT '',
            cooldown_until REAL NOT NULL DEFAULT 0,
            total_uses INTEGER NOT NULL DEFAULT 0,
            last_used_at REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_gemini_keys_status ON gemini_keys(is_active, status, cooldown_until);

        CREATE TABLE IF NOT EXISTS guild_music_state (
            guild_id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS guild_autoqueue_config (
            guild_id TEXT PRIMARY KEY,
            prompt TEXT NOT NULL DEFAULT '',
            count INTEGER NOT NULL DEFAULT 5,
            threshold INTEGER NOT NULL DEFAULT 2,
            set_by_name TEXT,
            set_by_avatar TEXT,
            updated_at REAL NOT NULL
        );
        """)
    logger.info("[DB] Initialized database at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Sessions Operations
# ---------------------------------------------------------------------------

def save_session(session_id: str, user_data: dict, guilds_data: list, access_token: str, duration: float = DEFAULT_SESSION_DURATION):
    """Save or update an active user session."""
    now = time.time()
    expires_at = now + duration
    user_id = str(user_data.get("id", ""))
    username = str(user_data.get("username", ""))

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_id, user_id, username, user_data, guilds_data, access_token, created_at, expires_at, last_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                user_data=excluded.user_data,
                guilds_data=excluded.guilds_data,
                access_token=excluded.access_token,
                expires_at=excluded.expires_at,
                last_active=excluded.last_active
            """,
            (
                session_id,
                user_id,
                username,
                json.dumps(user_data),
                json.dumps(guilds_data),
                access_token,
                now,
                expires_at,
                now,
            )
        )


def get_session(session_id: str) -> Optional[dict]:
    """Retrieve session by ID, automatically extending expiration if valid."""
    now = time.time()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ? AND expires_at > ?",
            (session_id, now)
        ).fetchone()

        if not row:
            return None

        # Touch session activity and extend expiry if less than 7 days remain
        expires_at = row["expires_at"]
        if expires_at - now < 7 * 86400:
            expires_at = now + DEFAULT_SESSION_DURATION
            conn.execute(
                "UPDATE sessions SET last_active = ?, expires_at = ? WHERE session_id = ?",
                (now, expires_at, session_id)
            )

        return {
            "session_id": row["session_id"],
            "user": json.loads(row["user_data"]),
            "guilds": json.loads(row["guilds_data"]),
            "access_token": row["access_token"],
            "created_at": row["created_at"],
            "expires_at": expires_at,
        }


def delete_session(session_id: str):
    """Delete a session (logout)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def get_user_avatar(user_id: str) -> Optional[str]:
    """Retrieve avatar URL from sessions table for a given user ID."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_data FROM sessions WHERE user_id = ? ORDER BY last_active DESC LIMIT 1",
            (str(user_id),)
        ).fetchone()
        if row:
            try:
                u = json.loads(row["user_data"])
                if u.get("avatar"):
                    return f"https://cdn.discordapp.com/avatars/{user_id}/{u['avatar']}.png?size=64"
            except Exception:
                pass
    return None


def cleanup_expired_sessions():
    """Purge expired sessions from disk."""
    now = time.time()
    with get_connection() as conn:
        res = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        if res.rowcount > 0:
            logger.info("[DB] Cleaned up %d expired sessions", res.rowcount)


# ---------------------------------------------------------------------------
# Gemini Keys Operations
# ---------------------------------------------------------------------------

def mask_key(key: str) -> str:
    """Mask API key for safe public/UI display (e.g. 'AQ.Ab8RN...t6Gw')."""
    k = (key or "").strip()
    if len(k) <= 12:
        return "****"
    return f"{k[:8]}...{k[-4:]}"


def add_gemini_key(
    api_key: str,
    user_id: str = "system",
    username: str = "System",
    avatar: Optional[str] = None,
    initial_status: str = "healthy",
    status_message: str = "",
    cooldown_seconds: float = 0.0,
    is_active: bool = True,
) -> dict:
    """Add or update a Gemini API key in the rotation pool."""
    clean_key = api_key.strip()
    now = time.time()
    cooldown_until = (now + max(60.0, cooldown_seconds)) if (initial_status == "quota_exceeded" and cooldown_seconds > 0) else 0.0
    active_flag = 1 if is_active else 0

    with get_connection() as conn:
        row = conn.execute("SELECT id FROM gemini_keys WHERE api_key = ?", (clean_key,)).fetchone()
        if row:
            conn.execute(
                """
                UPDATE gemini_keys
                SET is_active = ?, status = ?, status_message = ?, cooldown_until = ?,
                    added_by_user_id = ?, added_by_username = ?, added_by_avatar = coalesce(?, added_by_avatar)
                WHERE id = ?
                """,
                (active_flag, initial_status, status_message, cooldown_until, str(user_id), username, avatar, row["id"])
            )
            return {
                "id": row["id"],
                "status": "updated",
                "key_status": initial_status,
                "masked_key": mask_key(clean_key),
                "cooldown_remaining_seconds": max(0, int(cooldown_until - now)) if cooldown_until > now else 0,
            }

        cur = conn.execute(
            """
            INSERT INTO gemini_keys (api_key, added_by_user_id, added_by_username, added_by_avatar, added_at, is_active, status, status_message, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (clean_key, str(user_id), username, avatar, now, active_flag, initial_status, status_message, cooldown_until)
        )
        return {
            "id": cur.lastrowid,
            "status": "added",
            "key_status": initial_status,
            "masked_key": mask_key(clean_key),
            "cooldown_remaining_seconds": max(0, int(cooldown_until - now)) if cooldown_until > now else 0,
        }


def get_available_gemini_keys() -> list[dict]:
    """Get all healthy or cooldown-expired active Gemini keys ordered by last used."""
    now = time.time()
    with get_connection() as conn:
        # Automatically restore keys whose cooldown period has elapsed
        conn.execute(
            "UPDATE gemini_keys SET status = 'healthy', status_message = '' WHERE status = 'quota_exceeded' AND cooldown_until > 0 AND cooldown_until <= ?",
            (now,)
        )
        rows = conn.execute(
            """
            SELECT id, api_key, status, cooldown_until, total_uses, last_used_at
            FROM gemini_keys
            WHERE is_active = 1 AND status = 'healthy'
            ORDER BY last_used_at ASC, id ASC
            """
        ).fetchall()

        return [dict(r) for r in rows]


def get_all_gemini_keys_for_display() -> list[dict]:
    """Get all keys with masked values for the community/admin UI."""
    now = time.time()
    with get_connection() as conn:
        # Auto-restore expired cooldowns first
        conn.execute(
            "UPDATE gemini_keys SET status = 'healthy', status_message = '' WHERE status = 'quota_exceeded' AND cooldown_until > 0 AND cooldown_until <= ?",
            (now,)
        )
        rows = conn.execute(
            """
            SELECT id, api_key, added_by_user_id, added_by_username, added_by_avatar,
                   added_at, is_active, status, status_message, cooldown_until, total_uses, last_used_at
            FROM gemini_keys
            ORDER BY added_at DESC
            """
        ).fetchall()

        result = []
        for r in rows:
            cd = r["cooldown_until"]
            remaining_cooldown = max(0, int(cd - now)) if cd and cd > now else 0
            result.append({
                "id": r["id"],
                "masked_key": mask_key(r["api_key"]),
                "added_by_user_id": r["added_by_user_id"],
                "added_by_username": r["added_by_username"],
                "added_by_avatar": r["added_by_avatar"],
                "added_at": r["added_at"],
                "is_active": bool(r["is_active"]),
                "status": r["status"],
                "status_message": r["status_message"],
                "cooldown_remaining_seconds": remaining_cooldown,
                "total_uses": r["total_uses"],
                "last_used_at": r["last_used_at"],
            })
        return result


def record_gemini_key_usage(key_id: int):
    """Update usage statistics on a key after successful generation."""
    now = time.time()
    with get_connection() as conn:
        conn.execute(
            "UPDATE gemini_keys SET total_uses = total_uses + 1, last_used_at = ?, status = 'healthy' WHERE id = ?",
            (now, key_id)
        )


def mark_gemini_key_quota_exceeded(key_id: int, cooldown_seconds: float = 21600, message: str = ""):
    """Mark a key as quota exceeded (429) with exact cooldown duration."""
    now = time.time()
    cooldown_until = now + max(60, cooldown_seconds)
    with get_connection() as conn:
        conn.execute(
            "UPDATE gemini_keys SET status = 'quota_exceeded', cooldown_until = ?, status_message = ? WHERE id = ?",
            (cooldown_until, message or "Quota exceeded", key_id)
        )
    logger.warning("[DB] Key #%d marked quota_exceeded for %d seconds: %s", key_id, int(cooldown_seconds), message)


def mark_gemini_key_invalid(key_id: int, message: str = "Invalid API key"):
    """Mark a key permanently invalid or deactivated."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE gemini_keys SET status = 'invalid', is_active = 0, status_message = ? WHERE id = ?",
            (message, key_id)
        )
    logger.error("[DB] Key #%d marked invalid: %s", key_id, message)


def delete_gemini_key(key_id: int) -> bool:
    """Delete a key from the pool (admin only)."""
    with get_connection() as conn:
        res = conn.execute("DELETE FROM gemini_keys WHERE id = ?", (key_id,))
        return res.rowcount > 0


# ---------------------------------------------------------------------------
# Guild Music State Persistence Operations
# ---------------------------------------------------------------------------

def save_guild_music_states(states_dict: dict):
    """Save guild music states atomically into SQLite."""
    now = time.time()
    with get_connection() as conn:
        with conn:
            for guild_id_str, g_state in states_dict.items():
                conn.execute(
                    """
                    INSERT INTO guild_music_state (guild_id, state_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        state_json = excluded.state_json,
                        updated_at = excluded.updated_at
                    """,
                    (str(guild_id_str), json.dumps(g_state), now)
                )


def load_guild_music_states() -> dict:
    """Load all guild music states from SQLite."""
    with get_connection() as conn:
        rows = conn.execute("SELECT guild_id, state_json FROM guild_music_state").fetchall()
        result = {}
        for r in rows:
            try:
                result[r["guild_id"]] = json.loads(r["state_json"])
            except Exception:
                pass
        return result


# ---------------------------------------------------------------------------
# Guild AI Auto-Queue Config Persistence
# ---------------------------------------------------------------------------

def save_autoqueue_config(guild_id: str | int, cfg: dict):
    """Save or update guild's auto-queue configuration (vibe, rules, author) to SQLite."""
    gid = str(guild_id)
    prompt = cfg.get("prompt", "")
    count = int(cfg.get("count", 5))
    threshold = int(cfg.get("threshold", 2))
    set_by_name = cfg.get("set_by_name")
    set_by_avatar = cfg.get("set_by_avatar")
    now = time.time()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO guild_autoqueue_config (guild_id, prompt, count, threshold, set_by_name, set_by_avatar, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                prompt=excluded.prompt,
                count=excluded.count,
                threshold=excluded.threshold,
                set_by_name=excluded.set_by_name,
                set_by_avatar=excluded.set_by_avatar,
                updated_at=excluded.updated_at
            """,
            (gid, prompt, count, threshold, set_by_name, set_by_avatar, now),
        )


def load_all_autoqueue_configs() -> dict[str, dict]:
    """Load all saved autoqueue configs from DB. All configs are loaded with enabled=False to protect tokens."""
    result = {}
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT guild_id, prompt, count, threshold, set_by_name, set_by_avatar FROM guild_autoqueue_config"
        ).fetchall()
        for row in rows:
            result[row["guild_id"]] = {
                "enabled": False,  # Always initialize as disabled on startup
                "prompt": row["prompt"] or "",
                "count": row["count"] or 5,
                "threshold": row["threshold"] or 2,
                "set_by_name": row["set_by_name"],
                "set_by_avatar": row["set_by_avatar"],
            }
    return result


def load_autoqueue_config(guild_id: str | int) -> dict | None:
    """Load saved autoqueue config for a single guild (with enabled=False)."""
    gid = str(guild_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT prompt, count, threshold, set_by_name, set_by_avatar FROM guild_autoqueue_config WHERE guild_id = ?",
            (gid,),
        ).fetchone()
        if row:
            return {
                "enabled": False,  # Disabled by default
                "prompt": row["prompt"] or "",
                "count": row["count"] or 5,
                "threshold": row["threshold"] or 2,
                "set_by_name": row["set_by_name"],
                "set_by_avatar": row["set_by_avatar"],
            }
    return None


