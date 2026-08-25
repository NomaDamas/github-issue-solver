from __future__ import annotations

import sqlite3
import os

from .db import get_setting, set_setting


def normalize_owner(owner: str) -> str:
    return (owner or "").strip().lower()


def token_key(owner: str) -> str:
    return f"github_token_owner_{normalize_owner(owner)}"


def legacy_owner_token(conn: sqlite3.Connection, owner: str) -> str:
    owner_norm = normalize_owner(owner)
    return get_setting(conn, token_key(owner_norm), "") if owner_norm else ""


def migrate_legacy_owner_tokens(conn: sqlite3.Connection) -> None:
    """Move legacy github_token_owner_* settings into owner_tokens.

    Older project-specific builds stored organization tokens as settings keys
    such as github_token_owner_nomadamas. The open-source version uses the
    generic owner_tokens table. Keep runtime compatibility and migrate lazily so
    existing installations do not silently lose org tokens after upgrading.
    """
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'github_token_owner_%' AND value<>''"
    ).fetchall()
    for row in rows:
        owner = str(row["key"])[len("github_token_owner_"):]
        if not owner:
            continue
        exists = conn.execute("SELECT 1 FROM owner_tokens WHERE lower(owner)=lower(?)", (owner,)).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO owner_tokens (owner, token) VALUES (?, ?)
            ON CONFLICT(owner) DO UPDATE SET token=excluded.token, updated_at=CURRENT_TIMESTAMP
            """,
            (owner, row["value"]),
        )


def env_fallback_token() -> str:
    """Return the supervisor-refreshed GitHub token, when available.

    The UI/database can hold long-lived owner tokens, but GitHub CLI/keyring tokens
    are the most reliable source on this workstation. Prefer the supervisor
    token so a stale DB token cannot silently stall polling for every repo.
    """
    return os.environ.get("GIS_GH_TOKEN_FALLBACK", "").strip()


def get_owner_token(conn: sqlite3.Connection, owner: str) -> str:
    """Return the token to use for a given repo owner.

    An owner-specific token (stored via the UI) wins; legacy owner-token settings
    are migrated/read for backward compatibility; otherwise we fall back to the
    personal account token.
    """
    fallback = env_fallback_token()
    if fallback:
        return fallback
    migrate_legacy_owner_tokens(conn)
    owner_norm = normalize_owner(owner)
    if owner_norm:
        row = conn.execute("SELECT token FROM owner_tokens WHERE lower(owner)=lower(?)", (owner_norm,)).fetchone()
        if row:
            return row["token"]
        legacy = legacy_owner_token(conn, owner_norm)
        if legacy:
            return legacy
    return get_setting(conn, "github_token_personal", "") or get_setting(conn, "github_token", "")


def get_any_token(conn: sqlite3.Connection) -> str:
    fallback = env_fallback_token()
    if fallback:
        return fallback
    migrate_legacy_owner_tokens(conn)
    row = conn.execute("SELECT token FROM owner_tokens ORDER BY owner LIMIT 1").fetchone()
    legacy = conn.execute("SELECT value FROM settings WHERE key LIKE 'github_token_owner_%' AND value<>'' ORDER BY key LIMIT 1").fetchone()
    return (
        get_setting(conn, "github_token_personal", "")
        or get_setting(conn, "github_token", "")
        or (row["token"] if row else "")
        or (legacy["value"] if legacy else "")
    )


def set_owner_token(conn: sqlite3.Connection, owner: str, token: str) -> None:
    owner_norm = normalize_owner(owner)
    if not owner_norm:
        return
    conn.execute(
        """
        INSERT INTO owner_tokens (owner, token) VALUES (?, ?)
        ON CONFLICT(owner) DO UPDATE SET token=excluded.token, updated_at=CURRENT_TIMESTAMP
        """,
        (owner_norm, token.strip()),
    )
    # Keep the legacy setting in sync for old code paths during rolling updates.
    set_setting(conn, token_key(owner_norm), token.strip())


def delete_owner_token(conn: sqlite3.Connection, owner: str) -> None:
    owner_norm = normalize_owner(owner)
    conn.execute("DELETE FROM owner_tokens WHERE lower(owner)=lower(?)", (owner_norm,))
    if owner_norm:
        set_setting(conn, token_key(owner_norm), "")


def list_owner_tokens(conn: sqlite3.Connection) -> list[dict[str, object]]:
    migrate_legacy_owner_tokens(conn)
    rows = {
        r["owner"].lower(): {"owner": r["owner"], "configured": True}
        for r in conn.execute("SELECT owner FROM owner_tokens ORDER BY owner").fetchall()
    }
    for row in conn.execute("SELECT key FROM settings WHERE key LIKE 'github_token_owner_%' AND value<>'' ORDER BY key").fetchall():
        owner = str(row["key"])[len("github_token_owner_"):]
        if owner:
            rows.setdefault(owner.lower(), {"owner": owner, "configured": True})
    return sorted(rows.values(), key=lambda r: str(r["owner"]).lower())


def configured_org_owners(conn: sqlite3.Connection) -> list[str]:
    return [str(r["owner"]) for r in list_owner_tokens(conn)]


def configured_tokens(conn: sqlite3.Connection) -> dict[str, bool]:
    data = {
        "personal": bool(get_setting(conn, "github_token_personal", "") or get_setting(conn, "github_token", "")),
        "audit": bool(get_setting(conn, "github_token_audit", "")),
    }
    for item in list_owner_tokens(conn):
        data[str(item["owner"])] = True
    return data


def get_audit_token(conn: sqlite3.Connection) -> str:
    return env_fallback_token() or get_setting(conn, "github_token_audit", "")
