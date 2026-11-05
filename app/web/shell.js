/* ============================================================================
   shell.js — 前端 v4 体验增强层（纯增量，不改动任何业务逻辑）
   职责：侧栏折叠 / 命令面板 / Toast / 页签记忆与面包屑 / 回到顶部 /
        全局请求进度 / 数据状态同步 / 更多菜单 / 快捷键
   ========================================================================== */
(function () {
  'use strict';
  if (window.__shellReady) return;
  window.__shellReady = true;

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var LS = {
    get: function (k, d) { try { var v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { } }
  };

  /* ------------------------------ Toast ------------------------------ */
  var toastHost = null;
  function toast(msg, kind, ms) {
    if (!toastHost) toastHost = $('#toastHost');
    if (!toastHost) return;
    var el = document.createElement('div');
    el.className = 'toast ' + (kind || 'info');
    var ico = kind === 'ok' ? '✓' : kind === 'err' ? '!' : kind === 'warn' ? '⚠' : 'i';
    el.innerHTML = '<i>' + ico + '</i><span></span>';
    el.querySelector('span').textContent = msg;
    toastHost.appendChild(el);
    requestAnimationFrame(function () { el.classList.add('in'); });
    setTimeout(function () {
      el.classList.remove('in');
      setTimeout(function () { el.remove(); }, 260);
    }, ms || 2600);
  }
  window.shaiguToast = toast;

  /* ------------------------------ 侧栏折叠 ------------------------------ */
  var sb = $('#sidebar');
  function setCollapsed(on) {
    if (!sb) return;
    sb.classList.toggle('collapsed', on);
    document.body.classList.toggle('sb-collapsed', on);
    LS.set('shaigu.sb', on ? '1' : '0');
    var t = $('#sbToggle');
    if (t) t.title = on ? '展开侧栏（Ctrl+B）' : '折叠侧栏（Ctrl+B）';
    setTimeout(function () { window.dispatchEvent(new Event('resize')); }, 280);
  }
  if (sb && LS.get('shaigu.sb', '0') === '1') {
    sb.classList.add('collapsed');
    document.body.classList.add('sb-collapsed');
  }
  var sbToggle = $('#sbToggle');
  if (sbToggle) sbToggle.addEventListener('click', function () {
    setCollapsed(!sb.classList.contains('collapsed'));
  });

  /* ------------------------------ 更多菜单 ------------------------------ */
  var moreBtn = $('#tbMoreBtn'), morePop = $('#tbMorePop');
  function closeMore() { if (morePop) morePop.hidden = true; if (moreBtn) moreBtn.classList.remove('open'); }
  if (moreBtn && morePop) {
    moreBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      morePop.hidden = !morePop.hidden;
      moreBtn.classList.toggle('open', !morePop.hidden);
    });
    document.addEventListener('click', function (e) {
      if (morePop.hidden) return;
      if (!morePop.contains(e.target) && e.target !== moreBtn) closeMore();
    }, true);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMore(); });
    // 点菜单里的任意按钮后自动收起
    morePop.addEventListener('click', function (e) {
      if (e.target.closest('.btn')) setTimeout(closeMore, 60);
    });
  }

  /* ------------------------------ 顶栏上下文 ------------------------------ */
  var TAB_META = {};
  $$('.sb-nav .tab').forEach(function (btn) {
    var g = btn.closest('.sb-group');
    var gt = g ? g.querySelector('.sb-group-t span') : null;
    TAB_META[btn.dataset.tab] = {
      label: btn.dataset.label || btn.textContent.trim(),
      desc: btn.dataset.desc || '',
      group: gt ? gt.textContent.trim() : '',
      icon: btn.querySelector('.tab-ico') ? btn.querySelector('.tab-ico').innerHTML : ''
    };
  });

  var ignoreScrollOnce = false;
  function syncChrome(name) {
    var m = TAB_META[name];
    if (!m) return;
    var cg = $('#crumbGroup'), cn = $('#crumbName');
    if (cg) cg.textContent = m.group;
    if (cn) cn.textContent = m.label;
    document.title = m.label + ' · 筛股 P的初航';
    LS.set('shaigu.tab', name);
    // 侧栏当前项滚入视野
    var btn = $('.sb-nav .tab[data-tab="' + name + '"]');
    if (btn && !sb.classList.contains('collapsed')) {
      var r = btn.getBoundingClientRect();
      if (r.top < 120 || r.bottom > window.innerHeight - 60) {
        btn.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    }
    ignoreScrollOnce = true;
    window.scrollTo({ top: 0, behavior: 'auto' });
  }

  // 兜底：即便 switchTab 包装失败，点侧栏也能同步面包屑
  var navEl = $('#sbNav');
  if (navEl) {
    navEl.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('.tab') : null;
      if (b && b.dataset.tab) setTimeout(function () { syncChrome(b.dataset.tab); }, 0);
    });
  }

  // 包装 switchTab：程序化跳转（如点股票代码进 K 线）也能同步外壳
  function wrapSwitchTab() {
    if (typeof window.switchTab !== 'function' || window.switchTab.__shellWrapped) return;
    var orig = window.switchTab;
    var wrapped = function (name, noPush) {
      var r = orig.apply(this, arguments);
      try { syncChrome(name); } catch (e) { }
      return r;
    };
    wrapped.__shellWrapped = true;
    window.switchTab = wrapped;
  }

  /* ------------------------------ 数据状态 ------------------------------ */
  function syncDataStatus() {
    var info = $('#dbInfo');
    if (!info) return;
    var txt = (info.textContent || '').trim();
    if (!txt || txt.indexOf('读取中') >= 0) return;
    var mStocks = txt.match(/([\d,]+)\s*只股票/);
    var mRows = txt.match(/([\d.]+)\s*万条日线/);
    var mMax = txt.match(/数据至\s*([\d-]+)/);
    var mSync = txt.match(/上次同步\s*([\d\- :]+)/);
    var main = $('#sbsMain'), sub = $('#sbsSub'), dot = $('#sbsDot');
    if (main) main.textContent = mMax ? '数据至 ' + mMax[1] : '本地数据';
    if (sub) {
      var parts = [];
      if (mStocks) parts.push(mStocks[1] + ' 只');
      if (mRows) parts.push(mRows[1] + ' 万条');
      sub.textContent = parts.join(' · ') || '点击查看详情';
    }
    var stEl = $('#sbStatus');
    if (stEl) stEl.title = txt;
    if (dot) {
      var max = mMax ? mMax[1] : '';
      var today = new Date();
      var lag = 0;
      if (max) lag = Math.round((today - new Date(max + 'T00:00:00')) / 86400000);
      dot.className = 'sbs-dot' + (lag > 7 ? ' err' : lag > 4 ? ' warn' : '');
    }
    var tb = $('#tbDataDate');
    if (tb && mMax) tb.innerHTML = '<i class="dot"></i>数据至 ' + mMax[1];
  }
  var dbInfoEl = $('#dbInfo');
  if (dbInfoEl) {
    new MutationObserver(syncDataStatus).observe(dbInfoEl, { childList: true, characterData: true, subtree: true });
    syncDataStatus();
  }
  var sbStatus = $('#sbStatus');
  if (sbStatus) sbStatus.addEventListener('click', function () {
    var t = $('#btnTdxSync');
    var d = $('#dbInfo');
    toast(d ? d.textContent : '暂无数据状态', 'info', 4200);
  });

  /* ------------------------------ 范围状态 ------------------------------ */
  var rangePicker = $('#rangePicker');
  if (rangePicker) {
    rangePicker.addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (!b) return;
      var tb = $('#tbRange');
      if (tb) tb.innerHTML = '<i class="dot accent"></i>范围 ' + b.textContent.trim();
    });
  }

  /* ------------------------------ 全局请求进度 ------------------------------ */
  var inflight = 0, progTimer = null, progEl = $('#pageProgress');
  function progStart() {
    inflight++;
    if (inflight > 0 && !progTimer) {
      progTimer = setTimeout(function () {
        if (progEl) { progEl.classList.add('on'); progEl.style.width = '72%'; }
      }, 260);
    }
  }
  function progEnd() {
    inflight = Math.max(0, inflight - 1);
    if (inflight === 0) {
      clearTimeout(progTimer); progTimer = null;
      if (progEl) {
        progEl.style.width = '100%';
        setTimeout(function () { progEl.classList.remove('on'); progEl.style.width = '0'; }, 260);
      }
    }
  }
  var _fetch = window.fetch;
  if (typeof _fetch === 'function') {
    window.fetch = function () {
      progStart();
      var p = _fetch.apply(window, arguments);
      if (p && p.then) p.then(progEnd, progEnd); else progEnd();
      return p;
    };
  }

  /* ------------------------------ 回到顶部 ------------------------------ */
  var toTop = $('#toTop');
  function onScroll() {
    if (toTop) toTop.hidden = window.scrollY < 460;
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
  if (toTop) toTop.addEventListener('click', function () { window.scrollTo({ top: 0, behavior: 'smooth' }); });

  /* ------------------------------ 命令面板 ------------------------------ */
  var cmdk = $('#cmdk'), cmdkInput = $('#cmdkInput'), cmdkList = $('#cmdkList');
  var cmdkItems = [], cmdkIdx = 0, cmdkOpen = false;

  var ACTIONS = [
    { key: 'sync', label: '同步数据', desc: '按当前范围增量拉取最新行情', py: 'tbsj', icon: '⟳', run: function () { $('#btnSync').click(); } },
    { key: 'meta', label: '刷新信息', desc: '刷新股票列表与板块', py: 'sxxx', icon: '↻', run: function () { $('#btnRefreshMeta').click(); } },
    { key: 'flash', label: '全球风险快照', desc: 'VIX / A50 / 布伦特 / 恒生 + 期现结构', py: 'qqfjkz', icon: '🌐', run: function () { $('#btnIdxFlash').click(); } },
    { key: 'divt', label: '分红时点预测', desc: '本周哪些股票可能除权', py: 'fhsd', icon: '🗓', run: function () { $('#btnDivTiming').click(); } },
    { key: 'theme', label: '切换外观', desc: '暗色 / 羊皮纸 / 樱花 / 静谧 / 燕麦', py: 'qthw', icon: '◐', run: function () { $('#btnTheme').click(); } },
    { key: 'sb', label: '折叠 / 展开侧栏', desc: '给内容更多横向空间', py: 'zdc', icon: '▤', run: function () { setCollapsed(!sb.classList.contains('collapsed')); } },
    { key: 'top', label: '回到顶部', desc: '', py: 'hddb', icon: '↑', run: function () { window.scrollTo({ top: 0, behavior: 'smooth' }); } }
  ];

  function buildItems() {
    var items = [];
    Object.keys(TAB_META).forEach(function (t) {
      var m = TAB_META[t];
      items.push({
        kind: 'tab', key: t, label: m.label, desc: m.desc, group: m.group,
        py: (m.label + ' ' + t), icon: m.icon, html: true,
        run: function () { if (typeof window.switchTab === 'function') window.switchTab(t); else { var b = $('.sb-nav .tab[data-tab="' + t + '"]'); if (b) b.click(); } }
      });
    });
    ACTIONS.forEach(function (a) { items.push(Object.assign({ kind: 'act', group: '动作' }, a)); });
    return items;
  }

  function renderCmdk(q) {
    if (!cmdkList) return;
    var ql = (q || '').trim().toLowerCase();
    var pool = cmdkItems.filter(function (it) {
      if (!ql) return true;
      return (it.label + ' ' + (it.desc || '') + ' ' + (it.py || '') + ' ' + (it.key || '')).toLowerCase().indexOf(ql) >= 0;
    });
    if (!pool.length) {
      cmdkList.innerHTML = '<div class="cmdk-empty">没有匹配的页签或动作</div>';
      cmdkIdx = 0;
      return;
    }
    cmdkIdx = Math.min(cmdkIdx, pool.length - 1);
    var lastGroup = null;
    cmdkList.innerHTML = pool.map(function (it, i) {
      var head = '';
      if (it.group && it.group !== lastGroup) {
        head = '<div class="cmdk-g">' + it.group + '</div>';
        lastGroup = it.group;
      }
      var ico = it.html ? '<span class="ci-ico">' + it.icon + '</span>'
        : '<span class="ci-ico ci-emoji">' + (it.icon || '•') + '</span>';
      return head + '<button class="cmdk-item' + (i === cmdkIdx ? ' on' : '') + '" data-i="' + i + '">'
        + ico + '<span class="ci-txt"><b>' + it.label + '</b>'
        + (it.desc ? '<em>' + it.desc + '</em>' : '') + '</span>'
        + '<kbd>Enter</kbd></button>';
    }).join('');
    cmdkList._pool = pool;
    $$('.cmdk-item', cmdkList).forEach(function (el) {
      el.addEventListener('click', function () {
        var it = pool[+el.dataset.i];
        closeCmdk();
        if (it) setTimeout(it.run, 40);
      });
      el.addEventListener('mousemove', function () {
        var i = +el.dataset.i;
        if (i !== cmdkIdx) {
          cmdkIdx = i;
          $$('.cmdk-item', cmdkList).forEach(function (x) { x.classList.toggle('on', +x.dataset.i === i); });
        }
      });
    });
  }

  function openCmdk() {
    if (!cmdk) return;
    cmdkOpen = true;
    cmdk.hidden = false;
    cmdkIdx = 0;
    if (cmdkInput) { cmdkInput.value = ''; cmdkInput.focus(); }
    renderCmdk('');
  }
  function closeCmdk() {
    if (!cmdk) return;
    cmdkOpen = false;
    cmdk.hidden = true;
    if (cmdkInput) cmdkInput.blur();
  }
  function moveCmdk(d) {
    if (!cmdkList) return;
    var n = $$('.cmdk-item', cmdkList).length;
    if (!n) return;
    cmdkIdx = (cmdkIdx + d + n) % n;
    $$('.cmdk-item', cmdkList).forEach(function (x) { x.classList.toggle('on', +x.dataset.i === cmdkIdx); });
    var cur = $('.cmdk-item.on', cmdkList);
    if (cur) cur.scrollIntoView({ block: 'nearest' });
  }
  if (cmdk) {
    cmdkItems = buildItems();
    var bd = $('.cmdk-bd', cmdk);
    if (bd) bd.addEventListener('click', closeCmdk);
    if (cmdkInput) {
      cmdkInput.addEventListener('input', function () { cmdkIdx = 0; renderCmdk(cmdkInput.value); });
      cmdkInput.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowDown') { e.preventDefault(); moveCmdk(1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); moveCmdk(-1); }
        else if (e.key === 'Enter') {
          e.preventDefault();
          var it = (cmdkList._pool || [])[cmdkIdx];
          closeCmdk();
          if (it) setTimeout(it.run, 40);
        } else if (e.key === 'Escape') { e.preventDefault(); closeCmdk(); }
      });
    }
    var btnCmd = $('#btnCmd');
    if (btnCmd) btnCmd.addEventListener('click', openCmdk);
  }

  /* ------------------------------ 快捷键 ------------------------------ */
  document.addEventListener('keydown', function (e) {
    var mod = e.ctrlKey || e.metaKey;
    if (mod && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      if (cmdkOpen) closeCmdk(); else openCmdk();
      return;
    }
    if (mod && (e.key === 'b' || e.key === 'B')) {
      e.preventDefault();
      setCollapsed(!sb.classList.contains('collapsed'));
      return;
    }
    if (e.key === 'Escape' && cmdkOpen) { e.preventDefault(); closeCmdk(); }
  });

  /* ------------------------------ 启动 ------------------------------ */
  function pillify() {
    // 把「勾选类」inline label 变成胶囊开关：只处理纯 checkbox + 文本的
    $$('.controls label.inline').forEach(function (lb) {
      if (lb.classList.contains('chk-pill')) return;
      var inputs = lb.querySelectorAll('input');
      if (inputs.length !== 1 || inputs[0].type !== 'checkbox') return;
      if (lb.querySelector('select, textarea, input[type="number"], input[type="text"]')) return;
      lb.classList.add('chk-pill');
    });
  }

  function decorateTables() {
    // 结果容器滚动时给表头一点阴影，提示还有内容
    $$('.results').forEach(function (r) {
      if (r.__deco) return;
      r.__deco = true;
      r.addEventListener('scroll', function () {
        r.classList.toggle('scrolled', r.scrollTop > 2);
      }, { passive: true });
    });
  }

  function wrapOverlay() {
    // 把加载遮罩的子元素收进一张卡片，视觉更聚焦（不动任何 id）
    var ov = $('#overlay');
    if (ov && !ov.querySelector('.overlay-card')) {
      var card = document.createElement('div');
      card.className = 'overlay-card';
      while (ov.firstChild) card.appendChild(ov.firstChild);
      ov.appendChild(card);
    }
    var bm = $('#bootstrapModal');
    if (bm && !bm.querySelector('.boot-inner')) {
      var c2 = document.createElement('div');
      c2.className = 'boot-inner';
      while (bm.firstChild) c2.appendChild(bm.firstChild);
      bm.appendChild(c2);
    }
  }

  /* ------------------------------ 条件筛选 · 仪表盘摘要带 ------------------------------ */
  function mountConditionDash() {
    var panel = document.getElementById('tab-condition');
    if (!panel || panel.querySelector('.dash-strip')) return;
    var head = panel.querySelector('.panel-head');
    if (!head) return;

    var el = document.createElement('div');
    el.className = 'dash-strip';
    el.innerHTML =
      '<div class="dash-cell" data-k="range">' +
        '<span class="dash-k">筛选范围</span>' +
        '<span class="dash-v" id="dashRange">全部</span>' +
        '<span class="dash-sub">顶栏可切换</span>' +
      '</div>' +
      '<div class="dash-cell" data-k="cond">' +
        '<span class="dash-k">已选条件</span>' +
        '<span class="dash-v"><b id="dashCond">0</b><em>/ <span id="dashCondAll">0</span></em></span>' +
        '<span class="dash-bar"><i id="dashCondBar"></i></span>' +
      '</div>' +
      '<div class="dash-cell clickable" data-k="res" title="点击跳到结果区">' +
        '<span class="dash-k">最近结果</span>' +
        '<span class="dash-v"><b id="dashRes">—</b><em id="dashResUnit"></em></span>' +
        '<span class="dash-sub" id="dashResSub">尚未运行</span>' +
      '</div>' +
      '<div class="dash-cell" data-k="data">' +
        '<span class="dash-k">数据截至</span>' +
        '<span class="dash-v sm" id="dashDate">—</span>' +
        '<span class="dash-sub" id="dashRows">—</span>' +
      '</div>';
    head.insertAdjacentElement('afterend', el);

    function refresh() {
      // 范围
      var rb = document.querySelector('#rangePicker button.active');
      var rv = rb ? rb.textContent.trim() : '全部';
      var dr = $('#dashRange');
      if (dr) dr.textContent = rv;

      // 条件
      var grid = document.getElementById('condGrid');
      var boxes = grid ? grid.querySelectorAll('input[type="checkbox"]') : [];
      var all = boxes.length, on = 0;
      for (var i = 0; i < all; i++) if (boxes[i].checked) on++;
      var dc = $('#dashCond'), dca = $('#dashCondAll'), dcb = $('#dashCondBar');
      if (dc) dc.textContent = String(on);
      if (dca) dca.textContent = String(all);
      if (dcb) dcb.style.width = (all ? Math.round(on / all * 100) : 0) + '%';

      // 结果
      var tb = document.querySelector('#condTable tbody');
      var n = 0, ran = false;
      if (tb) {
        Array.prototype.forEach.call(tb.children, function (tr) {
          if (tr.classList.contains('table-tip')) return;
          if (tr.querySelector('.res-empty')) return;
          if (tr.querySelector('td[colspan]') && tr.children.length === 1) {
            var t = (tr.textContent || '');
            if (t.indexOf('暂无结果') >= 0) { ran = true; return; }
            return;
          }
          n++;
          ran = true;
        });
      }
      var dres = $('#dashRes'), dunit = $('#dashResUnit'), dsub = $('#dashResSub');
      if (dres) dres.textContent = ran ? String(n) : '—';
      if (dunit) dunit.textContent = ran ? ' 只' : '';
      if (dsub) dsub.textContent = ran ? (n ? '命中股票' : '无交集，可放宽条件') : '尚未运行';
      var cellRes = el.querySelector('[data-k="res"]');
      if (cellRes) cellRes.classList.toggle('has-val', ran);

      // 数据日期 + 行数
      var info = ($('#dbInfo') && $('#dbInfo').textContent) || '';
      var mMax = info.match(/数据至\s*([\d-]+)/);
      var mRows = info.match(/([\d.]+)\s*万条日线/);
      var ddate = $('#dashDate'), drows = $('#dashRows');
      if (ddate) ddate.textContent = mMax ? mMax[1] : '—';
      if (drows) drows.textContent = mRows ? mRows[1] + ' 万条日线' : '';
    }

    var grid = document.getElementById('condGrid');
    if (grid) {
      grid.addEventListener('change', refresh);
      new MutationObserver(refresh).observe(grid, { subtree: true, attributes: true, attributeFilter: ['class'] });
    }
    var tb = document.querySelector('#condTable tbody');
    if (tb) new MutationObserver(refresh).observe(tb, { childList: true });
    var rp = document.getElementById('rangePicker');
    if (rp) rp.addEventListener('click', function () { setTimeout(refresh, 0); });
    var db = document.getElementById('dbInfo');
    if (db) new MutationObserver(refresh).observe(db, { childList: true, characterData: true, subtree: true });
    var cellRes = el.querySelector('[data-k="res"]');
    if (cellRes) cellRes.addEventListener('click', function () {
      var r = document.querySelector('#tab-condition .results');
      if (r) r.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
    el.__refresh = refresh;
    refresh();
  }

  /* ------------------------------ 盯盘 · 仪表盘摘要带 ------------------------------ */
  function mountMonitorDash() {
    var panel = document.getElementById('tab-monitor');
    if (!panel || panel.querySelector('.dash-strip')) return;
    var head = panel.querySelector('.panel-head');
    if (!head) return;

    var el = document.createElement('div');
    el.className = 'dash-strip';
    el.innerHTML =
      '<div class="dash-cell" data-k="codes">' +
        '<span class="dash-k">盯盘股票</span>' +
        '<span class="dash-v"><b id="mtDashN">0</b><em> 只</em></span>' +
        '<span class="dash-sub" id="mtDashCodesSub">代码 / 盯盘组</span>' +
      '</div>' +
      '<div class="dash-cell" data-k="auto">' +
        '<span class="dash-k">自动刷新</span>' +
        '<span class="dash-v sm" id="mtDashAuto">关</span>' +
        '<span class="dash-sub">手动 / 定时拉取</span>' +
      '</div>' +
      '<div class="dash-cell" data-k="phase">' +
        '<span class="dash-k">市场时段</span>' +
        '<span class="dash-v sm" id="mtDashPhase">—</span>' +
        '<span class="dash-sub" id="mtDashPhaseSub">以行情源为准</span>' +
      '</div>' +
      '<div class="dash-cell clickable" data-k="res" title="点击跳到盯盘结果">' +
        '<span class="dash-k">最近结果</span>' +
        '<span class="dash-v"><b id="mtDashRes">—</b><em id="mtDashResUnit"></em></span>' +
        '<span class="dash-sub" id="mtDashResSub">尚未运行</span>' +
      '</div>';
    head.insertAdjacentElement('afterend', el);

    function codesCount() {
      var ta = document.getElementById('mtCodes');
      if (!ta) return 0;
      return (ta.value || '').split(/[\s,，;；|]+/).filter(function (s) { return /^\d{6}$/.test(s.trim()); }).length;
    }

    function refresh() {
      var n = codesCount();
      var dn = $('#mtDashN');
      if (dn) dn.textContent = String(n);
      var sub = $('#mtDashCodesSub');
      if (sub) sub.textContent = n ? '代码已填，可运行' : '填 6 位代码或用盯盘组';

      var sel = document.getElementById('mtAutoRefresh');
      var da = $('#mtDashAuto');
      if (da) {
        var txt = sel ? (sel.options[sel.selectedIndex].text || sel.value) : '关';
        da.textContent = /^(0|off|关闭?)$/i.test(sel ? sel.value : '0') ? '关' : txt;
      }

      // 市场时段：从 .mt-phase 的文案提炼（它已经含「交易/休市」）
      var ph = $('.mt-phase', panel);
      var dp = $('#mtDashPhase'), dps = $('#mtDashPhaseSub');
      var t = ph ? (ph.textContent || '').replace(/\s+/g, ' ').trim() : '';
      if (t) {
        var m = t.match(/(交易中|开盘|午休|休市|收盘|非交易时段)/);
        if (dp) dp.textContent = m ? m[1] : '已就绪';
        if (dps) dps.textContent = t.slice(0, 42);
      } else {
        if (dp) dp.textContent = '—';
      }

      var cards = panel.querySelectorAll('#mtList .mt-card').length;
      var dr = $('#mtDashRes'), du = $('#mtDashResUnit'), ds = $('#mtDashResSub');
      if (dr) dr.textContent = cards ? String(cards) : '—';
      if (du) du.textContent = cards ? ' 张' : '';
      if (ds) ds.textContent = cards ? '张盯盘卡片' : '尚未运行';
      var cellRes = el.querySelector('[data-k="res"]');
      if (cellRes) cellRes.classList.toggle('has-val', !!cards);
    }

    var ta = document.getElementById('mtCodes');
    if (ta) ta.addEventListener('input', refresh);
    var sel = document.getElementById('mtAutoRefresh');
    if (sel) sel.addEventListener('change', refresh);
    var list = document.getElementById('mtList');
    if (list) new MutationObserver(refresh).observe(list, { childList: true });
    var phase = $('.mt-phase', panel);
    if (phase) new MutationObserver(refresh).observe(phase, { childList: true, characterData: true, subtree: true });
    var grp = document.getElementById('mtGroup');
    if (grp) grp.addEventListener('change', function () { setTimeout(refresh, 30); });
    var cellRes = el.querySelector('[data-k="res"]');
    if (cellRes) cellRes.addEventListener('click', function () {
      var l = document.getElementById('mtList');
      if (l) l.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
    refresh();
  }

  function boot() {
    wrapSwitchTab();
    wrapOverlay();
    pillify();
    decorateTables();
    mountConditionDash();
    mountMonitorDash();
    var saved = LS.get('shaigu.tab', '');
    if (saved && TAB_META[saved] && typeof window.switchTab === 'function') {
      window.switchTab(saved, true);
    } else {
      var cur = $('.sb-nav .tab.active');
      if (cur) syncChrome(cur.dataset.tab);
      window.scrollTo(0, 0);
    }
    // 首次访问提示（只弹一次）
    if (!LS.get('shaigu.v4hint')) {
      LS.set('shaigu.v4hint', '1');
      setTimeout(function () { toast('按 Ctrl+K 可快速跳转到任意页签', 'info', 4600); }, 1800);
    }

    // 表格空行提示：按当前面板的主按钮命名动作，引导更具体
    $$('.results').forEach(function (r) {
      if (r.querySelector('.res-empty')) return;
      var tb = r.querySelector('tbody');
      if (!tb || tb.children.length) return;
      var panel = r.closest('.panel');
      var btn = panel && panel.querySelector('.actions .btn-primary');
      var act = btn ? (btn.textContent || '').replace(/^[^\u4e00-\u9fa5A-Za-z0-9]+/, '').trim() : '';
      if (act.length > 12) act = '';
      tb.innerHTML = '<tr><td><div class="res-empty"><i>▶</i><b>还没有结果</b><span>'
        + (act ? '点上方「' + act + '」开始' : '设置参数后点上方按钮开始')
        + '</span></div></td></tr>';
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(boot, 0); });
  } else {
    setTimeout(boot, 0);
  }
})();
