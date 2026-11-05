/* ==========================================================================
   盯盘（Watchtower）· 独立前端脚本
   --------------------------------------------------------------------------
   ★零挤占原则（同 bfilter.js）：
     - 不修改 app.js / style.css 任何一行；
     - 不覆盖 app.js 的任何全局函数；
     - 用「事件委托」监听页签点击来懒初始化，app.js 的 switchTab 无需知道本模块存在；
     - 自己实现表格/卡片渲染，不依赖 renderTable 的内部行为。

   ★为什么不做「综合评分」：
     把分时/席位/资金/ETF/技术面/信号压成一个总分，需要各维度的实证收益权重。
     本项目没有任何支持这些权重的检验（Bet on 的分数对未来收益 IC≈0 就是教训），
     所以这里只给分维度事实 + 规则化信号，绝不合成一个数。

   ★性能约定：
     - 主请求 /api/monitor/run 只返回「非分时」数据（每只票约 1 次网络往返）；
     - 分时点（241~1205 个）在卡片展开时按需拉 /api/monitor/intraday；
     - 自动刷新只调 /api/monitor/quote（一次腾讯批量快照，0.2s）。
   ========================================================================== */
(function () {
  'use strict';

  var INITED = false;
  var META = null;
  var LAST = null;          // 上一次 /run 的完整响应
  var GROUPS = [];          // 已保存的盯盘组
  var OPEN = {};            // {idx: true} 展开状态（刷新后保持）
  var DETAIL = {};          // {idx: true} 详细面板是否已渲染
  var INTRADAY = {};        // {code|days: 数据} 分时缓存
  var TIMER = null;         // 自动刷新定时器
  var BUSY = false;

  // ---------- 小工具（局部实现，不依赖 app.js 内部） ----------
  function $(sel) { return document.querySelector(sel); }
  function $$(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

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

  // 数值格式化：null/NaN 一律显示 —
  function n(v, nd) {
    if (v === null || v === undefined || v === '' || (typeof v === 'number' && !isFinite(v))) return '—';
    var x = Number(v);
    if (!isFinite(x)) return '—';
    return x.toLocaleString('zh-CN', { maximumFractionDigits: nd === undefined ? 2 : nd,
                                       minimumFractionDigits: 0 });
  }
  function pct(v, nd) {
    if (v === null || v === undefined || !isFinite(Number(v))) return '—';
    var x = Number(v);
    return (x > 0 ? '+' : '') + x.toFixed(nd === undefined ? 2 : nd) + '%';
  }
  // 金额（元）→ 人类可读
  function money(v) {
    if (v === null || v === undefined || !isFinite(Number(v))) return '—';
    var x = Number(v), a = Math.abs(x);
    if (a >= 1e8) return (x / 1e8).toFixed(2) + '亿';
    if (a >= 1e4) return (x / 1e4).toFixed(0) + '万';
    return x.toFixed(0);
  }
  function trendCls(v) {
    var x = Number(v);
    if (!isFinite(x) || x === 0) return '';
    return x > 0 ? 'mt-up' : 'mt-down';
  }
  function dirCls(d) { return d === '多' ? 'mt-up' : (d === '空' ? 'mt-down' : 'mt-neu'); }
  function levelCls(l) { return l === '强' ? 'lv-strong' : (l === '中' ? 'lv-mid' : 'lv-weak'); }

  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }

  function thsLink(code) { return 'https://stockpage.10jqka.com.cn/' + code + '/'; }

  // ---------- 初始化（事件委托：点击页签才初始化，不拖慢启动） ----------
  function boot() {
    if (INITED) return;
    var tab = $('.tab[data-tab="monitor"]');
    if (!tab) return;
    INITED = true;
    bindUI();
    loadMeta();
    loadGroups();
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    while (t && t !== document) {
      if (t.classList && t.classList.contains('tab') && t.getAttribute('data-tab') === 'monitor') {
        boot();
        return;
      }
      t = t.parentNode;
    }
  }, true);

  function bindUI() {
    var run = $('#btnMtRun');
    if (run) run.addEventListener('click', function () { doRun(); });
    var exp = $('#btnMtExpand');
    if (exp) exp.addEventListener('click', toggleAll);
    var csv = $('#btnMtCsv');
    if (csv) csv.addEventListener('click', exportCsv);
    var g = $('#btnMtLoadGroup');
    if (g) g.addEventListener('click', loadSelectedGroup);
    var s = $('#btnMtSaveGroup');
    if (s) s.addEventListener('click', saveGroup);
    var d = $('#btnMtDelGroup');
    if (d) d.addEventListener('click', deleteGroup);
    var auto = $('#mtAutoRefresh');
    if (auto) auto.addEventListener('change', setupAutoRefresh);
    var codes = $('#mtCodes');
    if (codes) {
      codes.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doRun(); }
      });
    }
    // 切主题时重画分时（注册到全局重绘表；kline.js 也用同一机制）
    window.__themeRedraw = window.__themeRedraw || [];
    window.__themeRedraw.push(function () {
      Object.keys(INTRADAY).forEach(function (k) {
        var cv = document.querySelector('canvas[data-mt-key="' + k + '"]');
        var d = INTRADAY[k];
        if (cv && d && d.ok) { try { drawIntraday(cv, d); } catch (e) { /* 忽略 */ } }
      });
    });
  }

  // ---------- 元信息 / 盯盘组 ----------
  function loadMeta() {
    req('/api/monitor/meta', {}, 20000).then(function (m) {
      META = m;
      var box = $('#mtRuleBox'), body = $('#mtRuleBody');
      if (!box || !body) return;
      var rows = (m.seat_rules || []).map(function (r) {
        return '<tr><td><b>' + esc(r.cat) + '</b></td><td>' +
          r.keywords.map(function (k) { return '<code>' + esc(k) + '</code>'; }).join(' ') +
          '</td><td>' + esc(r.why) + '</td></tr>';
      }).join('');
      body.innerHTML =
        '<table class="mt-tbl"><thead><tr><th>类别</th><th>命中关键词</th><th>判定含义</th></tr></thead>' +
        '<tbody>' + rows +
        '<tr><td><b>游资</b></td><td><code>' + esc('知名营业部名单（' + (m.hot_money_count || 0) + ' 家）') +
        '</code></td><td>' + esc(m.hot_money_note || '') + '</td></tr>' +
        '<tr><td><b>其他营业部</b></td><td>以上均未命中</td>' +
        '<td>非机构专用、非沪深股通、非国家队、非拉萨系、未在游资名单内</td></tr>' +
        '</tbody></table>' +
        '<p class="mtd-sub">报告期取值：' + (m.report_dates || []).join(' / ') + '</p>' +
        '<p class="mtd-sub">' + esc(m.disclaimer || '') + '</p>';
      box.hidden = false;
    }).catch(function () { /* 元信息失败不影响主流程 */ });
  }

  function loadGroups() {
    req('/api/monitor/watchlist', {}, 15000).then(function (r) {
      GROUPS = (r && r.groups) || [];
      renderGroups();
    }).catch(function () { /* 静默 */ });
  }

  function renderGroups() {
    var sel = $('#mtGroup');
    if (!sel) return;
    var cur = sel.value;
    sel.innerHTML = '<option value="">— 未保存的组 —</option>' +
      GROUPS.map(function (g, i) {
        return '<option value="' + i + '">' + esc(g.name) + '（' + (g.codes || []).length + ' 只）</option>';
      }).join('');
    if (cur) sel.value = cur;
    // 只有一个组时自动载入，省一次点击
    if (GROUPS.length === 1 && !$('#mtCodes').value.trim()) {
      $('#mtCodes').value = (GROUPS[0].codes || []).join(', ');
    }
  }

  function loadSelectedGroup() {
    var sel = $('#mtGroup');
    var idx = sel.value;
    if (idx === '') { $('#mtHint').textContent = '先在下拉里选一个组'; return; }
    var g = GROUPS[Number(idx)];
    if (!g) return;
    $('#mtCodes').value = (g.codes || []).join(', ');
    $('#mtHint').textContent = '已载入「' + g.name + '」';
  }

  function saveGroup() {
    var codes = $('#mtCodes').value.trim();
    if (!codes) { $('#mtHint').textContent = '代码为空，无法保存'; return; }
    var name = prompt('给这个盯盘组起个名字（同名会覆盖）：', '我的盯盘组');
    if (!name) return;
    req('/api/monitor/watchlist', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ name: name, codes: codes })
    }, 20000).then(function (r) {
      if (r && r.ok) {
        GROUPS = r.groups || [];
        renderGroups();
        $('#mtHint').textContent = '已保存「' + name + '」';
      } else {
        $('#mtHint').textContent = (r && r.err) || '保存失败';
      }
    }).catch(function (e) { $('#mtHint').textContent = '保存失败：' + e.message; });
  }

  function deleteGroup() {
    var sel = $('#mtGroup');
    if (sel.value === '') { $('#mtHint').textContent = '先选一个要删除的组'; return; }
    var g = GROUPS[Number(sel.value)];
    if (!g) return;
    if (!confirm('删除盯盘组「' + g.name + '」？')) return;
    req('/api/monitor/watchlist/delete', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ name: g.name })
    }, 20000).then(function (r) {
      if (r && r.ok) {
        GROUPS = r.groups || [];
        renderGroups();
        $('#mtHint').textContent = '已删除';
      } else {
        $('#mtHint').textContent = (r && r.err) || '删除失败';
      }
    }).catch(function (e) { $('#mtHint').textContent = '删除失败：' + e.message; });
  }

  // ---------- 主请求 ----------
  function doRun() {
    if (BUSY) return;
    var codes = ($('#mtCodes').value || '').trim();
    if (!codes) {
      $('#mtHint').textContent = '请先填股票代码（如 600519,300750）';
      return;
    }
    BUSY = true;
    $('#mtHint').textContent = '拉取中…（每只约 1 秒）';
    $('#mtSummary').innerHTML = '';
    hideMeme();
    if (typeof showOverlay === 'function') showOverlay('loading', '盯盘中…');
    var body = {
      codes: codes,
      cfg: {
        intraday_days: Number($('#mtIntradayDays').value) || 1,
        seat_lookback: Number($('#mtSeatLookback').value) || 120,
        node_tolerance: Number($('#mtTol').value) || 1.0
      }
    };
    req('/api/monitor/run', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body)
    }, 300000).then(function (d) {
      BUSY = false;
      if (typeof hideOverlay === 'function') hideOverlay();
      if (!d || d.ok === false) {
        $('#mtHint').textContent = (d && d.err) || '盯盘失败';
        showMeme((d && d.err) || '盯盘失败', 'error');
        return;
      }
      LAST = d;
      INTRADAY = {};
      DETAIL = {};
      OPEN = {};
      renderPhase(d.phase);
      renderSummary(d);
      renderList(d);
      var n = (d.rows || []).length;
      var ok = (d.rows || []).filter(function (r) { return !r.error; }).length;
      $('#mtHint').textContent = '完成：' + ok + '/' + n + ' 只 · ' +
        ((d.phase && d.phase.label) || '') + (d.asof ? ' · ' + fmtQuoteTime(d.asof) : '');
      if (d.bad_codes && d.bad_codes.length) {
        $('#mtHint').textContent += ' · 忽略：' + d.bad_codes.join('、');
      }
      showMeme('已盯上 ' + n + ' 只票', 'success');
      // 只有一只票时自动展开，省一次点击
      if (n === 1) toggleCard(0, true);
    }).catch(function (e) {
      BUSY = false;
      if (typeof hideOverlay === 'function') hideOverlay();
      $('#mtHint').textContent = '请求失败：' + e.message;
      showMeme('请求失败：' + e.message, 'error');
    });
  }

  function fmtQuoteTime(t) {
    // 腾讯时间格式 20261023161458
    var s = String(t || '');
    if (s.length >= 14) {
      return s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6, 8) + ' ' +
             s.slice(8, 10) + ':' + s.slice(10, 12);
    }
    return s;
  }

  function showMeme(text, kind) {
    var box = $('#mtMeme');
    if (!box) return;
    box.hidden = false;
    box.className = 'result-meme ' + kind;
    var pool = (typeof MEMES !== 'undefined' && MEMES[kind]) ? MEMES[kind] : null;
    var img = box.querySelector('img');
    if (img && pool && pool.length) {
      img.src = '/static/' + pool[Math.floor(Math.random() * pool.length)];
    }
    box.querySelector('.meme-big').textContent = text;
    box.querySelector('.meme-sub').textContent =
      kind === 'success' ? '点卡片右侧「展开」看完整盘面；盘中的话可开自动刷新' : '';
  }
  function hideMeme() { var b = $('#mtMeme'); if (b) b.hidden = true; }

  // ---------- 渲染：盘面状态 + 组概况 ----------
  function renderPhase(ph) {
    var box = $('#mtPhase');
    if (!box || !ph) return;
    box.hidden = false;
    var cls = ph.is_trading ? 'trading' : 'closed';
    box.innerHTML =
      '<span class="mtp-dot ' + cls + '"></span>' +
      '<span class="mtp-label">' + esc(ph.label || '—') + '</span>' +
      '<span class="mtp-note">' + (ph.is_trading
        ? '盘中数据实时更新；自动刷新可开 15/30/60 秒'
        : '非交易时段，行情为最近一次收盘定格') + '</span>';
  }

  function renderSummary(d) {
    var s = d.summary || {};
    var box = $('#mtSummary');
    if (!box) return;
    if (!s.n) { box.innerHTML = ''; return; }
    var flowIn = (s.flow_in || []).map(function (x) {
      return esc(x.name) + ' <span class="mt-up">+' + money(x.main5) + '</span>';
    }).join('　');
    var flowOut = (s.flow_out || []).map(function (x) {
      return esc(x.name) + ' <span class="mt-down">' + money(x.main5) + '</span>';
    }).join('　');
    var strong = (s.strong_signals || []).slice(0, 7).map(function (x) {
      // 以「信号名」为主体、股票名为辅注 —— 只显示股票名会出现 6 个同名徽章（实测踩到）
      return '<span class="mt-sig-dot ' + dirCls(x.dir) + '">' + esc(x.signal) +
        '<em>' + esc(x.name || x.code) + '</em></span>';
    }).join('');
    box.innerHTML =
      '<div class="mts-grid">' +
        '<div class="mts-cell"><em>组内</em><b>' + s.n_ok + '/' + s.n + '</b>' +
          '<span class="mts-sub">涨 ' + (s.up || 0) + ' · 跌 ' + (s.down || 0) +
          ' · 平 ' + (s.flat || 0) + '</span></div>' +
        '<div class="mts-cell"><em>平均涨跌</em><b class="' + trendCls(s.avg_pct) + '">' +
          pct(s.avg_pct) + '</b>' +
          '<span class="mts-sub">最强 ' + esc((s.top_gain || {}).name || '—') + ' ' +
          pct((s.top_gain || {}).pct) + '</span></div>' +
        '<div class="mts-cell"><em>5 日主力净流入</em>' +
          '<span class="mts-line">' + (flowIn || '—') + '</span></div>' +
        '<div class="mts-cell"><em>5 日主力净流出</em>' +
          '<span class="mts-line">' + (flowOut || '—') + '</span></div>' +
        '<div class="mts-cell"><em>信号分布</em><b class="mts-line">' +
          '<span class="mt-up">多 ' + ((s.signal_count || {})['多'] || 0) + '</span> · ' +
          '<span class="mt-down">空 ' + ((s.signal_count || {})['空'] || 0) + '</span> · ' +
          '<span class="mt-neu">中性 ' + ((s.signal_count || {})['中性'] || 0) + '</span></b></div>' +
      '</div>' +
      (strong ? '<div class="mts-strong"><em>强信号</em>' + strong + '</div>' : '');
  }

  // ---------- 渲染：卡片列表 ----------
  function renderList(d) {
    var box = $('#mtList');
    if (!box) return;
    var rows = d.rows || [];
    if (!rows.length) {
      box.innerHTML = '<div class="mt-empty">没有可展示的股票</div>';
      return;
    }
    box.innerHTML = rows.map(function (r, i) { return cardHtml(r, i); }).join('');
    // 事件绑定（事件委托到容器，避免几百个 listener）
    box.onclick = function (e) {
      var el = e.target;
      while (el && el !== box) {
        if (el.classList && el.classList.contains('mt-toggle')) {
          var idx = Number(el.getAttribute('data-idx'));
          toggleCard(idx);
          return;
        }
        if (el.classList && el.classList.contains('mt-code-link')) {
          e.preventDefault();
          if (typeof switchTab === 'function') switchTab('kline');
          var kc = $('#klineCode');
          if (kc) kc.value = el.getAttribute('data-code');
          if (typeof loadKline === 'function') loadKline();
          return;
        }
        el = el.parentNode;
      }
    };
  }

  function cardHtml(r, i) {
    var q = r.quote || {};
    var tech = r.tech || {};
    var fm = r.fund_mix || {};
    var tick = fm.tick || {};
    var d5 = tick.d5 || {};
    var sigs = r.signals || [];
    var strong = sigs.filter(function (s) { return s.level === '强'; }).slice(0, 3);
    var nodes = r.nodes || [];
    var nearest = nodes[0];
    var pctv = q.pct;
    if (pctv === null || pctv === undefined) pctv = 0;

    if (r.error) {
      return '<div class="mt-card mt-card-err" data-idx="' + i + '">' +
        '<div class="mtc-main"><div class="mtc-id">' +
        '<span class="mtc-name">' + esc(r.name || r.code) + '</span>' +
        '<span class="mtc-code">' + esc(r.code) + '</span></div>' +
        '<div class="mtc-err">' + esc(r.error) + '</div></div></div>';
    }

    var tags = [];
    if (r.sector) tags.push('<span class="mt-tag">' + esc(r.sector) + '</span>');
    if (tech.ma_arrange && tech.ma_arrange !== '数据不足') {
      tags.push('<span class="mt-tag ' + (tech.ma_arrange === '多头排列' ? 'tag-up' :
        (tech.ma_arrange === '空头排列' ? 'tag-down' : '')) + '">' + esc(tech.ma_arrange) + '</span>');
    }
    if (!r.seats || !(r.seats.records || []).length) {
      tags.push('<span class="mt-tag tag-dim" title="近期未上龙虎榜，无席位数据">无席位</span>');
    } else {
      var lt = r.seats.latest || {};
      tags.push('<span class="mt-tag tag-seat" title="最近一次上榜日">席位 ' +
        esc((lt.date || '').slice(5)) + '</span>');
    }
    if (r.etf && r.etf.ok) {
      tags.push('<span class="mt-tag tag-etf" title="持有该股的 ETF 数量（季报口径）">ETF×' +
        (r.etf.n_etf || 0) + '</span>');
    }

    var sigBadges = strong.map(function (s) {
      return '<span class="mt-sig ' + dirCls(s.dir) + '">' + esc(s.name) + '</span>';
    }).join('');

    return '' +
    '<div class="mt-card' + (OPEN[i] ? ' open' : '') + '" data-idx="' + i + '" data-code="' + esc(r.code) + '">' +
      '<div class="mtc-main">' +
        '<div class="mtc-id">' +
          '<a href="#" class="mtc-name mt-code-link" data-code="' + esc(r.code) +
            '" title="点名称看站内 K 线">' + esc(r.name || r.code) + '</a>' +
          '<span class="mtc-code">' + esc(r.code) + '</span>' +
          '<a class="mtc-ext" href="' + esc(thsLink(r.code)) +
            '" target="_blank" rel="noopener noreferrer" title="同花顺个股页">↗</a>' +
          '<span class="mtc-tags">' + tags.join('') + '</span>' +
        '</div>' +
        '<div class="mtc-price">' +
          '<b class="' + trendCls(pctv) + '">' + n(q.price, 2) + '</b>' +
          '<span class="' + trendCls(pctv) + '">' + pct(q.pct) + '</span>' +
        '</div>' +
        '<div class="mtc-metrics">' +
          metric('量比', n(q.vol_ratio, 2), q.vol_ratio >= 2 ? 'warn' : '') +
          metric('换手', q.turnover == null ? '—' : n(q.turnover, 2) + '%', '') +
          metric('5日主力', money(d5.main), trendCls(d5.main)) +
          metric('20日超额', pct((tech.rel || {}).excess20), trendCls((tech.rel || {}).excess20)) +
        '</div>' +
        '<div class="mtc-sigs">' + (sigBadges || '<span class="mt-sig mt-neu">无强信号</span>') + '</div>' +
        '<div class="mtc-node">' +
          (nearest
            ? '<em>最近节点</em><b>' + esc(nearest.name) + '</b>' +
              '<span class="' + (nearest.state === '贴近' ? 'mt-warn' : 'mt-neu') + '">' +
              (nearest.dist_pct > 0 ? '+' : '') + n(nearest.dist_pct, 2) + '%</span>' +
              '<i class="node-state s-' + esc(nearest.state) + '">' + esc(nearest.state) + '</i>'
            : '<em>最近节点</em><span class="mt-neu">—</span>') +
        '</div>' +
        '<button class="mt-toggle" data-idx="' + i + '">' +
          (OPEN[i] ? '收起 ▲' : '展开 ▼') + '</button>' +
      '</div>' +
      '<div class="mt-detail" data-idx="' + i + '"' + (OPEN[i] ? '' : ' hidden') + '>' +
        (DETAIL[i] ? detailHtml(r) : '') +
      '</div>' +
    '</div>';
  }

  function metric(label, val, cls) {
    return '<div class="mtm"><em>' + esc(label) + '</em><b class="' + (cls || '') + '">' +
      esc(val) + '</b></div>';
  }

  // ---------- 渲染：详细面板（懒渲染） ----------
  function toggleCard(idx, force) {
    var card = document.querySelector('.mt-card[data-idx="' + idx + '"]');
    if (!card || !LAST) return;
    var r = LAST.rows[idx];
    if (!r) return;
    var want = (force === undefined) ? !OPEN[idx] : !!force;
    var det = card.querySelector('.mt-detail');
    var btn = card.querySelector('.mt-toggle');
    OPEN[idx] = want;
    card.classList.toggle('open', want);
    if (btn) btn.textContent = want ? '收起 ▲' : '展开 ▼';
    if (!det) return;
    if (want) {
      if (!DETAIL[idx]) {
        det.innerHTML = detailHtml(r);
        DETAIL[idx] = true;
      }
      det.hidden = false;
      // 分时懒加载（只拉一次，缓存在 INTRADAY）
      var cv = det.querySelector('canvas[data-mt-code]');
      if (cv) ensureIntraday(r.code, cv);
    } else {
      det.hidden = true;
    }
  }

  function toggleAll() {
    if (!LAST) { $('#mtHint').textContent = '先跑一次盯盘'; return; }
    var rows = LAST.rows || [];
    var anyClosed = rows.some(function (_, i) { return !OPEN[i]; });
    rows.forEach(function (_, i) { toggleCard(i, anyClosed); });
    var btn = $('#btnMtExpand');
    if (btn) btn.textContent = anyClosed ? '⤢ 全部收起' : '⤢ 全部展开';
  }

  function detailHtml(r) {
    return '' +
      '<div class="mtd-grid">' +
        sectionIntraday(r) +
        sectionFund(r) +
        sectionSeat(r) +
        sectionEtf(r) +
        sectionTech(r) +
        sectionNode(r) +
      '</div>' +
      (r.notes && r.notes.length
        ? '<div class="mtd-notes">' + r.notes.map(function (x) {
            return '<span class="mtd-note">· ' + esc(x) + '</span>';
          }).join('') + '</div>'
        : '');
  }

  // ① 分时
  function sectionIntraday(r) {
    var days = Number($('#mtIntradayDays').value) || 1;
    return '<div class="mtd-sec mtd-sec-wide">' +
      '<div class="mtd-h"><b>⏱ 分时</b>' +
        '<span class="mtd-hsub">' + (days === 1 ? '当日 1 分钟' : '近 ' + days + ' 日 1 分钟') +
        ' · 实线=价格 · 金线=均价 · 虚线=昨收</span>' +
      '</div>' +
      '<div class="mt-intra-wrap" data-days="' + days + '">' +
        '<canvas class="mt-intra" data-mt-code="' + esc(r.code) +
          '" data-mt-key="' + esc(r.code) + '|' + days + '" width="900" height="240"></canvas>' +
        '<div class="mt-intra-tip" data-mt-tip="' + esc(r.code) + '">加载分时…</div>' +
      '</div>' +
      '<div class="mt-intra-stats" data-mt-stats="' + esc(r.code) + '"></div>' +
    '</div>';
  }

  // ② 资金画像
  function sectionFund(r) {
    var fm = r.fund_mix || {};
    var tick = fm.tick, seat = fm.seat;
    var out = '<div class="mtd-sec"><div class="mtd-h"><b>💰 资金占比</b>' +
      '<span class="mtd-hsub">两个口径并列，不合成</span></div>';

    if (tick) {
      var tk = tick.today || {};
      var rows = [
        ['超大单', tk.xlarge, '机构/大游资常用的单笔规模'],
        ['大单', tk.large, '机构/游资'],
        ['中单', tk.mid, '大户/中小游资'],
        ['小单', tk.small, '散户']
      ];
      var tot = rows.reduce(function (a, x) { return a + Math.abs(Number(x[1]) || 0); }, 0) || 1;
      out += '<div class="mtd-sub2">A. 逐笔分档（' + esc(tick.date || '') + '，当日）</div>';
      out += '<div class="mt-bars">' + rows.map(function (x) {
        var v = Number(x[1]) || 0;
        var w = Math.min(100, Math.abs(v) / tot * 100);
        return '<div class="mt-bar-row" title="' + esc(x[2]) + '">' +
          '<span class="mtb-label">' + esc(x[0]) + '</span>' +
          '<span class="mtb-track"><i class="' + (v >= 0 ? 'bar-up' : 'bar-down') +
            '" style="width:' + w.toFixed(1) + '%"></i></span>' +
          '<span class="mtb-val ' + trendCls(v) + '">' + money(v) + '</span>' +
        '</div>';
      }).join('') + '</div>';
      var d5 = tick.d5 || {};
      out += '<div class="mt-fund-d5">' +
        fundCell('5日 超大单', d5.xlarge) + fundCell('5日 大单', d5.large) +
        fundCell('5日 中单', d5.mid) + fundCell('5日 小单', d5.small) +
        fundCell('5日 主力合计', d5.main) + '</div>';
      out += '<div class="mtd-fine">' + esc(tick.note || '') + '</div>';
    } else {
      out += '<div class="mtd-none">逐笔分档不可用</div>';
    }

    if (seat) {
      out += '<div class="mtd-sub2">B. 龙虎榜席位（' + esc(seat.date || '') + ' 上榜日）</div>';
      out += '<div class="mt-bars">' + (seat.rows || []).map(function (x) {
        var v = Number(x.net) || 0;
        var w = Math.min(100, Math.abs(Number(x.share) || 0));
        return '<div class="mt-bar-row" title="买 ' + money(x.buy) + ' / 卖 ' + money(x.sell) +
          ' / ' + x.n + ' 个席位">' +
          '<span class="mtb-label">' + esc(x.cat) + '</span>' +
          '<span class="mtb-track"><i class="' + (v >= 0 ? 'bar-up' : 'bar-down') +
            '" style="width:' + w.toFixed(1) + '%"></i></span>' +
          '<span class="mtb-val ' + trendCls(v) + '">' + money(v) + '</span>' +
        '</div>';
      }).join('') + '</div>';
      out += '<div class="mtd-fine">' + esc(seat.note || '') + '；' + esc(seat.coverage || '') + '</div>';
    } else {
      out += '<div class="mtd-sub2">B. 龙虎榜席位</div>' +
        '<div class="mtd-none">近期未上榜，无席位数据（这是常态，不是故障）</div>';
    }

    var etfMix = fm.etf;
    if (etfMix) {
      out += '<div class="mtd-sub2">C. ETF 被动持仓（' + esc(etfMix.report_date || '') + ' 季报）</div>' +
        '<div class="mt-fund-d5">' +
        fundCell('持有 ETF 数', etfMix.n, '', true) +
        fundCell('合计持仓', etfMix.cap, 'money') +
        fundCell('其中宽基', etfMix.wide_cap, 'money') +
        fundCell('ETF 平均涨跌', etfMix.avg_pct, 'pct') +
        '</div><div class="mtd-fine">' + esc(etfMix.note || '') + '</div>';
    }
    if ((fm.notes || []).length) {
      out += '<div class="mtd-fine">' + fm.notes.map(esc).join('；') + '</div>';
    }
    return out + '</div>';
  }

  function fundCell(label, v, kind, raw) {
    var txt, cls = '';
    if (v === null || v === undefined) { txt = '—'; }
    else if (kind === 'money') { txt = money(v); cls = trendCls(v); }
    else if (kind === 'pct') { txt = pct(v); cls = trendCls(v); }
    else if (raw) { txt = n(v, 0); }
    else { txt = money(v); cls = trendCls(v); }
    return '<div class="mtf"><em>' + esc(label) + '</em><b class="' + cls + '">' + esc(txt) + '</b></div>';
  }

  // ③ 席位
  function sectionSeat(r) {
    var s = r.seats || {};
    var ch = r.seat_change || {};
    var recs = s.records || [];
    var out = '<div class="mtd-sec"><div class="mtd-h"><b>🏛 买卖席位</b>' +
      '<span class="mtd-hsub">龙虎榜口径 · 只在上榜日有数据</span></div>';

    if (!recs.length) {
      return out + '<div class="mtd-none">近 ' + esc($('#mtSeatLookback').value) +
        ' 天未上龙虎榜（无异动记录）—— 普通交易日没有席位数据是正常的</div></div>';
    }

    // 上榜历史。D1/D5（东财「上榜后N日涨跌」）多数日期为空 → 全空时干脆不渲染这两列，
    // 免得整列都是「—」让人误以为是取数失败
    var hasD1 = recs.some(function (x) { return x.after && x.after.d1 != null; });
    var hasD5 = recs.some(function (x) { return x.after && x.after.d5 != null; });
    out += '<div class="mtd-sub2">上榜历史（近 ' + recs.length + ' 次）</div>' +
      '<table class="mt-tbl mt-tbl-sm"><thead><tr>' +
      '<th>日期</th><th>上榜原因</th><th>净买额</th><th>涨跌</th><th>换手</th>' +
      (hasD1 ? '<th>次日</th>' : '') + (hasD5 ? '<th>5日</th>' : '') +
      '</tr></thead><tbody>' +
      recs.slice(0, 8).map(function (x) {
        var af = x.after || {};
        return '<tr><td class="mt-nowrap">' + esc(x.date) + '</td>' +
          '<td class="mt-td-reason" title="' + esc(x.reason) + '">' + esc(x.reason || '—') + '</td>' +
          '<td class="mt-nowrap ' + trendCls(x.net) + '">' + money(x.net) + '</td>' +
          '<td class="mt-nowrap ' + trendCls(x.chg) + '">' + pct(x.chg) + '</td>' +
          '<td class="mt-nowrap">' + (x.turnover == null ? '—' : n(x.turnover, 1) + '%') + '</td>' +
          (hasD1 ? '<td class="mt-nowrap ' + trendCls(af.d1) + '">' + pct(af.d1) + '</td>' : '') +
          (hasD5 ? '<td class="mt-nowrap ' + trendCls(af.d5) + '">' + pct(af.d5) + '</td>' : '') +
          '</tr>';
      }).join('') +
      '</tbody></table>' +
      (hasD1 || hasD5 ? '' : '<div class="mtd-fine">东财的「上榜后 N 日涨跌」字段对历史日期多为空，故不展示这两列</div>');

    // 最近一次买卖前五
    var lt = s.latest;
    if (lt) {
      out += '<div class="mtd-sub2">最近一次（' + esc(lt.date) + '）买卖前五席位</div>' +
        '<div class="mt-seat-cols">' + seatSide('买入', lt.buy, 'buy') +
        seatSide('卖出', lt.sell, 'sell') + '</div>';
    }

    // 变动
    if (ch && ch.ok) {
      out += '<div class="mtd-sub2">与上一次上榜（' + esc(ch.prev_date) + '）相比</div>' +
        '<div class="mt-chg-grid">' +
        chgBox('买入席位 新进', ch.buy_new, 'up') +
        chgBox('买入席位 消失', ch.buy_out, 'down') +
        chgBox('卖出席位 新进', ch.sell_new, 'down') +
        chgBox('卖出席位 消失', ch.sell_out, 'up') +
        '</div>';
      var cd = (ch.cat_delta || []).filter(function (x) { return Math.abs(x.delta) > 1e6; });
      if (cd.length) {
        out += '<div class="mt-cat-delta">' + cd.map(function (x) {
          return '<span class="mt-cd ' + trendCls(x.delta) + '">' + esc(x.cat) + ' 净额变化 ' +
            money(x.delta) + '</span>';
        }).join('') + '</div>';
      }
    } else {
      out += '<div class="mtd-fine">' + esc((ch && ch.note) || '上榜次数不足两次，无法对比席位变动') + '</div>';
    }
    return out + '</div>';
  }

  function seatSide(title, arr, side) {
    arr = arr || [];
    var sum = arr.reduce(function (a, x) {
      return a + (Number(side === 'buy' ? x.buy : x.sell) || 0);
    }, 0);
    return '<div class="mt-seat-col"><div class="mt-seat-h ' + (side === 'buy' ? 'mt-up' : 'mt-down') + '">' +
      esc(title) + '合计 ' + money(sum) + '</div>' +
      (arr.length ? arr.map(function (x) {
        return '<div class="mt-seat-row" title="' + esc(x.why || '') + '">' +
          '<span class="mt-dept">' + esc(x.dept || '—') + '</span>' +
          '<span class="mt-cat c-' + esc(x.cat) + '">' + esc(x.cat) + '</span>' +
          '<span class="mt-amt ' + trendCls(side === 'buy' ? x.buy : x.sell) + '">' +
            money(side === 'buy' ? x.buy : x.sell) + '</span>' +
          (x.rise_prob_3d != null
            ? '<span class="mt-prob" title="该席位近 3 日买入后上涨概率">胜率 ' + n(x.rise_prob_3d, 0) + '%</span>'
            : '') +
        '</div>';
      }).join('') : '<div class="mtd-none">无</div>') + '</div>';
  }

  function chgBox(title, arr, kind) {
    arr = arr || [];
    return '<div class="mt-chg"><em>' + esc(title) + '（' + arr.length + '）</em>' +
      (arr.length ? arr.slice(0, 5).map(function (x) {
        return '<span class="mt-chg-item ' + (kind === 'up' ? 'mt-up' : 'mt-down') + '">' +
          esc(shortDept(x)) + '</span>';
      }).join('') : '<span class="mt-neu">无</span>') + '</div>';
  }
  function shortDept(x) {
    var s = String(x || '');
    return s.replace(/证券股份有限公司|有限责任公司|有限公司|证券营业部|营业部|分公司/g, '')
            .slice(0, 14);
  }

  // ④ ETF
  function sectionEtf(r) {
    var e = r.etf || {};
    var out = '<div class="mtd-sec"><div class="mtd-h"><b>📦 ETF 情况</b>' +
      '<span class="mtd-hsub">季报持仓穿透 · 不是实时申赎</span></div>';
    if (!e.ok) {
      return out + '<div class="mtd-none">' + esc(e.err || '无 ETF 持仓数据') + '</div></div>';
    }
    out += '<div class="mt-etf-sum">' +
      '<span>报告期 <b>' + esc(e.report_date || '—') + '</b></span>' +
      '<span>持有 ETF <b>' + n(e.n_etf, 0) + '</b> 只</span>' +
      '<span>合计 <b>' + money(e.total_cap) + '</b></span>' +
      '<span>宽基 <b>' + money(e.wide_cap) + '</b>（' + n(e.n_wide, 0) + ' 只）</span>' +
      '<span>ETF 平均 <b class="' + trendCls(e.etf_avg_pct) + '">' + pct(e.etf_avg_pct) + '</b></span>' +
      '</div>';
    var hs = e.holdings || [];
    if (hs.length) {
      out += '<table class="mt-tbl mt-tbl-sm"><thead><tr>' +
        '<th>ETF</th><th>代码</th><th>持仓市值</th><th>占总股本</th><th>占净值</th>' +
        '<th>今日</th><th>环比</th><th>规模</th></tr></thead><tbody>' +
        hs.map(function (x) {
          return '<tr><td class="mt-td-name" title="' + esc(x.name) + '">' + esc(x.name) + '</td>' +
            '<td>' + esc(x.code) + '</td>' +
            '<td>' + money(x.cap) + '</td>' +
            '<td>' + (x.shares_ratio == null ? '—'
              : (x.shares_ratio < 0.01 ? x.shares_ratio.toFixed(4) : n(x.shares_ratio, 3)) + '%') + '</td>' +
            '<td>' + (x.nav_ratio == null ? '—' : n(x.nav_ratio, 2) + '%') + '</td>' +
            '<td class="' + trendCls(x.pct) + '">' + pct(x.pct) + '</td>' +
            '<td class="' + trendCls(x.delta_cap) + '">' +
              (x.delta_cap == null ? '—' : money(x.delta_cap)) + '</td>' +
            '<td>' + money(x.scale) + '</td></tr>';
        }).join('') + '</tbody></table>';
    }
    if ((e.delta_up || []).length || (e.delta_down || []).length) {
      out += '<div class="mt-etf-delta"><em>环比上一报告期（' + esc(e.prev_report_date || '—') + '）</em>' +
        (e.delta_up || []).map(function (x) {
          return '<span class="mt-up">增持 ' + esc(x.name) + ' ' + money(x.delta_cap) + '</span>';
        }).join('') +
        (e.delta_down || []).map(function (x) {
          return '<span class="mt-down">减持 ' + esc(x.name) + ' ' + money(x.delta_cap) + '</span>';
        }).join('') + '</div>';
    }
    out += '<div class="mtd-fine">' + esc(e.note || '') + '</div>';
    return out + '</div>';
  }

  // ⑤ 技术特征
  function sectionTech(r) {
    var t = r.tech || {};
    var out = '<div class="mtd-sec"><div class="mtd-h"><b>📐 技术特征</b>' +
      '<span class="mtd-hsub">本地日线 ' + n(t.bars, 0) + ' 根 · 截至 ' + esc(t.last_date || '—') + '</span></div>';
    if (!t.ok) {
      return out + '<div class="mtd-none">' + esc(t.err || '技术面不可用（本地日线不足）') + '</div></div>';
    }
    var ma = t.ma || {}, boll = t.boll || {}, macd = t.macd || {}, kdj = t.kdj || {};
    var rel = t.rel || {};
    out += '<div class="mt-tech-grid">' +
      techCell('均线排列', t.ma_arrange || '—',
        t.ma_arrange === '多头排列' ? 'mt-up' : (t.ma_arrange === '空头排列' ? 'mt-down' : '')) +
      techCell('RSI(14)', n(t.rsi14, 1),
        t.rsi14 >= 70 ? 'mt-up' : (t.rsi14 <= 30 ? 'mt-down' : '')) +
      techCell('MACD', (macd.cross || '—') + ' ' + (macd.hist == null ? '' : n(macd.hist, 2)),
        macd.hist > 0 ? 'mt-up' : (macd.hist < 0 ? 'mt-down' : '')) +
      techCell('KDJ', 'K' + n(kdj.k, 0) + ' D' + n(kdj.d, 0) + ' J' + n(kdj.j, 0), '') +
      techCell('BOLL 位置', n(boll.pos_pct, 0) + '%分位', boll.pos_pct > 85 ? 'mt-up' : (boll.pos_pct < 15 ? 'mt-down' : '')) +
      techCell('带宽', n(boll.width_pct, 1) + '%', boll.width_pct < 6 ? 'mt-warn' : '') +
      techCell('ATR(14)', n(t.atr14, 2) + '（' + n(t.atr_pct, 2) + '%）', '') +
      techCell('5/20 量能', n(t.vol_ratio_5_20, 2) + '×', t.vol_ratio_5_20 > 1.5 ? 'mt-up' : '') +
      techCell('换手分位', t.turnover_pct_rank == null ? '—' : n(t.turnover_pct_rank, 0) + '%', '') +
      techCell('20 日超额', pct(rel.excess20), trendCls(rel.excess20)) +
      techCell('60 日超额', pct(rel.excess60), trendCls(rel.excess60)) +
      techCell('连续', (t.streak && t.streak.n ? t.streak.sign + '×' + t.streak.n : '—'),
        t.streak && t.streak.sign === '阳' ? 'mt-up' : (t.streak && t.streak.sign === '阴' ? 'mt-down' : '')) +
      '</div>';
    out += '<div class="mt-ma-line">' +
      [5, 10, 20, 60, 120, 250].map(function (k) {
        var v = ma['ma' + k];
        if (v == null) return '';
        var c = r.quote && r.quote.price ? (r.quote.price > v ? 'mt-up' : 'mt-down') : '';
        return '<span class="mt-mai ' + c + '">MA' + k + ' ' + n(v, 2) + '</span>';
      }).join('') + '</div>';
    if (t.cost_peak) {
      out += '<div class="mtd-fine">筹码峰：' + n(t.cost_peak.price, 2) + ' 元（区间 ' +
        n(t.cost_peak.zone_lo, 2) + '~' + n(t.cost_peak.zone_hi, 2) + '，占近 ' +
        t.cost_peak.lookback + ' 日成交量 ' + n(t.cost_peak.weight_share, 1) + '%）</div>';
    }
    if ((t.gaps || []).length) {
      out += '<div class="mtd-fine">未回补缺口：' + t.gaps.map(function (g) {
        return esc(g.kind) + ' ' + n(g.lo, 2) + '~' + n(g.hi, 2) + '（' + esc(g.date) + '）';
      }).join('；') + '</div>';
    }
    return out + '</div>';
  }

  function techCell(label, val, cls) {
    return '<div class="mtt"><em>' + esc(label) + '</em><b class="' + (cls || '') + '">' +
      esc(val) + '</b></div>';
  }

  // ⑥ 关键节点 + 信号
  function sectionNode(r) {
    var nodes = r.nodes || [];
    var meta = r.nodes_meta || {};
    var sigs = r.signals || [];
    var out = '<div class="mtd-sec mtd-sec-wide"><div class="mtd-h"><b>📌 关键节点距离</b>' +
      '<span class="mtd-hsub">共 ' + (meta.total || nodes.length) + ' 个，显示最近的 ' +
      Math.min(nodes.length, 14) + ' 个 · 阈值 ' + n(meta.tolerance, 1) + '%</span></div>';
    if (!nodes.length) {
      out += '<div class="mtd-none">技术面不可用，无法计算节点距离</div>';
    } else {
      out += '<table class="mt-tbl mt-node-tbl"><thead><tr>' +
        '<th>节点</th><th>价位</th><th>距离</th><th>位置</th><th>状态</th><th>说明</th>' +
        '</tr></thead><tbody>' +
        nodes.slice(0, 14).map(function (x) {
          return '<tr><td><b>' + esc(x.name) + '</b><span class="mt-kind">' + esc(x.kind) + '</span></td>' +
            '<td class="mt-num">' + n(x.price, 2) + '</td>' +
            '<td class="mt-num ' + trendCls(x.dist_pct) + '">' +
              (x.dist_pct == null ? '—' :
                (x.dist_pct > 0 ? '+' : '') + Number(x.dist_pct).toFixed(2) + '%') + '</td>' +
            '<td>' + (x.dist_pct == null ? '—' : (x.dist_pct > 0 ? '现价在上方' : '现价在下方')) + '</td>' +
            '<td><i class="node-state s-' + esc(x.state) + '">' + esc(x.state) + '</i></td>' +
            '<td class="mt-td-desc">' + esc(x.desc || '') + '</td></tr>';
        }).join('') + '</tbody></table>';
    }
    out += '</div>';

    // 信号
    out += '<div class="mtd-sec mtd-sec-wide"><div class="mtd-h"><b>🚩 信号（' + sigs.length + '）</b>' +
      '<span class="mtd-hsub">强=当日新发生 · 中=状态性 · 弱=边缘提示</span></div>';
    if (!sigs.length) {
      out += '<div class="mtd-none">当前没有触发任何规则信号（这本身是一个中性结论）</div>';
    } else {
      out += '<div class="mt-sig-list">' + sigs.map(function (s) {
        return '<div class="mt-sig-row ' + levelCls(s.level) + '">' +
          '<span class="mt-sig-dir ' + dirCls(s.dir) + '">' + esc(s.dir) + '</span>' +
          '<span class="mt-sig-lv">' + esc(s.level) + '</span>' +
          '<b class="mt-sig-name">' + esc(s.name) + '</b>' +
          '<span class="mt-sig-desc">' + esc(s.desc || '') + '</span>' +
          (s.evidence ? '<span class="mt-sig-ev">' + esc(s.evidence) + '</span>' : '') +
        '</div>';
      }).join('') + '</div>';
    }
    out += '</div>';
    return out;
  }

  // ---------- 分时：懒加载 + 绘制 ----------
  function ensureIntraday(code, cv) {
    var days = Number($('#mtIntradayDays').value) || 1;
    var key = code + '|' + days;
    var tip = document.querySelector('[data-mt-tip="' + code + '"]');
    var stats = document.querySelector('[data-mt-stats="' + code + '"]');
    if (INTRADAY[key]) {
      if (INTRADAY[key].ok) {
        drawIntraday(cv, INTRADAY[key]);
        if (tip) tip.textContent = '';
        if (stats) stats.innerHTML = intradayStats(INTRADAY[key]);
      } else if (tip) {
        tip.textContent = INTRADAY[key].err || '分时不可用';
      }
      return;
    }
    req('/api/monitor/intraday?code=' + encodeURIComponent(code) + '&days=' + days, {}, 30000)
      .then(function (d) {
        INTRADAY[key] = d;
        var c2 = document.querySelector('canvas[data-mt-key="' + key + '"]') || cv;
        if (!d || !d.ok) {
          if (tip) tip.textContent = (d && d.err) || '分时不可用';
          return;
        }
        drawIntraday(c2, d);
        if (tip) tip.textContent = '';
        var st = document.querySelector('[data-mt-stats="' + code + '"]');
        if (st) st.innerHTML = intradayStats(d);
      })
      .catch(function (e) {
        if (tip) tip.textContent = '分时加载失败：' + e.message;
      });
  }

  function intradayStats(d) {
    var s = d.stats || {};
    var seg = s.seg_amount_pct || {};
    return '<span>开 <b>' + n(d.pre_close, 2) + '</b></span>' +
      '<span>高 <b class="mt-up">' + n(s.high, 2) + '</b></span>' +
      '<span>低 <b class="mt-down">' + n(s.low, 2) + '</b></span>' +
      '<span>均价 <b>' + n(s.vwap, 2) + '</b></span>' +
      '<span>振幅 <b>' + n(s.amp_pct, 2) + '%</b></span>' +
      '<span class="mt-seg">早盘 ' + n(seg['早盘(9:30-10:30)'], 0) + '% · 尾盘 ' +
      n(seg['尾盘(14:00-15:00)'], 0) + '%</span>' +
      (s.tail_strong ? '<span class="mt-warn">尾盘放量</span>' : '');
  }

  function hidpi(cv, scale) {
    var rect = cv.getBoundingClientRect ? cv.getBoundingClientRect() : { width: 0, height: 0 };
    var W = Math.max(320, Math.round(rect.width || cv.width || 900));
    var H = Math.max(140, Math.round(rect.height || cv.height || 240));
    scale = scale || (window.devicePixelRatio > 1 ? 2 : 1);
    cv.width = Math.round(W * scale);
    cv.height = Math.round(H * scale);
    cv.style.width = W + 'px';
    cv.style.height = H + 'px';
    var ctx = cv.getContext('2d');
    if (!ctx) return { ctx: null, W: W, H: H };
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, W, H);
    return { ctx: ctx, W: W, H: H };
  }

  function drawIntraday(cv, d) {
    var pts = d.points || [];
    if (!pts.length) return;
    var hi = hidpi(cv, window.devicePixelRatio > 1 ? 2 : 1);
    var ctx = hi.ctx, W = hi.W, H = hi.H;
    if (!ctx) return;

    var cUp = cssVar('--k-up', '#ef5350');
    var cDown = cssVar('--k-down', '#26a69a');
    var cGrid = cssVar('--k-grid', 'rgba(255,255,255,.07)');
    var cAxis = cssVar('--k-axis', '#7b7f88');
    var cAccent = cssVar('--accent', '#f0c14b');
    var cCross = cssVar('--k-cross', 'rgba(255,255,255,.42)');
    var cInk = cssVar('--ink', '#e9e7e2');

    var PADL = 44, PADR = 48, PADT = 10;
    var volH = Math.round(H * 0.22);
    var priceH = H - PADT - volH - 6;
    var plotW = W - PADL - PADR;

    var prices = pts.map(function (p) { return p[1]; }).filter(function (x) { return x != null; });
    var avgs = pts.map(function (p) { return p[2]; }).filter(function (x) { return x != null; });
    var pre = d.pre_close;
    var all = prices.concat(avgs);
    if (pre) all.push(pre);
    var mx = Math.max.apply(null, all), mn = Math.min.apply(null, all);
    if (!isFinite(mx) || !isFinite(mn)) return;
    // 上下留 6% 余量，避免线贴边
    var pad = (mx - mn) * 0.06 || (mx * 0.01) || 1;
    mx += pad; mn -= pad;
    var span = (mx - mn) || 1;

    var maxVol = Math.max.apply(null, pts.map(function (p) { return Number(p[3]) || 0; })) || 1;
    var xOf = function (i) { return PADL + (pts.length <= 1 ? plotW / 2 : i / (pts.length - 1) * plotW); };
    var yOf = function (v) { return PADT + (mx - v) / span * priceH; };

    // 网格（4 条横向）
    ctx.strokeStyle = cGrid; ctx.lineWidth = 1;
    ctx.font = '10px ' + cssVar('--mono', 'monospace');
    ctx.fillStyle = cAxis;
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (var g = 0; g <= 4; g++) {
      var yy = PADT + priceH * g / 4;
      ctx.beginPath(); ctx.moveTo(PADL, yy); ctx.lineTo(W - PADR, yy); ctx.stroke();
      var vv = mx - span * g / 4;
      ctx.fillText(vv.toFixed(2), PADL - 6, yy);
    }
    // 竖向时间分隔（多日分时按日切）
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    var dayIdx = {};
    // ★必须只记「首次出现」的索引：无脑覆盖会让单日分时的日期标签跑到最右端
    pts.forEach(function (p, i) {
      var k = String(p[0]).slice(0, 10);
      if (!(k in dayIdx)) dayIdx[k] = i;
    });
    Object.keys(dayIdx).forEach(function (dt, k) {
      var x = xOf(dayIdx[dt]);
      if (k > 0) {
        ctx.strokeStyle = cGrid;
        ctx.beginPath(); ctx.moveTo(x, PADT); ctx.lineTo(x, H - volH - 6); ctx.stroke();
      }
      ctx.fillStyle = cAxis;
      ctx.textAlign = 'left';
      ctx.fillText(dt.slice(5), x + 6, H - volH + 2);
      ctx.textAlign = 'center';
    });

    // 昨收虚线
    if (pre) {
      ctx.save();
      ctx.strokeStyle = cCross; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
      var yPre = yOf(pre);
      ctx.beginPath(); ctx.moveTo(PADL, yPre); ctx.lineTo(W - PADR, yPre); ctx.stroke();
      ctx.restore();
      ctx.fillStyle = cCross;
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillText('昨收', W - PADR + 4, yPre);
    }

    // 价格线 + 渐变填充
    var cMain = cInk;
    var grad = ctx.createLinearGradient(0, PADT, 0, PADT + priceH);
    grad.addColorStop(0, 'rgba(240,193,75,.16)');
    grad.addColorStop(1, 'rgba(240,193,75,0)');
    ctx.beginPath();
    pts.forEach(function (p, i) {
      if (p[1] == null) return;
      var x = xOf(i), y = yOf(p[1]);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    var lastX = xOf(pts.length - 1);
    ctx.lineTo(lastX, PADT + priceH);
    ctx.lineTo(PADL, PADT + priceH);
    ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();

    ctx.beginPath();
    var started = false;
    pts.forEach(function (p, i) {
      if (p[1] == null) return;
      var x = xOf(i), y = yOf(p[1]);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = cMain; ctx.lineWidth = 1.4; ctx.stroke();

    // 均价线
    ctx.beginPath();
    var st2 = false;
    pts.forEach(function (p, i) {
      if (p[2] == null) return;
      var x = xOf(i), y = yOf(p[2]);
      if (!st2) { ctx.moveTo(x, y); st2 = true; } else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = cAccent; ctx.lineWidth = 1.1; ctx.stroke();

    // 成交量柱
    var volTop = H - volH;
    var bw = Math.max(1, plotW / pts.length * 0.7);
    pts.forEach(function (p, i) {
      var v = Number(p[3]) || 0;
      var h = v / maxVol * (volH - 4);
      if (h <= 0) return;
      var prev = i > 0 ? pts[i - 1][1] : p[1];
      var up = (p[1] != null && prev != null) ? p[1] >= prev : true;
      ctx.fillStyle = up ? cUp : cDown;
      ctx.globalAlpha = 0.45;
      ctx.fillRect(xOf(i) - bw / 2, H - 2 - h, bw, h);
      ctx.globalAlpha = 1;
    });

    // 最新价标记
    var lastP = null, li = -1;
    for (var i = pts.length - 1; i >= 0; i--) {
      if (pts[i][1] != null) { lastP = pts[i][1]; li = i; break; }
    }
    if (lastP != null) {
      var yL = yOf(lastP);
      var chg = pre ? (lastP - pre) / pre * 100 : null;
      var cL = chg == null ? cInk : (chg >= 0 ? cUp : cDown);
      ctx.fillStyle = cL;
      ctx.fillRect(W - PADR + 2, yL - 8, PADR - 4, 16);
      ctx.fillStyle = '#fff';
      ctx.font = '10px ' + cssVar('--mono', 'monospace');
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(lastP.toFixed(2), W - PADR / 2, yL);
    }
  }

  // ---------- 自动刷新（只拉轻量快照） ----------
  function setupAutoRefresh() {
    if (TIMER) { clearInterval(TIMER); TIMER = null; }
    var sec = Number($('#mtAutoRefresh').value) || 0;
    if (!sec) return;
    TIMER = setInterval(refreshQuotes, sec * 1000);
  }

  function refreshQuotes() {
    if (!LAST || !LAST.rows) return;
    var codes = LAST.rows.map(function (r) { return r.code; });
    if (!codes.length) return;
    req('/api/monitor/quote?codes=' + codes.join(','), {}, 15000).then(function (d) {
      if (!d || !d.ok) return;
      var qs = d.quotes || {};
      Object.keys(qs).forEach(function (code) {
        var q = qs[code];
        var card = document.querySelector('.mt-card[data-code="' + code + '"]');
        if (!card) return;
        var r = LAST.rows.filter(function (x) { return x.code === code; })[0];
        if (r) r.quote = q;
        var priceEl = card.querySelector('.mtc-price b');
        var pctEl = card.querySelector('.mtc-price span');
        if (priceEl) {
          priceEl.textContent = n(q.price, 2);
          priceEl.className = trendCls(q.pct);
        }
        if (pctEl) {
          pctEl.textContent = pct(q.pct);
          pctEl.className = trendCls(q.pct);
        }
      });
      if (d.phase) renderPhase(d.phase);
      var hint = $('#mtHint');
      if (hint) {
        hint.textContent = '自动刷新 · ' + new Date().toTimeString().slice(0, 8) +
          ' · ' + ((d.phase || {}).label || '');
      }
    }).catch(function () { /* 轮询失败静默，下一轮再试 */ });
  }

  // ---------- CSV ----------
  function csvCell(v) {
    var s = String(v == null ? '' : v).replace(/<[^>]+>/g, '');
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  function exportCsv() {
    if (!LAST || !LAST.rows || !LAST.rows.length) {
      $('#mtHint').textContent = '还没有结果可以导出';
      return;
    }
    var rows = LAST.rows;
    var head = ['代码', '名称', '行业', '现价', '涨跌幅%', '量比', '换手%', '振幅%',
      '流通市值(亿)', '5日主力净流入(元)', '20日主力净流入(元)',
      'MA排列', 'RSI14', 'MACD', 'BOLL位置%', 'ATR%',
      '20日超额%', '60日超额%', '筹码峰', '最近节点', '节点距离%',
      '强信号', '全部信号', '最近上榜日', '最近上榜净买(元)',
      '机构净额(元)', '游资净额(元)', '散户净额(元)', '国家队净额(元)',
      '持有ETF数', 'ETF合计持仓(元)', 'ETF报告期', '备注'];
    var lines = [head.join(',')];
    rows.forEach(function (r) {
      if (r.error) {
        lines.push([r.code, r.name, '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '',
          '', '', '', '', '', '', '', '', '', '', '', '', '', '', 'ERR:' + r.error].map(csvCell).join(','));
        return;
      }
      var q = r.quote || {}, t = r.tech || {}, fm = r.fund_mix || {};
      var tick = fm.tick || {}, d5 = tick.d5 || {};
      // 20 日主力净流入：从 series（近 20 日逐日主力净额）求和，后端不再单独下发
      var s20 = (tick.series || []).reduce(function (a, x) {
        return a + (Number(x.main) || 0);
      }, 0);
      var seatRows = (fm.seat || {}).rows || [];
      var catNet = {};
      seatRows.forEach(function (x) { catNet[x.cat] = x.net; });
      var nearest = (r.nodes || [])[0] || {};
      var lt = (r.seats || {}).latest || null;
      var rec = (r.seats || {}).records || [];
      var ltRec = rec[0] || {};
      lines.push([
        r.code, r.name, r.sector || '', q.price, q.pct, q.vol_ratio, q.turnover, q.amplitude,
        q.float_cap, d5.main == null ? '' : d5.main, s20 || '',
        t.ma_arrange || '', t.rsi14, (t.macd || {}).cross || '', (t.boll || {}).pos_pct, t.atr_pct,
        (t.rel || {}).excess20, (t.rel || {}).excess60,
        (t.cost_peak || {}).price == null ? '' : t.cost_peak.price,
        nearest.name || '', nearest.dist_pct,
        (r.signals || []).filter(function (s) { return s.level === '强'; })
          .map(function (s) { return s.dir + '·' + s.name; }).join(' / '),
        (r.signals || []).map(function (s) { return s.dir + '·' + s.name; }).join(' / '),
        lt ? lt.date : '', ltRec.net == null ? '' : ltRec.net,
        catNet['机构'], catNet['游资'], catNet['散户'], catNet['国家队'],
        (r.etf || {}).n_etf, (r.etf || {}).total_cap, (r.etf || {}).report_date,
        (r.notes || []).join(' / ')
      ].map(csvCell).join(','));
    });
    // 口径段（跟在数据后面，方便一次读全）
    lines.push('');
    lines.push(['# 口径说明'].join(','));
    lines.push(['数据源', '腾讯快照/东财分时/东财资金流五档/东财龙虎榜/东财基金季报/本地日线'].map(csvCell).join(','));
    lines.push(['席位分类', '名称关键词规则，非官方身份披露；「机构专用」可信度最高，游资名单仅供参考'].map(csvCell).join(','));
    lines.push(['逐笔分档', '按单笔成交金额分档，只能说明大钱还是小钱，不能说明身份'].map(csvCell).join(','));
    lines.push(['龙虎榜覆盖', '只在异动日有数据，普通交易日无席位'].map(csvCell).join(','));
    lines.push(['ETF 持仓', '基金季报口径，滞后 1~3 个月，只披露前十大重仓'].map(csvCell).join(','));
    lines.push(['免责', '只做数据聚合与规则化特征描述，不构成投资建议'].map(csvCell).join(','));
    var csv = '\ufeff' + lines.join('\r\n');
    var blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = '盯盘_' + new Date().toISOString().slice(0, 10) + '.csv';
    a.click();
    URL.revokeObjectURL(a.href);
    $('#mtHint').textContent = '已导出 ' + rows.length + ' 条';
  }

  // 暴露少量接口给测试/调试（不污染全局命名空间，前缀 mt）
  window.MonitorView = {
    boot: boot,
    run: doRun,
    render: function (d) { LAST = d; renderPhase(d.phase); renderSummary(d); renderList(d); },
    toggle: toggleCard,
    detailHtml: detailHtml,
    drawIntraday: drawIntraday,
    exportCsv: exportCsv,
    _state: function () { return { LAST: LAST, OPEN: OPEN, DETAIL: DETAIL, INTRADAY: INTRADAY, META: META }; }
  };

  // DOM 就绪后如果页签已经是 monitor（例如刷新时停在该页），直接初始化
  if (document.readyState !== 'loading') {
    var active = document.querySelector('.tab[data-tab="monitor"].active');
    if (active) boot();
  } else {
    document.addEventListener('DOMContentLoaded', function () {
      var active = document.querySelector('.tab[data-tab="monitor"].active');
      if (active) boot();
    });
  }
})();
