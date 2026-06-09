from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path(os.environ.get("GIS_DB", "github_issue_solver.db"))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 180_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    candidate = password_hash(password, salt).split("$", 2)[2]
    return secrets.compare_digest(candidate, digest)


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                must_change_password INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS owner_tokens (
                owner TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS repositories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                name TEXT NOT NULL,
                default_branch TEXT NOT NULL DEFAULT 'main',
                enabled INTEGER NOT NULL DEFAULT 1,
                auto_merge INTEGER NOT NULL DEFAULT 1,
                implement_agent TEXT NOT NULL DEFAULT 'omx',
                verify_agent TEXT NOT NULL DEFAULT 'omx',
                issue_labels TEXT NOT NULL DEFAULT '',
                auto_discovered INTEGER NOT NULL DEFAULT 0,
                github_pushed_at TEXT NOT NULL DEFAULT '',
                github_updated_at TEXT NOT NULL DEFAULT '',
                html_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(owner, name)
            );

            CREATE TABLE IF NOT EXISTS issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                number INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                html_url TEXT NOT NULL DEFAULT '',
                github_updated_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                implement_summary TEXT NOT NULL DEFAULT '',
                verify_summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(repo_id, number)
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
                repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                agent TEXT NOT NULL DEFAULT '',
                branch TEXT NOT NULL DEFAULT '',
                pr_number INTEGER,
                pr_url TEXT NOT NULL DEFAULT '',
                verdict TEXT NOT NULL DEFAULT '',
                log TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS repository_creator_checks (
                owner TEXT NOT NULL,
                name TEXT NOT NULL,
                method TEXT NOT NULL DEFAULT '',
                created_by_user INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(owner, name)
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status, created_at);
            """
        )
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(repositories)").fetchall()}
        if "auto_discovered" not in cols:
            conn.execute("ALTER TABLE repositories ADD COLUMN auto_discovered INTEGER NOT NULL DEFAULT 0")
        if "github_pushed_at" not in cols:
            conn.execute("ALTER TABLE repositories ADD COLUMN github_pushed_at TEXT NOT NULL DEFAULT ''")
        if "github_updated_at" not in cols:
            conn.execute("ALTER TABLE repositories ADD COLUMN github_updated_at TEXT NOT NULL DEFAULT ''")
        if "html_url" not in cols:
            conn.execute("ALTER TABLE repositories ADD COLUMN html_url TEXT NOT NULL DEFAULT ''")
        issue_cols = {row["name"] for row in conn.execute("PRAGMA table_info(issues)").fetchall()}
        if "implement_summary" not in issue_cols:
            conn.execute("ALTER TABLE issues ADD COLUMN implement_summary TEXT NOT NULL DEFAULT ''")
        if "verify_summary" not in issue_cols:
            conn.execute("ALTER TABLE issues ADD COLUMN verify_summary TEXT NOT NULL DEFAULT ''")
        user = conn.execute("SELECT id FROM users WHERE id=1").fetchone()
        if not user:
            initial_username = os.environ.get("GIS_INITIAL_USERNAME", "admin")
            # No plaintext bootstrap password is embedded in the app.
            # For a fresh DB, set GIS_INITIAL_PASSWORD before first start.
            # If omitted, a random unusable-by-default password is generated.
            initial_password = os.environ.get("GIS_INITIAL_PASSWORD") or secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO users (id, username, password_hash, must_change_password) VALUES (1, ?, ?, 1)",
                (initial_username, password_hash(initial_password)),
            )
        defaults = {
            "poll_interval_seconds": "300",
            "workspace_dir": str(Path("workspace").resolve()),
            "github_token": "",
            "github_token_personal": "",
            "github_token_audit": "",
            "bot_comment_prefix": "[github-issue-solver]",
            "max_agent_seconds": "3600",
            "polling_enabled": "1",
            "auto_register_enabled": "1",
            "auto_register_owners": "",
            "issue_search_enabled": "0",
            "creator_negative_recheck_seconds": "86400",
            "default_implement_agent": "omx",
            "default_verify_agent": "omx",
            "last_poll_started_at": "",
            "last_poll_finished_at": "",
            "last_poll_result": "",
            "last_loop_heartbeat_at": "",
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
