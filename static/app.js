const $ = (id) => document.getElementById(id);

const api = async (path, opts = {}) => {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
};

function toast(msg) {
  $('toast').textContent = msg;
  $('toast').classList.remove('hidden');
  setTimeout(() => $('toast').classList.add('hidden'), 3500);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function badge(s) { return `<span class="badge ${esc(s)}">${esc(s)}</span>`; }
function repoKey(owner, name) { return `${owner}/${name}`; }
function fmtTime(s) { return s ? String(s).replace('T', ' ').replace('Z', '') : '-'; }
function shortTime(s) { return fmtTime(s).replace(/\.\d+/, '').replace(/\+.*$/, ''); }

function stageState(i) {
  const completed = ['merged', 'resolved'].includes(i.status); // 이슈가 실제로 해결/완료에 도달
  const external = i.status === 'resolved';                     // 우리 파이프라인 밖에서 완료됨
  const closedUnplanned = i.status === 'closed';
  return {
    completed, external, closedUnplanned,
    implemented: completed || i.implement_job_status === 'completed' || ['verifying', 'verified'].includes(i.status),
    implementFailed: !completed && (i.implement_job_status === 'failed' || i.status === 'failed'),
    verified: completed || i.verdict === 'PASS' || i.status === 'verified',
    verifyFailed: !completed && (i.verdict === 'FAIL' || i.status === 'verification_failed'),
    finalDone: completed || closedUnplanned,
  };
}

function stageClass(done, fail, active) {
  return fail ? 'fail' : done ? 'done' : active ? 'active' : '';
}

function mergeCloseText(i) {
  return i.status === 'merged' ? 'completed · 자동 머지'
    : i.status === 'resolved' ? 'completed · 외부에서 해결됨'
    : i.status === 'closed' ? 'closed · not planned'
    : i.status === 'verification_failed' ? '검증 실패 · 자동 재시도 없음'
    : i.status === 'failed' ? '작업 실패'
    : '-';
}

function implementLine(i, st) {
  if (st.external && i.implement_job_status !== 'completed') return '외부에서 처리됨';
  const status = i.implement_job_status || (i.status === 'queued' ? 'queued' : '-');
  return `${esc(status)}${i.implemented_at ? ` · ${esc(shortTime(i.implemented_at))}` : ''}`;
}

function verifyLine(i, st) {
  if (st.external) return '외부에서 해결됨';
  if (st.closedUnplanned) return 'not planned로 닫힘';
  const status = i.verify_job_status || '-';
  return `${esc(status)}${i.verdict ? ` · ${esc(i.verdict)}` : ''}${i.verified_at ? ` · ${esc(shortTime(i.verified_at))}` : ''}`;
}

function issueStage(i) {
  const st = stageState(i);
  return `
    <div class="steps">
      <span class="step done">감지됨</span>
      <span class="step ${stageClass(st.implemented, st.implementFailed, i.status === 'implementing')}">구현됨</span>
      <span class="step ${stageClass(st.verified, st.verifyFailed, i.status === 'verifying')}">검증됨</span>
      <span class="step ${st.finalDone ? 'done' : ''}">머지/완료</span>
    </div>`;
}

function issueTimeline(i) {
  const st = stageState(i);
  const implSummary = i.implement_summary ? `<pre class="mini-log">${esc(i.implement_summary)}</pre>` : '';
  const verifySummary = i.verify_summary ? `<pre class="mini-log">${esc(i.verify_summary)}</pre>` : '';
  return `
    <div class="timeline">
      <div class="timeline-row">
        <div class="dot done"></div>
        <div class="bubble"><b>감지됨</b><span>${esc(shortTime(i.created_at))}</span></div>
      </div>
      <div class="timeline-row">
        <div class="dot ${stageClass(st.implemented, st.implementFailed, i.status === 'implementing')}"></div>
        <div class="bubble has-log"><div><b>implement</b><span>${implementLine(i, st)}</span></div>${implSummary}</div>
      </div>
      <div class="timeline-row">
        <div class="dot ${stageClass(st.verified, st.verifyFailed, i.status === 'verifying')}"></div>
        <div class="bubble has-log"><div><b>verify</b><span>${verifyLine(i, st)}</span></div>${st.external ? '' : verifySummary}</div>
      </div>
      <div class="timeline-row">
        <div class="dot ${st.finalDone ? 'done' : ''}"></div>
        <div class="bubble"><b>merge / close</b><span>${mergeCloseText(i)}</span></div>
      </div>
    </div>`;
}

async function showApp() {
  const me = await api('/api/me');
  $('loginView').classList.add('hidden');
  $('appView').classList.remove('hidden');
  $('logoutBtn').classList.remove('hidden');
  $('topSyncBtn').classList.remove('hidden');
  $('mustChange').classList.toggle('hidden', !me.must_change_password);
  await loadAll();
}

async function loadAll() { await Promise.all([loadDashboard(), loadRepos(), loadSettings()]); }

async function loadDashboard() {
  const [d, jobs, issues] = await Promise.all([api('/api/dashboard'), api('/api/jobs'), api('/api/issues')]);
  const r = d.runtime || {};
  const owners = (d.owners || []).map(o => `${esc(o.owner)} (${esc(o.enabled_repos)}/${esc(o.repos)})`).join(' · ') || '없음';
  $('runtime').innerHTML = `
    <div class="item-head"><b>서비스 상태</b>${badge(r.polling_enabled ? '자동 감시 ON' : '자동 감시 OFF')}</div>
    <div class="meta">
      <span>감시 대상: ${owners}</span>
      <span>최근 루프: ${esc(fmtTime(r.last_loop_heartbeat_at))}</span>
      <span>최근 이슈 확인: ${esc(fmtTime(r.last_poll_finished_at))}</span>
      <span>결과: ${esc(r.last_poll_result || '-')}</span>
    </div>`;

  const issueStats = Object.entries(d.issues).map(([k, v]) => `${k}: ${v}`).join('<br>') || '없음';
  const jobStats = Object.entries(d.jobs).map(([k, v]) => `${k}: ${v}`).join('<br>') || '없음';
  $('stats').innerHTML = `
    <div class="stat">저장소<b>${d.enabled_repos}/${d.repos}</b><span>감시 중 / 전체</span></div>
    <div class="stat">이슈 상태<br><b class="small-stat">${issueStats}</b></div>
    <div class="stat">작업 상태<br><b class="small-stat">${jobStats}</b></div>`;

  const rank = { queued: 1, implementing: 2, verifying: 3, verification_failed: 4, failed: 5, verified: 8, merged: 9, resolved: 10, closed: 11 };
  const sortedIssues = [...issues].sort((a, b) => (rank[a.status] ?? 6) - (rank[b.status] ?? 6) || String(b.created_at).localeCompare(String(a.created_at)));
  $('issues').innerHTML = sortedIssues.map(i => `
    <div class="item">
      <div class="item-head">
        <b>${esc(i.owner)}/${esc(i.repo_name)} #${i.number}</b>
        <div class="badges">${badge(i.status)} ${i.verdict && !['resolved', 'closed'].includes(i.status) ? badge(i.verdict) : ''}</div>
      </div>
      ${issueStage(i)}
      <div class="title">${esc(i.title)}</div>
      ${issueTimeline(i)}
      <div class="card-actions">
        <a class="button-link" href="${esc(i.html_url)}" target="_blank">GitHub</a>
        ${i.pr_url ? `<a class="button-link" href="${esc(i.pr_url)}" target="_blank">PR #${esc(i.pr_number)}</a>` : ''}
      </div>
    </div>`).join('') || '<p class="hint">이슈 없음</p>';

  $('jobs').innerHTML = jobs.slice(0, 20).map(j => `
    <div class="item">
      <div class="item-head"><b>#${j.id} ${esc(j.type)} · ${esc(j.owner)}/${esc(j.repo_name)}</b><div class="badges">${badge(j.status)} ${j.verdict ? badge(j.verdict) : ''}</div></div>
      <div class="title">${esc(j.title)}</div>
      <div class="meta"><span>issue #${j.issue_number}</span><span>${esc(j.agent)}</span><span>${esc(fmtTime(j.created_at))}</span></div>
      <div class="card-actions">
        ${j.pr_url ? `<a class="button-link" href="${esc(j.pr_url)}" target="_blank">PR #${j.pr_number}</a>` : ''}
        <button data-action="show-log" data-id="${j.id}" class="secondary">로그</button>
      </div>
      ${j.error ? `<pre>${esc(j.error.slice(0, 1200))}</pre>` : ''}
    </div>`).join('') || '<p class="hint">작업 없음</p>';
}

async function showLog(id) {
  const j = await api(`/api/jobs/${id}`);
  alert((j.log || '(log empty)').slice(-12000));
}

async function loadRepos() {
  const repos = await api('/api/repos');
  window._repos = repos;
  $('repoList').innerHTML = repos.map(r => `
    <div class="item ${!r.enabled ? 'needs-action' : ''}">
      <div class="item-head">
        <b>${esc(r.owner)}/${esc(r.name)}</b>
        <div class="badges">${badge(r.enabled ? 'enabled' : 'disabled')} ${badge(r.auto_merge ? 'auto-merge' : 'manual-merge')} ${r.auto_discovered ? badge('auto') : ''}</div>
      </div>
      <div class="meta">
        <span>GitHub 업데이트 ${esc(fmtTime(r.github_pushed_at || r.github_updated_at || r.updated_at))}</span>
        <span>서비스 갱신 ${esc(fmtTime(r.updated_at))}</span>
        <span>base ${esc(r.default_branch)}</span>
        <span>labels ${esc(r.issue_labels || '-')}</span>
      </div>
      <div class="card-actions">
        <button data-action="fill-repo" data-repo-id="${r.id}" class="secondary">편집</button>
        <button data-action="delete-repo" data-repo-id="${r.id}" class="danger">삭제</button>
      </div>
    </div>`).join('') || '<p class="hint">등록된 저장소 없음</p>';
}

function repoForm() {
  return {
    owner: $('repoOwner').value,
    name: $('repoName').value,
    default_branch: $('repoBranch').value || 'main',
    enabled: $('repoEnabled').checked,
    auto_merge: $('repoMerge').checked,
    implement_agent: $('defaultImpl')?.value || 'gjc',
    verify_agent: $('defaultVerify')?.value || 'gjc',
    issue_labels: $('repoLabels').value,
  };
}

function fillRepo(id) {
  const r = window._repos.find(x => x.id === id);
  if (!r) return;
  $('repoOwner').value = r.owner;
  $('repoName').value = r.name;
  $('repoBranch').value = r.default_branch;
  $('repoLabels').value = r.issue_labels;
  $('repoEnabled').checked = !!r.enabled;
  $('repoMerge').checked = !!r.auto_merge;
  toast('폼에 불러왔습니다. 저장하면 업데이트됩니다.');
}

async function deleteRepo(id) {
  if (!confirm('삭제할까요?')) return;
  await api(`/api/repos/${id}`, { method: 'DELETE' });
  toast('삭제됨');
  await loadRepos();
}

function renderOrgTokens(ownerTokens) {
  $('orgTokenList').innerHTML = (ownerTokens || []).map(o => `
    <div class="item compact-row">
      <b>${esc(o.owner)}</b>
      <div class="badges">${badge('token OK')}</div>
      <button data-action="delete-org-token" data-owner="${esc(o.owner)}" class="danger">삭제</button>
    </div>`).join('') || '<p class="hint">등록된 조직 토큰 없음</p>';
}

async function loadSettings() {
  const s = await api('/api/settings');
  window._settings = s;
  const tc = s.tokens_configured || {};
  const orgCount = (s.owner_tokens || []).length;
  $('tokenState').innerHTML = `토큰 저장 상태 — 개인: ${tc.personal ? 'OK' : '없음'}, 조직: ${orgCount}개, audit: ${tc.audit ? 'OK' : '없음'}`;
  renderOrgTokens(s.owner_tokens || []);
  $('pollInterval').value = s.poll_interval_seconds;
  $('workspaceDir').value = s.workspace_dir;
  $('maxAgentSeconds').value = s.max_agent_seconds;
  $('pollingEnabled').checked = s.polling_enabled;
  $('autoRegisterEnabled').checked = s.auto_register_enabled;
  $('commentPrefix').value = s.bot_comment_prefix;
  $('defaultImpl').value = s.default_implement_agent || 'gjc';
  $('defaultVerify').value = s.default_verify_agent || 'gjc';
}

function settingsPayload(extraOrgTokens = []) {
  const ownerTokens = [...extraOrgTokens];
  const owner = $('orgOwner').value.trim();
  const token = $('orgToken').value.trim();
  if (owner && token) ownerTokens.push({ owner, token });
  return {
    personal_token: $('personalToken').value || null,
    audit_token: $('auditToken').value || null,
    owner_tokens: ownerTokens,
    poll_interval_seconds: +$('pollInterval').value,
    workspace_dir: $('workspaceDir').value,
    max_agent_seconds: +$('maxAgentSeconds').value,
    polling_enabled: $('pollingEnabled').checked,
    auto_register_enabled: $('autoRegisterEnabled').checked,
    auto_register_owners: '',
    bot_comment_prefix: $('commentPrefix').value,
    default_implement_agent: $('defaultImpl').value,
    default_verify_agent: $('defaultVerify').value,
  };
}

function bindEvents() {
  document.querySelectorAll('.tabs button').forEach(b => {
    b.onclick = () => {
      document.querySelectorAll('.tabs button,.panel').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      $(b.dataset.tab).classList.add('active');
    };
  });

  document.body.addEventListener('click', async (e) => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const action = el.dataset.action;
    const id = Number(el.dataset.id || el.dataset.repoId);
    try {
      if (action === 'show-log') await showLog(id);
      if (action === 'fill-repo') fillRepo(id);
      if (action === 'delete-repo') await deleteRepo(id);
      if (action === 'delete-org-token') {
        await api('/api/settings', { method: 'PUT', body: JSON.stringify(settingsPayload([{ owner: el.dataset.owner, delete: true }])) });
        toast('조직 토큰 삭제됨');
        await loadSettings();
      }
    } catch (err) { toast(err.message); }
  });

  $('loginBtn').onclick = async () => {
    try {
      await api('/api/login', { method: 'POST', body: JSON.stringify({ username: $('loginUser').value, password: $('loginPass').value }) });
      await showApp();
    } catch (e) { $('loginMsg').textContent = e.message; }
  };
  $('logoutBtn').onclick = async () => { await api('/api/logout', { method: 'POST' }); location.reload(); };
  const syncNow = async () => {
    const btn = $('topSyncBtn');
    if (btn.disabled) return;
    const original = btn.textContent;
    btn.disabled = true;
    btn.classList.add('spinning');
    btn.textContent = '↻ 동기화 중…';
    toast('동기화 중…');
    try {
      const r = await api('/api/poll-now', { method: 'POST' });
      await Promise.all([loadDashboard(), loadRepos()]);
      toast(`동기화 완료 · 새 작업 ${r.created_jobs}개`);
    } catch (err) {
      toast('동기화 실패: ' + err.message);
    } finally {
      btn.disabled = false;
      btn.classList.remove('spinning');
      btn.textContent = original;
    }
  };
  $('topSyncBtn').onclick = syncNow;
  $('discoverBtn').onclick = async () => { const r = await api('/api/discover-repos', { method: 'POST' }); toast(`자동 등록: 신규 ${r.created}개, 갱신 ${r.updated}개`); await Promise.all([loadRepos(), loadDashboard()]); };
  $('auditDiagBtn').onclick = async () => {
    const r = await api('/api/audit-diagnostics');
    const lines = [`GitHub 사용자: ${esc(r.login || '-')}`];
    if (r.audit_token) lines.push(`Audit token scopes: ${esc(r.audit_token.scopes || '-')}`);
    for (const o of (r.orgs || [])) {
      lines.push(`${esc(o.org)}: ${o.ok ? 'audit OK' : 'audit 실패'} · repo접근 ${esc(o.repo_access_count ?? '-')}개`);
      lines.push(`→ ${esc(o.diagnosis || o.message || '')}`);
    }
    $('auditDiag').innerHTML = lines.join('<br>');
    toast('진단 완료');
  };
  $('saveRepoBtn').onclick = async () => { await api('/api/repos', { method: 'POST', body: JSON.stringify(repoForm()) }); toast('저장소 저장됨'); await loadRepos(); };
  $('saveSettingsBtn').onclick = async () => {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify(settingsPayload()) });
    ['personalToken', 'auditToken', 'orgOwner', 'orgToken'].forEach(id => $(id).value = '');
    toast('설정 저장됨');
    await Promise.all([loadSettings(), loadRepos()]);
  };
  $('changePassBtn').onclick = async () => {
    await api('/api/change-password', { method: 'POST', body: JSON.stringify({ current_password: $('curPass').value, new_password: $('newPass').value }) });
    toast('비밀번호 변경됨');
    $('curPass').value = '';
    $('newPass').value = '';
    $('mustChange').classList.add('hidden');
  };
}

bindEvents();
api('/api/me').then(showApp).catch(() => {});
setInterval(() => { if (!$('appView').classList.contains('hidden')) loadDashboard().catch(() => {}); }, 15000);
