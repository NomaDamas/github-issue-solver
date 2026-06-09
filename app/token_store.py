from __future__ import annotations

import sqlite3

from .db import get_setting


def normalize_owner(owner: str) -> str:
    return (owner or "").strip().lower()


def get_owner_token(conn: sqlite3.Connection, owner: str) -> str:
    """Return the token to use for a given repo owner.

    An owner-specific token (stored via the UI) wins; otherwise we fall back to
    the personal account token. This keeps the service generic: users register a
    token per organization/account they want to operate on.
    """
    owner_norm = normalize_owner(owner)
    if owner_norm:
        row = conn.execute("SELECT token FROM owner_tokens WHERE lower(owner)=lower(?)", (owner_norm,)).fetchone()
        if row:
            return row["token"]
    return get_setting(conn, "github_token_personal", "") or get_setting(conn, "github_token", "")


def get_any_token(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT token FROM owner_tokens ORDER BY owner LIMIT 1").fetchone()
    return (
        get_setting(conn, "github_token_personal", "")
        or get_setting(conn, "github_token", "")
        or (row["token"] if row else "")
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


def delete_owner_token(conn: sqlite3.Connection, owner: str) -> None:
    owner_norm = normalize_owner(owner)
    conn.execute("DELETE FROM owner_tokens WHERE lower(owner)=lower(?)", (owner_norm,))


def list_owner_tokens(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = {
        r["owner"].lower(): {"owner": r["owner"], "configured": True}
        for r in conn.execute("SELECT owner FROM owner_tokens ORDER BY owner").fetchall()
    }
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
    return get_setting(conn, "github_token_audit", "")
