from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class GitHubError(Exception):
    status: int
    message: str
    headers: dict[str, str] | None = None

    def __str__(self) -> str:
        return f"GitHub API {self.status}: {self.message}"


class GitHubClient:
    def __init__(self, token: str):
        self.token = token.strip()
        if not self.token:
            raise GitHubError(401, "GitHub token is not configured")

    def request(self, method: str, path: str, data: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> Any:
        if params:
            path += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = "https://api.github.com" + path
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, method=method.upper())
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode()
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                msg = json.loads(raw).get("message", raw)
            except Exception:
                msg = raw
            raise GitHubError(e.code, msg, {k.lower(): v for k, v in e.headers.items()}) from e

    def request_with_headers(self, method: str, path: str, data: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
        if params:
            path += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = "https://api.github.com" + path
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, method=method.upper())
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode()
                parsed = None if not raw else json.loads(raw)
                return parsed, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                msg = json.loads(raw).get("message", raw)
            except Exception:
                msg = raw
            raise GitHubError(e.code, msg, {k.lower(): v for k, v in e.headers.items()}) from e

    @staticmethod
    def _last_page_from_link(link_header: str) -> int | None:
        for part in link_header.split(","):
            if 'rel="last"' in part:
                match = re.search(r"[?&]page=(\d+)", part)
                if match:
                    return int(match.group(1))
        return None


    def authenticated_user(self) -> dict[str, Any]:
        return self.request("GET", "/user")

    def oauth_status(self) -> dict[str, Any]:
        data, headers = self.request_with_headers("GET", "/user")
        return {
            "login": (data or {}).get("login"),
            "scopes": headers.get("x-oauth-scopes", ""),
            "accepted_scopes": headers.get("x-accepted-oauth-scopes", ""),
        }

    def list_accessible_repos(self, owners: set[str]) -> list[dict[str, Any]]:
        """Return private/public repositories accessible to the token for selected owners.

        Uses /user/repos so org-private repositories are included when the token has access.
        """
        owners_lower = {o.lower() for o in owners if o.strip()}
        found: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while True:
            items = self.request(
                "GET",
                "/user/repos",
                params={
                    "affiliation": "owner,organization_member,collaborator",
                    "visibility": "all",
                    "sort": "full_name",
                    "direction": "asc",
                    "per_page": per_page,
                    "page": page,
                },
            )
            if not items:
                break
            for repo in items:
                owner = ((repo.get("owner") or {}).get("login") or "").lower()
                if owner in owners_lower and not repo.get("archived") and not repo.get("fork"):
                    found.append(repo)
            if len(items) < per_page:
                break
            page += 1
        return found

    def repo(self, owner: str, name: str) -> dict[str, Any]:
        return self.request("GET", f"/repos/{owner}/{name}")
    def audit_log_access_status(self, org: str) -> dict[str, Any]:
        try:
            items, headers = self.request_with_headers("GET", f"/orgs/{org}/audit-log", params={"per_page": 1})
            return {
                "org": org,
                "ok": True,
                "status": 200,
                "message": "Audit log accessible",
                "sample_count": len(items or []),
                "scopes": headers.get("x-oauth-scopes", ""),
                "accepted_scopes": headers.get("x-accepted-oauth-scopes", ""),
                "diagnosis": "생성자 자동 판별 가능",
            }
        except GitHubError as exc:
            headers = exc.headers or {}
            scopes = headers.get("x-oauth-scopes", "")
            if exc.status == 404 and "read:audit_log" in scopes:
                diagnosis = "토큰 권한은 있으나 이 조직의 audit-log API가 GitHub에서 404입니다. 생성자 자동 판별 불가."
            elif "read:audit_log" not in scopes:
                diagnosis = "classic token에 read:audit_log 권한이 없습니다."
            else:
                diagnosis = "audit-log API 접근 실패"
            return {
                "org": org,
                "ok": False,
                "status": exc.status,
                "message": exc.message,
                "sample_count": 0,
                "scopes": scopes,
                "accepted_scopes": headers.get("x-accepted-oauth-scopes", ""),
                "diagnosis": diagnosis,
            }


    def org_repos_created_by_login(self, org: str, login: str, max_pages: int = 10) -> set[str]:
        """Return repo names from org audit log repo.create events by login."""
        names: set[str] = set()
        phrase = f"action:repo.create actor:{login}"
        for page in range(1, max_pages + 1):
            items = self.request("GET", f"/orgs/{org}/audit-log", params={"phrase": phrase, "per_page": 100, "page": page})
            if not items:
                break
            for item in items:
                repo = item.get("repo") or ""
                if item.get("action") == "repo.create" and (item.get("actor") or "").lower() == login.lower() and repo.lower().startswith(org.lower() + "/"):
                    names.add(repo.split("/", 1)[1])
            if len(items) < 100:
                break
        return names


    def org_repo_create_actor_is_login(self, org: str, repo_full_name: str, login: str) -> bool | None:
        """Return True/False from org audit log, or None if audit log is unavailable.

        GitHub's repository API does not expose a creator field. For org repos,
        repo.create audit-log events are the closest exact source when accessible.
        """
        phrase = f"action:repo.create repo:{repo_full_name}"
        try:
            items = self.request(
                "GET",
                f"/orgs/{org}/audit-log",
                params={"phrase": phrase, "per_page": 10},
            )
        except GitHubError as exc:
            if exc.status in {401, 403, 404}:
                return None
            raise
        for item in items or []:
            if (item.get("action") == "repo.create" and item.get("repo") == repo_full_name and (item.get("actor") or "").lower() == login.lower()):
                return True
            if item.get("action") == "repo.create" and item.get("repo") == repo_full_name:
                return False
        return False


    def oldest_commit(self, owner: str, name: str, default_branch: str) -> dict[str, Any] | None:
        params = {"per_page": 1, "sha": default_branch}
        try:
            items, headers = self.request_with_headers("GET", f"/repos/{owner}/{name}/commits", params=params)
        except GitHubError as exc:
            # 409 usually means an empty repository. Empty/unknown repos are not auto-registered.
            if exc.status == 409:
                return None
            raise
        if not items:
            return None
        last_page = self._last_page_from_link(headers.get("link", ""))
        if last_page and last_page > 1:
            items, _headers = self.request_with_headers("GET", f"/repos/{owner}/{name}/commits", params={"per_page": 1, "sha": default_branch, "page": last_page})
            if not items:
                return None
        return items[0]

    def repository_first_commit_by_login(self, owner: str, name: str, default_branch: str, login: str) -> bool:
        commit = self.oldest_commit(owner, name, default_branch)
        if not commit:
            return False
        target = login.lower()
        author_login = ((commit.get("author") or {}).get("login") or "").lower()
        committer_login = ((commit.get("committer") or {}).get("login") or "").lower()
        return target in {author_login, committer_login}


    def search_open_issues_for_owner(self, owner: str, pages: int = 5) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            result = self.request(
                "GET",
                "/search/issues",
                params={
                    "q": f"is:issue is:open archived:false user:{owner}",
                    "sort": "created",
                    "order": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            items = result.get("items") or []
            found.extend(items)
            if len(items) < 100:
                break
        return found


    def list_issues(self, owner: str, name: str, labels: str = "") -> list[dict[str, Any]]:
        params = {"state": "open", "per_page": 100, "sort": "created", "direction": "asc"}
        if labels.strip():
            params["labels"] = labels.strip()
        items = self.request("GET", f"/repos/{owner}/{name}/issues", params=params)
        return [i for i in items if "pull_request" not in i]

    def create_issue_comment(self, owner: str, name: str, number: int, body: str) -> None:
        self.request("POST", f"/repos/{owner}/{name}/issues/{number}/comments", {"body": body})

    def create_pull_request(self, owner: str, name: str, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        return self.request("POST", f"/repos/{owner}/{name}/pulls", {"title": title, "head": head, "base": base, "body": body})

    def merge_pull_request(self, owner: str, name: str, number: int, commit_title: str) -> dict[str, Any]:
        return self.request("PUT", f"/repos/{owner}/{name}/pulls/{number}/merge", {"commit_title": commit_title, "merge_method": "squash"})

    def get_pull_request(self, owner: str, name: str, number: int) -> dict[str, Any]:
        return self.request("GET", f"/repos/{owner}/{name}/pulls/{number}")

    def get_issue(self, owner: str, name: str, number: int) -> dict[str, Any]:
        return self.request("GET", f"/repos/{owner}/{name}/issues/{number}")

    def close_issue_completed(self, owner: str, name: str, number: int) -> dict[str, Any]:
        return self.request("PATCH", f"/repos/{owner}/{name}/issues/{number}", {"state": "closed", "state_reason": "completed"})

    def close_issue(self, owner: str, name: str, number: int) -> dict[str, Any]:
        return self.request("PATCH", f"/repos/{owner}/{name}/issues/{number}", {"state": "closed"})
