/* ==========================================================================
 * 筛股 · K 线工作站（kline.js）——「行情查看」页签的独立渲染模块
 * --------------------------------------------------------------------------
 * 设计约定（零侵入）：
 *   · app.js 的 loadKline() 委托到 window.KlineView.load()（行为不变）
 *   · 数据走 /api/kline（个股 daily / 指数 ant_index），不改其它模块
 *   · 配色全部读取 CSS 变量（--k-*），支持明 / 暗主题切换后重绘
 *   · 交互：悬停十字光标 + 数据浮框 / 滚轮缩放 / 拖拽平移 / 双击复位
 * 结构：单个 <canvas> 内上下分区（价格主图 70% + 成交量副图）
 * ========================================================================== */
(function () {
  'use strict';

  var $id = function (id) { return document.getElementById(id); };
  var FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';

  /* ---------------- 主题色读取（带缓存；切换主题时 clearColors()） ---------------- */
  var _colors = null;
  function C() {
    if (_colors) return _colors;
    var s = {};
    try { s = getComputedStyle(document.documentElement); } catch (e) { s = { getPropertyValue: function () { return ''; } }; }
    var g = function (n, fb) { var v = (s.getPropertyValue(n) || '').trim(); return v || fb; };
    _colors = {
      up: g('--k-up', '#e5484d'),
      down: g('--k-down', '#22a06b'),
      grid: g('--k-grid', 'rgba(128,128,128,.18)'),
      gridSoft: g('--k-grid-soft', 'rgba(128,128,128,.10)'),
      axis: g('--k-axis', '#888888'),
      cross: g('--k-cross', 'rgba(200,200,200,.55)'),
      ma: [g('--k-ma5', '#e8a838'), g('--k-ma10', '#7c9ef0'), g('--k-ma20', '#c56cf0'), g('--k-ma60', '#8a8f98')],
      volUp: g('--k-volup', 'rgba(229,72,77,.55)'),
      volDown: g('--k-voldn', 'rgba(34,160,107,.55)'),
      volMa: g('--k-volma', '#e8a838'),
      /* 布林线（BOLL 20, 2σ）：上/下轨一色、中轨另一色、带填充 */
      boll: g('--k-boll', '#5aa9e6'),
      bollMid: g('--k-boll-mid', '#f0a04b'),
      bollFill: g('--k-boll-fill', 'rgba(90,169,230,.07)'),
    };
    return _colors;
  }
  function clearColors() { _colors = null; }

  /* ---------------- 数值格式化 ---------------- */
  function fmtNum(v, d) {
    if (v == null || !isFinite(v)) return '—';
    var digits = d == null ? 2 : d;
    var s = v.toFixed(digits);
    if (Math.abs(v) >= 100000) s = Number(s).toLocaleString('zh-CN', { maximumFractionDigits: digits });
    return s;
  }
  function fmtPct(v) { return (v >= 0 ? '+' : '') + v.toFixed(2) + '%'; }
  function fmtVol(v) {
    v = Number(v) || 0;
    if (v >= 1e8) return (v / 1e8).toFixed(2) + ' 亿股';
    if (v >= 1e4) return (v / 1e4).toFixed(1) + ' 万股';
    return Math.round(v) + ' 股';
  }
  function fmtAmt(v) {
    v = Number(v) || 0;
    if (v >= 1e8) return (v / 1e8).toFixed(2) + ' 亿';
    if (v >= 1e4) return (v / 1e4).toFixed(1) + ' 万';
    return Math.round(v) + '';
  }
  function shortDate(d) { return String(d || '').slice(5); }   // 'MM-DD'
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  /* ---------------- 状态 ---------------- */
  var ST = {
    bars: [], meta: null,
    off: 0, cnt: 120,          // 视图窗口（off 起 cnt 根）
    maArrs: [], volMa: [],     // 全量均线 / 20 日量均线（含预热段）
    maOn: { 5: true, 10: true, 20: true, 60: true },
    bollOn: false,             // 布林线（默认关闭，用户点图例 BOLL 开启）
    boll: null,                // { mid:[], up:[], low:[] }（全量，含预热段）
    hAbs: -1,                  // 悬停的绝对 bar 索引（跨缩放/平移稳定）
    hoverY: null,
    drag: null,                // { x0, off0, pid }
  };
  var BOLL_N = 20, BOLL_K = 2;   // 布林参数：20 日均值 ± 2 倍总体标准差（同花顺/东财通用口径）
  var H = 560;                 // 逻辑高度
  var PAD = { l: 10, r: 66, t: 16, b: 24 };
  var VOLH = 108, GAP = 30;    // 成交量区高度 / 与主图间隔

  function plotBox(W) {
    var priceTop = PAD.t;
    var volBot = H - PAD.b;
    var volTop = volBot - VOLH;
    var priceBot = volTop - GAP;
    return { l: PAD.l, r: W - PAD.r, priceTop: priceTop, priceBot: priceBot, volTop: volTop, volBot: volBot };
  }
  function viewW(cv) {
    var stage = $id('klineStage');
    var w = 0;
    if (stage && stage.clientWidth) w = stage.clientWidth;
    else if (cv && cv.parentNode && cv.parentNode.clientWidth) w = cv.parentNode.clientWidth;
    if (!w) w = 1100;   // 无头环境 / 隐藏时兜底
    return clamp(Math.round(w), 360, 1680);
  }

  /* ---------------- canvas 高清初始化（逻辑尺寸可变） ---------------- */
  function setupCanvas(cv, W, scale) {
    scale = scale || clamp(window.devicePixelRatio || 1, 1, 2);
    if (cv.width !== W * scale || cv.height !== H * scale) {
      cv.width = W * scale; cv.height = H * scale;
    }
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
    cv.dataset.lw = W; cv.dataset.lh = H;
    var ctx = cv.getContext('2d');
    if (ctx.setTransform) ctx.setTransform(scale, 0, 0, scale, 0, 0);
    if (ctx.clearRect) ctx.clearRect(0, 0, W, H);
    return ctx;
  }

  /* ---------------- 均线计算（全量，含预热段） ---------------- */
  function computeMA() {
    var bars = ST.bars, n = bars.length;
    var mk = function (k) {
      var out = new Array(n), sum = 0;
      for (var i = 0; i < n; i++) {
        sum += bars[i].close;
        if (i >= k) sum -= bars[i - k].close;
        out[i] = i >= k - 1 ? sum / k : null;
      }
      return out;
    };
    ST.maArrs = [mk(5), mk(10), mk(20), mk(60)];
    /* ---- 布林线（BOLL 20, 2σ）：中轨=20 日均值，上下轨=中轨 ± 2×总体标准差 ----
       用滚动 sum / sumSq 单遍 O(n) 计算；方差用 max(0,…) 防浮点误差开负数。 */
    var mid = new Array(n), up = new Array(n), low = new Array(n);
    var s1 = 0, s2 = 0;
    for (var j = 0; j < n; j++) {
      var cv = bars[j].close;
      s1 += cv; s2 += cv * cv;
      if (j >= BOLL_N) { var ov = bars[j - BOLL_N].close; s1 -= ov; s2 -= ov * ov; }
      if (j >= BOLL_N - 1) {
        var mu = s1 / BOLL_N;
        var vr = s2 / BOLL_N - mu * mu;
        var sd = Math.sqrt(vr > 0 ? vr : 0);
        mid[j] = mu; up[j] = mu + BOLL_K * sd; low[j] = mu - BOLL_K * sd;
      } else { mid[j] = null; up[j] = null; low[j] = null; }
    }
    ST.boll = { mid: mid, up: up, low: low };
    var vsum = 0, vm = new Array(n);
    for (var i = 0; i < n; i++) {
      vsum += (bars[i].volume || 0);
      if (i >= 20) vsum -= (bars[i - 20].volume || 0);
      vm[i] = i >= 19 ? vsum / 20 : null;
    }
    ST.volMa = vm;
  }

  /* ---------------- 主渲染 ---------------- */
  var _raf = 0, _lastDraw = 0;
  function requestRender(force) {
    if (_raf) return;
    var run = function () {
      _raf = 0;
      var now = Date.now();
      if (!force && now - _lastDraw < 8) { requestRender(true); return; }
      _lastDraw = now;
      render();
    };
    if (typeof requestAnimationFrame === 'function') _raf = requestAnimationFrame(run);
    else run();
  }

  function render() {
    var cv = $id('klineChart');
    if (!cv || !cv.getContext) return;
    var stage = $id('klineStage');
    if (stage && stage.hidden) return;
    var bars = ST.bars;
    if (!bars || !bars.length) return;

    var W = viewW(cv);
    var ctx = setupCanvas(cv, W);
    if (!ctx) return;
    var c = C(), L = plotBox(W);
    var n = bars.length, cnt = clamp(ST.cnt, 1, n), off = clamp(ST.off, 0, n - cnt);
    ST.cnt = cnt; ST.off = off;
    var view = bars.slice(off, off + cnt);

    /* ---- 价格范围（视图内 K 线 + 均线 + 布林线） ---- */
    var mn = Infinity, mx = -Infinity, i, v;
    for (i = 0; i < cnt; i++) { var b = view[i]; if (b.low < mn) mn = b.low; if (b.high > mx) mx = b.high; }
    for (var k = 0; k < 4; k++) {
      if (!ST.maOn[[5, 10, 20, 60][k]]) continue;
      var arr = ST.maArrs[k]; if (!arr) continue;
      for (i = off; i < off + cnt; i++) { v = arr[i]; if (v != null && isFinite(v)) { if (v < mn) mn = v; if (v > mx) mx = v; } }
    }
    if (ST.bollOn && ST.boll) {   // 布林上下轨纳入范围（否则轨道会被裁掉；中轨夹在其中无需单算）
      for (i = off; i < off + cnt; i++) {
        v = ST.boll.up[i]; if (v != null && isFinite(v)) { if (v < mn) mn = v; if (v > mx) mx = v; }
        v = ST.boll.low[i]; if (v != null && isFinite(v)) { if (v < mn) mn = v; if (v > mx) mx = v; }
      }
    }
    if (!isFinite(mn) || !isFinite(mx)) { mn = 0; mx = 1; }
    if (mx - mn < 1e-9) { mx = mn + 1; }
    var padP = (mx - mn) * 0.07; mn -= padP; mx += padP;
    var xStep = (L.r - L.l) / cnt;
    var xAt = function (j) { return L.l + j * xStep + xStep / 2; };
    var yAt = function (pv) { return L.priceBot - (pv - mn) / (mx - mn) * (L.priceBot - L.priceTop); };

    /* ---- 网格 + 右侧价格轴 ---- */
    ctx.lineWidth = 1;
    ctx.strokeStyle = c.grid;
    ctx.font = '11px ' + FONT;
    ctx.textBaseline = 'middle';
    var rows = 4;
    for (i = 0; i <= rows; i++) {
      var y = L.priceTop + (L.priceBot - L.priceTop) * i / rows;
      ctx.beginPath(); ctx.moveTo(L.l, y); ctx.lineTo(L.r, y); ctx.stroke();
      ctx.fillStyle = c.axis; ctx.textAlign = 'left';
      ctx.fillText(fmtNum(mx - (mx - mn) * i / rows), L.r + 6, y);
    }
    ctx.strokeStyle = c.gridSoft;
    ctx.beginPath(); ctx.moveTo(L.l, L.volTop); ctx.lineTo(L.r, L.volTop); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(L.l, L.volBot); ctx.lineTo(L.r, L.volBot); ctx.stroke();

    /* ---- 日期刻度（底部） ---- */
    var ticks = clamp(Math.floor(cnt / 26), 2, 9);
    ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
    for (i = 0; i <= ticks; i++) {
      var ji = Math.min(cnt - 1, Math.round((cnt - 1) * i / ticks));
      var jx = xAt(ji);
      ctx.strokeStyle = c.gridSoft;
      ctx.beginPath(); ctx.moveTo(jx, L.priceTop); ctx.lineTo(jx, L.volBot); ctx.stroke();
      ctx.fillStyle = c.axis;
      ctx.fillText(shortDate(view[ji].date), jx, H - 8);
    }

    /* ---- 蜡烛 ---- */
    var cw = clamp(xStep * 0.68, 1, 26);
    for (i = 0; i < cnt; i++) {
      b = view[i];
      var x = xAt(i), up = b.close >= b.open, col = up ? c.up : c.down;
      ctx.strokeStyle = col; ctx.fillStyle = col;
      ctx.beginPath(); ctx.moveTo(x, yAt(b.high)); ctx.lineTo(x, yAt(b.low)); ctx.stroke();
      var y1 = yAt(Math.max(b.open, b.close)), y2 = yAt(Math.min(b.open, b.close));
      var bh = Math.max(y2 - y1, 1);
      if (cw >= 2.5) ctx.fillRect(x - cw / 2, y1, cw, bh);
      else ctx.fillRect(x - 0.5, y1, 1, bh);
    }

    /* ---- 布林线（BOLL 20,2σ；默认关闭，点图例 BOLL 开启）---- */
    if (ST.bollOn && ST.boll) {
      var bl = ST.boll, uA = bl.up, mA = bl.mid, lA = bl.low;
      /* 带宽填充：逐列矩形（xStep 归一，天然处理数据断点，无需闭合路径） */
      ctx.fillStyle = c.bollFill;
      for (i = 0; i < cnt; i++) {
        var uv = uA[off + i], dv = lA[off + i];
        if (uv == null || dv == null || !isFinite(uv) || !isFinite(dv)) continue;
        var by1 = yAt(uv), by2 = yAt(dv);
        ctx.fillRect(xAt(i) - xStep / 2, by1, Math.max(xStep, 1), Math.max(by2 - by1, 1));
      }
      var drawBollLine = function (arr, color, dash) {
        ctx.save();
        ctx.strokeStyle = color; ctx.lineWidth = 1.2;
        if (dash && ctx.setLineDash) ctx.setLineDash(dash);
        ctx.beginPath();
        var st2 = false;
        for (var q = 0; q < cnt; q++) {
          var bv = arr[off + q];
          if (bv == null || !isFinite(bv)) { st2 = false; continue; }
          var px2 = xAt(q), py2 = yAt(bv);
          if (!st2) { ctx.moveTo(px2, py2); st2 = true; } else ctx.lineTo(px2, py2);
        }
        ctx.stroke();
        ctx.restore();
      };
      drawBollLine(uA, c.boll, null);
      drawBollLine(lA, c.boll, null);
      drawBollLine(mA, c.bollMid, [4, 4]);   // 中轨虚线：数值 = MA20，虚线避免与其重叠混淆
    }

    /* ---- MA 线 ---- */
    var maN = [5, 10, 20, 60];
    for (k = 0; k < 4; k++) {
      if (!ST.maOn[maN[k]]) continue;
      var arr2 = ST.maArrs[k]; if (!arr2) continue;
      ctx.strokeStyle = c.ma[k]; ctx.lineWidth = 1.3;
      ctx.beginPath();
      var started = false;
      for (i = 0; i < cnt; i++) {
        v = arr2[off + i];
        if (v == null || !isFinite(v)) { started = false; continue; }
        var mxp = xAt(i), myp = yAt(v);
        if (!started) { ctx.moveTo(mxp, myp); started = true; } else ctx.lineTo(mxp, myp);
      }
      ctx.stroke();
      ctx.lineWidth = 1;
    }

    /* ---- 最新价虚线 + 右轴标签 ---- */
    var lastB = view[cnt - 1];
    var prevB = cnt > 1 ? view[cnt - 2] : (ST.meta && ST.meta.prev_close != null ? { close: ST.meta.prev_close } : null);
    var baseC = prevB ? prevB.close : lastB.open;
    var lastUp = lastB.close >= baseC;
    var yL = yAt(lastB.close);
    if (yL > L.priceTop && yL < L.priceBot) {
      ctx.save();
      ctx.setLineDash([4, 5]); ctx.strokeStyle = lastUp ? c.up : c.down; ctx.globalAlpha = 0.75;
      ctx.beginPath(); ctx.moveTo(L.l, yL); ctx.lineTo(L.r, yL); ctx.stroke();
      ctx.restore();
      var lab = fmtNum(lastB.close);
      ctx.font = 'bold 11px ' + FONT;
      var lw = ctx.measureText(lab).width + 10;
      ctx.fillStyle = lastUp ? c.up : c.down;
      ctx.fillRect(L.r + 2, yL - 9, Math.max(lw, 34), 18);
      ctx.fillStyle = '#fff'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillText(lab, L.r + 7, yL + 0.5);
      ctx.font = '11px ' + FONT;
    }

    /* ---- 成交量副图 ---- */
    var vmax = 0;
    for (i = 0; i < cnt; i++) { v = view[i].volume || 0; if (v > vmax) vmax = v; }
    for (i = 0; i < cnt; i++) { v = ST.volMa[off + i]; if (v != null && v > vmax) vmax = v; }
    if (vmax <= 0) vmax = 1;
    var vh = L.volBot - L.volTop - 4;
    for (i = 0; i < cnt; i++) {
      b = view[i];
      var vb = (b.volume || 0) / vmax * vh;
      ctx.fillStyle = b.close >= b.open ? c.volUp : c.volDown;
      if (cw >= 2.5) ctx.fillRect(xAt(i) - cw / 2, L.volBot - vb, cw, vb);
      else ctx.fillRect(xAt(i) - 0.5, L.volBot - vb, 1, vb);
    }
    ctx.strokeStyle = c.volMa; ctx.lineWidth = 1;
    ctx.beginPath();
    started = false;
    for (i = 0; i < cnt; i++) {
      v = ST.volMa[off + i];
      if (v == null || !isFinite(v)) { started = false; continue; }
      var vx = xAt(i), vy = L.volBot - v / vmax * vh;
      if (!started) { ctx.moveTo(vx, vy); started = true; } else ctx.lineTo(vx, vy);
    }
    ctx.stroke();
    ctx.fillStyle = c.axis; ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
    ctx.fillText('成交量', L.l + 2, L.volTop + 12);

    /* ---- 十字光标 ---- */
    if (ST.hAbs >= off && ST.hAbs < off + cnt) {
      var hi = ST.hAbs - off, hx = xAt(hi);
      ctx.save();
      ctx.setLineDash([3, 4]); ctx.strokeStyle = c.cross; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(hx, L.priceTop); ctx.lineTo(hx, L.volBot); ctx.stroke();
      if (ST.hoverY != null) {
        var hy = clamp(ST.hoverY, L.priceTop, L.volBot);
        ctx.beginPath(); ctx.moveTo(L.l, hy); ctx.lineTo(L.r, hy); ctx.stroke();
      }
      ctx.restore();
      /* 悬停 bar 的价格高亮小标签 */
      if (ST.hoverY != null && ST.hoverY <= L.priceBot) {
        var hpv = mn + (L.priceBot - clamp(ST.hoverY, L.priceTop, L.priceBot)) / (L.priceBot - L.priceTop) * (mx - mn);
        var hlab = fmtNum(hpv);
        ctx.fillStyle = c.cross;
        ctx.fillRect(L.r + 2, clamp(ST.hoverY, L.priceTop, L.priceBot) - 8, 62, 16);
        ctx.fillStyle = '#fff'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
        ctx.fillText(hlab, L.r + 6, clamp(ST.hoverY, L.priceTop, L.priceBot));
      }
    }
  }

  /* ---------------- 信息条 ---------------- */
  function setText(id, txt) { var el = $id(id); if (el) el.textContent = txt; }
  function fillBoard(d) {
    var bars = d.bars, last = bars[bars.length - 1];
    var prev = d.prev_close != null ? d.prev_close : (bars.length > 1 ? bars[bars.length - 2].close : null);
    var chg = prev != null ? last.close - prev : 0;
    var pct = prev ? chg / prev * 100 : 0;
    var cls = chg > 0 ? 'up' : (chg < 0 ? 'down' : 'flat');
    setText('kbCode', d.code);
    setText('kbName', d.name || '');
    var tag = $id('kbTag');
    if (tag) { tag.hidden = !d.is_index; }
    var pe = $id('kbPrice');
    if (pe) { pe.textContent = fmtNum(last.close); pe.className = 'kb-price ' + cls; }
    var ce = $id('kbChg');
    if (ce) {
      ce.textContent = (chg === 0 && !prev) ? '—' : ((chg >= 0 ? '+' : '') + fmtNum(chg) + '　' + fmtPct(pct));
      ce.className = 'kb-chg ' + cls;
    }
    /* 统计行 */
    var amp = prev ? (last.high - last.low) / prev * 100 : 0;
    var parts = [];
    parts.push('📅 ' + last.date);
    parts.push('开 ' + fmtNum(last.open) + ' · 高 ' + fmtNum(last.high) + ' · 低 ' + fmtNum(last.low) + ' · 收 ' + fmtNum(last.close));
    parts.push('振幅 ' + amp.toFixed(2) + '%');
    parts.push('量 ' + fmtVol(last.volume));
    if (last.amount != null) parts.push('额 ' + fmtAmt(last.amount));
    /* MA 趋势判定（用最后一根 + 全量均线） */
    var idx = bars.length - 1, ma5 = ST.maArrs[0][idx], ma10 = ST.maArrs[1][idx], ma20 = ST.maArrs[2][idx], ma60 = ST.maArrs[3][idx];
    var trend = '', tcls = '';
    if (ma5 != null && ma10 != null && ma20 != null) {
      if (last.close > ma5 && ma5 > ma10 && ma10 > ma20) { trend = '多头排列'; tcls = 'up'; }
      else if (last.close < ma5 && ma5 < ma10 && ma10 < ma20) { trend = '空头排列'; tcls = 'down'; }
      else if (ma20 != null && last.close >= ma20) { trend = 'MA20 上方'; tcls = 'up'; }
      else { trend = 'MA20 下方'; tcls = 'down'; }
    }
    var stats = $id('kbStats');
    if (stats) {
      stats.innerHTML = parts.map(function (p) { return '<span>' + p + '</span>'; }).join('') +
        (trend ? '<span class="kb-trend ' + tcls + '">' + trend + '</span>' : '');
    }
  }

  /* ---------------- 悬浮数据框 ---------------- */
  function updateTip() {
    var tip = $id('klineTip');
    if (!tip) return;
    if (ST.hAbs < 0 || !ST.bars.length) { tip.hidden = true; return; }
    var b = ST.bars[ST.hAbs];
    if (!b) { tip.hidden = true; return; }
    var prev = ST.hAbs > 0 ? ST.bars[ST.hAbs - 1].close : (b.open || b.close);
    var pct = prev ? (b.close - prev) / prev * 100 : 0;
    var cls = pct > 0.001 ? 'up' : (pct < -0.001 ? 'down' : 'flat');
    var maTxt = '';
    if (ST.maArrs.length) {
      var seg = [];
      [5, 10, 20, 60].forEach(function (n2, k) {
        var v = ST.maArrs[k][ST.hAbs];
        if (ST.maOn[n2] && v != null && isFinite(v)) seg.push('MA' + n2 + ' ' + fmtNum(v));
      });
      maTxt = seg.join(' · ');
    }
    var bollTxt = '';
    if (ST.bollOn && ST.boll) {
      var bu = ST.boll.up[ST.hAbs], bm = ST.boll.mid[ST.hAbs], blw = ST.boll.low[ST.hAbs];
      if (bu != null && isFinite(bu) && blw != null && isFinite(blw)) {
        bollTxt = 'BOLL 上' + fmtNum(bu) + ' · 中' + fmtNum(bm) + ' · 下' + fmtNum(blw);
      }
    }
    var o = b.open, h = b.high, lo = b.low, cl = b.close;
    tip.innerHTML =
      '<div class="kt-date">' + b.date + '　<span class="' + cls + '">' + fmtPct(pct) + '</span></div>' +
      '<div class="kt-grid">' +
      '<span>开盘</span><b>' + fmtNum(o) + '</b>' +
      '<span>最高</span><b class="up">' + fmtNum(h) + '</b>' +
      '<span>最低</span><b class="down">' + fmtNum(lo) + '</b>' +
      '<span>收盘</span><b>' + fmtNum(cl) + '</b>' +
      '<span>成交量</span><b>' + fmtVol(b.volume) + '</b>' +
      (b.amount != null ? '<span>成交额</span><b>' + fmtAmt(b.amount) + '</b>' : '') +
      '</div>' +
      (maTxt ? '<div class="kt-ma">' + maTxt + '</div>' : '') +
      (bollTxt ? '<div class="kt-ma kt-boll">' + bollTxt + '</div>' : '');
    tip.hidden = false;
    positionTip();
  }
  function positionTip() {
    var tip = $id('klineTip'), cv = $id('klineChart');
    if (!tip || tip.hidden || !cv) return;
    var r = cv.getBoundingClientRect ? cv.getBoundingClientRect() : null;
    if (!r || !r.width) return;
    var x = (_mxPct == null ? 0.5 : _mxPct) * r.width;
    var y = (_myPx == null ? 0.5 : _myPx);
    var tw = tip.offsetWidth || 210, th = tip.offsetHeight || 130;
    var left = x + 18, top = y + 14;
    if (left + tw > r.width - 8) left = x - tw - 18;
    if (top + th > r.height - 8) top = y - th - 14;
    tip.style.left = Math.max(4, left) + 'px';
    tip.style.top = Math.max(4, top) + 'px';
  }
  var _mxPct = null, _myPx = null;

  /* ---------------- 事件 ---------------- */
  function evPos(cv, e) {
    var r = (cv.getBoundingClientRect && cv.getBoundingClientRect()) || { left: 0, top: 0, width: cv.dataset.lw || 1100, height: H };
    var W = +(cv.dataset.lw || r.width || 1100);
    var sx = r.width ? W / r.width : 1;
    var sy = r.height ? H / r.height : 1;
    return { x: ((e.clientX || 0) - (r.left || 0)) * sx, y: ((e.clientY || 0) - (r.top || 0)) * sy };
  }

  function hoverFromEvent(e) {
    var cv = $id('klineChart');
    if (!cv || !ST.bars.length) return;
    var p = evPos(cv, e);
    var W = +(cv.dataset.lw || 1100);
    var L = plotBox(W);
    var cnt = ST.cnt, xStep = (L.r - L.l) / cnt;
    var j = Math.floor((p.x - L.l) / xStep);
    var inPlot = p.x >= L.l - 2 && p.x <= L.r + 2;
    if (!inPlot || j < 0 || j >= cnt) { ST.hAbs = -1; ST.hoverY = null; updateTip(); requestRender(true); return; }
    ST.hAbs = ST.off + j;
    ST.hoverY = clamp(p.y, L.priceTop, L.volBot);
    _mxPct = (p.x) / W; _myPx = p.y;
    updateTip(); requestRender(true);
  }

  function onWheel(e) {
    if (!ST.bars.length) return;
    e.preventDefault();
    var cv = $id('klineChart');
    var p = evPos(cv, e);
    var W = +(cv.dataset.lw || 1100), L = plotBox(W);
    var rel = clamp((p.x - L.l) / (L.r - L.l), 0, 1);
    var anchorIdx = ST.off + rel * ST.cnt;
    var k = (e.deltaY || 0) < 0 ? 0.85 : 1 / 0.85;
    var ncnt = Math.round(ST.cnt * k);
    ncnt = clamp(ncnt, 20, ST.bars.length);
    if (ncnt === ST.cnt) return;
    var noff = Math.round(anchorIdx - rel * ncnt);
    ST.cnt = ncnt;
    ST.off = clamp(noff, 0, ST.bars.length - ncnt);
    syncDaysSelect();
    requestRender(true);
  }

  function onPointerDown(e) {
    if (!ST.bars.length) return;
    var cv = $id('klineChart');
    ST.drag = { x0: e.clientX || 0, off0: ST.off, pid: e.pointerId };
    if (cv && cv.setPointerCapture && e.pointerId != null) { try { cv.setPointerCapture(e.pointerId); } catch (err) { } }
    if (cv && cv.classList) cv.classList.add('grabbing');
  }
  function onPointerMove(e) {
    if (ST.drag) {
      var cv = $id('klineChart');
      var W = +(cv.dataset.lw || 1100), L = plotBox(W);
      var r = (cv.getBoundingClientRect && cv.getBoundingClientRect()) || { width: W };
      var screenXStep = ((L.r - L.l) / ST.cnt) * ((r.width || W) / W);
      var dOff = Math.round(-((e.clientX || 0) - ST.drag.x0) / (screenXStep || 1));
      var noff = clamp(ST.drag.off0 + dOff, 0, Math.max(0, ST.bars.length - ST.cnt));
      if (noff !== ST.off) { ST.off = noff; requestRender(true); }
      return;
    }
    hoverFromEvent(e);
  }
  function onPointerUp() {
    if (ST.drag) {
      ST.drag = null;
      var cv = $id('klineChart');
      if (cv && cv.classList) cv.classList.remove('grabbing');
    }
  }
  function onPointerLeave() {
    if (ST.drag) ST.drag = null;
    ST.hAbs = -1; ST.hoverY = null;
    updateTip(); requestRender(true);
  }
  function onDblClick() { resetView(); }
  function resetView() {
    if (!ST.bars.length) return;
    var dd = (ST.meta && ST.meta.display_days) || 120;
    ST.cnt = clamp(dd, 20, ST.bars.length);
    ST.off = ST.bars.length - ST.cnt;
    syncDaysSelect();
    requestRender(true);
  }
  /* 缩放/复位后把「K 线天数」下拉同步成当前根数（有匹配项才改） */
  function syncDaysSelect() {
    var sel = $id('klineDays');
    if (!sel) return;
    var v = String(ST.cnt);
    var hit = false;
    if (sel.options) for (var i = 0; i < sel.options.length; i++) { if (sel.options[i].value === v) hit = true; }
    if (hit) sel.value = v;
  }

  /* ---------------- MA / 布林 图例开关 ---------------- */
  function bindMaLegend() {
    var box = $id('kbMa');
    if (!box || !box.querySelectorAll) return;
    box.querySelectorAll('.kb-ma-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var n = Number(btn.dataset.ma);
        if (!n) return;
        ST.maOn[n] = !ST.maOn[n];
        btn.classList.toggle('off', !ST.maOn[n]);
        requestRender(true);
      });
    });
    /* 布林线开关：默认关闭（按钮初始带 off 类），点击切换 */
    box.querySelectorAll('.kb-boll-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        ST.bollOn = !ST.bollOn;
        btn.classList.toggle('off', !ST.bollOn);
        updateTip();
        requestRender(true);
      });
    });
  }

  /* ---------------- 加载入口 ---------------- */
  async function load() {
    var codeEl = $id('klineCode');
    var code = codeEl ? String(codeEl.value || '').trim() : '';
    var meme = $id('klineMeme');
    if (!code) {
      showMeme('klineMeme', 'ask', '请输入代码', '例如 600519 / sh000300（指数加 sh/sz 前缀）');
      return;
    }
    var days = Number(($id('klineDays') || {}).value) || 120;
    showOverlay('loading', '正在加载 K 线…');
    var d = null;
    try { d = await API.kline(code, days); } catch (e) {
      hideOverlay();
      showMeme('klineMeme', 'error', '加载失败', String((e && e.message) || e));
      return;
    }
    hideOverlay();
    if (!d || !d.bars || !d.bars.length) {
      showMeme('klineMeme', 'error', '没有 K 线数据',
        (d && d.error) || '请检查代码；指数请加前缀（如 sh000300）');
      return;
    }
    hideMeme('klineMeme');
    ST.bars = d.bars; ST.meta = d;
    computeMA();
    ST.cnt = clamp(d.display_days || days, 20, d.bars.length);
    ST.off = d.bars.length - ST.cnt;
    ST.hAbs = -1; ST.hoverY = null;
    try { localStorage.setItem('shaigu.kline.last', code); } catch (e) { }
    fillBoard(d);
    var empty = $id('klineEmpty'); if (empty) empty.hidden = true;
    var board = $id('kBoard'); if (board) board.hidden = false;   // 信息条显示
    var stage = $id('klineStage'); if (stage) stage.hidden = false;
    var link = $id('klineLink');
    if (link && typeof thsLink === 'function') link.href = thsLink(d.code);
    updateTip();
    requestRender(true);
  }

  /* ---------------- 导出图片 ---------------- */
  function exportImage() {
    var cv = $id('klineChart');
    if (!cv || !ST.bars.length) { showMeme('klineMeme', 'ask', '还没有 K 线', '先加载一只股票或指数'); return; }
    if (typeof openChartWindow !== 'function') return;
    var hBak = ST.hAbs;
    ST.hAbs = -1; render();   // 导出时不要十字光标
    var title = (ST.meta ? (ST.meta.code + (ST.meta.name ? ' ' + ST.meta.name : '')) : 'K线') + ' K线';
    openChartWindow(cv, title);
    ST.hAbs = hBak;
    if (hBak >= 0) requestRender(true);
  }

  /* ---------------- 初始化 ---------------- */
  function init() {
    var cv = $id('klineChart');
    if (!cv) return;
    /* 恢复上次代码（只填输入框，不自动请求） */
    try {
      var last = localStorage.getItem('shaigu.kline.last');
      var inp = $id('klineCode');
      if (last && inp && !inp.value) inp.value = last;
    } catch (e) { }
    if (cv.addEventListener) {
      cv.addEventListener('pointerdown', onPointerDown);
      cv.addEventListener('pointermove', onPointerMove);
      cv.addEventListener('pointerup', onPointerUp);
      cv.addEventListener('pointerleave', onPointerLeave);
      cv.addEventListener('wheel', onWheel, { passive: false });
      cv.addEventListener('dblclick', onDblClick);
    }
    bindMaLegend();
    var ex = $id('btnKlineImg');
    if (ex) ex.addEventListener('click', exportImage);
    /* 空态示例 chips：点一下直接加载 */
    var chips = $id('klineChips');
    if (chips && chips.querySelectorAll) {
      chips.querySelectorAll('[data-code]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var inp = $id('klineCode');
          if (inp) inp.value = btn.dataset.code || '';
          load();
        });
      });
    }
    window.addEventListener('resize', function () { requestRender(true); });
    /* 注册到全站主题重绘表（app.js 切换主题时调用） */
    if (window.__themeRedraw && window.__themeRedraw.push) {
      window.__themeRedraw.push(function () { clearColors(); if (ST.bars.length) { requestRender(true); } });
    }
    if (ST.bars.length) requestRender(true);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.KlineView = {
    load: load,
    render: function () { requestRender(true); },
    themeChanged: function () { clearColors(); if (ST.bars.length) requestRender(true); },
    _st: ST,
  };
})();
