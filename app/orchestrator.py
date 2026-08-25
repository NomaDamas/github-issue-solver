from __future__ import annotations

import asyncio
import base64
import fcntl
import os
import re
import shlex
import shutil
import subprocess
import textwrap
import traceback
from pathlib import Path
from typing import Any

from .agents import run_agent
from .db import db, get_setting, row_to_dict, set_setting
from .github_client import GitHubClient, GitHubError
from .token_store import configured_org_owners, get_any_token, get_audit_token, get_owner_token

RUNNING = False
FINAL_ISSUE_STATUSES = {"verification_failed", "failed", "merged", "verified", "resolved", "closed"}
AUDIT_FALLBACK_WARNED: set[str] = set()

def touch_setting(key: str) -> None:
    with db() as conn:
        set_setting(conn, key, "")
        conn.execute("UPDATE settings SET value=datetime('now','localtime') WHERE key=?", (key,))


def mask_token(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def notify_target() -> str:
    with db() as conn:
        return get_setting(conn, "notification_target", "discord:1517044116167331850")


def send_notification(message: str) -> None:
    message = message.strip()
    if not message:
        return
    target = notify_target().strip() or "discord:1517044116167331850"
    hermes = shutil.which("hermes")
    if hermes:
        subprocess.run([hermes, "send", "--to", target, message], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return
    raise RuntimeError("hermes CLI not found for notifications")


def run_cmd(args: list[str], cwd: Path, token: str = "", timeout: int = 300) -> str:
    cmd = args
    if token and args and args[0] == "git":
        # GitHub REST accepts Bearer tokens, but git-over-HTTPS expects Basic
        # auth. Use an in-memory extraHeader so tokens never land in remotes.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd = ["git", "-c", f"http.extraHeader=Authorization: Basic {basic}"] + args[1:]
    proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    out = mask_token(proc.stdout, token)
    if token:
        out = out.replace(base64.b64encode(f"x-access-token:{token}".encode()).decode(), "***")
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(shlex.quote(a) for a in args)}\n{out}")
    return out


def append_job_log(job_id: int, text: str) -> None:
    with db() as conn:
        conn.execute("UPDATE jobs SET log = substr(log || ?, -200000) WHERE id=?", ("\n" + text, job_id))


def safe_branch(issue_number: int, job_id: int) -> str:
    return f"agent/issue-{issue_number}-{job_id}"


def reset_workspace_for_checkout(repo_dir: Path, ref: str, token: str) -> None:
    """Discard stale agent/build output before switching branches."""
    run_cmd(["git", "reset", "--hard"], repo_dir, token=token)
    run_cmd(["git", "clean", "-fd"], repo_dir, token=token)
    run_cmd(["git", "checkout", ref], repo_dir, token=token)

def checkout_workspace(owner: str, name: str, default_branch: str, branch: str, workspace: Path, token: str, job_id: int) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = workspace / f"{owner}__{name}"
    url = f"https://github.com/{owner}/{name}.git"
    if not repo_dir.exists():
        append_job_log(job_id, f"Cloning {owner}/{name}")
        run_cmd(["git", "clone", url, str(repo_dir)], workspace, token=token, timeout=900)
    append_job_log(job_id, "Fetching latest repository state")
    run_cmd(["git", "fetch", "origin", "--prune"], repo_dir, token=token, timeout=600)
    reset_workspace_for_checkout(repo_dir, default_branch, token)
    run_cmd(["git", "reset", "--hard", f"origin/{default_branch}"], repo_dir, token=token)
    run_cmd(["git", "clean", "-fd"], repo_dir, token=token)
    run_cmd(["git", "checkout", "-B", branch], repo_dir, token=token)
    run_cmd(["git", "config", "user.name", "github-issue-solver"], repo_dir, token=token)
    run_cmd(["git", "config", "user.email", "github-issue-solver@users.noreply.github.com"], repo_dir, token=token)
    return repo_dir


def git_has_changes(repo_dir: Path) -> bool:
    proc = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return bool(proc.stdout.strip())


def git_summary(repo_dir: Path) -> str:
    proc = subprocess.run(["git", "status", "--short"], cwd=repo_dir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc.stdout.strip()


def implementation_prompt(repo: dict[str, Any], issue: dict[str, Any], branch: str) -> str:
    return textwrap.dedent(f"""
    You are an autonomous coding agent working in this Git repository.

    Task source: GitHub issue #{issue['number']} in {repo['owner']}/{repo['name']}
    Title: {issue['title']}

    Issue body:
    {issue.get('body') or '(empty)'}

    Requirements:
    - Implement a focused fix for the issue.
    - Run relevant tests/build checks if the project provides them.
    - Do not merge or push; this orchestrator will commit and push branch {branch}.
    - Keep changes minimal and production-ready.
    - If the issue is impossible or ambiguous, leave a clear note in your final answer and avoid unrelated edits.
    """).strip()


def verification_prompt(repo: dict[str, Any], issue: dict[str, Any], pr_number: int) -> str:
    return textwrap.dedent(f"""
    You are a strict verification coding agent for {repo['owner']}/{repo['name']} PR #{pr_number}.

    Verify the implementation for GitHub issue #{issue['number']}: {issue['title']}

    Rules:
    - Inspect the diff and run relevant tests/build checks if possible.
    - Do not make persistent code changes.
    - Decide whether this PR is safe to merge.
    - End your final response with exactly one line: VERDICT: PASS or VERDICT: FAIL
    - Before the verdict, briefly explain evidence, commands run, and any risks.
    """).strip()


def parse_verdict(output: str) -> str:
    matches = re.findall(r"VERDICT:\s*(PASS|FAIL)", output.upper())
    return matches[-1] if matches else "FAIL"


def compact_agent_summary(output: str, limit: int = 1200) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", output or "")
    lines = [ln.strip() for ln in text.splitlines()]
    keep: list[str] = []
    capture = False
    for ln in lines:
        low = ln.lower()
        if any(k in low for k in ["summary", "validation", "changed files", "commands run", "evidence", "risks", "verdict"]):
            capture = True
        if capture and ln:
            keep.append(ln)
        if len("\n".join(keep)) >= limit:
            break
    if not keep:
        keep = [ln for ln in lines if ln][-12:]
    return "\n".join(keep)[-limit:].strip()


def process_implementation(job_id: int) -> None:
    with db() as conn:
        job = row_to_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        issue = row_to_dict(conn.execute("SELECT * FROM issues WHERE id=?", (job["issue_id"],)).fetchone())
        repo = row_to_dict(conn.execute("SELECT * FROM repositories WHERE id=?", (job["repo_id"],)).fetchone())
        token = get_owner_token(conn, repo["owner"])
        workspace = Path(get_setting(conn, "workspace_dir", "workspace")).expanduser().resolve()
        timeout = int(get_setting(conn, "max_agent_seconds", "3600"))
        prefix = get_setting(conn, "bot_comment_prefix", "[github-issue-solver]")
        branch = safe_branch(issue["number"], job_id)
        if issue["status"] in FINAL_ISSUE_STATUSES:
            conn.execute("UPDATE jobs SET status='failed', finished_at=CURRENT_TIMESTAMP, error=? WHERE id=?", (f"Issue is terminal ({issue['status']}); no retry is allowed.", job_id))
            return
        conn.execute("UPDATE jobs SET status='running', started_at=CURRENT_TIMESTAMP, error='', branch=?, agent=? WHERE id=?", (branch, repo["implement_agent"], job_id))
        conn.execute("UPDATE issues SET status='implementing', updated_at=CURRENT_TIMESTAMP WHERE id=?", (issue["id"],))

    try:
        send_notification(f"[작업 시작] implement {repo['owner']}/{repo['name']} #{issue['number']} — {issue['title']}")
    except Exception:
        Path("logs").mkdir(exist_ok=True)
        with open("logs/poller-errors.log", "a", encoding="utf-8") as f:
            f.write(f"\n--- notify implement-start {repo['owner']}/{repo['name']} #{issue['number']} ---\n{traceback.format_exc()}\n")

    gh = GitHubClient(token)
    repo_dir = checkout_workspace(repo["owner"], repo["name"], repo["default_branch"], branch, workspace, token, job_id)
    append_job_log(job_id, f"Running implementation agent: {repo['implement_agent']}")
    result = run_agent(repo["implement_agent"], repo_dir, implementation_prompt(repo, issue, branch), timeout)
    append_job_log(job_id, result.output)
    impl_summary = compact_agent_summary(result.output)
    if not result.ok:
        raise RuntimeError(f"Implementation agent exited with {result.returncode}")
    if not git_has_changes(repo_dir):
        raise RuntimeError("Implementation agent finished but produced no file changes")
    append_job_log(job_id, "Changed files:\n" + git_summary(repo_dir))
    run_cmd(["git", "add", "-A"], repo_dir, token=token)
    run_cmd(["git", "commit", "-m", f"Fix issue #{issue['number']}: {issue['title'][:80]}"], repo_dir, token=token)
    run_cmd(["git", "push", "-u", "origin", branch, "--force-with-lease"], repo_dir, token=token, timeout=900)
    pr = gh.create_pull_request(
        repo["owner"], repo["name"],
        title=f"Fix issue #{issue['number']}: {issue['title']}",
        head=branch,
        base=repo["default_branch"],
        body=f"{prefix}\n\nAutomated fix for #{issue['number']}.\n\nFixes #{issue['number']}\n\nVerification will run before merge.",
    )
    gh.create_issue_comment(repo["owner"], repo["name"], issue["number"], f"{prefix} 구현 에이전트가 PR #{pr['number']}을 생성했습니다: {pr['html_url']}")
    with db() as conn:
        conn.execute("UPDATE jobs SET status='completed', finished_at=CURRENT_TIMESTAMP, pr_number=?, pr_url=? WHERE id=?", (pr["number"], pr["html_url"], job_id))
        conn.execute("UPDATE issues SET status='verifying', implement_summary=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (impl_summary, issue["id"]))
        existing_verify = conn.execute("SELECT id FROM jobs WHERE issue_id=? AND type='verify' LIMIT 1", (issue["id"],)).fetchone()
        if not existing_verify:
            conn.execute(
                "INSERT INTO jobs (issue_id, repo_id, type, status, agent, branch, pr_number, pr_url) VALUES (?, ?, 'verify', 'queued', ?, ?, ?, ?)",
                (issue["id"], repo["id"], repo["verify_agent"], branch, pr["number"], pr["html_url"]),
            )


def process_verification(job_id: int) -> None:
    with db() as conn:
        job = row_to_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        issue = row_to_dict(conn.execute("SELECT * FROM issues WHERE id=?", (job["issue_id"],)).fetchone())
        repo = row_to_dict(conn.execute("SELECT * FROM repositories WHERE id=?", (job["repo_id"],)).fetchone())
        token = get_owner_token(conn, repo["owner"])
        workspace = Path(get_setting(conn, "workspace_dir", "workspace")).expanduser().resolve()
        timeout = int(get_setting(conn, "max_agent_seconds", "3600"))
        prefix = get_setting(conn, "bot_comment_prefix", "[github-issue-solver]")
        previous_failed_verify = conn.execute(
            """
            SELECT id FROM jobs
            WHERE issue_id=? AND type='verify' AND id<>?
              AND verdict='FAIL'
            LIMIT 1
            """,
            (issue["id"], job_id),
        ).fetchone()
        if issue["status"] == "verification_failed" or previous_failed_verify:
            conn.execute(
                "UPDATE jobs SET status='failed', finished_at=CURRENT_TIMESTAMP, verdict='FAIL', error=? WHERE id=?",
                ("Verification already failed once; retries are disabled for this issue.", job_id),
            )
            conn.execute("UPDATE issues SET status='verification_failed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (issue["id"],))
            return
        conn.execute("UPDATE jobs SET status='running', started_at=CURRENT_TIMESTAMP, error='', agent=? WHERE id=?", (repo["verify_agent"], job_id))

    try:
        send_notification(f"[작업 시작] verify {repo['owner']}/{repo['name']} #{issue['number']} — {issue['title']}")
    except Exception:
        Path("logs").mkdir(exist_ok=True)
        with open("logs/poller-errors.log", "a", encoding="utf-8") as f:
            f.write(f"\n--- notify verify-start {repo['owner']}/{repo['name']} #{issue['number']} ---\n{traceback.format_exc()}\n")

    repo_dir = workspace / f"{repo['owner']}__{repo['name']}"
    if not repo_dir.exists():
        repo_dir = checkout_workspace(repo["owner"], repo["name"], repo["default_branch"], job["branch"], workspace, token, job_id)
    run_cmd(["git", "fetch", "origin", "--prune"], repo_dir, token=token, timeout=600)
    reset_workspace_for_checkout(repo_dir, job["branch"], token)
    run_cmd(["git", "reset", "--hard", f"origin/{job['branch']}"], repo_dir, token=token)
    run_cmd(["git", "clean", "-fd"], repo_dir, token=token)
    append_job_log(job_id, f"Running verification agent: {repo['verify_agent']}")
    result = run_agent(repo["verify_agent"], repo_dir, verification_prompt(repo, issue, job["pr_number"]), timeout)
    append_job_log(job_id, result.output)
    if not result.ok:
        raise RuntimeError(f"Verification agent exited with {result.returncode}")
    verdict = parse_verdict(result.output)
    verify_summary = compact_agent_summary(result.output)
    gh = GitHubClient(token)
    if verdict == "PASS" and int(repo["auto_merge"]):
        merge = gh.merge_pull_request(repo["owner"], repo["name"], job["pr_number"], f"Merge automated fix for issue #{issue['number']}")
        append_job_log(job_id, f"Merge result: {merge}")
        try:
            closed = gh.close_issue_completed(repo["owner"], repo["name"], issue["number"])
            append_job_log(job_id, f"Issue closed as completed: {closed.get('html_url', '')}")
            close_note = "이 이슈를 resolved/completed 상태로 닫았습니다."
        except Exception as close_exc:
            # The PR body contains `Fixes #N`, so GitHub may already close it
            # on merge before this explicit close call lands. Treat an already
            # closed issue as success; otherwise fall back to a plain close.
            remote_issue = gh.get_issue(repo["owner"], repo["name"], issue["number"])
            if remote_issue.get("state") == "closed":
                append_job_log(job_id, f"Issue already closed after merge: {remote_issue.get('html_url', '')} reason={remote_issue.get('state_reason', '')}")
                close_note = "이 이슈는 이미 completed/closed 상태입니다."
            else:
                closed = gh.close_issue(repo["owner"], repo["name"], issue["number"])
                append_job_log(job_id, f"Issue closed with fallback API after completed-close failed ({close_exc}): {closed.get('html_url', '')}")
                close_note = "이 이슈를 closed 상태로 닫았습니다."
        gh.create_issue_comment(repo["owner"], repo["name"], issue["number"], f"{prefix} 검증 통과로 PR #{job['pr_number']}을 머지했습니다. {close_note}")
        issue_status = "merged"
    elif verdict == "PASS":
        gh.create_issue_comment(repo["owner"], repo["name"], issue["number"], f"{prefix} 검증은 통과했지만 자동 머지가 꺼져 있습니다. PR #{job['pr_number']}을 수동 머지하세요.")
        issue_status = "verified"
    else:
        gh.create_issue_comment(repo["owner"], repo["name"], issue["number"], f"{prefix} 검증 실패: PR #{job['pr_number']}은 머지하지 않았습니다. 이 이슈는 자동 재시도하지 않고 verification_failed 상태로 남깁니다.")
        issue_status = "verification_failed"
    try:
        if verdict == "PASS":
            if int(repo["auto_merge"]):
                send_notification(f"[작업 종료] verify {repo['owner']}/{repo['name']} #{issue['number']} — PASS / merged")
            else:
                send_notification(f"[작업 종료] verify {repo['owner']}/{repo['name']} #{issue['number']} — PASS / manual merge required")
        else:
            send_notification(f"[작업 종료] verify {repo['owner']}/{repo['name']} #{issue['number']} — FAIL")
    except Exception:
        Path("logs").mkdir(exist_ok=True)
        with open("logs/poller-errors.log", "a", encoding="utf-8") as f:
            f.write(f"\n--- notify verify-end {repo['owner']}/{repo['name']} #{issue['number']} ---\n{traceback.format_exc()}\n")
    with db() as conn:
        job_status = "completed" if verdict == "PASS" else "failed"
        conn.execute("UPDATE jobs SET status=?, finished_at=CURRENT_TIMESTAMP, verdict=?, error='' WHERE id=?", (job_status, verdict, job_id))
        conn.execute("UPDATE issues SET status=?, verify_summary=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (issue_status, verify_summary, issue["id"]))


def fail_job(job_id: int, exc: BaseException) -> None:
    err = "".join(traceback.format_exception(exc))[-50_000:]
    with db() as conn:
        job = row_to_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        conn.execute("UPDATE jobs SET status='failed', finished_at=CURRENT_TIMESTAMP, error=? WHERE id=?", (err, job_id))
        if job:
            issue_status = "failed" if job["type"] == "implement" else "verifying"
            conn.execute("UPDATE issues SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (issue_status, job["issue_id"]))
    append_job_log(job_id, "FAILED:\n" + err)


def recover_interrupted_jobs() -> int:
    """Requeue jobs left running by a process crash or system reboot."""
    with db() as conn:
        rows = conn.execute("SELECT id, issue_id, type FROM jobs WHERE status='running' ORDER BY id").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE jobs SET status='queued', started_at=NULL, error=? WHERE id=?",
                ("Recovered after service restart; requeued interrupted job.", row["id"]),
            )
            conn.execute(
                "UPDATE jobs SET log = substr(log || ?, -200000) WHERE id=?",
                ("\nRecovered after service restart; requeued interrupted job.", row["id"]),
            )
            issue_status = "queued" if row["type"] == "implement" else "verifying"
            conn.execute("UPDATE issues SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (issue_status, row["issue_id"]))
    return len(rows)

def parse_owner_list(raw: str) -> set[str]:
    return {part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()}


def parse_repo_list(raw: str) -> set[str]:
    return {part.strip().strip('/') for part in raw.replace("\n", ",").split(",") if part.strip().strip('/')}


def configured_org_repo_specs(conn) -> list[tuple[str, str]]:
    return []


def cached_creator_match(owner: str, name: str, negative_recheck_seconds: int) -> bool | None:
    """Return cached creator decision.

    True means previously confirmed as user-created.
    False means recently checked and not user-created.
    None means check now. New repositories therefore get checked immediately,
    while old non-matches do not slow down every poll cycle.
    """
    with db() as conn:
        row = conn.execute(
            """
            SELECT created_by_user, checked_at
            FROM repository_creator_checks
            WHERE lower(owner)=lower(?) AND lower(name)=lower(?)
            """,
            (owner, name),
        ).fetchone()
        if not row:
            return None
        if int(row["created_by_user"]):
            return True
        fresh = conn.execute(
            "SELECT datetime(?) > datetime('now', ?) AS fresh",
            (row["checked_at"], f"-{negative_recheck_seconds} seconds"),
        ).fetchone()["fresh"]
        return False if fresh else None


def store_creator_match(owner: str, name: str, method: str, created_by_user: bool, error: str = "") -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO repository_creator_checks (owner, name, method, created_by_user, checked_at, error)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(owner, name) DO UPDATE SET
              method=excluded.method,
              created_by_user=excluded.created_by_user,
              checked_at=CURRENT_TIMESTAMP,
              error=excluded.error
            """,
            (owner, name, method, int(created_by_user), error[-2000:]),
        )


def discover_repositories() -> dict[str, int]:
    """Auto-register personal repos and org repos made by the authenticated user.

    Personal token: all repos owned by the authenticated user.
    Org tokens: exact audit-log creator detection when GitHub allows it; otherwise
    automatically fall back to first-commit authorship. GitHub does not expose a
    repository creator field on normal repository APIs, and some org plans block
    audit logs, so first-commit authorship is the only non-manual fallback.
    Organization-wide issue search remains disabled.
    """
    with db() as conn:
        enabled = get_setting(conn, "auto_register_enabled", "1") == "1"
        personal_token = get_owner_token(conn, "__personal__")
        audit_token = get_audit_token(conn)
        org_specs = configured_org_repo_specs(conn)
        org_owners = configured_org_owners(conn)
        negative_recheck_seconds = int(get_setting(conn, "creator_negative_recheck_seconds", "86400"))
    if not enabled:
        return {"seen": 0, "created": 0, "updated": 0, "skipped": 0, "disabled": 0, "audit_unavailable": 0}

    repos: list[dict[str, Any]] = []
    seen = skipped = audit_unavailable = creator_checked = creator_cached = 0
    active_owner_names: set[str] = set()

    if personal_token:
        gh_personal = GitHubClient(personal_token)
        personal_login = (gh_personal.authenticated_user().get("login") or "").strip()
        if personal_login:
            active_owner_names.add(personal_login.lower())
            personal_repos = gh_personal.list_accessible_repos({personal_login})
            seen += len(personal_repos)
            repos.extend(personal_repos)

    audit_login = ""
    if audit_token:
        try:
            audit_gh = GitHubClient(audit_token)
            audit_login = (audit_gh.authenticated_user().get("login") or "").strip()
        except Exception:
            audit_login = ""

    creator_login = audit_login
    if not creator_login and personal_token:
        try:
            creator_login = (GitHubClient(personal_token).authenticated_user().get("login") or "").strip()
        except Exception:
            creator_login = ""

    personal_lower = creator_login.lower()
    for owner in org_owners:
        if owner.lower() == personal_lower:
            continue
        with db() as conn:
            owner_token = get_owner_token(conn, owner)
        if not owner_token or not creator_login:
            continue
        owner_gh = GitHubClient(owner_token)
        names: set[str] = set()
        audit_ok = False
        if audit_token and audit_login:
            try:
                names = GitHubClient(audit_token).org_repos_created_by_login(owner, audit_login)
                audit_ok = True
            except GitHubError as exc:
                skipped += 1
                audit_unavailable += 1
                if owner.lower() not in AUDIT_FALLBACK_WARNED:
                    AUDIT_FALLBACK_WARNED.add(owner.lower())
                    Path("logs").mkdir(exist_ok=True)
                    with open("logs/poller-errors.log", "a", encoding="utf-8") as f:
                        f.write(f"\n--- audit created repo discovery unavailable {owner}; falling back to first commit ({exc}) ---\n")
            except Exception:
                skipped += 1
                audit_unavailable += 1
                Path("logs").mkdir(exist_ok=True)
                with open("logs/poller-errors.log", "a", encoding="utf-8") as f:
                    f.write(f"\n--- audit created repo discovery failed {owner}; falling back to first commit ---\n{traceback.format_exc()}\n")

        if audit_ok:
            for name in sorted(names):
                try:
                    repos.append(owner_gh.repo(owner, name))
                    active_owner_names.add(owner.lower())
                    seen += 1
                except Exception:
                    skipped += 1
            continue

        # Automatic non-manual fallback for org plans that block audit logs:
        # scan repos accessible to the org token, but register only repos whose
        # oldest reachable commit is authored/committed by the authenticated user.
        try:
            accessible = owner_gh.list_accessible_repos({owner})
            seen += len(accessible)
        except Exception:
            skipped += 1
            Path("logs").mkdir(exist_ok=True)
            with open("logs/poller-errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- org repo listing failed {owner} ---\n{traceback.format_exc()}\n")
            accessible = []
        for item in accessible:
            repo_owner = ((item.get("owner") or {}).get("login") or owner).strip()
            repo_name = (item.get("name") or "").strip()
            default_branch = item.get("default_branch") or "main"
            if not repo_name:
                continue
            cached = cached_creator_match(repo_owner, repo_name, negative_recheck_seconds)
            if cached is True:
                repos.append(item)
                active_owner_names.add(repo_owner.lower())
                creator_cached += 1
                continue
            if cached is False:
                creator_cached += 1
                continue
            try:
                creator_checked += 1
                matched = owner_gh.repository_first_commit_by_login(repo_owner, repo_name, default_branch, creator_login)
                store_creator_match(repo_owner, repo_name, "first_commit", matched)
                if matched:
                    repos.append(item)
                    active_owner_names.add(repo_owner.lower())
            except Exception as exc:
                store_creator_match(repo_owner, repo_name, "first_commit", False, str(exc))
                skipped += 1

    for owner, name in org_specs:
        with db() as conn:
            token = get_owner_token(conn, owner)
        if not token:
            skipped += 1
            continue
        gh = GitHubClient(token)
        try:
            repo = gh.repo(owner, name)
            repos.append(repo)
            active_owner_names.add(((repo.get("owner") or {}).get("login") or owner).lower())
            seen += 1
        except Exception:
            skipped += 1
            Path("logs").mkdir(exist_ok=True)
            with open("logs/poller-errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- configured org repo failed {owner}/{name} ---\n{traceback.format_exc()}\n")

    created = updated = disabled = 0
    active_keys = {(((item.get("owner") or {}).get("login") or "").lower(), (item.get("name") or "").lower()) for item in repos}
    with db() as conn:
        for item in repos:
            owner = ((item.get("owner") or {}).get("login") or "").strip()
            name = (item.get("name") or "").strip()
            if not owner or not name:
                continue
            default_branch = item.get("default_branch") or "main"
            github_pushed_at = item.get("pushed_at") or ""
            github_updated_at = item.get("updated_at") or ""
            html_url = item.get("html_url") or ""
            existing = conn.execute("SELECT id FROM repositories WHERE lower(owner)=lower(?) AND lower(name)=lower(?)", (owner, name)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE repositories
                    SET owner=?, name=?, default_branch=?, github_pushed_at=?, github_updated_at=?, html_url=?,
                        enabled=1, auto_discovered=1, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (owner, name, default_branch, github_pushed_at, github_updated_at, html_url, existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO repositories
                    (owner, name, default_branch, github_pushed_at, github_updated_at, html_url, enabled, auto_merge, implement_agent, verify_agent, issue_labels, auto_discovered)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, '', 1)
                    """,
                    (
                        owner,
                        name,
                        default_branch,
                        github_pushed_at,
                        github_updated_at,
                        html_url,
                        get_setting(conn, "default_implement_agent", "gjc"),
                        get_setting(conn, "default_verify_agent", "gjc"),
                    ),
                )
                created += 1
        auto_rows = [dict(r) for r in conn.execute("SELECT id, owner, name FROM repositories WHERE auto_discovered=1 AND enabled=1").fetchall()]
        active_owner_lowers = {o.lower() for o in active_owner_names}
        for row in auto_rows:
            key = (row["owner"].lower(), row["name"].lower())
            if row["owner"].lower() in active_owner_lowers and key not in active_keys:
                conn.execute("UPDATE repositories SET enabled=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
                disabled += 1
    return {
        "seen": seen,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "disabled": disabled,
        "audit_unavailable": audit_unavailable,
        "creator_checked": creator_checked,
        "creator_cached": creator_cached,
    }


def repo_from_search_issue(item: dict[str, Any]) -> tuple[str, str] | None:
    repo_url = item.get("repository_url") or ""
    marker = "/repos/"
    if marker not in repo_url:
        return None
    full = repo_url.split(marker, 1)[1]
    parts = full.split("/")
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def create_jobs_for_repo_untracked_issues(repo_id: int) -> int:
    created = 0
    with db() as conn:
        issues = [dict(r) for r in conn.execute("SELECT * FROM issues WHERE repo_id=? AND status='repo_untracked'", (repo_id,)).fetchall()]
        repo = row_to_dict(conn.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone())
        if not repo:
            return 0
        for issue in issues:
            existing = conn.execute("SELECT id FROM jobs WHERE issue_id=? AND type='implement'", (issue["id"],)).fetchone()
            if existing:
                conn.execute("UPDATE issues SET status='queued', updated_at=CURRENT_TIMESTAMP WHERE id=?", (issue["id"],))
                continue
            conn.execute("INSERT INTO jobs (issue_id, repo_id, type, status, agent) VALUES (?, ?, 'implement', 'queued', ?)", (issue["id"], repo_id, repo["implement_agent"]))
            conn.execute("UPDATE issues SET status='queued', updated_at=CURRENT_TIMESTAMP WHERE id=?", (issue["id"],))
            created += 1
            try:
                send_notification(f"[이슈 감지] {repo['owner']}/{repo['name']} #{issue['number']} — {issue['title']}")
            except Exception:
                Path("logs").mkdir(exist_ok=True)
                with open("logs/poller-errors.log", "a", encoding="utf-8") as f:
                    f.write(f"\n--- notify detect {repo['owner']}/{repo['name']} #{issue['number']} ---\n{traceback.format_exc()}\n")
    return created


def discover_open_issue_candidates() -> dict[str, int]:
    # Disabled by policy: never track or display unconfirmed org repositories.
    return {"seen": 0, "untracked": 0}


def reconcile_repo_issues(conn, gh: GitHubClient, repo: dict[str, Any], open_numbers: set[int]) -> int:
    """Sync issues that were closed/resolved on GitHub but still look open locally.

    list_issues only returns open issues, so an issue closed on GitHub (manually
    or by an external PR) would otherwise stay frozen at its last local status.
    """
    reconciled = 0
    stale = conn.execute(
        "SELECT id, number, status FROM issues WHERE repo_id=? AND status NOT IN ('merged','resolved','closed')",
        (repo["id"],),
    ).fetchall()
    for row in stale:
        if row["number"] in open_numbers:
            continue
        try:
            remote = gh.get_issue(repo["owner"], repo["name"], row["number"])
        except Exception:
            continue
        if remote.get("state") != "closed":
            continue
        reason = remote.get("state_reason") or "completed"
        new_status = "closed" if reason == "not_planned" else "resolved"
        conn.execute(
            "UPDATE issues SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_status, row["id"]),
        )
        conn.execute(
            "UPDATE jobs SET status='cancelled', finished_at=CURRENT_TIMESTAMP WHERE issue_id=? AND status IN ('queued','running')",
            (row["id"],),
        )
        reconciled += 1
    return reconciled


def poll_once(run_discovery: bool = True, active_only: bool = False) -> int:
    created = 0
    touch_setting("last_poll_started_at")
    with db() as conn:
        set_setting(conn, "last_poll_result", "running")
    with db() as conn:
        any_token = get_any_token(conn)
    if not any_token:
        touch_setting("last_poll_finished_at")
        with db() as conn:
            set_setting(conn, "last_poll_result", "no token")
        return 0
    if run_discovery:
        try:
            discover_repositories()
        except Exception:
            Path("logs").mkdir(exist_ok=True)
            with open("logs/poller-errors.log", "a", encoding="utf-8") as f:
                f.write("\n--- repository auto-discovery ---\n" + traceback.format_exc() + "\n")
    with db() as conn:
        if active_only:
            # Manual sync: only refresh/reconcile repos that already have issues,
            # so it returns quickly. Full repo+issue scan stays on the background loop.
            repos = [dict(r) for r in conn.execute(
                "SELECT * FROM repositories WHERE enabled=1 AND id IN (SELECT DISTINCT repo_id FROM issues) ORDER BY owner, name"
            ).fetchall()]
        else:
            repos = [dict(r) for r in conn.execute("SELECT * FROM repositories WHERE enabled=1 ORDER BY owner, name").fetchall()]
    if not repos:
        return 0
    for repo in repos:
        try:
            with db() as conn:
                token = get_owner_token(conn, repo["owner"])
            if not token:
                continue
            gh = GitHubClient(token)
            remote_repo = gh.repo(repo["owner"], repo["name"])
            default_branch = remote_repo.get("default_branch") or repo["default_branch"]
            github_pushed_at = remote_repo.get("pushed_at") or ""
            github_updated_at = remote_repo.get("updated_at") or ""
            html_url = remote_repo.get("html_url") or ""
            issues = gh.list_issues(repo["owner"], repo["name"], repo.get("issue_labels", ""))
            with db() as conn:
                conn.execute(
                    "UPDATE repositories SET default_branch=?, github_pushed_at=?, github_updated_at=?, html_url=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (default_branch, github_pushed_at, github_updated_at, html_url, repo["id"]),
                )
                for item in issues:
                    exists = conn.execute("SELECT id FROM issues WHERE repo_id=? AND number=?", (repo["id"], item["number"])).fetchone()
                    if exists:
                        conn.execute(
                            "UPDATE issues SET title=?, body=?, html_url=?, github_updated_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('queued','implementing')",
                            (item.get("title") or "", item.get("body") or "", item.get("html_url") or "", item.get("updated_at") or "", exists["id"]),
                        )
                        continue
                    cur = conn.execute(
                        "INSERT INTO issues (repo_id, number, title, body, html_url, github_updated_at, status) VALUES (?, ?, ?, ?, ?, ?, 'queued')",
                        (repo["id"], item["number"], item.get("title") or "", item.get("body") or "", item.get("html_url") or "", item.get("updated_at") or ""),
                    )
                    issue_id = cur.lastrowid
                    conn.execute("INSERT INTO jobs (issue_id, repo_id, type, status, agent) VALUES (?, ?, 'implement', 'queued', ?)", (issue_id, repo["id"], repo["implement_agent"]))
                    created += 1
                open_numbers = {item["number"] for item in issues}
                reconcile_repo_issues(conn, gh, repo, open_numbers)
        except Exception as exc:
            # Repository-level errors are recorded as a synthetic log in settings-independent server logs.
            Path("logs").mkdir(exist_ok=True)
            with open("logs/poller-errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- {repo['owner']}/{repo['name']} ---\n{traceback.format_exc()}\n")
    touch_setting("last_poll_finished_at")
    with db() as conn:
        set_setting(conn, "last_poll_result", f"created_jobs={created}")
    return created


async def poll_with_heartbeat() -> None:
    task = asyncio.create_task(asyncio.to_thread(poll_once))
    while not task.done():
        touch_setting("last_loop_heartbeat_at")
        await asyncio.sleep(10)
    await task


def process_next_job() -> bool:
    global RUNNING
    if RUNNING:
        return False
    Path("logs").mkdir(exist_ok=True)
    with open("logs/process-next.lock", "w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        with db() as conn:
            job = row_to_dict(conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1").fetchone())
        if not job:
            return False
        RUNNING = True
        try:
            if job["type"] == "implement":
                process_implementation(job["id"])
            elif job["type"] == "verify":
                process_verification(job["id"])
            else:
                raise RuntimeError(f"Unknown job type: {job['type']}")
        except BaseException as exc:
            fail_job(job["id"], exc)
        finally:
            RUNNING = False
        return True


async def background_loop() -> None:
    # Let the web server finish binding before any GitHub discovery/polling work.
    await asyncio.sleep(10)
    last_poll = 0.0
    while True:
        try:
            with db() as conn:
                enabled = get_setting(conn, "polling_enabled", "1") == "1"
                interval = max(30, int(get_setting(conn, "poll_interval_seconds", "300")))
            touch_setting("last_loop_heartbeat_at")
            now = asyncio.get_running_loop().time()
            # Existing queued work is more important than scanning for more work.
            ran = await asyncio.to_thread(process_next_job)
            if ran:
                await asyncio.sleep(3)
                continue
            if enabled and (now - last_poll >= interval):
                last_poll = now
                try:
                    await poll_with_heartbeat()
                except Exception as exc:
                    Path("logs").mkdir(exist_ok=True)
                    with open("logs/poller-errors.log", "a", encoding="utf-8") as f:
                        f.write("\n--- poll cycle ---\n" + "".join(traceback.format_exception(exc)) + "\n")
            # Keep draining queued jobs independently of the GitHub poll interval:
            # implement -> verify -> merge should continue without waiting 5 minutes.
            ran = await asyncio.to_thread(process_next_job)
            await asyncio.sleep(3 if ran else 15)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(60)
