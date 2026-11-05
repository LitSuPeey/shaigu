# -*- coding: utf-8 -*-
"""分红/财报时点预测 —— 让每周分红拉取从「全市场」收敛到「可能分红的那几百只」。

======================================================================
背景
------------------------------------------------------------------
原实现每轮增量都对**全市场 5500+ 只**做一次按窗口批量分红查询（虽已并发优化到
1.5s，但用户希望进一步按「个股习惯 + 市场规律」预判，只读**可能分红**的股票）。

本模块做三件事：
  1. **习惯画像**：从本地 dividend 表（实测 5.2 万条 / 覆盖 1992 至今）统计每只
     个股的「除权月份分布」，得出它的分红习惯（年报季 / 中期 / 季度型 / 不规则）。
  2. **候选打分**：给定「当前周」，对每只股算一个 0~100 的「本周可能分红」分：
        · 历史上该周（±窗口）有过除权的比例  ← 主信号
        · 距上次除权的年数（是否已到习惯周期）
        · 报告期节奏（年报→4~7月、中报→8~10月、季报→11~次年1月）
  3. **四条硬规则过滤**（用户明确要求）：
        a. 最近几年亏损的公司 → 不读（用 valuation.pe_ttm 或 daily 缺失度代理）
        b. 上市来从未分红 → 不读（dividend 表无该 code）
        c. 近数月（按个股习惯）已分红 → 周期内不读
        d. 每年 2 月底全市场拉一次增量（**每年仅一次**，强制放行全部规则）

产出：`candidates(conn, week_start)` → {code: {...}}，交给 sync 只对这些股查分红。

注：**候选表只用于「减少请求」，绝不作为「删除依据」**——分红表永远只增不删；
未入选的股只是本轮不查，其历史事件原样保留，等它进入习惯月自然会被读到。
======================================================================
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

_log = None


def _log_():
    global _log
    if _log is None:
        import logging
        _log = logging.getLogger("divtiming")
    return _log_()


# ---- 调参（全部可用 cfg 覆盖）----
# 参数标定依据（2026-09-10 用本地 5.2 万条分红 + 2025 真实除权做留一年验证）：
#   · 留一年 = 用 ≤2024 历史建画像，预测 2025 各月，与 2025 真实除权逐月比对
#   · min_score 25 → 覆盖 74.7% / 请求量 27.1% 全市场
#     min_score 30 → 覆盖 71.2% / 请求量 24.6%   ← 取此值（覆盖与省量的折中）
#     min_score 35 → 覆盖 65.2% / 请求量 20.2%
#   · 旺季（5~7 月，占全年分红 ~70%）单月覆盖可达 79%，是真正的关键窗口
#   · 淡季覆盖较低是因为「本周期已分过」（规则c）与「多年亏损」（规则a）本就
#     不该重查；这两类恰好是用户明确要求剔除的，属预期内损失而非缺陷。
DEFAULTS: dict = {
    # 习惯窗口：以「年内第几天」为中心，±若干天视为同一习惯窗口
    "win_days": 21,            # ±21 天 ≈ 一个自然月
    "min_hist": 2,             # 至少要有几次历史分红才敢画像（否则归入「冷启动」按全市场查）
    "min_score": 30,           # 候选分阈值（低于此值本周不查）——见上方标定
    "loss_years": 3,           # 规则a：最近 N 年持续亏损则排除
    "recent_months": None,     # 规则c：近 N 月已分红则排除；None=按个股习惯自动
    # 规则c 的「习惯周期」：两次分红最小间隔（月），按画像自动取更小值
    "cycle_months_default": 6,  # 未识别出习惯时的保守周期
}


# ============ 画像缓存（同一进程内复用，避免 12 次重复建画像）============
# 画像基于「已落库的历史」，在一次请求/一次同步周期内不会变；用 (dividend 表
# 行数, max(ex_date), valuation 行数) 作为签名，签名不变则复用。
# 这样 /stats（12 个月）与 /week（9 周）只需建一次画像，耗时 25s → ~2s。
# ⚠ loss 结果还依赖 loss_years，故把它一并纳入签名（否则换 N 年查会读到旧集合）。
_PROF_CACHE: dict = {"sig": None, "profiles": None, "loss": None, "loss_years": None}


def _sig(conn, loss_years=None) -> tuple:
    try:
        nd, mx = conn.execute(
            "SELECT COUNT(*), MAX(ex_date) FROM dividend").fetchone()
        nv = conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0]
        return (nd or 0, mx or "", nv or 0, int(loss_years) if loss_years else 0)
    except Exception:  # noqa: BLE001
        return None


def _cache_path() -> str:
    import os
    from ..core import db as _db
    root = os.path.dirname(os.path.abspath(_db.db_path()))
    return os.path.join(root, "divtiming_cache.json.gz")


def _load_disk(sig):
    """落盘缓存：冷启动建画像实测 12.6s，读盘 ~0.1s。

    缓存键 = (dividend 行数, max(ex_date), valuation 行数, loss_years)，
    任何一次同步改变数据都会自然失效——不会读到过期画像。
    """
    import gzip
    import json
    if sig is None:
        return None, None
    try:
        with gzip.open(_cache_path(), "rt", encoding="utf-8") as f:
            j = json.load(f)
        if tuple(j.get("sig") or ()) != tuple(sig):
            return None, None
        profiles = {}
        for code, p in (j.get("profiles") or {}).items():
            # JSON 的 object key 一定是字符串，months 的月份键要还原成 int
            p["months"] = {int(k): v for k, v in (p.get("months") or {}).items()}
            profiles[code] = p
        return profiles, set(j.get("loss") or [])
    except Exception:      # noqa: BLE001 —— 缓存损坏就当没有
        return None, None


def _save_disk(sig, profiles, loss) -> None:
    import gzip
    import json
    import os
    if sig is None:
        return
    try:
        path = _cache_path()
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"sig": list(sig), "profiles": profiles,
                       "loss": sorted(loss or [])}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:      # noqa: BLE001
        pass


def _cached(conn, sig):
    if sig is not None and _PROF_CACHE["sig"] == sig:
        return _PROF_CACHE["profiles"], _PROF_CACHE["loss"]
    profiles, loss = _load_disk(sig)
    if profiles is not None:
        _store(sig, profiles, loss)
        return profiles, loss
    return None, None


def _store(sig, profiles, loss):
    _PROF_CACHE["sig"] = sig
    _PROF_CACHE["profiles"] = profiles
    _PROF_CACHE["loss"] = loss
    _save_disk(sig, profiles, loss)


# ============ 画像：从本地 dividend 表统计亩只个股的除权习惯 ============
def build_profiles(conn) -> dict:
    """返回 {code: profile}。

    profile = {
      months: {月: 次数},          # 除权月份分布
      doys: [年内第几天...],        # 全部除权日「年内序日」
      n: 总次数, last: 'YYYY-MM-DD', last_doy: int,
      span_years: 覆盖年数, per_year: 年均次数,
      kind: 'annual'|'semi'|'quarterly'|'irregular'|'unknown',
      typical_month: 主月, cycle_months: 习惯最小间隔(月),
    }
    """
    rows = list(conn.execute(
        "SELECT code, ex_date, cash_per_10 FROM dividend ORDER BY code, ex_date"))
    prof: dict = {}
    for code, ex, cash in rows:
        try:
            d = dt.datetime.strptime(str(ex)[:10], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            continue
        p = prof.setdefault(code, {"months": defaultdict(int), "doys": [],
                                   "years": set(), "dates": []})
        p["months"][d.month] += 1
        p["doys"].append(d.timetuple().tm_yday)
        p["years"].add(d.year)
        p["dates"].append(d)
    out: dict = {}
    for code, p in prof.items():
        ds = sorted(p["dates"])
        n = len(ds)
        span = (ds[-1] - ds[0]).days / 365.25 if n >= 2 else 0.0
        per_year = (n / span) if span > 0.5 else float(n)
        months = dict(p["months"])
        top = sorted(months.items(), key=lambda x: -x[1])
        typical = top[0][0] if top else 0
        # 归类
        if per_year >= 3.0:
            kind = "quarterly"
        elif per_year >= 1.6:
            kind = "semi"
        elif per_year >= 0.75:
            kind = "annual"
        else:
            kind = "irregular"
        # 习惯周期（月）：两次相邻除权的间隔中位数（至少 25% 分位，避免被除息噪音拉低）
        gaps = []
        for i in range(1, len(ds)):
            g = (ds[i] - ds[i - 1]).days / 30.44
            if g >= 0.5:
                gaps.append(g)
        gaps.sort()
        cyc = gaps[len(gaps) // 2] if gaps else DEFAULTS["cycle_months_default"]
        out[code] = {
            "months": months, "doys": p["doys"], "n": n,
            "last": ds[-1].strftime("%Y-%m-%d"),
            "last_doy": ds[-1].timetuple().tm_yday,
            "span_years": round(span, 2),
            "per_year": round(per_year, 2),
            "kind": kind, "typical_month": typical,
            "cycle_months": round(min(max(cyc, 1.5), 18.0), 2),
        }
    return out


# ============ 规则 a：亏损剔除（用本地估值代理，无财报表时的现实选择）============
def loss_codes(conn, years: int = 3) -> set:
    """近似「最近 N 年亏损」：以 valuation.pe_ttm 为负作为代理。

    真实财报（净利润）本项目未落表，PE(TTM)<0 ⇔ 近 12 个月滚动净利为负，
    是「亏损」最直接的可得代理。但**单点为负 ≠ 持续亏损**（一次性减值、商誉
    爆雷、周期底部都会让某一时点 PE 为负，而公司当年照样分红）。

    实测标定（2025 年数据）：用「近 N 年出现过 PE<0」判据会命中 1915 只，
    其中 506 只（26.4%）当年真的分了红 → 误杀严重，违背用户「最近几年亏损」
    （= 持续亏损）的本意。

    故改用**持续性判据**：要求近 N 年 PE 为负的交易日占比 ≥60%（接近常年为负），
    且至少横跨 2 个不同年度；这样只剔除真正长期亏损的公司。
    """
    cutoff = (dt.date.today() - dt.timedelta(days=int(years * 366))).strftime("%Y-%m-%d")
    try:
        rows = conn.execute(
            "SELECT code,"
            " SUM(CASE WHEN pe_ttm IS NOT NULL AND pe_ttm < 0 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN pe_ttm IS NOT NULL THEN 1 ELSE 0 END),"
            " COUNT(DISTINCT CASE WHEN pe_ttm IS NOT NULL AND pe_ttm < 0"
            "       THEN substr(date,1,4) END)"
            " FROM valuation WHERE date >= ? GROUP BY code",
            (cutoff,)).fetchall()
    except Exception as e:  # noqa: BLE001
        _log_().warning(f"亏损代理统计失败: {e}")
        return set()
    out = set()
    for code, neg, valid, neg_years in rows:
        if not valid:
            continue
        ratio = neg / valid
        # 持续为负（占比≥60%）且横跨≥2 个年度 → 判为「近几年亏损」
        if ratio >= 0.6 and (neg_years or 0) >= 2:
            out.add(code)
    return out


# ============ 候选打分 ============
def _score(prof: dict, week_start: dt.date, cfg: dict) -> float:
    """本周「可能分红」分 0~100（越高越可能）。"""
    win = int(cfg["win_days"])
    tgt_doy = week_start.timetuple().tm_yday
    n = prof["n"]
    # ① 历史同窗口覆盖率（主信号，0~70）
    hits = 0
    for doy in prof["doys"]:
        delta = abs(doy - tgt_doy)
        delta = min(delta, 366 - delta)     # 跨年环绕
        if delta <= win:
            hits += 1
    cov = hits / max(1, n)
    s = cov * 70.0
    # ② 距上次除权是否已够一个习惯周期（0~20）
    try:
        last = dt.datetime.strptime(prof["last"], "%Y-%m-%d").date()
        gap_m = (week_start - last).days / 30.44
        cyc = prof.get("cycle_months") or cfg["cycle_months_default"]
        if gap_m >= cyc * 0.85:
            s += 20.0
        elif gap_m >= cyc * 0.6:
            s += 10.0
        else:
            s -= 15.0            # 刚分过，本周几乎不可能
    except Exception:  # noqa: BLE001
        s += 5.0
    # ③ 报告期节奏加成（0~10）：年报 4-7月、中报 8-10月、季报 10-12月/1月
    m = week_start.month
    if prof["kind"] == "annual" and m in (5, 6, 7):
        s += 10.0
    elif prof["kind"] == "semi" and m in (5, 6, 7, 9, 10):
        s += 10.0
    elif prof["kind"] == "quarterly":
        s += 8.0 if m in (5, 6, 7, 9, 10, 11, 1) else 3.0
    else:
        s += 5.0 * cov
    return max(0.0, min(100.0, s))


def candidates(conn, week_start: dt.date | None = None,
               cfg: dict | None = None, force_all: bool = False) -> dict:
    """返回 {code: {"score","kind","last","n","typical_month","reason"}}。

    force_all=True（2 月底全市场）→ 返回全部曾分红股票且 score=100。
    """
    c = dict(DEFAULTS)
    c.update(cfg or {})
    week_start = week_start or dt.date.today()
    sig = _sig(conn, c["loss_years"])
    profiles, bad_cached = _cached(conn, sig)
    if profiles is None:
        profiles = build_profiles(conn)
        bad_cached = loss_codes(conn, int(c["loss_years"]))
        _store(sig, profiles, bad_cached)

    # 规则 b：上市来从未分红 → dividend 表里没有该 code（profiles 天然只含分过红的）
    n_all = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    never = n_all - len(profiles)

    notes = {"total_meta": n_all, "never_dividend": never,
             "with_history": len(profiles)}

    if force_all:
        out = {code: {"score": 100.0, "kind": p["kind"], "last": p["last"],
                      "n": p["n"], "typical_month": p["typical_month"],
                      "reason": "2月底全市场增量（每年一次）"}
               for code, p in profiles.items()}
        notes["mode"] = "force_all"
        notes["selected"] = len(out)
        return {"candidates": out, "notes": notes, "profiles": profiles}

    # 规则 a：亏损剔除（走缓存，与画像同签名）
    bad = bad_cached if bad_cached is not None else loss_codes(conn, int(c["loss_years"]))
    # 规则 c：本轮（本习惯周期）已分红过 → 不重复读
    #   实现要点：不是简单用「距上次天数 < 周期×系数」，而是把「上次除权日」
    #   映射到**本轮的预期除权窗口**再比较——同一习惯窗口内刚分过才跳过，
    #   跨过窗口（哪怕只隔 3 个月）就该查（否则会把中期分红/特别分红漏掉）。
    today = week_start
    out: dict = {}
    dropped_recent = 0
    dropped_loss = 0
    for code, p in profiles.items():
        if code in bad:
            dropped_loss += 1
            continue
        n = p["n"]
        if n < int(c["min_hist"]):
            # 历史太少无法画像 → 保守放行（宁可多查，不可漏）
            out[code] = {"score": float(c["min_score"]), "kind": p["kind"],
                         "last": p["last"], "n": n,
                         "typical_month": p["typical_month"],
                         "reason": "历史样本不足，保守纳入"}
            continue
        # 规则 c（周期版，已修正）：
        #   只跳过「本周期已经分过」的股票 —— 判据是**距上次除权不足一个完整周期**，
        #   且该周期按个股习惯（cycle_months）算，并设 45 天绝对下限
        #   （同一事件在源里常有除权/派息两行，避免重复计数）。
        #   ⚠ 千万不要用「上次除权日与本周序日相同就跳过」——那恰好把
        #   「去年此时分红、今年大概率再来一次」的最优候选误杀（实测 2025-06
        #   漏掉 397 只，且全是年付型主力）。
        if p.get("last"):
            last = dt.datetime.strptime(p["last"], "%Y-%m-%d").date()
            gap_days = (today - last).days
            gap_m = gap_days / 30.44
            cyc = c["recent_months"] or p.get("cycle_months") \
                or c["cycle_months_default"]
            # 不足半个周期 → 本周期已分过（半周期取整不短于 1.5 个月）
            if gap_days < 45 or gap_m < max(1.5, float(cyc) * 0.5):
                dropped_recent += 1
                continue
        sc = _score(p, week_start, c)
        if sc >= float(c["min_score"]):
            out[code] = {"score": round(sc, 1), "kind": p["kind"],
                         "last": p["last"], "n": n,
                         "typical_month": p["typical_month"], "reason": ""}

    notes.update({"mode": "weekly", "dropped_loss": dropped_loss,
                  "dropped_recent": dropped_recent,
                  "selected": len(out), "week_start": week_start.strftime("%Y-%m-%d"),
                  "min_score": c["min_score"]})
    return {"candidates": out, "notes": notes, "profiles": profiles}


def should_full_market(conn, today: dt.date | None = None,
                       cfg: dict | None = None) -> bool:
    """规则 d：是否到了「每年 2 月底全市场拉一次」的日子（且本年尚未拉过）。"""
    today = today or dt.date.today()
    # 2/22 ~ 2/29 之间（含闰日）视为「2 月底」窗口
    if not (today.month == 2 and today.day >= 22):
        return False
    try:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key='last_div_full_market'").fetchone()
        if row and str(row[0])[:4] == str(today.year):
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def mark_full_market(conn, today: dt.date | None = None) -> None:
    today = today or dt.date.today()
    conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?)",
                 ("last_div_full_market", today.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


def stats(conn, cfg: dict | None = None) -> dict:
    """画像整体统计（供 UI 展示「预计每周只需查 N 只」）。

    用缓存画像 + 一次 loss 计算，遍历 12 个月只做「打分+过滤」（无需重建画像）。
    """
    c = dict(DEFAULTS)
    c.update(cfg or {})
    sig = _sig(conn, c["loss_years"])
    profiles, bad = _cached(conn, sig)
    if profiles is None:
        profiles = build_profiles(conn)
        bad = loss_codes(conn, int(c["loss_years"]))
        _store(sig, profiles, bad)
    n = len(profiles)
    kinds: dict = defaultdict(int)
    months: dict = defaultdict(int)
    for p in profiles.values():
        kinds[p["kind"]] += 1
        months[p["typical_month"]] += 1
    # 估算各月候选数（一次画像，多次打分）
    weekly = {}
    y = dt.date.today().year
    n_all = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    for m in range(1, 13):
        ws = dt.date(y, m, 15)
        weekly[f"{m:02d}"] = _count(profiles, bad, ws, c, n_all)
    return {"profiles": n, "kinds": dict(kinds),
            "typical_months": {str(k): v for k, v in sorted(months.items())},
            "weekly_estimate": weekly}


def weeks_estimate(conn, week_start: dt.date | None = None,
                   cfg: dict | None = None, span: int = 4) -> dict:
    """给定周前后各 `span` 周的候选数（一次建画像，供 UI 周切换条）。"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    week_start = week_start or dt.date.today()
    sig = _sig(conn, c["loss_years"])
    profiles, bad = _cached(conn, sig)
    if profiles is None:
        profiles = build_profiles(conn)
        bad = loss_codes(conn, int(c["loss_years"]))
        _store(sig, profiles, bad)
    n_all = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    out = {}
    ws0 = week_start - dt.timedelta(days=week_start.weekday())
    for k in range(-span, span + 1):
        st = ws0 + dt.timedelta(days=7 * k)
        out[st.strftime("%Y-%m-%d")] = _count(profiles, bad, st, c, n_all)
    return out


def _count(profiles: dict, bad: set, ws: dt.date, c: dict, n_all: int = 0) -> int:
    """只算「通过四条规则且达阈值」的数量，不产出候选体（比 candidates 轻）。"""
    ms = float(c["min_score"])
    mh = int(c["min_hist"])
    cnt = 0
    for code, p in profiles.items():
        if code in bad:
            continue
        if p["n"] < mh:
            cnt += 1
            continue
        if p.get("last"):
            last = dt.datetime.strptime(p["last"], "%Y-%m-%d").date()
            gap_days = (ws - last).days
            gap_m = gap_days / 30.44
            cyc = c["recent_months"] or p.get("cycle_months") or c["cycle_months_default"]
            if gap_days < 45 or gap_m < max(1.5, float(cyc) * 0.5):
                continue
        if _score(p, ws, c) >= ms:
            cnt += 1
    return cnt

