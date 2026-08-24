# Jikji 내부 관리 콘솔 연동

## 목표

Markr.AI 관리 콘솔(`http://100.90.206.112:8787/`)의 기존 내부 서비스 메뉴와 인증 경계를 유지하면서 Rust Jikji GUI를 `/jikji`에 연결한다.

## 구조

- Jikji upstream: `http://127.0.0.1:18768`
- Markr.AI 메뉴: **Jikji 파일 인덱스** → `/jikji`
- 프록시: `/jikji/{path}`가 동일 HTTP method/body/query/content-type으로 upstream에 전달한다.
- HTML 내부의 root-relative API/asset/form 경로는 `/jikji/` prefix로 재작성한다.
- Jikji는 loopback에만 bind하며, Tailnet 접근은 기존 Markr.AI 콘솔 경계를 통해서만 가능하다.
- Jikji mutation API의 manage token 검증은 Rust upstream에서 계속 수행한다.

## 제공 기능

- 파일 탐색기와 `find` 검색
- 본문 미리보기 및 검색어 하이라이트
- 중앙 SQLite root/파일/index health 현황
- refresh/reindex/remove/deep-index 관리
- 미디어·압축 상세 인덱싱 root 관리

## 배포 및 검증

```bash
systemctl --user restart github-issue-solver
curl http://100.90.206.112:8787/              # Jikji 파일 인덱스 메뉴
curl http://100.90.206.112:8787/jikji          # Jikji Library HTML
curl http://100.90.206.112:8787/jikji/api/status
curl http://100.90.206.112:8787/jikji/api/roots
```

Jikji GUI 프로세스는 `/home/cheol/.local/bin/jikji gui /tmp --host 127.0.0.1 --port 18768 --no-open`으로 유지한다.
