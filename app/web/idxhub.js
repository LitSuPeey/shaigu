/* ==========================================================================
   指数指标（Index Hub）· 全球风险快照悬浮窗 + 页签
   —— 独立 IIFE，不污染全局；只对外暴露 window.initIdxhub / window.IdxHub。
   —— 依赖：无（自绘 canvas K 线，不依赖 kline.js，避免影响其 69 项探针）。
   ========================================================================== */
(function () {
  'use strict';

  var API = {
    panel: function (force) {
      return fetch('/api/idxhub/panel' + (force ? '?force=1' : '')).then(function (r) { return r.json(); });
    },
    kline: function (key, days) {
      return fetch('/api/idxhub/kline?key=' + encodeURIComponent(key) + '&days=' + (days || 180))
        .then(function (r) { return r.json(); });
    },
    meta: function () { return fetch('/api/idxhub/meta').then(function (r) { return r.json(); }); }
  };

  var IX = {
    data: null,
    loading: false,
    err: null,
    inited: false,
    watchOpen: false,
    chart: { key: 'vix', days: 180, bars: null, busy: false, hover: -1 },
    flashOpened: false
  };

  var HERO_TABS = [
    { k: 'vix', n: 'VIX' }, { k: 'brent', n: '布伦特' }, { k: 'a50', n: 'A50期货' },
    { k: 'hsi', n: '恒生' }, { k: 'sh000300', n: '沪深300' },
    { k: 'IF0', n: 'IF 主连' }, { k: 'IH0', n: 'IH 主连' },
    { k: 'IC0', n: 'IC 主连' }, { k: 'IM0', n: 'IM 主连' }
  ];

  var LEVEL_TXT = { crisis: '危机', warn: '预警', watch: '观察' };
  var BIAS_TXT = {
    repair: '统计偏修复', weaken: '统计偏走弱', choppy: '反弹难赚钱',
    flat: '统计无偏向', unknown: '样本不足'
  };

  // ---------- 工具 ----------
  function $(s, root) { return (root || document).querySelector(s); }
  function $$(s, root) { return Array.prototype.slice.call((root || document).querySelectorAll(s)); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function nf(v, dec) {
    if (v == null || !isFinite(v)) return '—';
    dec = dec == null ? 2 : dec;
    var s = Number(v).toFixed(dec);
    return s.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }
  function pf(v, dec) {
    if (v == null || !isFinite(v)) return '—';
    dec = dec == null ? 2 : dec;
    return (v > 0 ? '+' : '') + Number(v).toFixed(dec) + '%';
  }
  function cls(v) { return v == null ? 'ix-flat' : (v > 0 ? 'ix-up' : (v < 0 ? 'ix-down' : 'ix-flat')); }
  function cssVar(name, fb) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fb;
    } catch (e) { return fb; }
  }

  // ---------- hero 卡 ----------
  function sparkline(vals, pct) {
    if (!vals || vals.length < 3) return '';
    var step = Math.max(1, Math.floor(vals.length / 70));
    var arr = [];
    for (var i = 0; i < vals.length; i += step) arr.push(vals[i]);
    if (arr[arr.length - 1] !== vals[vals.length - 1]) arr.push(vals[vals.length - 1]);
    var min = Math.min.apply(null, arr), max = Math.max.apply(null, arr);
    var rng = (max - min) || 1;
    var w = 100, h = 30;
    var pts = arr.map(function (v, i) {
      var x = (i / (arr.length - 1)) * w;
      var y = h - ((v - min) / rng) * (h - 5) - 2.5;
      return x.toFixed(2) + ',' + y.toFixed(2);
    }).join(' ');
    var col = (pct != null && pct >= 0) ? 'var(--good)' : 'var(--bad)';
    var fill = (pct != null && pct >= 0) ? 'rgba(242,85,90,.10)' : 'rgba(47,191,127,.10)';
    return '<svg viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">' +
      '<polygon points="0,30 ' + pts + ' 100,30" fill="' + fill + '"></polygon>' +
      '<polyline points="' + pts + '" fill="none" stroke="' + col +
      '" stroke-width="1.5" vector-effect="non-scaling-stroke" stroke-linejoin="round"></polyline></svg>';
  }

  function rangePos(spark) {
    if (!spark || spark.length < 5) return null;
    var min = Infinity, max = -Infinity;
    for (var i = 0; i < spark.length; i++) {
      if (spark[i] < min) min = spark[i];
      if (spark[i] > max) max = spark[i];
    }
    if (max - min < 1e-9) return null;
    return (spark[spark.length - 1] - min) / (max - min) * 100;
  }

  function heroCard(h) {
    var tone = (h.state && h.state.tone) || 'calm';
    var extra = '';
    if (h.key === 'vix' && IX.data && IX.data.term_structure && IX.data.term_structure.ratio != null) {
      var r = IX.data.term_structure.ratio;
      extra = '<span>期限结构 VIX/VIX9D ' + nf(r, 2) + (r > 1 ? '（近端偏松）' : '（近端偏紧）') + '</span>';
    } else if (h.q != null) {
      extra = '<span>近 3 年分位 ' + nf(h.q, 0) + '</span>';
    } else if (h.key === 'hsi' && IX.data) {
      var hs = (IX.data.hero || []).filter(function (x) { return x.key === 'sh000300'; })[0];
      var on = (IX.data.onshore || []).filter(function (x) { return x.key === 'sh000300'; })[0];
      var base = hs || on;
      if (base && base.pct != null && h.pct != null) {
        var dv = h.pct - base.pct;
        extra = '<span>较沪深300 <b class="' + cls(dv) + '">' + pf(dv) + '</b></span>';
      }
    }
    // 区间位置条：让「高位/低位」可感知，而不是只有一个文字标签
    var pos = rangePos(h.spark);
    var posHtml = pos == null ? '' :
      '<div class="ix-card-pos" title="近 120 日区间位置 ' + nf(pos, 0) + '%（左低右高）">' +
      '<i style="left:' + Math.max(2, Math.min(98, pos)).toFixed(1) + '%"></i></div>';
    return '<div class="ix-card tone-' + esc(tone) + '" data-hero="' + esc(h.key) + '">' +
      '<div class="ix-card-h"><span class="ix-card-name">' + esc(h.name) + '</span>' +
      '<span class="ix-card-more">' + esc(h.state ? h.state.label : '') + '</span></div>' +
      '<div class="ix-card-v"><span class="ix-card-px">' + nf(h.price, h.dec) + '</span>' +
      '<span class="ix-card-pct ' + cls(h.pct) + '">' + pf(h.pct, 2) + '</span></div>' +
      '<div class="ix-card-meta">' + extra + '<span>' + esc(h.date || '') + '</span></div>' +
      posHtml +
      '<div class="ix-card-spark">' + sparkline(h.spark, h.pct) + '</div>' +
      '</div>';
  }

  function renderHero(el, list, twoCol) {
    if (!el) return;
    el.className = 'ix-hero' + (twoCol ? ' ix-hero-2' : '');
    el.innerHTML = (list || []).map(heroCard).join('');
  }

  // ---------- 温度 + 权重 ----------
  var DIM_COLOR = {
    // 全部用「分类色」而不是红绿：红绿在本模块里专属于涨跌语义，
    // 维度条再用红绿会让人误以为「红维度=危险、绿维度=安全」。
    panic: '#7986cb', commodity: '#ffb74d', basis: '#4db6ac',
    cross: '#64b5f6', trend: '#ba68c8', policy: '#a1887f'
  };

  function bigNum(v) {
    if (v == null || !isFinite(v)) return '—';
    if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(1) + '万';
    return nf(v, 0);
  }

  function renderTemp(el) {
    var d = IX.data;
    if (!el) return;
    if (!d) { el.innerHTML = ''; return; }
    var dims = d.dims || {}, w = d.weights || {}, names = d.dim_names || {};
    var keys = Object.keys(w);
    var segs = keys.map(function (k) {
      return '<i style="width:' + (w[k] * 100).toFixed(1) + '%;background:' +
        (DIM_COLOR[k] || 'var(--ink-faint)') + '" title="' + esc(names[k] || k) + ' 得分 ' +
        nf(dims[k], 0) + ' / 权重 ' + (w[k] * 100).toFixed(0) + '%"></i>';
    }).join('');
    var legend = keys.map(function (k) {
      return '<span title="' + esc(names[k] || k) + '：得分越高越紧张"><i style="background:' +
        (DIM_COLOR[k] || 'var(--ink-faint)') + '"></i>' +
        esc(names[k] || k) + ' <b style="color:var(--ink-soft)">' + nf(dims[k], 0) + '</b>' +
        ' · ' + (w[k] * 100).toFixed(0) + '%</span>';
    }).join('');
    var boost = (d.boost_note && d.boost_note.length)
      ? '<div class="ix-boost">权重上调：' + d.boost_note.map(esc).join('；') + '</div>' : '';
    var scale = '<div class="ix-temp-scale">' +
      '<span class="' + (d.tone === 'calm' ? 'on calm' : '') + '">0–44 平稳</span>' +
      '<span class="' + (d.tone === 'warn' ? 'on warn' : '') + '">45–69 偏紧</span>' +
      '<span class="' + (d.tone === 'crisis' ? 'on crisis' : '') + '">70+ 高危</span>' +
      '<span style="margin-left:auto">数字是各维度得分（0–100，越高越紧张）</span></div>';
    var toneCol = d.tone === 'crisis' ? 'var(--good)' : (d.tone === 'warn' ? 'var(--accent)' : 'var(--bad)');
    el.innerHTML =
      '<div><div class="ix-temp-num" style="color:' + toneCol + '">' + nf(d.temp, 1) +
      '<small>/100</small></div>' +
      '<div style="font-size:11px;color:var(--ink-faint);margin-top:3px">风险温度 · ' +
      esc(d.level) + '</div></div>' +
      '<div class="ix-temp-bar"><div class="ix-wbar">' + segs + '</div>' +
      '<div class="ix-wlegend">' + legend + '</div>' + boost + scale + '</div>';
  }

  function renderBanner(el) {
    if (!el) return;
    var d = IX.data;
    if (!d || !d.ok) { el.hidden = true; return; }
    var crisis = d.crisis_count > 0;
    if (!crisis && !(d.temp >= 70)) { el.hidden = true; return; }
    el.hidden = false;
    el.className = 'ix-banner' + (crisis ? ' crisis' : ' warn');
    el.innerHTML = '<b>' + (crisis ? '🚨 危机级信号已触发' : '⚠ 风险温度偏高') + '</b>' +
      '<span>' + esc(d.summary || '') + '</span>' +
      '<span class="ix-banner-n">' + d.crisis_count + ' 危机 · ' + d.warn_count +
      ' 预警 · ' + nf(d.temp, 0) + '/100</span>';
  }

  function renderTheme(el) {
    var d = IX.data;
    if (!el) return;
    if (!d || !d.theme) { el.innerHTML = ''; el.hidden = true; return; }
    el.hidden = false;
    el.className = 'ix-theme tone-' + (d.theme.tone || 'warn');
    el.innerHTML = '<div class="ix-theme-h">🔗 ' + esc(d.theme.title) + '</div>' +
      '<div class="ix-theme-b">' + esc(d.theme.text) + '</div>';
  }

  // ---------- 信号卡 ----------
  // 把文本里的 +x.xx% / -x.xx% 上色（0.00% 保持原色）——用户要求「+/- 对应红绿」
  function colorFact(t) {
    return esc(t).replace(/(^|[^\d.])([+\-]\d+(?:\.\d+)?%)/g, function (m, pre, num) {
      var v = parseFloat(num);
      if (!isFinite(v) || v === 0) return m;
      return pre + '<b class="' + (v > 0 ? 'ix-up' : 'ix-down') + '">' + num + '</b>';
    });
  }

  function signalCard(s) {
    var facts = (s.facts || []).map(function (f) { return '<span class="ix-fact">' + colorFact(f) + '</span>'; }).join('');
    var pat = (s.pattern && s.pattern !== '中性')
      ? '<span class="ix-tag ix-tag-acc">' + esc(s.pattern) + '可能</span>' : '';
    var bias = (s.calib && s.calib.bias) || 'unknown';
    var calib = s.calib
      ? '<div class="ix-sig-calib"><span class="ix-calib-k">历史证据</span>' +
        '<span>' + esc(s.calib.text) + ' <b class="ix-bias-' + esc(bias) + '" title="' +
        esc(s.calib.note || '') + '">' + esc(BIAS_TXT[bias] || '') + '</b></span></div>' : '';
    var play = s.playbook ? '<div class="ix-sig-play">' + esc(s.playbook) + '</div>' : '';
    return '<article class="ix-sig lv-' + esc(s.level) + '" data-sig="' + esc(s.id) + '">' +
      '<div class="ix-sig-h">' +
      '<span class="ix-tag ix-tag-' + esc(s.level) + '">' + esc(LEVEL_TXT[s.level] || s.level) + '</span>' +
      '<span class="ix-tag ix-tag-blue">' + esc(s.tag) + '</span>' +
      '<span class="ix-sig-t">' + esc(s.title) + '</span>' +
      '<span class="ix-tag ix-tag-term">' + esc(s.term) + '</span>' + pat +
      '</div>' +
      '<div class="ix-sig-body">' + esc(s.verdict) + '</div>' +
      (facts ? '<div class="ix-sig-facts">' + facts + '</div>' : '') +
      calib + play + '</article>';
  }

  function renderSignals(el, opts) {
    var d = IX.data;
    if (!el) return;
    if (!d) { el.innerHTML = ''; return; }
    opts = opts || {};
    var all = d.signals || [];
    var strong = all.filter(function (s) { return s.level === 'crisis' || s.level === 'warn'; });
    var weak = all.filter(function (s) { return s.level === 'watch'; });

    if (!all.length) {
      el.innerHTML = '<div class="ix-empty">当前没有触发任何联动信号。<br>' +
        '指数、期现、跨市场三个层面都在常态区间，不需要额外应对。</div>';
      return;
    }
    // 悬浮窗只放预警级：用户要求「未触发的不显示、观感简洁」
    if (opts.onlyStrong) {
      el.innerHTML = strong.length
        ? strong.map(signalCard).join('')
        : '<div class="ix-empty">当前没有预警级信号。<br>仅 ' + weak.length +
          ' 项环境状态处于观察区，进「指数指标」可展开看。</div>';
      return;
    }
    var html = strong.map(signalCard).join('');
    if (!strong.length) {
      html += '<div class="ix-empty">没有预警级信号，仅有 ' + weak.length + ' 项观察项。</div>';
    }
    if (weak.length && opts.foldWeak !== false) {
      html += '<div class="ix-weak">' +
        '<button class="ix-btn ix-btn-ghost" id="' + (opts.weakBtnId || 'ixWeakBtn') + '">' +
        (IX.watchOpen ? '▾' : '▸') + ' ' + weak.length + ' 项观察项（环境状态，非当日异动）</button>' +
        '<div class="ix-weak-body" ' + (IX.watchOpen ? '' : 'hidden') + '>' +
        weak.map(signalCard).join('') + '</div></div>';
    } else if (weak.length) {
      html += weak.map(signalCard).join('');
    }
    el.innerHTML = html;
    var btn = $('#ixWeakBtn', el);
    if (btn) {
      btn.addEventListener('click', function () {
        IX.watchOpen = !IX.watchOpen;
        renderSignals(el, { foldWeak: true, weakBtnId: 'ixWeakBtn' });
      });
    }
  }

  // ---------- 列表（用 CSS Grid 行，而不是 <table>：表格在 overflow 容器里
  //            容易出现表头错位/列挤压，grid 的行列表两侧对齐更可控） ----------
  function renderOnshore() {
    var el = $('#ixOnshore');
    if (!el || !IX.data) return;
    var head = '<div class="ix-row ix-row-h"><span>指数</span><span>最新</span>' +
      '<span>涨跌</span><span>日期</span></div>';
    var body = (IX.data.onshore || []).map(function (r) {
      return '<div class="ix-row" data-k="' + esc(r.key) + '">' +
        '<span class="ix-c-name">' + esc(r.name) +
        (r.core ? ' <span class="ix-tag ix-tag-acc" style="font-size:10px">核心</span>' : '') +
        '</span>' +
        '<span>' + nf(r.price, r.dec) + '</span>' +
        '<span class="' + cls(r.pct) + '">' + pf(r.pct) + '</span>' +
        '<span class="ix-sub">' + esc(r.date || '') + '</span></div>';
    }).join('');
    el.innerHTML = '<div class="ix-rowlist ix-cols-4">' + head + body + '</div>';
    bindRowClick(el);
  }

  function renderFutures() {
    var el = $('#ixFutures');
    if (!el || !IX.data) return;
    var head = '<div class="ix-row ix-row-h"><span>合约 / 标的</span><span>最新</span>' +
      '<span>涨跌</span><span>基差</span><span>持仓</span></div>';
    var body = (IX.data.futures || []).map(function (r) {
      var bs = r.basis_pct;
      var bsCls = bs == null ? 'ix-flat' : (bs < 0 ? 'ix-down' : 'ix-up');
      var sub = [];
      if (r.basis_ann != null) sub.push('年化 ' + pf(r.basis_ann, 1));
      if (r.basis_rank != null) sub.push('分位 ' + nf(r.basis_rank, 0));
      return '<div class="ix-row" data-k="' + esc(r.key) + '">' +
        '<span class="ix-c-name">' + esc(r.short) +
        (r.rollover ? ' <span class="ix-roll">换月</span>' : '') +
        '<br><span class="ix-sub">标的 ' + esc(r.under_name || '') + '</span></span>' +
        '<span>' + nf(r.price, r.dec) + '</span>' +
        '<span class="' + cls(r.pct) + '">' + pf(r.pct) + '</span>' +
        '<span class="' + bsCls + '">' + pf(bs, 2) +
        (sub.length ? '<br><span class="ix-sub">' + sub.join(' · ') + '</span>' : '') + '</span>' +
        '<span class="ix-sub">' + bigNum(r.hold) + '</span></div>';
    }).join('');
    el.innerHTML = '<div class="ix-rowlist ix-cols-5">' + head + body + '</div>';
    bindRowClick(el);
  }

  function bindRowClick(el) {
    $$('.ix-row[data-k]', el).forEach(function (row) {
      row.addEventListener('click', function () {
        var k = row.getAttribute('data-k');
        if (!k) return;
        $$('.ix-row[data-k]', el).forEach(function (x) { x.classList.remove('on'); });
        row.classList.add('on');
        loadChart(k);
      });
    });
  }

  function renderAux() {
    var el = $('#ixAux');
    if (!el || !IX.data) return;
    var ts = IX.data.term_structure || {};
    var aux = IX.data.aux || [];
    var chips = aux.map(function (a) {
      return '<span class="ix-fact">' + esc(a.short) + ' ' + nf(a.price, a.dec) +
        ' <b class="' + cls(a.pct) + '">' + pf(a.pct) + '</b></span>';
    });
    if (ts.ratio != null) {
      chips.push('<span class="ix-fact">VIX/VIX9D ' + nf(ts.ratio, 3) +
        '（' + (ts.ratio > 1 ? '近端偏松' : '近端偏紧') + '）</span>');
    }
    (IX.data.rates || []).forEach(function (r) {
      chips.push('<span class="ix-fact" title="' + esc(r.desc || '') + '">' +
        esc(r.short) + ' ' + nf(r.price, r.dec) +
        ' <b class="' + cls(r.pct) + '">' + pf(r.pct) + '</b></span>');
    });
    el.innerHTML = chips.join('');
  }

  // ---------- K 线（自绘） ----------
  function drawChart() {
    var cv = $('#ixCanvas');
    var wrap = $('#ixChartWrap');
    if (!cv || !wrap) return;
    var bars = IX.chart.bars || [];
    if (!bars.length) {
      wrap.innerHTML = '<div class="ix-chart-empty">暂无 K 线数据</div>';
      return;
    }
    if (!cv.parentNode) { wrap.innerHTML = '<canvas id="ixCanvas"></canvas>'; cv = $('#ixCanvas'); }

    var dpr = window.devicePixelRatio || 1;
    var W = Math.max(cv.clientWidth || wrap.clientWidth || 640, 320);
    var H = 340;
    var wpx = Math.round(W * dpr), hpx = Math.round(H * dpr);
    if (cv.width !== wpx) cv.width = wpx;
    if (cv.height !== hpx) cv.height = hpx;
    cv.style.height = H + 'px';
    IX.chart.draws = (IX.chart.draws || 0) + 1;
    var ctx = cv.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    var C = {
      up: cssVar('--k-up', '#ef5350'), down: cssVar('--k-down', '#26a69a'),
      flat: cssVar('--k-axis', '#7b7f88'),
      grid: cssVar('--k-grid', 'rgba(255,255,255,.07)'),
      gridSoft: cssVar('--k-grid-soft', 'rgba(255,255,255,.04)'),
      axis: cssVar('--k-axis', '#7b7f88'), ink: cssVar('--ink-faint', '#73757e'),
      // ★ 均线一律用「非红非绿」的冷色：红绿在本图里是 OHLC 相对昨收的语义，
      //   均线再用红绿会让人误读成涨跌方向（用户反馈「MA5/MA20 一直是红色」）。
      ma5: cssVar('--ix-ma5', '#4a9eff'),
      ma20: cssVar('--ix-ma20', '#ffa726'),
      ma60: cssVar('--ix-ma60', '#9aa0aa'),
      volup: cssVar('--k-volup', 'rgba(239,83,80,.5)'),
      voldn: cssVar('--k-voldn', 'rgba(38,166,154,.5)'),
      volflat: 'rgba(138,141,148,.45)',
      cross: cssVar('--k-cross', 'rgba(255,255,255,.42)'),
      bg: cssVar('--bg-card', '#171b23')
    };

    var padL = 8, padR = 56, padT = 8;
    var volH = Math.round(H * 0.22), gapH = 14;
    var mainH = H - padT - volH - gapH - 18;
    var mainW = W - padL - padR;
    var n = bars.length;
    var bw = Math.max(1.2, Math.min(9, mainW / n * 0.66));
    var step = mainW / n;
    var xOf = function (i) { return padL + step * (i + 0.5); };

    var hi = -Infinity, lo = Infinity;
    bars.forEach(function (b) { if (b.h > hi) hi = b.h; if (b.l < lo) lo = b.l; });
    var pad = (hi - lo) * 0.06 || 1;
    hi += pad; lo -= pad;
    var yOf = function (v) { return padT + (hi - v) / (hi - lo) * mainH; };

    // 网格 + 右侧价格轴
    ctx.strokeStyle = C.gridSoft;
    ctx.lineWidth = 1;
    ctx.font = '10px ' + cssVar('--mono', 'monospace');
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (var g = 0; g <= 4; g++) {
      var yv = lo + (hi - lo) * g / 4;
      var yy = yOf(yv);
      ctx.beginPath();
      ctx.moveTo(padL, yy + 0.5);
      ctx.lineTo(padL + mainW, yy + 0.5);
      ctx.stroke();
      ctx.fillStyle = C.axis;
      ctx.fillText(nf(yv, 2), padL + mainW + 6, yy);
    }

    // 日期轴（首/中/尾）
    ctx.textAlign = 'center';
    ctx.fillStyle = C.axis;
    [0, Math.floor(n / 2), n - 1].forEach(function (i) {
      if (i < 0 || i >= n) return;
      var lbl = (bars[i].d || '').slice(2);
      var x = xOf(i);
      x = Math.max(padL + 18, Math.min(padL + mainW - 18, x));
      ctx.fillText(lbl, x, padT + mainH + 9);
    });

    // 蜡烛：★ 全部颜色以「上一交易日收盘」为基准 —— 高红 / 低绿 / 平（=昨收）灰。
    // 实体跨越昨收时按 yP 分两段着色（上红下绿），这样开盘与收盘各自的涨跌都能看出来。
    var EPS = 1e-9;
    for (var i = 0; i < n; i++) {
      var b = bars[i];
      var prev = i > 0 ? bars[i - 1].c : b.o;
      var x = xOf(i);
      var yH = yOf(b.h), yL = yOf(b.l), yO = yOf(b.o), yC = yOf(b.c), yP = yOf(prev);
      var sO = b.o > prev + EPS ? 1 : (b.o < prev - EPS ? -1 : 0);
      var sC = b.c > prev + EPS ? 1 : (b.c < prev - EPS ? -1 : 0);
      var sH = b.h > prev + EPS ? 1 : (b.h < prev - EPS ? -1 : 0);
      var sL = b.l > prev + EPS ? 1 : (b.l < prev - EPS ? -1 : 0);
      var colOf = function (s) { return s > 0 ? C.up : (s < 0 ? C.down : C.flat); };

      var yTopBody = Math.min(yO, yC), yBotBody = Math.max(yO, yC);
      ctx.lineWidth = 1;
      // 上影（high → 实体顶）：颜色只看 high 与昨收的关系
      ctx.strokeStyle = colOf(sH);
      ctx.beginPath();
      ctx.moveTo(x, yH);
      ctx.lineTo(x, yTopBody);
      ctx.stroke();
      // 下影（实体底 → low）：颜色只看 low 与昨收的关系
      ctx.strokeStyle = colOf(sL);
      ctx.beginPath();
      ctx.moveTo(x, yBotBody);
      ctx.lineTo(x, yL);
      ctx.stroke();
      // 实体
      var hBody = Math.max(yBotBody - yTopBody, 1);
      if (sO === sC && sO !== 0) {
        ctx.fillStyle = colOf(sC);
        ctx.fillRect(x - bw / 2, yBotBody - hBody, bw, hBody);
      } else if (sO === 0 && sC === 0) {
        ctx.fillStyle = C.flat;
        ctx.fillRect(x - bw / 2, yBotBody - hBody, bw, hBody);
      } else {
        // 跨越昨收价：y < yP 是「高于昨收」→ 红；y > yP 是「低于昨收」→ 绿
        var ySplit = Math.max(yTopBody, Math.min(yBotBody, yP));
        if (ySplit - yTopBody > 0.15) {
          ctx.fillStyle = C.up;
          ctx.fillRect(x - bw / 2, yTopBody, bw, ySplit - yTopBody);
        }
        if (yBotBody - ySplit > 0.15) {
          ctx.fillStyle = C.down;
          ctx.fillRect(x - bw / 2, ySplit, bw, yBotBody - ySplit);
        }
        if (ySplit - yTopBody <= 0.15 && yBotBody - ySplit <= 0.15) {
          ctx.fillStyle = C.flat;
          ctx.fillRect(x - bw / 2, yTopBody, bw, hBody);
        }
      }
    }

    // 均线
    function line(key, color) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      var started = false;
      for (var i = 0; i < n; i++) {
        var v = bars[i][key];
        if (v == null || !isFinite(v)) continue;
        var x = xOf(i), y = yOf(v);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    line('ma5', C.ma5);
    line('ma20', C.ma20);
    line('ma60', C.ma60);

    // 成交量
    var vTop = padT + mainH + gapH + 10;
    var maxV = 0;
    bars.forEach(function (b) { if (b.v > maxV) maxV = b.v; });
    if (maxV > 0) {
      for (var i2 = 0; i2 < n; i2++) {
        var bb = bars[i2];
        var prev2 = i2 > 0 ? bars[i2 - 1].c : bb.o;
        var hh2 = Math.max((bb.v / maxV) * volH, 0.5);
        ctx.fillStyle = bb.c > prev2 + EPS ? C.volup : (bb.c < prev2 - EPS ? C.voldn : C.volflat);
        ctx.fillRect(xOf(i2) - bw / 2, vTop + volH - hh2, bw, hh2);
      }
    }

    // 十字光标（hover）
    if (IX.chart.hover >= 0 && IX.chart.hover < n) {
      var hv = bars[IX.chart.hover];
      var hx = xOf(IX.chart.hover);
      ctx.strokeStyle = C.cross;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(hx, padT);
      ctx.lineTo(hx, padT + mainH);
      ctx.stroke();
      ctx.setLineDash([]);

      var tx = hx + 10;
      if (tx > W - 150) tx = hx - 148;
      var prevC = IX.chart.hover > 0 ? bars[IX.chart.hover - 1].c : hv.o;
      var chg = prevC ? (hv.c / prevC - 1) * 100 : 0;
      var lines = [
        hv.d,
        '开 ' + nf(hv.o) + '  高 ' + nf(hv.h),
        '低 ' + nf(hv.l) + '  收 ' + nf(hv.c),
        '较昨收 ' + pf(chg),
        'MA5 ' + nf(hv.ma5) + '  MA20 ' + nf(hv.ma20),
        'MA60 ' + nf(hv.ma60)
      ];
      var boxW = 142, boxH = 13 * lines.length + 10;
      var ty = padT + 6;
      ctx.fillStyle = C.bg;
      ctx.globalAlpha = 0.94;
      ctx.fillRect(tx, ty, boxW, boxH);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = C.grid;
      ctx.strokeRect(tx + 0.5, ty + 0.5, boxW, boxH);
      ctx.textAlign = 'left';
      ctx.fillStyle = C.ink;
      ctx.font = '10.5px ' + cssVar('--mono', 'monospace');
      lines.forEach(function (t, k) {
        if (k === 0) ctx.fillStyle = C.axis;
        else if (k === 3) ctx.fillStyle = chg > 0 ? C.up : (chg < 0 ? C.down : C.flat);
        else ctx.fillStyle = C.ink;
        ctx.fillText(t, tx + 7, ty + 11 + k * 13);
      });
    }
  }

  function bindChartHover() {
    var cv = $('#ixCanvas');
    if (!cv || cv.__ixBound) return;
    cv.__ixBound = true;
    cv.addEventListener('mousemove', function (ev) {
      var bars = IX.chart.bars || [];
      if (!bars.length) return;
      var rect = cv.getBoundingClientRect();
      var W = rect.width || cv.clientWidth;
      var padL = 8, padR = 56;
      var mainW = W - padL - padR;
      var step = mainW / bars.length;
      var rel = ev.clientX - rect.left - padL;
      var idx = Math.floor(rel / step);
      idx = Math.max(0, Math.min(bars.length - 1, idx));
      if (idx !== IX.chart.hover) { IX.chart.hover = idx; drawChart(); }
    });
    cv.addEventListener('mouseleave', function () {
      IX.chart.hover = -1;
      drawChart();
    });
  }

  function renderChartTabs() {
    var el = $('#ixChartTabs');
    if (!el) return;
    el.innerHTML = HERO_TABS.map(function (t) {
      return '<button class="ix-ctab' + (IX.chart.key === t.k ? ' on' : '') +
        '" data-ck="' + esc(t.k) + '">' + esc(t.n) + '</button>';
    }).join('');
    $$('.ix-ctab', el).forEach(function (b) {
      b.addEventListener('click', function () { loadChart(b.getAttribute('data-ck')); });
    });
  }

  function loadChart(key, days) {
    if (days) IX.chart.days = days;
    IX.chart.key = key || IX.chart.key;
    IX.chart.hover = -1;
    IX.chart.busy = true;
    var wrap = $('#ixChartWrap');
    var title = $('#ixChartName');
    if (title) title.textContent = '加载中…';
    renderChartTabs();
    API.kline(IX.chart.key, IX.chart.days).then(function (j) {
      IX.chart.busy = false;
      if (!j || !j.ok) {
        IX.chart.bars = null;
        if (wrap) wrap.innerHTML = '<div class="ix-chart-empty">' + esc((j && j.err) || 'K 线加载失败') + '</div>';
        if (title) title.textContent = '—';
        return;
      }
      // canvas 可能被「空状态」替换掉了 → 重建
      if (wrap && !$('#ixCanvas')) wrap.innerHTML = '<canvas id="ixCanvas"></canvas>';
      IX.chart.bars = j.bars || [];
      if (title) title.textContent = j.name + '（' + IX.chart.bars.length + ' 根）';
      var days2 = $('#ixChartDays');
      if (days2) days2.value = String(IX.chart.days);
      drawChart();
      bindChartHover();
    }).catch(function () {
      IX.chart.busy = false;
      if (wrap) wrap.innerHTML = '<div class="ix-chart-empty">K 线请求失败</div>';
    });
  }

  // ---------- 主渲染 ----------
  function renderAll() {
    var d = IX.data;
    if (!d || !d.ok) {
      var msg = (d && d.err) || '加载失败';
      ['#ixHero', '#ixFlashHero'].forEach(function (sel) {
        var el = $(sel);
        if (el) el.innerHTML = '<div class="ix-empty" style="grid-column:1/-1">' + esc(msg) + '</div>';
      });
      var s1 = $('#ixSignals');
      if (s1) s1.innerHTML = '<div class="ix-empty">' + esc(msg) + '</div>';
      return;
    }
    renderHero($('#ixHero'), d.hero);
    renderHero($('#ixFlashHero'), d.hero);
    renderAux();
    renderBanner($('#ixBanner'));
    renderBanner($('#ixFlashBanner'));
    renderTemp($('#ixTemp'));
    renderTemp($('#ixFlashTemp'));
    renderTheme($('#ixTheme'));
    renderTheme($('#ixFlashTheme'));
    renderSignals($('#ixSignals'), { foldWeak: true, weakBtnId: 'ixWeakBtn' });
    renderSignals($('#ixFlashSignals'), { onlyStrong: true });
    renderOnshore();
    renderFutures();
    var ts1 = $('#ixStamp'), ts2 = $('#ixFlashSub');
    var stamp = '数据 ' + (d.asof || '—') + ' · 抓取 ' + (d.ts || '') + ' · ' + nf(d.elapsed, 1) + 's';
    var notes = [];
    if (d.spot_ok === false) notes.push('实时快照不可用');
    if (d.stale && d.stale.length) notes.push('本地缓存兜底 ' + d.stale.length + ' 项');
    if (d.fallbacks) Object.keys(d.fallbacks).forEach(function (k) { notes.push(d.fallbacks[k]); });
    if (notes.length) stamp += ' · ⚠ ' + notes.join('；');
    if (ts1) {
      ts1.textContent = stamp;
      ts1.style.color = notes.length ? 'var(--warn-ink)' : '';
    }
    if (ts2) ts2.textContent = (d.summary || '') + ' · ' + (d.asof || '');
    var dot = $('#ixFlashDot');
    if (dot) dot.className = 'ix-fb-dot ' + (d.tone || 'calm');
    if (!$('#ixCanvas')) {
      var wrap = $('#ixChartWrap');
      if (wrap) wrap.innerHTML = '<canvas id="ixCanvas"></canvas>';
    }
    if (!IX.chart.bars) loadChart(IX.chart.key, IX.chart.days);
    else drawChart();
  }

  // ---------- 数据加载 ----------
  function setLoading(on, where) {
    IX.loading = on;
    var el = where ? $(where) : null;
    if (el && on) el.innerHTML = '<div class="ix-loading"><span class="ix-spin"></span>正在拉取全球指数与期现数据（首次约 5-10 秒）…</div>';
  }

  function loadPanel(force) {
    if (IX.loading) return Promise.resolve(IX.data);
    setLoading(true, '#ixSignals');
    setLoading(true, '#ixFlashSignals');
    return API.panel(!!force).then(function (j) {
      IX.loading = false;
      IX.data = j;
      IX.err = (j && !j.ok) ? (j.err || '加载失败') : null;
      renderAll();
      return j;
    }).catch(function (e) {
      IX.loading = false;
      IX.err = '网络请求失败：' + (e && e.message ? e.message : e);
      IX.data = { ok: false, err: IX.err };
      renderAll();
      return IX.data;
    });
  }

  // ---------- 悬浮窗 ----------
  function openFlash(auto) {
    var box = $('#ixFlash');
    if (!box) return;
    box.hidden = false;
    IX.flashOpened = true;
    document.documentElement.style.overflow = 'hidden';
    if (!IX.data || auto) loadPanel(false);
    else renderAll();
  }

  function closeFlash() {
    var box = $('#ixFlash');
    if (!box) return;
    box.hidden = true;
    document.documentElement.style.overflow = '';
  }

  function goTab() {
    closeFlash();
    var btn = $('.tab[data-tab="idxhub"]');
    if (btn) btn.click();
    initIdxhub();
  }

  function scrollToAnalysis() {
    var el = $('#ixTemp');
    if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var card = $('#ixAnalysisCard');
    if (card) {
      card.classList.add('ix-hl');
      setTimeout(function () { card.classList.remove('ix-hl'); }, 1600);
    }
  }

  // ---------- 初始化 ----------
  function initIdxhub() {
    if (IX.inited) {
      if (!IX.data && !IX.loading) loadPanel(false);
      // 切回页签时补一次重绘（隐藏期间 clientWidth=0，绘制会被降级到 640px 位图）
      setTimeout(function () { if (IX.chart.bars) drawChart(); }, 60);
      return;
    }
    IX.inited = true;

    var b = {
      refresh: $('#ixRefresh'), analyze: $('#ixAnalyze'), meta: $('#ixMetaBtn'),
      flashRefresh: $('#ixFlashRefresh'), flashAnalyze: $('#ixFlashAnalyze'),
      flashGo: $('#ixFlashGo'), flashClose: $('#ixFlashClose'), flashBd: $('#ixFlashBd'),
      topBtn: $('#btnIdxFlash')
    };
    if (b.refresh) b.refresh.addEventListener('click', function () { loadPanel(true); });
    if (b.flashRefresh) b.flashRefresh.addEventListener('click', function () { loadPanel(true); });
    if (b.analyze) b.analyze.addEventListener('click', function () {
      if (IX.loading) return;
      loadPanel(true).then(function () { setTimeout(scrollToAnalysis, 120); });
    });
    if (b.flashAnalyze) b.flashAnalyze.addEventListener('click', function () {
      goTab();
      loadPanel(true).then(function () { setTimeout(scrollToAnalysis, 260); });
    });
    if (b.flashGo) b.flashGo.addEventListener('click', goTab);
    if (b.flashClose) b.flashClose.addEventListener('click', closeFlash);
    if (b.flashBd) b.flashBd.addEventListener('click', closeFlash);
    if (b.topBtn) b.topBtn.addEventListener('click', function () { openFlash(false); });
    if (b.meta) b.meta.addEventListener('click', function () { showMeta(); });

    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && $('#ixFlash') && !$('#ixFlash').hidden) closeFlash();
    });

    renderChartTabs();
    var daysSel = $('#ixChartDays');
    if (daysSel) daysSel.addEventListener('change', function () {
      loadChart(IX.chart.key, parseInt(daysSel.value, 10) || 180);
    });

    resizeHook();
    if (!IX.data) loadPanel(false);
  }

  function showMeta() {
    var el = $('#ixMetaBox');
    if (!el) return;
    if (el.innerHTML && !el.hidden) { el.hidden = true; return; }
    el.hidden = false;
    if (el.getAttribute('data-filled') === '1') return;
    API.meta().then(function (m) {
      var src = (m.sources || []).map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('');
      var cav = (m.caveats || []).map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('');
      el.innerHTML = '<div class="ix-note" style="border:none;padding:0">' +
        '<b>数据源</b><ul style="margin:4px 0 10px 18px">' + src + '</ul>' +
        '<b>口径与局限</b><ul style="margin:4px 0 0 18px">' + cav + '</ul></div>';
      el.setAttribute('data-filled', '1');
    });
  }

  var _resizeT = null;
  function resizeHook() {
    window.addEventListener('resize', function () {
      clearTimeout(_resizeT);
      _resizeT = setTimeout(function () { if (IX.chart.bars) drawChart(); }, 160);
    });
    // ★ 页签从 display:none 变为可见时 clientWidth 会从 0 跳到真实值，
    //   window.resize 捕捉不到 → 必须用 ResizeObserver 监听容器尺寸。
    try {
      var wrap = $('#ixChartWrap');
      if (wrap && typeof ResizeObserver === 'function' && !wrap.__ixRO) {
        wrap.__ixRO = new ResizeObserver(function () {
          clearTimeout(_resizeT);
          _resizeT = setTimeout(function () { if (IX.chart.bars) drawChart(); }, 80);
        });
        wrap.__ixRO.observe(wrap);
      }
    } catch (e) { /* ignore */ }
  }

  // 主题切换后重绘 canvas：① 监听 data-theme 属性变化（任何切换方式都覆盖）
  // ② 沿用站内 __themeRedraw 注册表（双保险）
  try {
    var mo = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        if (muts[i].attributeName === 'data-theme' && IX.chart.bars) {
          setTimeout(drawChart, 60);
          return;
        }
      }
    });
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  } catch (e) { /* ignore */ }
  try {
    window.__themeRedraw = window.__themeRedraw || [];
    window.__themeRedraw.push(function () { if (IX.chart.bars) setTimeout(drawChart, 80); });
  } catch (e) { /* ignore */ }

  // 页面打开即弹（用户要求：每次打开先以近全屏弹出）
  // ★ 必须先 initIdxhub()：悬浮窗是直接打开、不走 switchTab，
  //   不先绑定按钮就会「点上没反应」（关不掉、进不了页签）。
  function autoOpen() {
    var box = $('#ixFlash');
    if (!box) return;
    if (box.hidden === false) return;
    try { initIdxhub(); } catch (e) { /* ignore */ }
    openFlash(true);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(autoOpen, 480); });
  } else {
    setTimeout(autoOpen, 480);
  }

  window.initIdxhub = initIdxhub;
  window.IdxHub = { open: openFlash, close: closeFlash, reload: function () { return loadPanel(true); }, state: IX };
})();
