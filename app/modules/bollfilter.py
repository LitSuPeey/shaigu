# -*- coding: utf-8 -*-
"""BOLL 过滤器 · 大盘状态（市场环境）—— 把 BOLL 信号叠加到已有模块上。

定位（先说清楚，避免误用）
==========================
本模块提供**两个独立的新能力**，都作为「附加过滤层」存在，不改动任何已有模块：

  ① 大盘状态（market state）—— 回答「现在的市场环境适合用 BOLL 买点吗」
  ② BOLL 布尔过滤器      —— 给定一批股票代码，逐只回答「它现在满足哪个 BOLL 条件」

★★ 关于大盘过滤的诚实披露（有实测证据，不是免责声明）★★
----------------------------------------------------
`scripts/_market_verify.py` / `_market_buckets.py` 的事件研究显示：

  ⑭下轨 T+120 胜率  全部 48.2% → 强牛 50.3%（+2.1pp）
  ⑮突破 T+120 胜率  全部 46.1% → 强牛 48.3%（+2.2pp）

**但同期基准本身也是 50.8%（强牛）**，所以过滤后的**超额 alpha 仍是 −0.5pp**：

  > 胜率的提升来自「大盘本身在涨」（beta），**不是**这两个信号在强牛里更会选股。

各状态桶的 alpha 全部为负（−0.5 ~ −6.3pp），说明**任何市场状态下这两个信号都没有
选股超额**。因此本过滤器**不是「增强器」，是「择时器」**：

  它的真实价值 = 把资金暴露在「平均收益更高」的环境里
  （⑮突破 强牛 T+120 均收益 +10.32%，bear 只有 +4.63% —— 翻倍多），
  而不是「在同样环境里挑出更好的票」。

**前端必须如实展示这段结论**，不得写成「开启后胜率更高」这类误导文案。

用法
----
    from app.modules import bollfilter
    st = bollfilter.market_state()                 # 当前市场状态
    r  = bollfilter.filter_codes(codes, ["lower_support"], state_filter="strong")
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from ..core import batchload, db

# ============================================================================
# 参数
# ============================================================================
MA_SHORT, MA_MID, MA_LONG = 20, 60, 120
MIN_CODES = 300           # 单日横截面最少股票数（样本太少的日子不参与）
MIN_BARS = 400            # 个股长历史门槛
CLIP_RET = 0.20           # 单日收益截尾（剔复权接缝）

# 布林参数（与 conditions.py 的 DEFAULTS 保持一致；此处独立复刻以便过滤层自洽）
BOLL_N, BOLL_K = 20, 2.0
TOUCH_TOL, REBOUND_RATIO, MAX_TREND_DAYS = 1.0, 0.30, 3
BREAK_VOL, BREAK_WIDEN, BREAK_DAYS, BREAK_LOOKBACK = 1.20, 0.95, 5, 10

# 市场状态定义（顺序 = 从宽到严）
STATES = ("bear", "bull", "strong")
STATE_LABEL = {
    "bear":   "弱势",
    "bull":   "温和上行",
    "strong": "强牛",
    "unknown": "数据不足",
}
STATE_DESC = {
    "bear":   "净值在 MA60 下方，或 MA20 未走强 —— 多数股票承压",
    "bull":   "净值站上 MA60 且 MA20 转强 —— 温和上行",
    "strong": "净值 > MA60 > MA120 —— 中期强牛（最严）",
    "unknown": "指数代理历史不足 120 日，无法判定",
}

# ============================================================================
# 行情缓存（进程内，和 batchload 一样按需加载一次）
# ============================================================================
_MK_LOCK = threading.RLock()
_MK_CACHE: Dict[str, object] = {}          # {"dates": [...], "nav": np.ndarray, "state": {...}}


def _hist_path() -> str:
    import os
    root = os.path.dirname(os.path.abspath(db.db_path()))
    return os.path.join(root, "hist.db").replace("\\", "/")


def _build_index(force: bool = False) -> Dict[str, object]:
    """构造「全市场等权指数代理」并逐日判定状态。只构造一次，常驻进程。

    为什么不用真实指数：ant_index 只有 350 个交易日（连 MA120 都不够）、
    hist.db 只有个股没指数、外部拉取会引入网络依赖。等权代理无网络依赖且可回溯 30 年。
    """
    with _MK_LOCK:
        if _MK_CACHE and not force:
            return _MK_CACHE

    conn = sqlite3.connect(f"file:{_hist_path()}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA cache_size = -131072")
    conn.execute("PRAGMA query_only = ON")
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM daily_hist GROUP BY code HAVING COUNT(*) >= ?",
        (MIN_BARS,)).fetchall()]
    codes = sorted(codes)

    daily: Dict[str, List[float]] = {}
    CHUNK = 400
    for i in range(0, len(codes), CHUNK):
        part = codes[i:i + CHUNK]
        marks = ",".join("?" * len(part))
        rows = conn.execute(
            f"SELECT code, date, close FROM daily_hist WHERE code IN ({marks}) "
            f"ORDER BY code, date", part).fetchall()
        cur, prev_c = None, None
        for code, d, c in rows:
            if code != cur:
                cur, prev_c = code, None
                continue
            if prev_c is not None and prev_c > 0 and c is not None:
                r = float(c) / float(prev_c) - 1.0
                if -0.25 < r < 0.25:                 # 剔复权接缝
                    daily.setdefault(str(d), []).append(r)
            prev_c = c
    conn.close()

    ret = {d: float(np.mean(v)) for d, v in daily.items() if len(v) >= MIN_CODES}
    ds = sorted(ret.keys())
    nav = np.cumprod(1.0 + np.clip(np.array([ret[d] for d in ds], float),
                                   -CLIP_RET, CLIP_RET))
    state = {}
    for i, d in enumerate(ds):
        state[d] = _classify(nav, i)
    out = {"dates": ds, "nav": nav, "state": state,
           "first": ds[0] if ds else None, "last": ds[-1] if ds else None,
           "n": len(ds)}
    with _MK_LOCK:
        _MK_CACHE.clear()
        _MK_CACHE.update(out)
        return _MK_CACHE


def _classify(nav: np.ndarray, i: int) -> str:
    """只用 <= i 的数据判定（无未来函数）。"""
    if i < MA_LONG:
        return "unknown"
    maS = float(nav[i - MA_SHORT + 1:i + 1].mean())
    maM = float(nav[i - MA_MID + 1:i + 1].mean())
    maL = float(nav[i - MA_LONG + 1:i + 1].mean())
    maS_prev = float(nav[i - MA_SHORT:i].mean())
    cur = float(nav[i])
    if cur > maM > maL:
        return "strong"
    if cur > maM and (cur > maS or maS > maS_prev):
        return "bull"
    return "bear"


# ============================================================================
# 对外：市场状态
# ============================================================================
def market_state(asof: Optional[str] = None) -> dict:
    """当前（或指定日）市场状态。

    返回 {state, label, desc, date, nav, ma20, ma60, ma120, asof_is_latest, coverage, dist}
    """
    idx = _build_index()
    ds, nav = idx["dates"], idx["nav"]
    if not ds:
        return {"state": "unknown", "label": STATE_LABEL["unknown"],
                "desc": "指数代理无数据", "date": None}
    if asof and asof in idx["state"]:
        pos = ds.index(asof)
        d = asof
    else:
        pos = len(ds) - 1
        d = ds[-1]
    st = idx["state"][d]
    maS = float(nav[pos - MA_SHORT + 1:pos + 1].mean()) if pos >= MA_SHORT else None
    maM = float(nav[pos - MA_MID + 1:pos + 1].mean()) if pos >= MA_MID else None
    maL = float(nav[pos - MA_LONG + 1:pos + 1].mean()) if pos >= MA_LONG else None
    return {
        "state": st, "label": STATE_LABEL[st], "desc": STATE_DESC[st],
        "date": d, "nav": float(nav[pos]),
        "ma20": maS, "ma60": maM, "ma120": maL,
        "asof_is_latest": d == ds[-1],
        "coverage": {"first": idx["first"], "last": idx["last"], "days": idx["n"]},
        "dist": state_distribution(idx),
    }


def state_distribution(idx=None) -> dict:
    """历史各状态占比（供前端展示，让用户知道 strong 不是常态）。"""
    idx = idx or _build_index()
    cnt = {k: 0 for k in ("unknown", "bear", "bull", "strong")}
    for s in idx["state"].values():
        cnt[s] = cnt.get(s, 0) + 1
    tot = max(1, sum(cnt.values()))
    return {k: {"n": v, "pct": round(v / tot * 100, 1)} for k, v in cnt.items()}


def market_state_series(start: Optional[str] = None, end: Optional[str] = None) -> list:
    """区间内逐日状态（供前端画状态带）。"""
    idx = _build_index()
    out = []
    for d in idx["dates"]:
        if start and d < start:
            continue
        if end and d > end:
            break
        out.append({"date": d, "state": idx["state"][d]})
    return out


# ============================================================================
# 对外：BOLL 布尔过滤器（给定代码 → 满足哪个 BOLL 条件）
# ============================================================================
def _boll(close: np.ndarray, n: int, k: float):
    """向量化布林带（总体标准差 ddof=0，与 conditions.py 同口径）。"""
    m = len(close)
    mid = np.full(m, np.nan)
    std = np.full(m, np.nan)
    if m < n:
        return mid, mid, mid
    cs = np.concatenate([[0.0], np.cumsum(close)])
    cs2 = np.concatenate([[0.0], np.cumsum(close * close)])
    idx = np.arange(n - 1, m)
    s = cs[idx + 1] - cs[idx + 1 - n]
    s2 = cs2[idx + 1] - cs2[idx + 1 - n]
    mu = s / n
    var = np.maximum(s2 / n - mu * mu, 0.0)
    mid[idx] = mu
    std[idx] = np.sqrt(var)
    return mid, mid + k * std, mid - k * std


def _sma_prev(x: np.ndarray, n: int) -> np.ndarray:
    """不含当日的 n 日均值（用于量比）。"""
    out = np.full(len(x), np.nan)
    cs = np.concatenate([[0.0], np.cumsum(x)])
    for i in range(n, len(x)):
        out[i] = (cs[i] - cs[i - n]) / n
    return out


def _last_true(mask: np.ndarray, within: int) -> bool:
    """最近 within 根里是否有 True（含最后一根）。"""
    if mask.size == 0:
        return False
    s = max(0, mask.size - within)
    return bool(np.any(mask[s:]))


def hit_lower_support(c: np.ndarray, h: np.ndarray, lo: np.ndarray) -> bool:
    """⑭ BOLL 下轨支撑低吸（与 conditions.py 逐点同口径，此处独立实现）。"""
    if len(c) < BOLL_N + 1:
        return False
    mid, up, low = _boll(c, BOLL_N, BOLL_K)
    w = mid - low
    with np.errstate(invalid="ignore"):
        ok = (np.isfinite(low) & (w > 0)
              & (lo <= low * (1 + TOUCH_TOL / 100.0))
              & (c > low)
              & (c - low >= w * REBOUND_RATIO)
              & (c < mid))
    return _last_true(ok, MAX_TREND_DAYS)


def hit_break_mid(c: np.ndarray, v: np.ndarray, h: np.ndarray) -> bool:
    """⑮ BOLL 中轨突破多头（同口径独立实现）。h 为 high（本条件未用，保留签名一致）。"""
    m = len(c)
    if m < BOLL_N + 60:
        return False
    mid, up, low = _boll(c, BOLL_N, BOLL_K)
    ma60 = np.full(m, np.nan)
    cs = np.concatenate([[0.0], np.cumsum(c)])
    for i in range(59, m):
        ma60[i] = (cs[i + 1] - cs[i + 1 - 60]) / 60
    prev_vma = _sma_prev(v, BOLL_N)
    w_now = mid - low
    w_prev = np.full(m, np.nan)
    w_prev[BOLL_N:] = w_now[:-BOLL_N] if BOLL_N < m else np.nan
    with np.errstate(invalid="ignore"):
        cross = np.zeros(m, dtype=bool)
        cross[1:] = (c[:-1] <= mid[:-1]) & (c[1:] > mid[1:])
        vol_ok = (prev_vma > 0) & (v >= prev_vma * BREAK_VOL)
        # ★避免 divide-by-zero 警告：先筛掉 w_prev<=0，再做除法
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(w_prev > 0, w_now / np.where(w_prev > 0, w_prev, 1.0), np.nan)
        wid_ok = np.isfinite(ratio) & (ratio >= BREAK_WIDEN)
        ma_ok = np.isfinite(ma60) & (c > ma60)
        cand = np.nonzero(cross & vol_ok & wid_ok & ma_ok
                          & np.isfinite(low) & np.isfinite(mid))[0]
    hit = np.zeros(m, dtype=bool)
    for i in cand:
        if i < max(BOLL_N + 1, 60):
            continue
        s = max(BOLL_N, i - BREAK_LOOKBACK - 1)
        seg_m, seg_c = mid[s:i - 1], c[s:i - 1]
        msk = np.isfinite(seg_m)
        if np.any(seg_c[msk] > seg_m[msk]):
            continue
        hit[i] = True
    return _last_true(hit, BREAK_DAYS)


FILTERS = {
    "lower_support": {"label": "⑭ 下轨支撑低吸", "fn": hit_lower_support},
    "break_mid":     {"label": "⑮ 中轨突破多头", "fn": hit_break_mid},
}


def filter_codes(codes: Iterable[str],
                 filters: List[str],
                 state_filter: str = "any",
                 min_bars: int = 120,
                 progress_cb=None) -> dict:
    """对给定代码逐个判定 BOLL 过滤条件。

    filters       : 要检查的过滤键（'lower_support' / 'break_mid'），多选取【并集】
    state_filter  : 'any' 不过滤 / 'bull' 温和上行及以上 / 'strong' 仅强牛
                    —— 用**最新交易日**的市场状态判定（对全批统一，不逐股）
    返回 {rows, state, n_in, n_out, n_missing, skipped_state}
    """
    filters = [f for f in (filters or []) if f in FILTERS]
    # ★去重且保序：上游可能传带重复的代码列表（如多个板块取并集），
    #   不去重会让前端表格出现重复行、也会让 n_out 虚高。
    _seen = set()
    codes = [c for c in (str(x) for x in codes) if not (c in _seen or _seen.add(c))]

    mk = market_state()
    cur_state = mk["state"]
    # 状态闸：'bull' 表示 bull 或 strong 都放行
    gate_ok = True
    if state_filter == "strong":
        gate_ok = cur_state == "strong"
    elif state_filter == "bull":
        gate_ok = cur_state in ("bull", "strong")

    if not gate_ok:
        return {"rows": [], "state": mk, "n_in": len(codes), "n_out": 0,
                "n_missing": 0, "skipped_state": True,
                "reason": f"当前市场状态为「{mk['label']}」，"
                          f"未达所选门槛（{STATE_LABEL.get(state_filter, state_filter)}），已跳过全部信号"}

    bars_map = batchload.load_daily_map(window_days=0, codes=codes)
    rows, missing = [], 0
    total = len(codes)
    for i, code in enumerate(codes):
        # ★进度回调必须在最前面：池子里大量股票「数据不足」会走 continue，
        #   若把回调放在循环尾部，这些池子会**永远不报进度**（前端进度条卡住）。
        #   签名必须是 (a, b, msg)：全站 _task_progress 是三元组回调，
        #   只传两个参数会在「池子大到跨过 300 只」时才炸（小池测不出来）。
        if progress_cb and ((i + 1) % 300 == 0 or i + 1 == total):
            progress_cb(i + 1, total, f"BOLL 过滤 {i + 1}/{total}")
        df = bars_map.get(code)
        if df is None or len(df) < min_bars:
            missing += 1
            continue
        c = df["close"].to_numpy(dtype=float)
        v = df["volume"].to_numpy(dtype=float)
        h = df["high"].to_numpy(dtype=float)
        lo = df["low"].to_numpy(dtype=float)
        hits = []
        for fk in filters:
            try:
                if FILTERS[fk]["fn"](c, v, h) if fk == "break_mid" else FILTERS[fk]["fn"](c, h, lo):
                    hits.append(fk)
            except Exception:
                continue
        if hits:
            rows.append({
                "代码": code,
                "命中": "、".join(FILTERS[k]["label"] for k in hits),
                "filter_keys": hits,
                "收盘": float(c[-1]),
                "数据日期": str(df["date"].iloc[-1])[:10],
            })
    rows.sort(key=lambda r: r["代码"])
    return {"rows": rows, "state": mk, "n_in": len(codes),
            "n_out": len(rows), "n_missing": missing, "skipped_state": False}


def clear_cache() -> None:
    with _MK_LOCK:
        _MK_CACHE.clear()
