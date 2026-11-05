/* ==========================================================================
   技术分析 · 独立前端脚本
   --------------------------------------------------------------------------
   ★零挤占原则（同 bfilter.js / monitor.js）：
     - 不改 app.js / style.css 一行；
     - 不覆盖 app.js 的全局函数；
     - 用「事件委托」监听页签点击来懒初始化。
   ★三条必须始终成立的展示规则：
     1. 分数是「形态健康度」，页面任何地方都不写「分数高就会涨」；
     2. 回测必须同时给「宽松口径（≥2 个共同信号）」与「高相似口径」，
        因为前者在参照股信号多时会退化成「该股常态」；
     3. 横截面必须给「同期市场基线」与「超额」，绝对收益在单边行情里没有意义。
   ========================================================================== */
(function () {
  'use strict';

  var INITED = false;
  var META = null;
  var LAST = null;
  var POLL_TID = null;
  var START_TS = 0;
  // 信号 key → 中文名（创建时由 /api/ta/meta 填充；回测明细里要用）
  var SIG_LABEL = {};

  // ------------------------------------------------------------------
  // 小工具（局部实现，不依赖 app.js 内部）
  // ------------------------------------------------------------------
  function $(sel) { return document.querySelector(sel); }
  function $$(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function num(v, nd) {
    if (v === null || v === undefined || v === '' || isNaN(Number(v))) return '—';
    return Number(v).toFixed(nd == null ? 2 : nd);
  }

  function msg(text, kind) {
    var el = $('#taMsg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'ta-msg' + (kind ? ' ' + kind : '');
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

  function postJson(url, body, timeout) {
    return req(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }, timeout);
  }

  // 涨红跌绿
  function signCls(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return '';
    var n = Number(v);
    return n > 0 ? 'pos' : (n < 0 ? 'neg' : '');
  }
  function pctText(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return '—';
    var n = Number(v);
    return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
  }
  function ppText(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return '—';
    var n = Number(v);
    return (n > 0 ? '+' : '') + n.toFixed(2) + 'pp';
  }

  // ------------------------------------------------------------------
  // 进度轮询（后端 _task_progress 三元组回调 → /api/progress）
  // ------------------------------------------------------------------
  function stopPoll() {
    if (POLL_TID) { clearInterval(POLL_TID); POLL_TID = null; }
  }
  function startPoll() {
    stopPoll();
    START_TS = Date.now();
    POLL_TID = setInterval(function () {
      req('/api/progress', {}, 4000).then(function (j) {
        if (!j || j.status !== 'running') return;
        var frac = Math.max(0, Math.min(100, Math.round((j.frac || 0))));
        var secs = Math.round((Date.now() - START_TS) / 1000);
        msg('⏳ ' + (j.msg || '分析中') + ' · ' + frac + '% · 已用 ' + secs + 's', 'info');
      }).catch(function () { /* 轮询失败不影响主流程 */ });
    }, 800);
  }

  // ------------------------------------------------------------------
  // 渲染：不可选提示
  // ------------------------------------------------------------------
  function renderBlock(r) {
    var cnt = r.signal_count == null ? 0 : r.signal_count;
    var hits = (r.signals || []).filter(function (s) { return s.on; });
    var hitTxt = hits.length
      ? hits.map(function (s) { return s.label; }).join('、')
      : '无';
    var html = '';
    html += '<div class="ta-block">';
    html += '<h3>⚠ 这只股票当前不可回测</h3>';
    html += '<div>' + esc(r.reason || '命中信号不足') + '</div>';
    html += '<div class="ta-block-sub">当前命中 ' + cnt + ' 个技术信号：' +
            esc(hitTxt) + '</div>';
    if (r.hint) html += '<div class="ta-block-sub">' + esc(r.hint) + '</div>';
    // ★信号不足 ≠ 没机会：把第二把尺子的判断也放进来，避免「用信号门槛又漏一次」
    if (r.rebound_hint) {
      var rb = r.rebound || {};
      var isReb = (rb.type || {}).key === 'rebound';
      html += '<div class="ta-block-sub" style="margin-top:8px;' +
              (isReb ? 'color:var(--accent-deep);font-weight:600' : '') + '">' +
              esc(r.rebound_hint) + '</div>';
    }
    html += '</div>';
    return html;
  }

  // ------------------------------------------------------------------
  // 渲染：总分卡
  // ------------------------------------------------------------------
  var GRADE_COLOR = {
    strong: 'var(--good)', good: 'var(--accent)', mid: 'var(--neutral)',
    weak: 'var(--bad)', bad: 'var(--bad)'
  };
  var REB_COLOR = {
    strong: 'var(--good)', good: 'var(--accent)', mid: 'var(--ink-soft)',
    none: 'var(--ink-faint)'
  };

  function renderHero(r) {
    var g = r.grade || { label: '—', key: 'mid', desc: '' };
    var col = GRADE_COLOR[g.key] || 'var(--accent)';
    var sc = Math.max(0, Math.min(100, Number(r.score) || 0));
    var rb = r.rebound || {};
    var rs = Math.max(0, Math.min(100, Number(rb.score) || 0));
    var rgb = rb.grade || {};
    var rcol = REB_COLOR[rgb.key] || 'var(--ink-faint)';
    var rtype = rb.type || {};
    var chgCls = signCls(r.change);
    var sign = chgCls === 'pos' ? 'up' : (chgCls === 'neg' ? 'down' : 'flat');

    var h = '';
    h += '<div class="ta-hero">';

    // 两把尺子并排：均衡分（大）+ 反抽分（小）。绝不合成一个数。
    h += '<div class="ta-rings">';
    h += '<div class="ta-ringwrap">' +
         '<div class="ta-ring" style="--ring-p:' + sc.toFixed(1) +
         ';--ring-c:' + col + '">' +
         '<div class="ta-ring-in">' +
         '<span class="ta-ring-num">' + sc.toFixed(1) + '</span>' +
         '<span class="ta-ring-of">/ 100 均衡分</span>' +
         '</div></div>' +
         '<span class="ta-ring-cap">技术面健康度</span></div>';
    h += '<div class="ta-ringwrap">' +
         '<div class="ta-ring sm" style="--ring-p:' + rs.toFixed(1) +
         ';--ring-c:' + rcol + '">' +
         '<div class="ta-ring-in">' +
         '<span class="ta-ring-num">' + rs.toFixed(1) + '</span>' +
         '<span class="ta-ring-of">/ 100 反抽分</span>' +
         '</div></div>' +
         '<span class="ta-ring-cap">超跌反抽（' + esc(rgb.label || '—') + '）</span></div>';
    h += '</div>';

    h += '<div class="ta-hero-main">';
    h += '<div class="ta-hero-top">';
    h += '<span class="ta-hero-name">' + esc(r.name || r.code) + '</span>';
    h += '<a class="ta-hero-code" href="#" data-code="' + esc(r.code) +
         '" title="点击查看 K 线">' + esc(r.code) + '</a>';
    h += '<span class="ta-hero-price">' + num(r.price, 2) + '</span>';
    h += '<span class="ta-hero-chg ' + sign + '">' + pctText(r.change) + '</span>';
    h += '<span class="ta-grade g-' + esc(g.key) + '" title="' + esc(g.desc) + '">' +
         esc(g.label) + '</span>';
    h += '<span class="ta-type t-' + esc(rtype.key || 'none') + '" title="' +
         esc(rtype.desc || '') + '">' + esc(rtype.label || '—') + '</span>';
    h += '</div>';

    h += '<div class="ta-hero-desc">' + esc(g.desc) +
         (rtype.desc ? '　·　' + esc(rtype.desc) : '') + '</div>';
    h += '<div class="ta-hero-meta">';
    h += '<span>数据日 <b>' + esc(r.asof || '—') + '</b></span>';
    h += '<span>K线 <b>' + esc(r.bars) + '</b> 根</span>';
    h += '<span>命中信号 <b>' + esc(r.signal_count) + '</b> / 14</span>';
    var rk = r.bt_cross && r.bt_cross.score_rank;
    if (rk && rk.pct != null) {
      h += '<span>全市场分位 <b>前 ' + num(100 - rk.pct, 1) + '%</b>（共 ' +
           esc(rk.of) + ' 只）</span>';
    }
    // 压制率 / 风险也放进 meta：一眼能看到，不占额外版面
    var cx = r.ctx || {};
    if (cx.suppress != null) {
      h += '<span>压制 <b>' + num(cx.suppress, 0) + '%</b></span>';
    }
    if (cx.risk != null) {
      h += '<span>风险 <b>' + num(cx.risk, 0) + '</b></span>';
    }
    h += '<span>数据源 <b>' + esc(r.source || '—') + '</b></span>';
    h += '</div>';
    if (r.seam_note) {
      h += '<div class="ta-hero-meta"><span style="color:var(--accent-deep)">' +
           esc(r.seam_note) + '</span></div>';
    }
    h += '</div></div>';
    return h;
  }

  // ------------------------------------------------------------------
  // 渲染：市场背景（牛/熊/震荡）
  // ------------------------------------------------------------------
  function bandCell(name, score, band, ic) {
    var h = '<div class="ta-mkt-cell">';
    h += '<div class="ta-mkt-k">' + esc(name) + ' <b>' + num(score, 1) + '</b>';
    if (ic != null && !isNaN(Number(ic))) {
      var n = Number(ic);
      h += ' <span class="ta-mkt-ic ' + (n > 0 ? 'pos' : 'neg') + '">IC ' +
           (n > 0 ? '+' : '') + n.toFixed(3) + '</span>';
    }
    h += '</div>';
    if (band) {
      h += '<div class="ta-mkt-v">落在 <b>' + esc(band.band) + '</b> 档 · 胜率 ' +
           num(band.win, 1) + '% · 20日均值 <span class="' + signCls(band.avg) + '">' +
           pctText(band.avg) + '</span></div>';
    } else {
      h += '<div class="ta-mkt-v">—</div>';
    }
    h += '</div>';
    return h;
  }

  function renderMarket(r) {
    var mk = r.market;
    if (!mk || !mk.now) return '';
    var now = mk.now;
    var ex = mk.expect || {};
    var h = '<div class="ta-mkt rg-' + esc(now.key || 'range') + '">';

    h += '<div class="ta-mkt-head">';
    h += '<span class="ta-mkt-badge">' + esc(now.label || '—') + '</span>';
    h += '<span class="ta-mkt-title">市场背景' +
         (mk.manual ? '（手动指定）' : '（自动识别）') + '</span>';
    var meta = [];
    if (now.pos250 != null) meta.push('距年线 ' + pctText(now.pos250));
    if (now.slope60 != null) meta.push('MA60 20日斜率 ' + pctText(now.slope60));
    if (now.days != null) meta.push('已持续 ' + esc(now.days) + ' 个交易日');
    h += '<span class="ta-mkt-meta">' + meta.join(' · ') + '</span>';
    h += '</div>';

    h += '<div class="ta-mkt-body">';
    h += bandCell('均衡分', r.score, ex.balance, ex.balance_ic);
    h += bandCell('反抽分', (r.rebound || {}).score, ex.rebound, ex.rebound_ic);
    h += '</div>';

    h += '<div class="ta-mkt-advice">' + esc(now.advice || '') + '</div>';

    if (mk.manual && mk.auto_key && mk.auto_key !== now.key) {
      h += '<div class="ta-mkt-warn">注意：系统自动识别当前是「' +
           esc(mk.auto_label || '') + '」，你手动切成了「' + esc(now.label) +
           '」。上面的档位预期按手动选择计算。</div>';
    }
    h += '</div>';
    return h;
  }


  // ------------------------------------------------------------------
  // 渲染：横向上下文（市值 / 风险 / 受压制率 / 基本面）
  // ------------------------------------------------------------------
  function renderCtx(r) {
    var c = r.ctx;
    if (!c) return '';
    var h = '<div class="ta-ctx">';
    h += '<div class="ta-ctx-head">';
    h += '<span class="ta-ctx-title">横向上下文</span>';
    h += '<span class="ta-ctx-sub">市值 / 风险系数 / 受压制率 / 基本面</span>';
    h += '</div>';
    h += '<div class="ta-ctx-grid">';
    h += ctxItem('流通市值', capText(c.mcap_yi), '');
    h += ctxItem('风险系数', c.risk == null ? '—' : num(c.risk, 0) + '<small> / 100</small>',
                 riskCls(c.risk));
    h += ctxItem('受压制率',
                 c.suppress == null ? '—' : num(c.suppress, 0) + '<small> %</small>',
                 supCls(c.suppress));
    h += ctxItem('基本面分', c.fund == null ? '—' : num(c.fund, 0) + '<small> / 100</small>', '');
    h += '</div>';

    var sp = c.suppress_parts || {};
    var bits = [];
    if (sp.intraday) bits.push('分时 ' + num(sp.intraday.sim, 0) + '%');
    if (sp.daily) bits.push('日线 ' + num(sp.daily.sim, 0) + '%');
    if (sp.weekly) bits.push('周线 ' + num(sp.weekly.sim, 0) + '%');
    if (bits.length) {
      h += '<div class="ta-ctx-note">受压制率 = ' + bits.join(' / ') +
           (c.index_name ? '（对应 <b>' + esc(c.index_name) + '</b>）' : '') +
           '。忽略幅度、只比形状。</div>';
    }
    if (c.suppress_err) {
      h += '<div class="ta-ctx-note">分时部分不可用（' + esc(c.suppress_err) +
           '），当前只用日线 + 周线。</div>';
    }
    if ((c.risk_items || []).length) {
      h += '<div class="ta-ctx-note ta-ctx-warn">风险项：' +
           esc(c.risk_items.join('；')) + '</div>';
    } else {
      h += '<div class="ta-ctx-note">未触发风险项（波动、回撤、流动性、趋势破坏型大跌均正常）。</div>';
    }
    var fd = c.fund_detail || {};
    if (fd.pe_pct != null || fd.div_yield != null) {
      var fbits = [];
      if (fd.pe_pct != null) fbits.push('PE 分位 ' + num(fd.pe_pct, 0) + '%');
      if (fd.pb_pct != null) fbits.push('PB 分位 ' + num(fd.pb_pct, 0) + '%');
      if (fd.div_yield != null) fbits.push('股息 ' + num(fd.div_yield, 2) + '%');
      if (fd.mcap_yi != null) fbits.push('市值 ' + num(fd.mcap_yi, 0) + ' 亿');
      h += '<div class="ta-ctx-note">基本面：' + fbits.join(' · ') +
           '。只在熊市参与排序（权重 8%）；市值不计入基本面，单独看。</div>';
    }
    h += '</div>';
    return h;
  }

  // ------------------------------------------------------------------
  // 渲染：顶部一句话总结（个股胜率 / 近一个月胜率 / 平均收益）
  // ------------------------------------------------------------------
  function sumCell(k, v, cls) {
    return '<div class="ta-sum-cell"><div class="ta-sum-k">' + esc(k) + '</div>' +
           '<div class="ta-sum-v ' + (cls || '') + '">' + v + '</div></div>';
  }

  function renderSummary(r) {
    var bt = r.bt_stock || null;
    var cr = r.bt_cross || null;
    var cs = (cr && cr.stat) || null;
    if (!bt && !cs) return '';

    var h = '<div class="ta-summary">';

    if (bt) {
      h += '<div class="ta-sum-row">';
      h += '<div class="ta-sum-name">个股历史' +
           '<small>' + esc(bt.horizon) + ' 日 · 同形态事件</small></div>';
      h += sumCell('胜率', bt.win == null ? '—' : num(bt.win, 1) + '%', '');
      h += sumCell('平均收益', pctText(bt.avg), signCls(bt.avg));
      h += sumCell('样本', esc(bt.n == null ? '—' : bt.n), '');
      h += '</div>';
    }

    if (cs) {
      h += '<div class="ta-sum-row">';
      h += '<div class="ta-sum-name">近一个月' +
           '<small>' + esc(cs.horizon || 5) + ' 日 · 全市场相似形态</small></div>';
      h += sumCell('胜率', cs.win == null ? '—' : num(cs.win, 1) + '%', '');
      h += sumCell('平均收益', pctText(cs.avg), signCls(cs.avg));
      h += sumCell('样本', esc(cs.n == null ? '—' : cs.n), '');
      h += '</div>';
    }

    // 参照注：绝对收益必须配同期市场，否则单边市里会误读
    var notes = [];
    if (bt && bt.base != null) {
      notes.push('个股历史对照 = 该股「没有相似信号」的日子，' +
                 pctText(bt.base) + '，超额 ' + ppText(bt.alpha) + '。');
    }
    if (cs) {
      var as = cs.alpha_stat || {};
      notes.push('近一个月同期市场平均 <b>' + pctText(cs.base) + '</b>，超额 <b>' +
                 ppText(as.avg) + '</b>（超额胜率 ' +
                 (as.win == null ? '—' : num(as.win, 1) + '%') + '）。');
      if (cs.scanned) {
        notes.push('扫描 ' + esc(cs.scanned) + ' 只，命中 ' + esc(cs.n) + ' 只。');
      }
    }
    if (notes.length) {
      h += '<div class="ta-sum-note">' + notes.join(' ') + '</div>';
    }
    h += '</div>';
    return h;
  }

  // ------------------------------------------------------------------
  // 范围内评分排行（Top N，同分并列）
  // ------------------------------------------------------------------
  function currentRange() {
    // 顶栏的「全部 / 沪市 / 深市 / 北交所」是 app.js 的全局 state，
    // 两个脚本同处全局词法作用域可直接读；探针里没有 app.js，故做 typeof 保护。
    try {
      if (typeof state !== 'undefined' && state && state.range) {
        var r = String(state.range).toUpperCase();
        return (r === 'ALL' || !r) ? null : r;
      }
    } catch (e) { /* 探针环境无 state */ }
    return null;
  }

  function setRankBusy(on) {
    var b = $('#btnTaRank');
    if (!b) return;
    b.disabled = !!on;
    b.textContent = on ? '⏳ 扫描中…' : '🏆 生成评分排行';
  }


  // ------------------------------------------------------------------
  // 榜单元数据格式化：市值 / 风险 / 压制
  // ------------------------------------------------------------------
  function capText(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return '—';
    var n = Number(v);
    if (n >= 10000) return (n / 10000).toFixed(2) + '<small>万亿</small>';
    if (n >= 100) return n.toFixed(0) + '<small>亿</small>';
    return n.toFixed(1) + '<small>亿</small>';
  }
  function riskCls(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return 'na';
    var n = Number(v);
    return n >= 70 ? 'hi' : (n >= 45 ? 'mid' : 'lo');
  }
  function supCls(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return 'na';
    var n = Number(v);
    return n >= 80 ? 'hi' : (n >= 55 ? 'mid' : 'lo');
  }
  function mCell(k, v, cls) {
    return '<span class="ta-rank-m"><span class="ta-rank-mk">' + esc(k) + '</span>' +
           '<span class="ta-rank-mv ' + cls + '">' + v + '</span></span>';
  }
  function ctxItem(k, v, cls) {
    return '<div class="ta-ctx-item"><div class="ta-ctx-k">' + esc(k) + '</div>' +
           '<div class="ta-ctx-v ' + (cls || '') + '">' + v + '</div></div>';
  }

  function renderRank(d) {
    var host = $('#taRankBox');
    if (!host) return;
    if (!d || d.ok === false) {
      host.innerHTML = '<div class="ta-table-empty">' +
        esc((d && d.note) || '排行失败') + '</div>';
      host.hidden = false;
      return;
    }
    var rows = d.rows || [];
    var isBal = d.metric === 'balance';
    var rg = d.regime || {};
    var h = '<div class="ta-rank-card">';

    h += '<div class="ta-rank-head">';
    h += '<span class="ta-rank-title">🏆 ' + esc(d.metric_label || '评分') + '榜</span>';
    if (rg.label) {
      h += '<span class="ta-rank-rg rg-' + esc(rg.key || 'range') + '">' +
           esc(rg.label) + '口径·' + (rg.auto ? '自动' : '手动') + '</span>';
    }
    h += '<span class="ta-rank-sub">门槛 <b>' + num(d.cutoff, 1) + '</b> 分 · 上榜 <b>' +
         rows.length + '</b> 只';
    if (d.ties > 0) {
      h += '（第 ' + esc(d.top) + ' 名同分 ' + esc(d.ties + 1) + ' 只，已全部列出）';
    }
    h += ' · 扫描 ' + esc(d.scanned) + ' 只 · ' + esc(d.asof || '') + '</span>';
    if (d.cached) {
      h += '<span class="ta-rank-tag">走缓存 · 切换榜单秒出</span>';
    }
    h += '</div>';

    h += '<div class="ta-rank-list">';
    var prev = null;
    rows.forEach(function (r) {
      var main = isBal ? r.balance : r.rebound;
      var other = isBal ? r.rebound : r.balance;
      var otherK = isBal ? '反抽' : '均衡';
      var tie = (prev !== null && main != null && Math.abs(main - prev) < 1e-9);
      prev = main;
      var g = (isBal ? r.grade : r.rebound_grade) || {};
      var t = r.type || {};

      // ★按用户要求：左侧只留「名次 + 代码 + 名称 + 排序分」，右侧三指标
      h += '<div class="ta-rank-row' + (tie ? ' tie' : '') + '">';
      h += '<span class="ta-rank-no">' + esc(r.rank) +
           (tie ? '<i class="ta-tie-mark">=</i>' : '') + '</span>';
      h += '<a class="ta-rank-code" href="#" data-code="' + esc(r.code) + '">' +
           esc(r.code) + '</a>';
      h += '<span class="ta-rank-name" title="' + esc(r.sector || '') + '">' +
           esc(r.name) + '</span>';
      if (r.warn === 'bear_high_balance') {
        h += '<span class="ta-rank-warn" title="熊市里均衡分 ≥65 的票历史上会补跌' +
             '（20/20 年成立），技术强不等于抗跌">补跌</span>';
      }
      h += '<span class="ta-rank-score">' + num(main, 1) + '</span>';
      h += '<span class="ta-rank-metrics">';
      h += mCell('市值', capText(r.mcap_yi), '');
      h += mCell('风险', r.risk == null ? '—' : num(r.risk, 0), riskCls(r.risk));
      h += mCell('压制', r.suppress == null ? '—' : num(r.suppress, 0) + '<small>%</small>',
                 supCls(r.suppress));
      h += '</span>';
      h += '</div>';
    });
    h += '</div>';

    h += '<div class="ta-rank-foot">' + esc(d.disclosure || '') +
         ' <b>' + esc(d.note || '') + '</b>';
    if (rg.advice) {
      h += '<br><b>' + esc(rg.advice) + '</b>';
    }
    h += '</div>';
    h += '</div>';

    host.innerHTML = h;
    host.hidden = false;
    bindCodeLinks(host);
  }

  function runRank() {
    var metricEl = $('#taRankMetric');
    var topEl = $('#taRankTop');
    var metric = (metricEl && metricEl.value) || 'balance';
    var top = parseInt((topEl && topEl.value), 10) || 10;
    var ex = currentRange();
    var scopeTxt = ex ? ({ SH: '沪市', SZ: '深市', BJ: '北交所' }[ex] || ex) : '全市场';

    msg('正在扫描' + scopeTxt + '，按' +
        (metric === 'auto' ? '市场状态自动选择尺子' :
         (metric === 'balance' ? '均衡分' : '反抽分')) +
        '排序（约 10-18 秒）…', 'info');
    setRankBusy(true);
    startPoll();
    postJson('/api/ta/rank', {
      metric: metric, top: top, exchange: ex, cfg: readCfg(),
      regime: currentRegime()
    }, 300000)
      .then(function (d) {
        stopPoll();
        setRankBusy(false);
        if (!d || d.ok === false) {
          msg('排行失败：' + ((d && d.note) || '未知错误'), 'err');
          renderRank(d);
          return;
        }
        renderRank(d);
        msg('排行完成 · ' + scopeTxt + ' · ' + d.n + ' 只上榜（门槛 ' +
            num(d.cutoff, 1) + ' 分）· 耗时 ' + esc(d.elapsed) + 's', 'ok');
      })
      .catch(function (e) {
        stopPoll();
        setRankBusy(false);
        msg('排行请求失败：' + String((e && e.message) || e), 'err');
      });
  }

  // ------------------------------------------------------------------
  // 渲染：维度
  // ------------------------------------------------------------------
  function renderDims(r) {
    var dims = r.dims || [];
    var h = '<div class="ta-dims">';
    dims.forEach(function (d) {
      var pct = Math.max(0, Math.min(100, Number(d.pct) || 0));
      var cls = pct >= 70 ? 'hi' : (pct >= 40 ? 'mid' : 'lo');
      h += '<div class="ta-dim">';
      h += '<span class="ta-dim-name">' + esc(d.label) + '</span>';
      h += '<span class="ta-dim-bar"><i class="ta-dim-fill ' + cls +
           '" style="width:' + pct.toFixed(1) + '%"></i></span>';
      h += '<span class="ta-dim-val">' + num(d.score, 1) +
           ' <small>/ ' + esc(d.max) + '</small></span>';
      h += '<span class="ta-dim-desc">' + esc(d.desc) + '</span>';
      h += '</div>';
    });
    h += '</div>';
    return h;
  }

  // ------------------------------------------------------------------
  // 渲染：信号
  // ------------------------------------------------------------------
  var GROUP_ORDER = ['趋势', '动量', '位置', '量能', '形态'];

  function renderSignals(r) {
    var sigs = r.signals || [];
    var groups = {};
    sigs.forEach(function (s) {
      var g = s.group || '其它';
      (groups[g] = groups[g] || []).push(s);
    });
    var keys = GROUP_ORDER.filter(function (g) { return groups[g]; });
    Object.keys(groups).forEach(function (g) {
      if (keys.indexOf(g) < 0) keys.push(g);
    });

    var h = '<div class="ta-sig-groups">';
    keys.forEach(function (g) {
      h += '<div class="ta-sig-group">';
      h += '<div class="ta-sig-gname">' + esc(g) + '</div>';
      h += '<div class="ta-sig-chips">';
      groups[g].forEach(function (s) {
        var cls = 'ta-chip' + (s.on ? ' on' : '') + (s.on && s.strong ? ' strong' : '');
        var title = s.desc + (s.on && s.days ? '（已持续 ' + s.days + ' 个交易日）' : '');
        h += '<span class="' + cls + '" title="' + esc(title) + '">';
        if (s.strong) h += '<span class="ta-chip-star">★</span>';
        h += esc(s.label);
        if (s.on && s.days) {
          h += '<span class="ta-chip-days">' + esc(s.days) + 'd</span>';
        }
        h += '</span>';
      });
      h += '</div></div>';
    });
    h += '</div>';
    return h;
  }

  // ------------------------------------------------------------------
  // 渲染：反抽评分（第二把尺子）
  // ------------------------------------------------------------------
  function renderRebound(r) {
    var rb = r.rebound;
    if (!rb) return '';
    var g = rb.gate || {};
    var dims = rb.dims || [];
    var cal = rb.calib || {};
    var gv = Math.max(0, Math.min(100, Number(g.value) || 0));

    var h = '<div class="ta-sec">';
    h += '<div class="ta-sec-head">';
    h += '<span class="ta-sec-title">反抽评分 · 第二把尺子</span>';
    h += '<span class="ta-sec-note">6 维加权是 <b>算术累加</b>：趋势一弱，那 25 分就直接扣掉，别的维度补不回来。'
       + '可「弱趋势 + 强劲反抽」恰恰是超跌后最常见的形态——此时趋势、动量、位置同时是残的，'
       + '只有量能会亮起来。所以这里用第二把尺子单独量，<b>不与均衡分相加</b>。</span>';
    h += '</div>';

    h += '<div class="ta-reb">';
    h += '<div class="ta-reb-gate">';
    h += '<div class="ta-reb-gate-k">超跌深度门控 <b>' + num(gv, 1) + '</b> / 100</div>';
    h += '<div class="ta-reb-gate-bar"><i style="width:' + gv.toFixed(0) + '%"></i></div>';
    h += '<div class="ta-reb-formula">反抽分 =（转强动能 + 止跌确认 + 反抽力度）× 门控<sup>0.55</sup><br>'
       + esc(g.desc || '') + '</div>';
    h += '</div>';

    h += '<div class="ta-reb-dims"><div class="ta-dims">';
    dims.forEach(function (d) {
      var pct = Math.max(0, Math.min(100, Number(d.pct) || 0));
      var cls = pct >= 70 ? 'hi' : (pct >= 40 ? 'mid' : 'lo');
      h += '<div class="ta-dim">';
      h += '<span class="ta-dim-name">' + esc(d.label) + '</span>';
      h += '<span class="ta-dim-bar"><i class="ta-dim-fill ' + cls +
           '" style="width:' + pct.toFixed(1) + '%"></i></span>';
      h += '<span class="ta-dim-val">' + num(d.score, 1) +
           ' <small>/ ' + esc(d.max) + '</small></span>';
      h += '<span class="ta-dim-desc">' + esc(d.desc) + '</span>';
      h += '</div>';
    });
    h += '</div></div>';
    h += '</div>';

    if (cal.bands && cal.bands.length) {
      h += '<div class="ta-table-wrap"><table class="ta-table"><thead><tr>' +
           '<th>反抽分区间（均衡分 &lt;50 的子样本）</th><th>20 日胜率</th><th>20 日均值</th>' +
           '</tr></thead><tbody>';
      cal.bands.forEach(function (b) {
        h += '<tr><td class="l">' + esc(b.label) + '</td>' +
             '<td>' + num(b.win, 1) + '%</td>' +
             '<td class="' + signCls(b.avg) + '">' + pctText(b.avg) + '</td></tr>';
      });
      h += '</tbody></table></div>';
      h += '<div class="ta-sec-note">标定口径：' + esc(cal.note || '') +
           ' 覆盖 ' + esc(cal.sample) + ' 个历史时点，' + esc(cal.year_pos) +
           ' 个年份里成立。但 2009 / 2014 这类大牛市里反抽组反而跑输——牛市里强者更强。</div>';
    }

    h += '</div>';
    return h;
  }

  // ------------------------------------------------------------------
  // 渲染：统计卡
  // ------------------------------------------------------------------
  function statCard(k, v, sub, cls) {
    var h = '<div class="ta-stat">';
    h += '<div class="ta-stat-k">' + esc(k) + '</div>';
    h += '<div class="ta-stat-v ' + (cls || '') + '">' + v + '</div>';
    if (sub) h += '<div class="ta-stat-sub">' + sub + '</div>';
    h += '</div>';
    return h;
  }

  function statCards(st, opts) {
    opts = opts || {};
    var n = st.n == null ? 0 : st.n;
    var h = '<div class="ta-stats">';
    h += statCard('样本数', String(n),
                  '样本 <10 时结论不可信', n >= 10 ? '' : 'neg');

    var win = st.win;
    h += statCard('胜率', win == null ? '—' : num(win, 1) + '%',
                  '5 日收益 > 0 的占比');

    var avg = st.avg;
    h += statCard('平均收益', pctText(avg), 'T+1 开盘 → T+5 收盘',
                  signCls(avg));

    h += statCard('中位收益', pctText(st.med), '剔除极端值后更稳',
                  signCls(st.med));

    if (opts.showAlpha !== false) {
      var a = st.alpha;
      var baseTxt = opts.baseLabel || '基准';
      h += statCard('超额', a == null ? '—' : ppText(a),
                    baseTxt + ' ' + pctText(st.base), signCls(a));
    }

    if (st.ci_low != null && st.ci_high != null) {
      var ciCls = (st.ci_low > 0 || st.ci_high < 0) ? '' : 'neg';
      h += statCard('95% 置信区间',
                    pctText(st.ci_low) + ' ~ ' + pctText(st.ci_high),
                    (st.ci_low > 0 || st.ci_high < 0) ? '不跨 0，统计上显著' : '跨 0，与随机无异',
                    ciCls);
    }
    h += '</div>';
    return h;
  }

  // ------------------------------------------------------------------
  // 渲染：个股回测（两个口径并排）
  // ------------------------------------------------------------------
  function renderStockBt(r) {
    var bt = r.bt_stock;
    if (!bt) return '';
    var h = '';
    h += '<div class="ta-sec">';
    h += '<div class="ta-sec-head">';
    h += '<span class="ta-sec-title">① 个股历史回测</span>';
    h += '<span class="ta-sec-note">在这只股票的 <b>' + esc(r.bars) +
         '</b> 根历史 K 线上，找出与「当前信号组合」相似的时点；' +
         '统计信号后 ' + esc(bt.horizon) + ' 个交易日的表现。</span>';
    h += '</div>';

    var strict = bt.strict || {};
    h += '<div class="ta-pair">';

    // 宽松口径
    h += '<div class="ta-pane">';
    h += '<div class="ta-pane-head">';
    h += '<span class="ta-pane-title">宽松口径</span>';
    h += '<span class="ta-pane-tag">共同信号 ≥ ' + esc(bt.min_signals || 2) + ' 个</span>';
    h += '<div class="ta-pane-desc">要求与当前信号有至少 ' + esc(bt.min_signals || 2) +
         ' 个重合，样本多但「像」的程度参差。本轮共 ' + esc(bt.n_total) +
         ' 次（已按 ' + esc(bt.horizon) + ' 日冷却去重），占全部可评估日的 ' +
         esc(bt.event_rate) + '%。</div>';
    h += '</div>';
    h += statCards(bt, { baseLabel: '无信号日' });
    h += '<div class="ta-sec-note">对照 = 该股「没有相似信号」的其他交易日，' +
         '共 ' + esc(bt.control_n) + ' 天。这剔除了个股自身长期漂移，' +
         '<b>但没有剔除市场整体的时间波动</b>。</div>';
    h += '</div>';

    // 高相似口径
    h += '<div class="ta-pane">';
    h += '<div class="ta-pane-head">';
    h += '<span class="ta-pane-title">高相似口径</span>';
    h += '<span class="ta-pane-tag">重合 ≥ ' + esc(bt.strict_n) + ' 个（' +
         Math.round((bt.strict_n / Math.max(1, bt.ref_n)) * 100) + '%）</span>';
    h += '<div class="ta-pane-desc">要求重合当前信号集的 60% 以上。' +
         '样本更小，但才真正接近「同一个技术形态」。</div>';
    h += '</div>';
    h += statCards(strict, { baseLabel: '无信号日' });
    h += '</div>';

    h += '</div>';   // ta-pair

    // 多周期
    var multi = bt.multi || {};
    var mkeys = Object.keys(multi);
    if (mkeys.length) {
      h += '<div class="ta-table-wrap"><table class="ta-table"><thead><tr>' +
           '<th>持有周期</th><th>样本</th><th>胜率</th><th>平均收益</th>' +
           '</tr></thead><tbody>';
      mkeys.forEach(function (k) {
        var m = multi[k];
        h += '<tr><td>' + esc(k) + '</td><td>' + esc(m.n) + '</td><td>' +
             num(m.win, 1) + '%</td><td class="' + signCls(m.avg) + '">' +
             pctText(m.avg) + '</td></tr>';
      });
      h += '</tbody></table></div>';
    }

    // 事件明细
    var evs = bt.events || [];
    if (evs.length) {
      h += '<details class="ta-fold"><summary>历史事件明细（最近 ' +
           evs.length + ' 条，共 ' + esc(bt.n_total) + ' 条）</summary>';
      h += '<div class="ta-fold-body"><table class="ta-table"><thead><tr>' +
           '<th>信号日</th><th>共同信号</th><th>相似度</th><th>后 5 日</th>' +
           '</tr></thead><tbody>';
      evs.forEach(function (e) {
        var labels = (e.keys || []).map(function (k) {
          return (SIG_LABEL[k] || k);
        }).join('、');
        h += '<tr><td>' + esc(e.date) + '</td>' +
             '<td class="l ta-sig-mini">' + esc(labels) + '</td>' +
             '<td>' + (e.sim == null ? '—' : num(e.sim * 100, 0) + '%') + '</td>' +
             '<td class="' + signCls(e.ret) + '">' + pctText(e.ret) + '</td></tr>';
      });
      h += '</tbody></table></div></details>';
    }

    h += '</div>';
    return h;
  }

  // 信号 key → 中文名（由 /api/ta/meta 填充，见 fillMeta）

  // ------------------------------------------------------------------
  // 渲染：横截面回测
  // ------------------------------------------------------------------
  function renderCross(r) {
    var cr = r.bt_cross;
    // null = 用户没勾「全市场相似形态回测」→ 整块不渲染
    if (cr === null || cr === undefined) return '';
    var h = '<div class="ta-sec">';
    h += '<div class="ta-sec-head">';
    h += '<span class="ta-sec-title">② 全市场相似形态回测</span>';
    h += '<span class="ta-sec-note">最近 <b>' + esc(cr.window || 21) +
         '</b> 个交易日内，全市场出现与当前信号组合「至少 2 个共同信号」的股票，' +
         '在信号出现后 ' + esc((cr.stat || {}).horizon || 5) + ' 个交易日的表现。</span>';
    h += '</div>';

    if (!cr || cr.ok === false) {
      h += '<div class="ta-table-empty">' + esc((cr && (cr.note || cr.hint)) || '横截面回测未执行') + '</div></div>';
      return h;
    }

    var st = cr.stat || {};
    var as = st.alpha_stat || {};
    var strict = st.strict || {};
    var sas = strict.alpha_stat || {};

    h += '<div class="ta-stats">';
    h += statCard('命中股票', String(st.n || 0),
                  '扫描 ' + esc(st.scanned || 0) + ' 只中命中');
    h += statCard('高相似（≥60%）', String(strict.n || 0), '真正算「像」的子集');
    h += statCard('同期市场', pctText(st.base), '按信号日逐日对齐的平均');
    h += statCard('平均超额',
                  as.avg == null ? '—' : ppText(as.avg),
                  '信号组 − 同期市场', signCls(as.avg));
    h += statCard('超额胜率',
                  as.win == null ? '—' : num(as.win, 1) + '%',
                  '跑赢同期市场的比例');
    h += statCard('高相似超额',
                  sas.avg == null ? '—' : ppText(sas.avg),
                  '越像是否越有效', signCls(sas.avg));
    h += '</div>';

    var excl = st.excluded_incomplete || 0;
    if (excl) {
      h += '<div class="ta-sec-note">⚠ 另有 ' + esc(excl) +
           ' 次「信号出现但持有期还没走完」的命中被剔除（信号日距今不足 ' +
           esc(st.horizon) + ' 个交易日）。若强行计入，会用到不完整的未来数据。</div>';
    }

    var rows = cr.rows || [];
    if (!rows.length) {
      h += '<div class="ta-table-empty">最近 ' + esc(cr.window || 21) +
           ' 个交易日内没有其他股票出现 ≥2 个相同信号。</div>';
      h += '</div>';
      return h;
    }

    // ★样本股票默认折叠：300 行明细铺在页面上会盖掉结论，用户要先看数、再看票
    h += '<details class="ta-fold"><summary>样本股票明细（' + esc(cr.n) +
         ' 只，按相似度排序）</summary>';
    h += '<div class="ta-fold-body"><table class="ta-table"><thead><tr>';
    h += '<th>代码</th><th>名称</th><th>信号日</th><th>共同</th><th>相似度</th>' +
         '<th>后 5 日</th><th>同期市场</th><th>超额</th><th>共同信号</th>';
    h += '</tr></thead><tbody>';
    rows.forEach(function (x) {
      h += '<tr>';
      h += '<td><a class="ta-code-link" href="#" data-code="' + esc(x['代码']) + '">' +
           esc(x['代码']) + '</a></td>';
      h += '<td class="l">' + esc(x['名称']) + '</td>';
      h += '<td>' + esc(x['信号日']) + '</td>';
      h += '<td>' + esc(x['共同信号数']) + '</td>';
      h += '<td>' + num(x['相似度'], 0) + '%</td>';
      h += '<td class="' + signCls(x['后5日收益(%)']) + '">' +
           pctText(x['后5日收益(%)']) + '</td>';
      h += '<td>' + pctText(x['同期市场(%)']) + '</td>';
      h += '<td class="' + signCls(x['超额(%)']) + '">' + ppText(x['超额(%)']) + '</td>';
      h += '<td class="l ta-sig-mini">' + esc(x['共同信号']) + '</td>';
      h += '</tr>';
    });
    h += '</tbody></table>';
    if (cr.n > cr.n_shown) {
      h += '<div class="ta-sec-note">表格只显示前 ' + esc(cr.n_shown) +
           ' 只（按相似度排序），完整 ' + esc(cr.n) + ' 只在 CSV 里。</div>';
    }
    h += '</div></details>';
    h += '</div>';
    return h;
  }

  // ------------------------------------------------------------------
  // 渲染：评分分档检验
  // ------------------------------------------------------------------
  function renderScoreBt(r) {
    var sb = r.score_bt;
    if (!sb || !(sb.buckets || []).length) return '';
    var buckets = sb.buckets || [];
    var maxAbs = 0.1;
    buckets.forEach(function (b) {
      if (b.avg != null) maxAbs = Math.max(maxAbs, Math.abs(b.avg));
    });

    var h = '<div class="ta-sec">';
    h += '<div class="ta-sec-head">';
    h += '<span class="ta-sec-title">③ 评分有效性检验</span>';
    h += '<span class="ta-sec-note">把该股历史上每一天的分数都算出来，按档统计未来 ' +
         esc(sb.horizon || 5) + ' 日收益。<b>这是检验「分数」本身有没有用的唯一办法</b>，' +
         '不是装饰。</span>';
    h += '</div>';

    h += '<div class="ta-buckets">';
    buckets.forEach(function (b) {
      var w = b.avg == null ? 0 : Math.min(100, Math.abs(b.avg) / maxAbs * 100);
      var cls = signCls(b.avg);
      h += '<div class="ta-bucket">';
      h += '<div class="ta-bucket-label">' + esc(b.label) + '</div>';
      h += '<div class="ta-bucket-avg ' + cls + '">' +
           (b.avg == null ? '—' : pctText(b.avg)) + '</div>';
      h += '<div class="ta-bucket-bar"><i style="width:' + w.toFixed(0) +
           '%;background:' + (cls === 'pos' ? 'var(--good)' : (cls === 'neg' ? 'var(--bad)' : 'var(--neutral)')) +
           '"></i></div>';
      h += '<div class="ta-bucket-sub">' + esc(b.n) + ' 天' +
           (b.win == null ? '' : ' · 胜率 ' + num(b.win, 1) + '%') + '</div>';
      h += '</div>';
    });
    h += '</div>';

    var rho = sb.spearman;
    var warn = (rho != null && rho >= 0.6) || (sb.verdict || '').indexOf('没有区分度') >= 0
      || (sb.verdict || '').indexOf('无法判断') >= 0;
    h += '<div class="ta-verdict' + (warn ? ' warn' : '') + '">' +
         (rho == null ? '' : '<b>秩相关 ρ = ' + num(rho, 2) + '</b>　') +
         esc(sb.verdict || '') + '</div>';

    h += '</div>';
    return h;
  }

  // ------------------------------------------------------------------
  // 渲染：提示与披露
  // ------------------------------------------------------------------
  function renderNotes(r) {
    var notes = r.notes || [];
    var h = '<div class="ta-notes">';
    h += '<b>口径与边界</b>';
    h += '<ul>';
    (notes.length ? notes : []).forEach(function (x) {
      h += '<li>' + esc(x) + '</li>';
    });
    h += '<li>' + esc(r.disclosure || '') + '</li>';
    h += '<li>均衡分口径：' + Object.keys(r.weights || {}).map(function (k) {
      return k + ' ' + r.weights[k];
    }).join(' / ') + '，合计 100。反抽分是独立的第二把尺子，不与它相加。</li>';
    h += '</ul></div>';
    return h;
  }

  // ------------------------------------------------------------------
  // 主渲染
  // ------------------------------------------------------------------
  function render(r) {
    var host = $('#taResult');
    if (!host) return;
    LAST = r;
    var h = '';

    // ★不可选时也要把评分与信号画出来，否则用户不知道「差在哪」
    h += renderHero(r);
    // 市场背景紧跟 hero：先说明「现在是什么市」，再给这只票的结论
    h += renderMarket(r);
    h += renderCtx(r);
    // 一句话总结：先给结论，再给拆解
    h += renderSummary(r);
    h += '<div class="ta-sec"><div class="ta-sec-head">' +
         '<span class="ta-sec-title">维度得分</span>' +
         '<span class="ta-sec-note">6 个维度加权，合计 100 分。鼠标悬停可看口径。</span>' +
         '</div>' + renderDims(r) + '</div>';

    h += renderRebound(r);

    h += '<div class="ta-sec"><div class="ta-sec-head">' +
         '<span class="ta-sec-title">技术信号</span>' +
         '<span class="ta-sec-note">共 14 个，★ 为形态级信号。灰色 = 当前未成立。</span>' +
         '</div>' + renderSignals(r) + '</div>';

    if (r.selectable === false) {
      h += renderBlock(r);
      host.innerHTML = h;
      host.hidden = false;
      bindCodeLinks(host);
      return;
    }

    h += renderStockBt(r);
    h += renderCross(r);
    h += renderScoreBt(r);
    h += renderNotes(r);

    host.innerHTML = h;
    host.hidden = false;
    bindCodeLinks(host);
  }

  // 代码点击 → 跳行情查看（不依赖 window.switchTab，它永远是 undefined）
  function bindCodeLinks(host) {
    Array.prototype.forEach.call(host.querySelectorAll('[data-code]'), function (a) {
      a.addEventListener('click', function (ev) {
        ev.preventDefault();
        var code = a.getAttribute('data-code');
        var kInp = document.querySelector('#klineCode');
        if (kInp) kInp.value = code;
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

  // ------------------------------------------------------------------
  // 运行
  // ------------------------------------------------------------------
  function readCfg() {
    var cfg = {};
    var ms = $('#taMinSig');
    if (ms) cfg.MIN_SIGNALS = parseInt(ms.value, 10) || 2;
    var hz = $('#taHorizon');
    if (hz) cfg.HORIZON = parseInt(hz.value, 10) || 5;
    var cw = $('#taCrossWin');
    if (cw) cfg.CROSS_WINDOW = parseInt(cw.value, 10) || 21;
    return cfg;
  }

  function setBusy(on, text) {
    var btn = $('#btnTaRun');
    if (!btn) return;
    btn.disabled = !!on;
    btn.textContent = on ? '⏳ ' + (text || '分析中…') : '▶ 开始分析';
  }

  function currentRegime() {
    var el = $('#taRegime');
    return (el && el.value) || 'auto';
  }

  function run() {
    var codeEl = $('#taCode');
    var code = codeEl ? String(codeEl.value || '').trim() : '';
    if (!/^\d{6}$/.test(code)) {
      msg('请输入 6 位股票代码（如 600519）', 'warn');
      return;
    }
    var doCross = $('#taDoCross') ? !!$('#taDoCross').checked : true;
    var doScoreBt = $('#taDoScore') ? !!$('#taDoScore').checked : true;
    var rg = currentRegime();

    msg('正在读取 ' + code + ' 的历史行情并计算指标' +
        (rg === 'auto' ? '（市场背景自动识别）' : '（市场背景：手动 ' +
         (({ bull: '牛市', bear: '熊市', range: '震荡市' })[rg] || rg) + '）') + '…', 'info');
    setBusy(true, doCross ? '分析中（含全市场扫描约 10-15s）' : '分析中');
    startPoll();

    postJson('/api/ta/run', {
      code: code, cfg: readCfg(), regime: rg,
      do_cross: doCross, do_score_bt: doScoreBt
    }, 300000)
      .then(function (r) {
        stopPoll();
        setBusy(false);
        if (!r || (!r.ok && !r.score && !r.grade)) {
          msg('分析失败：' + ((r && (r.reason || r.error)) || '未知错误'), 'err');
          var host0 = $('#taResult');
          if (host0) host0.hidden = true;
          return;
        }
        if (r.ok === false && !r.grade) {
          msg('分析失败：' + (r.reason || '未知错误'), 'err');
          return;
        }
        if (r.selectable === false) {
          msg('该股命中信号不足，不可回测（详见下方说明）', 'warn');
        } else {
          msg('完成 · ' + r.code + ' ' + (r.name || '') + ' · ' +
              num(r.score, 1) + ' 分 · 命中 ' + r.signal_count + ' 个信号', 'ok');
        }
        render(r);
      })
      .catch(function (e) {
        stopPoll();
        setBusy(false);
        var m = String((e && e.message) || e);
        msg('请求失败：' + m + '（若超过 5 分钟请检查后台日志）', 'err');
      });
  }

  // ------------------------------------------------------------------
  // CSV 导出
  // ------------------------------------------------------------------
  function csvCell(v) {
    var s = String(v == null ? '' : v)
      .replace(/<[^>]+>/g, '')
      .replace(/"/g, '""');
    return '"' + s + '"';
  }

  function exportCsv() {
    if (!LAST || (!LAST.grade && LAST.ok === false && !LAST.score)) {
      msg('暂无可导出的结果', 'warn');
      return;
    }
    var r = LAST;
    var L = [];
    var push = function (arr) { L.push(arr.map(csvCell).join(',')); };

    push(['技术分析报告']);
    push(['生成时间', new Date().toLocaleString()]);
    push([]);
    push(['=== 概览 ===']);
    var rb0 = r.rebound || {};
    push(['代码', '名称', '板块', '数据日', '收盘', '涨跌幅(%)',
          '均衡分', '均衡评级', '反抽分', '反抽档', '形态类型', '命中信号数', '数据源']);
    push([r.code, r.name, r.sector, r.asof, r.price, r.change, r.score,
          (r.grade || {}).label, rb0.score, (rb0.grade || {}).label,
          (rb0.type || {}).label, r.signal_count, r.source]);
    if (r.selectable === false) {
      push([]);
      push(['不可回测原因', r.reason]);
      push(['说明', r.hint || '']);
      if (r.rebound_hint) push(['反抽判断', r.rebound_hint]);
    }
    // 一句话总结（与页面顶部同口径）
    push([]);
    push(['=== 一句话总结 ===']);
    push(['口径', '周期(交易日)', '样本', '胜率(%)', '平均收益(%)', '基准(%)', '超额(pp)']);
    if (r.bt_stock) {
      var b0 = r.bt_stock;
      push(['个股历史（同形态事件）', b0.horizon, b0.n, b0.win, b0.avg, b0.base, b0.alpha]);
    }
    var crs0 = (r.bt_cross || {}).stat;
    if (crs0) {
      push(['近一个月（全市场相似形态）', crs0.horizon, crs0.n, crs0.win, crs0.avg,
            crs0.base, (crs0.alpha_stat || {}).avg]);
      push(['  其中高相似子集(≥60%)', crs0.horizon, (crs0.strict || {}).n,
            (crs0.strict || {}).win, (crs0.strict || {}).avg, crs0.base,
            ((crs0.strict || {}).alpha_stat || {}).avg]);
    }

    push([]);
    push(['=== 维度得分（均衡分 · 6 维加权） ===']);
    push(['维度', '得分', '满分', '占比(%)', '口径']);
    (r.dims || []).forEach(function (d) {
      push([d.label, d.score, d.max, d.pct, d.desc]);
    });

    if (r.rebound) {
      var rb = r.rebound;
      push([]);
      push(['=== 反抽评分（第二把尺子，不与均衡分相加） ===']);
      push(['反抽分', rb.score, '档位', (rb.grade || {}).label,
            '形态类型', (rb.type || {}).label]);
      push(['超跌深度门控(0~100)', (rb.gate || {}).value]);
      push(['维度', '得分', '满分', '占比(%)', '口径']);
      (rb.dims || []).forEach(function (d) {
        push([d.label, d.score, d.max, d.pct, d.desc]);
      });
      if (rb.calib && (rb.calib.bands || []).length) {
        push([]);
        push(['标定口径（全市场 ' + rb.calib.sample + ' 个时点，'
              + rb.calib.year_pos + ' 个年份成立）']);
        push(['反抽分区间', '20 日胜率(%)', '20 日均值(%)']);
        rb.calib.bands.forEach(function (b) {
          push([b.label, b.win, b.avg]);
        });
      }
    }
    push([]);
    push(['=== 技术信号 ===']);
    push(['信号', '分组', '是否成立', '形态级', '起始日', '已持续(交易日)', '说明']);
    (r.signals || []).forEach(function (s) {
      push([s.label, s.group, s.on ? '成立' : '未成立', s.strong ? '是' : '否',
            s.since || '', s.days == null ? '' : s.days, s.desc]);
    });

    if (r.bt_stock) {
      var bt = r.bt_stock;
      push([]);
      push(['=== ① 个股历史回测 ===']);
      push(['口径', '样本', '胜率(%)', '平均收益(%)', '中位(%)', '超额(pp)', '基准(%)',
            '95%下限', '95%上限', '最好(%)', '最差(%)']);
      push(['宽松(共同信号≥' + (bt.min_signals || 2) + ')', bt.n, bt.win, bt.avg, bt.med,
            bt.alpha, bt.base, bt.ci_low, bt.ci_high, bt.best, bt.worst]);
      var s2 = bt.strict || {};
      push(['高相似(≥' + bt.strict_n + ')', s2.n, s2.win, s2.avg, s2.med,
            s2.alpha, s2.base, s2.ci_low, s2.ci_high, s2.best, s2.worst]);
      push([]);
      push(['多周期对比', '样本', '胜率(%)', '平均收益(%)']);
      Object.keys(bt.multi || {}).forEach(function (k) {
        var m = bt.multi[k];
        push([k, m.n, m.win, m.avg]);
      });
      push([]);
      push(['历史事件明细', '信号日', '共同信号数', '相似度(%)', '后5日收益(%)', '共同信号']);
      (bt.events || []).forEach(function (e) {
        push(['', e.date, e.n, e.sim == null ? '' : (e.sim * 100).toFixed(1), e.ret,
              (e.keys || []).map(function (k) { return SIG_LABEL[k] || k; }).join('、')]);
      });
    }

    var cr = r.bt_cross;
    if (cr && cr.ok && (cr.rows || []).length) {
      var st = cr.stat || {};
      push([]);
      push(['=== ② 全市场相似形态回测 ===']);
      push(['观察窗', (cr.window || 21) + ' 个交易日', '区间', cr.date_range || '']);
      push(['样本', st.n, '扫描', st.scanned, '剔除未走完', st.excluded_incomplete,
            '同期市场均值(%)', st.base]);
      var as = st.alpha_stat || {};
      push(['平均超额(pp)', as.avg, '超额胜率(%)', as.win,
            '95%下限', as.ci_low, '95%上限', as.ci_high]);
      var ss = st.strict || {};
      var sas = ss.alpha_stat || {};
      push(['高相似子集', '样本', ss.n, '超额(pp)', sas.avg, '胜率(%)', sas.win]);
      push([]);
      push(['代码', '名称', '信号日', '共同信号数', '相似度(%)', '后5日收益(%)',
            '同期市场(%)', '超额(pp)', '共同信号']);
      cr.rows.forEach(function (x) {
        push([x['代码'], x['名称'], x['信号日'], x['共同信号数'], x['相似度'],
              x['后5日收益(%)'], x['同期市场(%)'], x['超额(%)'], x['共同信号']]);
      });
    }

    if (r.score_bt && (r.score_bt.buckets || []).length) {
      push([]);
      push(['=== ③ 评分有效性检验（未来 ' + (r.score_bt.horizon || 5) + ' 日） ===']);
      push(['分档', '样本(天)', '胜率(%)', '平均收益(%)']);
      r.score_bt.buckets.forEach(function (b) {
        push([b.label, b.n, b.win, b.avg]);
      });
      push(['秩相关 rho', r.score_bt.spearman]);
      push(['结论', r.score_bt.verdict]);
    }

    if ((r.notes || []).length) {
      push([]);
      push(['=== 提示 ===']);
      r.notes.forEach(function (x) { push(['', x]); });
    }
    push([]);
    push(['免责', r.disclosure || '']);

    var text = '\ufeff' + L.join('\r\n');
    var blob = new Blob([text], { type: 'text/csv;charset=utf-8;' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'ta_' + (r.code || 'stock') + '_' + (r.asof || '') + '.csv';
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
      if (a.parentNode) a.parentNode.removeChild(a);
    }, 100);
  }

  // ------------------------------------------------------------------
  // 初始化
  // ------------------------------------------------------------------
  function fillMeta(m) {
    META = m;
    SIG_LABEL = {};
    (m.signals || []).forEach(function (s) { SIG_LABEL[s.key] = s.label; });
  }

  function loadMeta() {
    return req('/api/ta/meta', {}, 30000).then(function (j) {
      fillMeta(j || {});
    }).catch(function () {
      // 失败时给兜底结构，保证页面仍可用（信号名会退化成 key）
      fillMeta({ signals: [], defaults: {}, weights: {} });
    });
  }

  // ------------------------------------------------------------------
  // 市场背景：进页面就先问一次，把「自动识别」的选项写成「自动识别（当前 熊市）」
  // ------------------------------------------------------------------
  var REGIME_NOW = null;

  function loadRegime() {
    return req('/api/ta/regime', {}, 25000).then(function (j) {
      REGIME_NOW = j || null;
      var sel = $('#taRegime');
      if (sel && sel.options && sel.options.length) {
        var o0 = sel.options[0];
        if (o0 && o0.value === 'auto') {
          o0.textContent = '自动识别（当前 ' + ((j && j.label) || '未知') + '）';
        }
      }
      return j;
    }).catch(function () { return null; });
  }

  function init() {
    if (INITED) return;
    INITED = true;

    loadMeta();
    loadRegime();

    var btn = $('#btnTaRun');
    if (btn) btn.addEventListener('click', run);
    var exp = $('#btnTaExport');
    if (exp) exp.addEventListener('click', exportCsv);
    var rk = $('#btnTaRank');
    if (rk) rk.addEventListener('click', runRank);
    var inp = $('#taCode');
    if (inp) {
      inp.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') run();
      });
    }
    msg('输入 6 位股票代码后点「开始分析」。首次会读长历史，约 0.1s；勾选全市场扫描约 10-15s。', 'info');
  }

  // ------------------------------------------------------------------
  // 挂载：事件委托（app.js 的 switchTab 无需知道本模块存在）
  // ------------------------------------------------------------------
  function hookTab() {
    document.addEventListener('click', function (ev) {
      var el = ev.target;
      while (el && el !== document) {
        if (el.classList && el.classList.contains('tab') &&
            el.getAttribute('data-tab') === 'ta') {
          init();
          return;
        }
        el = el.parentNode;
      }
    }, true);
  }

  // 供无头 DOM 探针直接驱动
  window.__taInit = init;
  window.__taRun = run;
  window.__taExport = exportCsv;
  window.__taRank = runRank;
  window.__taRender = render;
  window.__taRenderRank = renderRank;
  window.__taCsvCell = csvCell;
  window.__taState = function () {
    return {
      inited: INITED, hasResult: !!LAST,
      signals: META ? (META.signals || []).length : 0,
      code: LAST ? LAST.code : null,
      score: LAST ? LAST.score : null,
      regime: REGIME_NOW ? REGIME_NOW.key : null,
      rankShown: !!($('#taRankBox') && !$('#taRankBox').hidden)
    };
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', hookTab);
  } else {
    hookTab();
  }
})();
