# GitHub Issue Solver

> GitHub 이슈를 주기적으로 감시하고, 코딩 에이전트에게 해결을 맡긴 뒤, 검증·머지·이슈 종료까지 자동으로 처리하는 셀프호스팅 자동화 서비스.

![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)

GitHub Issue Solver는 여러분이 관리하는 저장소의 새 이슈를 감지하면, 서버에 설치된 코딩 에이전트(`gjc`, `omx`, 또는 `claude`)에게 구현을 맡기고, 변경 사항을 브랜치로 push해 PR을 만들고, 별도의 검증 에이전트로 검증한 뒤, 통과하면 자동으로 머지하고 이슈를 닫습니다. 모든 설정과 모니터링은 모바일에서도 깔끔하게 동작하는 웹 대시보드에서 합니다.

누구나 클론해서 자신의 GitHub 토큰과 에이전트를 설정하고 바로 셀프호스팅할 수 있습니다. 토큰·계정 같은 민감 정보는 코드에 전혀 박혀 있지 않으며, 실행 시 로컬 SQLite DB에만 저장됩니다.

---

## ✨ 주요 기능

- **이슈 자동 감지** — 등록된 저장소를 폴링 주기마다 확인하고 새 이슈를 작업 큐에 등록합니다.
- **자동 구현 → PR** — 코딩 에이전트가 이슈를 해결하고, `agent/issue-<n>` 브랜치로 push한 뒤 `Fixes #N`이 포함된 PR을 생성합니다.
- **자동 검증** — 별도의 검증 에이전트가 PR을 검토하고 `PASS`/`FAIL` 판정을 내립니다.
- **자동 머지 & 종료** — 검증 통과 시 PR을 머지하고 이슈를 `completed`로 닫습니다.
- **검증 실패는 최종 상태** — `FAIL`이면 자동 재시도하지 않고 `verification_failed`로 남깁니다.
- **외부 해결 동기화** — 사람이 직접 닫거나 다른 PR로 해결한 이슈도 GitHub 실제 상태로 자동 동기화합니다.
- **저장소 자동 등록** — 개인 계정 repo는 토큰 사용자 소유 기준으로, 조직 repo는 (Audit log 또는 최초 커밋 기준으로) 본인이 만든 repo만 자동 등록합니다.
- **모바일 웹 대시보드** — 토큰/에이전트/폴링 설정, 이슈별 진행 타임라인(감지 → 구현 → 검증 → 머지/완료), 작업 로그를 어디서든 확인.
- **멀티 토큰** — 개인 계정 + 여러 조직 토큰 + 선택적 Audit 토큰을 분리 저장합니다.

## 🔄 동작 방식

```
                ┌─────────────┐   poll    ┌──────────────┐
                │  GitHub API │ ────────▶ │  Orchestrator │
                └─────────────┘           └──────┬───────┘
                                                 │ new issue
                                                 ▼
   감지됨 ──▶ implement 에이전트 ──▶ branch push ──▶ Pull Request
                                                 │
                                                 ▼
                                          verify 에이전트
                                          ┌──────┴───────┐
                                       PASS              FAIL
                                          │                │
                            auto-merge + close       verification_failed
                            (이슈 completed)          (재시도 없음)
```

폴링 루프는 백그라운드에서 계속 돌며, 작업 큐(implement → verify)를 폴링 주기와 독립적으로 비웁니다.

## 📦 요구 사항

- Python 3.10+
- 서버에 설치된 코딩 에이전트 CLI 중 하나 이상
  - [`gjc`](https://github.com/NomaDamas/gajae-code) (default) 또는
  - [`omx`](https://www.npmjs.com/package/oh-my-codex) (oh-my-codex) 또는
  - [`claude`](https://docs.anthropic.com/en/docs/claude-code) (Claude Code)
- `git`
- repo / PR / 이슈 write 권한이 있는 GitHub Personal Access Token

## 🚀 빠른 시작

```bash
git clone https://github.com/NomaDamas/github-issue-solver.git
cd github-issue-solver

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# (선택) 첫 관리자 계정 부트스트랩
cp .env.example .env
# .env에서 GIS_INITIAL_USERNAME / GIS_INITIAL_PASSWORD 수정

./run.sh
```

브라우저에서 `http://<서버주소>:8787` 접속 → 로그인 → 비밀번호 변경.

> 첫 관리자 계정: `.env`에 `GIS_INITIAL_USERNAME` / `GIS_INITIAL_PASSWORD`를 설정하지 않으면 사용자명은 `admin`, 비밀번호는 무작위 문자열로 생성됩니다(로그에 노출되지 않음). 직접 로그인하려면 `.env`로 초기 비밀번호를 지정하세요. DB가 이미 있으면 이 값은 무시됩니다.

## ⚙️ 설정 (웹 대시보드)

로그인 후 **설정** 탭에서:

| 항목 | 설명 |
|------|------|
| 개인 계정 토큰 | 개인 repo 자동 추적 및 push/PR/머지에 사용 |
| 조직 토큰 | 조직별 resource owner 토큰을 여러 개 등록 (조직 repo 접근용) |
| Audit 토큰 (선택) | `read:audit_log` 권한. 있으면 조직에서 본인이 만든 repo를 정확히 판별 |
| 구현/검증 에이전트 | `gjc`, `omx`, 또는 `claude` 선택 |
| 폴링 주기(초) | 기본 300초 (최소 30초) |
| 작업 디렉터리 | 저장소 체크아웃 위치 (기본 `workspace/`) |
| 에이전트 최대 실행 시간(초) | 기본 3600초 |

**저장소** 탭에서 감시할 repo를 직접 추가/수정하거나, 자동 등록된 목록을 관리할 수 있습니다(자동 머지/라벨 필터/감시 on-off).

## 🖥️ 배포 (systemd, 재부팅에도 유지)

```bash
# 서비스 파일의 경로를 클론 위치로 수정
cp github-issue-solver.service.example ~/.config/systemd/user/github-issue-solver.service
$EDITOR ~/.config/systemd/user/github-issue-solver.service   # WorkingDirectory/ExecStart 경로 수정

systemctl --user daemon-reload
systemctl --user enable --now github-issue-solver.service

# 로그아웃/재부팅에도 사용자 서비스가 계속 실행되도록 linger 활성화
loginctl enable-linger "$USER"
```

서비스는 `Restart=always`로 동작하며, 재부팅 후에도 자동으로 다시 시작됩니다.

## 🔒 보안 주의사항

- 토큰·비밀번호 등 모든 민감 정보는 **로컬 SQLite DB**(`github_issue_solver.db`)에만 저장되며 `.gitignore`로 커밋이 차단됩니다.
- 토큰 값은 UI나 로그에 절대 평문으로 출력되지 않습니다.
- 코딩 에이전트는 샌드박스 우회 모드로 실행되므로, **신뢰할 수 있는 환경에서만** 운영하세요.
- 공개 네트워크에 노출할 경우 리버스 프록시 + HTTPS + 접근 제한을 권장합니다.

## 🗂️ 프로젝트 구조

```
app/
  main.py           FastAPI 라우트, 인증, 설정/저장소/이슈 API
  orchestrator.py   폴링 루프, 구현/검증/머지 워크플로, 상태 동기화
  github_client.py  GitHub REST API 래퍼
  token_store.py    owner별 토큰 저장/조회
  db.py             SQLite 스키마/마이그레이션/시드
  agents.py         코딩 에이전트(gjc/omx/claude) 실행
static/             모바일 웹 대시보드 (HTML/CSS/JS)
run.sh              uvicorn 실행 스크립트
```

## 🤝 기여

이슈와 PR 환영합니다. 변경 전 다음을 확인해 주세요.

- 민감 정보(토큰/사용자명/로컬 경로)를 코드나 커밋에 포함하지 마세요.
- 백엔드는 `python -m py_compile app/*.py`, 프론트엔드는 `node --check static/app.js`로 기본 검증할 수 있습니다.

## 📄 라이선스

[Apache License 2.0](./LICENSE) © NomaDamas
