/* ==========================================================================
   数据过滤（BOLL 过滤器 + 大盘状态）· 独立前端脚本
   --------------------------------------------------------------------------
   ★零挤占原则：
     - 不修改 app.js / style.css 的任何一行；
     - 不覆盖 app.js 的任何全局函数；
     - 用「事件委托」监听页签点击来懒初始化（app.js 的 switchTab 无需知道本模块存在）；
     - 自己实现表格渲染（复用 .table 样式），不依赖 renderTable 的内部行为。
   ★诚实披露：过滤器是「择时器」不是「增强器」，页面上必须有这段文字。
   ========================================================================== */
(function () {
  'use strict';

  var INITED = false;
  var LAST_ROWS = [];
  var LAST_SOURCE = 'market';
  var FILTERS = [];

  // ---------- 小工具（局部实现，避免依赖 app.js 内部） ----------
  function $(sel) { return document.querySelector(sel); }
  function $$(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function msg(text, kind) {
    var el = $('#bfMsg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'bf-msg' + (kind ? ' ' + kind : '');
  }

  // ---------- fetch 封装：带超时，失败不炸整页 ----------
  function req(url, opts, timeout) {
    var ctl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var o = opts || {};
    if (ctl) {
      o.signal = ctl.signal;
      setTimeout(function () { ctl.abort(); }, timeout || 120000);
    }
    return fetch(url, o).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  // ======================================================================
  // 市场状态
  // ======================================================================
  function stateClass(s) { return 'state-' + (s || 'unknown'); }

  function renderMarket(m) {
    var badge = $('#bfmBadge');
    var desc = $('#bfmDesc');
    var meta = $('#bfmMeta');
    if (!badge) return;
    badge.className = 'bfm-badge ' + stateClass(m.state);
    badge.textContent = m.label || '—';
    if (desc) desc.textContent = m.desc || '';
    if (meta) {
      var parts = ['数据日 ' + (m.date || '—')];
      if (typeof m.nav === 'number') parts.push('净值 ' + m.nav.toFixed(2));
      if (m.ma20 != null) parts.push('MA20 ' + m.ma20.toFixed(2));
      if (m.ma60 != null) parts.push('MA60 ' + m.ma60.toFixed(2));
      if (m.ma120 != null) parts.push('MA120 ' + m.ma120.toFixed(2));
      if (m.coverage && m.coverage.days) parts.push('覆盖 ' + m.coverage.days + ' 日');
      meta.textContent = parts.join(' · ');
    }
    renderDist(m.dist);
    // 联动提示：当前状态与所选闸不匹配时，提前告知
    syncGateHint(m.state);
  }

  function renderDist(dist) {
    var box = $('#bfDist');
    if (!box || !dist) return;
    var order = ['strong', 'bull', 'bear', 'unknown'];
    var label = { strong: '强牛', bull: '温和上行', bear: '弱势', unknown: '数据不足' };
    var maxPct = Math.max.apply(null, order.map(function (k) {
      return (dist[k] && dist[k].pct) || 0;
    })) || 1;
    box.innerHTML = order.map(function (k) {
      var d = dist[k] || { pct: 0, n: 0 };
      var w = Math.max(2, Math.round((d.pct / maxPct) * 46));
      return '<span class="bfd-item ' + stateClass(k) + '" title="' + esc(d.n) + ' 个交易日">' +
        '<span class="bfd-bar" style="width:' + w + 'px"></span>' +
        esc(label[k]) + ' ' + d.pct + '%</span>';
    }).join('') +
      '<span class="bfd-item">历史占比（全市场等权指数代理，可回溯 30 年）</span>';
  }

  function syncGateHint(state) {
    var sel = $('#bfGate');
    if (!sel) return;
    var v = sel.value;
    var need = v === 'strong' ? ['strong'] : (v === 'bull' ? ['bull', 'strong'] : null);
    var show = $('#bfChkRow');
    if (!show) return;
    if (need && need.indexOf(state) < 0) {
      msg('⚠ 当前市场状态为「' + stateLabel(state) + '」，未达所选门槛 —— ' +
          '点「开始过滤」会被直接拦下（这是设计行为，不是故障）。', 'warn');
    } else if (msgHasGate()) {
      msg('');
    }
  }
  function msgHasGate() {
    var el = $('#bfMsg');
    return el && el.textContent && el.textContent.indexOf('未达所选门槛') >= 0;
  }
  function stateLabel(s) {
    return { strong: '强牛', bull: '温和上行', bear: '弱势', unknown: '数据不足' }[s] || s;
  }

  function loadMarket() {
    var badge = $('#bfmBadge');
    if (badge) { badge.textContent = '加载中…'; badge.className = 'bfm-badge state-unknown'; }
    return req('/api/bollfilter/market', {}, 120000)
      .then(function (j) { renderMarket(j.market || {}); return j; })
      .catch(function (e) {
        if (badge) { badge.textContent = '读取失败'; badge.className = 'bfm-badge state-unknown'; }
        if ($('#bfmDesc')) $('#bfmDesc').textContent = String(e.message || e);
      });
  }

  // ======================================================================
  // 过滤器清单
  // ======================================================================
  function loadFilters() {
    return req('/api/bollfilter/filters', {}, 30000).then(function (j) {
      FILTERS = j.filters || [];
      var row = $('#bfChkRow');
      if (!row) return;
      row.innerHTML = FILTERS.map(function (f, i) {
        return '<label class="bf-chk"><input type="checkbox" value="' + esc(f.key) + '"' +
          (i === 0 ? ' checked' : '') + ' />' + esc(f.label) + '</label>';
      }).join('') ||
        '<span style="color:var(--ink-faint);font-size:12px;">无可用过滤器</span>';
    });
  }

  function checkedFilters() {
    return $$('#bfChkRow input:checked').map(function (el) { return el.value; });
  }

  // ======================================================================
  // 股票池来源（★第 1 项：从已有模块取池 → 叠加过滤，零侵入）
  // ======================================================================
  var SOURCES = [];
  var POOL_CACHE = null;    // {source, codes}

  function loadSources() {
    return req('/api/bollfilter/sources', {}, 30000).then(function (j) {
      SOURCES = j.sources || [];
      var sel = $('#bfScope');
      if (!sel) return;
      sel.innerHTML = SOURCES.map(function (s) {
        return '<option value="' + esc(s.key) + '" title="' + esc(s.desc) + '">' +
          esc(s.label) + (s.slow ? '（较慢）' : '') + '</option>';
      }).join('');
      sel.value = 'market';
      POOL_CACHE = null;
    });
  }

  function isCustom() { return (($('#bfScope') || {}).value || 'market') === 'custom'; }

  function renderPoolHint(text, kind) {
    var el = $('#bfPoolHint');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'bf-pool-hint' + (kind ? ' ' + kind : '');
  }

  // 取池：全市场直接用 filters 已有的 codes=[] 语义（后端自己 load_universe）
  // 其它来源走 /pool 端点拿代码，然后随 run 一起提交
  function fetchPool(source, cb) {
    if (source === 'market') { POOL_CACHE = { source: source, codes: [] }; cb(null); return; }
    renderPoolHint('正在从「' + sourceLabel(source) + '」取池…（首次较慢）', 'info');
    setRunBusy(true, '取池中…');
    req('/api/bollfilter/pool',
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source: source }) },
        900000)
      .then(function (j) {
        setRunBusy(false);
        if (j.hint) { renderPoolHint('取池失败：' + j.hint, 'warn'); POOL_CACHE = null; cb(j.hint); return; }
        POOL_CACHE = { source: source, codes: j.codes || [] };
        var extra = [];
        if (j.stats) extra.push('剔 ST ' + (j.stats.st || 0) + ' / 剔次新 ' + (j.stats.new || 0));
        if (j.ref_date) extra.push('基准日 ' + j.ref_date);
        renderPoolHint('池：' + (j.label || source) + ' · ' + (POOL_CACHE.codes.length) + ' 只' +
                       (extra.length ? '（' + extra.join(' · ') + '）' : ''), 'ok');
        if (j.disclosure) renderPoolHint('⚠ ' + j.disclosure, 'warn');
        cb(null);
      })
      .catch(function (e) {
        setRunBusy(false);
        renderPoolHint('取池失败：' + (e.message || e), 'warn');
        POOL_CACHE = null; cb(String(e.message || e));
      });
  }

  function sourceLabel(key) {
    var s = SOURCES.filter(function (x) { return x.key === key; })[0];
    return s ? s.label : key;
  }

  function setRunBusy(busy, text) {
    var btn = $('#btnBfRun');
    if (!btn) return;
    btn.disabled = !!busy;
    btn.textContent = busy ? '⏳ ' + (text || '过滤中…') : '🧫 开始过滤';
  }

  // ======================================================================
  // 运行
  // ======================================================================
  function parseCodes(text) {
    return String(text || '').split(/[\s,，;；]+/).filter(Boolean);
  }

  function run() {
    var keys = checkedFilters();
    if (!keys.length) { msg('请至少勾选一个 BOLL 过滤器', 'warn'); return; }

    var source = (($('#bfScope') || {}).value) || 'market';
    var gate = (($('#bfGate') || {}).value) || 'any';
    var minBars = parseInt(($('#bfMinBars') || {}).value, 10) || 120;

    var doRun = function () {
      var payload;
      if (source === 'custom') {
        payload = { codes: parseCodes(($('#bfCodes') || {}).value),
                    filters: keys, state_filter: gate, min_bars: minBars };
        if (!payload.codes.length) { msg('请填写股票代码（逗号/空格分隔）', 'warn'); return; }
      } else {
        // 复用已取好的池（避免重复跑 beton 的 80s）
        if (!POOL_CACHE || POOL_CACHE.source !== source) {
          fetchPool(source, function (err) { if (!err) doRun(); });
          return;
        }
        payload = { codes: POOL_CACHE.codes, filters: keys,
                    state_filter: gate, min_bars: minBars };
      }

      setRunBusy(true);
      msg(payload.codes.length === 0
        ? '正在对全市场运行，可能需要十几秒…'
        : '正在过滤 ' + payload.codes.length + ' 只…', 'info');

      req('/api/bollfilter/run',
          { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload) },
          900000)
        .then(function (j) {
          if (j.market) renderMarket(j.market);
          renderResult(j, source);
        })
        .catch(function (e) {
          msg('运行失败：' + (e.message || e), 'warn');
          var t = $('#bfTable'); if (t) t.innerHTML = '';
        })
        .then(function () { setRunBusy(false); });
    };

    doRun();
  }

  function renderResult(j, source) {
    var cnt = $('#bfResCount');
    var note = $('#bfResNote');
    var table = $('#bfTable');
    LAST_ROWS = j.rows || [];
    LAST_SOURCE = source || 'market';

    if (j.skipped_state) {
      if (cnt) cnt.textContent = '已被大盘闸拦下';
      if (note) note.textContent = '';
      if (table) {
        table.innerHTML = '<div class="bf-gate-block"><b>未执行过滤</b><br />' +
          esc(j.reason || '当前市场状态未达所选门槛。') + '</div>';
      }
      if (msg) msg('');
      return;
    }

    if (j.hint) {
      if (cnt) cnt.textContent = '—';
      if (note) note.textContent = '';
      if (table) table.innerHTML = '<div class="bf-gate-block">' + esc(j.hint) + '</div>';
      return;
    }

    if (cnt) {
      cnt.textContent = '命中 ' + (j.n_out || 0) + ' / ' + (j.n_in || 0) + ' 只';
    }
    if (note) {
      var bits = [];
      if (source && source !== 'market') bits.push('池：' + sourceLabel(source));
      if (j.n_missing) bits.push('数据不足剔除 ' + j.n_missing + ' 只');
      if (j.market) bits.push('市场状态：' + stateLabel(j.market.state));
      note.textContent = bits.join(' · ');
    }
    msg(j.note || '', 'info');
    renderTable(table, LAST_ROWS);
  }

  // ---------- 自渲染表格（复用 .table 样式；代码列可点，涨跌列红绿） ----------
  function renderTable(host, rows) {
    if (!host) return;
    if (!rows || !rows.length) {
      host.innerHTML = '<div class="bf-gate-block">没有命中的股票。</div>';
      return;
    }
    var cols = ['代码', '名称', '命中', '收盘', '数据日期'];
    var hasName = rows.some(function (r) { return r['名称'] != null; });
    var useCols = hasName ? cols : ['代码', '命中', '收盘', '数据日期'];

    var html = '<table class="table"><thead><tr>' +
      useCols.map(function (c) { return '<th>' + esc(c) + '</th>'; }).join('') +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      html += '<tr>';
      useCols.forEach(function (c) {
        var v = r[c];
        if (c === '代码') {
          html += '<td><a href="#" class="bf-code" data-code="' + esc(v) + '">' +
            esc(v) + '</a></td>';
        } else if (c === '收盘') {
          html += '<td>' + (typeof v === 'number' ? v.toFixed(2) : esc(v)) + '</td>';
        } else {
          html += '<td>' + esc(v) + '</td>';
        }
      });
      html += '</tr>';
    });
    html += '</tbody></table>';
    host.innerHTML = html;

    // 代码列点击 → 跳「行情查看」页签并填入代码
    // ★不能用 window.switchTab：app.js 里 switchTab 是**普通函数声明**，
    //   并未挂到 window 上（grep 确认过），window.switchTab 永远是 undefined。
    //   改用「模拟点击对应页签按钮」+ 填输入框 + 点加载按钮 —— 全程只操作真实 DOM，
    //   既不依赖 app.js 暴露任何东西，也不会与它的内部实现耦合。
    Array.prototype.forEach.call(host.querySelectorAll('.bf-code'), function (a) {
      a.addEventListener('click', function (ev) {
        ev.preventDefault();
        var code = a.getAttribute('data-code');
        var kInp = document.querySelector('#klineCode');
        if (kInp) { kInp.value = code; }
        var kTab = document.querySelector('.tab[data-tab="kline"]');
        if (kTab) {
          if (typeof kTab.click === 'function') kTab.click();
          else if (kTab._fire) kTab._fire('click', { target: kTab });
        }
        var kBtn = document.querySelector('#btnLoadKline');
        if (kBtn) {
          if (typeof kBtn.click === 'function') kBtn.click();
          else if (kBtn._fire) kBtn._fire('click', { target: kBtn });
        }
      });
    });
  }

  // ======================================================================
  // 导出 CSV
  // ======================================================================
  function exportCsv() {
    if (!LAST_ROWS.length) { msg('暂无可导出的结果', 'warn'); return; }
    var cols = Object.keys(LAST_ROWS[0]).filter(function (k) { return k !== 'filter_keys'; });
    var lines = [cols.join(',')];
    LAST_ROWS.forEach(function (r) {
      lines.push(cols.map(function (c) {
        var v = r[c]; if (v == null) v = '';
        v = String(v);
        return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
      }).join(','));
    });
    var blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'bollfilter_' + new Date().toISOString().slice(0, 10) + '.csv';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  // ======================================================================
  // 初始化（懒加载：首次切到该页签才跑）
  // ======================================================================
  function bindControls() {
    var runBtn = $('#btnBfRun');
    if (runBtn) runBtn.addEventListener('click', run);
    var exp = $('#btnBfExport');
    if (exp) exp.addEventListener('click', exportCsv);
    var rf = $('#bfmRefresh');
    if (rf) rf.addEventListener('click', loadMarket);
    var scope = $('#bfScope');
    if (scope) {
      scope.addEventListener('change', function () {
        var w = $('#bfCodesWrap');
        if (w) w.hidden = scope.value !== 'custom';
        POOL_CACHE = null;
        if (scope.value === 'custom') {
          renderPoolHint('手工输入代码；逗号 / 空格 / 换行分隔。', 'info');
        } else if (scope.value === 'market') {
          renderPoolHint('全市场口径（沿用条件筛选的股票池，剔 ST / 次新）。', 'info');
        } else {
          renderPoolHint('点「开始过滤」时才会去取池（该来源较慢，首次约需 1 分钟）。', 'info');
        }
      });
    }
    var gate = $('#bfGate');
    if (gate) {
      gate.addEventListener('change', function () {
        req('/api/bollfilter/market', {}, 120000)
          .then(function (j) { syncGateHint((j.market || {}).state); })
          .catch(function () {});
      });
    }
  }

  function init() {
    if (INITED) { loadMarket(); return; }
    INITED = true;
    bindControls();
    loadFilters();
    loadMarket();
    loadSources().then(function () {
      renderPoolHint('全市场口径（沿用条件筛选的股票池，剔 ST / 次新）。', 'info');
    });
  }

  // ★事件委托：监听页签按钮点击（app.js 的 switchTab 完全无需知道本模块）
  function hookTab() {
    document.addEventListener('click', function (ev) {
      var el = ev.target;
      while (el && el !== document) {
        if (el.classList && el.classList.contains('tab') &&
            el.getAttribute('data-tab') === 'bfilter') {
          init();
          return;
        }
        el = el.parentNode;
      }
    }, true);
  }

  // 供无头 DOM 探针直接驱动（不依赖用户点击）
  window.__bfInit = init;
  window.__bfRun = run;
  window.__bfExport = exportCsv;
  window.__bfState = function () {
    return { inited: INITED, rows: LAST_ROWS.length, filters: FILTERS.length };
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', hookTab);
  } else {
    hookTab();
  }
})();
