const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const ICON_COPY = '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><rect x="5" y="5" width="8" height="8" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M3 10.5V3.8A1.3 1.3 0 0 1 4.3 2.5h6.7" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';
const ICON_EDIT = '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M11.2 2.1l2.7 2.7-8.3 8.3-3.5.8.8-3.5z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>';
const ICON_TRASH = '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M3 4.6h10M6.4 4.6V3.1h3.2v1.5M4.6 4.6l.6 8.3h5.6l.6-8.3" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';

const VIEW_TITLES = {
  overview: '總覽儀表板',
  nodes: '伺服器節點',
  targets: '壓測目標',
  reports: '壓測報告',
  bot: 'Discord 機器人',
  deploy: '部署終端',
  accounts: '帳號與安全',
};

let latestState = null;
let currentUser = null;
let activeJobId = null;
let editingKey = null;
let jobTimer = null;
let stateTimer = null;
let renderedLines = 0;

/* ---------------------------------------------------------------- 主題與版面 */

function applyTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('lt_theme', next);
  const label = $('#theme-label');
  if (label) label.textContent = next === 'dark' ? '淺色模式' : '深色模式';
}

applyTheme(localStorage.getItem('lt_theme') || 'dark');

$('#btn-theme').addEventListener('click', () => {
  const cur = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  applyTheme(cur === 'dark' ? 'light' : 'dark');
});

function setView(view) {
  const targetView = VIEW_TITLES[view] ? view : 'overview';
  $('#workspace').dataset.view = targetView;
  $('#page-title').textContent = VIEW_TITLES[targetView];
  $$('.nav-item[data-nav]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.nav === targetView);
  });
  sessionStorage.setItem('lt_view', targetView);
  $('#app').classList.remove('sidebar-open');
  if (targetView === 'accounts' && currentUser && currentUser.role === 'admin') {
    loadUsers();
  }
}

$$('.nav-item[data-nav]').forEach((btn) => {
  btn.addEventListener('click', () => setView(btn.dataset.nav));
});

$('#btn-menu').addEventListener('click', () => {
  $('#app').classList.toggle('sidebar-open');
});

$('#sidebar-overlay').addEventListener('click', () => {
  $('#app').classList.remove('sidebar-open');
});

setView(sessionStorage.getItem('lt_view') || 'overview');

/* ---------------------------------------------------------------- 工具與通知 */

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function toast(message, kind = 'info') {
  const root = $('#toast-root');
  if (!root) return;
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  root.appendChild(el);
  setTimeout(() => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 220);
  }, 3000);
}

async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement('textarea');
  input.value = text;
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.select();
  document.execCommand('copy');
  input.remove();
}

async function api(path, options = {}) {
  const { skipAuthRedirect = false, ...fetchOpts } = options;
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...fetchOpts,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* 無 JSON 內容 */ }
  if (res.status === 401 && !skipAuthRedirect) {
    showLogin(false);
    throw new Error('unauth');
  }
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
  return data;
}

function showAuthMode(mode) {
  $('#login-form').classList.toggle('hidden', mode !== 'login');
  $('#setup-form').classList.toggle('hidden', mode !== 'setup');
  $('#reset-form').classList.toggle('hidden', mode !== 'reset');
  $('#login-error').textContent = '';
  $('#setup-error').textContent = '';
  $('#reset-error').textContent = '';
  if (mode === 'login') setTimeout(() => $('#login-username').focus(), 30);
  if (mode === 'setup') setTimeout(() => $('#setup-token').focus(), 30);
  if (mode === 'reset') setTimeout(() => $('#reset-token').focus(), 30);
}

function showLogin(setupRequired = false) {
  currentUser = null;
  $('#login').classList.remove('hidden');
  $('#app').classList.add('hidden');
  stopTimers();
  showAuthMode(setupRequired ? 'setup' : 'login');
}

function showApp() {
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
}

function stopTimers() {
  if (stateTimer) { clearInterval(stateTimer); stateTimer = null; }
  if (jobTimer) { clearInterval(jobTimer); jobTimer = null; }
}

function updateStrength(inputEl, meterEl) {
  if (!inputEl || !meterEl) return;
  const val = inputEl.value || '';
  const fill = meterEl.querySelector('.strength-fill');
  const text = meterEl.querySelector('.strength-text');
  if (!val) {
    fill.className = 'strength-fill';
    text.textContent = '請輸入至少 8 字元';
    return;
  }
  let score = 0;
  if (val.length >= 8) score += 1;
  if (val.length >= 12) score += 1;
  if (/[a-z]/.test(val) && /[A-Z]/.test(val)) score += 1;
  if (/\d/.test(val) && /[^A-Za-z0-9]/.test(val)) score += 1;
  const lvl = val.length < 8 ? 1 : Math.max(1, Math.min(4, score));
  const labels = ['', '弱（建議加長）', '普通', '良好', '高強度'];
  fill.className = `strength-fill s${lvl}`;
  text.textContent = labels[lvl];
}

/* ---------------------------------------------------------------- 登入 / 初始化 / 重設 */

$('#btn-toggle-password').addEventListener('click', () => {
  const input = $('#login-password');
  input.type = input.type === 'password' ? 'text' : 'password';
  input.focus();
});

$('#btn-toggle-setup-token').addEventListener('click', () => {
  const input = $('#setup-token');
  input.type = input.type === 'password' ? 'text' : 'password';
  input.focus();
});

$('#btn-show-reset').addEventListener('click', () => {
  const u = $('#login-username').value.trim();
  if (u) $('#reset-username').value = u;
  showAuthMode('reset');
});

$('#btn-reset-back').addEventListener('click', () => showAuthMode('login'));

$('#setup-password').addEventListener('input', () => {
  updateStrength($('#setup-password'), $('#setup-strength'));
});
$('#reset-password').addEventListener('input', () => {
  updateStrength($('#reset-password'), $('#reset-strength'));
});

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#login-error').textContent = '';
  const btn = $('#btn-login-submit');
  btn.disabled = true;
  try {
    await api('/api/login', {
      method: 'POST',
      skipAuthRedirect: true,
      body: JSON.stringify({
        username: $('#login-username').value.trim(),
        password: $('#login-password').value,
        remember: $('#login-remember').checked,
      }),
    });
    $('#login-password').value = '';
    $('#login-password').type = 'password';
    await boot();
  } catch (err) {
    $('#login-error').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$('#setup-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#setup-error').textContent = '';
  const pwd = $('#setup-password').value;
  const pwd2 = $('#setup-password2').value;
  if (pwd.length < 8) {
    $('#setup-error').textContent = '密碼長度至少需要 8 個字元';
    return;
  }
  if (pwd !== pwd2) {
    $('#setup-error').textContent = '兩次輸入的密碼不一致';
    return;
  }
  const btn = $('#btn-setup-submit');
  btn.disabled = true;
  try {
    await api('/api/setup', {
      method: 'POST',
      skipAuthRedirect: true,
      body: JSON.stringify({
        setup_token: $('#setup-token').value.trim(),
        username: $('#setup-username').value.trim(),
        display_name: $('#setup-display-name').value.trim(),
        password: pwd,
      }),
    });
    $('#setup-form').reset();
    updateStrength($('#setup-password'), $('#setup-strength'));
    toast('管理員帳號已建立並登入', 'ok');
    await boot();
  } catch (err) {
    $('#setup-error').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$('#reset-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#reset-error').textContent = '';
  const pwd = $('#reset-password').value;
  const pwd2 = $('#reset-password2').value;
  if (pwd.length < 8) {
    $('#reset-error').textContent = '新密碼長度至少需要 8 個字元';
    return;
  }
  if (pwd !== pwd2) {
    $('#reset-error').textContent = '兩次輸入的密碼不一致';
    return;
  }
  const btn = $('#btn-reset-submit');
  btn.disabled = true;
  const username = $('#reset-username').value.trim();
  try {
    await api('/api/auth/reset', {
      method: 'POST',
      skipAuthRedirect: true,
      body: JSON.stringify({
        setup_token: $('#reset-token').value.trim(),
        username,
        new_password: pwd,
      }),
    });
    $('#reset-form').reset();
    updateStrength($('#reset-password'), $('#reset-strength'));
    $('#login-username').value = username;
    showAuthMode('login');
    toast('密碼已重設，請使用新密碼登入', 'ok');
  } catch (err) {
    $('#reset-error').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$('#btn-logout').addEventListener('click', async () => {
  try {
    await api('/api/logout', { method: 'POST', skipAuthRedirect: true });
  } finally {
    showLogin(false);
  }
});

/* ---------------------------------------------------------------- 狀態 */

async function refreshState() {
  try {
    const state = await api('/api/state');
    latestState = state;
    renderState(state);
    if (state.job && state.job.id !== activeJobId) {
      attachJob(state.job.id);
    }
    return true;
  } catch (err) {
    if (err.message !== 'unauth') console.warn('[state]', err.message);
    return false;
  }
}

function renderState(state) {
  const { server, summary, nodes, job } = state;

  if (state.me) {
    renderMe(state.me);
  }
  const navAccounts = $('#nav-badge-accounts');
  if (navAccounts) {
    navAccounts.textContent = state.users_count != null ? String(state.users_count) : '安全';
  }

  $('#server-line').textContent =
    `${server.hostname} · ${server.master_ip} · JMeter ${server.jmeter}`;
  $('#sidebar-server-host').textContent = server.hostname || server.master_ip;
  $('#sidebar-server-meta').textContent = `${server.master_ip} · JM ${server.jmeter}`;

  const healthCls = summary.online === summary.total && summary.total > 0
    ? 'ok'
    : summary.online === 0 ? 'err' : 'warn';

  const onlineChip = $('#chip-online');
  onlineChip.textContent = `${summary.online} / ${summary.total} 在線`;
  onlineChip.className = 'chip ' + healthCls;

  const navNodes = $('#nav-badge-nodes');
  navNodes.textContent = `${summary.online}/${summary.total}`;
  navNodes.className = 'nav-badge ' + (summary.total ? healthCls : '');

  $('#chip-capacity').textContent = `容量 ${summary.capacity.toLocaleString()}`;

  $('#stat-online').textContent = `${summary.online} / ${summary.total}`;
  $('#stat-online-hint').textContent = summary.offline.length
    ? `離線：${summary.offline.join('、')}`
    : (summary.total ? '全部在線' : '尚未設定節點');

  $('#stat-capacity').textContent = summary.capacity.toLocaleString();
  $('#stat-master').textContent = server.master_ip;
  $('#stat-jmeter').textContent = `JMeter ${server.jmeter}${server.ssh ? ' · SSH 可用' : ' · 無 ssh'}`;
  $('#stat-time').textContent = new Date().toLocaleTimeString('zh-TW', { hour12: false });

  $('#node-count').textContent = `共 ${summary.total} 台 · 啟用 ${summary.enabled} 台`;

  if (!server.ssh) {
    $('#action-hint').innerHTML = '<span style="color:var(--err)">本機找不到 ssh 指令，無法遠端部署。</span>';
    $('#btn-deploy-all').disabled = true;
  }

  renderNodes(nodes, job);
  renderTargets(state.targets);
  renderBot(state.bot);
  renderLoadtest(state.loadtest);
  if (job) renderJobChip(job);
}

function sparkline(history, key) {
  const values = (history || []).filter((v) => typeof v === 'number');
  if (values.length < 2) return '<div class="spark-empty">延遲資料累積中…</div>';

  const width = 300;
  const height = 34;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = Math.max(max - min, 1);
  const step = width / Math.max(history.length - 1, 1);

  const lastVal = values[values.length - 1];
  const color = lastVal < 80 ? 'var(--ok)' : lastVal < 200 ? 'var(--warn)' : 'var(--err)';
  const safeId = String(key || 'node').replace(/[^a-zA-Z0-9_-]/g, '_');

  let linePath = '';
  let areaPath = '';
  let segStart = null;
  let lastPt = null;

  history.forEach((value, index) => {
    if (typeof value !== 'number') {
      if (segStart !== null && lastPt !== null) {
        areaPath += ` L${lastPt.x.toFixed(1)} ${height} L${segStart.toFixed(1)} ${height} Z`;
        segStart = null;
      }
      return;
    }
    const x = index * step;
    const y = height - ((value - min) / span) * (height - 7) - 3.5;
    if (segStart === null) {
      segStart = x;
      linePath += ` M${x.toFixed(1)} ${y.toFixed(1)}`;
      areaPath += ` M${x.toFixed(1)} ${y.toFixed(1)}`;
    } else {
      linePath += ` L${x.toFixed(1)} ${y.toFixed(1)}`;
      areaPath += ` L${x.toFixed(1)} ${y.toFixed(1)}`;
    }
    lastPt = { x, y };
  });

  if (segStart !== null && lastPt !== null) {
    areaPath += ` L${lastPt.x.toFixed(1)} ${height} L${segStart.toFixed(1)} ${height} Z`;
  }

  const dotHtml = lastPt
    ? `<circle cx="${lastPt.x.toFixed(1)}" cy="${lastPt.y.toFixed(1)}" r="2.8" fill="${color}"></circle>`
    : '';

  return `<svg class="spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    <defs>
      <linearGradient id="sp-${safeId}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity="0.28"></stop>
        <stop offset="100%" stop-color="${color}" stop-opacity="0.0"></stop>
      </linearGradient>
    </defs>
    <path d="${areaPath}" fill="url(#sp-${safeId})"></path>
    <path d="${linePath}" fill="none" stroke="${color}" stroke-width="1.75"
          stroke-linejoin="round" stroke-linecap="round"></path>
    ${dotHtml}
  </svg>`;
}

function renderNodes(nodes, job) {
  const grid = $('#node-grid');
  if (!nodes.length) {
    grid.innerHTML = `<div class="empty-state">
      <div>還沒有任何伺服器節點</div>
      <button class="btn primary" data-act="add">新增第一台伺服器</button>
    </div>`;
    return;
  }

  const busy = job && job.status === 'running';
  const deploying = new Set();
  if (busy && job.nodes) {
    Object.entries(job.nodes).forEach(([key, status]) => {
      if (status !== '完成' && status !== '等待中') deploying.add(key);
    });
  }

  grid.innerHTML = nodes.map((node) => {
    const classes = ['node', node.online ? 'online' : 'offline'];
    if (!node.enabled) classes.push('disabled');
    if (deploying.has(node.key)) classes.push('deploying');

    const latency = node.online && node.latency_ms != null
      ? `${node.latency_ms < 1 ? '<1' : Math.round(node.latency_ms)} ms`
      : '離線';

    const latCls = !node.online
      ? 'bad'
      : (node.latency_ms != null && node.latency_ms >= 200)
        ? 'bad'
        : (node.latency_ms != null && node.latency_ms >= 80)
          ? 'warn'
          : 'ok';

    const sshBadge = node.ssh_ok
      ? '<span class="badge ok">SSH 可用</span>'
      : '<span class="badge err">SSH 不通</span>';

    const deployStatus = busy && job.nodes && job.nodes[node.key]
      ? `<span class="badge warn">${esc(job.nodes[node.key])}</span>`
      : sshBadge;

    const addr = `${node.host}:${node.port}`;

    return `
      <article class="${classes.join(' ')}">
        <div class="node-head">
          <span class="dot ${node.online ? 'online' : 'offline'}"></span>
          <span class="node-name">${esc(node.key)}</span>
          <span class="region">${esc(node.region)}</span>
          <label class="switch" title="${node.enabled ? '已啟用' : '已停用'}">
            <input type="checkbox" data-act="toggle" data-key="${esc(node.key)}" ${node.enabled ? 'checked' : ''}>
            <span class="slider"></span>
          </label>
        </div>
        <div class="node-host">
          <span class="addr mono">${esc(addr)}</span>
          <span class="host-actions">
            <button class="icon-mini" data-act="copy" data-addr="${esc(addr)}" title="複製位址">${ICON_COPY}</button>
            <button class="icon-mini" data-act="edit" data-key="${esc(node.key)}" title="編輯">${ICON_EDIT}</button>
            <button class="icon-mini danger" data-act="delete" data-key="${esc(node.key)}" title="刪除">${ICON_TRASH}</button>
          </span>
        </div>
        <div class="node-metrics">
          <div class="metric">
            <span class="k">延遲</span>
            <span class="v ${latCls}">${latency}</span>
          </div>
          <div class="metric">
            <span class="k">執行緒上限</span>
            <span class="v">${node.max_threads}</span>
          </div>
          <div class="metric">
            <span class="k">檢查</span>
            <span class="v muted-val">
              ${node.checked_ago == null ? '—' : Math.round(node.checked_ago) + ' 秒前'}
            </span>
          </div>
        </div>
        <div class="spark-wrap">${sparkline(node.history, node.key)}</div>
        <div class="node-foot">
          ${deployStatus}
          <span class="spacer"></span>
          <button class="btn small" data-act="inspect" data-key="${esc(node.key)}">檢查</button>
          <button class="btn small" data-act="deploy" data-key="${esc(node.key)}" ${busy ? 'disabled' : ''}>部署</button>
        </div>
      </article>`;
  }).join('');
}

/* ---------------------------------------------------------------- 節點操作 */

$('#node-grid').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-act]');
  if (!button) return;
  const { act, key, addr } = button.dataset;
  if (act === 'copy') {
    try {
      await copyText(addr);
      toast(`已複製 ${addr}`, 'ok');
    } catch (_) {
      toast('無法複製到剪貼簿', 'err');
    }
  } else if (act === 'deploy') {
    await startDeploy({ scope: 'node', key });
  } else if (act === 'inspect') {
    await inspectNode(key);
  } else if (act === 'edit') {
    openNodeModal(key);
  } else if (act === 'delete') {
    await deleteNode(key);
  } else if (act === 'add') {
    openNodeModal(null);
  }
});

$('#node-grid').addEventListener('change', async (event) => {
  const input = event.target.closest('input[data-act="toggle"]');
  if (!input) return;
  const key = input.dataset.key;
  try {
    await api(`/api/nodes/${key}/enabled`, {
      method: 'POST',
      body: JSON.stringify({ enabled: input.checked }),
    });
    toast(`${key} 已${input.checked ? '啟用' : '停用'}`, 'ok');
    await refreshState();
  } catch (err) {
    toast(`切換失敗：${err.message}`, 'err');
    input.checked = !input.checked;
  }
});

async function inspectNode(key) {
  openModal(`節點檢查 · ${key}`, '讀取中，約需數秒…');
  try {
    const result = await api(`/api/nodes/${key}/inspect`);
    $('#modal-body').textContent = result.output;
  } catch (err) {
    $('#modal-body').textContent = `檢查失敗：${err.message}`;
  }
}

function openModal(title, body) {
  $('#modal-title').textContent = title;
  $('#modal-body').textContent = body;
  $('#modal').classList.remove('hidden');
}

$('#modal-close').addEventListener('click', () => $('#modal').classList.add('hidden'));
$('#modal').addEventListener('click', (event) => {
  if (event.target.id === 'modal') $('#modal').classList.add('hidden');
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    $('#modal').classList.add('hidden');
    closeNodeModal();
    closeTargetModal();
    closeUserModal();
    $('#app').classList.remove('sidebar-open');
  }
});

/* ---------------------------------------------------------------- 節點表單 */

const nodeForm = $('#node-form');
let testedOk = false;
let hasStoredCred = false;

$('#btn-add-node').addEventListener('click', () => openNodeModal(null));
$('#node-modal-close').addEventListener('click', closeNodeModal);
$('#btn-node-cancel').addEventListener('click', closeNodeModal);
$('#node-modal').addEventListener('click', (event) => {
  if (event.target.id === 'node-modal') closeNodeModal();
});

function openNodeModal(key) {
  editingKey = key;
  hasStoredCred = false;
  nodeForm.reset();
  nodeForm.port.value = '1099';
  nodeForm.ssh_port.value = '22';
  nodeForm.max_threads.value = '600';
  nodeForm.ssh_user.value = 'root';
  nodeForm.enabled.checked = true;
  nodeForm.ssh_password.value = '';
  nodeForm.ssh_password.placeholder = '部署時使用';
  setFormStatus('', '');
  $('#node-modal-title').textContent = key ? `編輯伺服器 · ${key}` : '新增伺服器';

  if (key && latestState) {
    const node = latestState.nodes.find((item) => item.key === key);
    if (node) {
      nodeForm.key.value = node.key;
      nodeForm.host.value = node.host;
      nodeForm.port.value = node.port;
      nodeForm.ssh_port.value = node.ssh_port || 22;
      nodeForm.ssh_user.value = node.ssh_user || 'root';
      nodeForm.region.value = node.region === '—' ? '' : node.region;
      nodeForm.max_threads.value = node.max_threads;
      nodeForm.note.value = node.note || '';
      nodeForm.enabled.checked = node.enabled;
      hasStoredCred = Boolean(node.has_password);
      if (hasStoredCred) nodeForm.ssh_password.placeholder = '已儲存，留空則沿用';
    }
  }
  setTested(hasStoredCred);
  $('#node-modal').classList.remove('hidden');
  setTimeout(() => nodeForm.key.focus(), 50);
}

function closeNodeModal() {
  $('#node-modal').classList.add('hidden');
  editingKey = null;
  testedOk = false;
}

function setTested(ok) {
  testedOk = ok;
  $('#btn-node-save-deploy').disabled = !ok;
}

function setFormStatus(text, cls) {
  const element = $('#node-form-status');
  element.textContent = text;
  element.className = 'form-status span2' + (cls ? ' ' + cls : '');
}

function formPayload() {
  return {
    key: nodeForm.key.value.trim(),
    host: nodeForm.host.value.trim(),
    port: Number(nodeForm.port.value || 1099),
    ssh_port: Number(nodeForm.ssh_port.value || 22),
    region: nodeForm.region.value.trim(),
    max_threads: Number(nodeForm.max_threads.value || 600),
    enabled: nodeForm.enabled.checked,
    note: nodeForm.note.value.trim(),
    ssh_user: nodeForm.ssh_user.value.trim() || 'root',
    ssh_password: nodeForm.ssh_password.value,
  };
}

['host', 'ssh_user', 'ssh_password', 'ssh_port'].forEach((name) => {
  nodeForm[name].addEventListener('input', () => {
    if (testedOk) {
      setTested(false);
      setFormStatus('連線參數已變更，請重新測試連接', 'busy');
    }
  });
});

nodeForm.addEventListener('submit', (event) => {
  event.preventDefault();
  saveNode(false);
});

$('#btn-node-save-deploy').addEventListener('click', () => {
  if (!testedOk) {
    setFormStatus('請先測試連接，成功後才能部署', 'err');
    return;
  }
  saveNode(true);
});

async function saveNode(deployAfter) {
  const payload = formPayload();
  if (!payload.key || !payload.host) {
    setFormStatus('名稱與伺服器 IP 為必填', 'err');
    return;
  }
  setFormStatus('儲存中…', 'busy');
  $('#btn-node-save').disabled = true;
  $('#btn-node-save-deploy').disabled = true;
  try {
    const saved = editingKey
      ? await api(`/api/nodes/${encodeURIComponent(editingKey)}`, {
          method: 'PUT',
          body: JSON.stringify(payload),
        })
      : await api('/api/nodes', { method: 'POST', body: JSON.stringify(payload) });
    closeNodeModal();
    toast(`已儲存節點 ${saved.key}`, 'ok');
    await refreshState();
    if (deployAfter) await startDeploy({ scope: 'node', key: saved.key });
  } catch (err) {
    setFormStatus(err.message, 'err');
  } finally {
    $('#btn-node-save').disabled = false;
    $('#btn-node-save-deploy').disabled = !testedOk;
  }
}

$('#btn-test-node').addEventListener('click', async () => {
  const payload = formPayload();
  if (!payload.host) {
    setFormStatus('請先填伺服器 IP', 'err');
    return;
  }
  if (!payload.ssh_password && !hasStoredCred) {
    setFormStatus('請輸入 SSH 密碼', 'err');
    return;
  }
  setTested(false);
  setFormStatus('連接中…', 'busy');
  $('#btn-test-node').disabled = true;
  try {
    const result = await api('/api/ssh-test', {
      method: 'POST',
      body: JSON.stringify({
        host: payload.host,
        ssh_port: payload.ssh_port,
        ssh_user: payload.ssh_user,
        ssh_password: payload.ssh_password,
        key: editingKey,
      }),
    });
    if (!result.ok) {
      setFormStatus(`連接失敗：${result.error}`, 'err');
      return;
    }
    const bits = [`SSH 認證成功（${result.user}）`];
    if (result.os) bits.push(result.os);
    if (result.cpu) bits.push(`${result.cpu} 核`);
    if (result.mem_mb) bits.push(`${(result.mem_mb / 1024).toFixed(1)} G`);
    bits.push(result.sudo ? 'sudo 可用' : 'sudo 不可用');
    bits.push(result.service === 'active' ? 'JMeter 已安裝' : 'JMeter 未安裝');
    const deployable = Boolean(result.sudo);
    setTested(deployable);
    setFormStatus(
      bits.join(' · ') + (deployable ? '' : '　→ 需要 root 權限才能部署'),
      deployable ? 'ok' : 'err'
    );
  } catch (err) {
    setFormStatus(err.message, 'err');
  } finally {
    $('#btn-test-node').disabled = false;
  }
});

async function deleteNode(key) {
  if (!confirm(`刪除節點 ${key}？\n\n只會從 nodes.json 移除，不會動到那台伺服器上的 jmeter-server。`)) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(key)}`, { method: 'DELETE' });
    toast(`已刪除節點 ${key}`, 'ok');
    await refreshState();
  } catch (err) {
    toast(`刪除失敗：${err.message}`, 'err');
  }
}

/* ---------------------------------------------------------------- 部署 */

$('#btn-deploy-all').addEventListener('click', () => {
  const includeDisabled = $('#include-disabled').checked;
  const names = (latestState ? latestState.nodes : [])
    .filter((n) => includeDisabled || n.enabled)
    .map((n) => n.key);
  if (!names.length) return;
  if (!confirm(`將對以下節點執行部署：\n\n${names.join('、')}\n\n部署期間節點上的 jmeter-server 會重啟，正在執行的壓測會中斷。確定繼續？`)) return;
  startDeploy({ scope: 'all', include_disabled: includeDisabled });
});

async function startDeploy(body) {
  try {
    const job = await api('/api/deploy', { method: 'POST', body: JSON.stringify(body) });
    clearLog();
    appendLine({ t: nowText(), level: 'info', text: `任務 ${job.job_id}：${job.title}` });
    attachJob(job.job_id);
    const curView = $('#workspace').dataset.view;
    if (curView !== 'overview' && curView !== 'deploy') {
      setView('deploy');
    } else if (curView === 'overview') {
      const dp = $('#deploy-panel');
      if (dp) dp.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    toast(`已啟動部署任務：${job.title}`, 'info');
    await refreshState();
  } catch (err) {
    toast(`無法開始部署：${err.message}`, 'err');
  }
}

function attachJob(jobId) {
  activeJobId = jobId;
  renderedLines = 0;
  if (jobTimer) clearInterval(jobTimer);
  pollJob();
  jobTimer = setInterval(pollJob, 1200);
}

async function pollJob() {
  if (!activeJobId) return;
  try {
    const job = await api(`/api/jobs/${activeJobId}`);
    renderJob(job);
    if (job.status !== 'running') {
      if (jobTimer) { clearInterval(jobTimer); jobTimer = null; }
      refreshState();
    }
  } catch (err) {
    if (err.message !== 'unauth') console.warn('[job]', err.message);
  }
}

function renderJob(job) {
  renderJobChip(job);

  $('#btn-cancel').classList.toggle('hidden', job.status !== 'running');
  $('#btn-clear-log').disabled = job.status === 'running';
  $('#btn-deploy-all').disabled = job.status === 'running';

  if (Array.isArray(job.lines)) {
    const log = $('#log');
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    const previousTop = log.scrollTop;
    log.innerHTML = job.lines.map(lineHtml).join('') || '<div class="log-empty">（無輸出）</div>';
    log.scrollTop = nearBottom ? log.scrollHeight : previousTop;
  }
}

function jobLabel(job) {
  const names = { running: '執行中', success: '成功', failed: '有失敗', cancelled: '已中止' };
  let label = names[job.status] || '—';
  if (job.status === 'running' && job.started) {
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - job.started));
    const mm = String(Math.floor(seconds / 60)).padStart(2, '0');
    const ss = String(seconds % 60).padStart(2, '0');
    label += ` · ${mm}:${ss}`;
  }
  return label;
}

function renderJobChip(job) {
  const chip = $('#job-chip');
  const map = { running: 'run', success: 'ok', failed: 'err', cancelled: 'warn' };
  const cls = map[job.status] || '';
  const text = jobLabel(job);
  chip.textContent = text;
  chip.className = 'chip ' + cls;

  const navDeploy = $('#nav-badge-deploy');
  if (navDeploy) {
    const shortNames = { running: '執行中', success: '完成', failed: '失敗', cancelled: '中止' };
    navDeploy.textContent = shortNames[job.status] || '閒置';
    navDeploy.className = 'nav-badge ' + cls;
  }
}

$('#btn-cancel').addEventListener('click', async () => {
  if (!activeJobId) return;
  if (!confirm('確定中止目前的部署任務？')) return;
  await api(`/api/jobs/${activeJobId}/cancel`, { method: 'POST' });
  toast('已送出中止指令', 'warn');
});

$('#btn-clear-log').addEventListener('click', clearLog);

function clearLog() {
  $('#log').innerHTML = '<div class="log-empty">（尚無輸出）</div>';
}

function nowText() {
  return new Date().toLocaleTimeString('zh-TW', { hour12: false });
}

function lineHtml(line) {
  const level = esc(line.level || 'info');
  return `<div class="ln ${level}"><span class="t">${esc(line.t)}</span><span class="text">${esc(line.text)}</span></div>`;
}

function appendLine(line) {
  const log = $('#log');
  if (log.querySelector('.log-empty')) log.innerHTML = '';
  log.insertAdjacentHTML('beforeend', lineHtml(line));
  log.scrollTop = log.scrollHeight;
}

/* ---------------------------------------------------------------- Discord */

const botForm = $('#bot-form');
let botFormDirty = false;
let botCmdsRendered = false;

['token', 'owner_id', 'staff_ids', 'guild_id', 'admin_ids'].forEach((name) => {
  botForm[name].addEventListener('input', () => {
    botFormDirty = true;
  });
});

botForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  setBotStatus('儲存中…', 'busy');
  try {
    const result = await api('/api/bot/config', {
      method: 'POST',
      body: JSON.stringify({
        token: botForm.token.value.trim(),
        owner_id: botForm.owner_id.value.trim(),
        staff_ids: botForm.staff_ids.value.trim(),
        guild_id: botForm.guild_id.value.trim(),
        admin_ids: botForm.admin_ids.value.trim(),
      }),
    });
    botFormDirty = false;
    botForm.token.value = '';
    renderBot(result.bot);
    setBotStatus(result.restarted ? '已儲存，服務已重啟' : '已儲存', 'ok');
    refreshState();
  } catch (err) {
    setBotStatus(err.message, 'err');
  }
});

$('#btn-bot-test').addEventListener('click', async () => {
  const token = botForm.token.value.trim();
  setBotStatus('驗證 Token 中…', 'busy');
  $('#btn-bot-test').disabled = true;
  try {
    const result = await api('/api/bot/test-token', {
      method: 'POST',
      body: JSON.stringify({ token }),
    });
    if (result.ok) {
      setBotStatus(`Token 有效：${result.username}（${result.id}）`, 'ok');
    } else {
      setBotStatus(result.error, 'err');
    }
  } catch (err) {
    setBotStatus(err.message, 'err');
  } finally {
    $('#btn-bot-test').disabled = false;
  }
});

$('#btn-bot-clear-log').addEventListener('click', async () => {
  try {
    await api('/api/bot/clear-log', { method: 'POST' });
    $('#bot-log').textContent = '（已清空）';
  } catch (err) {
    setBotStatus(err.message, 'err');
  }
});

function lastErrorLine(text) {
  const lines = (text || '').split('\n').map((line) => line.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (/error|exception|failure|unauthorized|improper/i.test(lines[i])) {
      return lines[i].replace(/^File ".*", line \d+, in /, '').slice(0, 110);
    }
  }
  return '';
}

$('#btn-bot-start').addEventListener('click', () => botAction('start'));
$('#btn-bot-restart').addEventListener('click', () => botAction('restart'));
$('#btn-bot-stop').addEventListener('click', () => {
  if (!confirm('停止 Discord 機器人？停止後斜線指令不會有回應。')) return;
  botAction('stop');
});

async function botAction(action) {
  const label = { start: '啟動中…', stop: '停止中…', restart: '重啟中…' }[action] || '處理中…';
  setBotStatus(label, 'busy');
  ['#btn-bot-start', '#btn-bot-stop', '#btn-bot-restart'].forEach((sel) => {
    $(sel).disabled = true;
  });
  try {
    const result = await api('/api/bot/action', {
      method: 'POST',
      body: JSON.stringify({ action }),
    });
    renderBot(result.bot);
    const ok = action === 'stop' ? result.bot.service !== 'active' : result.bot.service === 'active';
    let message = ok
      ? (action === 'stop' ? '已停止' : '已啟動')
      : '服務未持續執行，請看下方日誌';
    if (!ok) {
      const detail = lastErrorLine(result.bot.log);
      if (detail) message = `啟動失敗：${detail}`;
    }
    setBotStatus(message, ok ? 'ok' : 'err');
    refreshState();
  } catch (err) {
    setBotStatus(err.message, 'err');
  } finally {
    ['#btn-bot-start', '#btn-bot-stop', '#btn-bot-restart'].forEach((sel) => {
      $(sel).disabled = false;
    });
    if (latestState && latestState.bot) renderBot(latestState.bot);
  }
}

function renderBot(bot) {
  if (!bot) return;

  const chip = $('#bot-chip');
  const map = {
    active: ['執行中', 'ok'],
    activating: ['啟動中', 'run'],
    deactivating: ['停止中', 'run'],
    failed: ['啟動失敗', 'err'],
    inactive: [bot.configured ? '已停止' : '未設定', bot.configured ? 'warn' : ''],
  };
  const entry = map[bot.service] || (bot.configured ? ['無法偵測', ''] : ['未設定', '']);
  chip.textContent = entry[0];
  chip.className = 'chip ' + entry[1];

  const navBot = $('#nav-badge-bot');
  if (navBot) {
    navBot.className = 'nav-dot' + (entry[1] ? ' ' + entry[1] : '');
    navBot.title = entry[0];
  }

  $('#btn-bot-start').disabled = !bot.configured || bot.service === 'active' || botFormDirty;
  $('#btn-bot-stop').disabled = bot.service !== 'active';
  $('#btn-bot-restart').disabled = !bot.configured || botFormDirty;
  $('#btn-bot-start').title = botFormDirty ? '請先儲存設定' : '';

  if (!botFormDirty) {
    botForm.owner_id.value = bot.owner_id || '';
    botForm.staff_ids.value = bot.staff_ids || '';
    botForm.guild_id.value = bot.guild_id || '';
    botForm.admin_ids.value = (bot.admins || []).join(', ');
    botForm.token.value = '';
  }
  botForm.token.placeholder = bot.configured
    ? `已設定 ${bot.token_hint}，留空則不變更`
    : '貼上 Discord Bot Token';

  const log = $('#bot-log');
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.textContent = bot.log || '（尚無日誌）';
  if (nearBottom) log.scrollTop = log.scrollHeight;

  if (!botCmdsRendered && Array.isArray(bot.commands) && bot.commands.length) {
    $('#bot-cmd-list').innerHTML = bot.commands
      .map((item) => `<div class="cmd-item"><code>${esc(item.cmd)}</code><span>${esc(item.desc)}</span></div>`)
      .join('');
    botCmdsRendered = true;
  }
}

function setBotStatus(text, cls) {
  const element = $('#bot-status-text');
  element.textContent = text;
  element.className = 'form-status' + (cls ? ' ' + cls : '');
}

/* ---------------------------------------------------------------- 壓測狀態 */

let ltCache = null;

const LT_LABELS = {
  idle: '目前沒有壓測在執行',
  running: '壓測執行中',
  finished: '壓測結束，統計中…',
  done: '上一次壓測完成',
  failed: '上一次壓測失敗',
  aborted: '上一次壓測已中止',
  stale: '狀態不明（機器人可能已離線）',
};

function fmtNum(value) {
  return Number(value || 0).toLocaleString('zh-TW');
}

function renderLoadtest(lt) {
  ltCache = lt || { state: 'idle' };
  const state = ltCache.state || 'idle';
  if (state === 'done' && ltCache.key && ltCache.key !== lastResultsKey) {
    lastResultsKey = ltCache.key;
    loadResults();
  }
  $('#lt-panel').className = 'panel lt-panel ' + state;
  $('#lt-state').textContent = LT_LABELS[state] || state;

  const target = $('#lt-target');
  if (ltCache.domain) {
    target.textContent = ltCache.domain + (ltCache.client ? `（${ltCache.client}）` : '');
  } else {
    target.textContent = '';
  }

  const meta = [];
  if (state === 'running' || state === 'finished') {
    if (ltCache.mode === 'stepped') {
      meta.push(`階段 <b>${ltCache.stage_index || 1}/${ltCache.stage_total || '?'}</b>`);
      if (ltCache.stage_threads) meta.push(`本級 <b>${ltCache.stage_threads}</b> 併發`);
      if (ltCache.stage_wait) meta.push(`間隔等待 <b>${ltCache.stage_wait}s</b>`);
    }
    if (ltCache.threads) meta.push(`併發 <b>${ltCache.threads}</b>`);
    if (ltCache.nodes && ltCache.nodes.length) {
      meta.push(`節點 <b>${ltCache.nodes.map(esc).join('、')}</b>`);
    }
    if (ltCache.requests != null) meta.push(`已送回 <b>${fmtNum(ltCache.requests)}</b> 筆`);
    if (ltCache.operator) meta.push(`操作者 <b>${esc(ltCache.operator)}</b>`);
  } else if (state === 'done') {
    if (ltCache.mode === 'stepped') {
      if (ltCache.knee_threads != null) {
        meta.push(`拐點 <b>${ltCache.knee_threads}</b> 併發`);
      }
      if (ltCache.best_ok_rps != null) {
        meta.push(`峰值成功 <b>${Math.round(ltCache.best_ok_rps)}</b> req/s`);
      }
      if (ltCache.stage_total) meta.push(`共 <b>${ltCache.stage_total}</b> 級`);
    }
    if (ltCache.requests != null) meta.push(`請求 <b>${fmtNum(ltCache.requests)}</b>`);
    if (ltCache.err_pct != null) meta.push(`錯誤 <b>${ltCache.err_pct}%</b>`);
    if (ltCache.avg_ms != null) meta.push(`平均 <b>${ltCache.avg_ms} ms</b>`);
    if (ltCache.p90_ms != null) meta.push(`P90 <b>${ltCache.p90_ms} ms</b>`);
    meta.push(ltCache.ok
      ? '<b style="color:var(--ok)">達標</b>'
      : '<b style="color:var(--err)">未達標</b>');
  } else if (state === 'failed' || state === 'aborted' || state === 'stale') {
    if (ltCache.error) meta.push(esc(ltCache.error));
    if (ltCache.domain) meta.push(`目標 <b>${esc(ltCache.domain)}</b>`);
    if (ltCache.updated_at) meta.push(`更新於 ${esc(ltCache.updated_at.replace('T', ' '))}`);
  }
  $('#lt-meta').innerHTML = meta.length ? meta.join(' · ') : '<span>閒置待命，隨時可從 Discord 發起測試</span>';

  if (state === 'running') {
    if (ltCache.mode === 'stepped') {
      const idx = ltCache.stage_index || 1;
      const tot = ltCache.stage_total || 1;
      const se = ltCache.stage_elapsed || 0;
      const sd = ltCache.duration || 1;
      $('#lt-timer').textContent = `第 ${idx} / ${tot} 級`;
      $('#lt-timer-sub').textContent = ltCache.stage_wait
        ? `間隔等待 ${ltCache.stage_wait}s`
        : `${se} / ${sd} s`;
      const done = ((idx - 1) + Math.min(1, se / sd)) / tot;
      $('#lt-bar').style.width = `${Math.min(100, done * 100).toFixed(1)}%`;
    } else {
      $('#lt-timer').textContent = `${ltCache.elapsed} / ${ltCache.duration} s`;
      $('#lt-timer-sub').textContent = `剩餘 ${ltCache.remaining} s`;
      $('#lt-bar').style.width = `${ltCache.progress}%`;
    }
  } else if (state === 'idle') {
    $('#lt-timer').textContent = '—';
    $('#lt-timer-sub').textContent = '';
    $('#lt-bar').style.width = '0';
  } else {
    $('#lt-timer').textContent = `${ltCache.duration || ltCache.elapsed || 0} s`;
    $('#lt-timer-sub').textContent = {
      done: '已完成',
      failed: '已失敗',
      aborted: '已中止',
      stale: '狀態不明',
      finished: '統計中',
    }[state] || '';
    $('#lt-bar').style.width = '100%';
  }
}

setInterval(() => {
  if (!ltCache || ltCache.state !== 'running' || !ltCache.started_ts) return;
  if (ltCache.mode === 'stepped') return;
  const duration = ltCache.duration || 0;
  const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - ltCache.started_ts));
  const shown = duration ? Math.min(elapsed, duration) : elapsed;
  $('#lt-timer').textContent = `${shown} / ${duration} s`;
  $('#lt-timer-sub').textContent = `剩餘 ${Math.max(0, duration - elapsed)} s`;
  $('#lt-bar').style.width =
    `${duration ? Math.min(100, (elapsed / duration) * 100).toFixed(1) : 0}%`;
}, 1000);

/* ---------------------------------------------------------------- 壓測目標 */

let latestTargets = [];
let targetPlans = [];
let editingTargetKey = null;

function renderTargets(targets) {
  latestTargets = targets || [];
  const list = $('#targets-list');
  const count = $('#target-count');
  const navTargets = $('#nav-badge-targets');

  if (navTargets) navTargets.textContent = String(latestTargets.length);

  if (!latestTargets.length) {
    list.innerHTML = '<div class="results-empty">尚無目標，請先新增要壓測的網站</div>';
    count.textContent = '0 個';
    return;
  }
  count.textContent = `${latestTargets.length} 個`;
  list.innerHTML = latestTargets.map((t) => {
    const active = t.status === 'approved';
    const badge = active
      ? '<span class="badge ok">可壓測</span>'
      : `<span class="badge err">${esc(t.status)}</span>`;
    const expired = t.authorized_until && t.authorized_until < new Date().toISOString().slice(0, 10);
    return `
      <article class="target-row ${active ? '' : 'off'}">
        <div class="target-main">
          <div class="target-head">
            <span class="target-name">${esc(t.key)}</span>
            ${badge}
            ${expired ? '<span class="badge err">已過期</span>' : ''}
            ${t.client ? `<span class="region">${esc(t.client)}</span>` : ''}
          </div>
          <div class="target-url mono">${esc(t.url || '（未設定網址）')}</div>
          <div class="target-meta">
            <span title="${esc(t.plan || '')}">計畫 <b>${esc(t.plan_label || t.plan || '—')}</b></span>
            <span>併發上限 <b>${t.max_threads}</b></span>
            <span>時長上限 <b>${t.max_duration}s</b></span>
            <span>授權至 <b>${esc(t.authorized_until || '—')}</b></span>
          </div>
        </div>
        <div class="target-actions">
          <label class="switch" title="${active ? '已啟用' : '已停用'}">
            <input type="checkbox" data-act="toggle-target" data-key="${esc(t.key)}" ${active ? 'checked' : ''}>
            <span class="slider"></span>
          </label>
          <button class="icon-mini" data-act="edit-target" data-key="${esc(t.key)}" title="編輯">${ICON_EDIT}</button>
          <button class="icon-mini danger" data-act="delete-target" data-key="${esc(t.key)}" title="刪除">${ICON_TRASH}</button>
        </div>
      </article>`;
  }).join('');
}

async function ensureTargetPlans() {
  if (targetPlans.length) return;
  try {
    const data = await api('/api/targets');
    targetPlans = data.plans || [];
  } catch (err) {
    console.warn('[plans]', err.message);
    return;
  }
  const select = $('#target-plan');
  const current = select.value;
  const groups = new Map();
  targetPlans.forEach((p) => {
    if (!groups.has(p.group)) groups.set(p.group, []);
    groups.get(p.group).push(p);
  });
  select.innerHTML = [...groups.entries()].map(([group, items]) => {
    const opts = items.map((p) =>
      `<option value="${esc(p.value)}">${esc(p.label)}${p.badge ? '（' + esc(p.badge) + '）' : ''}</option>`
    ).join('');
    return `<optgroup label="${esc(group)}">${opts}</optgroup>`;
  }).join('');
  if (current && targetPlans.some((p) => p.value === current)) select.value = current;
  updatePlanHint();
}

function currentPlanInfo() {
  return targetPlans.find((p) => p.value === $('#target-plan').value) || null;
}

function updatePlanHint() {
  const hint = $('#plan-hint');
  if (!hint) return;
  const info = currentPlanInfo();
  if (!info) {
    hint.innerHTML = '';
    return;
  }
  hint.innerHTML =
    `<span class="plan-desc">${esc(info.desc)}</span>` +
    (info.note ? `<span class="plan-note">注意：${esc(info.note)}</span>` : '') +
    `<span class="plan-file">${esc(info.value)}</span>`;
}

$('#target-plan').addEventListener('change', updatePlanHint);

const targetForm = $('#target-form');

$('#btn-add-target').addEventListener('click', () => openTargetModal(null));
$('#target-modal-close').addEventListener('click', closeTargetModal);
$('#btn-target-cancel').addEventListener('click', closeTargetModal);
$('#target-modal').addEventListener('click', (event) => {
  if (event.target.id === 'target-modal') closeTargetModal();
});

$('#targets-list').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const key = button.dataset.key;
  const target = latestTargets.find((item) => item.key === key);
  if (!target) return;
  const act = button.dataset.act;

  if (act === 'edit-target') {
    openTargetModal(key);
  } else if (act === 'delete-target') {
    if (!confirm(`刪除目標 ${key}？之後 Discord 將無法選用它。`)) return;
    try {
      await api(`/api/targets/${encodeURIComponent(key)}`, { method: 'DELETE' });
      toast(`已刪除目標 ${key}`, 'ok');
      await refreshState();
    } catch (err) {
      toast(err.message, 'err');
    }
  }
});

$('#targets-list').addEventListener('change', async (event) => {
  const input = event.target.closest('[data-act="toggle-target"]');
  if (!input) return;
  const key = input.dataset.key;
  const target = latestTargets.find((item) => item.key === key);
  if (!target) return;
  try {
    await api(`/api/targets/${encodeURIComponent(key)}`, {
      method: 'PUT',
      body: JSON.stringify({
        key: target.key,
        url: target.url,
        client: target.client || '',
        plan: target.plan,
        max_threads: target.max_threads,
        max_duration: target.max_duration,
        authorized_until: target.authorized_until,
        enabled: input.checked,
      }),
    });
    toast(`${key} 已${input.checked ? '啟用' : '停用'}`, 'ok');
    await refreshState();
  } catch (err) {
    toast(err.message, 'err');
    input.checked = !input.checked;
  }
});

async function openTargetModal(key) {
  editingTargetKey = key;
  await ensureTargetPlans();
  targetForm.reset();
  targetForm.max_threads.value = '600';
  targetForm.max_duration.value = '600';
  targetForm.authorized_days.value = '30';
  targetForm.enabled.checked = true;
  setTargetStatus('', '');
  $('#target-modal-title').textContent = key ? `編輯目標 · ${key}` : '新增目標';

  if (key) {
    const target = latestTargets.find((item) => item.key === key);
    if (target) {
      targetForm.url.value = target.url || '';
      targetForm.key.value = target.key;
      targetForm.client.value = target.client || '';
      if (target.plan && targetPlans.some((p) => p.value === target.plan)) {
        targetForm.plan.value = target.plan;
      }
      targetForm.max_threads.value = target.max_threads;
      targetForm.max_duration.value = target.max_duration;
      targetForm.enabled.checked = target.status === 'approved';
    }
  }
  $('#target-modal').classList.remove('hidden');
  updatePlanHint();
  setTimeout(() => targetForm.url.focus(), 50);
}

function closeTargetModal() {
  $('#target-modal').classList.add('hidden');
  editingTargetKey = null;
}

function setTargetStatus(text, cls) {
  const element = $('#target-form-status');
  element.textContent = text;
  element.className = 'form-status span2' + (cls ? ' ' + cls : '');
}

targetForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = {
    url: targetForm.url.value.trim(),
    key: targetForm.key.value.trim() || null,
    client: targetForm.client.value.trim(),
    plan: targetForm.plan.value,
    max_threads: Number(targetForm.max_threads.value),
    max_duration: Number(targetForm.max_duration.value),
    authorized_days: Number(targetForm.authorized_days.value),
    enabled: targetForm.enabled.checked,
  };
  if (editingTargetKey) {
    const existing = latestTargets.find((item) => item.key === editingTargetKey);
    if (existing && existing.authorized_until) {
      payload.authorized_until = existing.authorized_until;
    }
  }
  $('#btn-target-save').disabled = true;
  setTargetStatus('儲存中…', '');
  try {
    if (editingTargetKey) {
      await api(`/api/targets/${encodeURIComponent(editingTargetKey)}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
    } else {
      await api('/api/targets', { method: 'POST', body: JSON.stringify(payload) });
    }
    closeTargetModal();
    toast('已儲存壓測目標', 'ok');
    await refreshState();
  } catch (err) {
    setTargetStatus(err.message, 'err');
  } finally {
    $('#btn-target-save').disabled = false;
  }
});

/* ---------------------------------------------------------------- 壓測報告 */

let lastResultsKey = null;

async function loadResults() {
  try {
    const data = await api('/api/results');
    renderResults(data.items || []);
  } catch (err) {
    /* 讀取失敗不影響主畫面 */
  }
}

function renderResults(items) {
  const box = $('#results-list');
  const countText = items.length ? `${items.length} 份` : '—';
  $('#results-count').textContent = countText;
  const navReports = $('#nav-badge-reports');
  if (navReports) navReports.textContent = String(items.length);

  if (!items.length) {
    box.innerHTML = '<div class="results-empty">尚無測試結果</div>';
    return;
  }
  box.innerHTML = items.map((item) => {
    const stem = encodeURIComponent(item.stem);
    const verdictBadge = item.ok === true
      ? '<span class="badge ok">達標</span>'
      : item.ok === false
        ? '<span class="badge err">未達標</span>'
        : '';
    const threads = item.threads ? `${item.threads} 併發` : '—';
    return `
      <div class="result-row">
        <div class="result-main">
          <div class="result-title-line">
            <span class="result-title">${esc(item.domain || item.stem)}</span>
            ${verdictBadge}
          </div>
          <div class="result-meta">${esc(item.mtime)} · ${threads} · ${item.size_kb} KB</div>
        </div>
        <div class="result-actions">
          <a class="btn small" href="/api/results/${stem}/report?format=html" target="_blank" rel="noopener">報告</a>
          <a class="btn small ghost" href="/api/results/${stem}/report?format=md">Markdown</a>
          <a class="btn small ghost" href="/api/results/${stem}/raw?kind=jtl">原始資料</a>
        </div>
      </div>`;
  }).join('');
}

/* ---------------------------------------------------------------- 帳號與安全 */

let latestUsers = [];
let editingUsername = null;

function roleLabel(role) {
  return role === 'admin' ? '系統管理員' : '操作員';
}

function renderMe(user) {
  if (!user) return;
  currentUser = user;
  const initials = (user.display_name || user.username || '?').trim().slice(0, 1).toUpperCase();
  const roleText = roleLabel(user.role);

  $('#user-avatar').textContent = initials;
  $('#user-name').textContent = user.display_name || user.username;
  $('#user-role').textContent = roleText;

  $('#profile-avatar').textContent = initials;
  $('#profile-display-name').textContent = user.display_name || user.username;
  $('#profile-username').textContent = `@${user.username}`;

  const roleBadge = $('#profile-role');
  roleBadge.textContent = roleText;
  roleBadge.className = 'badge ' + (user.role === 'admin' ? 'ok' : '');

  const lastLogin = user.last_login_at
    ? `${user.last_login_at.replace('T', ' ')}${user.last_login_ip ? ' · ' + user.last_login_ip : ''}`
    : '首次登入';
  $('#profile-last-login').textContent = `最近登入：${lastLogin}`;

  const profileForm = $('#profile-form');
  if (profileForm && document.activeElement !== profileForm.display_name) {
    profileForm.display_name.value = user.display_name || '';
  }

  $('#admin-users-panel').classList.toggle('hidden', user.role !== 'admin');
}

$('#user-pill').addEventListener('click', () => setView('accounts'));

function setInlineStatus(sel, text, cls) {
  const el = $(sel);
  if (!el) return;
  el.textContent = text;
  el.className = 'form-status' + (cls ? ' ' + cls : '');
}

$('#profile-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const displayName = $('#profile-form').display_name.value.trim();
  setInlineStatus('#profile-status', '儲存中…', 'busy');
  try {
    const res = await api('/api/account/profile', {
      method: 'PUT',
      body: JSON.stringify({ display_name: displayName }),
    });
    renderMe(res.user);
    setInlineStatus('#profile-status', '已更新顯示名稱', 'ok');
    toast('已更新顯示名稱', 'ok');
    if (currentUser && currentUser.role === 'admin') loadUsers();
  } catch (err) {
    setInlineStatus('#profile-status', err.message, 'err');
  }
});

$('#pwd-new').addEventListener('input', () => {
  updateStrength($('#pwd-new'), $('#pwd-strength'));
});

$('#password-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = $('#password-form');
  const currentPassword = form.current_password.value;
  const newPassword = form.new_password.value;
  const confirmPassword = form.confirm_password.value;

  if (newPassword.length < 8) {
    setInlineStatus('#password-status', '新密碼長度至少需要 8 個字元', 'err');
    return;
  }
  if (newPassword !== confirmPassword) {
    setInlineStatus('#password-status', '兩次輸入的新密碼不一致', 'err');
    return;
  }
  setInlineStatus('#password-status', '更新中…', 'busy');
  try {
    await api('/api/account/password', {
      method: 'POST',
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    });
    form.reset();
    updateStrength($('#pwd-new'), $('#pwd-strength'));
    setInlineStatus('#password-status', '密碼已更新，其他裝置已登出', 'ok');
    toast('登入密碼已更新', 'ok');
  } catch (err) {
    setInlineStatus('#password-status', err.message, 'err');
  }
});

async function loadUsers() {
  if (!currentUser || currentUser.role !== 'admin') return;
  try {
    const data = await api('/api/users');
    latestUsers = data.items || [];
    renderUsers(latestUsers);
  } catch (err) {
    if (err.message !== 'unauth') console.warn('[users]', err.message);
  }
}

function renderUsers(items) {
  const box = $('#users-list');
  const countChip = $('#users-count');
  const navAccounts = $('#nav-badge-accounts');
  if (countChip) countChip.textContent = `${items.length} 個帳號`;
  if (navAccounts) navAccounts.textContent = String(items.length);

  if (!items.length) {
    box.innerHTML = '<div class="results-empty">尚無帳號資料</div>';
    return;
  }

  box.innerHTML = items.map((u) => {
    const isSelf = currentUser && u.username === currentUser.username;
    const roleBadge = u.role === 'admin'
      ? '<span class="badge ok">系統管理員</span>'
      : '<span class="badge">操作員</span>';
    const stateBadge = u.enabled
      ? '<span class="badge ok">啟用中</span>'
      : '<span class="badge err">已停用</span>';
    const selfBadge = isSelf ? '<span class="region">目前登入</span>' : '';
    const lastLogin = u.last_login_at
      ? `${esc(u.last_login_at.replace('T', ' '))}${u.last_login_ip ? ' (' + esc(u.last_login_ip) + ')' : ''}`
      : '尚未登入';

    return `
      <article class="user-row ${u.enabled ? '' : 'off'}">
        <div class="avatar-circle">${esc((u.display_name || u.username).slice(0, 1).toUpperCase())}</div>
        <div class="user-row-main">
          <div class="user-row-head">
            <span class="user-row-name">${esc(u.display_name || u.username)}</span>
            <span class="user-row-id mono">@${esc(u.username)}</span>
            ${roleBadge}
            ${stateBadge}
            ${selfBadge}
          </div>
          <div class="user-row-meta">
            <span>建立 <b>${esc((u.created_at || '—').replace('T', ' '))}</b></span>
            <span>最近登入 <b>${lastLogin}</b></span>
          </div>
        </div>
        <div class="user-row-actions">
          <label class="switch" title="${u.enabled ? '已啟用' : '已停用'}">
            <input type="checkbox" data-act="toggle-user" data-username="${esc(u.username)}" ${u.enabled ? 'checked' : ''} ${isSelf ? 'disabled' : ''}>
            <span class="slider"></span>
          </label>
          <button type="button" class="btn small ghost" data-act="revoke-user" data-username="${esc(u.username)}" title="使該帳號所有裝置的工作階段失效">強制下線</button>
          <button type="button" class="icon-mini" data-act="edit-user" data-username="${esc(u.username)}" title="編輯">${ICON_EDIT}</button>
          ${isSelf ? '' : `<button type="button" class="icon-mini danger" data-act="delete-user" data-username="${esc(u.username)}" title="刪除">${ICON_TRASH}</button>`}
        </div>
      </article>`;
  }).join('');
}

const userForm = $('#user-form');

$('#btn-add-user').addEventListener('click', () => openUserModal(null));
$('#user-modal-close').addEventListener('click', closeUserModal);
$('#btn-user-cancel').addEventListener('click', closeUserModal);
$('#user-modal').addEventListener('click', (event) => {
  if (event.target.id === 'user-modal') closeUserModal();
});

$('#user-modal-password').addEventListener('input', () => {
  updateStrength($('#user-modal-password'), $('#user-pwd-strength'));
});

$('#btn-gen-password').addEventListener('click', async () => {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*';
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let pwd = '';
  for (let i = 0; i < bytes.length; i += 1) {
    pwd += alphabet[bytes[i] % alphabet.length];
  }
  const input = $('#user-modal-password');
  input.type = 'text';
  input.value = pwd;
  updateStrength(input, $('#user-pwd-strength'));
  try {
    await copyText(pwd);
    toast('已產生高強度密碼並複製到剪貼簿', 'ok');
  } catch (_) {
    toast('已產生高強度密碼', 'ok');
  }
});

function openUserModal(username) {
  editingUsername = username;
  userForm.reset();
  userForm.role.value = 'operator';
  userForm.enabled.checked = true;
  userForm.username.readOnly = Boolean(username);
  const pwdInput = $('#user-modal-password');
  pwdInput.type = 'password';
  pwdInput.required = !username;
  $('#user-pwd-label').textContent = username ? '重設密碼（留空則不變更）' : '登入密碼（至少 8 碼）';
  pwdInput.placeholder = username ? '留空則保留原密碼' : '至少 8 字元';
  updateStrength(pwdInput, $('#user-pwd-strength'));
  setInlineStatus('#user-form-status', '', '');
  $('#user-modal-title').textContent = username ? `編輯帳號 · @${username}` : '新增帳號';

  if (username) {
    const target = latestUsers.find((u) => u.username === username);
    if (target) {
      userForm.username.value = target.username;
      userForm.display_name.value = target.display_name || '';
      userForm.role.value = target.role || 'operator';
      userForm.enabled.checked = Boolean(target.enabled);
    }
  }
  $('#user-modal').classList.remove('hidden');
  setTimeout(() => (username ? userForm.display_name : userForm.username).focus(), 50);
}

function closeUserModal() {
  const modal = $('#user-modal');
  if (modal) modal.classList.add('hidden');
  editingUsername = null;
}

userForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const username = userForm.username.value.trim().toLowerCase();
  const displayName = userForm.display_name.value.trim();
  const role = userForm.role.value;
  const password = userForm.password.value;
  const enabled = userForm.enabled.checked;

  if (!editingUsername && password.length < 8) {
    setInlineStatus('#user-form-status', '密碼長度至少需要 8 個字元', 'err');
    return;
  }
  if (editingUsername && password && password.length < 8) {
    setInlineStatus('#user-form-status', '新密碼長度至少需要 8 個字元', 'err');
    return;
  }

  $('#btn-user-save').disabled = true;
  setInlineStatus('#user-form-status', '儲存中…', 'busy');
  try {
    if (editingUsername) {
      const payload = {
        display_name: displayName,
        role,
        enabled,
      };
      if (password) payload.new_password = password;
      await api(`/api/users/${encodeURIComponent(editingUsername)}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
      toast(`已更新帳號 @${editingUsername}`, 'ok');
    } else {
      await api('/api/users', {
        method: 'POST',
        body: JSON.stringify({
          username,
          display_name: displayName,
          role,
          password,
          enabled,
        }),
      });
      toast(`已建立帳號 @${username}`, 'ok');
    }
    closeUserModal();
    await loadUsers();
    await refreshState();
  } catch (err) {
    setInlineStatus('#user-form-status', err.message, 'err');
  } finally {
    $('#btn-user-save').disabled = false;
  }
});

$('#users-list').addEventListener('click', async (event) => {
  const btn = event.target.closest('[data-act]');
  if (!btn) return;
  const { act, username } = btn.dataset;
  if (!username) return;

  if (act === 'edit-user') {
    openUserModal(username);
  } else if (act === 'revoke-user') {
    if (!confirm(`確定強制下線帳號 @${username} 的所有已登入裝置？`)) return;
    try {
      const res = await api(`/api/users/${encodeURIComponent(username)}/revoke`, { method: 'POST' });
      toast(res.self ? '已使其他裝置登出（保留目前連線）' : `已強制下線 @${username}`, 'ok');
    } catch (err) {
      toast(err.message, 'err');
    }
  } else if (act === 'delete-user') {
    if (!confirm(`確定永久刪除帳號 @${username}？刪除後該帳號將無法再登入。`)) return;
    try {
      await api(`/api/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
      toast(`已刪除帳號 @${username}`, 'ok');
      await loadUsers();
      await refreshState();
    } catch (err) {
      toast(err.message, 'err');
    }
  }
});

$('#users-list').addEventListener('change', async (event) => {
  const input = event.target.closest('input[data-act="toggle-user"]');
  if (!input) return;
  const username = input.dataset.username;
  try {
    await api(`/api/users/${encodeURIComponent(username)}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled: input.checked }),
    });
    toast(`@${username} 已${input.checked ? '啟用' : '停用'}`, 'ok');
    await loadUsers();
  } catch (err) {
    toast(err.message, 'err');
    input.checked = !input.checked;
  }
});

/* ---------------------------------------------------------------- 啟動 */

$('#btn-refresh').addEventListener('click', async () => {
  const btn = $('#btn-refresh');
  btn.classList.add('is-spinning');
  btn.disabled = true;
  const ok = await refreshState();
  btn.classList.remove('is-spinning');
  btn.disabled = false;
  if (ok) toast('狀態已更新', 'ok');
});

async function boot() {
  try {
    const session = await api('/api/session');
    if (!session.authed) {
      showLogin(Boolean(session.setup_required));
      return;
    }
    if (session.user) renderMe(session.user);
    showApp();
    await refreshState();
    if (currentUser && currentUser.role === 'admin') {
      loadUsers();
    }
    if (stateTimer) clearInterval(stateTimer);
    stateTimer = setInterval(refreshState, 5000);
    loadResults();
    setInterval(loadResults, 30000);
    if (location.hash === '#add') openNodeModal(null);
  } catch (err) {
    if (err.message !== 'unauth') console.warn('[boot]', err.message);
  }
}

boot();
