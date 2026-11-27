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

$('#btn-add-node').addEventListener('click', () => openNodeModal(null));
$('#node-modal-close').addEventListener('click', closeNodeModal);
$('#btn-node-cancel').addEventListener('click', closeNodeModal);
$('#node-modal').addEventListener('click', (event) => {
  if (event.target.id === 'node-modal') closeNodeModal();
});

function openNodeModal(key) {
  editingKey = key;
  nodeForm.reset();
  nodeForm.port.value = '1099';
  nodeForm.ssh_port.value = '22';
  nodeForm.max_threads.value = '600';
  nodeForm.enabled.checked = true;
  setFormStatus('', '');
  $('#node-modal-title').textContent = key ? `編輯伺服器 · ${key}` : '新增伺服器';

  if (key && latestState) {
    const node = latestState.nodes.find((item) => item.key === key);
    if (node) {
      nodeForm.key.value = node.key;
      nodeForm.host.value = node.host;
      nodeForm.port.value = node.port;
      nodeForm.ssh_port.value = node.ssh_port || 22;
      nodeForm.region.value = node.region === '—' ? '' : node.region;
      nodeForm.max_threads.value = node.max_threads;
      nodeForm.note.value = node.note || '';
      nodeForm.enabled.checked = node.enabled;
    }
  }
  $('#node-modal').classList.remove('hidden');
  setTimeout(() => nodeForm.key.focus(), 50);
}

function closeNodeModal() {
  $('#node-modal').classList.add('hidden');
  editingKey = null;
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
  };
}

nodeForm.addEventListener('submit', (event) => {
  event.preventDefault();
  saveNode(false);
});

$('#btn-node-save-deploy').addEventListener('click', () => saveNode(true));

async function saveNode(deployAfter) {
  const payload = formPayload();
  if (!payload.key || !payload.host) {
    setFormStatus('名稱與主機為必填', 'err');
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
    $('#btn-node-save-deploy').disabled = false;
  }
}

$('#btn-test-node').addEventListener('click', async () => {
  const payload = formPayload();
  if (!payload.host) {
    setFormStatus('請先填主機位址', 'err');
    return;
  }
  setFormStatus('測試中…', 'busy');
  try {
    const result = await api('/api/probe', {
      method: 'POST',
      body: JSON.stringify({
        host: payload.host,
        port: payload.port,
        ssh_port: payload.ssh_port,
      }),
    });
    const parts = [
      result.rmi
        ? `RMI ${payload.port} 可達（${Math.round(result.rmi_ms)} ms）`
        : `RMI ${payload.port} 無回應`,
      result.ssh ? `SSH ${payload.ssh_port} 可達` : `SSH ${payload.ssh_port} 不通`,
    ];
    setFormStatus(parts.join(' · '), result.rmi && result.ssh ? 'ok' : 'err');
  } catch (err) {
    setFormStatus(err.message, 'err');
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
    running: ['執行中', 'run'],
    success: ['成功', 'ok'],
    failed: ['有失敗', 'err'],
    cancelled: ['已中止', 'warn'],
  };
  const [label, cls] = map[job.status] || ['—', ''];
  chip.textContent = label;
  chip.className = 'chip ' + cls;

  $('#btn-cancel').classList.toggle('hidden', job.status !== 'running');
  $('#btn-clear-log').disabled = job.status === 'running';
  $('#btn-deploy-all').disabled = job.status === 'running';

  if (Array.isArray(job.lines)) {
    const log = $('#log');
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    log.innerHTML = job.lines.map(lineHtml).join('') || '<div class="log-empty">（無輸出）</div>';
    if (nearBottom) log.scrollTop = log.scrollHeight;
  }
}

function renderJobChip(job) {
  const chip = $('#job-chip');
  const map = {
    running: ['執行中', 'run'],
    success: ['成功', 'ok'],
    failed: ['有失敗', 'err'],
    cancelled: ['已中止', 'warn'],
  };
  const [label, cls] = map[job.status] || ['—', ''];
  chip.textContent = label;
  chip.className = 'chip ' + cls;
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
    if (location.hash === '#add') openNodeModal(null);
  } catch (err) {
    if (err.message !== 'unauth') console.warn('[boot]', err.message);
  }
}

boot();
