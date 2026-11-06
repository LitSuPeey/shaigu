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
    meta: function () { return fetch('/api/idxhub/meta').then(function (r) { return r.json(); }); },
    // ---- 新增四块：全部独立接口，主面板不等它们 ----
    opt: function (force) {
      return fetch('/api/idxopt/panel' + (force ? '?force=1' : '')).then(function (r) { return r.json(); });
    },
    optHist: function (force) {
      return fetch('/api/idxopt/history' + (force ? '?force=1' : '')).then(function (r) { return r.json(); });
    },
    policy: function (force) {
      return fetch('/api/idxpolicy/panel' + (force ? '?force=1' : '')).then(function (r) { return r.json(); });
    },
    mins: function (key) {
      return fetch('/api/idxhub/mins?key=' + encodeURIComponent(key)).then(function (r) { return r.json(); });
    },
    calib: function (force) {
      return fetch('/api/idxhub/calib' + (force ? '?force=1' : '')).then(function (r) { return r.json(); });
    },
    qvix: function (force) {
      return fetch('/api/idxopt/qvix?name=300etf' + (force ? '&force=1' : ''))
        .then(function (r) { return r.json(); });
    },
    fx: function (force) {
      return fetch('/api/idxhub/fx' + (force ? '?force=1' : '')).then(function (r) { return r.json(); });
    }
  };

  var IX = {
    data: null,
    loading: false,
    err: null,
    inited: false,
    watchOpen: false,
    chart: { key: 'vix', days: 180, bars: null, busy: false, hover: -1 },
    dataTs: 0,
    flashOpened: false,
    ext: { opt: null, optHist: null, policy: null, calib: null, mins: null, qvix: null,
           fx: null, started: false, busy: {}, optPick: null }
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
    // ★ 标定结论与「高危」措辞冲突时必须就地提示，不能让用户自己发现
    var cal = IX.ext && IX.ext.calib;
    var flip = (cal && cal.ok && cal.grade === 'flipped')
      ? '<div class="ix-boost">⚠ 历史标定：该温度与未来 ' + cal.horizon +
        ' 日收益<b>正相关</b>（高温之后反而偏涨），别把「高危」读成看跌信号 → 见下方「权重标定」</div>'
      : '';
    // 温度历史曲线：71.3 是不是「高」，要看它在自己历史上排第几
    var hist = '';
    if (cal && cal.ok && cal.temp && cal.temp.series && cal.temp.series.length > 20) {
      var tv = cal.temp.series.map(function (x) { return x.v; });
      hist = '<div class="ix-temp-hist">' +
        '<span class="ix-th-l">近 ' + cal.temp.n + ' 日</span>' +
        tinyLine(tv, cssVar('--accent', '#4a9eff')) +
        '<span class="ix-th-r">分位 <b>' + nf(cal.temp.pct, 0) + '%</b> · 中位 ' +
        nf(cal.temp.med, 1) + '</span></div>' +
        '<div class="ix-th-note">曲线为<b>基准权重</b>口径；上方数字含「信号触发时权重上调」' +
        '（VIX 高位 ×1.70 等），所以两者会差几个点，不是算错。</div>';
    }
    var toneCol = d.tone === 'crisis' ? 'var(--good)' : (d.tone === 'warn' ? 'var(--accent)' : 'var(--bad)');
    el.innerHTML =
      '<div><div class="ix-temp-num" style="color:' + toneCol + '">' + nf(d.temp, 1) +
      '<small>/100</small></div>' +
      '<div style="font-size:11px;color:var(--ink-faint);margin-top:3px">风险温度 · ' +
      esc(d.level) + '</div></div>' +
      '<div class="ix-temp-bar"><div class="ix-wbar">' + segs + '</div>' +
      '<div class="ix-wlegend">' + legend + '</div>' + boost + scale + flip + hist + '</div>';
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
    renderGlance();
    // 四个较重/较慢的区块延后触发：主面板（3s）不能被它们拖住
    if (!IX.ext.started) setTimeout(function () { loadExtras(false); }, 260);
  }

  // ==========================================================================
  // 分时 / 期权 / 政策面 / 权重标定
  // —— 四块独立异步，主面板（renderAll）跑完才触发，任何一块失败都不影响上面
  // ==========================================================================

  function toneTag(tone) {
    return tone === 'crisis' ? 'ix-tag-crisis' : (tone === 'warn' ? 'ix-tag-warn' : 'ix-tag-watch');
  }

  function minSpark(pts) {
    if (!pts || pts.length < 2) return '';
    var W = 300, H = 46, P = 2;
    var vs = pts.map(function (p) { return p.p; });
    var mn = Math.min.apply(null, vs), mx = Math.max.apply(null, vs);
    var rng = (mx - mn) || Math.abs(mx) * 0.001 || 1;
    var n = pts.length;
    var seg = [];
    for (var i = 0; i < n; i++) {
      var x = P + (W - 2 * P) * i / (n - 1);
      var y = H - P - (H - 2 * P) * (vs[i] - mn) / rng;
      seg.push((i ? 'L' : 'M') + x.toFixed(1) + ',' + y.toFixed(1));
    }
    var up = vs[n - 1] >= vs[0];
    var col = up ? cssVar('--good', '#c62828') : cssVar('--bad', '#2e7d32');
    var area = seg.join(' ') +
      ' L' + (W - P).toFixed(1) + ',' + (H - P) + ' L' + P + ',' + (H - P) + ' Z';
    var gid = 'mg' + Math.random().toString(36).slice(2, 8);
    return '<svg class="ix-min-spark" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
      '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="' + col + '" stop-opacity=".22"/>' +
      '<stop offset="100%" stop-color="' + col + '" stop-opacity="0"/></linearGradient></defs>' +
      '<path d="' + area + '" fill="url(#' + gid + ')"/>' +
      '<path d="' + seg.join(' ') + '" fill="none" stroke="' + col +
      '" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>';
  }

  // ---------- 顶部速览条 ----------
  // 页面有 10 个区块、7 个异步接口，全貌散在 2000px 之外。这里把关键读数并列成一行，
  // 点一下跳到对应区块。★ 只并列、不加权 —— 不做「综合分」，那与全站铁律冲突。
  function renderGlance() {
    var el = $('#ixGlance');
    if (!el) return;
    var items = [];
    var d = IX.data;

    if (d && d.ok && d.temp != null) {
      items.push({ j: 'ixTemp', k: '风险温度', v: nf(d.temp, 1),
                   s: d.level, t: d.tone === 'crisis' ? 'crisis' : (d.tone === 'warn' ? 'warn' : 'calm') });
    }
    var fx = IX.ext.fx;
    if (fx && fx.ok) {
      var cny = (fx.spot || {}).cny || {};
      if (cny.price) {
        var p = cny.pct;
        items.push({ j: 'ixFxCard', k: '在岸人民币', v: nf(cny.price, 4),
                     s: p == null ? '' : ('人民币' + (p > 0 ? '贬值 ' : '升值 ') + nf(Math.abs(p), 2) + '%'),
                     t: p > 0 ? 'warn' : 'up' });
      }
      var r = fx.rate || {};
      if (r.gap != null) {
        var bp = Math.round(r.gap * 100);
        items.push({ j: 'ixFxCard', k: '中美 10Y', v: (bp > 0 ? '+' : '') + bp + 'bp',
                     s: bp < 0 ? '倒挂' : '正常', t: bp < -100 ? 'warn' : 'calm' });
      }
    }
    var opt = IX.ext.opt, hs = IX.ext.optHist;
    if (opt && opt.ok) {
      var io = (opt.items || []).filter(function (x) { return x.ok && x.key === 'io'; })[0];
      if (io && io.front && io.front.atm_iv) {
        var st = (hs && hs.stats && hs.stats.io && hs.stats.io.enough) ? hs.stats.io.state : io.atm_state;
        items.push({ j: 'ixOptCard', k: 'IO 期权IV',
                     v: nf(io.front.atm_iv * 100, 1) + '%',
                     s: (st && st.label) || '', t: (st && st.tone) || 'calm' });
      }
    }
    var br = (IX.ext.mins || {}).brent;
    if (br && br.ok && br.overnight) {
      items.push({ j: 'ixMinCard', k: '布伦特隔夜', v: nf(br.last, 2),
                   s: '收盘后 ' + pf(br.overnight.pct),
                   t: br.overnight.pct < -1 ? 'warn' : 'calm' });
    }
    var pol = IX.ext.policy;
    if (pol && pol.ok && pol.tone && pol.tone.code !== 'none') {
      items.push({ j: 'ixPolicyCard', k: '政策面', v: pol.tone.label,
                   s: '宽松' + (pol.tone.ease || 0) + ' / 紧缩' + (pol.tone.tight || 0),
                   t: pol.tone.code === 'ease' ? 'up' : (pol.tone.code === 'tight' ? 'crisis' : 'warn') });
    }
    var cal = IX.ext.calib;
    if (cal && cal.ok && cal.grade === 'flipped') {
      items.push({ j: 'ixCalibCard', k: '温度读数', v: '方向相反',
                   s: '历史标定见下方', t: 'warn' });
    }

    if (!items.length) { el.hidden = true; return; }
    el.hidden = false;
    el.innerHTML = items.map(function (x) {
      return '<a class="ix-gl t-' + x.t + '" href="#' + x.j + '" data-jump="' + x.j + '">' +
        '<span class="ix-gl-k">' + esc(x.k) + '</span>' +
        '<span class="ix-gl-v">' + esc(String(x.v)) + '</span>' +
        (x.s ? '<span class="ix-gl-s">' + esc(x.s) + '</span>' : '') + '</a>';
    }).join('');
    $$('.ix-gl[data-jump]', el).forEach(function (a) {
      a.addEventListener('click', function (ev) {
        ev.preventDefault();
        var t = document.getElementById(a.getAttribute('data-jump'));
        if (!t) return;
        t.scrollIntoView({ behavior: 'smooth', block: 'start' });
        t.classList.add('ix-hl');
        setTimeout(function () { t.classList.remove('ix-hl'); }, 1700);
      });
    });
  }

  // ---------- 汇率与中美利差 ----------
  function tinyLine(vals, color) {
    if (!vals || vals.length < 2) return '';
    var W = 300, H = 40, P = 2;
    var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    var rng = (mx - mn) || Math.abs(mx) * 0.001 || 1;
    var n = vals.length, seg = [];
    for (var i = 0; i < n; i++) {
      var x = P + (W - 2 * P) * i / (n - 1);
      var y = H - P - (H - 2 * P) * (vals[i] - mn) / rng;
      seg.push((i ? 'L' : 'M') + x.toFixed(1) + ',' + y.toFixed(1));
    }
    return '<svg class="ix-min-spark" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
      '<path d="' + seg.join(' ') + '" fill="none" stroke="' + color +
      '" stroke-width="1.4" vector-effect="non-scaling-stroke"/></svg>';
  }

  function renderFx() {
    var el = $('#ixFx');
    if (!el) return;
    var d = IX.ext.fx;
    if (!d) { el.innerHTML = '<div class="ix-empty">汇率与利差加载中…</div>'; return; }
    if (!d.ok) { el.innerHTML = '<div class="ix-empty">' + esc(d.err || '暂不可用') + '</div>'; return; }

    // ★ 汇率方向不能按红绿直觉读：USDCNY 数值涨 = 人民币贬值。
    //   所以数值本身用中性色，只用一个明确的文字标签讲清方向。
    function cnyCell(title, price, pct, extra) {
      if (price == null) return '';
      var dir = pct == null ? '' : (pct > 0 ? '贬值' : (pct < 0 ? '升值' : '持平'));
      return '<div class="ix-fx-cell"><div class="k">' + esc(title) + '</div>' +
        '<div class="v">' + nf(price, 4) + '</div>' +
        '<div class="s">' + (pct == null ? '' :
          '<span class="ix-fx-num">' + (pct > 0 ? '+' : '') + nf(pct, 2) + '%</span> ' +
          '<span class="ix-fx-dir">人民币' + dir + '</span>') + (extra || '') + '</div></div>';
    }

    var sp = d.spot || {};
    var mid = d.mid || {};
    var cny = sp.cny || {}, cnh = sp.cnh || {};
    var gap = d.cnh_gap;
    var r = d.rate || {};

    var cells = cnyCell('在岸 USDCNY', cny.price, cny.pct);
    cells += cnyCell('离岸 USDCNH', cnh.price, cnh.pct);
    cells += cnyCell('央行中间价', mid.value, mid.chg20,
      mid.pct250 == null ? '' : ' <span class="ix-fx-sub">· 250日 ' + nf(mid.pct250, 0) + '分位</span>');
    if (gap != null) {
      cells += '<div class="ix-fx-cell"><div class="k">离岸 − 在岸</div>' +
        '<div class="v">' + (gap > 0 ? '+' : '') + nf(gap, 2) + '%</div>' +
        '<div class="s"><span class="ix-fx-dir">' +
        (gap > 0.05 ? '贬值预期' : (gap < -0.05 ? '升值预期' : '基本平水')) + '</span></div></div>';
    }

    var rcells = '';
    if (r.cn10y != null) {
      rcells += '<div class="ix-fx-cell"><div class="k">中国 10Y</div><div class="v">' +
        nf(r.cn10y, 2) + '%</div><div class="s ix-fx-sub">' + esc(r.cn_date || '') + '</div></div>';
    }
    if (r.us10y != null) {
      rcells += '<div class="ix-fx-cell"><div class="k">美国 10Y</div><div class="v">' +
        nf(r.us10y, 2) + '%</div><div class="s ix-fx-sub">' + esc(r.us_date || '') + '</div></div>';
    }
    if (r.gap != null) {
      var bp = Math.round(r.gap * 100);
      rcells += '<div class="ix-fx-cell"><div class="k">10Y 利差</div><div class="v">' +
        (bp > 0 ? '+' : '') + bp + '<small>bp</small></div>' +
        '<div class="s"><span class="ix-fx-dir">' + (bp < 0 ? '倒挂' : '正常') + '</span>' +
        (r.gap_chg20 == null ? '' : ' <span class="ix-fx-sub">20日 ' +
          (r.gap_chg20 > 0 ? '+' : '') + nf(r.gap_chg20, 0) + 'bp</span>') + '</div></div>';
    }

    var fxVals = (d.series && d.series.fx || []).map(function (x) { return x.v; });
    var gapVals = (d.series && d.series.gap || []).map(function (x) { return x.v; });
    var accent = cssVar('--accent', '#4a9eff');
    var warn = cssVar('--warn-ink', '#b26a00');
    // ★ 三个期限并列给：只报一个等于挑好看的。实测 5 日 −0.12 / 20 日 +0.04 / 60 日 +0.11，
    //   符号都不一致，那就得直说「这条关系不稳定」。
    var cf = (d.corr || {}).fx || {};
    var ckeys = ['5', '20', '60'].filter(function (k) { return cf[k] != null; });
    var ctxt = '';
    if (ckeys.length) {
      var pos = ckeys.filter(function (k) { return cf[k] > 0; }).length;
      var neg = ckeys.filter(function (k) { return cf[k] < 0; }).length;
      var mixed = pos > 0 && neg > 0;
      var allWeak = ckeys.every(function (k) { return Math.abs(cf[k]) < 0.1; });
      ctxt = '汇率变化 → 沪深300 未来同期限收益的历史相关性：' +
        ckeys.map(function (k) {
          return k + ' 日 <b>' + (cf[k] > 0 ? '+' : '') + nf(cf[k], 3) + '</b>';
        }).join(' · ') +
        (mixed
          ? '。<b>三个期限符号不一致</b> —— 这条关系不稳定，别拿单一期限的结论去做判断'
          : (allWeak ? '。三个期限都在 ±0.1 内，<b>几乎无关</b>，「贬值所以看空 A 股」在这段样本里没有统计支持'
                     : '。三个期限同号，方向相对稳定，但绝对值仍小')) +
        '。<br>';
    }

    el.innerHTML =
      '<div class="ix-fx-grid">' + cells + '</div>' +
      (fxVals.length > 2
        ? '<div class="ix-fx-chart"><span class="ix-fx-ct">人民币中间价 · 近 ' + fxVals.length + ' 个交易日</span>' +
          tinyLine(fxVals, accent) + '</div>' : '') +
      (rcells ? '<div class="ix-fx-grid ix-fx-rates">' + rcells + '</div>' : '') +
      (gapVals.length > 2
        ? '<div class="ix-fx-chart"><span class="ix-fx-ct">中美 10Y 利差（%，负值=倒挂）</span>' +
          tinyLine(gapVals, warn) + '</div>' : '') +
      '<div class="ix-pol-foot">' +
      ctxt +
      '⚠ ' + esc((d.caveats || []).join('；')) + '</div>';
  }

  function renderMin() {
    var el = $('#ixMin');
    if (!el) return;
    var m = IX.ext.mins || {};
    var order = ['a50', 'brent', 'IF0'];
    var cards = order.map(function (k) {
      var d = m[k];
      if (!d) return '';
      if (!d.ok) {
        return '<div class="ix-min-card"><div class="ix-min-top"><span class="ix-min-name">' +
          esc(d.short || k) + '</span><span class="ix-min-pct ix-flat">' + esc(d.err || '不可用') +
          '</span></div></div>';
      }
      var ov = d.overnight;
      var ovHtml = '';
      if (ov) {
        ovHtml = '<div class="ix-min-ov"><span class="ix-ov-k">隔夜 ' + esc(ov.base_time) +
          '→' + esc((d.points[d.points.length - 1] || {}).t || '') +
          (ov.hours != null ? '（' + nf(ov.hours, 1) + 'h）' : '') + '</span>' +
          '<b class="' + cls(ov.pct) + '">' + pf(ov.pct) + '</b>' +
          '<span>' + nf(ov.base_price, d.dec) + ' → ' + nf(ov.last_price, d.dec) + '</span></div>';
      } else {
        ovHtml = '<div class="ix-min-ov"><span class="ix-ov-k">' +
          (d.key === 'IF0' ? '股指期货无夜盘，隔夜段不适用' : '数据只覆盖到 15:00 前，无法算隔夜段') +
          '</span></div>';
      }
      return '<div class="ix-min-card">' +
        '<div class="ix-min-top"><span class="ix-min-name">' + esc(d.short) + '</span>' +
        '<span class="ix-min-px ' + cls(d.intraday_pct) + '">' + nf(d.last, d.dec) + '</span>' +
        '<span class="ix-min-pct ' + cls(d.intraday_pct) + '">日内 ' + pf(d.intraday_pct) + '</span>' +
        '<span class="ix-min-pct ix-flat" style="margin-left:auto">' + esc(d.day || '') +
        ' · ' + d.n + ' 点</span></div>' +
        ovHtml + minSpark(d.points) + '</div>';
    }).join('');
    el.innerHTML = cards
      ? '<div class="ix-min-grid">' + cards + '</div>'
      : '<div class="ix-empty">分时数据暂不可用</div>';
  }

  // ---------- 期权 ----------
  function renderOpt() {
    var el = $('#ixOpt');
    if (!el) return;
    var d = IX.ext.opt;
    if (!d) { el.innerHTML = '<div class="ix-empty">期权链加载中…</div>'; return; }
    var items = (d.items || []).filter(function (x) { return x.ok; });
    var fails = (d.items || []).filter(function (x) { return !x.ok; });
    if (!items.length) {
      el.innerHTML = '<div class="ix-empty">期权数据暂不可用：' +
        esc((d.err || '') + ' ' + fails.map(function (x) { return x.name + '(' + x.err + ')'; }).join(' ')) +
        '</div>';
      return;
    }
    if (!IX.ext.optPick || !items.some(function (x) { return x.key === IX.ext.optPick; })) {
      IX.ext.optPick = items[0].key;
    }
    var cards = items.map(function (it) {
      var f = it.front || {};
      var checks = it.checks || {};
      // ★ 有历史分位就用分位判高低，没有才退回经验绝对刻度。
      //   实测 IO 21.45% 在自身 120 日里是 80.8 分位，经验刻度会判成「常态」。
      var hsLoaded = !!(IX.ext.optHist && IX.ext.optHist.stats);
      var hs = hsLoaded ? (IX.ext.optHist.stats[it.key] || { n: 0 }) : null;
      var usePct = !!(hs && hs.enough);
      // ETF 期权没有历史 CSV，改用官方波指 QVIX 的分位（口径不同，只用来判贵贱）
      var qv = (it.key === 'etf300' && IX.ext.qvix && IX.ext.qvix.ok) ? IX.ext.qvix : null;
      var st = qv ? qv.state : ((usePct && hs.state) ? hs.state : (it.atm_state || {}));
      var pctTag;
      if (qv) {
        pctTag = '<span class="ix-opt-pct" title="中国波指 QVIX：官方编制、30 天恒定期限口径，' +
          '与本卡片的近月 ATM IV 不是同一口径，只用来判断当前贵不贵。数据日 ' +
          esc(qv.date) + (qv.stale ? '（滞后 ' + qv.lag_days + ' 天，源未更新）' : '') +
          '，样本 ' + qv.n + ' 日">QVIX ' + nf(qv.close, 1) + ' · ' + nf(qv.pct, 0) + '% 分位</span>';
      } else if (usePct) {
        pctTag = '<span class="ix-opt-pct" title="当前固定 30 天 IV 在近 ' + hs.n +
          ' 个交易日中的分位">分位 ' + nf(hs.pct120, 0) + '%</span>';
      } else if (hsLoaded) {
        pctTag = '<span class="ix-opt-pct dim" title="ETF 期权没有历史 CSV，只能从本模块启用当天起逐日累积">' +
          (hs.n ? '样本 ' + hs.n + ' 天' : '无历史样本') + '</span>';
      } else {
        pctTag = '<span class="ix-opt-pct dim">分位加载中</span>';
      }
      var liveTag = it.data_mode === 'live'
        ? '<span class="ix-opt-live" title="盘中用中金所延时快照的买卖中价算 IV，不是 T-1">实时</span>'
        : '';
      var chk = checks.n
        ? '<span class="ix-fact" title="用反解出的 IV 回代算 Delta，与中金所官方 Delta 比对">Δ 校验 ' +
          nf(checks.med_abs, 4) + (checks.pass ? ' ✓' : ' ⚠') + '</span>'
        : '<span class="ix-fact" title="ETF 期权无官方希腊字母，无法交叉验证">无官方 Δ 可比对</span>';
      return '<div class="ix-opt-card' + (it.key === IX.ext.optPick ? ' on' : '') +
        '" data-ok="' + esc(it.key) + '">' +
        '<div class="ix-opt-h"><span class="ix-opt-t">' + esc(it.short) + ' ' + esc(it.name) +
        '</span>' + liveTag + '<span class="ix-tag ' + toneTag(st.tone) + '">' +
        esc(st.label || '—') + '</span></div>' +
        '<div class="ix-opt-big">' + (f.atm_iv == null ? '—' : nf(f.atm_iv * 100, 2)) + '<small>% ATM IV</small>' + pctTag + '</div>' +
        '<div class="ix-opt-meta">近月 <b>' + esc(f.yymm || '') + '</b> 到期 ' + esc(f.expiry || '') +
        '（' + (f.days || 0) + ' 天）· 平值行权价 <b>' + nf(it.slices[0].atm_strike, 0) + '</b><br>' +
        '25Δ 偏斜 <b>' + (f.skew25 == null ? '—' : (f.skew25 > 0 ? '+' : '') + nf(f.skew25, 2) + 'pp') +
        '</b> <span class="ix-tag ' + toneTag((it.skew_state || {}).tone) + '">' +
        esc((it.skew_state || {}).label || '—') + '</span>' +
        (it.slope == null ? '' : ' · 期限斜率 <b>' + (it.slope > 0 ? '+' : '') + nf(it.slope * 100, 2) + 'pp</b>') +
        '</div>' +
        '<div class="ix-opt-kv">' + chk +
        '<span class="ix-fact">' + (it.slices.length) + ' 个到期月</span>' +
        (usePct ? '<span class="ix-fact" title="历史区间">区间 ' +
          nf(hs.p20 * 100, 1) + '~' + nf(hs.p80 * 100, 1) + '%（20~80 分位）</span>' : '') +
        '</div></div>';
    }).join('');
    var cur = items.filter(function (x) { return x.key === IX.ext.optPick; })[0] || items[0];
    el.innerHTML = '<div class="ix-opt-grid">' + cards + '</div>' +
      '<div class="ix-panel-h" style="border-top:1px solid var(--line-soft)">' +
      esc(cur.short) + ' 波动率微笑 <span class="ix-hint">横轴 = 行权价 / 远期（1.0 = 平值）· 纵轴 = IV</span></div>' +
      '<div class="ix-opt-chart"><canvas id="ixOptCanvas"></canvas></div>' +
      '<div class="ix-opt-legend" id="ixOptLegend"></div>' +
      '<div class="ix-opt-fail">口径：' + esc(cur.source || '') +
      ' · 远期由 put-call parity 反推（F<sub>opt</sub> ' + nf(cur.slices[0].fwd_opt, 2) +
      (cur.future && cur.future.settle ? ' / 期货 ' + esc(cur.future.code || '') + ' ' + nf(cur.future.settle, 2) : '') +
      '）· 剩余年限 ACT/365 · 无风险利率 ' + nf((cur.risk_free || 0) * 100, 1) + '%</div>';
    $$('.ix-opt-card[data-ok]', el).forEach(function (c) {
      c.addEventListener('click', function () {
        IX.ext.optPick = c.getAttribute('data-ok');
        renderOpt();
      });
    });
    requestAnimationFrame(function () { drawOptChart(cur); });
  }

  function drawOptChart(it) {
    var cv = $('#ixOptCanvas');
    if (!cv || !it) return;
    var wrap = cv.parentNode;
    var W = Math.max(wrap.clientWidth - 8, 320), H = 280;
    var dpr = window.devicePixelRatio || 1;
    cv.width = W * dpr; cv.height = H * dpr;
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
    var ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    var L = 46, R = 54, T = 14, B = 26;
    var slices = it.slices || [];
    var xs = [], ys = [];
    slices.forEach(function (s) {
      s.points.forEach(function (p) { xs.push(p.m); ys.push(p.iv * 100); });
    });
    if (!xs.length) return;
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    var padY = (y1 - y0) * 0.12 || 1;
    y0 = Math.max(0, y0 - padY); y1 = y1 + padY;
    var px = function (v) { return L + (W - L - R) * (v - x0) / ((x1 - x0) || 1); };
    var py = function (v) { return H - B - (H - T - B) * (v - y0) / ((y1 - y0) || 1); };

    var ink = cssVar('--ink-faint', '#888');
    var line = cssVar('--line-soft', '#e5e5e5');
    ctx.font = '10px ui-monospace,Consolas,monospace';
    ctx.strokeStyle = line; ctx.lineWidth = 1;
    // Y 网格（5 条）
    for (var i = 0; i <= 4; i++) {
      var yv = y0 + (y1 - y0) * i / 4;
      var yy = py(yv);
      ctx.beginPath(); ctx.moveTo(L, yy); ctx.lineTo(W - R, yy); ctx.stroke();
      ctx.fillStyle = ink; ctx.textAlign = 'right';
      ctx.fillText(yv.toFixed(1) + '%', L - 6, yy + 3);
    }
    // 平值竖线
    if (x0 <= 1 && x1 >= 1) {
      ctx.save();
      ctx.setLineDash([3, 3]); ctx.strokeStyle = cssVar('--accent', '#4a9eff');
      ctx.beginPath(); ctx.moveTo(px(1), T); ctx.lineTo(px(1), H - B); ctx.stroke();
      ctx.restore();
      ctx.fillStyle = cssVar('--accent', '#4a9eff'); ctx.textAlign = 'center';
      ctx.fillText('平值', px(1), H - B + 14);
    }
    // X 刻度
    ctx.fillStyle = ink; ctx.textAlign = 'center';
    for (var j = 0; j <= 4; j++) {
      var xv = x0 + (x1 - x0) * j / 4;
      ctx.fillText(xv.toFixed(2), px(xv), H - B + 14);
    }

    var COLORS = ['#4a9eff', '#ffa726', '#66bb6a', '#ab47bc', '#ef5350', '#26c6da'];
    var legend = [];
    slices.forEach(function (s, k) {
      var col = COLORS[k % COLORS.length];
      ctx.strokeStyle = col; ctx.lineWidth = 1.8;
      ctx.beginPath();
      s.points.forEach(function (p, idx) {
        var X = px(p.m), Y = py(p.iv * 100);
        if (idx === 0) ctx.moveTo(X, Y); else ctx.lineTo(X, Y);
      });
      ctx.stroke();
      // 右端标签
      var last = s.points[s.points.length - 1];
      if (last) {
        ctx.fillStyle = col; ctx.textAlign = 'left';
        ctx.fillText(s.days + 'd', W - R + 6, py(last.iv * 100) + 3);
      }
      legend.push('<span><i style="background:' + col + '"></i>' + esc(s.yymm) +
        ' · ' + s.days + 'd · ATM ' + (s.atm_iv == null ? '—' : nf(s.atm_iv * 100, 2) + '%') + '</span>');
    });
    var lg = $('#ixOptLegend');
    if (lg) lg.innerHTML = legend.join('');
  }

  // ---------- 政策面 ----------
  function renderPolicy() {
    var el = $('#ixPolicy');
    if (!el) return;
    var d = IX.ext.policy;
    if (!d) { el.innerHTML = '<div class="ix-empty">政策事件扫描中…</div>'; return; }
    if (d.unavailable || !d.events) {
      el.innerHTML = '<div class="ix-empty">' + esc(d.err || '政策面暂不可用') +
        '<br><span style="font-size:11px">主面板与其它区块不受影响</span></div>';
      return;
    }
    var t = d.tone || {};
    var f = d.filtered || {};
    var head = '<div class="ix-pol-head">' +
      '<span class="ix-pol-tone tone-' + esc(t.code || 'none') + '">' + esc(t.label || '—') + '</span>' +
      '<span class="ix-fact">宽松 ' + (t.ease || 0) + ' · 紧缩 ' + (t.tight || 0) +
      ' · 中性 ' + (t.neutral || 0) + '</span>' +
      '<span class="ix-pol-conf">' +
      '<span class="ix-fact" title="快讯源一次能抓到的总条数">扫 ' + (d.n_news || 0) + ' 条</span>' +
      '<span class="ix-fact" title="过了噪声与弱信号两道筛之后剩下的事件数">命中 ' + (d.n_hit || 0) + '</span>' +
      '<span class="ix-fact" title="海外主体/公司公告 + 只有主体没有动作的低信息条数">剔 ' +
      ((f.noise || 0) + (f.weak || 0)) + '</span>' +
      '</span></div>';

    var list = (d.events || []).map(function (e) {
      var ttl = esc(e.title);
      if (e.url) ttl = '<a href="' + esc(e.url) + '" target="_blank" rel="noopener">' + ttl + '</a>';
      return '<div class="ix-pol-item">' +
        '<span class="ix-pol-time">' + esc((e.time || '').slice(-5) || '—') + '</span>' +
        '<span class="ix-pol-ag">' + esc(e.agency || '—') + '</span>' +
        '<span class="ix-pol-dir d-' + esc(e.dir) + '">' + esc(e.dir) + '</span>' +
        '<span class="ix-pol-title">' + ttl + '</span></div>';
    }).join('');
    if (!list) list = '<div class="ix-empty" style="margin:12px 13px">近 7 日未见政策相关快讯（不是没政策，是快讯源里没有命中）</div>';

    var wins = (d.windows || []).filter(function (w) { return w.days <= 30; }).map(function (w) {
      return '<span class="ix-fact" title="' + esc(w.note) + '">' + esc(w.name) + ' ' +
        (w.days === 0 ? '今天' : w.days + ' 天后') + '</span>';
    }).join('');
    var ags = (d.agencies || []).slice(0, 6).map(function (a) {
      return '<span class="ix-fact">' + esc(a.name) + ' ' + a.n + '</span>';
    }).join('');
    var ths = (d.themes || []).slice(0, 5).map(function (a) {
      return '<span class="ix-fact">' + esc(a.name) + ' ' + a.n + '</span>';
    }).join('');

    el.innerHTML = head + '<div class="ix-pol-list">' + list + '</div>' +
      ((ags || ths) ? '<div class="ix-pol-foot" style="padding-bottom:4px">部委：' + ags +
        '<br>主题：' + ths + '</div>' : '') +
      (wins ? '<div class="ix-pol-win"><span class="ix-fact" style="background:transparent;border:none;padding-left:0">惯例窗口：</span>' + wins + '</div>' : '') +
      '<div class="ix-pol-foot">⚠ ' + esc((d.caveats || []).join('；')) + '</div>';
  }

  // ---------- 权重标定 ----------
  function renderCalib() {
    var el = $('#ixCalib');
    if (!el) return;
    var d = IX.ext.calib;
    if (!d) { el.innerHTML = '<div class="ix-empty">标定计算中…</div>'; return; }
    if (!d.ok) { el.innerHTML = '<div class="ix-empty">' + esc(d.err || '标定失败') + '</div>'; return; }

    var W = d.base.weights, B = d.best.weights;
    var rows = Object.keys(W).map(function (k) {
      var nm = (IX.data && IX.data.dim_names && IX.data.dim_names[k]) || k;
      var bw = W[k], ow = B[k];
      return '<tr><td>' + esc(nm) + '</td>' +
        '<td>' + (bw * 100).toFixed(1) + '%</td>' +
        '<td><span class="ix-cal-bar" style="width:' + Math.round(bw * 160) + 'px"></span></td>' +
        '<td>' + (ow * 100).toFixed(1) + '%</td>' +
        '<td><span class="ix-cal-bar neg" style="width:' + Math.round(ow * 160) + 'px"></span></td></tr>';
    }).join('');

    var single = (d.single || []).map(function (s) {
      var v = s.ic_all;
      return '<tr><td>' + esc(s.name) + '</td>' +
        '<td class="' + (v < 0 ? 'ix-down' : 'ix-up') + '">' + (v > 0 ? '+' : '') + nf(v, 3) + '</td>' +
        '<td class="' + (s.ic_out < 0 ? 'ix-down' : 'ix-up') + '">' + (s.ic_out > 0 ? '+' : '') + nf(s.ic_out, 3) + '</td>' +
        '<td style="font-family:var(--font);font-size:11px;color:var(--ink-faint);text-align:left">' +
        (v < -0.05 ? '风险型（高→跌）' : (v > 0.05 ? '反向型（高→涨）' : '≈ 噪声')) + '</td></tr>';
    }).join('');

    // 多期限：同一组权重换个期限还成不成立
    var multi = (d.multi || []).map(function (m) {
      if (m.base_all == null) return '';
      return '<tr><td>' + m.h + ' 日</td>' +
        '<td class="' + (m.base_all < 0 ? 'ix-down' : 'ix-up') + '">' + (m.base_all > 0 ? '+' : '') + nf(m.base_all, 3) + '</td>' +
        '<td class="' + (m.base_out < 0 ? 'ix-down' : 'ix-up') + '">' + (m.base_out > 0 ? '+' : '') + nf(m.base_out, 3) + '</td>' +
        '<td class="' + (m.best_all < 0 ? 'ix-down' : 'ix-up') + '">' + (m.best_all > 0 ? '+' : '') + nf(m.best_all, 3) + '</td>' +
        '<td class="' + (m.best_out < 0 ? 'ix-down' : 'ix-up') + '">' + (m.best_out > 0 ? '+' : '') + nf(m.best_out, 3) + '</td></tr>';
    }).join('');

    // 滚动 3 折：单段样本外很容易是「刚好这一段」，多折才看得出稳不稳
    var walk = (d.walk || []).map(function (w) {
      return '<tr><td>第 ' + w.fold + ' 折</td>' +
        '<td style="font-family:var(--font);font-size:11px;text-align:left">' +
        esc(w.test[0]) + ' → ' + esc(w.test[1]) + '</td>' +
        '<td>' + w.n_test + '</td>' +
        '<td class="' + (w.base_out < 0 ? 'ix-down' : 'ix-up') + '">' + (w.base_out > 0 ? '+' : '') + nf(w.base_out, 3) + '</td>' +
        '<td class="' + (w.best_out < 0 ? 'ix-down' : 'ix-up') + '">' + (w.best_out > 0 ? '+' : '') + nf(w.best_out, 3) + '</td></tr>';
    }).join('');

    var bk = function (arr) {
      return arr.map(function (b, i) {
        return '<div class="ix-cal-bk"><div class="k">第 ' + (i + 1) + ' 档</div>' +
          '<div class="v ' + cls(b.avg) + '">' + (b.avg > 0 ? '+' : '') + nf(b.avg, 2) + '%</div>' +
          '<div class="w">胜率 ' + nf(b.win, 0) + '%</div></div>';
      }).join('');
    };

    el.innerHTML =
      '<div class="ix-cal-verdict g-' + esc(d.grade) + '">' + d.verdict.replace(/\*\*/g, '') +
      '<span class="ix-cal-g">样本 ' + d.n_days + ' 天（' + esc(d.range[0]) + ' → ' + esc(d.range[1]) +
      '）· 未来 ' + d.horizon + ' 日收益 · 训练 ' + esc(d.split.train[0]) + '~' + esc(d.split.train[1]) +
      ' / 测试 ' + esc(d.split.test[0]) + '~' + esc(d.split.test[1]) +
      '（隔离带 ' + d.split.gap_days + ' 日）· 搜索 ' + d.n_search + ' 组 · 单维上限 ' +
      (d.max_weight * 100).toFixed(0) + '%</span></div>' +

      '<div class="ix-panel-h" style="border-top:1px solid var(--line-soft)">分档检验' +
      '<span class="ix-hint">按温度分 5 档，看未来 ' + d.horizon + ' 日平均收益 —— 越单调越说明温度有方向信息</span></div>' +
      '<div class="ix-cal-buckets">' + bk(d.base.buckets) + '</div>' +

      '<div class="ix-panel-h" style="border-top:1px solid var(--line-soft)">单维 IC' +
      '<span class="ix-hint">每个维度单独与未来收益的秩相关（负 = 越高越跌）</span></div>' +
      '<div class="table-scroll"><table class="ix-cal-tbl"><thead><tr>' +
      '<th>维度</th><th>全样本 IC</th><th>样本外 IC</th><th>性质</th></tr></thead><tbody>' +
      single + '</tbody></table></div>' +

      '<div class="ix-panel-h" style="border-top:1px solid var(--line-soft)">权重对比' +
      '<span class="ix-hint">经验值 vs 标定值（标定值受单维上限约束，不是无约束最优）</span></div>' +
      '<div class="table-scroll"><table class="ix-cal-tbl"><thead><tr>' +
      '<th>维度</th><th>经验权重</th><th></th><th>标定权重</th><th></th></tr></thead><tbody>' +
      rows + '</tbody></table></div>' +
      '<div class="ix-cal-tbl" style="padding:8px 13px;font-size:11.5px;color:var(--ink-faint);text-align:left">' +
      '温度 IC：经验 <b>' + (d.base.ic_all > 0 ? '+' : '') + nf(d.base.ic_all, 3) + '</b>（全样本）/ ' +
      (d.base.ic_out > 0 ? '+' : '') + nf(d.base.ic_out, 3) + '（样本外）　·　标定 ' +
      (d.best.ic_all > 0 ? '+' : '') + nf(d.best.ic_all, 3) + ' / ' +
      (d.best.ic_out > 0 ? '+' : '') + nf(d.best.ic_out, 3) + '</div>' +

      (multi ? '<div class="ix-panel-h" style="border-top:1px solid var(--line-soft)">多期限 IC' +
        '<span class="ix-hint">换个期限还成不成立 —— 方向打架就说明「一个温度」这个说法不成立</span></div>' +
        '<div class="table-scroll"><table class="ix-cal-tbl"><thead><tr>' +
        '<th>期限</th><th>经验·全样本</th><th>经验·样本外</th><th>标定·全样本</th><th>标定·样本外</th>' +
        '</tr></thead><tbody>' + multi + '</tbody></table></div>' : '') +

      (walk ? '<div class="ix-panel-h" style="border-top:1px solid var(--line-soft)">滚动 3 折' +
        '<span class="ix-hint">单段样本外很容易是「刚好这一段」，多折才看得出稳不稳</span></div>' +
        '<div class="table-scroll"><table class="ix-cal-tbl"><thead><tr>' +
        '<th>折</th><th>测试区间</th><th>样本</th><th>经验</th><th>标定</th>' +
        '</tr></thead><tbody>' + walk + '</tbody></table></div>' : '') +

      '<div class="ix-pol-foot">⚠ ' + esc((d.caveats || []).join('；')) + '</div>';
  }

  // ---------- 异步加载（主面板跑完再触发，互不阻塞） ----------
  function loadExtras(force) {
    IX.ext.started = true;
    var jobs = [];

    jobs.push(API.mins('a50').then(function (j) { (IX.ext.mins = IX.ext.mins || {}).a50 = j; })
      .catch(function () {}));
    jobs.push(API.mins('brent').then(function (j) { (IX.ext.mins = IX.ext.mins || {}).brent = j; })
      .catch(function () {}));
    jobs.push(API.mins('IF0').then(function (j) { (IX.ext.mins = IX.ext.mins || {}).IF0 = j; })
      .catch(function () {}));

    if (!IX.ext.opt || force) {
      jobs.push(API.opt(force).then(function (j) { IX.ext.opt = j; }).catch(function (e) {
        IX.ext.opt = { ok: false, items: [], err: '期权面板请求失败：' + (e && e.message ? e.message : e) };
      }));
    }
    if (!IX.ext.policy || force) {
      jobs.push(API.policy(force).then(function (j) { IX.ext.policy = j; }).catch(function (e) {
        IX.ext.policy = { ok: false, events: [], err: '政策面请求失败：' + (e && e.message ? e.message : e) };
      }));
    }
    if (!IX.ext.calib || force) {
      jobs.push(API.calib(force).then(function (j) { IX.ext.calib = j; }).catch(function (e) {
        IX.ext.calib = { ok: false, err: '标定请求失败：' + (e && e.message ? e.message : e) };
      }));
    }
    // 历史 IV 分位单独走：它要回溯 120 个交易日的 CSV（首次 15~30s），
    // 绝不能挡住期权卡片本身 —— 先用当日横截面画出来，分位到了再重绘一次。
    if (!IX.ext.optHist || force) {
      API.optHist(force).then(function (j) {
        IX.ext.optHist = j;
        if ($('#ixOpt')) renderOpt();
        renderGlance();
      }).catch(function () { IX.ext.optHist = { ok: false, stats: {} }; });
    }
    // 中国波指（2s 内）——ETF 期权的历史分位只能靠它
    if (!IX.ext.qvix || force) {
      API.qvix(force).then(function (j) {
        IX.ext.qvix = j;
        if ($('#ixOpt')) renderOpt();
        renderGlance();
      }).catch(function () { IX.ext.qvix = { ok: false }; });
    }
    // 汇率与利差（首次约 6s）
    if (!IX.ext.fx || force) {
      API.fx(force).then(function (j) {
        IX.ext.fx = j;
        if ($('#ixFx')) renderFx();
        renderGlance();
      }).catch(function () { IX.ext.fx = { ok: false }; });
    }

    // 分时先到先画；其余各自到各自画
    jobs[0].then(renderMin);
    jobs[1].then(renderMin);
    jobs[2].then(renderMin);
    return Promise.all(jobs).then(function () {
      if ($('#ixMin')) renderMin();
      if ($('#ixOpt')) renderOpt();
      if ($('#ixPolicy')) renderPolicy();
      if ($('#ixCalib')) renderCalib();
      if ($('#ixFx')) renderFx();
      renderGlance();
      // 标定结果到了要回填到温度卡（否则「高危」和「高温后偏涨」两句话会自相矛盾）
      if (IX.data) {
        renderTemp($('#ixTemp'));
        renderTemp($('#ixFlashTemp'));
      }
    }).catch(function () {});
  }

  // ---------- 数据加载 ----------
  function setLoading(on, where) {
    IX.loading = on;
    var el = where ? $(where) : null;
    if (el && on) el.innerHTML = '<div class="ix-loading"><span class="ix-spin"></span>正在拉取全球指数与期现数据（首次约 5-10 秒）…</div>';
  }

  // ---------- 拉取进度（后端每完成一个数据源就更新，前端 220ms 轮询） ----------
  var PROG_TIMER = null;
  var progStartTs = 0;

  function startProgress() {
    progStartTs = Date.now();
    ['#ixProg', '#ixFlashProg'].forEach(function (s) {
      var el = $(s);
      if (el) { el.hidden = false; }
    });
    ['#ixProgFill', '#ixFlashProgFill'].forEach(function (s) {
      var f = $(s);
      if (f) f.style.width = '0%';
    });
    if (PROG_TIMER) clearInterval(PROG_TIMER);
    PROG_TIMER = setInterval(function () {
      fetch('/api/idxhub/progress').then(function (r) { return r.json(); }).then(function (p) {
        var pct = p.pct || 0;
        ['#ixProgFill', '#ixFlashProgFill'].forEach(function (s) {
          var f = $(s);
          if (f) f.style.width = pct + '%';
        });
        var txt = (p.msg || '') + (p.total > 1 ? '（' + pct.toFixed(0) + '%）' : '');
        ['#ixProgTxt', '#ixFlashProgTxt'].forEach(function (s) {
          var t = $(s);
          if (t) t.textContent = txt;
        });
        // 后端还没开始拉（刚点下去那 200ms）不要急着收起进度条
        if (!p.active) {
          if (IX.loading && (Date.now() - progStartTs) < 2500) return;
          finishProgress(pct >= 100);
        }
      }).catch(function () { /* 轮询失败不打扰用户 */ });
    }, 220);
  }

  function finishProgress(done) {
    if (PROG_TIMER) { clearInterval(PROG_TIMER); PROG_TIMER = null; }
    var hide = function () {
      ['#ixProg', '#ixFlashProg'].forEach(function (s) {
        var el = $(s);
        if (el) el.hidden = true;
      });
    };
    if (done) {
      // 收尾补到 100%：否则会停在「88.9%」这种半截状态一闪而过
      ['ixProgFill', 'ixFlashProgFill'].forEach(function (id) {
        var f = document.getElementById(id);
        if (f) f.style.width = '100%';
      });
      setTimeout(hide, 650);
    } else hide();
  }

  function loadPanel(force) {
    if (IX.loading) return Promise.resolve(IX.data);
    setLoading(true, '#ixSignals');
    setLoading(true, '#ixFlashSignals');
    startProgress();
    return API.panel(!!force).then(function (j) {
      IX.loading = false;
      IX.data = j;
      IX.dataTs = Date.now();
      IX.err = (j && !j.ok) ? (j.err || '加载失败') : null;
      renderAll();
      finishProgress(true);
      return j;
    }).catch(function (e) {
      IX.loading = false;
      IX.err = '网络请求失败：' + (e && e.message ? e.message : e);
      IX.data = { ok: false, err: IX.err };
      renderAll();
      finishProgress(false);
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
    if (b.refresh) b.refresh.addEventListener('click', function () {
      loadPanel(true);
      loadExtras(true);        // 刷新时把四块重资产也一起强制重取
    });
    if (b.flashRefresh) b.flashRefresh.addEventListener('click', function () { loadPanel(true); });
    // ★「一键分析」= 立刻给结论：2 分钟内的数据直接用，不重新拉
    //   （force 重拉要 9 秒，用户点分析不是为了等下载）
    if (b.analyze) b.analyze.addEventListener('click', function () {
      if (IX.loading) return;
      var stale = !IX.data || (Date.now() - (IX.dataTs || 0)) > 120000;
      loadPanel(stale).then(function () { setTimeout(scrollToAnalysis, 120); });
    });
    if (b.flashAnalyze) b.flashAnalyze.addEventListener('click', function () {
      goTab();
      var stale = !IX.data || (Date.now() - (IX.dataTs || 0)) > 120000;
      loadPanel(stale).then(function () { setTimeout(scrollToAnalysis, 260); });
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

  // 每天首次打开时弹一次（2026-11-05 调整）
  // ★ 原行为是「每次刷新都弹近全屏浮层」，会挡住全部页签、必须先关掉才能干活。
  //   现在按自然日记忆：当天第一次打开才弹，之后刷新不再打扰；
  //   仍可随时点顶部「🌐 全球快照」手动打开。
  // ★ 必须先 initIdxhub()：悬浮窗是直接打开、不走 switchTab，
  //   不先绑定按钮就会「点上没反应」（关不掉、进不了页签）。
  function autoOpen() {
    var box = $('#ixFlash');
    if (!box) return;
    if (box.hidden === false) return;
    try {
      var d = new Date();
      var today = d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
      if (localStorage.getItem('idxFlashSeenDay') === today) return;
      localStorage.setItem('idxFlashSeenDay', today);
    } catch (e) { /* 隐私模式读不到 localStorage：退回每次弹 */ }
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
