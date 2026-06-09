from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import db, get_setting, init_db, password_hash, row_to_dict, set_setting, verify_password
from .github_client import GitHubClient, GitHubError
from .orchestrator import create_jobs_for_repo_untracked_issues, discover_open_issue_candidates, discover_repositories, poll_once, process_next_job, background_loop
from .token_store import configured_org_owners, configured_tokens, delete_owner_token, get_any_token, get_audit_token, get_owner_token, list_owner_tokens, set_owner_token

app = FastAPI(title="GitHub Issue Solver", version="0.1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
_bg_task: asyncio.Task | None = None


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class SettingsIn(BaseModel):
    github_token: str | None = None  # legacy/personal fallback
    personal_token: str | None = None
    owner_tokens: list[dict[str, Any]] = []
    audit_token: str | None = None
    poll_interval_seconds: int = Field(ge=30, le=86400)
    workspace_dir: str
    max_agent_seconds: int = Field(ge=60, le=21600)
    polling_enabled: bool = True
    auto_register_enabled: bool = True
    auto_register_owners: str = ""
    bot_comment_prefix: str = "[github-issue-solver]"
    default_implement_agent: str = "omx"
    default_verify_agent: str = "omx"


class RepoIn(BaseModel):
    owner: str
    name: str
    default_branch: str = "main"
    enabled: bool = True
    auto_merge: bool = True
    implement_agent: str = "omx"
    verify_agent: str = "omx"
    issue_labels: str = ""


def require_user(request: Request) -> dict:
    token = request.cookies.get("gis_session", "")
    if not token:
        raise HTTPException(401, "Not authenticated")
    with db() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires_at > CURRENT_TIMESTAMP",
            (token,),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Not authenticated")
    return dict(row)


@app.on_event("startup")
async def startup() -> None:
    global _bg_task
    init_db()
    Path("static").mkdir(exist_ok=True)
    _bg_task = asyncio.create_task(background_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    if _bg_task:
        _bg_task.cancel()


@app.get("/")
def index() -> HTMLResponse:
    html = Path("static/index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "styles.css"):
        try:
            ver = int(Path(f"static/{asset}").stat().st_mtime)
        except OSError:
            ver = 0
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={ver}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


@app.post("/api/login")
def login(data: LoginIn, response: Response) -> dict:
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username=?", (data.username,)).fetchone()
        if not user or not verify_password(data.password, user["password_hash"]):
            raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다")
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(days=14)
        conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)", (token, user["id"], expires.strftime("%Y-%m-%d %H:%M:%S")))
    response.set_cookie("gis_session", token, httponly=True, samesite="lax", max_age=14 * 86400)
    return {"ok": True, "must_change_password": bool(user["must_change_password"])}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get("gis_session", "")
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    response.delete_cookie("gis_session")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(require_user)) -> dict:
    return {"username": user["username"], "must_change_password": bool(user["must_change_password"])}


@app.post("/api/change-password")
def change_password(data: PasswordIn, user: dict = Depends(require_user)) -> dict:
    if not verify_password(data.current_password, user["password_hash"]):
        raise HTTPException(400, "현재 비밀번호가 올바르지 않습니다")
    with db() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, must_change_password=0, updated_at=CURRENT_TIMESTAMP WHERE id=1",
            (password_hash(data.new_password),),
        )
    return {"ok": True}


@app.get("/api/settings")
def get_settings(user: dict = Depends(require_user)) -> dict:
    with db() as conn:
        tokens = configured_tokens(conn)
        return {
            "github_token_configured": any(tokens.values()),
            "tokens_configured": tokens,
            "owner_tokens": list_owner_tokens(conn),
            "poll_interval_seconds": int(get_setting(conn, "poll_interval_seconds", "300")),
            "workspace_dir": get_setting(conn, "workspace_dir", "workspace"),
            "max_agent_seconds": int(get_setting(conn, "max_agent_seconds", "3600")),
            "polling_enabled": get_setting(conn, "polling_enabled", "1") == "1",
            "auto_register_enabled": get_setting(conn, "auto_register_enabled", "1") == "1",
            "auto_register_owners": get_setting(conn, "auto_register_owners", ""),
            "bot_comment_prefix": get_setting(conn, "bot_comment_prefix", "[github-issue-solver]"),
            "default_implement_agent": get_setting(conn, "default_implement_agent", "omx"),
            "default_verify_agent": get_setting(conn, "default_verify_agent", "omx"),
            "last_poll_started_at": get_setting(conn, "last_poll_started_at", ""),
            "last_poll_finished_at": get_setting(conn, "last_poll_finished_at", ""),
            "last_poll_result": get_setting(conn, "last_poll_result", ""),
            "last_loop_heartbeat_at": get_setting(conn, "last_loop_heartbeat_at", ""),
        }


@app.put("/api/settings")
def save_settings(data: SettingsIn, user: dict = Depends(require_user)) -> dict:
    with db() as conn:
        personal = data.personal_token or data.github_token
        if personal is not None and personal.strip():
            set_setting(conn, "github_token_personal", personal.strip())
            set_setting(conn, "github_token", personal.strip())  # legacy fallback
        for item in data.owner_tokens or []:
            owner = str(item.get("owner") or "").strip()
            token = str(item.get("token") or "").strip()
            delete_flag = str(item.get("delete") or "").lower() in {"1", "true", "yes"}
            if owner and delete_flag:
                delete_owner_token(conn, owner)
            elif owner and token:
                set_owner_token(conn, owner, token)
        if data.audit_token is not None and data.audit_token.strip():
            set_setting(conn, "github_token_audit", data.audit_token.strip())
        set_setting(conn, "poll_interval_seconds", str(data.poll_interval_seconds))
        set_setting(conn, "workspace_dir", data.workspace_dir)
        set_setting(conn, "max_agent_seconds", str(data.max_agent_seconds))
        set_setting(conn, "polling_enabled", "1" if data.polling_enabled else "0")
        set_setting(conn, "auto_register_enabled", "1" if data.auto_register_enabled else "0")
        set_setting(conn, "auto_register_owners", data.auto_register_owners)
        set_setting(conn, "bot_comment_prefix", data.bot_comment_prefix)
        impl = validate_agent(data.default_implement_agent)
        verify = validate_agent(data.default_verify_agent)
        set_setting(conn, "default_implement_agent", impl)
        set_setting(conn, "default_verify_agent", verify)
        conn.execute("UPDATE repositories SET implement_agent=?, verify_agent=?, updated_at=CURRENT_TIMESTAMP", (impl, verify))
    return {"ok": True}


@app.get("/api/audit-diagnostics")
def audit_diagnostics(user: dict = Depends(require_user)) -> dict:
    with db() as conn:
        personal_token = get_owner_token(conn, "__personal__")
        audit_token = get_audit_token(conn)
        owner_tokens = {owner: get_owner_token(conn, owner) for owner in configured_org_owners(conn)}
    login = None
    audit_token_status = None
    if personal_token:
        login = GitHubClient(personal_token).authenticated_user().get("login")
    if audit_token:
        audit_token_status = GitHubClient(audit_token).oauth_status()
    orgs = []
    for owner, token in owner_tokens.items():
        if login and owner.lower() == str(login).lower():
            continue
        diag_token = audit_token or token
        if not diag_token:
            orgs.append({"org": owner, "ok": False, "status": 0, "message": "Token not configured", "sample_count": 0, "diagnosis": "토큰 미설정"})
            continue
        diag = GitHubClient(diag_token).audit_log_access_status(owner)
        diag["owner_token_configured"] = bool(token)
        try:
            diag["repo_access_count"] = len(GitHubClient(token).list_accessible_repos({owner})) if token else 0
        except Exception:
            diag["repo_access_count"] = None
        orgs.append(diag)
    return {"ok": True, "login": login, "audit_token": audit_token_status, "orgs": orgs}


@app.get("/api/repos")
def list_repos(user: dict = Depends(require_user)) -> list[dict]:
    with db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM repositories
                ORDER BY datetime(COALESCE(NULLIF(github_pushed_at,''), NULLIF(github_updated_at,''), updated_at)) DESC,
                         owner, name
                """
            ).fetchall()
        ]


def validate_agent(name: str) -> str:
    name = name.lower().strip()
    if name not in {"omx", "claude"}:
        raise HTTPException(400, "agent는 omx 또는 claude만 지원합니다")
    return name


@app.post("/api/repos")
def add_repo(data: RepoIn, user: dict = Depends(require_user)) -> dict:
    owner, name = data.owner.strip(), data.name.strip()
    if not owner or not name:
        raise HTTPException(400, "owner/name이 필요합니다")
    with db() as conn:
        token = get_owner_token(conn, owner)
    if token:
        try:
            remote = GitHubClient(token).repo(owner, name)
            data.default_branch = remote.get("default_branch") or data.default_branch
        except GitHubError as exc:
            raise HTTPException(400, f"GitHub 저장소 확인 실패: {exc}")
    with db() as conn:
        impl = validate_agent(data.implement_agent or get_setting(conn, "default_implement_agent", "omx"))
        verify = validate_agent(data.verify_agent or get_setting(conn, "default_verify_agent", "omx"))
        conn.execute(
            """
            INSERT INTO repositories (owner, name, default_branch, enabled, auto_merge, implement_agent, verify_agent, issue_labels)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner, name) DO UPDATE SET
              default_branch=excluded.default_branch,
              enabled=excluded.enabled,
              auto_merge=excluded.auto_merge,
              implement_agent=excluded.implement_agent,
              verify_agent=excluded.verify_agent,
              issue_labels=excluded.issue_labels,
              updated_at=CURRENT_TIMESTAMP
            """,
            (owner, name, data.default_branch.strip() or "main", int(data.enabled), int(data.auto_merge), impl, verify, data.issue_labels.strip()),
        )
    return {"ok": True}


@app.delete("/api/repos/{repo_id}")
def delete_repo(repo_id: int, user: dict = Depends(require_user)) -> dict:
    with db() as conn:
        conn.execute("DELETE FROM repositories WHERE id=?", (repo_id,))
    return {"ok": True}


@app.patch("/api/repos/{repo_id}")
def update_repo(repo_id: int, data: RepoIn, user: dict = Depends(require_user)) -> dict:
    with db() as conn:
        conn.execute(
            """
            UPDATE repositories SET owner=?, name=?, default_branch=?, enabled=?, auto_merge=?, implement_agent=?, verify_agent=?, issue_labels=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (data.owner.strip(), data.name.strip(), data.default_branch.strip() or "main", int(data.enabled), int(data.auto_merge), validate_agent(data.implement_agent), validate_agent(data.verify_agent), data.issue_labels.strip(), repo_id),
        )
    return {"ok": True}


@app.get("/api/issues")
def list_issues(user: dict = Depends(require_user)) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT i.*, r.owner, r.name AS repo_name,
                   ij.status AS implement_job_status, ij.finished_at AS implemented_at,
                   vj.status AS verify_job_status, vj.verdict, vj.finished_at AS verified_at,
                   COALESCE(vj.pr_url, ij.pr_url, '') AS pr_url,
                   COALESCE(vj.pr_number, ij.pr_number, '') AS pr_number
            FROM issues i JOIN repositories r ON r.id=i.repo_id
            LEFT JOIN jobs ij ON ij.id=(SELECT max(id) FROM jobs WHERE issue_id=i.id AND type='implement')
            LEFT JOIN jobs vj ON vj.id=(SELECT max(id) FROM jobs WHERE issue_id=i.id AND type='verify')
            ORDER BY i.created_at DESC LIMIT 200
            """
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/jobs")
def list_jobs(user: dict = Depends(require_user)) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT j.id, j.type, j.status, j.agent, j.branch, j.pr_number, j.pr_url, j.verdict, j.error,
                   j.created_at, j.started_at, j.finished_at, i.number AS issue_number, i.title, r.owner, r.name AS repo_name
            FROM jobs j
            JOIN issues i ON i.id=j.issue_id
            JOIN repositories r ON r.id=j.repo_id
            ORDER BY j.created_at DESC, j.id DESC LIMIT 200
            """
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int, user: dict = Depends(require_user)) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "job not found")
        return dict(row)


@app.get("/api/dashboard")
def dashboard(user: dict = Depends(require_user)) -> dict:
    with db() as conn:
        repos = conn.execute("SELECT COUNT(*) c FROM repositories").fetchone()["c"]
        enabled = conn.execute("SELECT COUNT(*) c FROM repositories WHERE enabled=1").fetchone()["c"]
        stats = {r["status"]: r["c"] for r in conn.execute("SELECT status, COUNT(*) c FROM issues GROUP BY status").fetchall()}
        jobs = {r["status"]: r["c"] for r in conn.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()}
        runtime = {
            "last_poll_started_at": get_setting(conn, "last_poll_started_at", ""),
            "last_poll_finished_at": get_setting(conn, "last_poll_finished_at", ""),
            "last_poll_result": get_setting(conn, "last_poll_result", ""),
            "last_loop_heartbeat_at": get_setting(conn, "last_loop_heartbeat_at", ""),
            "polling_enabled": get_setting(conn, "polling_enabled", "1") == "1",
        }
        owners = [
            dict(r)
            for r in conn.execute(
                """
                SELECT owner, COUNT(*) AS repos, SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled_repos
                FROM repositories
                GROUP BY owner
                ORDER BY lower(owner)
                """
            ).fetchall()
        ]
    return {"repos": repos, "enabled_repos": enabled, "issues": stats, "jobs": jobs, "runtime": runtime, "owners": owners}


@app.post("/api/discover-issues")
async def discover_issues_now(user: dict = Depends(require_user)) -> dict:
    try:
        result = await asyncio.to_thread(discover_open_issue_candidates)
        return {"ok": True, **result}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/repos/{repo_id}/approve-created-by-me")
async def approve_created_by_me(repo_id: int, user: dict = Depends(require_user)) -> dict:
    with db() as conn:
        repo = conn.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()
        if not repo:
            raise HTTPException(404, "repo not found")
        conn.execute("UPDATE repositories SET enabled=1, auto_discovered=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (repo_id,))
    created = await asyncio.to_thread(create_jobs_for_repo_untracked_issues, repo_id)
    return {"ok": True, "created_jobs": created}


@app.post("/api/discover-repos")
async def discover_repos_now(user: dict = Depends(require_user)) -> dict:
    try:
        result = await asyncio.to_thread(discover_repositories)
        return {"ok": True, **result}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/poll-now")
async def poll_now(user: dict = Depends(require_user)) -> dict:
    try:
        created = await asyncio.to_thread(poll_once, False, True)
        return {"ok": True, "created_jobs": created}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/process-next")
async def process_now(user: dict = Depends(require_user)) -> dict:
    ran = await asyncio.to_thread(process_next_job)
    return {"ok": True, "ran": ran}
