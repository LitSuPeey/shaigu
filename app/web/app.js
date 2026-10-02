// ========= 通用工具 =========
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));


// 表单数值读取（缺省兜底）
function num(sel, dflt) {
  const el = $(sel);
  if (!el) return dflt;
  const v = parseFloat(el.value);
  return Number.isFinite(v) ? v : dflt;
}

// CSV 下载：直接从结果表格提取（BOM 头保证 Excel 中文不乱码）
function downloadCsv(tableId, filename) {
  const t = $('#' + tableId);
  if (!t) return;
  const head = [...t.querySelectorAll('thead th')].map(th => th.textContent);
  const body = [...t.querySelectorAll('tbody tr')]
    .filter(tr => tr.children.length === head.length || head.length === 0)
    .map(tr => [...tr.querySelectorAll('td')].map(td => {
      const txt = td.textContent.replace(/\s+/g, ' ').trim();
      return /[",\n]/.test(txt) ? '"' + txt.replace(/"/g, '""') + '"' : txt;
    }).join(','));
  if (!body.length) { alert('当前没有结果可下载'); return; }
  const csv = '\ufeff' + [head.join(','), ...body].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

// 波动幅度开关：similar 模式才显示阈值输入
function bindSimilarVolToggle() {
  const sel = $('#similarVolMode'), wrap = $('#similarVolThreshWrap');
  if (sel && wrap) {
    const sync = () => { wrap.hidden = sel.value !== 'similar'; };
    sel.addEventListener('change', sync); sync();
  }
}

// 带超时的 fetch：避免后端卡住时前端无限等待导致 loading 界面卡死
// run 类长任务挂到全局 _activeFetchCtrl，取消按钮可中断
let _activeFetchCtrl = null;   // 当前运行中的可取消请求
let _userCancelled = false;    // 区分「用户取消」和「超时」
let _bgTask = null;            // 后台运行中的任务 {label, failed, settled}

function fetchJSON(url, options = {}, timeoutMs = 600000, cancellable = false) {
  // 已有后台任务在跑时不允许再叠一个长任务（后端进度状态是全局单槽，并发会互踩）
  if (cancellable && _bgTask) {
    return Promise.reject(new Error('已有后台任务在运行（见右下角角标，点它可恢复进度窗口），等它完成或取消后再开始新任务'));
  }
  const ctrl = new AbortController();
  if (cancellable) { _activeFetchCtrl = ctrl; _userCancelled = false; }
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  return fetch(url, { ...options, signal: ctrl.signal })
    .then(r => r.json())
    .catch(e => {
      if (_bgTask && _activeFetchCtrl === ctrl) _bgTask.failed = true;
      if (e && e.name === 'AbortError') {
        throw new Error(_userCancelled ? '已取消' : `请求超时（超过 ${Math.round(timeoutMs / 1000)} 秒）`);
      }
      throw e;
    })
    .finally(() => {
      clearTimeout(timer);
      if (cancellable && _activeFetchCtrl === ctrl) {
        _activeFetchCtrl = null;
        if (_bgTask) _bgTask.settled = true;   // 后台任务收尾：hideOverlay 时据此弹完成提示
      }
    });
}

const API = {
  status: () => fetchJSON('/api/status', {}, 15000),
  stocks: p => fetchJSON('/api/stocks?' + new URLSearchParams(p || {}), {}, 15000),
  kline: (code, days = 180) => fetchJSON(`/api/kline?code=${encodeURIComponent(code)}&days=${days}`, {}, 30000),
  strategiesList: () => fetchJSON('/api/strategies/list', {}, 15000),
  strategiesRun: body => fetchJSON('/api/strategies/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 600000, true),
  conditionsList: () => fetchJSON('/api/conditions/list', {}, 15000),
  conditionsRun: body => fetchJSON('/api/conditions/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 600000, true),
  patternRun: body => fetchJSON('/api/pattern/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 600000, true),
  snapRun: body => fetchJSON('/api/snap/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 600000, true),
  r9Meta: () => fetchJSON('/api/r9/meta', {}, 20000),
  r9Run: body => fetchJSON('/api/r9/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 900000, true),
  betonMeta: () => fetchJSON('/api/beton/meta', {}, 20000),
  betonRun: body => fetchJSON('/api/beton/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 900000, true),
  betonKline: (code, years) => fetchJSON(`/api/beton/kline?code=${encodeURIComponent(code)}&years=${years || 12}`, {}, 60000),
  antRun: body => fetchJSON('/api/ant/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 600000, true),
  ant1000Run: body => fetchJSON('/api/ant1000/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 3600000, true),
  ant1000Cache: () => fetchJSON('/api/ant1000/cache', {}, 30000),
  ant1000Kline: (code, years) => fetchJSON(`/api/ant1000/kline?code=${encodeURIComponent(code)}&years=${years || 20}`, {}, 60000),
  similarRun: body => fetchJSON('/api/similar/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 600000, true),
  alertRun: body => fetchJSON('/api/alert/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 600000, true),
  alertReport: body => fetchJSON('/api/alert/report', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 60000),
  ticker: force => fetchJSON('/api/ticker' + (force ? '?refresh=1' : ''), {}, 30000),
  cycleRun: body => fetchJSON('/api/cycle/analyze', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 120000),
  futuresScan: body => fetchJSON('/api/futures/scan', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 300000, true),
  futuresSectors: () => fetchJSON('/api/futures/sectors', {}, 15000),
  biliMeta: () => fetchJSON('/api/bili/meta', {}, 20000),
  biliUps: () => fetchJSON('/api/bili/ups', {}, 15000),
  biliUpAdd: body => fetchJSON('/api/bili/ups/add', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 60000),
  biliUpRemove: body => fetchJSON('/api/bili/ups/remove', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 15000),
  biliUpRename: body => fetchJSON('/api/bili/ups/rename', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 30000),
  biliScan: body => fetchJSON('/api/bili/scan', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 3600000, true),
  biliSummary: body => fetchJSON('/api/bili/summary', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 60000),
  biliSessionReset: () => fetchJSON('/api/bili/session/reset', { method: 'POST' }, 60000),
  biliCacheClear: () => fetchJSON('/api/bili/cache/clear', { method: 'POST' }, 30000),
  biliCredSet: body => fetchJSON('/api/bili/cred/set', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 90000),
  biliCredClear: () => fetchJSON('/api/bili/cred/clear', { method: 'POST' }, 60000),
  biliAsrState: () => fetchJSON('/api/bili/asr/state', {}, 30000),
  biliAsrHelp: () => fetchJSON('/api/bili/asr/help', {}, 20000),
  biliArchive: () => fetchJSON('/api/bili/archive', {}, 20000),
  biliArchiveClear: body => fetchJSON('/api/bili/archive/clear', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body || {}) }, 30000),
  divtimingStats: () => fetchJSON('/api/divtiming/stats', {}, 60000),
  divtimingWeek: dateStr => fetchJSON('/api/divtiming/week' + (dateStr ? `?date_str=${encodeURIComponent(dateStr)}` : ''), {}, 60000),
  divtimingFullMarket: () => fetchJSON('/api/divtiming/full_market', {}, 20000),
  syncStart: body => fetchJSON('/api/sync/start', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 30000),
  syncStop: () => fetchJSON('/api/sync/stop', { method: 'POST' }, 10000),
  syncProgress: () => fetchJSON('/api/sync/progress', {}, 15000),
  progress: () => fetchJSON('/api/progress', {}, 15000),
  cancel: () => fetchJSON('/api/cancel', { method: 'POST' }, 10000),
  refreshMeta: () => fetchJSON('/api/sync/refresh_meta', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' }, 30000),
  freshness: () => fetchJSON('/api/data/freshness', {}, 15000),
};

// ========= 表情包系统（30 张全量接入） =========
const MEMES = {
  welcome: ['memes/表情套组_默认_你好！.png', 'memes/开心.png', 'memes/表情套组_卫戍专用_happy.png'],
  empty: ['memes/表情套组_默认_？？？.png', 'memes/表情套组_默认_换换项目！.png',
          'memes/表情套组_虫动_？？？.png', 'memes/表情套组_虫动_换换项目！.png'],
  success: ['memes/表情套组_默认_合作愉快！.png', 'memes/表情套组_虫动_合作愉快！.png',
            'memes/表情套组_卫戍专用_noproblem.png', 'memes/表情套组_卫戍专用_respect.png',
            'memes/表情套组_卫戍专用_cooperate.png'],
  error: ['memes/表情套组_默认_对不起！.png', 'memes/表情套组_虫动_对不起！.png',
          'memes/表情套组_卫戍专用_sad.png', 'memes/表情套组_卫戍专用_sorry.png',
          'memes/表情套组_卫戍专用_scared.png', 'memes/表情套组_卫戍专用_dying.png'],
  ask: ['memes/表情套组_默认_你请先选！.png', 'memes/表情套组_默认_我想先选！.png',
        'memes/表情套组_卫戍专用_thinking.png', 'memes/表情套组_卫戍专用_call.png',
        'memes/表情套组_默认_请快些！.png'],
  thanks: ['memes/表情套组_默认_谢谢！.png', 'memes/表情套组_卫戍专用_thanks.png'],
  farewell: ['memes/表情套组_默认_再见！.png', 'memes/表情套组_卫戍专用_playingcool.png'],
};
// 加载轮播池：全部 30 张表情包都在等待时轮换登场
const MEME_ALL = [
  'memes/开心.png',
  'memes/表情套组_默认_你好！.png', 'memes/表情套组_默认_再见！.png',
  'memes/表情套组_默认_合作愉快！.png', 'memes/表情套组_默认_对不起！.png',
  'memes/表情套组_默认_很快就好！.png', 'memes/表情套组_默认_我想先选！.png',
  'memes/表情套组_默认_换换项目！.png', 'memes/表情套组_默认_要上啦！.png',
  'memes/表情套组_默认_请快些！.png', 'memes/表情套组_默认_你请先选！.png',
  'memes/表情套组_默认_？？？.png', 'memes/表情套组_默认_谢谢！.png',
  'memes/表情套组_虫动_合作愉快！.png', 'memes/表情套组_虫动_对不起！.png',
  'memes/表情套组_虫动_很快就好！.png', 'memes/表情套组_虫动_换换项目！.png',
  'memes/表情套组_虫动_？？？.png',
  'memes/表情套组_卫戍专用_call.png', 'memes/表情套组_卫戍专用_cooperate.png',
  'memes/表情套组_卫戍专用_dying.png', 'memes/表情套组_卫戍专用_happy.png',
  'memes/表情套组_卫戍专用_noproblem.png', 'memes/表情套组_卫戍专用_playingcool.png',
  'memes/表情套组_卫戍专用_respect.png', 'memes/表情套组_卫戍专用_sad.png',
  'memes/表情套组_卫戍专用_scared.png', 'memes/表情套组_卫戍专用_sorry.png',
  'memes/表情套组_卫戍专用_thanks.png', 'memes/表情套组_卫戍专用_thinking.png',
];
const pickMeme = (kind, i = 0) => {
  const pool = MEMES[kind] || MEMES.empty;
  return pool[i % pool.length];
};
const pickMemeRandom = kind => {
  const pool = MEMES[kind] || MEMES.empty;
  return pool[Math.floor(Math.random() * pool.length)];
};
const thsLink = code => `https://stockpage.10jqka.com.cn/${code}/`;

const state = { range: 'all', sectors: [], syncPoll: null, syncing: false };

// ========= 表格渲染（动态列） =========
// 列名踩中此正则才会按正负做红绿着色（中国习惯：涨红跌绿）。新增涨跌类列命名时
// 务必含「涨跌幅/涨幅/幅度/涨跌/超额/收益/振幅/回撤/回报/宽度/净流入」之一。
const PCT_RE = /涨跌幅|涨幅|幅度|涨跌|超额|收益|振幅|回撤|回报|宽度|净流入/i;
function renderTable(tableId, rows, opts = {}) {
  const t = $('#' + tableId);
  if (!t) return;
  const thead = t.querySelector('thead');
  const tbody = t.querySelector('tbody');
  if (!rows || rows.length === 0) {
    thead.innerHTML = '';
    tbody.innerHTML = `<tr><td style="text-align:center; padding:26px; color:var(--ink-faint);">— 暂无结果 —</td></tr>`;
    return;
  }
  const cols = Object.keys(rows[0]);
  thead.innerHTML = '<tr>' + cols.map(c => `<th>${c}</th>`).join('') + '</tr>';
  const MAX = opts.max || 300;
  const shown = rows.slice(0, MAX);
  // 点击行为说明行：有股票代码列的表格才显示（CSV 导出时因列数不符会被自动过滤）
  const hasCodeCol = cols.some(c => c === '代码' || c === 'code');
  const tipRow = hasCodeCol
    ? `<tr class="table-tip"><td colspan="${cols.length}">💡 点击代码＝快速查看（站内 K 线）· 点击名称/简称＝详细查询（同花顺新窗口）</td></tr>`
    : '';
  tbody.innerHTML = tipRow + shown.map(r => '<tr>' + cols.map(c => {
    const v = r[c];
    if (c === '代码' || c === 'code') {
      // 代码：站内切换到 K 线查看（快速查看）
      return `<td><a href="#" class="stock-link code-link" data-code="${v}" title="快速查看 · 站内看K线">${v}</a></td>`;
    }
    if (c === '名称' || c === 'name') {
      const code = r['代码'] || r['code'] || '';
      // 名称：新标签页打开同花顺（详细查询）
      return `<td><a href="${thsLink(code)}" target="_blank" rel="noopener noreferrer" class="stock-link name-link" data-code="${code}" title="详细查询 · 同花顺新窗口">${v ?? ''}</a></td>`;
    }
    if (c === '简称') {
      // 简称（预警国家队等表格）：同样跳同花顺（详细查询）
      const code = r['代码'] || r['code'] || '';
      if (!code) return `<td>${v ?? ''}</td>`;
      return `<td><a href="${thsLink(code)}" target="_blank" rel="noopener noreferrer" class="stock-link name-link" title="详细查询 · 同花顺新窗口">${v ?? ''}</a></td>`;
    }
    if (typeof v === 'number') {
      const isPct = PCT_RE.test(c);
      let cls = '';
      if (isPct) cls = v > 0 ? 'up' : (v < 0 ? 'down' : '');
      return `<td class="num ${cls}" title="${v}">${v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}</td>`;
    }
    if (v == null || v === '') return `<td style="color:var(--ink-faint)">—</td>`;
    return `<td title="${escHtml(String(v))}">${v}</td>`;
  }).join('') + '</tr>').join('');
  if (rows.length > shown.length) {
    tbody.innerHTML += `<tr><td colspan="${cols.length}" style="text-align:center; color:var(--ink-faint); padding:10px;">…显示前 ${shown.length} / 共 ${rows.length} 行</td></tr>`;
  }
  // 代码链接：站内切 K 线（快速查看），经 switchTab 记录来源界面供「↩ 退回」
  tbody.querySelectorAll('.code-link').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      switchTab('kline');
      $('#klineCode').value = a.dataset.code;
      loadKline();
    });
  });
  // 名称链接：保持原生 target=_blank 新标签页打开同花顺，无需 JS 干预
}

// ========= 表情包反馈框 =========
function showMeme(id, kind, big, sub) {
  const box = $('#' + id);
  box.hidden = false;
  box.className = 'result-meme ' + kind;
  box.querySelector('img').src = pickMemeRandom(kind);
  box.querySelector('.meme-big').textContent = big;
  box.querySelector('.meme-sub').textContent = sub || '';
}
function hideMeme(id) { const b = $('#' + id); if (b) b.hidden = true; }

// ========= 加载进度条轮询 + 表情包轮播 =========
let _progressTimer = null;
let _memeTimer = null;
let _memeIdx = 0;

function startProgressPolling() {
  stopProgressPolling();
  $('#overlayFill').style.width = '0%';
  $('#overlayProgressText').textContent = '';
  _progressTimer = setInterval(async () => {
    try {
      const p = await API.progress();
      if (p.status === 'running') {
        const pct = Math.max(0, Math.min(100, Math.round((p.frac || 0) * 100)));
        $('#overlayFill').style.width = pct + '%';
        $('#overlayProgressText').textContent =
          pct + '%' + (p.msg ? ' · ' + p.msg : '');
      }
    } catch (e) { /* 忽略轮询异常 */ }
  }, 400);
}
function stopProgressPolling() {
  if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
}

// loading 时所有表情包轮流登场（每 2.2s 换一张，不连续重复）
function startMemeCarousel() {
  stopMemeCarousel();
  _memeIdx = Math.floor(Math.random() * MEME_ALL.length);
  _memeTimer = setInterval(() => {
    _memeIdx = (_memeIdx + 1) % MEME_ALL.length;
    const img = $('#overlayImg');
    img.classList.add('swap');
    setTimeout(() => {
      img.src = MEME_ALL[_memeIdx];
      img.classList.remove('swap');
    }, 180);
  }, 2200);
}
function stopMemeCarousel() {
  if (_memeTimer) { clearInterval(_memeTimer); _memeTimer = null; }
}

// 取消正在运行的任务：通知后端停算 + 中断前端请求
async function cancelRunning() {
  _userCancelled = true;
  _bgTask = null;          // 用户主动取消：清掉后台状态，不再弹「已完成」提示
  hideBgPill();
  try { await API.cancel(); } catch (e) { /* 忽略 */ }
  if (_activeFetchCtrl) {
    try { _activeFetchCtrl.abort(); } catch (e) { /* 忽略 */ }
  }
  $('#overlayText').textContent = '已取消任务';
  const img = $('#overlayImg');
  stopMemeCarousel();
  img.src = pickMemeRandom('farewell');
  setTimeout(hideOverlay, 900);
}

function showOverlay(kind, text, opts = {}) {
  $('#overlayImg').src = pickMemeRandom(kind);
  $('#overlayText').textContent = text || '';
  $('#overlay').hidden = false;
  const cancelBtn = $('#btnCancelTask');
  // 只有筛选类长任务才显示取消按钮（初始加载/同步不显示）
  cancelBtn.hidden = !opts.cancellable;
  // 后台运行按钮与取消按钮同进退：短操作（清缓存等）没有后台运行的必要
  const bgBtn = $('#btnBgRun');
  if (bgBtn) bgBtn.hidden = !opts.cancellable;
  startProgressPolling();
  if (opts.cancellable) startMemeCarousel();
}
function hideOverlay() {
  $('#overlay').hidden = true;
  stopProgressPolling();
  stopMemeCarousel();
  // 后台任务此时收尾：右下角弹完成提示（正常完成/失败文案不同）
  if (_bgTask && _bgTask.settled) bgFinish();
}

// ========= 后台运行：收起加载遮罩，任务照常跑，右下角角标提示状态 =========
function bgLabel() {
  let t = ($('#overlayText').textContent || '任务').split('…')[0];
  t = t.replace(/（[^）]*）/g, '');   // 去成对括号（避免截出「正在抓取（串行限速…」这类残缺标签）
  t = t.split('，')[0].trim();
  if (!t) t = '任务';
  return t.length > 22 ? t.slice(0, 22) + '…' : t;
}
function hideBgPill() {
  const p = $('#bgPill');
  if (p) { p.hidden = true; p.onclick = null; }
}
// 点「后台运行」：只收起窗口，不中断请求、不通知后端取消
function minimizeOverlay() {
  const ov = $('#overlay');
  if (!ov || ov.hidden || !_activeFetchCtrl || _bgTask) return;
  _bgTask = { label: bgLabel(), failed: false, settled: false };
  ov.hidden = true;
  stopProgressPolling();
  stopMemeCarousel();
  const pill = $('#bgPill');
  pill.hidden = false;
  pill.className = 'bg-pill running';
  pill.textContent = '⏳ 后台运行中：' + _bgTask.label + '（点此恢复进度窗口）';
  pill.onclick = restoreBgOverlay;
}
// 点角标：把进度窗口弹回前台（任务仍在跑，完成后照常收尾）
function restoreBgOverlay() {
  if (!_bgTask) { hideBgPill(); return; }
  $('#bgPill').hidden = true;
  $('#overlay').hidden = false;
  $('#btnCancelTask').hidden = false;
  const bgBtn = $('#btnBgRun');
  if (bgBtn) bgBtn.hidden = false;
  startProgressPolling();
  startMemeCarousel();
}
// 后台任务结束：角标转为完成/失败提示，8 秒后自动消失
function bgFinish() {
  const t = _bgTask;
  _bgTask = null;
  if (!t) return;
  const pill = $('#bgPill');
  pill.hidden = false;
  pill.className = 'bg-pill done' + (t.failed ? ' fail' : '');
  pill.textContent = t.failed
    ? '⚠ 后台任务已结束：' + t.label + '（详情见页面提示）'
    : '✅ ' + t.label + ' 已完成，结果已显示在页面';
  pill.onclick = () => { pill.hidden = true; };
  setTimeout(() => {
    if (!_bgTask && pill.className.indexOf('done') >= 0) pill.hidden = true;
  }, 8000);
}

// ========= 预估耗时：按当前范围（全A/沪/深/北）股票数 × 单股成本估算 =========
const RANGE_STOCKS = { all: 5550, SH: 1700, SZ: 2850, BJ: 270 };
const MODULE_PER_STOCK = { strategy: 0.02, cond: 0.015, pattern: 0.045, ant: 0.02, alert: 0.008 };
function estText(kind, exchange) {
  const n = RANGE_STOCKS[exchange || 'all'] || 5550;
  const secs = Math.max(3, Math.round(n * (MODULE_PER_STOCK[kind] || 0.02)));
  return secs >= 90 ? `预计约 ${Math.round(secs / 60)} 分钟` : `预计约 ${secs} 秒`;
}

// ========= 范围 =========
function bindRange() {
  $$('#rangePicker button').forEach(b => b.addEventListener('click', () => {
    $$('#rangePicker button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    state.range = b.dataset.range;
    $('#rangeBadge').textContent = b.textContent;
  }));
}
const rangeVal = () => state.range === 'all' ? null : state.range;

// ========= Tab（含界面回退历史栈） =========
const _panelHistory = [];   // 界面访问历史，供「↩ 退回」按钮使用

function switchTab(name, noPush) {
  const cur = $$('.tab').find(b => b.classList.contains('active'));
  if (cur && cur.dataset.tab && cur.dataset.tab !== name && !noPush) {
    _panelHistory.push(cur.dataset.tab);   // 记录来源界面
  }
  $$('.tab').forEach(b => b.classList.remove('active'));
  $$('.panel').forEach(p => p.classList.remove('active'));
  const btn = $('.tab[data-tab="' + name + '"]');
  if (btn) btn.classList.add('active');
  const panel = $('#tab-' + name);
  if (panel) panel.classList.add('active');
  if (name === 'bili' && typeof initBili === 'function') initBili();
  if (name === 'beton' && typeof initBeton === 'function') initBeton();
}

function bindTabs() {
  $$('.tab').forEach(btn => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));
}

// ========= 策略 =========
const STRATEGY_PARAMS = {
  turtle:  { '窗口(日)': 20, '回看(日)': 60 },
  ma_vol:  { '短期均线': 5, '长期均线': 20, '量比阈值': 1.5 },
  flag:    { '整理(日)': 15, '突破幅度(%)': 2, '振幅上限(%)': 12 },
  shake:   { '洗盘回撤上限(%)': 7 },
  limit_d: { '近期窗口(日)': 20, '最低涨幅(%)': 8 },
  rps:     { 'RPS阈值': 80, 'Top N': 100 },
};
async function initStrategy() {
  const list = await API.strategiesList();
  $('#strategyKey').innerHTML = '<option value="all">全部一起跑</option>' +
    list.map(s => `<option value="${s.key}">${s.label}</option>`).join('');
  $('#strategyKey').addEventListener('change', buildStrategyParams);
  buildStrategyParams();
}
function buildStrategyParams() {
  const key = $('#strategyKey').value;
  const params = key === 'all'
    ? Object.assign({}, ...Object.values(STRATEGY_PARAMS))
    : (STRATEGY_PARAMS[key] || {});
  $('#strategyParamsGrid').innerHTML = Object.entries(params).map(([k, v]) =>
    `<label>${k}<input type="number" data-param="${k}" value="${v}" step="0.5" /></label>`
  ).join('') || '<div style="color:var(--ink-faint);font-size:12px;">该策略无额外参数</div>';
}
async function runStrategies() {
  hideMeme('strategyMeme');
  const key = $('#strategyKey').value;
  const keys = key === 'all'
    ? Array.from($('#strategyKey').options).filter(o => o.value !== 'all').map(o => o.value)
    : [key];
  showOverlay('loading', `正在策略扫描，稍等片刻…（${estText('strategy', rangeVal())}）`, { cancellable: true });
  try {
    const data = await API.strategiesRun({ keys, exchange: rangeVal(), sectors: null });
    hideOverlay();
    // 后端返回 {rows: [...扁平...], by_strategy: {key: {label, rows}}}
    const flat = Array.isArray(data.rows) ? data.rows : [];
    if (data.by_strategy) {
      for (const v of Object.values(data.by_strategy)) {
        if (v.error) console.warn(v.error);
      }
    }
    if (flat.length === 0) {
      showMeme('strategyMeme', 'empty', '没有命中任何股票', '放宽参数或换个范围再试试');
      renderTable('strategyTable', []);
      return;
    }
    showMeme('strategyMeme', 'success', `命中 ${flat.length} 只 🎉`, '点击代码＝快速查看 · 点击名称＝详细查询');
    renderTable('strategyTable', flat, { max: 300 });
  } catch (e) {
    hideOverlay();
    if (_userCancelled) { showMeme('strategyMeme', 'farewell', '已取消这次扫描', '随时可以重新开始'); return; }
    showMeme('strategyMeme', 'error', '请求失败', String(e));
  }
}

// ========= 条件 =========
async function initConditions() {
  const list = await API.conditionsList();
  $('#condGrid').innerHTML = list.map(c => `
    <label class="cond-item">
      <input type="checkbox" value="${c.key}" />
      <div><div class="ci-name">${c.label}</div><div class="ci-desc">${c.desc || ''}</div></div>
    </label>`).join('');
  $$('#condGrid .cond-item').forEach(item => {
    const cb = item.querySelector('input');
    cb.addEventListener('change', () => item.classList.toggle('active', cb.checked));
    item.addEventListener('click', e => {
      if (e.target.tagName === 'INPUT') return;   // 点小框：原生行为，翻一次
      e.preventDefault();                          // 关键：阻断 label 默认转发
      cb.checked = !cb.checked; item.classList.toggle('active', cb.checked);   // 否则会与 label 转发二次抵消
    });
  });
  // 默认勾选前 4 个条件
  $$('#condGrid .cond-item').slice(0, 4).forEach(item => {
    const cb = item.querySelector('input');
    cb.checked = true; item.classList.add('active');
  });
}
async function runConditions() {
  hideMeme('condMeme');
  const keys = $$('#condGrid input:checked').map(c => c.value);
  if (keys.length === 0) { showMeme('condMeme', 'ask', '请先勾选条件', '至少选一个条件哦'); return; }
  showOverlay('loading', `正在按条件过滤…（${estText('cond', rangeVal())}）`, { cancellable: true });
  try {
    const cfg = {
      CHANNEL_DAYS: num('#pChannelDays', 60), CHANNEL_R2: num('#pChannelR2', 0.6),
      PULLBACK_DAYS: num('#pPullbackDays', 20), SUPPORT_MA: num('#pSupportMa', 20),
      PULLBACK_TOL: num('#pPullbackTol', 2), SMALL_YANG_DAYS: num('#pSmallYangDays', 5),
      SMALL_YANG_MAX: num('#pSmallYangMax', 3), W_BOTTOM_DAYS: num('#pWBottomDays', 60),
      W_BOTTOM_TOL: num('#pWBottomTol', 3), DIVERGENCE_DAYS: num('#pDivergenceDays', 60),
      RSI_DAYS: num('#pRsiDays', 14), VAL_YEARS: num('#pValYears', 10),
      PE_PERCENTILE: num('#pPePercentile', 10), PB_PERCENTILE: num('#pPbPercentile', 10),
      MIN_VAL_ROWS: num('#pMinValRows', 120), DIV_YIELD_MIN: num('#pDivYieldMin', 3),
      NEW_STOCK_DAYS: num('#pNewStockDays', 365), EXCLUDE_ST: $('#pExcludeSt').checked,
    };
    const data = await API.conditionsRun({ keys, exchange: rangeVal(), cfg });
    hideOverlay();
    const rows = data.rows || [];
    if (rows.length === 0) {
      showMeme('condMeme', 'empty', '没有交集', '条件太严了，放宽一点试试');
      renderTable('condTable', []);
      return;
    }
    showMeme('condMeme', 'success', `${keys.length} 条件命中 ${rows.length} 只 ✨`, '按 PE-TTM 升序展示');
    renderTable('condTable', rows, { max: 300 });
  } catch (e) {
    hideOverlay();
    if (_userCancelled) { showMeme('condMeme', 'farewell', '已取消这次筛选', '随时可以重新开始'); return; }
    showMeme('condMeme', 'error', '请求失败', String(e));
  }
}

// ========= 形态 =========
async function runPattern() {
  hideMeme('patternMeme');
  showOverlay('loading', `正在形态打分（较慢，稍候）…（${estText('pattern', rangeVal())}）`, { cancellable: true });
  try {
    const data = await API.patternRun({
      min_score: Number($('#patternMinScore').value),
      only_bottom: $('#patternOnlyBottom').checked && !$('#patternOnlyTop').checked,
      only_top: $('#patternOnlyTop').checked,
      exchange: rangeVal(),
    });
    hideOverlay();
    const results = data.results || [];
    if (results.length === 0) {
      showMeme('patternMeme', 'empty', '没有达到分数线的形态', '把分数降一点再试');
      renderTable('patternTable', []);
      return;
    }
    showMeme('patternMeme', 'success',
      `${results.length} 只形态命中 🪄`,
      Object.entries(data.skip_stats || {}).map(([k, v]) => `${k}:${v}`).join(' · '));
    renderTable('patternTable', results.map(r => ({
      '代码': r.code, '名称': r.name, '匹配度': r.raw_score, '综合分': r.score,
      '形态分': r.sim_score,
      '趋势分': r.struct_score, '评级': r.grade, '类型': r.type_label,
      '层': r.tier_short, '最像样本': r.nearest_sample, '收盘': r.close,
    })), { max: 300 });
  } catch (e) {
    hideOverlay();
    if (_userCancelled) { showMeme('patternMeme', 'farewell', '已取消这次打分', '随时可以重新开始'); return; }
    showMeme('patternMeme', 'error', '请求失败', String(e));
  }
}

// ========= 形态打分 · 短线快拍 =========
// 列名必须踩中 PCT_RE（涨跌幅|涨幅|幅度|涨跌|超额|收益|振幅|回撤|回报）才会红绿着色：
// 「均振幅%」「回撤%」「反弹幅度%」故意这样命名；「区间位置 / 量能分位 / 距年线%」不参与着色。
function snapCfg() {
  const modes = [];
  if ($('#snapModeA').checked) modes.push('A');
  if ($('#snapModeB').checked) modes.push('B');
  if ($('#snapModeC').checked) modes.push('C');
  if ($('#snapModeD').checked) modes.push('D');
  const pct = (id, dv) => { const v = num(id, dv); return v / 100; };
  return {
    modes,
    min_score: num('#snapMinScore', 55),
    require_above_ma250: $('#snapAboveMA250').checked,
    zz_thr: pct('#snapZzThr', 12),
    cycle_lookback: num('#snapCycleLookback', 180),
    min_amt20_yi: num('#snapMinAmt', 1),
    min_mktcap_yi: num('#snapMinMktcap', 30),
    // v2：波动率门槛默认 0（关闭）。后端默认值也改成 0 —— 原 30% 会误杀
    // 「长期低波动后即将爆发」的标的（实测 300475 @2025-08-01 因此被剔除，
    // 而它此后 60 日 +330.5%）。留 0 表示不启用硬门槛。
    min_vol20: pct('#snapMinVol', 0),
    min_listed_days: num('#snapMinListed', 250),
    min_price: num('#snapMinPrice', 3),
    max_price: num('#snapMaxPrice', 400),
  };
}

async function runSnap() {
  hideMeme('snapMeme');
  const cfg = snapCfg();
  if (!cfg.modes.length) {
    showMeme('snapMeme', 'ask', '请至少勾选一个子模式', '周期震荡 / 即将大涨 / 反弹动能 / 即将反弹');
    return;
  }
  showOverlay('loading', `正在全市场快拍（${cfg.modes.length} 个模式）…（${estText('pattern', rangeVal())}）`, { cancellable: true });
  try {
    const data = await API.snapRun({ cfg, exchange: rangeVal() });
    hideOverlay();
    const results = data.results || [];
    if (results.length === 0) {
      showMeme('snapMeme', 'empty', '这轮没拍到符合条件的票', '降低最低分、放宽年线限制或换个范围再试');
      renderTable('snapTable', []);
      return;
    }
    const nf = v => (v == null ? null : v);
    showMeme('snapMeme', 'success',
      `${results.length} 只快拍命中 ⚡`,
      Object.entries(data.skip_stats || {}).map(([k, v]) => `${k}:${v}`).join(' · ') || '按总分降序');
    renderTable('snapTable', results.map(r => ({
      '代码': r.code, '名称': r.name, '模式': r.modes, '总分': r.score,
      // v2：把「临界触发」单列出来 —— 需求 b/d 的核心是「1-2 天内」，
      // 状态达标但触发 0 的票（`0/5`）应当一眼可见。
      '临界触发': `${r.trigger_n || 0}/5`,
      '均振幅%': nf(r.amp_pct), '周期数': nf(r.n_cycles),
      '区间位置': r.pos120, '量能分位': r.vol_rank, '回撤%': r.dd250_pct,
      '反弹幅度%': nf(r.rebound_pct), '动能持续': nf(r.persist),
      '距年线%': nf(r.ma250_dev_pct),
      '触发要点': r.state, '收盘': r.close,
    })), { max: 300 });
  } catch (e) {
    hideOverlay();
    if (_userCancelled) { showMeme('snapMeme', 'farewell', '已取消这次快拍', '随时可以重新开始'); return; }
    showMeme('snapMeme', 'error', '请求失败', String(e));
  }
}

function bindPatternSubTabs() {
  const tabs = $$('#patternSubTabs .sub-tab');
  if (!tabs.length) return;
  tabs.forEach(btn => btn.addEventListener('click', () => {
    tabs.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const which = btn.dataset.sub;
    const classic = $('#patternSubClassic');
    const sn = $('#patternSubSnap');
    if (classic) classic.hidden = which !== 'classic';
    if (sn) sn.hidden = which !== 'snap';
  }));
}

// ========= 9Reverse9 · 神奇九转（日线 / 周线 / 月线，可叠加）=========
// 折叠态：排名 + 代码 + 名称 + 命中周期标签 + 状态 + 两个排序依据指标 + 展开按钮
//        （flex-wrap，绝不重叠）
// 展开态：按**命中周期**分组，每组左右两栏（买入九转 / 卖出九转）明细表；
//        每组的 n/m 单位各自标注（日 / 周 / 月），绝不混在一张表里
let _r9Last = null;
let _r9AllOpen = false;

// 周期多选：返回选中周期（按 日→周→月 顺序），全不选时兜底为 ["day"]
function r9Periods() {
  const picked = [];
  $$('#r9Periods .r9-period-cb').forEach(cb => { if (cb.checked) picked.push(cb.value); });
  const order = ['day', 'week', 'month'];
  return order.filter(p => picked.includes(p)).length
    ? order.filter(p => picked.includes(p)) : ['day'];
}

function r9Cfg() {
  return {
    periods: r9Periods(),
    near_remaining: num('#r9Near', 2),
    include_triggered: $('#r9Triggered').checked,
    ref_gap: num('#r9Gap', 4),
    match_window: num('#r9Win', 5),
    shrink_k: num('#r9ShrinkK', 3),
    confirm_pct: num('#r9ConfirmPct', 3),
    confirm_bars: num('#r9ConfirmBars', 10),
    hist_years: num('#r9HistYears', 5),
    min_listed_days: num('#r9MinListed', 120),
    min_amt20_yi: num('#r9MinAmt', 0.5),
    min_price: num('#r9MinPrice', 2),
    max_price: num('#r9MaxPrice', 2000),
    max_results: num('#r9MaxResults', 600),
    exclude_st: $('#r9ExcludeST').checked,
  };
}

// 周期 key → 展示元信息（单位、配色类）；与后端 PERIODS 保持一致
const R9_PDEF = {
  day:   { label: '日线', unit: '天', cls: 'r9-p-day' },
  week:  { label: '周线', unit: '周', cls: 'r9-p-week' },
  month: { label: '月线', unit: '月', cls: 'r9-p-month' },
};

// 明细表：n/m 用色阶（≤1 最强 / ≤3 中 / 其余弱；负数＝拐点在信号另一侧，单独配色）
// 列名不进 PCT_RE，故不会被全站红绿规则误染
function r9KClass(k) {
  if (k < 0) return 'r9-k-lag';
  return k <= 1 ? 'r9-k-best' : (k <= 3 ? 'r9-k-mid' : 'r9-k-weak');
}

// 单个周期的明细表（两张：买入 / 卖出）。unit 决定列头与文案单位（天/周/月）
function r9DetailTable(recs, side, unit) {
  const isBuy = side === 'buy';
  const u = unit || '天';
  if (!recs || !recs.length) {
    return `<div class="r9-empty">历史上暂无「已确认」的${isBuy ? '买入' : '卖出'}九转样本</div>`;
  }
  const head = isBuy
    ? '<tr><th>信号日</th>'
      + `<th title="真正开始反弹的${isBuy ? 'K线' : ''}（对称窗口内最低价所在日）">真正反弹日</th>`
      + `<th title="n = 真正反弹底 − 信号日，单位：${u}。负数表示底在信号之前，即信号滞后">n(${u})</th>`
      + '<th>信号收盘</th><th>阶段低点</th><th>记录</th></tr>'
    : '<tr><th>信号日</th>'
      + '<th title="真正开始下行的K线（对称窗口内最高价所在日）">真正顶部日</th>'
      + `<th title="m = 信号日 − 真正顶部日，单位：${u}。负数表示顶在信号之后，即信号提前预警">m(${u})</th>`
      + '<th>信号收盘</th><th>阶段高点</th><th>记录</th></tr>';
  const body = recs.map(x => {
    const k = isBuy ? x.n : x.m;
    return `<tr>
      <td class="r9-mono">${x.date}</td>
      <td class="r9-mono">${x.rev_date}</td>
      <td class="num ${r9KClass(k)}">${k}</td>
      <td class="num">${x.price}</td>
      <td class="num">${x.rev_price}</td>
      <td class="r9-text">${escHtml(x.text || '')}</td>
    </tr>`;
  }).join('');
  return `<div class="r9-table-wrap"><table class="result-table small">
    <thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}

// 一个周期的完整明细块（标题 + 该周期的买入/卖出两栏）
function r9PeriodBlock(row) {
  const p = row.period;
  const pd = R9_PDEF[p] || { label: p, unit: '天', cls: 'r9-p-day' };
  const u = row.unit || pd.unit;
  const bh = row.buy_history || [];
  const sh = row.sell_history || [];
  const lag = row.n_lag || 0;
  const topAfter = row.m_neg || 0;
  const buySub = `共 ${row.buy_n || 0} 条，下表为最近 ${bh.length} 条`
    + (lag ? `；其中 ${lag} 条真底在信号之前（信号滞后，不计入平均 n）` : '');
  const sellSub = `共 ${row.sell_n || 0} 条，下表为最近 ${sh.length} 条`
    + (topAfter ? `；其中 ${topAfter} 条真顶在信号之后（该信号属提前预警）` : '');
  const stat = `<span class="r9-pstat">`
    + `当前 <b>${row.cur_count || 0}/9</b>`
    + (row.triggered ? ` · <b>已达成</b>` : ` · 还差 <b>${row.remain || 0}</b>${u}`)
    + ` · 历史K线 <b>${row.bars_hist || 0}</b> 根${pd.label === '日线' ? '' : `（${pd.label}）`}`
    + ` · 平均n <b>${row.n_mean == null ? '—' : row.n_mean}</b>`
    + ` · 排序分 <b>${row.n_rank == null ? '—' : row.n_rank}</b>`
    + ` · n样本 <b>${row.n_pos || 0}/${row.buy_n || 0}</b>`
    + ` · 卖出9转/1年 <b>${row.sell_1y || 0}</b>`
    + (row.bar_date ? ` · 末根 <b>${row.bar_date}</b>` : '')
    + `</span>`;
  const note = row.note
    ? `<div class="r9-note">⚠ ${pd.label}：${escHtml(row.note)}</div>` : '';
  return `<div class="r9-pgroup">
    <h4 class="r9-phead ${pd.cls}"><span class="r9-ptag">${pd.label}</span>
      <small>n / m 单位 = <b>${u}</b>${pd.label === '日线' ? '（交易日）' : ''}</small>
      ${stat}
    </h4>
    <div class="r9-cols">
      <div class="r9-col r9-col-buy">
        <h5>买入九转 · n = 真正反弹底 − 信号日 <small>${buySub}</small></h5>
        ${r9DetailTable(bh, 'buy', u)}
      </div>
      <div class="r9-col r9-col-sell">
        <h5>卖出九转 · m = 信号日 − 真正顶部日 <small>${sellSub}</small></h5>
        ${r9DetailTable(sh, 'sell', u)}
      </div>
    </div>
  </div>${note}`;
}

// 展开区整体：按命中周期**分组**展示（叠加时一天里可能日线命中、月线也命中）
// 只在用户点开时生成 —— 见 r9FillDetail。
function r9DetailHtml(r) {
  const rows = (r.period_rows && r.period_rows.length)
    ? r.period_rows
    : [{ period: r.period || 'day', label: r.period_label, unit: r.unit,
         buy_history: r.buy_history, sell_history: r.sell_history,
         buy_n: r.buy_n, sell_n: r.sell_n, n_lag: r.n_lag, m_neg: r.m_neg,
         cur_count: r.cur_count, remain: r.remain, triggered: r.triggered,
         bars_hist: r.bars_hist, n_mean: r.n_mean, n_rank: r.n_rank,
         n_pos: r.n_pos, sell_1y: r.sell_1y, bar_date: r.bar_date, note: r.note }];
  const multi = rows.length > 1;
  const tip = multi
    ? `<div class="r9-multitip">命中 <b>${rows.length}</b> 个周期 —— 各周期<strong>独立成立</strong>，下表按周期分组；
       不同周期的 n / m <strong>单位不同</strong>（日 / 周 / 月），不合并计算。</div>`
    : '';
  return tip + rows.map(r9PeriodBlock).join('');
}

// 懒渲染：折叠态只放一个空壳，点开/批量展开时才生成明细 DOM。
// 不做懒加载的话，几百只股票 × 每只 80 条明细 ≈ 20 万个节点，首屏必卡。
function r9FillDetail(card) {
  const det = card.querySelector('.r9-detail');
  if (!det || det.children.length) return;      // 已渲染
  const idx = Number(det.dataset.idx);
  const r = (_r9Last && (_r9Last.results || [])[idx]) || null;
  det.innerHTML = r ? r9DetailHtml(r) : '<div class="r9-empty">数据已失效，请重新扫描</div>';
}

function r9Card(r, idx) {
  const na = v => (v == null ? '—' : v);
  // 命中周期标签（叠加时逐周期显示，一眼看出是哪个级别在动）
  const plabels = (r.periods || ['day']).map(p => {
    const pd = R9_PDEF[p] || { label: p, cls: 'r9-p-day' };
    const multi = (r.periods || []).length > 1;
    const isBest = multi && r.best_period === p;
    return `<span class="r9-ptag ${pd.cls}${isBest ? ' r9-ptag-best' : ''}"`
      + `${isBest ? ' title="各命中周期中排序分最优（决定名次）"' : ''}>${pd.label}`
      + `${isBest ? ' ★' : ''}</span>`;
  }).join('');
  const status = `<span class="r9-status">${escHtml(r.status || '')}</span>`;
  return `<div class="r9-card">
    <div class="r9-head">
      <span class="r9-rank" title="排序名次">${r.rank}</span>
      <a href="#" class="stock-link code-link r9-code" data-code="${r.code}" title="快速查看 · 站内看K线">${r.code}</a>
      <a href="${thsLink(r.code)}" target="_blank" rel="noopener noreferrer" class="stock-link name-link r9-name" title="详细查询 · 同花顺新窗口">${escHtml(r.name || '')}</a>
      <span class="r9-pbadges">${plabels}</span>
      ${status}
      <span class="r9-metrics">
        <span class="r9-metric" title="历史买入九转的平均 n（只统计 n≥0 的样本；单位随命中周期）"><i>平均n</i><b>${na(r.n_mean)}</b></span>
        <span class="r9-metric" title="第 1 排序依据：排序分 = (Σn + K×该周期全市场均值)/(样本数 + K)。叠加时取各命中周期中的最小值；样本少时会向该周期全市场均值收缩"><i>排序分</i><b>${na(r.n_rank)}</b></span>
        <span class="r9-metric" title="参与排序的 n≥0 样本数 / 全部买入九转条数（取自排序分最优的周期）"><i>n样本</i><b>${r.n_pos || 0}/${r.buy_n || 0}</b></span>
        <span class="r9-metric" title="最近一年内的卖出九转条数（第 2 排序依据，各命中周期之和）"><i>卖出9转/1年</i><b>${r.sell_1y || 0}</b></span>
        <span class="r9-metric" title="参与统计的历史K线根数（取自排序分最优的周期）"><i>K线数</i><b>${r.bars_hist || 0}</b></span>
      </span>
      <button class="btn btn-ghost r9-expand" type="button">展开 ▾</button>
    </div>
    <div class="r9-detail" hidden data-idx="${idx}"></div>
  </div>`;
}

const R9_RENDER_MAX = 400;      // 折叠行上限：再多就只渲染前 N 只（CSV 仍导出全量）

function renderR9(data) {
  const list = $('#r9List');
  const sum = $('#r9Summary');
  if (!list) return;
  const rows = (data && data.results) || [];
  if (!rows.length) {
    if (sum) sum.hidden = true;
    list.innerHTML = '<div class="r9-empty-big">— 暂无结果 —</div>';
    return;
  }
  const p = (data && data.params) || {};
  const sk = (data && data.skip_stats) || {};
  const s = (data && data.stats) || {};
  const per = (data && data.period_stats) || {};
  const plist = (data && data.periods) || ['day'];
  // 逐周期统计（每个周期的样本量级完全不同，必须分开报；不能加总）
  const perTxt = plist.map(pk => {
    const ps = per[pk] || {};
    const pd = R9_PDEF[pk] || { label: pk };
    const lag = (ps.buy_lag_pct == null) ? ''
      : `，真底早于信号 <b>${ps.buy_lag_pct}%</b>`;
    const top = (ps.sell_top_after_pct == null) ? ''
      : `，真顶晚于信号 <b>${ps.sell_top_after_pct}%</b>`;
    return `<b class="r9-sum-p">${pd.label}</b>：候选 <b>${ps.analyzable || 0}</b>/${ps.candidates || 0} 只可统计`
      + ` · 买入样本 <b>${ps.buy_total || 0}</b>${lag} · 卖出样本 <b>${ps.sell_total || 0}</b>${top}`
      + ` · 均n基准 <b>${ps.n_prior == null ? '—' : ps.n_prior}</b>`
      + ((ps.thin != null) ? ` · 单样本 <b>${ps.thin}</b>` : '');
  }).join('<br>');
  // 跳过统计里的 [日线]/[周线]/[月线] 前缀去掉，按周期归并展示，避免刷屏
  const skTxt = Object.keys(sk).length
    ? ' · 跳过（按周期同名合并）：' + Object.entries(sk)
      .map(([k, v]) => `${k.replace(/^\[[^\]]+\]/, '')}:${v}`)
      .filter((v, i, a) => a.indexOf(v) === i).join(' / ') : '';
  const rankTxt = (s.shrink_k == null) ? ''
    : ` · 排序分 = (Σn + ${s.shrink_k}×该周期全市场均值) / (样本数 + ${s.shrink_k})`
      + `，各周期独立计算，叠加时取名次最优者`;
  if (sum) {
    const pTxt = plist.map(pk => (R9_PDEF[pk] || { label: pk }).label).join(' + ');
    sum.hidden = false;
    sum.innerHTML = `共 <b>${rows.length}</b> 只 · 数据截止 <b>${data.ref_date || '—'}</b>`
      + ` · 周期 <b>${pTxt}</b>`
      + ` · 「即将」还差 ≤ <b>${p.near_remaining}</b> 个周期`
      + ` · |n|,|m| ≤ <b>${p.match_window}</b> · 历史回看 <b>${p.hist_years}</b> 年`
      + ` · 用时 <b>${data.elapsed || 0}s</b>`
      + `<span class="r9-sum-note"><span class="r9-sum-per">${perTxt}</span>${rankTxt}${skTxt}</span>`;
  }
  const shown = rows.slice(0, R9_RENDER_MAX);
  _r9Last = data;        // 明细懒渲染时按 data-idx 回查，必须先落好引用
  list.innerHTML = shown.map((r, i) => r9Card(r, i)).join('')
    + (rows.length > shown.length
      ? `<div class="r9-more">仅渲染前 ${shown.length} / 共 ${rows.length} 只（下方展开按钮不影响导出）；`
        + '需要全量请点「⬇ 下载 CSV」</div>' : '');
  // 代码 → 站内 K 线（与全站一致）
  list.querySelectorAll('.code-link').forEach(a => a.addEventListener('click', e => {
    e.preventDefault();
    switchTab('kline');
    $('#klineCode').value = a.dataset.code;
    loadKline();
  }));
  // 展开 / 收起（各自独立，首次展开才生成明细）
  list.querySelectorAll('.r9-expand').forEach(b => b.addEventListener('click', () => {
    const card = b.closest('.r9-card');
    const det = card.querySelector('.r9-detail');
    const willOpen = det.hidden;
    if (willOpen) r9FillDetail(card);
    det.hidden = !willOpen;
    b.textContent = willOpen ? '收起 ▴' : '展开 ▾';
  }));
}

function r9ToggleAll() {
  const cards = $$('#r9List .r9-card');
  if (!cards.length) return;
  _r9AllOpen = !_r9AllOpen;
  if (_r9AllOpen) cards.forEach(r9FillDetail);   // 先补渲染再展开
  cards.forEach(c => {
    c.querySelector('.r9-detail').hidden = !_r9AllOpen;
    const b = c.querySelector('.r9-expand');
    if (b) b.textContent = _r9AllOpen ? '收起 ▴' : '展开 ▾';
  });
  const btn = $('#btnR9ExpandAll');
  if (btn) btn.textContent = _r9AllOpen ? '⇕ 全部收起' : '⇕ 全部展开';
}

async function runR9() {
  hideMeme('r9Meme');
  const cfg = r9Cfg();
  const pTxt = cfg.periods.map(p => (R9_PDEF[p] || { label: p }).label).join(' + ');
  const multi = cfg.periods.length > 1;
  showOverlay('loading',
    `9Reverse9：程序一按 ${pTxt} ${multi ? '分别' : ''}筛选 → 程序二历史反转统计…`,
    { cancellable: true });
  try {
    const data = await API.r9Run({ cfg, exchange: rangeVal() });
    hideOverlay();
    if (data && data.error) {
      showMeme('r9Meme', 'error', '扫描失败', String(data.error));
      renderR9({ results: [] });
      return;
    }
    _r9Last = data;
    const rows = (data && data.results) || [];
    if (!rows.length) {
      showMeme('r9Meme', 'empty', `这轮没有「即将买入九转」的股票（${pTxt}）`,
        '把「还差周期数」放宽、勾选「含刚达成第 9 个周期」，或加选/换一个周期再试');
      renderR9(data);
      return;
    }
    const top = rows.slice(0, 3).map(r => r.name || r.code).join('、');
    // 命中周期分布：多周期叠加时，一眼看出各周期各贡献了多少只
    const hit = {};
    rows.forEach(r => (r.periods || []).forEach(p => { hit[p] = (hit[p] || 0) + 1; }));
    const hitTxt = Object.keys(hit).length
      ? ' · 命中：' + Object.keys(R9_PDEF).filter(p => hit[p])
        .map(p => `${R9_PDEF[p].label} ${hit[p]}`).join(' / ') : '';
    showMeme('r9Meme', 'success', `${rows.length} 只即将 / 已达成买入九转 🔁（${pTxt}）`,
      `最优：${top} · 数据截止 ${data.ref_date || '—'}${hitTxt}`);
    renderR9(data);
  } catch (e) {
    hideOverlay();
    if (_userCancelled) { showMeme('r9Meme', 'farewell', '已取消这次扫描', '随时可以重新开始'); return; }
    showMeme('r9Meme', 'error', '请求失败', String(e));
  }
}

// CSV：先总表，再附「历史明细」段落（买入 / 卖出分开列）
// 单元格统一走 csvCell 转义 —— 名称/板块/文案里出现逗号或引号时不会串列
function csvCell(v) {
  if (v == null) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function downloadR9Csv() {
  const d = _r9Last;
  if (!d || !((d.results || []).length)) {
    showMeme('r9Meme', 'ask', '还没有可导出的结果', '先点「开始扫描」');
    return;
  }
  const st = d.stats || {};
  const per = d.period_stats || {};
  const plist = d.periods || ['day'];
  const pTxt = plist.map(pk => (R9_PDEF[pk] || { label: pk }).label).join('+');
  const head = ['日期', '排名', '代码', '名称', '板块', '命中周期', '最优周期',
    '当前买入计数', '还差周期数', '已达成', '单位',
    '历史买入9转数', '平均n(仅n≥0)', '排序分', 'n≥0样本数',
    '买入样本中真底早于信号的条数',
    '近一年卖出9转数', '历史卖出9转数', '卖出样本中真顶晚于信号的条数',
    'K线数', '末根日期', '收盘'];
  const lines = d.results.map(r => {
    const lbl = (r.period_labels || []).join('/');
    const best = r.best_period ? ((R9_PDEF[r.best_period] || {}).label || r.best_period) : '';
    return [d.ref_date || '', r.rank, r.code, r.name, r.sector, lbl, best, r.cur_count,
      r.triggered ? 0 : r.remain, r.triggered ? '是' : '', r.unit || '',
      r.buy_n || 0, r.n_mean == null ? '' : r.n_mean,
      r.n_rank == null ? '' : r.n_rank, r.n_pos || 0,
      r.n_lag || 0, r.sell_1y || 0, r.sell_n || 0, r.m_neg || 0,
      r.bars_hist || 0, r.bar_date || '', r.close].map(csvCell).join(',');
  });
  // 口径段落：逐周期分开列（各周期量纲不同，绝不把 n 平均值跨周期合并）
  const meta = [
    ['口径', '数据截止', d.ref_date || '', '周期', pTxt,
      '窗口(|n|,|m|)', '|n|,|m| ≤ ' + ((d.params || {}).match_window || '')],
    ['口径', '叠加规则', '各周期先分别筛选、再整体并集；参数不跨周期复用（避免用「还差 2 日」限制月线）'],
    ['口径', '排序分公式', '(Σn + K×该周期全市场均值) / (样本数 + K)，K='
      + (st.shrink_k == null ? '' : st.shrink_k) + '，各周期独立计算，叠加时取名次最优者'],
  ];
  plist.forEach(pk => {
    const ps = per[pk] || {};
    const pd = R9_PDEF[pk] || { label: pk, unit: '天' };
    meta.push([`周期:${pd.label}`, '候选(可统计/命中)',
      (ps.analyzable || 0) + '/' + (ps.candidates || 0),
      '单位', pd.unit,
      '买入样本总数', ps.buy_total == null ? '' : ps.buy_total,
      '其中真底早于信号', ps.buy_lag_pct == null ? '' : ps.buy_lag_pct + '%']);
    meta.push([`周期:${pd.label}`, '卖出样本总数', ps.sell_total == null ? '' : ps.sell_total,
      '其中真顶晚于信号', ps.sell_top_after_pct == null ? '' : ps.sell_top_after_pct + '%',
      'n均值基准', ps.n_prior == null ? '' : ps.n_prior,
      '单样本只数', ps.thin == null ? '' : ps.thin]);
  });
  const metaLines = meta.map(r => r.map(csvCell).join(','));
  // 明细段落：带「周期」列，单位随周期标注（日/周/月），绝不混算
  const detail = ['', '—— 历史九转明细（按周期分组，单位随周期） ——',
    '代码,名称,周期,单位,类型,信号日,真正反转日,n或m,记录'];
  d.results.forEach(r => {
    const rows = (r.period_rows && r.period_rows.length)
      ? r.period_rows
      : [{ period: r.period || 'day', unit: r.unit,
           buy_history: r.buy_history, sell_history: r.sell_history }];
    rows.forEach(pr => {
      const pd = R9_PDEF[pr.period] || { label: pr.period, unit: pr.unit || '天' };
      const u = pr.unit || pd.unit;
      (pr.buy_history || []).slice().reverse().forEach(x =>
        detail.push([r.code, r.name, pd.label, u, '买入九转', x.date, x.rev_date, x.n, x.text]
          .map(csvCell).join(',')));
      (pr.sell_history || []).slice().reverse().forEach(x =>
        detail.push([r.code, r.name, pd.label, u, '卖出九转', x.date, x.rev_date, x.m, x.text]
          .map(csvCell).join(',')));
    });
  });
  const csv = '\ufeff' + [head.join(','), ...lines, ...metaLines, ...detail].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '9Reverse9_' + (plist.join('-')) + '_'
    + new Date().toISOString().slice(0, 10) + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ========= Bet on · 长期布局（年度级趋势） =========
// 折叠态：排名 + 代码 + 名称 + 买点档徽章 + 状态徽章 + 评分 + 周期时长 + 目标价 + 「详细 ▾」
// 展开态（懒渲染）：得分构成 / 入选理由 / 历史周期档案 / 行业视角 / 上游期货联动 /
//                基本面快照 / 卖出纪律 / 提醒 / 长期走势图
let _boLast = null;
let _boAllOpen = false;
let _boMeta = null;
let _boLastWatch = [];

const BO_COMP_ORDER = ['q_state', 'q_left', 'q_trend', 'q_mom', 'q_vol', 'q_hist', 'q_room'];
const BO_COMP_NAME = {
  q_state: '趋势状态分', q_left: '位置分', q_trend: '趋势质量分', q_mom: '动量健康分',
  q_vol: '量能配合分', q_hist: '历史体质分', q_room: '空间与风控分',
};

function boCfg() {
  const states = [];
  [['#boStAbove', 'ABOVE'], ['#boStExtend', 'EXTEND'], ['#boStTurning', 'TURNING'],
   ['#boStBelow', 'BELOW'], ['#boStMixed', 'MIXED']].forEach(([sel, key]) => {
    const el = $(sel);
    if (el && el.checked) states.push(key);
  });
  return {
    min_score: num('#boMinScore', 65),
    tier: $('#boTier') ? $('#boTier').value : '',
    states: states.length ? states : ['ABOVE', 'EXTEND', 'TURNING', 'BELOW'],
    max_results: num('#boMaxResults', 300),
    min_dur_days: num('#boMinDur', 0),
    with_futures: $('#boWithFutures') ? $('#boWithFutures').checked : true,
    hist_years: num('#boHistYears', 12),
    long_gain: num('#boLongGain', 80) / 100,
    long_days: num('#boLongDays', 250),
    dd_deep: num('#boDdDeep', 45) / 100,
    down_confirm: num('#boDownConfirm', 10),
    cycle_min_gain: num('#boCycleMinGain', 30) / 100,
    min_amt20_yi: num('#boMinAmt', 1),
    min_mktcap_yi: num('#boMinMktcap', 30),
    min_listed_days: num('#boMinListed', 400),
    min_price: num('#boMinPrice', 3),
    max_price: num('#boMaxPrice', 3000),
    exclude_st: $('#boExcludeST') ? $('#boExcludeST').checked : true,
  };
}

const boNum = (v, d = 2) => (v == null || !Number.isFinite(Number(v)))
  ? '—' : Number(v).toFixed(d);
const boPct = (v, d = 1) => (v == null || !Number.isFinite(Number(v)))
  ? '—' : `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(d)}%`;
const boTierCls = t => ({ T1: 'bo-t1', T2: 'bo-t2', T3: 'bo-t3', T4: 'bo-t4' }[t] || 'bo-t4');
const BO_TIER_NAME = { T1: '启动前埋伏', T2: '山腰确认', T3: '加速/已走远', T4: '观察' };
const boTierName = t => BO_TIER_NAME[t] || '';
const boStateCls = s => ({ ABOVE: 'bo-s-above', EXTEND: 'bo-s-extend', TURNING: 'bo-s-turning',
                           BELOW: 'bo-s-below', MIXED: 'bo-s-mixed' }[s] || 'bo-s-mixed');
// 观察榜渲染上限（宽松轨条数多，限制一次生成的 DOM 量）
const BO_WATCH_RENDER_MAX = 200;

// ---- 展开区：得分构成 ----
function boCompHtml(r) {
  const meta = (_boMeta && _boMeta.weights) || [];
  const wmap = {};
  meta.forEach(w => { wmap[w.key] = w; });
  const rows = BO_COMP_ORDER.map(k => {
    const q = (r.components || {})[k];
    const w = (wmap[k] && wmap[k].w) || 0;
    const val = (q == null) ? 0 : q;
    const contrib = val * w * 100;
    const pct = Math.max(0, Math.min(100, val * 100));
    const title = (wmap[k] && wmap[k].note) || '';
    return `<div class="bo-comp" title="${escHtml(title)}">
      <span class="bo-comp-name">${BO_COMP_NAME[k] || k}<i>权重 ${(w * 100).toFixed(0)}%</i></span>
      <span class="bo-comp-bar"><i style="width:${pct.toFixed(1)}%"></i></span>
      <span class="bo-comp-val">${val.toFixed(2)}<i>贡献 ${contrib.toFixed(1)}</i></span>
    </div>`;
  }).join('');
  let pen = '';
  if ((r.penalties || []).length) {
    pen = `<div class="bo-pen"><b>折扣（只影响排序与展示，不参与判档）：</b>`
      + r.penalties.map(p => `<div class="bo-pen-row">×${p.factor} — ${p.why}</div>`).join('')
      + `<div class="bo-pen-row">合计系数 ×${r.penalty}：原始分 ${r.raw_score} → 最终分 `
      + `<b>${r.score}</b></div></div>`;
  } else {
    pen = `<div class="bo-pen bo-pen-none">无折扣（未触发过热 / 假启动 / 波动过热 / 短历史）·
      原始分 ${r.raw_score} → 最终分 <b>${r.score}</b></div>`;
  }
  return `<section class="bo-sec"><h4>① 得分构成（0~100）</h4>${rows}${pen}</section>`;
}

// ---- 展开区：入选理由 ----
function boReasonHtml(r) {
  const li = (r.reasons || []).map(t => `<li>${t}</li>`).join('');
  return `<section class="bo-sec"><h4>② 为什么选它 / 为什么分高</h4>
    <ul class="bo-reasons">${li}</ul></section>`;
}

// ---- 展开区：历史周期档案 ----
function boCycleHtml(r) {
  const st = r.cycle_stats || {};
  const cv = r.cur_cycle;
  const rows = (r.cycles || []).map(x => `<tr>
      <td class="bo-mono">${x.start}</td><td class="bo-mono">${x.peak}</td>
      <td class="bo-mono">${x.end}</td>
      <td class="num ${x.gain_pct >= 0 ? 'up' : 'down'}">${x.gain_pct >= 0 ? '+' : ''}${x.gain_pct}%</td>
      <td class="num">${x.days}</td>
      <td class="num down">${x.max_dd_pct}%</td>
      <td>${x.long ? '<b class="bo-tag-long">长周期</b>' : ''}</td></tr>`).join('');
  const head = `<div class="bo-stat-line">
      <span>历史上涨段 <b>${st.n || 0}</b> 段</span>
      <span>其中长周期（≥80% 且 ≥250 日）<b>${st.n_long || 0}</b> 段</span>
      ${st.gain_med != null ? `<span>中位涨幅 <b>${(st.gain_med * 100).toFixed(0)}%</b></span>` : ''}
      ${st.days_med != null ? `<span>中位时长 <b>${Math.round(st.days_med)}</b> 日</span>` : ''}
      ${st.dd_med != null ? `<span>中位最大回撤 <b>${(st.dd_med * 100).toFixed(0)}%</b></span>` : ''}
    </div>`;
  const curTxt = cv ? `<div class="bo-cur">当前正处在本轮上涨段：起点 <b>${cv.start}</b>
      （起点价 ${cv.start_c} 元）· 已走 <b>${cv.elapsed}</b> 个交易日 ·
      最高涨幅 <b class="${cv.gain_pct >= 0 ? 'up' : 'down'}">${cv.gain_pct >= 0 ? '+' : ''}${cv.gain_pct}%</b> ·
      段内最大回撤 <b class="down">${cv.max_dd_pct}%</b></div>`
    : `<div class="bo-cur bo-cur-none">当前<b>不在</b>可确认的上涨段内（MA60 未站稳 MA120 上方）。</div>`;
  const warn = `<div class="bo-warn-inline">⚠ 上面的周期是 v4 趋势状态机在<strong>全历史</strong>上切出来的，
    切段必须用到「这段行情后来如何收场」的信息 —— 它是<strong>标签</strong>，不是信号，
    只用来统计这只股票的历史体质与节奏，<strong>不参与买入评分</strong>。</div>`;
  return `<section class="bo-sec"><h4>③ 历史周期档案（该股自己的上涨段）</h4>
    ${head}${curTxt}
    <div class="bo-table-wrap"><table class="result-table small">
      <thead><tr><th>起点</th><th>段内最高点</th><th>段结束</th><th>涨幅</th>
        <th title="起点 → 段内最高点的交易日数">到顶天数</th><th>段内最大回撤</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7">暂无合格上涨段</td></tr>'}</tbody>
    </table></div>${warn}</section>`;
}

// ---- 展开区：行业视角 ----
function boSectorHtml(r) {
  const n = r.sector_n, r120 = r.sector_ret120, r250 = r.sector_ret250;
  return `<section class="bo-sec"><h4>④ 所在行业</h4>
    <div class="bo-kv">
      <span>所属板块</span><b>${escHtml(r.sector || '—')}</b>
      <span>同行业入选</span><b>${n == null ? '—' : n + ' 只'}</b>
      <span>行业中期动量（候选池中位 ret120）</span>
      <b class="${(r120 || 0) >= 0 ? 'up' : 'down'}">${boPct(r120)}</b>
      <span>行业长期动量（候选池中位 ret250）</span>
      <b class="${(r250 || 0) >= 0 ? 'up' : 'down'}">${boPct(r250)}</b>
    </div>
    <div class="bo-note">口径说明：行业动量取自<strong>本次扫描中该行业所有通过硬门槛的股票</strong>
      （不是全行业），用来判断这只股票是「行业贝塔带动」还是「个股独立走强」。
      若该股评分明显高于行业中位而行业动量为负，更可能是个股逻辑驱动。</div>
  </section>`;
}

// ---- 展开区：上游期货联动 ----
function boFuturesHtml(r, futMeta) {
  const rows = r.futures || [];
  if (!rows.length) {
    const noMap = ((futMeta && futMeta.no_mapping) || []).indexOf(r.sector) >= 0;
    return `<section class="bo-sec"><h4>⑤ 上游商品期货联动</h4>
      <div class="bo-note">${noMap
        ? `「${escHtml(r.sector || '该行业')}」<strong>没有直接对应的商品期货品种</strong>
           （它的成本/售价不锚定任何单一商品），因此无法给出产业因果提示。
           若要观察整体风险偏好，可参考股指期货（IF/IC/IM），但那属于市场层面、不是行业因果。`
        : `本次未取到该行业的期货数据（可能网络不通，或在参数里关掉了「拉取上游商品期货」）。`}</div>
    </section>`;
  }
  const body = rows.map(x => {
    const vc = x.verdict === '顺风' ? 'up' : (x.verdict === '逆风' ? 'down' : '');
    return `<div class="bo-fut-row">
      <span class="bo-fut-name">${escHtml(x.name)}<i>${escHtml(x.symbol)}</i></span>
      <span class="bo-fut-rel">${escHtml(x.relation_text)}</span>
      <span class="bo-fut-chg">60日 <b class="${(x.ret60 || 0) >= 0 ? 'up' : 'down'}">${boPct(x.ret60)}</b>
        · 120日 <b class="${(x.ret120 || 0) >= 0 ? 'up' : 'down'}">${boPct(x.ret120)}</b></span>
      <span class="bo-fut-verdict ${vc}">${escHtml(x.verdict)}</span>
      <span class="bo-fut-why">${escHtml(x.why)}</span>
    </div>`;
  }).join('');
  return `<section class="bo-sec"><h4>⑤ 上游商品期货联动（${escHtml(r.sector || '')}）</h4>
    ${body}
    <div class="bo-note">方向判读：「同向利好」＝该品种价格上行通常利好本公司；
      「成本反向」＝该品种上行通常压缩本公司毛利。走势为期货主连合约近 60 / 120 个交易日的涨跌幅
      （数据截至 ${escHtml((rows[0] || {}).date || '—')}）。
      <strong>相关性不等于因果</strong>，且期货价格本身也受宏观与资金影响，此处只作交叉验证的一条线索。</div>
  </section>`;
}

// ---- 展开区：基本面快照 ----
function boFundHtml(r) {
  const v = r.valuation || {};
  const divY = v.div_yield == null ? null : v.div_yield * 100;
  return `<section class="bo-sec"><h4>⑥ 基本面与估值快照</h4>
    <div class="bo-kv">
      <span>PE(TTM)</span><b>${boNum(v.pe)}</b>
      <span>PE 自身历史分位</span><b>${v.pe_pct == null ? '—' : (v.pe_pct * 100).toFixed(0) + '%'}</b>
      <span>PB</span><b>${boNum(v.pb)}</b>
      <span>PB 自身历史分位</span><b>${v.pb_pct == null ? '—' : (v.pb_pct * 100).toFixed(0) + '%'}</b>
      <span>股息率</span><b>${divY == null ? '—' : divY.toFixed(2) + '%'}</b>
      <span>最近除息日 / 每10股派现</span>
      <b>${escHtml(v.last_ex_date || '—')}${v.cash_per_10 == null ? '' : ' / ' + v.cash_per_10 + ' 元'}</b>
      <span>流通市值</span><b>—</b>
    </div>
    <div class="bo-warn-inline">⚠ <strong>本地库只有估值与分红，没有营收/利润/ROE 等财务报表数据</strong>，
      因此「基本面」在这里只能到 PE / PB / 股息这一层。
      行业前景也只用「行业动量 + 上游商品期货」两个侧面近似。
      真正的公司质地（毛利率趋势、在手订单、产能、股东结构、商誉、质押）<strong>必须自行核实</strong>，
      本模块不提供也不臆测。估值分位为 0% 表示处于自身历史最便宜的一端（数据日期
      ${escHtml(v.snap_date || '—')}）。</div>
  </section>`;
}

// ---- 展开区：卖出纪律 ----
function boSellHtml(r) {
  const s = r.sell || {};
  const t = s.target || null;
  let tgt = '';
  if (t && !t.no_target) {
    tgt = `<div class="bo-target">
      <div class="bo-target-head">目标价区间（<b>${escHtml(t.basis.split('。')[0])}</b>）</div>
      <div class="bo-target-grid">
        <span>保守 P25<b>${boNum(t.conservative)}</b></span>
        <span>中性 中位<b class="bo-tgt-mid">${boNum(t.neutral)}</b></span>
        <span>乐观 P75<b>${boNum(t.optimistic)}</b></span>
        <span>近端阻力 前高<b>${boNum(t.resistance)}</b></span>
      </div>
      <div class="bo-note">${escHtml(t.basis)}${t.resistance_note ? ' ' + escHtml(t.resistance_note) + '。' : ''}</div>
      ${t.extreme ? `<div class="bo-warn-inline">${escHtml(t.extreme_note)}</div>` : ''}
    </div>`;
  } else if (t) {
    // 样本不足时不给目标价，但**近端阻力（前高）依旧给** —— 它是可验证的第一个里程碑
    tgt = `<div class="bo-target bo-target-none">
      <div class="bo-target-head">目标价：本股不给（样本不足）</div>
      ${t.resistance ? `<div class="bo-target-grid">
        <span>近端阻力 前高<b>${boNum(t.resistance)}</b></span>
      </div>` : ''}
      <div class="bo-note">${escHtml(t.basis)}${t.resistance_note ? ' ' + escHtml(t.resistance_note) + '。' : ''}</div>
    </div>`;
  }
  const lines = (s.lines || []).map(x => `<div class="bo-sell-line">
      <span class="bo-sell-k">${escHtml(x.k)}</span>
      <span class="bo-sell-v">${escHtml(x.v)}</span>
      <span class="bo-sell-why">${escHtml(x.why)}</span>
    </div>`).join('');
  return `<section class="bo-sec"><h4>⑦ 建议卖出的量化指标（本股专属）</h4>
    ${tgt}${lines}
    <div class="bo-note">以上四条是<strong>纪律线而非预测</strong>：长周期上涨不等于低回撤，
      run1 实测四段目标行情的最大回撤在 23%~49% 之间。事先想清楚「跌到哪条线减多少」，
      比事后临场判断可靠得多。</div>
  </section>`;
}

// ---- 展开区：提醒 ----
function boWarnHtml(r) {
  const items = [];
  items.push(`研究档案（run1 + run2 + 两次复核）的最终结论都是 <b>RESEARCH_REJECTED</b>：
    这套方法<strong>不足以直接用于实盘</strong>。本模块把它当<strong>筛选漏斗与纪律工具</strong>，
    而不是「买入信号」。`);
  if (r.state === 'EXTEND') {
    items.push(`当前处于<b>加速段·已走远</b>：run2 实测该状态 120 日净收益均值最高（+27.6%），
      但最大不利偏移也最深（−16.4%）—— 只宜作为<strong>已持仓的加仓参考</strong>，不宜新建重仓。`);
  }
  if (r.state === 'MIXED') {
    items.push(`当前<b>无明显趋势</b>：run2 实测该状态 120 日胜率仅 43.97%、中位收益 −2.59%，
      是全部状态中最差的回避区。`);
  }
  if (r.tier === 'T1') {
    items.push(`<b>左侧埋伏档已被告知证伪</b>：run2 用无未来函数的 PIT 口径检验后，
      「埋伏信号」的净增量只有 +1.49pp，8 年里 4 年为负，Bonferroni 校正后不显著。
      保留此档只是因为它符合「提前布局」的诉求 —— 请务必小仓位、分步建仓，并接受较高失败率。`);
  }
  items.push(`<b>数据缺口</b>：长历史为<strong>前复权</strong>价（hist.db 与主库 daily 同为前复权、重叠区间逐行相等；
    若个别源混入不复权序列会按板块涨跌停上限自动识别修复，本股兜底修复 ${r.adj_events || 0} 次）；
    仅有主库 ~288 根日K 的股票（约 1200 只，多为 301/688/北交所新代码）
    因不足 ${(_boMeta && _boMeta.min_bars) || 500} 根已被跳过。`);
  items.push(`<b>幸存者偏差</b>：池子只含当前在市股票，已退市的不在内 —— 与 run1 相同的结构性缺陷，
    无法用统计手段消除。`);
  items.push(`<b>统计效力</b>：run1 的有效独立周期仅 13 段、run2 仅 10 只标的 8 年。
    本模块虽然把样本扩到全市场（用<strong>该股自身历史</strong>做统计），
    但个股层面的「周期时长」仍是低样本推断，请只看量级、不要当精确预测。`);
  items.push(`本模块仅用于研究与教育目的，<b>不构成投资建议</b>，不承诺收益。
    期货与估值数据来自第三方，可能存在延迟或误差。`);
  return `<section class="bo-sec bo-sec-warn"><h4>⑧ 提醒与风险</h4>
    <ul class="bo-warns">${items.map(x => `<li>⚠ ${x}</li>`).join('')}</ul></section>`;
}

// ---- 展开区：长期走势图 ----
function boChartHtml(r) {
  return `<section class="bo-sec"><h4>⑨ 长期走势图（月线 · 历史上涨段高亮）</h4>
    <div class="bo-chart-actions">
      <button class="btn btn-ghost bo-chart-btn" type="button" data-code="${r.code}">📈 加载走势图</button>
      <span class="bo-note">月线收盘 + MA120/MA250；<b>绿色带</b>＝历史上涨段，<b>橙色带</b>＝进行中的段。
        （图表按需加载，避免几百只股票一次性拉数据）</span>
    </div>
    <div class="bo-chart-holder"></div></section>`;
}

function boDetailHtml(r, futMeta) {
  return boCompHtml(r) + boReasonHtml(r) + boCycleHtml(r) + boSectorHtml(r)
    + boFuturesHtml(r, futMeta) + boFundHtml(r) + boSellHtml(r) + boWarnHtml(r)
    + boChartHtml(r);
}

// 懒渲染：折叠态只放空壳，点开/批量展开时才生成明细 DOM
// （不做懒加载的话，几百只 × 9 个 section ≈ 十几万节点，首屏必卡 —— 与 9Reverse9 同因）
function boFillDetail(card) {
  const det = card.querySelector('.bo-detail');
  if (!det || det.children.length) return;
  const idx = Number(det.dataset.idx);
  const data = _boLast || {};
  const r = (data.results || [])[idx];
  det.innerHTML = r ? boDetailHtml(r, data.stats && data.stats.futures)
    : '<div class="bo-empty">数据已失效，请重新扫描</div>';
  det.querySelectorAll('.bo-chart-btn').forEach(b => b.addEventListener('click', () => {
    const holder = b.closest('.bo-sec').querySelector('.bo-chart-holder');
    b.disabled = true;
    b.textContent = '加载中…';
    loadBetonChart(b.dataset.code, holder, b);
  }));
}

function boCard(r, idx) {
  const t = (r.sell && r.sell.target) || {};
  const tgt = (t && !t.no_target) ? `${boNum(t.neutral)} 元` : '不给（样本不足）';
  const dur = (r.dur && r.dur.ok) ? r.dur.total_text : '—';
  const durTitle = (r.dur && r.dur.basis) ? escHtml(r.dur.basis) : '';
  const stCls = boStateCls(r.state);
  return `<div class="bo-card">
    <div class="bo-head">
      <span class="bo-rank" title="排序名次">${idx + 1}</span>
      <a href="#" class="stock-link code-link bo-code" data-code="${r.code}" title="快速查看 · 站内看K线">${r.code}</a>
      <a href="${thsLink(r.code)}" target="_blank" rel="noopener noreferrer" class="stock-link name-link bo-name" title="详细查询 · 同花顺新窗口">${escHtml(r.name || '')}</a>
      <span class="bo-badge ${boTierCls(r.tier)}" title="${escHtml(r.tier_name)}">${r.tier} · ${escHtml(r.tier_name)}</span>
      <span class="bo-badge bo-st ${stCls}" title="run2 实测该状态 120 日胜率 ${r.state_winrate}%（全样本 58.35%）">${escHtml(r.state_name)} · ${r.state_winrate}%</span>
      <span class="bo-metrics">
        <span class="bo-metric" title="买入评分（0~100，已计入折扣）"><i>评分</i><b>${r.score}</b></span>
        <span class="bo-metric" title="${durTitle}"><i>预估周期</i><b>${escHtml(dur)}</b></span>
        <span class="bo-metric" title="按该股自身历史涨幅分布推算的中性目标价"><i>中性目标</i><b>${escHtml(tgt)}</b></span>
        <span class="bo-metric" title="收盘价（数据截至 ${escHtml(r.date || '')}）"><i>现价</i><b>${boNum(r.close)}</b></span>
        <span class="bo-metric" title="参与分析的长历史日K根数"><i>K线数</i><b>${r.bars}</b></span>
      </span>
      <button class="btn btn-ghost bo-expand" type="button">详细 ▾</button>
    </div>
    <div class="bo-detail" hidden data-idx="${idx}"></div>
  </div>`;
}

const BO_RENDER_MAX = 300;   // 折叠行上限（CSV 仍导出全量）

function renderBeton(data) {
  const list = $('#boList');
  const sum = $('#boSummary');
  if (!list) return;
  const rows = (data && data.results) || [];
  if (!rows.length) {
    if (sum) sum.hidden = true;
    list.innerHTML = '<div class="bo-empty-big">— 暂无结果 —</div>';
    return;
  }
  const st = (data.stats) || {};
  const sk = (data.skip_stats) || {};
  const p = (data.params) || {};
  const skTxt = Object.keys(sk).length
    ? ' · 跳过：' + Object.entries(sk).map(([k, v]) => `${k}:${v}`).join(' / ') : '';
  const tierTxt = st.tiers
    ? ' · 档位：' + ['T2', 'T3', 'T1', 'T4']
      .filter(k => st.tiers[k]).map(k => `${k}:${st.tiers[k]}`).join(' / ') : '';
  const futTxt = (st.futures && st.futures.note) ? ` · ${st.futures.note}` : '';
  const durTxt = (st.dur_cut) ? ` · 因「预估周期」不达标剔除 ${st.dur_cut} 只` : '';
  if (sum) {
    sum.hidden = false;
    sum.innerHTML = `共 <b>${rows.length}</b> 只 · 数据截止 <b>${escHtml(data.ref_date || '—')}</b>`
      + ` · 参与分析 <b>${st.scanned || 0}</b> 只 · 最低分 <b>${p.min_score}</b>`
      + ` · 用时 <b>${data.elapsed || 0}s</b>`
      + `<span class="bo-sum-note">${tierTxt}${durTxt}${skTxt}${futTxt}</span>`;
  }
  const shown = rows.slice(0, BO_RENDER_MAX);
  _boLast = data;
  list.innerHTML = shown.map((r, i) => boCard(r, i)).join('')
    + (rows.length > shown.length
      ? `<div class="bo-more">仅渲染前 ${shown.length} / 共 ${rows.length} 只（不影响 CSV 导出）</div>`
      : '');
  list.querySelectorAll('.code-link').forEach(a => a.addEventListener('click', e => {
    e.preventDefault();
    switchTab('kline');
    $('#klineCode').value = a.dataset.code;
    loadKline();
  }));
  list.querySelectorAll('.bo-expand').forEach(b => b.addEventListener('click', () => {
    const card = b.closest('.bo-card');
    const det = card.querySelector('.bo-detail');
    const willOpen = det.hidden;
    if (willOpen) boFillDetail(card);
    det.hidden = !willOpen;
    b.textContent = willOpen ? '收起 ▴' : '详细 ▾';
  }));
  renderBetonWatch(data);
}

// 观察榜（宽松轨）—— 与主榜严格分离，带显著的「未经证实」警示条
function renderBetonWatch(data) {
  const box = $('#boWatch');
  if (!box) return;
  const rows = (data && data.watch) || [];
  if (!rows.length) { box.hidden = true; box.innerHTML = ''; return; }
  box.hidden = false;
  _boLastWatch = rows;
  const meta = _boMeta || {};
  const shown = rows.slice(0, BO_WATCH_RENDER_MAX);
  box.innerHTML = `
    <div class="bo-watch-head">
      <div class="bo-watch-title">🔍 长期形态观察榜
        <span class="bo-watch-cnt">共 ${rows.length} 只</span></div>
      <div class="bo-watch-warn">
        ⚠ 宽松轨 · <b>不是推荐</b>：只按「长期形态」筛选，不设分数门槛、未做显著性检验。
        封段回测实测本模块主榜分数的 Spearman IC ≈ 0（未来 120 日 −0.002 / 250 日 −0.018），
        即<b>基于价格的形态特征无法稳定预测未来 250 日收益</b>（与两份研究档案的
        RESEARCH_REJECTED 结论一致）。此处仅提供研究线索，请配合主榜与卖出纪律线使用。
      </div>
      <div class="bo-watch-crit">判据：${(meta.watch_criteria || []).map(escHtml).join('　·　')}</div>
    </div>
    <div class="bo-watch-list">${shown.map((r, i) => boWatchCard(r, i)).join('')}</div>
    ${rows.length > shown.length
      ? `<div class="bo-more">仅渲染前 ${shown.length} / 共 ${rows.length} 只（不影响 CSV 导出）</div>`
      : ''}`;
  box.querySelectorAll('.code-link').forEach(a => a.addEventListener('click', e => {
    e.preventDefault();
    switchTab('kline');
    $('#klineCode').value = a.dataset.code;
    loadKline();
  }));
}

function boWatchCard(r, i) {
  const dev = (r.px_ma250 == null) ? '—' : (r.px_ma250 * 100).toFixed(1) + '%';
  const fl = (r.loose_bull_60 == null) ? '—' : (r.loose_bull_60 * 100).toFixed(0) + '%';
  const pos = (r.pos250 == null) ? '—' : r.pos250.toFixed(2);
  const stCls = boStateCls(r.state);
  return `<div class="bo-watch-card">
    <span class="bo-rank">${i + 1}</span>
    <span class="code-link" data-code="${escHtml(r.code)}">${escHtml(r.code)}</span>
    <span class="bo-name">${escHtml(r.name || '')}</span>
    <span class="bo-badge ${boTierCls(r.tier)}">${escHtml(r.tier)} ${escHtml(boTierName(r.tier))}</span>
    <span class="bo-st ${stCls}">${escHtml(r.state_name || r.state || '')}</span>
    <span class="bo-shape">形态分 <b>${r.shape_score}</b></span>
    <span class="bo-watch-kv">年线偏离 ${dev}</span>
    <span class="bo-watch-kv">多头占比 ${fl}</span>
    <span class="bo-watch-kv">区间位置 ${pos}</span>
  </div>`;
}

function boToggleAll() {
  const cards = $$('#boList .bo-card');
  if (!cards.length) return;
  _boAllOpen = !_boAllOpen;
  if (_boAllOpen) cards.forEach(boFillDetail);
  cards.forEach(c => {
    c.querySelector('.bo-detail').hidden = !_boAllOpen;
    const b = c.querySelector('.bo-expand');
    if (b) b.textContent = _boAllOpen ? '收起 ▴' : '详细 ▾';
  });
  const btn = $('#btnBetonExpandAll');
  if (btn) btn.textContent = _boAllOpen ? '⇕ 全部收起' : '⇕ 全部展开';
}

async function runBeton() {
  hideMeme('boMeme');
  const cfg = boCfg();
  showOverlay('loading', 'Bet on：读取长历史（hist.db）→ 合并主库 → PIT 状态与因子 → 历史周期统计…', { cancellable: true });
  try {
    const data = await API.betonRun({ cfg, exchange: rangeVal() });
    hideOverlay();
    if (data && data.error) {
      showMeme('boMeme', 'error', '扫描失败', String(data.error));
      renderBeton({ results: [] });
      return;
    }
    _boLast = data;
    const rows = (data && data.results) || [];
    if (!rows.length) {
      showMeme('boMeme', 'empty', '这轮没有符合条件的股票',
        '把最低分下调、勾上更多趋势状态，或换一个范围再试');
      renderBeton(data);
      return;
    }
    const top = rows.slice(0, 3).map(r => `${r.name || r.code}`).join('、');
    showMeme('boMeme', 'success', `${rows.length} 只进入 Bet on 名单 🎯`,
      `最优：${top} · 主推档 ${(data.stats.tiers || {}).T2 || 0} 只 · 数据截止 ${data.ref_date || '—'}`);
    renderBeton(data);
  } catch (e) {
    hideOverlay();
    if (_userCancelled) { showMeme('boMeme', 'farewell', '已取消这次扫描', '随时可以重新开始'); return; }
    showMeme('boMeme', 'error', '请求失败', String(e));
  }
}

// 长期走势图：月线 + 历史上涨段背景带 + MA120/MA250
async function loadBetonChart(code, holder, btn) {
  try {
    const d = await API.betonKline(code, num('#boHistYears', 12));
    if (!d || !d.ok) {
      holder.innerHTML = `<div class="bo-empty">${escHtml((d && d.error) || '没有可用的长历史数据')}</div>`;
      if (btn) { btn.disabled = false; btn.textContent = '📈 加载走势图'; }
      return;
    }
    holder.innerHTML = `<canvas class="bo-chart" width="640" height="260"></canvas>
      <div class="bo-chart-legend">${
      (d.spans || []).map(x => `<span class="bo-lg-item"><i class="bo-lg-band"></i>
        ${x.from} → ${x.to}　+${x.gain_pct}% / ${x.days} 日${x.long ? '（长周期）' : ''}</span>`).join('')
    }${d.cur ? `<span class="bo-lg-item bo-lg-cur"><i class="bo-lg-band bo-lg-band-cur"></i>
        进行中：${d.cur.from} 起，已 ${d.cur.elapsed} 日 / ${d.cur.gain_pct >= 0 ? '+' : ''}${d.cur.gain_pct}%</span>` : ''}</div>`;
    const cv = holder.querySelector('canvas');
    drawBetonChart(cv, d);
    cv.style.cursor = 'zoom-in';
    cv.title = '点击在新窗口打开高清大图';
    cv.addEventListener('click', () => openChartWindow(cv, `${code} 长期走势（月线）`));
    if (btn) { btn.disabled = false; btn.textContent = '🔄 重新加载'; }
  } catch (e) {
    holder.innerHTML = `<div class="bo-empty">走势图加载失败：${escHtml(String(e))}</div>`;
    if (btn) { btn.disabled = false; btn.textContent = '📈 加载走势图'; }
  }
}

function drawBetonChart(cv, d) {
  const { ctx, W, H } = hiDPI(cv, 2);
  const series = [
    { key: 'close', data: d.close || [], color: '#b86b3f', w: 1.8, label: '月线收盘' },
    { key: 'ma120', data: d.ma120 || [], color: '#5b7c9d', w: 1.1, label: 'MA120' },
    { key: 'ma250', data: d.ma250 || [], color: '#8a8a8a', w: 1.1, label: 'MA250' },
  ];
  const n = (d.dates || []).length;
  if (n < 2) { ctx.fillStyle = '#a3a3a3'; ctx.fillText('数据不足', 12, 20); return; }
  const vals = [];
  series.forEach(s => s.data.forEach(v => { if (Number.isFinite(v) && v > 0) vals.push(v); }));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) { lo = 0; hi = 1; }
  const pad = (hi - lo) * 0.08 || 1;
  lo = Math.max(0, lo - pad); hi += pad;
  const L = 44, R = 8, T = 8, B = 18;
  const pw = W - L - R, ph = H - T - B;
  const X = i => L + (i / (n - 1)) * pw;
  const Y = v => T + ph - ((v - lo) / (hi - lo)) * ph;

  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, W, H);

  // 历史上涨段背景带（绿色）/ 进行中的段（橙色）
  const step = d.step || 21;
  (d.spans || []).forEach(sp => {
    const x0 = X(Math.max(0, Math.min(n - 1, sp.i0 / step)));
    const x1 = X(Math.max(0, Math.min(n - 1, sp.i1 / step)));
    ctx.fillStyle = 'rgba(46,139,87,0.10)';
    ctx.fillRect(Math.min(x0, x1), T, Math.max(1.5, Math.abs(x1 - x0)), ph);
  });
  if (d.cur) {
    const x0 = X(Math.max(0, Math.min(n - 1, d.cur.i0 / step)));
    ctx.fillStyle = 'rgba(184,107,63,0.16)';
    ctx.fillRect(Math.min(x0, X(n - 1)), T, Math.max(1.5, Math.abs(X(n - 1) - x0)), ph);
  }

  // 网格 + Y 轴刻度
  ctx.strokeStyle = '#eeebe2'; ctx.lineWidth = 1;
  ctx.font = '10px sans-serif'; ctx.fillStyle = '#a3a3a3'; ctx.textAlign = 'right';
  for (let k = 0; k <= 3; k++) {
    const v = lo + (hi - lo) * k / 3, y = Y(v);
    ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
    ctx.fillText(v.toFixed(v >= 100 ? 0 : 2), L - 5, y + 3.5);
  }
  // X 轴首末日期
  ctx.textAlign = 'left';
  ctx.fillText((d.dates[0] || '').slice(0, 7), L, H - 5);
  ctx.textAlign = 'right';
  ctx.fillText((d.dates[n - 1] || '').slice(0, 7), W - R, H - 5);

  // 折线
  series.forEach(s => {
    ctx.strokeStyle = s.color; ctx.lineWidth = s.w;
    ctx.beginPath();
    let started = false;
    s.data.forEach((v, i) => {
      if (!Number.isFinite(v) || v <= 0) { started = false; return; }
      const px = X(i), py = Y(v);
      if (!started) { ctx.moveTo(px, py); started = true; } else ctx.lineTo(px, py);
    });
    ctx.stroke();
  });

  // 图例
  let lx = L + 4;
  ctx.textAlign = 'left'; ctx.font = '10px sans-serif';
  series.forEach(s => {
    ctx.fillStyle = s.color;
    ctx.fillRect(lx, T + 2, 10, 2.5);
    ctx.fillStyle = '#6b6b6b';
    ctx.fillText(s.label, lx + 13, T + 6.5);
    lx += 13 + ctx.measureText(s.label).width + 14;
  });
}

// CSV：总表 + 得分构成 + 历史周期明细
function downloadBetonCsv() {
  const d = _boLast;
  if (!d || !((d.results || []).length)) {
    showMeme('boMeme', 'ask', '还没有可导出的结果', '先点「开始扫描」');
    return;
  }
  const head = ['排名', '代码', '名称', '板块', '评分', '原始分', '折扣', '买点档', '状态',
    '状态120日胜率%', '现价', '预估周期', '周期区间', '中性目标', '保守目标', '乐观目标',
    '前高', '目标样本数', 'K线数', '除权修复次数',
    ...BO_COMP_ORDER.map(k => BO_COMP_NAME[k]),
    '历史上涨段数', '长周期段数', '中位涨幅%', '中位时长日', '中位最大回撤%', '本轮涨幅%', '本轮已走日'];
  const lines = d.results.map((r, i) => {
    const t = (r.sell && r.sell.target) || {};
    const st = r.cycle_stats || {};
    const cv = r.cur_cycle || {};
    return [i + 1, r.code, r.name, r.sector, r.score, r.raw_score, r.penalty, r.tier,
      r.state_name, r.state_winrate, r.close,
      (r.dur && r.dur.ok) ? r.dur.total_text : '', (r.dur && r.dur.ok) ? r.dur.range_text : '',
      t.no_target ? '' : t.neutral, t.no_target ? '' : t.conservative,
      t.no_target ? '' : t.optimistic, t.resistance, t.count, r.bars, r.adj_events,
      ...BO_COMP_ORDER.map(k => (r.components || {})[k]),
      st.n, st.n_long,
      st.gain_med == null ? '' : (st.gain_med * 100).toFixed(1),
      st.days_med == null ? '' : Math.round(st.days_med),
      st.dd_med == null ? '' : (st.dd_med * 100).toFixed(1),
      cv.gain_pct, cv.elapsed].map(csvCell).join(',');
  });
  const meta = [
    ['口径', '研究结论', 'run1 DSR=0.8107<0.95、run2 埋伏增量+1.49pp，两轮均为 RESEARCH_REJECTED'],
    ['口径', '状态表（run2 §4.1）', Object.entries((d.stats || {}).state_winrate || {})
      .map(([k, v]) => `${k}=${v}%`).join(' / ')],
    ['口径', '折扣系数', `过热${0.8} / 假启动${0.75} / 波动过热${0.9} / 短历史${0.85} / 震荡区${0.85}`],
    ['口径', '长历史来源', (d.stats || {}).hist_db + '（前复权；跳空修复为兜底）'],
    ['口径', '跳过统计', Object.entries(d.skip_stats || {}).map(([k, v]) => `${k}=${v}`).join(' / ')],
  ].map(r => r.map(csvCell).join(','));
  const tail = ['', '—— 历史上涨段明细 ——', '代码,名称,起点,段内最高点,段结束,涨幅%,到顶天数,段内最大回撤%,是否长周期'];
  d.results.forEach(r => {
    (r.cycles || []).forEach(x => tail.push([r.code, r.name, x.start, x.peak, x.end,
      x.gain_pct, x.days, x.max_dd_pct, x.long ? '是' : ''].map(csvCell).join(',')));
  });
  const idetail = ['', '—— 入选理由 ——', '代码,名称,理由'];
  d.results.forEach(r => (r.reasons || []).forEach(t =>
    idetail.push([r.code, r.name, String(t).replace(/<[^>]+>/g, '')].map(csvCell).join(','))));
  // 观察榜（宽松轨）：单独一段，并在口径里写明证据等级
  const watch = (d.watch || []);
  const wsec = [];
  if (watch.length) {
    const wmeta = [
      ['', '—— 长期形态观察榜（宽松轨）——', ''],
      ['口径', '声明', '只按长期形态筛选，不设分数门槛、未做显著性检验；'
        + '实测主榜分数 Spearman IC≈0（未来120日 −0.002 / 250日 −0.018），'
        + '形态特征无法稳定预测未来250日收益。此表仅提供研究线索，不是推荐。'],
      ['口径', '判据', ((_boMeta || {}).watch_criteria || []).join(' / ')],
      ['代码', '名称', '板块,当前状态,买点档,形态分,年线偏离,中期多头占比,250日区间位置,现价'].join(','),
    ];
    const wlines = watch.map(x => [x.code, x.name, x.sector, x.state_name, x.tier,
      x.shape_score, x.px_ma250, x.loose_bull_60, x.pos250, x.close]
      .map(csvCell).join(','));
    wsec.push(...wmeta, ...wlines);
  }
  const csv = '\ufeff' + [head.join(','), ...lines, ...meta, ...tail, ...idetail, ...wsec].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'BetOn_' + new Date().toISOString().slice(0, 10) + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---- 面板元信息：首次切到该页签时拉一次（权重 / 最小K线数 / 状态表） ----
// 懒加载而不是 init() 里同步拉，避免拖慢启动；失败时给一份兜底结构，
// 这样「得分构成」仍能渲染（只是没有权重注释）。
async function initBeton() {
  if (_boMeta) return;
  try {
    _boMeta = await API.betonMeta();
  } catch (e) {
    _boMeta = { weights: [], states: [], tiers: [], min_bars: 500 };
  }
}

// ========= 蚂蚁 =========
async function runAnt() {
  hideMeme('antMeme');
  showOverlay('loading', `蚂蚁四层扫描中…（${estText('ant', rangeVal())}）`, { cancellable: true });
  try {
    const data = await API.antRun({
      exchange: rangeVal(),
      cfg: {
        min_score: Number($('#antMinScore').value),
        only_trigger: $('#antOnlyTrigger').checked,
        scan: { lookback: num('#aLookback', 300) },
        hard_filter: { min_list_days: num('#aMinListDays', 250),
                       liquidity_quantile: num('#aLiquidityQ', 0.30) },
        pattern: { amplitude_range: [num('#aAmpMin', 0.20), num('#aAmpMax', 0.45)],
                   p_threshold: num('#aPThreshold', 0.05),
                   close_pos_range: [num('#aClosePosMin', 0.25), num('#aClosePosMax', 0.70)],
                   second_half_vol_ratio_max: num('#aSecondHalfVol', 0.85),
                   recent10_vol_ratio_max: num('#aRecent10Vol', 0.75),
                   up_down_vol_ratio_min: num('#aUpDownVol', 1.15),
                   min_up_days: 3, min_down_days: 3 },
        ranking: { min_bottom_rise_pct: num('#aBottomRise', 0.02),
                   support_touch_min: num('#aSupportTouch', 2),
                   ma_convergence_pct: num('#aMaConverge', 3),
                   ma_convergence_ratio: num('#aMaConvRatio', 0.6),
                   excess_return_min: num('#aExcessReturn', 0),
                   downside_resistance_ratio: num('#aDownside', 0.85),
                   prior_drop_min: num('#aPriorDrop', 0.10) },
        chip_filter: { enable: $('#aChipEnable').checked,
                       only_keep_up: $('#aChipKeepUp').checked },
        breakout: { volume_ratio: num('#aVolumeRatio', 1.5) },
      },
      only_pass: !$('#antOnlyTrigger').checked,
      limit: 300,
    });
    hideOverlay();
    const rows = data.rows || [];
    if (rows.length === 0) {
      showMeme('antMeme', 'empty', '这轮没扫到蚂蚁', '放宽分数阈值再试');
      renderTable('antTable', []);
      return;
    }
    const triggered = rows.filter(r => (r['启动状态'] || '').includes('启动')).length;
    showMeme('antMeme', 'success',
      `扫到 ${rows.length} 只，其中 ${triggered} 只接近启动 🐜`,
      '总分越高越值得关注');
    renderTable('antTable', rows.map(r => ({
      '代码': r['代码'], '名称': r['名称'], '板块': r['板块'],
      '总分': r['总分（0-7）'], '有效项': r['有效项数'],
      '启动状态': r['启动状态'], '振幅60日': r['60日振幅'],
      '质量': r.data_quality,
    })), { max: 300 });
  } catch (e) {
    hideOverlay();
    if (_userCancelled) { showMeme('antMeme', 'farewell', '已取消这次扫描', '随时可以重新开始'); return; }
    showMeme('antMeme', 'error', '请求失败', String(e));
  }
}

// ========= 持有个1000年试试呢？（长周期版蚂蚁） =========
async function refreshAnt1000Cache() {
  try {
    const c = await API.ant1000Cache();
    const el = $('#ant1000Cache');
    if (!el) return;
    const miss = (c.missing || 0) + (c.failed || 0);
    el.textContent = `历史数据缓存：${c.ok || 0}/${c.total || 0} 只` +
      (miss ? `（待补齐 ${miss} 只）` : '（已就绪）');
  } catch (e) { /* 忽略 */ }
}

async function runAnt1000() {
  hideMeme('ant1000Meme');
  const years = Number(($('#ant1000Years') || {}).value) || 20;
  // 首次运行预估：把缺失数量提前告诉用户（避免对长时间等待感到意外）
  let tip = `${years} 年窗口`;
  try {
    const c = await API.ant1000Cache();
    const miss = (c.missing || 0) + (c.failed || 0);
    if (miss > 50) {
      const est = Math.max(1, Math.round(miss * 2.45 / 6 / 60));   // 实测 ~2.45s/只 · 6 并发
      tip = `首次运行需下载 ${miss} 只历史数据（约 ${est} 分钟，仅一次）`;
    }
  } catch (e) { /* 忽略 */ }
  showOverlay('loading', `长周期扫描中…（${tip}）`, { cancellable: true });
  try {
    const data = await API.ant1000Run({
      years,
      exchange: rangeVal(),
      cfg: {
        hard_filter: { min_list_years: num('#a1MinYears', 5),
                       liquidity_quantile: num('#a1LiqQ', 0.30) },
        cycle: { zigzag_thr: num('#a1Zigzag', 0.30),
                 swing_dd_min: num('#a1SwingDd', 0.35),
                 swing_up_min: num('#a1SwingUp', 0.70) },
        score: { max_dd_min: num('#a1MaxDd', 40) / 100,
                 max_rebound_min: num('#a1MaxRebound', 60) / 100,
                 trend_annual_max: num('#a1TrendMax', 10),
                 downside_resistance: num('#a1Downside', 0.85),
                 low_pos_max: num('#a1LowPos', 50) / 100,
                 high_drop_min: num('#a1HighDrop', 25) / 100 },
      },
      only_pass: true,
    });
    hideOverlay();
    refreshAnt1000Cache();
    const allRows = data.rows || [];
    const minScore = num('#a1MinScore', 4);
    const rows = allRows.filter(r => (Number(r['总分（0-7）']) || 0) >= minScore);
    if (rows.length === 0) {
      showMeme('ant1000Meme', 'empty', '这轮没筛到长周期股',
        allRows.length ? `总分≥${minScore} 无结果（共 ${allRows.length} 只通过硬过滤，可调低阈值）` : '换个窗口或放宽参数再试');
      renderTable('ant1000Table', []);
      return;
    }
    const top = rows.slice(0, 3).map(r => r['名称']).join('、');
    showMeme('ant1000Meme', 'success',
      `筛到 ${rows.length} 只长周期标的 🐢（总分≥${minScore}）`,
      `前三：${top} · 总分越高「周期特征」越强 · 点击代码看长周期图`);
    renderTable('ant1000Table', rows.map(r => ({
      '代码': r['代码'], '名称': r['名称'], '板块': r['板块'],
      '总分': r['总分（0-7）'], '有效项': r['有效项数'],
      '循环数': r['周期循环数'], '上市年': r['上市年数'],
      '最大回撤%': r['历史最大回撤%'], '最大反弹%': r['最大反弹%'],
      '年化波动%': r['年化波动率%'], '趋势年化%': r['趋势年化%'],
      '当前分位%': r['当前分位%'], '距高回撤%': r['距高点回撤%'],
      '长期超额%': r['长期超额%'], '熊市韧性': r['熊市韧性'],
    })), { max: 300 });
    bindAnt1000Clicks();
    loadAnt1000Chart(rows[0]['代码']);   // 自动展示第一名
  } catch (e) {
    hideOverlay();
    if (_userCancelled) { showMeme('ant1000Meme', 'farewell', '已取消这次扫描', '随时可以重新开始'); return; }
    showMeme('ant1000Meme', 'error', '请求失败', String(e));
  }
}

// 结果表第一列「代码」改为：点击即绘制该股长周期图（覆盖默认的跳 K 线行为）
function bindAnt1000Clicks() {
  const t = $('#ant1000Table');
  if (!t) return;
  t.querySelectorAll('tbody .code-link').forEach(a => {
    const clone = a.cloneNode(true);   // 克隆以清空旧的跳转监听
    a.replaceWith(clone);
    clone.title = '点击查看该股长周期图';
    clone.addEventListener('click', e => {
      e.preventDefault();
      loadAnt1000Chart(clone.dataset.code);
    });
  });
}

async function loadAnt1000Chart(code) {
  const years = Number(($('#ant1000Years') || {}).value) || 20;
  showOverlay('loading', `加载 ${code} 长周期数据…`);
  try {
    const d = await API.ant1000Kline(code, years);
    hideOverlay();
    if (d.error) {
      showMeme('ant1000Meme', 'error', `无法加载 ${code} 长周期图`, d.error);
      return;
    }
    drawHistChart(d);
    const cv = $('#ant1000Chart');
    if (cv) { cv.hidden = false; }
  } catch (e) {
    hideOverlay();
    showMeme('ant1000Meme', 'error', '长周期图加载失败', String(e));
  }
}

// 长周期月线图：对数价格轴 + ZigZag 顶底标注 + 循环配对连线 + 上证指数对比线
function drawHistChart(d) {
  const canvas = $('#ant1000Chart');
  if (!canvas) return;
  const { ctx, W, H } = hiDPI(canvas);
  const PAD_L = 58, PAD_R = 16, PAD_T = 30, PAD_B = 34;
  ctx.clearRect(0, 0, W, H);
  const months = d.months || [];
  if (!months.length) return;
  const N = months.length;
  const closes = months.map(m => m.close);
  const idxs = d.index || [];
  // —— 数据范围（对数轴）——
  let mn = Math.min(...closes), mx = Math.max(...closes);
  idxs.forEach(p => { if (p.v > 0) { mn = Math.min(mn, p.v); mx = Math.max(mx, p.v); } });
  if (!(mn > 0)) mn = Math.max(mx * 0.001, 0.001);
  const logMin = Math.log(mn), logMax = Math.log(mx);
  const xStep = (W - PAD_L - PAD_R) / Math.max(N - 1, 1);
  const xAt = i => PAD_L + i * xStep;
  const yMap = v => H - PAD_B - ((Math.log(Math.max(v, 1e-6)) - logMin) / (logMax - logMin || 1)) * (H - PAD_T - PAD_B);
  const dateIdx = {};
  months.forEach((m, i) => { dateIdx[m.date] = i; });
  // —— 标题 ——
  ctx.fillStyle = '#2a2a2a';
  ctx.font = '600 13px sans-serif';
  const cyc = d.cycles || [];
  ctx.fillText(`${d.code} ${d.name} · 月线（对数轴） · 窗口 ${d.years} 年 · 完整循环 ${cyc.length} 次`, PAD_L, 18);
  // —— 网格（对数均匀 5 条）——
  ctx.strokeStyle = '#eeebe2'; ctx.lineWidth = 1;
  ctx.font = '10px sans-serif';
  for (let i = 0; i <= 4; i++) {
    const lv = logMin + (logMax - logMin) * i / 4;
    const y = yMap(Math.exp(lv));
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(W - PAD_R, y); ctx.stroke();
    ctx.fillStyle = '#a3a3a3';
    ctx.fillText(Math.exp(lv) >= 100 ? Math.exp(lv).toFixed(0) : Math.exp(lv).toFixed(2), 6, y + 3);
  }
  // —— 时间轴标签（约每 1/8 宽度一个年）——
  ctx.fillStyle = '#a3a3a3';
  const labelStep = Math.max(1, Math.ceil(N / 8));
  months.forEach((m, i) => {
    if (i % labelStep === 0 || i === N - 1) {
      ctx.fillText(m.date.slice(0, 4), xAt(i) - 12, H - PAD_B + 14);
    }
  });
  // —— 指数对比线（归一化到股票起点价，灰色虚线）——
  if (idxs.length >= 2) {
    const baseIdx = idxs[0].v, baseStk = closes[dateIdx[idxs[0].date]] || closes[0];
    ctx.strokeStyle = '#b9b3a4'; ctx.lineWidth = 1.2; ctx.setLineDash([5, 4]);
    ctx.beginPath();
    let started = false;
    idxs.forEach(p => {
      const i = dateIdx[p.date];
      if (i === undefined) return;
      const v = p.v / baseIdx * baseStk;   // 指数平移到股票量纲
      if (!started) { ctx.moveTo(xAt(i), yMap(v)); started = true; }
      else ctx.lineTo(xAt(i), yMap(v));
    });
    ctx.stroke(); ctx.setLineDash([]);
  }
  // —— 股票收盘折线 ——
  ctx.strokeStyle = '#b86b3f'; ctx.lineWidth = 1.6;
  ctx.beginPath();
  months.forEach((m, i) => {
    if (i === 0) ctx.moveTo(xAt(i), yMap(m.close));
    else ctx.lineTo(xAt(i), yMap(m.close));
  });
  ctx.stroke();
  // —— 循环区间底色（峰值顶 → 终顶）——
  cyc.forEach(c => {
    const i0 = dateIdx[c.peak0], i1 = dateIdx[c.peak1];
    if (i0 === undefined || i1 === undefined) return;
    ctx.fillStyle = 'rgba(184,107,63,.07)';
    ctx.fillRect(xAt(i0), PAD_T, Math.max(xAt(i1) - xAt(i0), 1), H - PAD_T - PAD_B);
  });
  // —— ZigZag 顶底标注 ——
  (d.pivots || []).forEach(p => {
    const i = dateIdx[p.date];
    if (i === undefined) return;
    const x = xAt(i), y = yMap(p.price);
    ctx.beginPath();
    if (p.kind === 'H') {           // 顶：红色倒三角
      ctx.fillStyle = '#c0392b';
      ctx.moveTo(x, y - 5); ctx.lineTo(x - 4.5, y - 13); ctx.lineTo(x + 4.5, y - 13);
    } else {                         // 底：绿色正三角
      ctx.fillStyle = '#2e8b57';
      ctx.moveTo(x, y + 5); ctx.lineTo(x - 4.5, y + 13); ctx.lineTo(x + 4.5, y + 13);
    }
    ctx.closePath(); ctx.fill();
  });
  // —— 循环配对标注（底点上标 dd/up）——
  ctx.font = '10px sans-serif';
  cyc.forEach(c => {
    const i = dateIdx[c.trough];
    if (i === undefined) return;
    const p = (d.pivots || []).find(pp => pp.date === c.trough);
    if (!p) return;
    const x = xAt(i), y = yMap(p.price);
    const yBase = Math.min(y + 30, H - PAD_B + 24);
    ctx.fillStyle = '#2e8b57';
    ctx.fillText(`↓${c.dd}%`, x - 12, yBase);
    ctx.fillStyle = '#c0392b';
    ctx.fillText(`↑${c.up}%`, x - 12, yBase + 12);
  });
  // —— 图例 ——
  ctx.font = '10.5px sans-serif';
  ctx.fillStyle = '#b86b3f';
  ctx.fillText('—— 月收盘价', W - 300, 18);
  ctx.fillStyle = '#b9b3a4';
  ctx.fillText('---- 上证指数（归一化）', W - 225, 18);
  ctx.fillStyle = '#c0392b';
  ctx.fillText('▲顶', W - 90, 18);
  ctx.fillStyle = '#2e8b57';
  ctx.fillText('▼底', W - 60, 18);
}

// ========= 相似股 =========
async function runSimilar() {
  hideMeme('similarMeme');
  const code = $('#similarCode').value.trim();
  if (!code) { showMeme('similarMeme', 'ask', '请输入目标代码', '例如 600519'); return; }
  const volMode = $('#similarVolMode').value || 'ignore';
  const volThreshold = Number($('#similarVolThreshold').value) || 50;
  const volNote = volMode === 'similar'
    ? `（已约束波动幅度相似，阈值 ±${volThreshold}%）`
    : '（无视波动幅度，只看相关性）';
  showOverlay('loading', '正在全市场匹配相似走势…', { cancellable: true });
  try {
    const data = await API.similarRun({
      target_code: code,
      ref_days: Number($('#similarRefDays').value),
      max_lag: num('#similarMaxLag', 5),
      vol_mode: ($('#similarVolMode') || {}).value || 'ignore',
      vol_threshold: num('#similarVolThreshold', 50),
      top_n: Number($('#similarTopN').value),
      vol_mode: volMode,
      vol_threshold: volThreshold,
    });
    hideOverlay();
    if (data.detail) { showMeme('similarMeme', 'error', '参数有误', JSON.stringify(data.detail).slice(0, 120)); return; }
    if (data.error) { showMeme('similarMeme', 'error', '匹配失败', String(data.error)); return; }
    const a = data.a || [], b = data.b || [], c = data.c || [];
    // 图上每类只画最符合的 2 只（后端已按相关度排序）；表格仍列全部
    const pick2 = (arr, cls) => arr.slice(0, 2).map((x, j) => ({ ...x, cls, sub: j }));
    drawSimilarChart(data.target, [
      ...pick2(a, 'a'), ...pick2(b, 'b'), ...pick2(c, 'c'),
    ]);
    const mapCols = r => ({
      '代码': r.code, '名称': r.name, '相关度': r.corr,
      '领先/滞后(日)': (r.lag === null || r.lag === undefined || r.lag === 'None') ? '—' : r.lag,
      '备注': r.note || '—',
    });
    renderTable('similarATable', a.map(mapCols), { max: 50 });
    renderTable('similarBTable', b.map(mapCols), { max: 50 });
    renderTable('similarCTable', c.map(mapCols), { max: 50 });
    showMeme('similarMeme', 'success',
      `a 类 ${a.length} · b 类 ${b.length} · c 类 ${c.length}`,
      '图上鲜红线是目标股，每类只画最像的 2 只（A蓝/B橙/C绿，虚线为第2只）；' + volNote);
  } catch (e) {
    hideOverlay();
    if (_userCancelled) { showMeme('similarMeme', 'farewell', '已取消这次匹配', '随时可以重新开始'); return; }
    showMeme('similarMeme', 'error', '请求失败', String(e));
  }
}

function drawSimilarChart(target, candidates) {
  const canvas = $('#similarChart');
  const { ctx, W, H } = hiDPI(canvas);   // 2x 超采样高清
  const PAD = 46;
  ctx.clearRect(0, 0, W, H);
  const series = [];
  if (candidates.length > 0 && candidates[0].chart) {
    const t0 = candidates[0].chart;
    const tVals = Array.isArray(t0.target) ? t0.target : (Array.isArray(t0.cand) ? t0.cand : null);
    if (tVals) series.push({ name: `目标 ${target.code} ${target.name || ''}`, vals: tVals, color: '#ff2d2d', width: 3, dash: [] });   // 目标股：鲜亮红
    // a 蓝 / b 橙 / c 绿 —— 高区分度且柔和不刺眼；类内第 1 只实线、第 2 只虚线
    const palette = { a: '#1f6fd0', b: '#e8890c', c: '#18a47c' };
    candidates.forEach(cd => {
      const vals = Array.isArray(cd.chart.cand) ? cd.chart.cand : (Array.isArray(cd.chart.candidate) ? cd.chart.candidate : null);
      if (vals) series.push({ name: `[${cd.cls.toUpperCase()}${cd.sub ? 2 : 1}] ${cd.code} ${cd.name || ''}`, vals,
                              color: palette[cd.cls] || '#999', width: 1.6, dash: cd.sub ? [6, 3] : [] });
    });
  }
  if (series.length === 0) {
    ctx.fillStyle = '#a3a3a3'; ctx.font = '13px sans-serif';
    ctx.fillText('暂无对比数据：请先运行匹配', PAD, PAD);
    return;
  }
  let mn = Infinity, mx = -Infinity;
  series.forEach(s => s.vals.forEach(v => { if (v != null) { mn = Math.min(mn, v); mx = Math.max(mx, v); } }));
  if (!isFinite(mn)) { mn = 0; mx = 1; }
  const pad = (mx - mn) * 0.08 || 1;
  mn -= pad; mx += pad;
  const yMap = v => H - PAD - ((v - mn) / (mx - mn)) * (H - PAD * 2);
  ctx.strokeStyle = '#eeebe2'; ctx.lineWidth = 1;
  for (let i = 0; i < 5; i++) {
    const y = PAD + (H - PAD * 2) * (i / 4);
    ctx.beginPath(); ctx.moveTo(PAD, y); ctx.lineTo(W - PAD, y); ctx.stroke();
    ctx.fillStyle = '#a3a3a3'; ctx.font = '10px sans-serif';
    ctx.fillText((mx - (mx - mn) * i / 4).toFixed(1), 6, y + 3);
  }
  series.forEach(s => {
    ctx.strokeStyle = s.color; ctx.lineWidth = s.width;
    ctx.setLineDash(s.dash || []);
    ctx.beginPath();
    // 关键修复：每条序列按自身长度独立铺满整个 x 轴（形态对比语义）。
    // a/b/c 三类窗口长度不同（a 类可变、b/c 各有天数），若共用"最长序列"的
    // x 轴刻度，短窗口的目标线与 a 类线只能画到左侧一小段（后半段空白）。
    const step = (W - PAD * 2) / Math.max(s.vals.length - 1, 1);
    let started = false;
    s.vals.forEach((v, i) => {
      if (v == null) return;
      const x = PAD + i * step, y = yMap(v);
      started ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      started = true;
    });
    ctx.stroke();
  });
  ctx.setLineDash([]);
  ctx.font = '11px sans-serif';
  let lx = PAD;
  series.slice(0, 7).forEach(s => {
    ctx.fillStyle = s.color;
    const label = '— ' + s.name;
    ctx.fillText(label, lx, 16);
    lx += ctx.measureText(label).width + 14;
    if (lx > W - 120) return;
  });
}

// ========= 前瞻预警 =========
let _alertLast = null;  // 最近一次预警结果（供网页报告导出复用）

async function runAlert() {
  hideMeme('alertMeme');
  const sections = [];
  [['alertSecCalendar', 'calendar'], ['alertSecStealth', 'stealth'], ['alertSecCold', 'cold'],
   ['alertSecNational', 'national'], ['alertSecForeign', 'foreign'],
   ['alertSecCrowding', 'crowding'], ['alertSecChains', 'chains'],
   ['alertSecSpecial', 'special']]
    .forEach(([id, key]) => { const el = $('#' + id); if (el && el.checked) sections.push(key); });
  if (sections.length === 0) {
    showMeme('alertMeme', 'ask', '请至少勾选一个模块', '核心信号会基于勾选模块自动合成');
    return;
  }
  const cfg = {
    calendar: { horizon_days: num('#aCalHorizon', 35) },
    stealth: { gain_min: num('#aGainMin', 6), inflow_min: num('#aInflowMin', 3),
               turnover_pct_max: num('#aTurnoverPctMax', 60),
               hot_gain_min: num('#aHotGain', 20),
               hot_turnover_pct_min: num('#aHotTurnoverPct', 80) },
    cold: { window: num('#aColdWindow', 60), top_n: num('#aColdTopN', 3),
            w_amount: num('#aWAmt', 0.35), w_turnover: num('#aWTur', 0.35),
            w_gain: num('#aWGain', 0.30) },
    national: { days: num('#aNatlDays', 5), groups: (() => {
      // 四列表各自可选：ETF / 汇金 / 社保 / 证金（全不勾 = 该节不拉取名单）
      const gs = [];
      [['natGrpEtf', 'etf'], ['natGrpHjj', 'huijin'], ['natGrpSsf', 'ssf'], ['natGrpZjj', 'zhengjin']]
        .forEach(([id, k]) => { const el = $('#' + id); if (el && el.checked) gs.push(k); });
      return gs;
    })() },
    crowding: { turnover_pct: num('#aCrowdTurnover', 0.90),
                amount_pct: num('#aCrowdAmount', 0.90),
                gain20_min: num('#aCrowdGain', 15),
                trigger_n: num('#aCrowdTrigger', 2) },
    foreign: { top_n: num('#aForeignTopN', 10) },
    special: { window: num('#aSpecWindow', 3), zodiac_lead_days: num('#aSpecZodiacLead', 150),
               min_hits: num('#aSpecMinHits', 5), top_n: num('#aSpecTopN', 15),
               excess_min: num('#aSpecExcess', 1.5), limitup_ratio_min: num('#aSpecLuRatio', 2),
               density_breadth_floor: num('#aSpecLuFloor', -5),
               surge_pct: num('#aSpecSurge', 5), standing_top_n: num('#aSpecObserve', 3) },
  };
  showOverlay('loading', `前瞻预警扫描中（联网采集）…（${estText('alert', rangeVal())}）`, { cancellable: true });
  try {
    const data = await API.alertRun({ cfg, sections });
    hideOverlay();
    if (data.cancelled) { showMeme('alertMeme', 'farewell', '已取消这次预警', '随时可以重新开始'); return; }
    if (data.error) { showMeme('alertMeme', 'error', '预警失败', String(data.error).slice(0, 140)); return; }
    _alertLast = data;
    renderAlert(data);
    const sig = (data.signals || []).length;
    const st = (data.stealth || []).length + (data.selling || []).length;
    const spHit = ((data.special || {}).concepts || []).length;
    showMeme('alertMeme', sig > 0 || st > 0 ? 'success' : 'empty',
      `核心信号 ${sig} 条 · 异动 ${st} 条 · 特殊概念 ${spHit} 个 ⏰`,
      `耗时 ${data.elapsed || '—'}s；点击代码可站内查看 K 线`);
  } catch (e) {
    hideOverlay();
    if (_userCancelled) { showMeme('alertMeme', 'farewell', '已取消这次预警', '随时可以重新开始'); return; }
    showMeme('alertMeme', 'error', '请求失败', String(e));
  }
}

function renderAlert(data) {
  const cal = data.calendar || {};
  $('#alertMonthThemes').textContent =
    (cal['本月主线'] && cal['本月主线'].length) ? '本月主线：' + cal['本月主线'].join(' / ') : '';
  // 核心信号：动作导向（对象/动作/时效/依据/来源）。各模块表格负责「为什么」与明细，
  // 本表只回答「现在该做什么」，因此不再复述整段口径描述，避免与各模块重复。
  renderTable('alertSignalTable', (data.signals || []).map(r => ({
    '类型': r['类型'], '对象': r['对象'], '动作': r['动作'], '时效': r['时效'],
    '关键依据': r['依据'], '来源模块': r['来源'] || '' })), { max: 100 });
  renderTable('alertCalendarTable', (cal['日历预警'] || []).map(r => ({
    '事件': r['事件'], '日期': r['日期'], '距今(天)': r['距今(天)'], '备注': r['备注'] })), { max: 50 });
  renderTable('alertWindowTable', (cal['埋伏窗口'] || []).map(r => ({
    '主题': r['主题'], '关联事件': r['关联事件'], '事件日期': r['事件日期'],
    '距事件(天)': r['距事件(天)'], '规律窗口': r['规律窗口'] })), { max: 50 });
  renderTable('alertStealthTable', [
    ...(data.stealth || []).map(r => ({ '类型': '埋伏嫌疑', '板块': r['板块'],
      '5日涨幅%': r['5日涨幅%'], '5日主力净流入(亿)': r['5日主力净流入(亿)'],
      '换手分位%': r['换手分位%'] == null ? '—' : r['换手分位%'], '判定': r['判定'] })),
    ...(data.selling || []).map(r => ({ '类型': '出货嫌疑', '板块': r['板块'],
      '5日涨幅%': r['5日涨幅%'], '5日主力净流入(亿)': r['5日主力净流入(亿)'],
      '换手分位%': r['换手分位%'] == null ? '—' : r['换手分位%'], '判定': r['判定'] })),
  ], { max: 100 });
  renderTable('alertColdTable', (data.cold || []).map(r => {
    // 冷门表列名去歧义：原样输出 {板块, 冷度得分, 成交额占比%, 60日换手%, 60日涨幅%, 提示}
    // 其中窗口天数来自后端动态键，这里统一改名为「窗口换手%/窗口涨幅%」以免与「今日换手」混淆。
    const o = { '板块': r['板块'], '冷度得分': r['冷度得分'], '成交额占比%': r['成交额占比%'] };
    Object.keys(r).forEach(k => {
      if (/^\d+日换手%$/.test(k)) o['窗口换手%'] = r[k];
      else if (/^\d+日涨幅%$/.test(k)) o['窗口涨幅%'] = r[k];
    });
    o['判定'] = r['提示'];
    return o;
  }), { max: 50 });
  // 国家队四列表：新结构 {date, groups:{etf/huijin/ssf/zhengjin:{title, rows}}}
  const nat = (data.national && data.national.groups) ? data.national : { date: '', groups: {} };
  $('#natReportDate').textContent = nat.date ? `📋 ${nat.date} 报告期 · 前十大股东名单口径 · 持股≥1000万股` : '';
  const natCols = r => ({ '简称': r['简称'], '代码': r['代码'], '股东名称': r['股东名称'],
    '持股(万股)': r['持股(万股)'], '占总股本%': r['占总股本%'], '市值(亿)': r['市值(亿)'],
    '变动': r['变动'], '排名': r['排名'] });
  const natColsEtf = r => ({ '简称': r['简称'], '代码': r['代码'], '命中ETF只数': r['命中ETF只数'],
    '合计持仓市值(亿)': r['合计持仓市值(亿)'], '持有ETF明细': r['持有ETF明细'] });
  let natCount = 0;
  [['alertNatEtfTable', 'etf', 'natCntEtf'], ['alertNatHjjTable', 'huijin', 'natCntHjj'],
   ['alertNatSsfTable', 'ssf', 'natCntSsf'], ['alertNatZjjTable', 'zhengjin', 'natCntZjj']]
    .forEach(([tid, key, cid]) => {
      const rows = (nat.groups[key] || {}).rows || [];
      natCount += rows.length;
      renderTable(tid, rows.map(key === 'etf' ? natColsEtf : natCols), { max: 50 });
      const cnt = $('#' + cid);
      if (cnt) cnt.textContent = rows.length + ' 条';
    });
  renderTable('alertForeignTable', data.foreign || [], { max: 50 });
  renderTable('alertCrowdingTable', data.crowding || [], { max: 100 });
  renderTable('alertChainTable', data.chains || [], { max: 100 });
  // 特殊概念（名字玄学/谐音梗/生肖字辈）：概念总览 + 命中个股明细
  const sp = data.special || {};
  const spConcepts = sp.concepts || [];
  const spStocks = sp.stocks || [];
  const zoo = sp.zodiac || {};
  const spHint = $('#specialHint');
  if (spHint) {
    if (!data.special) spHint.textContent = '';               // 未勾选该模块
    else if (!sp.src) spHint.textContent = '数据源不可用，本次未取得全市场行情';  // 勾了但没拿到
    else spHint.textContent = `基准：全市场均涨 ${sp.market_avg}% · 上涨占比 ${sp['market_up%']}%`
      + ` · 涨停率 ${sp['market_lu%']}%（${sp.src}）`
      + (zoo['当前'] ? ` ｜ 当前生肖 ${zoo['当前']}年，下一生肖 ${zoo['下一']}年`
        + `（春节 ${zoo['春节日期']}，还有 ${zoo['距春节(天)']} 天）` : '');
  }
  const surgeHdr = '大涨(≥' + (sp.surge != null ? sp.surge : 5) + '%)';
  // 档位：🔴触发（跑赢全市场）/ 🟡异动（少数冲板、组内跑输）/ ⚪观察
  const gradeOf = r => r['触发'] ? '🔴 触发' : (r['异动'] ? '🟡 异动' : '⚪ 观察');
  renderTable('alertSpecialTable', spConcepts.map(r => ({
    '概念': r['概念'], '档位': gradeOf(r),
    '命中': r['命中'], '上涨': r['上涨'], '涨停': r['涨停'], [surgeHdr]: r['大涨'],
    '均涨%': r['均涨幅%'], '超额%': r['超额%'], '宽度%': r['宽度%'],
    '涨停密度×': r['涨停密度×'], '最强': r['最强'], '判定': r['判定'] })), { max: 50 });
  renderTable('alertSpecialStockTable', spStocks.map(r => ({
    '概念': r['概念'], '代码': r['代码'], '简称': r['简称'], '涨跌幅%': r['涨跌幅%'],
    '最新价': r['最新价'], '成交额(亿)': r['成交额(亿)'], '换手率%': r['换手率%'],
    '量比': r['量比'], '状态': r['涨停'] })), { max: 200 });
  $('#macroNarrative').textContent = data.macro_narrative || '';
  // 宏观雷达：横向卡片阵列（每主题/信号一卡，一行并排多卡）
  $('#macroCards').innerHTML = (data.macro || []).map(m => {
    const cls = m['方向'] === '偏宽松' ? 'up' : (m['方向'] === '偏紧缩' ? 'down' : '');
    return `<div class="macro-card ${cls}">` +
      `<div class="mc-head"><span class="mc-type">${escHtml(m['类型'])}</span>` +
      `<span class="mc-dir ${cls}">${escHtml(m['方向'])}</span></div>` +
      `<div class="mc-title">${escHtml(m['触发'])}</div>` +
      `<div class="mc-detail">${escHtml(m['细节'])}</div>` +
      `<div class="mc-note">${escHtml(m['应对'])}</div></div>`;
  }).join('') || '<div class="hint">雷达无回波——近端快讯未见宏观主题相关报道</div>';
  // 排版优化：本次无数据的模块卡片整体收起，并给有数据的卡片加计数徽章
  const counts = {
    alertSignalTable: (data.signals || []).length,
    alertCalendarTable: (cal['日历预警'] || []).length,
    alertWindowTable: (cal['埋伏窗口'] || []).length,
    alertStealthTable: (data.stealth || []).length + (data.selling || []).length,
    alertColdTable: (data.cold || []).length,
    alertNatEtfTable: ((data.national || {}).groups || {}).etf ? (data.national.groups.etf.rows || []).length : 0,
    alertNatHjjTable: ((data.national || {}).groups || {}).huijin ? (data.national.groups.huijin.rows || []).length : 0,
    alertNatSsfTable: ((data.national || {}).groups || {}).ssf ? (data.national.groups.ssf.rows || []).length : 0,
    alertNatZjjTable: ((data.national || {}).groups || {}).zhengjin ? (data.national.groups.zhengjin.rows || []).length : 0,
    alertForeignTable: (data.foreign || []).length,
    alertCrowdingTable: (data.crowding || []).length,
    alertChainTable: (data.chains || []).length,
    alertSpecialTable: spConcepts.length,
    alertSpecialStockTable: spStocks.length,
  };
  // 各区块空数据时收起 + 计数徽章（核心信号已改为通栏，不再受 280px 网格影响）
  const hiddenNames = [];
  $$('#tab-alert .sim-block').forEach(b => {
    if (b.id === 'natOuterBlock') return;   // 国家队外块单独按四子表合计处理（见下方 natOuter）
    if (b.id === 'macroOuterBlock') return; // 宏观雷达单独按 macro 行数处理（见下方 macroOuter）
    const t = b.querySelector('table');
    const n = t ? (counts[t.id] ?? 0) : 0;
    b.hidden = n === 0;
    const h3 = b.querySelector('h3');
    if (!h3) return;
    if (n === 0) { hiddenNames.push(h3.textContent.trim().split(' ·')[0]); return; }
    let badge = h3.querySelector('.sec-badge');
    if (!badge) { badge = document.createElement('span'); badge.className = 'sec-badge'; h3.appendChild(badge); }
    badge.textContent = n + ' 条';
  });
  const macroOuter = $('#macroOuterBlock');
  if (macroOuter) macroOuter.hidden = !(data.macro || []).length;
  const baseNotes = (data.notes || []).length ? '数据说明：' + data.notes.join(' ｜ ') : '';
  const hiddenNote = hiddenNames.length ? `（无数据已收起：${hiddenNames.join(' · ')}）` : '';
  const natOuter = $('#natOuterBlock');
  if (natOuter) natOuter.hidden = natCount === 0;   // 外块由四子表合计决定（上面的循环会误命中第一个子表的计数）
  $('#alertNotes').textContent = [baseNotes, hiddenNote].filter(Boolean).join('  ');
  refreshSecToggleState();
}

// ========= 前瞻预警：点击结果卡片标题 = 选入/踢出该模块（与上方勾选框联动） =========
const SEC_CHECKBOX = { calendar: 'alertSecCalendar', stealth: 'alertSecStealth', cold: 'alertSecCold',
  national: 'alertSecNational', foreign: 'alertSecForeign', crowding: 'alertSecCrowding',
  chains: 'alertSecChains', special: 'alertSecSpecial' };
function refreshSecToggleState() {
  $$('.sec-toggle').forEach(h => {
    const cb = $('#' + (SEC_CHECKBOX[h.dataset.sec] || ''));
    h.classList.toggle('sec-off', !(cb && cb.checked));
  });
}
function bindSecToggles() {
  $$('.sec-toggle').forEach(h => {
    h.addEventListener('click', () => {
      const cb = $('#' + (SEC_CHECKBOX[h.dataset.sec] || ''));
      if (!cb) return;
      cb.checked = !cb.checked;
      refreshSecToggleState();
    });
  });
  refreshSecToggleState();
}

async function exportAlertReport() {
  if (!_alertLast) { showMeme('alertMeme', 'ask', '还没有预警结果', '先点「生成预警」再导出报告'); return; }
  showOverlay('loading', '正在渲染网页报告…');
  try {
    const r = await API.alertReport({ result: _alertLast });
    hideOverlay();
    if (!r.html) { showMeme('alertMeme', 'error', '报告生成失败', '后端未返回内容'); return; }
    const blob = new Blob([r.html], { type: 'text/html;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '前瞻预警_' + (_alertLast.date || new Date().toISOString().slice(0, 10)) + '.html';
    a.click();
    window.open(url, '_blank');
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    showMeme('alertMeme', 'success', '报告已导出 📄', '已在新标签页打开并开始下载');
  } catch (e) {
    hideOverlay();
    showMeme('alertMeme', 'error', '报告生成失败', String(e));
  }
}

// ========= 快讯轮播条 =========
const escHtml = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// 图表超采样：canvas 像素尺寸放大 scale 倍、CSS 尺寸不变、绘制坐标不变 → 输出清晰度翻倍
function hiDPI(canvas, scale = 2) {
  if (!canvas.dataset.bw) {           // 首次记录 HTML 属性里的逻辑尺寸（之后 width 属性已变大）
    canvas.dataset.bw = canvas.getAttribute('width');
    canvas.dataset.bh = canvas.getAttribute('height');
  }
  const W = +canvas.dataset.bw, H = +canvas.dataset.bh;
  if (canvas.width !== W * scale) {
    canvas.width = W * scale; canvas.height = H * scale;
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  return { ctx, W, H };
}

// 点击图表 → 新窗口打开高清大图（可另存）
function openChartWindow(canvas, title) {
  const img = canvas.toDataURL('image/png');
  const w = window.open('', '_blank');
  if (!w) { showOverlay('error', '新窗口被浏览器拦截，请允许弹窗后重试'); setTimeout(hideOverlay, 2500); return; }
  w.document.write('<!DOCTYPE html><html><head><meta charset="utf-8"><title>' + title + '</title>' +
    '<style>body{margin:0;background:#faf8f3;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;gap:12px}' +
    'img{max-width:97vw;max-height:90vh;box-shadow:0 10px 34px rgba(60,40,20,.18);border-radius:10px;background:#fff;cursor:zoom-out}' +
    'p{font:13px/1.6 sans-serif;color:#8a8578;margin:0}</style></head>' +
    '<body><img src="' + img + '" title="点击另存为 PNG" ' +
    'onclick="const a=document.createElement(\'a\');a.href=this.src;a.download=\'' + title + '.png\';a.click()"/>' +
    '<p>' + title + ' · 高清图 · 点击图片可另存</p></body></html>');
  w.document.close();
}

async function loadTicker(force = false) {
  try {
    const d = await API.ticker(force);
    const items = d.items || [];
    if (!items.length) return;
    const seg = items.map(it => {
      if (it.type === 'news') {
        // 关键词徽章：优先后端 kws 数组（1~2 个实体词），兼容旧 kw/kw_url 单字段
        const kws = (Array.isArray(it.kws) && it.kws.length)
          ? it.kws
          : (it.kw ? [{ kw: it.kw, url: it.kw_url || it.url }] : []);
        const kwHtml = kws.map(k =>
          `<a class="tk-kw" href="${escHtml(k.url)}" target="_blank" rel="noopener" title="${escHtml(it.time || '')} 关键词检索">【${escHtml(k.kw)}】</a>`).join('');
        return `<span class="tk-item">` + kwHtml +
          `<span class="tk-txt"><a href="${escHtml(it.url)}" target="_blank" rel="noopener" title="阅读原文">${escHtml(it.text)}</a></span></span>`;
      }
      if (it.type === 'stock') {
        return `<span class="tk-item"><a class="tk-stock" href="${escHtml(it.url)}" target="_blank" rel="noopener">📈 ${escHtml(it.name)} ${escHtml(it.code)} ↗</a></span>`;
      }
      if (it.type === 'market') {
        const pct = parseFloat(it.pct);
        const cls = pct > 0 ? 'tk-up up' : (pct < 0 ? 'tk-down down' : '');
        return `<span class="tk-item"><span class="tk-mkt ${cls}">${escHtml(it.label)} ${escHtml(it.value)} ${pct > 0 ? '+' : ''}${escHtml(it.pct)}%</span></span>`;
      }
      if (it.type === 'cci') {
        const v = parseFloat(it.value);
        const arrow = v > 0 ? '↗' : (v < 0 ? '↘' : '→');
        // 预警（|CCI|>100）标红最高优先级；否则按多空方向红/绿
        const dirCls = it.warn ? 'tk-warn' : (v > 0 ? 'tk-up up' : (v < 0 ? 'tk-down down' : ''));
        return `<span class="tk-item"><span class="tk-mkt tk-cci ${dirCls}" ` +
          `title="CCI(20)·上证指数（${escHtml(it.date || '')}）——衡量市场多空强弱，|CCI|>100 触发预警">` +
          `📊 CCI(20)·上证 ${escHtml(String(it.value))} ${arrow} ${escHtml(it.trend)}` +
          `${it.warn ? ' ⚠ 预警' : ''}</span></span>`;
      }
      if (it.type === 'fut') {   // 伦敦金/布伦特原油：暗金色（与红绿涨跌、预警红均区分）
        const pct = parseFloat(it.pct);
        const arrow = pct > 0 ? '▲' : (pct < 0 ? '▼' : '—');
        return `<span class="tk-item"><a class="tk-fut" href="${escHtml(it.url)}" target="_blank" rel="noopener" ` +
          `title="点击查看 ${escHtml(it.label)} 行情数据">◈ ${escHtml(it.label)} ${escHtml(it.value)} ${arrow} ${escHtml(it.pct)}%</a></span>`;
      }
      if (it.type === 'fut_alert') {   // 期货异动：|主连涨跌幅|≥5%，点击跳 iwencai 筛查
        const pct = parseFloat(it.pct);
        return `<span class="tk-item"><a class="tk-fut-alert" href="${escHtml(it.url)}" target="_blank" rel="noopener" ` +
          `title="期货异动预警：主力连续涨跌幅绝对值≥5%，点击筛查相关标的">⚡ 期货异动 <b>${escHtml(it.label)}</b> ${escHtml(it.pct)}%</a></span>`;
      }
      if (it.type === 'quote') {   // 小概率彩蛋一句话
        return `<span class="tk-item"><span class="tk-quote">✦ ${escHtml(it.text)} ✦</span></span>`;
      }
      return '';
    }).join('<span class="tk-sep">◆</span>');
    const track = $('#tickerTrack');
    // 内容双份拼接实现无缝横向循环；速度随内容长度自适应，悬停暂停（CSS）
    track.innerHTML = seg + '<span class="tk-sep">◆</span>' + seg + '<span class="tk-sep">◆</span>';
    track.style.setProperty('--ticker-dur', Math.max(60, items.length * 8) + 's');
    $('#tickerBar').hidden = false;
  } catch (e) { console.warn('ticker:', e); }
}

// ========= 大周期通道（可复用模块） =========
let _cycleLast = null;

async function runCycle() {
  hideMeme('cycleMeme');
  const cfg = {
    channel_window: num('#cChanWin', 48),
    trend_window: num('#cTrendWin', 120),
    upper_q: num('#cUpperQ', 0.95),
    lower_q: num('#cLowerQ', 0.05),
    high_pos: num('#cHighPos', 80),
    low_pos: num('#cLowPos', 20),
  };
  const source = $('#cycleSource').value;
  const fy = num('#cForecastYears', 3);
  showOverlay('loading', `大周期分析中（取月K）…`);
  try {
    const data = await API.cycleRun({ source, cfg, forecast_years: fy });
    hideOverlay();
    if (data.error) { showMeme('cycleMeme', 'error', '分析失败', String(data.error).slice(0, 140)); return; }
    _cycleLast = data;
    renderCycle(data);
    const v = data.verdict || {}, te = data.trend_engine || {};
    showMeme('cycleMeme', 'success', `${v.trend || '—'} · 评分 ${te.total > 0 ? '+' : ''}${te.total ?? '—'} 🧭`,
      `数据源：${data.source_label || '—'}`);
  } catch (e) {
    hideOverlay();
    showMeme('cycleMeme', 'error', '请求失败', String(e));
  }
}

function renderCycle(data) {
  const m = data.metrics || {}, v = data.verdict || {}, te = data.trend_engine || {};
  $('#cycleNotes').textContent =
    `数据源：${data.source_label || '—'}（${data.source_rows || '—'} 个月）` +
    ((data.notes || []).length ? ' ｜ ' + data.notes.join(' ｜ ') : '');
  const tot = te.total ?? 0;
  const trendCls = tot > 0 ? 'up' : 'down';
  $('#cycleTrend').innerHTML =
    `<div class="v-big ${trendCls}">${escHtml(v.trend || '—')} <span class="v-mut">多因子评分 <b>${tot > 0 ? '+' : ''}${tot}</b>/100</span></div>` +
    `<div>对数年化 <b class="${m.annualized_pct > 0 ? 'up' : 'down'}">${m.annualized_pct > 0 ? '+' : ''}${m.annualized_pct}%</b>（近 ${v.trend_window} 个月）｜ 月线 MA12 ${m.ma12} ${m.ma_bull ? '＞' : '＜'} MA36 ${m.ma36 ?? '—'}（${m.ma_bull ? '多头' : '空头'}排列）</div>` +
    `<div class="v-mut">六法加权：RSL 25% · 均线排列 20% · Weinstein 阶段 20% · 月线 MACD 15% · ROC 动量 15% · 通道位置 5%（≥50 强势 / ±20 内震荡）</div>`;
  $('#cycleFactors').innerHTML = (te.factors || []).map(x => {
    const cls = x.score > 0.15 ? 'up' : (x.score < -0.15 ? 'down' : '');
    const w = Math.min(Math.abs(x.score) * 100, 100);
    return `<div class="tf-row"><span class="tf-name" title="${escHtml(x.desc)}">${escHtml(x.name)}</span>` +
      `<span class="tf-val">${escHtml(x.value)}</span>` +
      `<span class="tf-bar"><i class="${cls}" style="width:${w}%"></i></span>` +
      `<span class="tf-score ${cls}">${x.score > 0 ? '+' : ''}${x.score}</span>` +
      `<span class="tf-judge ${cls}">${escHtml(x.judge)}</span></div>` +
      `<div class="tf-desc">${escHtml(x.desc)}</div>`;
  }).join('');
  const posCls = m.position_pct >= v.high_th ? 'up' : (m.position_pct <= v.low_th ? 'down' : '');
  $('#cyclePos').innerHTML =
    `<div class="v-big ${posCls}">通道位置 ${m.position_pct}% · 周期${escHtml(v.zone || '—')}</div>` +
    `<div>距上轨 <b>${m.dist_upper_pct > 0 ? '+' : ''}${m.dist_upper_pct}%</b> ｜ 距下轨 <b>${m.dist_lower_pct > 0 ? '+' : ''}${m.dist_lower_pct}%</b></div>` +
    `<div class="v-mut">近 ${v.channel_window} 个月通道 · 月斜率 ${m.channel_slope} ｜ 全历史分位 ${m.history_pct}%` +
    (m.chg_12m != null ? ` ｜ 近12月 ${m.chg_12m > 0 ? '+' : ''}${m.chg_12m}%` : '') + `</div>`;
  $('#cycleAdvice').innerHTML =
    '<ul>' + (data.advice || []).map(a => `<li>${escHtml(a)}</li>`).join('') + '</ul>';
  $('#cycleForecast').innerHTML =
    `<div>预测年数：<b>${data.forecast_years || '—'} 年</b>（可在上方参数区调整 1~14 年）</div>` +
    `<div>${escHtml(data.forecast_note || '')}</div>` +
    `<div class="v-mut">图示：实线 = 历史月K与通道；虚线 = 通道斜率外推（统计口径与历史一致，轨道宽度不变）</div>`;
  $('#cycleVerdicts').hidden = false;
  drawCycleChart(data);
}

function drawCycleChart(data) {
  const rows = (data.monthly || []).filter(r => r.close != null);
  // 走势预测已按要求隐藏：不再绘制外推虚线（fc 置空后所有预测分支自然跳过，
  // min/max 也不再纳入预测轨道，图面只保留历史月K与通道）
  const fc = [];
  const canvas = $('#cycleChart');
  canvas.hidden = false;
  const { ctx, W, H } = hiDPI(canvas);   // 2x 超采样高清
  const PAD = 54;
  ctx.clearRect(0, 0, W, H);
  const N = rows.length, F = fc.length, TOT = N + F;
  if (!N) return;
  // min/max 纳入预测轨道
  let mn = Math.min(...rows.map(k => Math.min(k.low ?? k.close, k.lower ?? Infinity)));
  let mx = Math.max(...rows.map(k => Math.max(k.high ?? k.close, k.upper ?? -Infinity)));
  fc.forEach(p => {
    mn = Math.min(mn, p.lower); mx = Math.max(mx, p.upper);
  });
  if (!Number.isFinite(mn) || !Number.isFinite(mx)) { mn = 0; mx = 1; }
  const pad = (mx - mn) * 0.07 || 1; mn -= pad; mx += pad;
  const xStep = (W - PAD * 2) / TOT;
  const yMap = v => H - PAD - ((v - mn) / (mx - mn)) * (H - PAD * 2);
  const xAt = i => PAD + i * xStep + xStep / 2;
  ctx.strokeStyle = '#eeebe2'; ctx.lineWidth = 1; ctx.font = '10px sans-serif';
  for (let i = 0; i <= 4; i++) {
    const y = PAD + (H - PAD * 2) * (i / 4);
    ctx.beginPath(); ctx.moveTo(PAD, y); ctx.lineTo(W - PAD, y); ctx.stroke();
    ctx.fillStyle = '#a3a3a3';
    ctx.fillText((mx - (mx - mn) * i / 4).toFixed(0), 8, y + 3);
  }
  // 预测起点竖虚线
  const fStart = rows[N - 1];
  if (F > 0) {
    ctx.strokeStyle = '#b8860b'; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(xAt(N - 1), PAD); ctx.lineTo(xAt(N - 1), H - PAD); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#b8860b'; ctx.fillText('预测起点 →', xAt(N - 1) + 4, PAD + 10);
  }
  // 通道带填充：历史（较深）+ 预测（更浅）
  const i0 = rows.findIndex(r => r.upper != null);
  if (i0 >= 0 && N - i0 > 1) {
    ctx.beginPath();
    for (let i = i0; i < N; i++) { const x = xAt(i), y = yMap(rows[i].upper); i === i0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); }
    for (let i = N - 1; i >= i0; i--) ctx.lineTo(xAt(i), yMap(rows[i].lower));
    ctx.closePath();
    ctx.fillStyle = 'rgba(90,120,190,0.07)';
    ctx.fill();
  }
  if (F > 1) {
    ctx.beginPath();
    ctx.moveTo(xAt(N - 1), yMap(fStart.upper ?? fc[0].upper));
    fc.forEach((p, k) => ctx.lineTo(xAt(N + k), yMap(p.upper)));
    for (let k = F - 1; k >= 0; k--) ctx.lineTo(xAt(N + k), yMap(fc[k].lower));
    ctx.lineTo(xAt(N - 1), yMap(fStart.lower ?? fc[0].lower));
    ctx.closePath();
    ctx.fillStyle = 'rgba(184,134,11,0.06)';
    ctx.fill();
  }
  // 轨道线（历史实线）
  const line = (key, color, width, dash) => {
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash || []);
    let started = false;
    rows.forEach((r, i) => {
      if (r[key] == null) return;
      const x = xAt(i), y = yMap(r[key]);
      if (!started) { ctx.beginPath(); ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    });
    ctx.stroke(); ctx.setLineDash([]);
  };
  line('upper', '#c0392b', 1.4, [6, 4]);
  line('mid', '#8a8a8a', 1, [2, 3]);
  line('lower', '#2e8b57', 1.4, [6, 4]);
  // 预测轨道（虚线延伸：从最后历史点连到各预测点）
  if (F > 0) {
    const fline = (key, color, width) => {
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash([5, 5]);
      ctx.beginPath();
      ctx.moveTo(xAt(N - 1), yMap(fStart[key] ?? fc[0][key]));
      fc.forEach((p, k) => ctx.lineTo(xAt(N + k), yMap(p[key])));
      ctx.stroke(); ctx.setLineDash([]);
    };
    fline('upper', '#c0392b', 1.3);
    fline('mid', '#8a8a8a', 1);
    fline('lower', '#2e8b57', 1.3);
    // 预测终点标注
    const fe = fc[F - 1];
    ctx.fillStyle = '#b8860b'; ctx.font = 'bold 11px sans-serif';
    ctx.fillText(`外推 ${fe.date} 中轨 ${fe.mid}`, Math.min(xAt(TOT - 1) - 60, W - PAD - 130), yMap(fe.mid) - 8);
  }
  // 历史月K（涨红跌绿）
  const bw = Math.max(xStep * 0.55, 1);
  rows.forEach((r, i) => {
    if (r.open == null || r.close == null) return;
    const up = r.close >= r.open;
    ctx.strokeStyle = up ? '#d23f31' : '#1d9a6c';
    ctx.fillStyle = up ? '#d23f31' : '#1d9a6c';
    const x = xAt(i);
    if (r.high != null && r.low != null) {
      ctx.beginPath(); ctx.moveTo(x, yMap(r.high)); ctx.lineTo(x, yMap(r.low)); ctx.lineWidth = 1; ctx.stroke();
    }
    const yO = yMap(r.open), yC = yMap(r.close);
    const top = Math.min(yO, yC), hgt = Math.max(Math.abs(yC - yO), 1);
    ctx.fillRect(x - bw / 2, top, bw, hgt);
  });
  // X 轴年份标注（历史 + 预测区）
  ctx.fillStyle = '#a3a3a3'; ctx.font = '10px sans-serif';
  rows.forEach((r, i) => {
    const yr = (r.date || '').slice(0, 4);
    const prev = i ? (rows[i - 1].date || '').slice(0, 4) : '';
    if (yr && yr !== prev) ctx.fillText(yr, xAt(i) - 11, H - PAD + 14);
  });
  fc.forEach((p, k) => {
    const yr = (p.date || '').slice(0, 4);
    const prev = k ? (fc[k - 1].date || '').slice(0, 4) : fStart.date.slice(0, 4);
    if (yr && yr !== prev && k % 2 === 0) ctx.fillText(yr + '(预)', xAt(N + k) - 14, H - PAD + 14);
  });
  // 图例 + 最新点
  ctx.font = '10.5px sans-serif';
  ctx.fillStyle = '#c0392b'; ctx.fillText('— 上轨', W - PAD - 200, 16);
  ctx.fillStyle = '#8a8a8a'; ctx.fillText('— 中轨', W - PAD - 150, 16);
  ctx.fillStyle = '#2e8b57'; ctx.fillText('— 下轨', W - PAD - 100, 16);
  const lastRow = rows[N - 1];
  ctx.beginPath(); ctx.arc(xAt(N - 1), yMap(lastRow.close), 3.2, 0, Math.PI * 2);
  ctx.fillStyle = '#b86b3f'; ctx.fill();
  ctx.fillStyle = '#2a2a2a'; ctx.font = 'bold 13px sans-serif';
  ctx.fillText(`${data.source_label || ''} · ${N} 个月`, PAD, 18);
  ctx.fillStyle = '#6b6b6b'; ctx.font = '11px sans-serif';
  ctx.fillText(`最新 ${lastRow.close} (${lastRow.date}) ｜ 通道位置 ${(data.metrics || {}).position_pct}%`, PAD, H - 10);
}

function exportCycleCsv() {
  if (!_cycleLast) { showMeme('cycleMeme', 'ask', '还没有分析结果', '先点「分析大周期」'); return; }
  const rows = _cycleLast.monthly || [];
  if (!rows.length) { showMeme('cycleMeme', 'empty', '没有可导出的数据', ''); return; }
  const head = '月份,开盘,最高,最低,收盘,中轨,上轨,下轨';
  const body = rows.map(r => [r.date, r.open ?? '', r.high ?? '', r.low ?? '', r.close ?? '',
    r.mid ?? '', r.upper ?? '', r.lower ?? ''].join(','));
  const csv = '\ufeff' + [head, ...body].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '大周期_' + new Date().toISOString().slice(0, 10) + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ========= 期货视察（主连多因子打分） =========
let _futLast = null;

async function runFutures(opts = {}) {
  hideMeme('futMeme');
  const cfg = {
    weeks: Number($('#futWeeks').value) || 8,
    top_n: Number($('#futTopN').value) || 5,
    max_news: Number($('#futNewsN').value),
    min_vol: Number($('#fMinVol').value),
    win_scale: Number($('#fWinScale').value),
    adx_scale: Number($('#fAdxScale').value),
    rsi_scale: Number($('#fRsiScale').value),
    with_news: Number($('#futNewsN').value) > 0,
    refresh_symbols: !!opts.refreshSymbols,
    weights: {
      mom: Number($('#fWMom').value), er: Number($('#fWEr').value),
      adx: Number($('#fWAdx').value), ma: Number($('#fWMa').value),
      macd: Number($('#fWMacd').value), rsi: Number($('#fWRsi').value),
      oi: Number($('#fWOi').value), news: Number($('#fWNews').value),
    },
  };
  const sec = $('#futSector').value;
  if (sec) cfg.sectors = [sec];
  showOverlay('loading', `期货视察中（近 ${cfg.weeks} 周窗口）…`);
  try {
    const data = await API.futuresScan({ cfg });
    hideOverlay();
    if (data.cancelled) { showMeme('futMeme', 'ask', '已取消', '可调整参数后重新视察'); return; }
    if (data.error) { showMeme('futMeme', 'error', '视察失败', String(data.error).slice(0, 160)); return; }
    _futLast = data;
    renderFutures(data);
    const t = (data.top || [])[0], b = (data.bottom || [])[0];
    showMeme('futMeme', 'success',
      `已评估 ${data.n_scored || 0} 个主连 🛢`,
      `最强 ${t ? t.name + ' ' + (t.score > 0 ? '+' : '') + t.score : '—'} ｜ 最弱 ${b ? b.name + ' ' + b.score : '—'} ｜ 耗时 ${data.elapsed ?? '—'}s`);
  } catch (e) {
    hideOverlay();
    showMeme('futMeme', 'error', '请求失败', String(e).slice(0, 160));
  }
}

// 迷你走势（sparkline）：canvas 绘制，涨红跌绿
function drawSpark(canvas, vals) {
  if (!canvas || !vals || vals.length < 2) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 160, h = canvas.clientHeight || 34;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const span = (mx - mn) || 1;
  const xAt = i => (i / (vals.length - 1)) * (w - 2) + 1;
  const yAt = v => h - 3 - ((v - mn) / span) * (h - 6);
  const up = vals[vals.length - 1] >= vals[0];
  // 面积
  ctx.beginPath();
  ctx.moveTo(xAt(0), yAt(vals[0]));
  vals.forEach((v, i) => ctx.lineTo(xAt(i), yAt(v)));
  ctx.lineTo(xAt(vals.length - 1), h); ctx.lineTo(xAt(0), h); ctx.closePath();
  ctx.fillStyle = up ? 'rgba(210,63,49,0.12)' : 'rgba(29,154,108,0.12)';
  ctx.fill();
  // 折线
  ctx.beginPath();
  vals.forEach((v, i) => i ? ctx.lineTo(xAt(i), yAt(v)) : ctx.moveTo(xAt(i), yAt(v)));
  ctx.strokeStyle = up ? '#d23f31' : '#1d9a6c';
  ctx.lineWidth = 1.6; ctx.stroke();
}

function futCard(r, rank) {
  const cls = r.score >= 0 ? 'up' : 'down';
  const w = Math.min(Math.abs(r.score), 100);
  const fac = (r.factors || []).map(x => {
    const c = x.score > 0.15 ? 'up' : (x.score < -0.15 ? 'down' : '');
    return `<div class="tf-row"><span class="tf-name" title="${escHtml(x.desc)}">${escHtml(x.name)}</span>` +
      `<span class="tf-val">${escHtml(x.value)}</span>` +
      `<span class="tf-score ${c}">${x.score > 0 ? '+' : ''}${x.score}</span></div>`;
  }).join('');
  const news = (r.news_hits || []).slice(0, 2)
    .map(t => `<div class="fut-news-hit">📰 ${escHtml(t)}</div>`).join('');
  return `<div class="fut-card ${cls}">
    <div class="fut-card-head">
      <span class="fut-rank">#${rank}</span>
      <span class="fut-name">${escHtml(r.name)}</span>
      <span class="fut-sym">${escHtml(r.symbol)}</span>
      <span class="fut-ex">${escHtml(r.exchange_name || r.exchange || '')}</span>
      <span class="fut-score ${cls}">${r.score > 0 ? '+' : ''}${r.score}</span>
    </div>
    <div class="fut-card-sub">
      <span class="${cls}">${escHtml(r.label)}</span> ·
      窗口涨跌 <b class="${r.chg_pct >= 0 ? 'up' : 'down'}">${r.chg_pct >= 0 ? '+' : ''}${r.chg_pct}%</b> ·
      最新 <b>${r.last.toLocaleString('zh-CN')}</b>（${escHtml(r.last_date || '')}）·
      ${escHtml(r.sector_name || '')}
    </div>
    <div class="fut-bar"><i class="${cls}" style="width:${w}%"></i></div>
    <canvas class="fut-spark" width="320" height="40" data-spark="${escHtml(JSON.stringify(r.spark || []))}"></canvas>
    <div class="fut-factors">${fac}</div>
    ${news}
  </div>`;
}

function renderFutures(data) {
  const p = data.params || {};
  $('#futNotes').textContent =
    `主连 ${data.n_symbols ?? '—'} 个 · 有效评估 ${data.n_scored ?? '—'} 个 · 窗口 近 ${p.weeks} 周 · ` +
    `数据截至 ${(data.rows || [])[0] ? data.rows[0].last_date : '—'}` +
    ((data.notes || []).length ? ' ｜ ' + data.notes.join(' ｜ ') : '') +
    ((data.filtered || []).length ? ` ｜ 已过滤不活跃 ${data.filtered.length} 个` : '');

  const n = (data.top || []).length;
  $('#futTopNLabel').textContent = n;
  $('#futBotNLabel').textContent = (data.bottom || []).length;
  $('#futTopList').innerHTML = (data.top || []).map((r, i) => futCard(r, i + 1)).join('')
    || '<div class="fut-empty">无看涨品种</div>';
  $('#futBottomList').innerHTML = (data.bottom || []).map((r, i) => futCard(r, i + 1)).join('')
    || '<div class="fut-empty">无看跌品种</div>';
  $('#futBlocks').hidden = false;
  // sparkline 逐个绘制（canvas 尺寸依赖布局，需在插入 DOM 后执行）
  $$('#futBlocks .fut-spark').forEach(cv => {
    try { drawSpark(cv, JSON.parse(cv.dataset.spark || '[]')); }
    catch (e) { /* 数据异常时静默跳过 */ }
  });

  // 板块汇总
  const secs = data.sectors || [];
  if (secs.length) {
    const mx = Math.max(...secs.map(s => Math.abs(s.avg_score)), 1);
    $('#futSectors').innerHTML = secs.map(s => {
      const c = s.avg_score > 0 ? 'up' : 'down';
      return `<div class="sec-row"><span class="sec-name">${escHtml(s.sector_name)}</span>` +
        `<span class="tf-bar"><i class="${c}" style="width:${Math.abs(s.avg_score) / mx * 100}%"></i></span>` +
        `<span class="sec-score ${c}">${s.avg_score > 0 ? '+' : ''}${s.avg_score}</span>` +
        `<span class="sec-mut">${s.n} 个 · 最强 ${escHtml(s.best_name || '—')}</span></div>`;
    }).join('');
    $('#futSectorBlock').hidden = false;
  }

  // 新闻
  const news = data.news || [];
  if (news.length) {
    $('#futNewsList').innerHTML = '<ul class="fut-news">' + news.map(x => {
      const t = escHtml(x.title || '');
      const s = Number(x.sent || 0);
      // 情绪徽章：与打分「⑧ 新闻情绪」同源（都来自 _hit_sym + _sentiment），
      // 让用户能直接看出这条新闻给品种加了正票还是负票。涨=红/跌=绿（A 股口径）。
      const sent = s > 0 ? '<span class="fut-news-sent up">偏多</span>'
        : (s < 0 ? '<span class="fut-news-sent down">偏空</span>' : '');
      const title = x.url
        ? `<a class="fut-news-title" href="${escHtml(x.url)}" target="_blank" rel="noopener" title="${t}">${t}</a>`
        : `<span class="fut-news-title" title="${t}">${t}</span>`;
      return `<li><span class="fut-news-src">${escHtml(x.source || '')}</span>` +
        `<span class="fut-news-time">${escHtml((x.time || '').slice(5, 16))}</span>` +
        sent + title +
        (x.symbols && x.symbols.length
          ? `<span class="fut-news-sym">→ ${x.symbols.map(escHtml).join('、')}</span>` : '') +
        `</li>`;
    }).join('') + '</ul>';
    $('#futNewsBlock').hidden = false;
  } else {
    $('#futNewsBlock').hidden = true;
  }

  // 全部品种表（自渲染，避免 renderTable 把期货名当股票名生成同花顺链接）
  const rows = data.rows || [];
  const t = $('#futAllTable');
  t.querySelector('thead').innerHTML =
    '<tr><th>排名</th><th>名称</th><th>代码</th><th>交易所</th><th>板块</th><th>评分</th>' +
    '<th>判定</th><th>窗口涨跌</th><th>最新价</th><th>数据日期</th><th>关联新闻</th></tr>';
  t.querySelector('tbody').innerHTML = rows.map((r, i) => {
    const c = r.score >= 0 ? 'up' : 'down';
    return `<tr><td>${i + 1}</td><td>${escHtml(r.name)}</td><td>${escHtml(r.symbol)}</td>` +
      `<td>${escHtml(r.exchange_name || r.exchange || '')}</td><td>${escHtml(r.sector_name || '')}</td>` +
      `<td class="num ${c}">${r.score > 0 ? '+' : ''}${r.score}</td><td>${escHtml(r.label)}</td>` +
      `<td class="num ${r.chg_pct >= 0 ? 'up' : 'down'}">${r.chg_pct >= 0 ? '+' : ''}${r.chg_pct}%</td>` +
      `<td class="num">${r.last.toLocaleString('zh-CN')}</td><td>${escHtml(r.last_date || '')}</td>` +
      `<td>${(r.news_hits || []).length} 条</td></tr>`;
  }).join('');
  $('#futAllWrap').hidden = false;
}

// 期货板块下拉（后端 SECTORS 字典，与后端保持同源，避免前后端硬编码漂移）
async function initFuturesSectors() {
  try {
    const list = await API.futuresSectors();
    const sel = $('#futSector');
    if (!sel || !Array.isArray(list)) return;
    sel.innerHTML = '<option value="">全部板块</option>' +
      list.map(s => `<option value="${escHtml(s.key)}">${escHtml(s.label)}</option>`).join('');
  } catch (e) { console.warn('futures sectors:', e); }
}

function exportFuturesCsv() {
  if (!_futLast) { showMeme('futMeme', 'ask', '还没有视察结果', '先点「开始视察」'); return; }
  const rows = _futLast.rows || [];
  if (!rows.length) { showMeme('futMeme', 'empty', '没有可导出的数据', ''); return; }
  const head = ['排名', '名称', '代码', '交易所', '板块', '评分', '判定', '窗口涨跌%',
                '最新价', '数据日期', '日均成交(手)', '持仓量(手)', '关联新闻数'];
  const body = rows.map((r, i) => [i + 1, r.name, r.symbol, r.exchange_name || r.exchange,
    r.sector_name, r.score, r.label, r.chg_pct, r.last, r.last_date, r.avg_vol, r.oi,
    (r.news_hits || []).length].join(','));
  const csv = '\ufeff' + [head.join(','), ...body].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '期货视察_' + (_futLast.date || new Date().toISOString().slice(0, 10)) + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ========= K 线 =========
async function loadKline() {
  const code = $('#klineCode').value.trim();
  if (!code) { showMeme('klineMeme', 'ask', '请输入代码', '例如 600519 / 000001'); return; }
  showOverlay('loading', '正在加载 K 线…');
  const data = await API.kline(code, Number($('#klineDays').value));
  hideOverlay();
  if (!data.bars || data.bars.length === 0) {
    showMeme('klineMeme', 'error', '没有 K 线数据', data.error || '请检查代码');
    return;
  }
  hideMeme('klineMeme');
  drawKlineCanvas(data.bars, data.code, data.display_days);
  $('#klineLink').href = thsLink(data.code);
}
function drawKlineCanvas(bars, code, displayDays) {
  // bars 含后端多取的 60 根均线预热段；K 线只显示最后 displayDays 根，
  // MA 用全量数据计算后对齐显示窗口 → 显示区间内 MA5/10/20/60 全程可画
  const dd = Math.min(displayDays || bars.length, bars.length);
  const view = bars.slice(-dd);          // 显示窗口
  const v0 = bars.length - view.length;  // 预热段偏移
  const canvas = $('#klineChart');
  const { ctx, W, H } = hiDPI(canvas);   // 2x 超采样高清
  const PAD = 50;
  ctx.clearRect(0, 0, W, H);
  const N = view.length;
  let mn = Math.min(...view.map(k => k.low)), mx = Math.max(...view.map(k => k.high));
  const ma = n => bars.map((_, i) => i < n - 1 ? null :
    bars.slice(i - n + 1, i + 1).reduce((s, b) => s + b.close, 0) / n);
  const maArrs = [ma(5), ma(10), ma(20), ma(60)];
  maArrs.forEach(arr => arr.slice(v0).forEach(v => {
    if (v != null && Number.isFinite(v)) { mn = Math.min(mn, v); mx = Math.max(mx, v); }
  }));
  const pad = (mx - mn) * 0.06 || 1; mn -= pad; mx += pad;
  const xStep = (W - PAD * 2) / N;
  const xAt = i => PAD + i * xStep + xStep / 2;
  const yMap = v => H - PAD - ((v - mn) / (mx - mn)) * (H - PAD * 2);
  ctx.strokeStyle = '#eeebe2'; ctx.lineWidth = 1;
  for (let i = 0; i < 5; i++) {
    const y = PAD + (H - PAD * 2) * (i / 4);
    ctx.beginPath(); ctx.moveTo(PAD, y); ctx.lineTo(W - PAD, y); ctx.stroke();
    ctx.fillStyle = '#a3a3a3'; ctx.font = '10px sans-serif';
    ctx.fillText((mx - (mx - mn) * i / 4).toFixed(2), 6, y + 3);
  }
  const candleW = Math.max(xStep * 0.62, 1);
  view.forEach((k, i) => {
    const x = xAt(i);
    const up = k.close >= k.open;
    const color = up ? '#c0392b' : '#2e8b57';   // 涨红 跌绿
    ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, yMap(k.high)); ctx.lineTo(x, yMap(k.low)); ctx.stroke();
    const top = yMap(Math.max(k.open, k.close));
    const bot = yMap(Math.min(k.open, k.close));
    ctx.fillStyle = color;
    ctx.fillRect(x - candleW / 2, top, candleW, Math.max(bot - top, 1));
  });
  const maColors = ['#b86b3f', '#8a6a2f', '#4a7a8a', '#7a7a7a'];
  maArrs.forEach((arr, k) => {
    ctx.strokeStyle = maColors[k]; ctx.lineWidth = 1.4;
    let started = false;
    arr.slice(v0).forEach((v, i) => {
      if (v == null) return;
      if (!started) { ctx.beginPath(); ctx.moveTo(xAt(i), yMap(v)); started = true; }
      else ctx.lineTo(xAt(i), yMap(v));
    });
    ctx.stroke();
  });
  ctx.font = '10px sans-serif';
  ctx.fillStyle = '#b86b3f'; ctx.fillText('—MA5', W - PAD - 152, 16);
  ctx.fillStyle = '#8a6a2f'; ctx.fillText('—MA10', W - PAD - 116, 16);
  ctx.fillStyle = '#4a7a8a'; ctx.fillText('—MA20', W - PAD - 80, 16);
  ctx.fillStyle = '#7a7a7a'; ctx.fillText('—MA60', W - PAD - 44, 16);
  const last = view[view.length - 1];
  ctx.fillStyle = '#2a2a2a'; ctx.font = 'bold 13px sans-serif';
  ctx.fillText(`${code} · ${N} 个交易日`, PAD, 18);
  ctx.fillStyle = '#6b6b6b'; ctx.font = '11px sans-serif';
  ctx.fillText(`最新 ${last.close} (${last.date})`, PAD, H - 10);
}

// ========= 同步 =========
function setSyncBtn(syncing) {
  const b = $('#btnSync');
  if (!b) return;
  b.textContent = syncing ? '⏹ 停止同步' : '⬇ 同步数据';
  b.classList.toggle('btn-stop', syncing);
  b.title = syncing ? '停止当前正在进行的拉取任务' : '按当前范围增量同步最新行情';
}
async function startSync() {
  if (state.syncing) {   // 同步中 → 变成「停止」
    try { await API.syncStop(); } catch (e) { console.warn(e); }
    return;              // pollSync 识别 cancelled 后复位按钮
  }
  try {
    const withValuation = $('#syncWithValuation') ? $('#syncWithValuation').checked : true;
    const r = await API.syncStart({
      exchange: rangeVal(),
      with_valuation: withValuation,
      with_dividend: $('#syncWithDividend') ? $('#syncWithDividend').checked : true,
    });
    if (r.error || r.detail) { showOverlay('error', String(r.detail || r.error)); setTimeout(hideOverlay, 2000); return; }
    if (state.syncPoll) clearInterval(state.syncPoll);
    state.syncing = true;
    setSyncBtn(true);
    $('#syncBar').hidden = false;
    pollSync();
  } catch (e) { showOverlay('error', String(e)); setTimeout(hideOverlay, 2000); }
}
function pollSync() {
  const startTs = Date.now();
  state.syncPoll = setInterval(async () => {
    try {
      const p = await API.syncProgress();
      $('#syncFill').style.width = ((p.frac || 0) * 100).toFixed(1) + '%';
      $('#syncText').textContent = `${p.msg || ''} (${((p.frac || 0) * 100).toFixed(0)}%)`;
      const logs = p.logs || [];
      if (logs.length) {
        $('#syncLogs').innerHTML = logs.slice(-15).map(l => `<div class="log-line">${l}</div>`).join('');
        $('#syncLogs').scrollTop = $('#syncLogs').scrollHeight;
      }
      if (p.status !== 'running') {
        clearInterval(state.syncPoll); state.syncPoll = null;
        state.syncing = false;
        setSyncBtn(false);
        hideOverlay();
        if (p.status === 'cancelled') {
          showMeme('strategyMeme', 'info', '已停止同步', '未完成的部分下次点「同步数据」会继续');
          await refreshDbInfo();
        } else if (p.status === 'done' || p.status === 'idle') {
          showMeme('strategyMeme', 'success', '数据同步完成 ✨', '现在可以去筛选了');
          $('#freshTip').hidden = true;   // 数据已更新，滞后提示随之消失
          await refreshDbInfo();
        } else if (p.status === 'failed' || p.status === 'error') {
          showMeme('strategyMeme', 'error', '同步失败', p.msg || '');
        }
        setTimeout(() => $('#syncBar').hidden = true, 2500);
        return;
      }
      // 兜底：轮询超过 35 分钟仍未结束，提示（同步全市场本就较慢）
      if (Date.now() - startTs > 35 * 60 * 1000) {
        clearInterval(state.syncPoll); state.syncPoll = null;
        state.syncing = false;
        setSyncBtn(false);
        hideOverlay();
        showMeme('strategyMeme', 'error', '同步耗时过长', '可能网络较慢，可查看日志或稍后重试');
        $('#syncBar').hidden = true;
      }
    } catch (e) { console.warn(e); }
  }, 1500);
}

// ========= 状态 =========
async function refreshDbInfo() {
  try {
    const s = await API.status();
    const st = s.stats || {};
    const info = $('#dbInfo');
    info.textContent =
      `${st.stocks} 只股票 · ${(st.daily_rows / 10000).toFixed(0)} 万条日线 · 数据至 ${st.daily_max || '—'}` +
      (st.last_sync ? ` · 上次同步 ${st.last_sync}` : '');
    info.title = '数据留存于本地 data/unified_data.db，重启程序不丢失；' +
      '「⬇ 同步数据」只拉取增量（自各股最新日期起继续），不会重拉全量';
    state.sectors = s.sectors || [];
    // 恢复同步状态：刷新页面后若后端仍在同步，恢复进度条 + 停止按钮（可接管并停止）
    const syncStatus = (s.sync && s.sync.status) || 'idle';
    state.syncing = (syncStatus === 'running');
    setSyncBtn(state.syncing);
    if (state.syncing && !state.syncPoll) {
      $('#syncBar').hidden = false;
      pollSync();
    }
  } catch (e) { console.warn(e); }
}

// ========= 数据滞后提示（本地数据落后最近交易日时提示同步，一天最多一次） =========
// 判定在后端：本地 000001 最新日线日期 < 最近已完成交易日 → stale。
// 节假日/盘中由交易日历兜住（此时本地与最近完成交易日一致，不提示不误报）。
function _freshTipShownToday() {
  const today = new Date().toISOString().slice(0, 10);
  return localStorage.getItem('freshTipDate') === today;
}
function showFreshTip(f) {
  const tip = $('#freshTip');
  if (!tip) return;
  $('#freshTipText').textContent =
    `⚠ 本地数据截至 ${f.local_max || '—'}，最新交易日为 ${f.last_trade_date || '—'}，` +
    '筛选与打分可能不含最新行情';
  tip.hidden = false;
}
async function checkFreshness() {
  try {
    const f = await API.freshness();
    if (!f || !f.stale) return;          // 相符（或降级/空库）→ 不显示
    if (_freshTipShownToday()) return;   // 今天已提示过 → 不再打扰
    localStorage.setItem('freshTipDate', new Date().toISOString().slice(0, 10));
    showFreshTip(f);
  } catch (e) { console.warn(e); }
}

// ========= 初始化 =========
function goBack() {
  // 「↩ 退回」：回到上一个界面；没有历史时回主页（第一个 tab）
  const first = $$('.tab')[0];
  const home = first ? first.dataset.tab : null;
  const prev = _panelHistory.pop() || home;
  if (prev) switchTab(prev, true);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function injectBackButtons() {
  $$('.panel-head').forEach(ph => {
    if (ph.querySelector('.back-btn')) return;  // 已注入跳过
    ph.insertAdjacentHTML('beforeend',
      '<button class="back-btn" title="退回到上一个界面">↩ 退回</button>');
  });
  $$('.back-btn').forEach(b => b.addEventListener('click', goBack));
}

async function init() {
  // file:// 直开检测：双击 index.html 时所有 /api 请求会被浏览器拦截，给出明确指引
  if (location.protocol === 'file:') {
    const bar = document.createElement('div');
    bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;background:#c0392b;color:#fff;padding:10px 16px;font-size:14px;line-height:1.6;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.25);';
    bar.textContent = '⚠️ 检测到直接双击打开了本地文件。请关闭本页，双击项目根目录的 start.bat 启动服务，然后访问 http://127.0.0.1:8765/ ，所有数据功能才能正常使用。';
    (document.body || document.documentElement).appendChild(bar);
  }
  bindTabs(); bindRange();
  injectBackButtons();
  $('#btnCancelTask').addEventListener('click', cancelRunning);
  const bgBtn = $('#btnBgRun');
  if (bgBtn) bgBtn.addEventListener('click', minimizeOverlay);
  bindSimilarVolToggle();
  // 月份计算机：年 → 月（四舍五入），如 1.5 年 → 18 个月
  const btnY2M = $('#btnYearToMonth');
  if (btnY2M) btnY2M.addEventListener('click', () => {
    const out = $('#cYearOut');
    const v = parseFloat($('#cYearInput').value);
    if (!Number.isFinite(v) || v < 0) { out.textContent = '请输入有效年数'; return; }
    out.textContent = Math.round(v * 12) + ' 个月';
  });
  const csvMap = [['btnStrategyCsv','strategyTable','策略扫描'],
                  ['btnCondCsv','condTable','条件筛选'],
                  ['btnPatternCsv','patternTable','形态打分'],
                  ['btnSnapCsv','snapTable','短线快拍'],
                  ['btnAntCsv','antTable','蚂蚁扫描'],
                  ['btnAnt1000Csv','ant1000Table','长周期筛选'],
                  ['btnSimilarCsv','similarATable','相似股A类'],
                  ['btnAlertCsv','alertSignalTable','前瞻预警核心信号']];
  csvMap.forEach(([bid, tid, label]) => {
    const b = $('#' + bid);
    if (b) b.addEventListener('click', () => downloadCsv(tid, label + '_' + new Date().toISOString().slice(0,10) + '.csv'));
  });
  $('#btnRunStrategies').addEventListener('click', runStrategies);
  $('#btnRunCond').addEventListener('click', runConditions);
  $('#btnCondAll').addEventListener('click', () => $$('#condGrid input').forEach(cb => { cb.checked = true; cb.closest('.cond-item').classList.add('active'); }));
  $('#btnCondNone').addEventListener('click', () => $$('#condGrid input').forEach(cb => { cb.checked = false; cb.closest('.cond-item').classList.remove('active'); }));
  $('#btnRunPattern').addEventListener('click', runPattern);
  bindPatternSubTabs();                                    // 形态打分：经典形态 / 短线快拍 子页签
  $('#btnRunSnap').addEventListener('click', runSnap);
  $('#btnRunR9').addEventListener('click', runR9);          // 9Reverse9 · 神奇九转
  $('#btnR9ExpandAll').addEventListener('click', r9ToggleAll);
  $('#btnR9Csv').addEventListener('click', downloadR9Csv);
  $('#btnRunBeton').addEventListener('click', runBeton);    // Bet on · 长期布局
  $('#btnBetonExpandAll').addEventListener('click', boToggleAll);
  $('#btnBetonCsv').addEventListener('click', downloadBetonCsv);
  $('#btnRunAnt').addEventListener('click', runAnt);
  $('#btnRunAnt1000').addEventListener('click', runAnt1000);
  refreshAnt1000Cache();   // 长周期历史数据缓存状态（异步，失败静默）
  $('#btnRunSimilar').addEventListener('click', runSimilar);
  $('#btnRunAlert').addEventListener('click', runAlert);
  $('#btnAlertReport').addEventListener('click', exportAlertReport);
  $('#btnRunCycle').addEventListener('click', runCycle);       // 此前缺失绑定，导致大周期点击无响应
  $('#btnCycleCsv').addEventListener('click', exportCycleCsv); // 此前缺失绑定
  $('#btnRunFutures').addEventListener('click', () => runFutures());
  $('#btnFuturesCsv').addEventListener('click', exportFuturesCsv);
  $('#btnFuturesRefreshSym').addEventListener('click', () => runFutures({ refreshSymbols: true }));
  bindSecToggles();
  bindTickerRefresh();
  $('#similarVolMode').addEventListener('change', e => {
    $('#similarVolThreshWrap').hidden = (e.target.value !== 'similar');
  });
  $('#btnLoadKline').addEventListener('click', loadKline);
  $('#klineCode').addEventListener('keydown', e => { if (e.key === 'Enter') loadKline(); });
  // 三张图表：点击在新窗口打开高清大图
  [['#similarChart', '相似股走势对比'], ['#cycleChart', '大周期通道'],
   ['#klineChart', 'K线走势'], ['#ant1000Chart', '长周期周期图']]
    .forEach(([sel, name]) => {
      const cv = $(sel);
      if (cv) {
        cv.style.cursor = 'zoom-in'; cv.title = '点击在新窗口打开高清大图';
        cv.addEventListener('click', () => openChartWindow(cv, name));
      }
    });
  $('#similarCode').addEventListener('keydown', e => { if (e.key === 'Enter') runSimilar(); });
  $('#btnSync').addEventListener('click', startSync);
  initDivTiming();   // 分红时点预测面板（绑定按钮，懒加载数据）
  $('#freshTipClose').addEventListener('click', () => { $('#freshTip').hidden = true; });
  $('#freshTipSync').addEventListener('click', () => {
    $('#freshTip').hidden = true;   // 开始同步即收起提示（完成后由 done 分支保持隐藏）
    startSync();
  });
  $('#btnRefreshMeta').addEventListener('click', async () => {
    showOverlay('loading', '刷新基础信息…');
    try { await API.refreshMeta(); } catch (e) {}
    hideOverlay(); refreshDbInfo();
  });
  $('#syncToggle').addEventListener('click', () => $('#syncLogs').classList.toggle('open'));

  showOverlay('loading', '正在初始化…');
  try {
    await refreshDbInfo();
    checkFreshness();   // 异步不阻塞：数据滞后提示（一天最多一次）
    await initStrategy();
    await initConditions();
    initFuturesSectors();   // 期货板块下拉（异步，失败不影响主流程）
  } catch (e) { console.error(e); }
  hideOverlay();
  maybeShowBootstrap();   // 空库时弹出启动引导（选范围 → 同步 → 解锁模块）
  loadTicker();
  setInterval(loadTicker, 5 * 60 * 1000);   // 轮播条 5 分钟自动换一批
  bindGifFloat();
  $('#brandAvatar').src = pickMeme('welcome');
}

// 刷新字幕专用键：跳过后端 5 分钟缓存强制重建，只影响轮播条（旋转动画反馈，防连点）
function bindTickerRefresh() {
  const btn = $('#tickerRefresh');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (btn.dataset.busy === '1') return;
    btn.dataset.busy = '1'; btn.classList.add('spinning');
    try { await loadTicker(true); }
    finally { btn.dataset.busy = '0'; btn.classList.remove('spinning'); }
  });
}

// ========= 首次启动引导：空库强制选范围并同步（每次程序启动至多 1 次） =========
let _bootstrapDone = false;
async function maybeShowBootstrap() {
  if (_bootstrapDone) return;
  let stocks = -1, dailyRows = -1;
  try {
    const s = await API.status();
    stocks = (s.stats || {}).stocks || 0;
    dailyRows = (s.stats || {}).daily_rows || 0;
  } catch (e) { return; }               // 后端异常不打扰用户
  if (stocks > 0 && dailyRows > 0) return;   // 数据齐全才跳过；"有列表无日线"的半成品状态重新引导
  const modal = $('#bootstrapModal'), btn = $('#btnBootSync');
  modal.hidden = false;                 // 全屏遮罩自带"同步完成前禁用模块"效果
  // 若同步已在进行（如刷新页面），直接接管进度条
  let cur = null;
  try { cur = await API.syncProgress(); } catch (e) {}
  if (cur && cur.status === 'running') {
    $('#bootRanges').hidden = true; $('#bootProgress').hidden = false;
    $('#bootHint').textContent = '检测到数据同步正在进行中，已接管进度…';
    bootPoll();
    return;
  }
  const RANGE_NAME = { BJ: '北交所', SH: '沪市', SZ: '深市' };
  $$('#bootRanges input').forEach(r => r.addEventListener('change', () => {
    btn.disabled = false;
    btn.textContent = '同步 ' + (RANGE_NAME[r.value] || '全 A') + ' 数据';
  }));
  btn.addEventListener('click', async () => {
    const sel = $('#bootRanges input:checked');
    if (!sel) return;
    btn.disabled = true;
    $('#bootRanges').hidden = true; $('#bootProgress').hidden = false;
    $('#bootHint').textContent = '同步完成后此窗口自动关闭，期间其他模块不可运行；请勿关闭 start.bat 服务窗口（关窗会中断同步）';
    try {
      // 空库时 meta 表为空，sync_all 无股可同步——必须先拉取股票列表（两阶段）
      $('#bootProgressText').textContent = '第 1/2 步：拉取股票列表…';
      const r0 = await API.refreshMeta();
      if (r0 && (r0.error || r0.detail)) {
        $('#bootProgressText').textContent = '拉取列表失败：' + String(r0.detail || r0.error).slice(0, 120);
        btn.disabled = false; return;
      }
      bootPoll(async () => {   // 列表完成后进入第 2 步：真实同步
        $('#bootProgressText').textContent = '第 2/2 步：同步行情数据…';
        try {
          const r = await API.syncStart({ exchange: sel.value || null, with_valuation: true, with_dividend: true });
          if (r.error || r.detail) {
            $('#bootProgressText').textContent = '启动失败：' + String(r.detail || r.error).slice(0, 120);
            btn.disabled = false; return;
          }
          bootPoll(async () => {   // 同步完成 → 解锁
            _bootstrapDone = true;
            $('#bootstrapModal').hidden = true;
            await refreshDbInfo();
            showMeme('strategyMeme', 'success', '数据同步完成 ✨', '所有模块已解锁，开始筛选吧');
          }, '第 2/2 步：');
        } catch (e) {
          $('#bootProgressText').textContent = '启动失败：' + String(e).slice(0, 120);
          btn.disabled = false;
        }
      }, '第 1/2 步：');
    } catch (e) {
      $('#bootProgressText').textContent = '启动失败：' + String(e).slice(0, 120);
      btn.disabled = false;
    }
  });
}
function bootPoll(onDone, stagePrefix) {
  const startTs = Date.now();
  const t = setInterval(async () => {
    try {
      const p = await API.syncProgress();
      $('#bootFill').style.width = ((p.frac || 0) * 100).toFixed(1) + '%';
      $('#bootProgressText').textContent =
        (stagePrefix || '') + `${p.msg || ''} (${((p.frac || 0) * 100).toFixed(0)}%)`;
      if (p.status !== 'running') {
        clearInterval(t);
        if (p.status === 'done' || p.status === 'idle') {
          if (onDone) { onDone(); return; }
        } else {
          $('#bootProgressText').textContent = '同步失败：' + (p.msg || '未知错误');
          $('#bootHint').textContent = '可关闭页面后重启程序重试，或到「数据同步」页查看日志';
          $('#btnBootSync').disabled = false;
        }
        return;
      }
      if (Date.now() - startTs > 40 * 60 * 1000) {   // 兜底：40 分钟超时
        clearInterval(t);
        $('#bootProgressText').textContent = '同步耗时过长，可查看日志或稍后重试';
        $('#btnBootSync').disabled = false;
      }
    } catch (e) {
      console.warn(e);
      if (String(e).includes('连接已中断')) {
        clearInterval(t);   // 服务已停止，停止无意义轮询并明确告知
        $('#bootProgressText').textContent = String(e);
        $('#bootHint').textContent = '恢复服务后刷新本页面即可继续';
      }
    }
  }, 1500);
}

/* ========= GIF 悬浮窗：点击切换下一张动图，可拖动，永远置顶 ========= */
const GIF_LIST = ['bunny', 'loading', 'stuck', 'smile', 'shy', 'flag', 'pat', 'jump',
  ...Array.from({ length: 24 }, (_, i) => 'new' + String(i).padStart(2, '0'))];   // 2026-09 新增 24 张动图素材
function bindGifFloat() {
  const box = $('#gifFloat'), img = $('#gifFloatImg'),
        hide = $('#gifFloatHide'), restore = $('#gifFloatRestore');
  if (!box || !img) return;
  let idx = 0;
  const saved = localStorage.getItem('gifFloatHidden') === '1';
  if (saved) { box.style.display = 'none'; restore.hidden = false; }
  // 恢复上次拖动位置
  try {
    const pos = JSON.parse(localStorage.getItem('gifFloatPos') || 'null');
    if (pos && pos.left != null) {
      box.style.left = pos.left + 'px'; box.style.top = pos.top + 'px';
      box.style.right = 'auto'; box.style.bottom = 'auto';
    }
  } catch (e) {}
  GIF_LIST.slice(0, 4).forEach(n => { const im = new Image(); im.src = '/static/gifs/' + n + '.gif'; });  // 仅预热前 4 张，其余点击切换时按需加载（32 张全预热约 25MB）

  // —— 拖动（位移 >5px 判定为拖动，否则视为点击切换）——
  let dragging = false, moved = false, sx = 0, sy = 0, ox = 0, oy = 0;
  const DRAG_TH = 5;
  box.addEventListener('mousedown', e => {
    if (e.target === hide) return;   // ✕ 按钮不触发拖动
    const r = box.getBoundingClientRect();
    sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
    moved = false; dragging = true;
    e.preventDefault();
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const dx = e.clientX - sx, dy = e.clientY - sy;
    if (!moved && Math.hypot(dx, dy) > DRAG_TH) moved = true;
    if (moved) {
      const w = box.offsetWidth, h = box.offsetHeight;
      const L = Math.min(Math.max(ox + dx, 0), window.innerWidth - w);
      const T = Math.min(Math.max(oy + dy, 0), window.innerHeight - h);
      box.style.left = L + 'px'; box.style.top = T + 'px';
      box.style.right = 'auto'; box.style.bottom = 'auto';
    }
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    if (moved) {
      const r = box.getBoundingClientRect();
      localStorage.setItem('gifFloatPos', JSON.stringify({ left: r.left, top: r.top }));
    } else {
      switchGif();   // 未拖动 = 点击切换下一张
    }
    moved = false;
  });
  // 触屏拖动
  box.addEventListener('touchstart', e => {
    const t = e.touches[0];
    const r = box.getBoundingClientRect();
    sx = t.clientX; sy = t.clientY; ox = r.left; oy = r.top; moved = false; dragging = true;
  }, { passive: true });
  document.addEventListener('touchmove', e => {
    if (!dragging) return;
    const t = e.touches[0];
    const dx = t.clientX - sx, dy = t.clientY - sy;
    if (!moved && Math.hypot(dx, dy) > DRAG_TH) moved = true;
    if (moved) {
      e.preventDefault();
      const w = box.offsetWidth, h = box.offsetHeight;
      box.style.left = Math.min(Math.max(ox + dx, 0), window.innerWidth - w) + 'px';
      box.style.top = Math.min(Math.max(oy + dy, 0), window.innerHeight - h) + 'px';
      box.style.right = 'auto'; box.style.bottom = 'auto';
    }
  }, { passive: false });
  document.addEventListener('touchend', () => {
    if (!dragging) return;
    dragging = false;
    if (moved) {
      const r = box.getBoundingClientRect();
      localStorage.setItem('gifFloatPos', JSON.stringify({ left: r.left, top: r.top }));
    } else switchGif();
    moved = false;
  });

  function switchGif() {
    idx = (idx + 1) % GIF_LIST.length;
    img.src = '/static/gifs/' + GIF_LIST[idx] + '.gif';   // 预热过缓存，瞬时切换且保持动画
    box.classList.remove('pop'); void box.offsetWidth; box.classList.add('pop');
  }
  hide.addEventListener('click', e => {
    e.stopPropagation();
    box.style.display = 'none'; restore.hidden = true;
    localStorage.setItem('gifFloatHidden', '1');
  });
  restore.addEventListener('click', () => {
    box.style.display = ''; restore.hidden = true;
    localStorage.setItem('gifFloatHidden', '0');
  });
}

// ========= 分红时点预测（规则 a/b/c/d） =========
const divtState = { inited: false, week: null, stats: null };

async function initDivTiming() {
  if (divtState.inited) return;
  divtState.inited = true;
  $('#btnDivTiming').addEventListener('click', () => toggleDivTiming());
  $('#divtClose').addEventListener('click', () => { $('#divtPanel').hidden = true; });
  $('#divtToday').addEventListener('click', () => { $('#divtDate').value = ''; loadDivTimingWeek(''); });
  $('#divtDate').addEventListener('change', () => loadDivTimingWeek($('#divtDate').value));
  $('#divtFull').addEventListener('click', async () => {
    const el = $('#divtFlag');
    el.style.color = 'var(--ink-soft)';
    el.textContent = '查询中…';
    try {
      const r = await API.divtimingFullMarket();
      if (r && r.ok) {
        if (r.active) { el.style.color = 'var(--good)'; el.textContent = '● 全市场窗口开启（每年2月底，同步时全市场拉取一次）'; }
        else { el.style.color = 'var(--ink-soft)'; el.textContent = r.note || '未到「每年2月底」的全市场增量窗口'; }
      } else { el.style.color = 'var(--bad)'; el.textContent = (r && r.error) || '查询失败'; }
    } catch (e) { el.style.color = 'var(--bad)'; el.textContent = '查询失败：' + e.message; }
  });
}

async function toggleDivTiming() {
  const p = $('#divtPanel');
  if (!p.hidden) { p.hidden = true; return; }
  p.hidden = false;
  await initDivTiming();
  const r = await API.divtimingFullMarket().catch(() => null);
  renderDivtFlag(r);
  await loadDivTimingStats();
  await loadDivTimingWeek($('#divtDate').value || '');
}

function renderDivtFlag(r) {
  const el = $('#divtFlag');
  if (!r || !r.ok) { el.textContent = ''; return; }
  if (r.active) { el.textContent = '● 全市场窗口开启（2月底）'; el.style.color = 'var(--good)'; }
  else { el.textContent = ''; }
}

async function loadDivTimingStats() {
  const box = $('#divtStats');
  box.innerHTML = '<span class="bili-hint">统计中…</span>';
  try {
    const r = await API.divtimingStats();
    divtState.stats = r;
    if (!r || !r.ok) { box.innerHTML = `<span class="divt-empty">${(r && r.error) || '统计失败'}</span>`; return; }
    const wk = r.weekly_estimate || {};
    const cur = String(new Date().getMonth() + 1).padStart(2, '0');
    const kinds = r.kinds || {};
    const kindTxt = Object.entries(kinds).map(([k, v]) => `${k}${v}`).join(' / ');
    box.innerHTML = [
      ['可画像股票', r.profiles ?? 0],
      ['分红习惯', kindTxt || '-'],
      ['本月预计候选', wk[cur] != null ? wk[cur] : '-'],
      ['5月', wk['05'] ?? '-'], ['6月', wk['06'] ?? '-'], ['7月', wk['07'] ?? '-'],
    ].map(([k, v]) => `<span class="divt-chip">${k} <b>${v}</b></span>`).join('');
  } catch (e) {
    box.innerHTML = `<span class="divt-empty">统计失败：${e.message}</span>`;
  }
}

async function loadDivTimingWeek(dateStr) {
  const list = $('#divtList');
  list.innerHTML = '<div class="divt-empty">计算中…</div>';
  try {
    const r = await API.divtimingWeek(dateStr);
    if (!r || !r.ok) { list.innerHTML = `<div class="divt-empty">${(r && r.error) || '计算失败'}</div>`; return; }
    divtState.week = r;
    renderDivTimingWeeks(r.weeks || [], r.week_start);
    renderDivTimingList(r.candidates || [], r.notes || []);
    $('#divtCount').textContent = (r.candidates || []).length;
  } catch (e) {
    list.innerHTML = `<div class="divt-empty">计算失败：${e.message}</div>`;
  }
}

function renderDivTimingWeeks(weeks, cur) {
  const box = $('#divtWeeks');
  if (!weeks.length) { box.innerHTML = ''; return; }
  box.innerHTML = weeks.map(w => {
    const on = w.start === cur ? ' on' : '';
    const peak = w.peak ? ' peak' : '';
    return `<button class="divt-wk${on}${peak}" data-d="${w.start}" title="该周候选 ${w.n} 只">`
         + `${w.label}<br><span style="font-size:11px;opacity:.8">${w.n} 只</span></button>`;
  }).join('');
  $$('#divtWeeks .divt-wk').forEach(b => b.addEventListener('click', () => {
    $('#divtDate').value = b.dataset.d;
    loadDivTimingWeek(b.dataset.d);
  }));
}

function renderDivTimingList(items, notes) {
  const box = $('#divtList');
  const noteHtml = notes && notes.length
    ? `<div class="divt-empty" style="padding:0 0 8px">${notes.map(escHtml).join(' · ')}</div>` : '';
  if (!items.length) {
    box.innerHTML = noteHtml + '<div class="divt-empty">本周无候选（可点「2月底全市场」查看全市场窗口）。</div>';
    return;
  }
  box.innerHTML = noteHtml + items.map(it => {
    const score = it.score != null ? Number(it.score).toFixed(0) : '-';
    const kind = it.kind ? ` <span class="dv-kind">${escHtml(it.kind)}</span>` : '';
    const last = it.last ? `上次 ${escHtml(it.last)}` : '无历史';
    return `<div class="divt-item" title="${escHtml(it.reason || '')}">
      <span class="dv-code">${escHtml(it.code)}</span>
      <span class="dv-name">${escHtml(it.name || '')}</span>
      <span class="dv-score">${score}</span>
      <span class="dv-kind">${last}${kind}</span>
    </div>`;
  }).join('');
}

// ========= 仅供学习（B站 UP主内容挖掘） =========
const biliState = { ups: [], data: null, inited: false };

async function initBili() {
  if (biliState.inited) return;
  biliState.inited = true;
  await loadBiliUps();
  await loadBiliMeta();
  await loadBiliArchive();
  if ($('#biliArchiveClear')) {
    $('#biliArchiveClear').addEventListener('click', async () => {
      if (!confirm('确定清空全部本地存档？下次抓取将重新联网拉取全部时间范围。')) return;
      showOverlay('loading', '正在清空本地存档…');
      try {
        const r = await API.biliArchiveClear({});
        hideOverlay();
        showBiliNote(['已清空 ' + (r.removed || 0) + ' 个 UP主 的本地存档（下次将全量重抓）']);
        await loadBiliArchive();
      } catch (e) { hideOverlay(); showBiliNote([String(e)]); }
    });
  }
  $('#biliAddUp').addEventListener('click', addBiliUp);
  $('#biliUid').addEventListener('keydown', e => { if (e.key === 'Enter') addBiliUp(); });
  $('#biliRun').addEventListener('click', runBiliScan);
  $('#biliSummary').addEventListener('click', runBiliSummary);
  if ($('#biliAsrHelp')) {
    $('#biliAsrHelp').addEventListener('click', async () => {
      const box = $('#biliAsrHelpBox');
      if (!box.hidden) { box.hidden = true; return; }
      try {
        const h = await API.biliAsrHelp();
        const opts = (h.options || []).map(e =>
          `【${e.name}】\n  安装：${e.install || ''}\n  ${e.why || ''}\n  ${e.extra || ''}`).join('\n\n');
        box.textContent = (h.note ? h.note + '\n\n' : '')
          + (h.ready ? '' : '') + opts
          + (h.privacy ? `\n\n🔒 ${h.privacy}` : '')
          + (h.note_extra ? `\n🛡 ${h.note_extra}` : '');
        box.hidden = false;
      } catch (e) { showBiliNote(['获取安装指引失败：' + e.message]); }
    });
  }
  $('#biliSessionReset').addEventListener('click', async () => {
    showOverlay('loading', '正在重建 B站 会话…');
    try {
      const r = await API.biliSessionReset();
      hideOverlay();
      showBiliNote(['会话已重建：' + (r.has_session ? '已就绪' : '失败')]);
      await loadBiliMeta();
    } catch (e) { hideOverlay(); showBiliNote([String(e)]); }
  });
  $('#biliCacheClear').addEventListener('click', async () => {
    showOverlay('loading', '正在清空缓存…');
    try {
      const r = await API.biliCacheClear();
      hideOverlay();
      showBiliNote(['已清空 ' + (r.removed || 0) + ' 个缓存文件（下次抓取会重新联网）']);
      await loadBiliMeta();
    } catch (e) { hideOverlay(); showBiliNote([String(e)]); }
  });
  $('#biliCredSave').addEventListener('click', async () => {
    const v = $('#biliSessdata').value.trim();
    if (!v) { showBiliNote(['请先粘贴 SESSDATA']); return; }
    showOverlay('loading', '正在保存凭据并重建会话…');
    try {
      const r = await API.biliCredSet({ sessdata: v });
      hideOverlay();
      $('#biliSessdata').value = '';
      showBiliNote([r.msg || '已保存', '提示：字幕能否取到取决于该账号对目标视频的可见性。']);
      await loadBiliMeta();
    } catch (e) { hideOverlay(); showBiliNote([String(e)]); }
  });
  $('#biliCredClear').addEventListener('click', async () => {
    showOverlay('loading', '正在清除凭据…');
    try {
      const r = await API.biliCredClear();
      hideOverlay();
      showBiliNote([r.msg || '已清除']);
      await loadBiliMeta();
    } catch (e) { hideOverlay(); showBiliNote([String(e)]); }
  });
}

async function loadBiliMeta() {
  try {
    const m = await API.biliMeta();
    const d = m.defaults || {};
    const setIf = (id, v) => {
      const el = $(id);
      if (el && v !== undefined && v !== null) el.value = v;
    };
    setIf('#biliInterval', d.min_interval);
    setIf('#biliMaxReq', d.max_requests);
    setIf('#biliVidPages', d.video_pages);
    setIf('#biliMaxVideos', d.max_videos);
    setIf('#biliDynPages', d.dyn_pages);
    setIf('#biliMaxDyns', d.max_dyns);
    setIf('#biliCHot', d.comment_pages_hot);
    setIf('#biliCNew', d.comment_pages_new);
    setIf('#biliSubPages', d.sub_reply_pages);
    setIf('#biliCtxLen', d.ctx_min_len);
    if ($('#biliWithSub')) $('#biliWithSub').checked = !!d.with_subtitle;
    if ($('#biliWithDyn')) $('#biliWithDyn').checked = !!d.with_dynamic;
    if ($('#biliLocalVideo')) $('#biliLocalVideo').checked = !!d.video_local;
    setIf('#biliAsrMax', d.video_local_max);
    if ($('#biliAsrDir')) $('#biliAsrDir').value = d.video_local_dir || '';
    // 本地转写引擎状态（faster-whisper / funasr）
    const lv = m.local_video || {};
    const st = $('#biliAsrState');
    if (st) {
      if (lv.engine) {
        st.textContent = '✅ 可用引擎：' + lv.engine + (lv.note ? '（' + lv.note + '）' : '');
        st.style.color = 'var(--good)';
      } else {
        st.textContent = '⚠ 未检测到离线转写引擎（点右侧「查看引擎安装指引」）';
        st.style.color = 'var(--bad)';
      }
    }
    const c = m.cred || {};
    $('#biliCredState').textContent = c.has_sessdata
      ? ('已设置（' + (c.sessdata_masked || '') + '，' + (c.saved_at || '') + '）')
      : '未设置（视频字幕将无法读取）';
    const s = m.cache || {};
    $('#biliSessState').textContent =
      '会话：' + (m.has_session ? ('已就绪（' + m.age_min + ' 分钟前）') : '未建立')
      + ' · 本进程请求 ' + (m.requests_this_process || 0) + ' 次'
      + ' · 缓存 ' + (s.files || 0) + ' 个文件';
  } catch (e) { /* 元信息失败不影响主流程 */ }
}

async function loadBiliUps() {
  try {
    const r = await API.biliUps();
    biliState.ups = r.ups || [];
    renderBiliUps();
  } catch (e) {
    $('#biliUps').innerHTML = '<span class="bili-hint">读取失败：' + escHtml(String(e)) + '</span>';
  }
}

async function loadBiliArchive() {
  const box = $('#biliArchive');
  if (!box) return;
  try {
    const r = await API.biliArchive();
    renderBiliArchive(r);
  } catch (e) {
    box.innerHTML = '<span class="bili-hint">读取失败：' + escHtml(String(e)) + '</span>';
  }
}

function renderBiliArchive(a) {
  const box = $('#biliArchive');
  if (!box) return;
  const ups = (a && a.ups) || [];
  const hint = $('#biliArchiveHint');
  if (hint) {
    hint.textContent = ups.length
      ? ('共 ' + ups.length + ' 个 UP主 · 提及 ' + (a.total_mentions || 0)
         + ' 条 · 占用 ' + (a.mb || 0) + ' MB')
      : '暂无本地存档（首次抓取后自动生成）';
  }
  if (!ups.length) {
    box.innerHTML = '<span class="bili-hint">还没有本地存档；抓取一次后，这里会显示每个 UP主 的覆盖区间。</span>';
    return;
  }
  box.innerHTML = ups.map(u => {
    const v = u.video || {}, d = u.dynamic || {};
    return '<div class="bili-arch-row" data-uid="' + escHtml(u.uid) + '">'
      + '<span class="bili-arch-name">' + escHtml(u.name || ('UID' + u.uid)) + '</span>'
      + '<span class="bili-arch-cov">视频 ' + escHtml(v.covered ? (v.from + '~' + v.to) : '—')
      + ' ｜ 动态 ' + escHtml(d.covered ? (d.from + '~' + d.to) : '—') + '</span>'
      + '<span class="bili-arch-meta">提及 ' + (u.mentions || 0) + ' · 单元 ' + (u.units || 0)
      + (u.updated_at ? ' · ' + escHtml(u.updated_at) : '') + '</span>'
      + '<button type="button" class="btn" data-archdel="' + escHtml(u.uid) + '">清除</button>'
      + '</div>';
  }).join('');
  $$('#biliArchive button[data-archdel]').forEach(b => b.addEventListener('click', async () => {
    const uid = b.dataset.archdel;
    if (!confirm('清除该 UP主 的本地存档？下次抓取将重新联网拉取。')) return;
    showOverlay('loading', '正在清除…');
    try {
      await API.biliArchiveClear({ uid });
      hideOverlay();
      await loadBiliArchive();
    } catch (e) { hideOverlay(); showBiliNote([String(e)]); }
  }));
}

function renderBiliUps() {
  const box = $('#biliUps');
  if (!biliState.ups.length) {
    box.innerHTML = '<span class="bili-hint">还没有保存过 UP主。填入 UID（UP主 主页 '
      + 'space.bilibili.com/ 后面那串数字）后点「保存该 UP主」；保存后点名称旁的 ✎ 可自定义名称。</span>';
    return;
  }
  box.innerHTML = biliState.ups.map(u => {
    const dn = u.alias || u.name || ('UID' + u.uid);
    return '<label class="bili-up on" data-uid="' + escHtml(u.uid) + '">'
      + '<input type="checkbox" checked />'
      + (u.face ? '<img src="' + escHtml(u.face) + '" alt="" referrerpolicy="no-referrer" />' : '')
      + '<span class="bili-up-name' + (u.alias ? ' custom' : '') + '" title="'
      + (u.alias ? '自定义名称' : '自动读取的昵称') + '">' + escHtml(dn) + '</span>'
      + '<span class="uid">' + escHtml(u.uid) + '</span>'
      + '<button type="button" class="up-edit" title="自定义名称（留空=恢复自动昵称）"'
      + ' data-rename="' + escHtml(u.uid) + '" data-alias="' + escHtml(u.alias || '') + '">✎</button>'
      + '<button type="button" title="删除该 UP主" data-del="' + escHtml(u.uid) + '">✕</button>'
      + '</label>';
  }).join('');
  $$('#biliUps .bili-up').forEach(el => {
    const cb = el.querySelector('input');
    cb.addEventListener('change', () => el.classList.toggle('on', cb.checked));
  });
  $$('#biliUps button[data-del]').forEach(b => b.addEventListener('click', async e => {
    e.preventDefault();
    e.stopPropagation();
    showOverlay('loading', '正在删除…');
    try {
      const r = await API.biliUpRemove({ uid: b.dataset.del });
      biliState.ups = r.ups || [];
      renderBiliUps();
    } catch (err) { showBiliNote([String(err)]); }
    hideOverlay();
  }));
  $$('#biliUps button[data-rename]').forEach(b => b.addEventListener('click', e => {
    e.preventDefault();
    e.stopPropagation();
    const label = b.closest('.bili-up');
    if (!label || label.querySelector('.bili-name-edit')) return;
    const span = label.querySelector('.bili-up-name');
    const uid = b.dataset.rename;
    const cur = b.dataset.alias || '';
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.className = 'bili-name-edit';
    inp.value = cur;
    inp.placeholder = '自定义名称（留空=自动）';
    inp.maxLength = 20;
    span.replaceWith(inp);
    inp.focus();
    inp.select();
    // 文本框是「可交互内容」，label 不会把点击转发到勾选框，无需拦截
    let done = false;
    const finish = async save => {
      if (done) return;
      done = true;
      if (!save) { renderBiliUps(); return; }
      const name = inp.value.trim();
      if (name === cur) { renderBiliUps(); return; }
      showOverlay('loading', '正在保存名称…');
      try {
        const r = await API.biliUpRename({ uid, name });
        hideOverlay();
        if (!r.ok) { showBiliNote([r.msg || '保存失败']); return; }
        biliState.ups = r.ups || [];
        renderBiliUps();
        showBiliNote([r.msg || '已保存']);
      } catch (err) { hideOverlay(); showBiliNote([String(err)]); }
    };
    inp.addEventListener('keydown', ev => {
      if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
      else if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
    });
    inp.addEventListener('blur', () => finish(true));
  }));
}

function biliSelectedUids() {
  return $$('#biliUps .bili-up')
    .filter(el => el.querySelector('input').checked)
    .map(el => el.dataset.uid);
}

async function addBiliUp() {
  const uid = $('#biliUid').value.trim();
  if (!/^\d{1,12}$/.test(uid)) {
    showBiliNote(['UID 必须是纯数字（UP主 主页 URL 里 /space/ 之后的那串数字）']);
    return;
  }
  showOverlay('loading', '正在保存 UP主 ' + uid + '（会尝试读取昵称）…');
  try {
    const r = await API.biliUpAdd({ uid });
    hideOverlay();
    if (!r.ok) { showBiliNote([r.msg || '保存失败']); return; }
    biliState.ups = r.ups || [];
    renderBiliUps();
    $('#biliUid').value = '';
    const up = r.up || {};
    showBiliNote([(r.msg || '已保存') + '：' + (up.alias || up.name || uid)
      + '（点名称旁的 ✎ 可自定义名称）']);
  } catch (e) { hideOverlay(); showBiliNote([String(e)]); }
}

function biliCfg() {
  const num = (id, dv) => {
    const el = $(id);
    const v = el ? parseFloat(el.value) : NaN;
    return isFinite(v) ? v : dv;
  };
  return {
    days: num('#biliDays', 30),
    use_local: $('#biliUseLocal') ? $('#biliUseLocal').checked : true,
    min_interval: num('#biliInterval', 4.5),
    max_requests: num('#biliMaxReq', 260),
    video_pages: num('#biliVidPages', 2),
    max_videos: num('#biliMaxVideos', 40),
    dyn_pages: num('#biliDynPages', 3),
    max_dyns: num('#biliMaxDyns', 60),
    comment_pages_hot: num('#biliCHot', 2),
    comment_pages_new: num('#biliCNew', 1),
    sub_reply_pages: num('#biliSubPages', 1),
    ctx_min_len: num('#biliCtxLen', 3),
    with_subtitle: $('#biliWithSub') ? $('#biliWithSub').checked : true,
    with_dynamic: $('#biliWithDyn') ? $('#biliWithDyn').checked : true,
    video_local: $('#biliLocalVideo') ? $('#biliLocalVideo').checked : false,
    video_local_max: num('#biliAsrMax', 5),
    video_local_dir: $('#biliAsrDir') ? ($('#biliAsrDir').value || '').trim() : '',
  };
}

async function runBiliScan() {
  const uids = biliSelectedUids();
  if (!uids.length) { showBiliNote(['请先保存并勾选至少一个 UP主']); return; }
  const cfg = biliCfg();
  if (cfg.min_interval < 3) {
    showBiliNote(['为避免触发风控，请求间隔不建议低于 3 秒']);
    return;
  }
  $('#biliSummaryBox').hidden = true;
  $('#biliNotes').innerHTML = '';
  showOverlay('loading',
    '正在抓取（串行限速 ' + cfg.min_interval + 's/次，绝不并发）…最多约 '
    + (uids.length * (cfg.max_videos + cfg.max_dyns))
    + ' 条内容，可能需要数分钟到数十分钟；中途可点「停止」安全中断。',
    { cancellable: true });
  try {
    const data = await API.biliScan({ uids, cfg });
    hideOverlay();
    if (data.error) { showBiliNote([data.error]); return; }
    if (data.cancelled) { showBiliNote(['已取消（已抓到的部分已进缓存）']); return; }
    biliState.data = data;
    renderBiliResult(data);
    await loadBiliMeta();
    await loadBiliArchive();
  } catch (e) {
    hideOverlay();
    showBiliNote([_userCancelled ? '已取消' : String(e)]);
  }
}

function showBiliNote(lines) {
  $('#biliNotes').innerHTML = (lines || [])
    .map(t => '<div>' + escHtml(t) + '</div>').join('');
}

function renderBiliResult(data) {
  const s = data.stats || {};
  $('#biliStats').hidden = false;
  $('#biliStats').innerHTML =
    '<b>本次抓取</b>：UP主 ' + (s.ups || 0) + ' 个 · 近 <b>' + s.days + '</b> 天（'
    + escHtml(String(s.win_start || '')) + ' ~ ' + escHtml(String(s.win_end || '')) + '）'
    + ' · 视频 <b>' + (s.videos || 0) + '</b> 条 · 动态 <b>' + (s.dynamics || 0) + '</b> 条'
    + ' · 评论 <b>' + (s.comments_scanned || 0) + '</b> 条（UP主 自己 '
    + (s.up_comments || 0) + ' 条）'
    + '<br>命中 <b>' + (s.mentions || 0) + '</b> 处提及，覆盖 <b>' + (s.stocks || 0)
    + '</b> 只个股 · 共发起 <b>' + (s.requests || 0) + '</b>/' + (s.request_cap || 0)
    + ' 次请求 · 耗时 ' + (s.elapsed || 0) + 's'
    + (s.use_local
        ? '<br>🗃 本地存档：复用 <b>' + (s.local_mentions || 0) + '</b> 处提及 · 纯本地 UP主 <b>'
          + (s.local_ups || 0) + '</b> 个（0 网络）· 本轮实际抓取单元 <b>' + (s.fetched_units || 0) + '</b> 条'
        : '');
  const notes = (data.notes || []).slice();
  if (!s.has_sessdata) {
    notes.push('未设置 SESSDATA：B站 已对匿名关闭视频字幕接口，视频正文里的提及读不到；'
      + '可在「高级设置 → 可选登录凭据」填入自己的 SESSDATA（仅存本地）。');
  }
  showBiliNote(notes);
  renderBiliStocks(data.stocks || []);
}

function hlSnip(snip, code, name) {
  const esc = escHtml(snip);
  const cands = [name, code];
  for (let i = 0; i < cands.length; i++) {
    const t = cands[i];
    if (!t) continue;
    const j = esc.indexOf(t);
    if (j >= 0) {
      return esc.slice(0, j) + '<mark>' + t + '</mark>' + esc.slice(j + t.length);
    }
  }
  return esc;
}

function renderBiliStocks(stocks) {
  const box = $('#biliStocks');
  if (!stocks.length) {
    box.innerHTML = '<span class="bili-hint">本次范围内没有提取到股票提及。'
      + '可放宽时间范围、或确认该 UP主 是否会提到具体个股（含代码或简称）。</span>';
    return;
  }
  box.innerHTML = stocks.map((g, i) => {
    const cls = g.score >= 3 ? '' : ' low';
    const mts = (g.mentions || []).map(m =>
      '<li>'
      + '<span class="bili-mt-time">' + escHtml(m.time || '时间未知') + '</span>'
      + '<span class="bili-mt-src">' + escHtml(m.source_label || m.source || '') + '</span>'
      + '<span class="bili-mt-who' + (m.is_up ? ' up' : '') + '">'
      + escHtml(m.is_up ? 'UP主本人' : (m.author || '网友')) + '</span>'
      + (m.url
        ? '<a class="bili-mt-title" href="' + escHtml(m.url) + '" target="_blank" rel="noopener">'
          + escHtml((m.title || '').slice(0, 40) || '查看原处') + '</a>'
        : escHtml((m.title || '').slice(0, 40)))
      + (m.via ? '<span class="bili-hint">（' + escHtml(m.via) + '）</span>' : '')
      + '<div class="bili-mt-snip">' + hlSnip(m.snippet || '', g.code, g.name) + '</div>'
      + '</li>').join('');
    return '<div class="bili-stock" data-code="' + escHtml(g.code) + '">'
      + '<div class="bili-stock-head">'
      + '<span class="bili-idx">' + (i + 1) + '</span>'
      + '<span class="bili-code">' + escHtml(g.code) + '</span>'
      + '<span class="bili-name"><a href="https://stockpage.10jqka.com.cn/'
      + escHtml(g.code) + '/" target="_blank" rel="noopener">' + escHtml(g.name) + '</a></span>'
      + '<span class="bili-score' + cls + '">权重 ' + g.score + '</span>'
      + '<span class="bili-meta">UP主 ' + g.up_mentions + ' 处 / 其它用户 ' + g.other_mentions
      + ' 处 · 共 ' + g.total + ' 处 · 最近 ' + escHtml(g.last_time || '未知')
      + ' · 来源：' + escHtml((g.sources || []).join('、')) + '</span>'
      + '<button type="button" class="bili-expand">▸ 展开</button>'
      + '</div>'
      + '<ul class="bili-mentions" hidden>' + mts + '</ul>'
      + '</div>';
  }).join('');
  $$('#biliStocks .bili-expand').forEach(btn => {
    btn.addEventListener('click', () => {
      const ul = btn.closest('.bili-stock').querySelector('.bili-mentions');
      const open = !ul.hidden;
      ul.hidden = open;
      btn.textContent = open ? '▸ 展开' : '▾ 收起';
    });
  });
}

async function runBiliSummary() {
  if (!biliState.data) { showBiliNote(['请先执行一次抓取，再点一键总结']); return; }
  showOverlay('loading', '正在汇总分析（本地计算，不联网）…');
  try {
    const r = await API.biliSummary({ uids: [], cfg: {} });
    hideOverlay();
    if (r.error) { showBiliNote([r.error]); return; }
    $('#biliSummaryBox').hidden = false;
    const lines = (r.lines || []).map(t => {
      const i = t.indexOf('】');
      return '<p>' + (i > 0
        ? '<span class="tag">' + escHtml(t.slice(0, i + 1)) + '</span>' + escHtml(t.slice(i + 1))
        : escHtml(t)) + '</p>';
    }).join('');
    $('#biliSummaryBody').innerHTML = lines
      + (r.scanned_at ? '<p class="bili-hint">基于 ' + escHtml(r.scanned_at) + ' 的抓取结果</p>' : '');
    $('#biliSummaryBox').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  } catch (e) { hideOverlay(); showBiliNote([String(e)]); }
}

document.addEventListener('DOMContentLoaded', init);
