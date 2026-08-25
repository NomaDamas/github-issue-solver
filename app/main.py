from __future__ import annotations

import asyncio
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest, urlopen, build_opener, HTTPRedirectHandler

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import db, get_setting, init_db, password_hash, row_to_dict, set_setting, verify_password
from .downloads import get_download_apps, register_download_routes
from .github_client import GitHubClient, GitHubError
from .orchestrator import create_jobs_for_repo_untracked_issues, discover_open_issue_candidates, discover_repositories, poll_once, process_next_job, background_loop, recover_interrupted_jobs
from .package_lake_service import PACKAGE_LAKE_ENDPOINT, create_launch_url
from .token_store import configured_org_owners, configured_tokens, delete_owner_token, get_any_token, get_audit_token, get_owner_token, list_owner_tokens, set_owner_token

app = FastAPI(title="GitHub Issue Solver", version="0.1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
register_download_routes(app)
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
    default_implement_agent: str = "gjc"
    default_verify_agent: str = "gjc"


class RepoIn(BaseModel):
    owner: str
    name: str
    default_branch: str = "main"
    enabled: bool = True
    auto_merge: bool = True
    implement_agent: str = "gjc"
    verify_agent: str = "gjc"
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
    recover_interrupted_jobs()
    Path("static").mkdir(exist_ok=True)
    _bg_task = asyncio.create_task(background_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    if _bg_task:
        _bg_task.cancel()


@app.get("/")
def index() -> HTMLResponse:
    html = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>Markr.AI 관리 콘솔</title>
  <style>
    :root { --bg:#0b1020; --card:#141b2f; --line:#26314f; --pri:#7c3aed; --muted:#9aa4b2; --text:#eef2ff; }
    * { box-sizing:border-box; }
    body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:linear-gradient(180deg,#0b1020,#111827); color:var(--text); }
    .shell { width:min(100%, 1080px); margin:0 auto; padding:18px 14px 48px; }
    .hero, .card { background:rgba(20,27,47,.92); border:1px solid var(--line); border-radius:18px; padding:16px; box-shadow:0 10px 30px rgba(0,0,0,.18); margin:0 0 14px; }
    .hero { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
    .eyebrow { margin:0 0 8px; color:#c4b5fd; font-size:12px; letter-spacing:.12em; text-transform:uppercase; }
    h1 { margin:0 0 6px; font-size:28px; letter-spacing:-.04em; }
    h2 { margin:0 0 10px; font-size:18px; }
    p { margin:6px 0; color:var(--muted); line-height:1.5; }
    strong { color:#fff; }
    label { display:block; color:#cbd5e1; font-size:13px; margin:12px 0 8px; }
    input { width:100%; border:1px solid var(--line); background:#0f172a; color:var(--text); border-radius:12px; padding:12px; font-size:16px; margin-top:6px; }
    button, a.btn { border:0; background:var(--pri); color:white; padding:12px 14px; border-radius:12px; font-weight:800; font-size:14px; cursor:pointer; min-height:44px; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; }
    .ghost { background:transparent; border:1px solid var(--line); }
    .hidden { display:none !important; }
    .service-grid { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; }
    .service-card { background:#0f172a; border:1px solid var(--line); border-radius:14px; padding:14px; text-decoration:none; color:inherit; display:grid; gap:6px; min-height:116px; text-align:left; width:100%; }
    .service-card b { font-size:16px; }
    .service-card span { color:var(--muted); font-size:13px; line-height:1.45; }
    .service-card small { color:#a78bfa; font-size:12px; }
    .service-icon { display:grid; place-items:center; width:40px; height:40px; border-radius:12px; background:#312e81; color:#e0e7ff; font-size:13px; font-weight:900; letter-spacing:.06em; }
    .portal-summary { color:#e2e8f0; }
    .narrow { max-width:480px; margin:30px auto; }
    .top-actions { display:flex; gap:10px; align-items:center; flex-shrink:0; }
    .msg { color:#fecaca; min-height:1.5em; }
    @media (max-width: 760px) {
      .shell { padding:14px 10px 40px; }
      .hero { display:grid; gap:10px; }
      h1 { font-size:24px; }
      .service-grid { grid-template-columns:1fr; }
      button, a.btn { width:100%; }
      .top-actions { width:100%; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="hero">
      <div>
        <p class="eyebrow">Markr.AI 관리 콘솔</p>
        <h1>관리 포털</h1>
        <p>내부 운영 도구와 다운로드 센터를 한 곳에서 관리합니다.</p>
      </div>
      <div class="top-actions">
        <button id="logoutBtn" class="ghost hidden">로그아웃</button>
      </div>
    </header>

    <section id="loginView" class="card narrow">
      <h2>관리자 로그인</h2>
      <p class="hint">Markr.AI 내부 서비스 접근을 위한 인증이 필요합니다.</p>
      <label>아이디 <input id="loginUser" autocomplete="username" /></label>
      <label>비밀번호 <input id="loginPass" type="password" autocomplete="current-password" /></label>
      <button id="loginBtn">로그인</button>
      <p id="loginMsg" class="msg"></p>
    </section>

    <section id="portalView" class="hidden">
      <div class="card">
        <h2>다운로드 센터</h2>
        <p class="hint">앱별 다운로드 화면을 별도로 구성했습니다. 현재 OpenMinis, Dolshoi, LangGate APK를 제공합니다.</p>
        <div id="downloadGrid" class="service-grid"></div>
      </div>
      <div class="card">
        <h2>내부 서비스</h2>
        <p class="hint">운영 도구와 내부 상태 조회 화면입니다.</p>
        <div id="serviceGrid" class="service-grid"></div>
      </div>
      <div class="card">
        <h2>데모 서비스</h2>
        <p class="hint">Tailnet에서 운영 중인 통합 AI 플랫폼과 데모 관리 화면입니다.</p>
        <div id="demoGrid" class="service-grid"></div>
      </div>
      <div class="card">
        <h2>접속 상태</h2>
        <div id="sessionInfo" class="portal-summary">세션 확인 대기 중…</div>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const SERVICES = [
      { name: 'GitHub Issue Solver', route: '/solver', desc: '별도 GitHub Issue Solver 작업 페이지' },
      { name: 'Jikji 파일 인덱스', route: '/jikji', desc: 'Rust 기반 파일 탐색·검색·본문 미리보기·인덱스 관리' },
      { name: '정책 지원 에이전트', route: '/policy-agent', desc: '통계·공공데이터·법률 지식 네트워크 웹챗 · 내부 서비스 3123' },
      { name: 'Marker Monitor', route: '/monitor', desc: '마커 연구원 작업 모니터링 · 내부 서비스 8799' },
      { name: 'Bid Monitoring', route: '/bid-monitor', desc: 'AI 공공사업 입찰·지원사업 모니터링 대시보드 · 내부 서비스 8800' },
      { name: '관리 콘솔 API', route: '/api/me', desc: '로그인 세션 확인 및 내부 상태 조회' },
      { name: '작업 대시보드 API', route: '/api/dashboard', desc: '저장소·이슈·작업 런타임 상태' },
    ];
    const DEMO_SERVICES = [
      { name: 'Company Agent 통합 콘솔', url: 'https://cheol-nucbox-evo-x2.taildf528d.ts.net', desc: 'Dify·Keycloak·Phoenix·Milvus·vLLM 통합 운영 콘솔' },
      { name: 'Dify 앱 스튜디오', url: 'https://cheol-nucbox-evo-x2.taildf528d.ts.net:8444/', desc: '앱·워크플로·지식 파이프라인 제작 및 배포' },
      { name: 'vLLM API 문서', url: 'https://cheol-nucbox-evo-x2.taildf528d.ts.net:8445/docs', desc: 'Qwen OpenAI 호환 추론 API 문서와 상태 확인' },
      { name: 'Package Lake', url: '/api/services/package-lake/launch', endpoint: '__PACKAGE_LAKE_ENDPOINT__', icon: 'PL', desc: '폐쇄망 패키지 골든 카탈로그, 반입 요청, 승인 및 검사 증거를 안전한 합성 데이터로 체험합니다.' },
    ];
    const DOWNLOAD_APPS = __DOWNLOAD_APPS__;

    async function api(path, opts = {}) {
      const res = await fetch(path, { headers: {'Content-Type':'application/json'}, ...opts });
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(body.detail || res.statusText);
      }
      return res.json();
    }

    function renderServices() {
      $('serviceGrid').innerHTML = SERVICES.map((s) => `
        <a class="service-card" href="${s.route}" ${s.route.startsWith('http') ? 'target="_blank" rel="noopener"' : ''}>
          <b>${s.name}</b>
          <span>${s.desc}</span>
          <small>${s.route}</small>
        </a>
      `).join('');
    }

    function renderDemos() {
      $('demoGrid').innerHTML = DEMO_SERVICES.map((s) => `
        <${s.url.startsWith('/api/') ? 'button' : 'a'} class="service-card" ${s.url.startsWith('/api/') ? `type="button" data-launch="${s.url}"` : `href="${s.url}" target="_blank" rel="noopener"`}>
          ${s.icon ? `<span class="service-icon" aria-hidden="true">${s.icon}</span>` : ''}
          <b>${s.name}</b>
          <span>${s.desc}</span>
          <small>${s.endpoint || s.url}</small>
          ${s.url.startsWith('/api/') ? '<small>MarkerAI 관리 콘솔 SSO · USER 최소 권한</small>' : ''}
        </${s.url.startsWith('/api/') ? 'button' : 'a'}>
      `).join('');
      document.querySelectorAll('[data-launch]').forEach((button) => button.onclick = async () => {
        const result = await api(button.dataset.launch, { method: 'POST' });
        location.assign(result.launch_url);
      });
    }

    function renderDownloads() {
      $('downloadGrid').innerHTML = DOWNLOAD_APPS.map((app) => `
        <a class="service-card" href="${app.url}" target="_blank" rel="noopener">
          <b>${app.name}</b>
          <span>${app.description}</span>
          <small>${app.platform} · v${app.version} · ${app.size}</small>
          <small>SHA-256 ${app.sha256}</small>
        </a>
      `).join('');
    }

    async function syncAuth() {
      try {
        const me = await api('/api/me');
        $('loginView').classList.add('hidden');
        $('portalView').classList.remove('hidden');
        $('logoutBtn').classList.remove('hidden');
        $('sessionInfo').textContent = `로그인됨 · ${me.username}${me.must_change_password ? ' · 비밀번호 변경 필요' : ''}`;
      } catch {
        $('loginView').classList.remove('hidden');
        $('portalView').classList.add('hidden');
        $('logoutBtn').classList.add('hidden');
      }
    }

    $('loginBtn').onclick = async () => {
      try {
        await api('/api/login', { method: 'POST', body: JSON.stringify({ username: $('loginUser').value, password: $('loginPass').value }) });
        await syncAuth();
      } catch (e) { $('loginMsg').textContent = e.message; }
    };
    $('logoutBtn').onclick = async () => { await api('/api/logout', { method: 'POST' }); location.reload(); };
    renderServices();
    renderDownloads();
    renderDemos();
    syncAuth();
  </script>
</body>
</html>"""
    html = html.replace("__DOWNLOAD_APPS__", json.dumps(get_download_apps(), ensure_ascii=False)).replace("__PACKAGE_LAKE_ENDPOINT__", PACKAGE_LAKE_ENDPOINT)
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


MONITOR_UPSTREAM = "http://127.0.0.1:8799"
BID_MONITOR_UPSTREAM = "http://127.0.0.1:8800"
JIKJI_UPSTREAM = "http://127.0.0.1:18768"


def _rewrite_jikji_html(html: str) -> str:
    """Scope root-relative browser URLs to the mounted /jikji prefix once."""
    # Keep absolute URLs, protocol-relative URLs, and URLs already scoped to
    # this mount untouched. The Rust UI uses both quoted attributes and fetch.
    root_url = re.compile(r"([\"'`(=])/(?!/|jikji(?:/|[\"'`?#)]|$))")
    html = root_url.sub(r"\1/jikji/", html)
    # Template-built API paths are root-relative in the embedded SPA.
    html = html.replace("const url = `${path}?${params(values)}`;", "const url = `/jikji${path}?${params(values)}`;")
    html = html.replace("location.assign(`/download?", "location.assign(`/jikji/download?")
    return html


def _fetch_jikji_upstream(path: str = "", method: str = "GET", body: bytes | None = None, content_type: str | None = None) -> tuple[int, dict[str, str], bytes]:
    url = f"{JIKJI_UPSTREAM}/{path.lstrip('/')}" if path else JIKJI_UPSTREAM
    headers = {"User-Agent": "Markr-Console/jikji-proxy"}
    if content_type:
        headers["Content-Type"] = content_type
    req = URLRequest(url, data=body, method=method, headers=headers)
    try:
        with urlopen(req, timeout=30) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}, exc.read()
    except URLError as exc:
        raise HTTPException(502, f"Jikji upstream unavailable: {exc.reason}") from exc


def _jikji_response(status: int, headers: dict[str, str], payload: bytes) -> Response:
    """Return upstream status/body and content type without FastAPI coercion."""
    response_headers = {
        key: value
        for key, value in headers.items()
        if key.lower() not in {"content-length", "transfer-encoding", "connection"}
    }
    content_type = headers.get("content-type", "application/octet-stream")
    if "text/html" in content_type.lower():
        response_headers.setdefault("cache-control", "no-store, must-revalidate")
        payload = _rewrite_jikji_html(payload.decode("utf-8", "replace")).encode("utf-8")
    return Response(content=payload, status_code=status, headers=response_headers, media_type=None)


POLICY_AGENT_UPSTREAM = "http://127.0.0.1:3123"




def _rewrite_monitor_html(html: str) -> str:
    return (
        html.replace('fetch("/api/data")', 'fetch("/monitor/api/data")')
        .replace("fetch('/api/data')", "fetch('/monitor/api/data')")
        .replace('fetch("/api/refresh",{method:"POST"})', 'fetch("/monitor/api/refresh",{method:"POST"})')
        .replace("fetch('/api/refresh',{method:'POST'})", "fetch('/monitor/api/refresh',{method:'POST'})")
        .replace('href="/', 'href="/monitor/')
        .replace('src="/', 'src="/monitor/')
        .replace('action="/', 'action="/monitor/')
    )


def _rewrite_bid_monitor_html(html: str) -> str:
    replacements = (
        ('href="/', 'href="/bid-monitor/'),
        ("href='/", "href='/bid-monitor/"),
        ('src="/', 'src="/bid-monitor/'),
        ("src='/", "src='/bid-monitor/"),
        ('action="/', 'action="/bid-monitor/'),
        ("action='/", "action='/bid-monitor/"),
        ('fetch("/', 'fetch("/bid-monitor/'),
        ("fetch('/", "fetch('/bid-monitor/"),
    )
    for old, new in replacements:
        html = html.replace(old, new)
    return html


def _rewrite_policy_agent_html(html: str) -> str:
    replacements = (
        ('href="/', 'href="/policy-agent/'),
        ("href='/", "href='/policy-agent/"),
        ('src="/', 'src="/policy-agent/'),
        ("src='/", "src='/policy-agent/"),
        ('action="/', 'action="/policy-agent/'),
        ("action='/", "action='/policy-agent/"),
        ('fetch("/', 'fetch("/policy-agent/'),
        ("fetch('/", "fetch('/policy-agent/"),
    )
    for old, new in replacements:
        html = html.replace(old, new)
    return html


def _fetch_policy_agent_upstream(path: str = "", method: str = "GET", body: bytes | None = None, content_type: str | None = None) -> tuple[int, dict[str, str], bytes]:
    url = f"{POLICY_AGENT_UPSTREAM}/{path.lstrip('/')}" if path else POLICY_AGENT_UPSTREAM
    headers = {"User-Agent": "Markr-Console/policy-agent-proxy"}
    if content_type:
        headers["Content-Type"] = content_type
    req = URLRequest(url, data=body, method=method, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}, exc.read()
    except URLError as exc:
        raise HTTPException(502, f"policy agent upstream unavailable: {exc.reason}") from exc


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        # Do not follow redirects: the bid-monitor backend sets the session
        # cookie on its 3xx login response, which must reach the browser.
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirect)

@app.get("/jikji")
def jikji() -> Response:
    status, headers, body = _fetch_jikji_upstream()
    return _jikji_response(status, headers, body)


@app.api_route("/jikji/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def jikji_proxy(path: str, request: Request) -> Response:
    body = await request.body()
    upstream_path = f"{path}?{request.url.query}" if request.url.query else path
    status, headers, payload = _fetch_jikji_upstream(
        upstream_path,
        method=request.method.upper(),
        body=body or None,
        content_type=request.headers.get("content-type"),
    )
    return _jikji_response(status, headers, payload)


def _fetch_bid_monitor_upstream(path: str = "", method: str = "GET", body: bytes | None = None, content_type: str | None = None, cookie: str | None = None, authorization: str | None = None) -> tuple[int, dict[str, str], bytes]:
    url = f"{BID_MONITOR_UPSTREAM}/{path.lstrip('/')}" if path else BID_MONITOR_UPSTREAM
    headers = {"User-Agent": "Markr-Console/bid-monitor-proxy"}
    if content_type:
        headers["Content-Type"] = content_type
    if cookie:
        headers["Cookie"] = cookie
    if authorization:
        headers["Authorization"] = authorization
    req = URLRequest(url, data=body, method=method, headers=headers)
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=30) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}, exc.read()
    except URLError as exc:
        raise HTTPException(502, f"bid monitor upstream unavailable: {exc.reason}") from exc



def _fetch_monitor_upstream(path: str = "", method: str = "GET", body: bytes | None = None, content_type: str | None = None) -> tuple[int, dict[str, str], bytes]:
    url = f"{MONITOR_UPSTREAM}/{path.lstrip('/')}" if path else MONITOR_UPSTREAM
    headers = {"User-Agent": "GitHub-Issue-Solver/monitor-proxy"}
    if content_type:
        headers["Content-Type"] = content_type
    req = URLRequest(url, data=body, method=method, headers=headers)
    try:
        with urlopen(req, timeout=20) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}, exc.read()
    except URLError as exc:
        raise HTTPException(502, f"monitor upstream unavailable: {exc.reason}") from exc


@app.get("/solver")
def solver() -> HTMLResponse:
    return HTMLResponse(Path("static/index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/policy-agent")
def policy_agent() -> HTMLResponse:
    status, headers, body = _fetch_policy_agent_upstream()
    ctype = headers.get("content-type", "")
    if status >= 400 or "html" not in ctype.lower():
        if status >= 400:
            raise HTTPException(status, "policy agent upstream unavailable")
        raise HTTPException(502, f"policy agent upstream did not return HTML (got {ctype or 'unknown'})")
    return HTMLResponse(_rewrite_policy_agent_html(body.decode("utf-8", "replace")), status_code=status, headers={"Cache-Control": "no-store, must-revalidate"})


@app.api_route("/policy-agent/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def policy_agent_proxy(path: str, request: Request) -> Response:
    body = await request.body()
    upstream_path = f"{path}?{request.url.query}" if request.url.query else path
    status, headers, payload = _fetch_policy_agent_upstream(upstream_path, method=request.method.upper(), body=body or None, content_type=request.headers.get("content-type"))
    ctype = headers.get("content-type", "application/octet-stream")
    if status >= 400:
        detail = payload.decode("utf-8", "replace") if payload else "policy agent upstream error"
        raise HTTPException(status, detail[:200])
    if "text/html" in ctype.lower():
        return HTMLResponse(_rewrite_policy_agent_html(payload.decode("utf-8", "replace")), status_code=status, headers={"Cache-Control": "no-store, must-revalidate"})
    return Response(content=payload, status_code=status, media_type=ctype)

@app.get("/monitor")
def monitor() -> HTMLResponse:
    status, headers, body = _fetch_monitor_upstream()
    ctype = headers.get("content-type", "")
    if status >= 400 or "html" not in ctype.lower():
        if status >= 400:
            raise HTTPException(status, "monitor upstream unavailable")
        raise HTTPException(502, f"monitor upstream did not return HTML (got {ctype or 'unknown'})")
    return HTMLResponse(_rewrite_monitor_html(body.decode("utf-8", "replace")), status_code=status, headers={"Cache-Control": "no-store, must-revalidate"})


@app.api_route("/monitor/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def monitor_proxy(path: str, request: Request) -> Response:
    body = await request.body()
    status, headers, payload = _fetch_monitor_upstream(path, method=request.method.upper(), body=body or None, content_type=request.headers.get("content-type"))
    ctype = headers.get("content-type", "application/octet-stream")
    if status >= 400:
        detail = payload.decode("utf-8", "replace") if payload else "monitor upstream error"
        raise HTTPException(status, detail[:200])
    if "text/html" in ctype.lower():
        return HTMLResponse(_rewrite_monitor_html(payload.decode("utf-8", "replace")), status_code=status, headers={"Cache-Control": "no-store, must-revalidate"})
    return Response(content=payload, status_code=status, media_type=ctype)


@app.get("/bid-monitor")
def bid_monitor() -> HTMLResponse:
    status, headers, body = _fetch_bid_monitor_upstream()
    ctype = headers.get("content-type", "")
    if status >= 400 or "html" not in ctype.lower():
        if status >= 400:
            raise HTTPException(status, "bid monitor upstream unavailable")
        raise HTTPException(502, f"bid monitor upstream did not return HTML (got {ctype or 'unknown'})")
    response = HTMLResponse(_rewrite_bid_monitor_html(body.decode("utf-8", "replace")), status_code=status, headers={"Cache-Control": "no-store, must-revalidate"})
    if set_cookie := headers.get("set-cookie"):
        response.headers["set-cookie"] = set_cookie
    return response


@app.api_route("/bid-monitor/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def bid_monitor_proxy(path: str, request: Request) -> Response:
    body = await request.body()
    # Preserve the query string so filters/sort/pagination reach the backend.
    upstream_path = f"{path}?{request.url.query}" if request.url.query else path
    status, headers, payload = _fetch_bid_monitor_upstream(
        upstream_path,
        method=request.method.upper(),
        body=body or None,
        content_type=request.headers.get("content-type"),
        cookie=request.headers.get("cookie"),
        authorization=request.headers.get("authorization"),
    )
    ctype = headers.get("content-type", "application/octet-stream")
    response_headers = {"Cache-Control": "no-store, must-revalidate"}
    if set_cookie := headers.get("set-cookie"):
        response_headers["set-cookie"] = set_cookie
    if 300 <= status < 400 and (location := headers.get("location")):
        if location.startswith("/"):
            location = "/bid-monitor" + location
        response_headers["location"] = location
        return Response(content=payload, status_code=status, headers=response_headers)
    if status >= 400:
        detail = payload.decode("utf-8", "replace") if payload else "bid monitor upstream error"
        raise HTTPException(status, detail[:200])
    if "text/html" in ctype.lower():
        return HTMLResponse(_rewrite_bid_monitor_html(payload.decode("utf-8", "replace")), status_code=status, headers=response_headers)
    return Response(content=payload, status_code=status, media_type=ctype, headers=response_headers)


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


@app.post("/api/services/package-lake/launch")
def launch_package_lake(user: dict = Depends(require_user)) -> dict:
    try:
        return {"launch_url": create_launch_url(user["username"])}
    except (OSError, RuntimeError) as exc:
        raise HTTPException(503, "Package Lake SSO launch is not configured") from exc


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
            "default_implement_agent": get_setting(conn, "default_implement_agent", "gjc"),
            "default_verify_agent": get_setting(conn, "default_verify_agent", "gjc"),
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
    if name not in {"gjc", "omx", "claude"}:
        raise HTTPException(400, "agent는 gjc, omx 또는 claude만 지원합니다")
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
        impl = validate_agent(data.implement_agent or get_setting(conn, "default_implement_agent", "gjc"))
        verify = validate_agent(data.verify_agent or get_setting(conn, "default_verify_agent", "gjc"))
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
