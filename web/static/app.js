const $ = (sel) => document.querySelector(sel);

const ICON_EDIT = '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M11.2 2.1l2.7 2.7-8.3 8.3-3.5.8.8-3.5z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>';
const ICON_TRASH = '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M3 4.6h10M6.4 4.6V3.1h3.2v1.5M4.6 4.6l.6 8.3h5.6l.6-8.3" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';

let latestState = null;
let activeJobId = null;
let editingKey = null;
let jobTimer = null;
let stateTimer = null;
let renderedLines = 0;

/* ---------------------------------------------------------------- 工具 */

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (res.status === 401) {
    showLogin();
    throw new Error('unauth');
  }
  let data = null;
  try { data = await res.json(); } catch (_) { /* 無 JSON 內容 */ }
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
  return data;
}

function showLogin() {
  $('#login').classList.remove('hidden');
  $('#app').classList.add('hidden');
  stopTimers();
  $('#token').focus();
}

function showApp() {
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
}

function stopTimers() {
  if (stateTimer) { clearInterval(stateTimer); stateTimer = null; }
  if (jobTimer) { clearInterval(jobTimer); jobTimer = null; }
}

/* ---------------------------------------------------------------- 登入 */

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#login-error').textContent = '';
  try {
    await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({ token: $('#token').value.trim() }),
    });
    $('#token').value = '';
    await boot();
  } catch (err) {
    $('#login-error').textContent = err.message === 'unauth' ? '權杖不正確' : err.message;
  }
});

$('#btn-logout').addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST' });
  showLogin();
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
  } catch (err) {
    if (err.message !== 'unauth') console.warn('[state]', err.message);
  }
}

function renderState(state) {
  const { server, summary, nodes, job } = state;

  $('#server-line').textContent =
    `${server.hostname} · ${server.master_ip} · JMeter ${server.jmeter}`;

  const onlineChip = $('#chip-online');
  onlineChip.textContent = `${summary.online} / ${summary.total} 在線`;
  onlineChip.className = 'chip ' + (summary.online === summary.total && summary.total > 0 ? 'ok' : summary.online === 0 ? 'err' : 'warn');

  $('#chip-capacity').textContent = `容量 ${summary.capacity}`;

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
  renderBot(state.bot);
  renderLoadtest(state.loadtest);
  if (job) renderJobChip(job);
}

function sparkline(history) {
  const values = history.filter((v) => typeof v === 'number');
  if (values.length < 2) return '<div class="spark-empty">延遲資料累積中…</div>';
  const width = 300;
  const height = 34;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = Math.max(max - min, 1);
  const step = width / Math.max(history.length - 1, 1);
  let path = '';
  let drawing = false;
  history.forEach((value, index) => {
    if (typeof value !== 'number') { drawing = false; return; }
    const x = index * step;
    const y = height - ((value - min) / span) * (height - 6) - 3;
    path += `${drawing ? ' L' : ' M'}${x.toFixed(1)} ${y.toFixed(1)}`;
    drawing = true;
  });
  return `<svg class="spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    <path d="${path}" fill="none" stroke="var(--accent)" stroke-width="1.6"
          stroke-linejoin="round" stroke-linecap="round" opacity="0.85"></path>
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

    const sshBadge = node.ssh_ok
      ? '<span class="badge ok">SSH 可用</span>'
      : '<span class="badge err">SSH 不通</span>';

    const deployStatus = busy && job.nodes && job.nodes[node.key]
      ? `<span class="badge">${esc(job.nodes[node.key])}</span>`
      : sshBadge;

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
          <span class="addr mono">${esc(node.host)}:${node.port}</span>
          <span class="host-actions">
            <button class="icon-mini" data-act="edit" data-key="${esc(node.key)}" title="編輯">${ICON_EDIT}</button>
            <button class="icon-mini danger" data-act="delete" data-key="${esc(node.key)}" title="刪除">${ICON_TRASH}</button>
          </span>
        </div>
        <div class="node-metrics">
          <div class="metric">
            <span class="k">延遲</span>
            <span class="v ${node.online ? '' : 'bad'}">${latency}</span>
          </div>
          <div class="metric">
            <span class="k">執行緒上限</span>
            <span class="v">${node.max_threads}</span>
          </div>
          <div class="metric">
            <span class="k">檢查</span>
            <span class="v" style="font-weight:500;font-size:12.5px;color:var(--muted)">
              ${node.checked_ago == null ? '—' : Math.round(node.checked_ago) + ' 秒前'}
            </span>
          </div>
        </div>
        <div class="spark-wrap">${sparkline(node.history)}</div>
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
  const { act, key } = button.dataset;
  if (act === 'deploy') {
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
    await refreshState();
  } catch (err) {
    alert(`切換失敗：${err.message}`);
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
    await refreshState();
  } catch (err) {
    alert(`刪除失敗：${err.message}`);
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
    await refreshState();
  } catch (err) {
    alert(`無法開始部署：${err.message}`);
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
  const chip = $('#job-chip');
  const map = {
    running: 'run',
    success: 'ok',
    failed: 'err',
    cancelled: 'warn',
  };
  chip.textContent = jobLabel(job);
  chip.className = 'chip ' + (map[job.status] || '');

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
  chip.textContent = jobLabel(job);
  chip.className = 'chip ' + (map[job.status] || '');
}

$('#btn-cancel').addEventListener('click', async () => {
  if (!activeJobId) return;
  if (!confirm('確定中止目前的部署任務？')) return;
  await api(`/api/jobs/${activeJobId}/cancel`, { method: 'POST' });
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
  $('#lt-meta').innerHTML = meta.length ? meta.join(' · ') : '<span>—</span>';

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

// 每秒在前端推算秒數，不必等 5 秒輪詢
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
  $('#results-count').textContent = items.length ? `${items.length} 份` : '—';
  if (!items.length) {
    box.innerHTML = '<div class="results-empty">尚無測試結果</div>';
    return;
  }
  box.innerHTML = items.map((item) => {
    const stem = encodeURIComponent(item.stem);
    const verdict = item.ok === true ? ' · 達標' : item.ok === false ? ' · 未達標' : '';
    const threads = item.threads ? `${item.threads} 併發` : '—';
    return `
      <div class="result-row">
        <div class="result-main">
          <div class="result-title">${esc(item.domain || item.stem)}</div>
          <div class="result-meta">${esc(item.mtime)} · ${threads} · ${item.size_kb} KB${verdict}</div>
        </div>
        <div class="result-actions">
          <a class="btn small" href="/api/results/${stem}/report?format=html">報告</a>
          <a class="btn small ghost" href="/api/results/${stem}/report?format=md">Markdown</a>
          <a class="btn small ghost" href="/api/results/${stem}/raw?kind=jtl">原始資料</a>
        </div>
      </div>`;
  }).join('');
}

/* ---------------------------------------------------------------- 啟動 */

$('#btn-refresh').addEventListener('click', refreshState);

async function boot() {
  try {
    const session = await api('/api/session');
    if (!session.authed) { showLogin(); return; }
    showApp();
    await refreshState();
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
