# -*- coding: utf-8 -*-
"""技术分析的「横向上下文」指标：市值 / 风险系数 / 受压制率 / 基本面。

为什么单独一个模块
==================
`ta.py` 已有 2000+ 行，负责「单只股票的纵向评分与回测」；本模块负责
「这只股票与市场的关系」——市值多大、有多危险、跟大盘绑得多紧、基本面如何。
两者计算口径不同、数据源不同（一个走本地日线、一个还要联网拉分时），
拆开后可以独立测试，也不会让 ta.py 继续膨胀。

三个核心指标
------------
1. **受压制率（0~100）**：个股走势与「所属交易所指数」的**形状相似度**。
   ★关键：用户要的是「视觉上像」，不是「涨跌幅相关」。
     所以先降采样、再去均值做相关——**线性缩放不影响结果**，
     一只涨 10% 和一只涨 2% 但波形一样的票，得分相同。
   ★「持续时间」用**按日分段取最低值**来体现：
     5 天里有 1 天完全不像，分数会被拉下来，而不是被平均值掩盖。

2. **风险系数（0~100）**：波动 + 回撤 + 流动性 + **趋势破坏型大跌**。
   ★「趋势向上时突然大跌」必须看**大跌之前**的趋势：
     大跌当天往往已经跌破 MA20，用当天状态判「趋势向上」永远为假。
     实测：格力 2026-10-20（−3.28%，z=−3.92）、快意电梯 2026-10-29
     （−9.97%，但自身 20 日波动率 4.11%，z 只有 −2.42）——
     所以阈值用「z < −2.0 **或** 3 日累计 < −8%」双条件。

3. **基本面分（0~100）**：低估值分位 + 高股息 + 大市值。
   只在**熊市**参与排序（权重 15%），牛市/震荡不启用。

数据边界（实测）
----------------
- `valuation` 960 万行 / 2018 起：能算 PE/PB 历史分位
- `daily.outstanding_share` 覆盖 95%：`close × 流通股本` = 流通市值
- `ant_index` **只有沪深300 且只到 2026-08-28** → 深/沪/京指数必须自己用等权收益构建
- 分时（东财 trends2）支持指数 secid：`1.000001` / `0.399001` / `0.899050`，
  5 日 = 1205 点；北交所小票常只有 1 天数据，需降级
"""
from __future__ import annotations

import math
import threading
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..core import db

# ============================================================================
# 参数
# ============================================================================
SIM_RESAMPLE = 48          # 形状比较前先降到 48 个点（约 5 分钟一根，接近人眼）
SIM_MIN_POINTS = 12        # 少于这个点数不做判定
RISK_W_LOW = 0.95          # 风险分下限 / 上限（用于归一化）
INTRADAY_DAYS = 5

# 交易所 → 指数 secid（东财）
INDEX_SECID = {
    "SH": "1.000001",      # 上证指数
    "SZ": "0.399001",      # 深证成指
    "BJ": "0.899050",      # 北证50
}
INDEX_NAME = {"SH": "上证指数", "SZ": "深证成指", "BJ": "北证50"}


def exchange_of(code: str) -> str:
    """代码 → 交易所（与 monitor._secid 同一套前缀规则，含北交所/ETF/B 股）。"""
    c = str(code or "")
    if c[:2] in ("43", "83", "87", "88", "92"):
        return "BJ"
    if c[:1] in ("6", "9", "5"):
        return "SH"
    return "SZ"


# ============================================================================
# 形状相似（核心算法）
# ============================================================================
def _resample(x: np.ndarray, n: int = SIM_RESAMPLE) -> np.ndarray:
    """等分 n 段取均值：降噪，接近「人眼看到的走势」。"""
    x = np.asarray(x, dtype=float)
    m = len(x)
    if m <= n:
        return x
    # ★向量化：一开始用 Python 循环逐段求均值，5000 只 × 2 次 × 48 段 = 太慢
    idx = np.linspace(0, m, n + 1).astype(int)
    starts = idx[:-1]
    cnt = np.maximum(1, idx[1:] - starts)
    return np.add.reduceat(x, starts) / cnt


def _pair_sim(a: np.ndarray, b: np.ndarray) -> Optional[dict]:
    """两条序列的形状相似度（0~1）。**幅度不影响结果**。

    · 相关系数：对线性缩放不变（一只涨 10%、一只涨 2%，波形一样则同分）
    · 同向率：逐点涨跌符号的匹配度，作为辅助
    · 相关占 3/4：分钟级方向噪声大，方向只做辅助
    """
    a = _resample(a)
    b = _resample(b)
    m = min(len(a), len(b))
    if m < SIM_MIN_POINTS:
        return None
    a, b = a[-m:], b[-m:]
    if np.std(a) < 1e-12 or np.std(b) < 1e-12:
        return None
    c = float(np.corrcoef(a, b)[0, 1])
    d = float((np.sign(np.diff(a)) == np.sign(np.diff(b))).mean())
    return {"sim": 0.75 * (c + 1) / 2 + 0.25 * d, "corr": c, "dir": d}


def shape_stats(a, b, per_day: int = 0) -> Optional[dict]:
    """形状相似度。per_day > 0 时按日分段（分时用），否则整段算（日线/周线）。

    ★「持续时间」的做法：
      · 分时按日切，**取最不像的那天**参与加权（0.6 均 + 0.4 最差）
      · 日线/周线整段算，但另给 min（把序列切 3 段各算一次）
    这样「只有某一段像」的票拿不到高分。
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    n = min(len(a), len(b))
    if n < SIM_MIN_POINTS:
        return None
    a, b = a[-n:], b[-n:]

    if per_day and n >= per_day:
        days = n // per_day
        sims, cs, ds = [], [], []
        for k in range(days):
            r = _pair_sim(a[k * per_day:(k + 1) * per_day],
                          b[k * per_day:(k + 1) * per_day])
            if r is None:
                continue
            sims.append(r["sim"])
            cs.append(r["corr"])
            ds.append(r["dir"])
        if not sims:
            return None
        mean_s, min_s = float(np.mean(sims)), float(np.min(sims))
        return {"sim": 0.6 * mean_s + 0.4 * min_s,
                "sim_mean": mean_s, "sim_min": min_s,
                "corr": float(np.mean(cs)), "corr_min": float(np.min(cs)),
                "dir": float(np.mean(ds)), "days": len(sims)}

    r = _pair_sim(a, b)
    if r is None:
        return None
    # 整段 + 分三段取最差，体现「持续相似」
    seg_min = r["sim"]
    if n >= SIM_MIN_POINTS * 3:
        step = n // 3
        parts = []
        for k in range(3):
            s, e = k * step, (n if k == 2 else (k + 1) * step)
            rr = _pair_sim(a[s:e], b[s:e])
            if rr is not None:
                parts.append(rr["sim"])
        if parts:
            seg_min = min(parts)
    return {"sim": 0.6 * r["sim"] + 0.4 * seg_min,
            "sim_mean": r["sim"], "sim_min": seg_min,
            "corr": r["corr"], "dir": r["dir"], "days": 1}


# ============================================================================
# 交易所等权指数（本地构建）
# ============================================================================
_IDX_CACHE: Dict[str, object] = {}
_IDX_LOCK = threading.Lock()

# ★全局限速：东财 trends2 在短时间内被连环请求会直接 RemoteDisconnected。
#   实测一轮排行（10 只）+ 浏览器验收跑 4 遍之后，接口连续拒绝服务。
#   0.25s 间隔 × 10 只 ≈ 2.5s，换来的是「不被打爆」，值得。
_RATE_LOCK = threading.Lock()
_RATE_LAST = [0.0]
_MIN_INTERVAL = 0.25


def _throttle() -> None:
    with _RATE_LOCK:
        now = time.time()
        wait = _MIN_INTERVAL - (now - _RATE_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _RATE_LAST[0] = time.time()


def _data_sig() -> str:
    try:
        row = db.reader().execute("SELECT MAX(date), COUNT(*) FROM daily").fetchone()
        return f"{row[0]}|{row[1]}"
    except Exception:                          # noqa: BLE001
        return "?"


def exchange_index(exchange: str, days: int = 320) -> Dict[str, float]:
    """按交易所构建等权日收益序列 {date: ret}。

    ★为什么不用 ant_index：它只有沪深300 且只到 2026-08-28（350 天），
      既没有深证成指也没有北证50，无法支持「按上市地区对应指数」。
      用全市场等权收益代理，形状上高度一致（差异只是加权方式）。
    """
    ex = str(exchange or "SZ").upper()
    key = f"{ex}|{days}|{_data_sig()}"
    with _IDX_LOCK:
        c = _IDX_CACHE.get(key)
        if c is not None:
            return c

    if ex == "BJ":
        cond = "substr(code,1,2) IN ('43','83','87','88','92')"
    elif ex == "SH":
        cond = "substr(code,1,1) IN ('6','9','5') AND substr(code,1,2) NOT IN ('43','83','87','88','92')"
    else:
        cond = "substr(code,1,1) IN ('0','3')"

    try:
        conn = db.reader()
        rows = conn.execute(
            "SELECT date, AVG(ret), COUNT(*) FROM ("
            "  SELECT date, close*1.0/(LAG(close) OVER (PARTITION BY code ORDER BY date))-1 AS ret"
            "  FROM daily WHERE " + cond + ") "
            "WHERE ret IS NOT NULL GROUP BY date ORDER BY date DESC LIMIT ?",
            (int(days),)).fetchall()
    except Exception:                          # noqa: BLE001
        return {}
    out = {}
    for d, r, n in rows:
        if n is not None and n >= 200 and r is not None and math.isfinite(float(r)):
            out[str(d)[:10]] = float(np.clip(float(r), -0.11, 0.11))
    with _IDX_LOCK:
        _IDX_CACHE[key] = out
    return out


# ============================================================================
# 受压制率
# ============================================================================
def suppression_local(code: str, exchange: str, dates: List[str], close: np.ndarray,
                      days_daily: int = 21, weeks: int = 12) -> dict:
    """本地部分：日线（21 日）+ 周线（12 周）的形状相似。无需联网。

    返回 {ok, score, daily:{...}, weekly:{...}, index}
    score = 0.6×日线 + 0.4×周线（分时若另算，见 intraday_suppression）
    """
    idx = exchange_index(exchange)
    if not idx or len(dates) < days_daily + 2:
        return {"ok": False, "err": "指数或日线不足", "score": None}

    ex = str(exchange or "SZ").upper()
    # ---- 日线：最近 21 个交易日 ----
    # ★用累计收益代替原始收盘价：接缝（除权跳空）在价格序列里是一根尖刺，
    #   会凭空拉低「形状相似度」；换成收益率重建后，形状只反映真实波动。
    ret = np.concatenate([[0.0], np.diff(close) / np.where(close[:-1] > 0, close[:-1], np.nan)])
    ret = np.where(np.abs(ret) > 0.32, 0.0, ret)      # 接缝归零
    ret = np.nan_to_num(ret, nan=0.0)
    cum = np.cumprod(1.0 + ret)

    d_pairs = [(dates[i], float(cum[i])) for i in range(len(dates) - days_daily, len(dates))
               if dates[i] in idx]
    d_res = None
    if len(d_pairs) >= SIM_MIN_POINTS:
        a = np.array([p[1] for p in d_pairs])
        b = np.cumprod(np.array([1.0 + idx[p[0]] for p in d_pairs]))
        d_res = shape_stats(a, b)

    # ---- 周线：每 5 个交易日取一个点，共 12 周 ----
    w_pairs = []
    for i in range(len(dates) - weeks * 5, len(dates), 5):
        if i < 0:
            continue
        # 用该周最后一天
        j = min(i + 4, len(dates) - 1)
        if dates[j] in idx:
            w_pairs.append((dates[j], float(cum[j])))
    w_res = None
    if len(w_pairs) >= 8:
        a = np.array([p[1] for p in w_pairs])
        b = np.cumprod(np.array([1.0 + idx[p[0]] for p in w_pairs]))
        w_res = shape_stats(a, b)

    if d_res is None and w_res is None:
        return {"ok": False, "err": "形状相似计算失败", "score": None}

    parts = []
    if d_res:
        parts.append((d_res["sim"], 0.6))
    if w_res:
        parts.append((w_res["sim"], 0.4))
    tot_w = sum(w for _, w in parts)
    score = sum(s * w for s, w in parts) / tot_w * 100.0

    return {
        "ok": True,
        "score": round(score, 1),
        "index": INDEX_NAME.get(ex, ex),
        "daily": None if not d_res else {
            "sim": round(d_res["sim"] * 100, 1),
            "corr": round(d_res["corr"], 3),
            "min": round(d_res["sim_min"] * 100, 1),
            "n": days_daily,
        },
        "weekly": None if not w_res else {
            "sim": round(w_res["sim"] * 100, 1),
            "corr": round(w_res["corr"], 3),
            "min": round(w_res["sim_min"] * 100, 1),
            "n": len(w_pairs),
        },
    }


def intraday_series(code: str, days: int = INTRADAY_DAYS):
    """拉个股分时价格序列（东财 trends2）。失败返回 None。"""
    try:
        from . import monitor
        _throttle()
        pts, _nm, _pre = monitor.fetch_trends_raw(monitor.secid_of(code), days=days)
        return [p[1] for p in pts if p[1]]
    except Exception:                          # noqa: BLE001
        return None


def intraday_index_series(exchange: str, days: int = INTRADAY_DAYS):
    """拉指数分时（secid 直传）。"""
    ex = str(exchange or "SZ").upper()
    secid = INDEX_SECID.get(ex)
    if not secid:
        return None
    try:
        from . import monitor
        _throttle()
        pts, _nm, _pre = monitor.fetch_trends_raw(secid, days=days)
        return [p[1] for p in pts if p[1]]
    except Exception:                          # noqa: BLE001
        return None


def intraday_suppression(code: str, exchange: str, index_ser=None) -> dict:
    """分时压制率：最近 5 日、按日分段、取最不像那天。

    返回 {ok, score, corr, days}。指数序列可外部传入以复用（避免每只股票都拉一次）。
    """
    a = intraday_series(code)
    if not a:
        return {"ok": False, "err": "分时无数据", "score": None}
    b = index_ser if index_ser is not None else intraday_index_series(exchange)
    if not b:
        return {"ok": False, "err": "指数分时无数据", "score": None}
    r = shape_stats(a, b, per_day=241)
    if r is None:
        return {"ok": False, "err": "分时点数不足（可能停牌/北交所低流动性）", "score": None}
    return {"ok": True, "score": round(r["sim"] * 100, 1),
            "corr": round(r["corr"], 3), "corr_min": round(r["corr_min"], 3),
            "days": r["days"]}


def merge_suppression(local: dict, intraday: Optional[dict]) -> dict:
    """合成最终压制率：分时 50% + 日线 30% + 周线 20%。

    分时拿不到（停牌/北交所）就只用日线+周线，并在 parts 里说明。
    """
    parts, detail = [], {}
    if intraday and intraday.get("ok"):
        parts.append(("intraday", intraday["score"], 0.5))
        detail["intraday"] = {"sim": intraday["score"], "corr": intraday.get("corr"),
                              "days": intraday.get("days")}
    if local and local.get("ok"):
        if local.get("daily"):
            parts.append(("daily", local["daily"]["sim"], 0.3))
        if local.get("weekly"):
            parts.append(("weekly", local["weekly"]["sim"], 0.2))
        detail["daily"] = local.get("daily")
        detail["weekly"] = local.get("weekly")
        detail["index"] = local.get("index")
    if not parts:
        return {"ok": False, "score": None, "err": "无可用数据", "parts": detail}
    tot = sum(w for _, _, w in parts)
    score = sum(s * w for _, s, w in parts) / tot
    return {"ok": True, "score": round(score, 1),
            "weights": {k: round(v / tot, 2) for k, _, v in parts},
            "parts": detail}


# ============================================================================
# 风险系数
# ============================================================================
def risk_coef(close: np.ndarray, high: np.ndarray, low: np.ndarray,
              amount: np.ndarray, ma20: np.ndarray, regime: str = "range") -> dict:
    """0~100，越高越危险。

    组成：波动 35 + 回撤 25 + 流动性 15 + 趋势破坏型大跌 25（熊市 ×1.6）
    """
    n = len(close)
    if n < 25:
        return {"ok": False, "score": None, "err": "日线不足", "items": []}
    pct = np.concatenate([[np.nan], np.diff(close) / np.where(close[:-1] > 0, close[:-1], np.nan)])
    # ★除权接缝修复：单日 |涨跌| > 32%（北交所 30% 涨跌停上限之上）只可能是复权跳空。
    #   不修的话 20 日波动率会被污染到 7~16%（正常 2~4%），
    #   z = 当日跌幅 / 波动率 被整体压低 → **真实的大跌反而检测不到**（漏报）。
    #   实测：001285 sd20=7.47%、301139 sd20=15.97%，都是接缝喂出来的。
    pct = np.where(np.abs(pct) > 0.32, np.nan, pct)
    sd20 = float(np.nanstd(pct[-21:-1])) * 100 if n >= 22 else 0.0
    # 波动：2% 以下低，5% 以上高
    vol = _seg1(sd20, [(1.0, 0.10), (2.0, 0.30), (3.5, 0.60), (5.0, 0.85), (8.0, 1.0)]) * 35

    hi60 = float(np.max(high[-60:])) if n >= 60 else float(np.max(high))
    dd = (close[-1] / hi60 - 1.0) * 100 if hi60 > 0 else 0.0
    dds = _seg1(dd, [(-45, 1.0), (-30, 0.85), (-20, 0.6), (-10, 0.3), (0, 0.08)]) * 25

    seg = amount[-20:] if n >= 20 else amount
    amo20 = float(np.nanmean(seg)) / 1e8 if np.isfinite(seg).any() else 0.0
    liq = _seg1(amo20, [(0.1, 1.0), (0.3, 0.75), (0.6, 0.5), (1.5, 0.25), (3.0, 0.1)]) * 15

    # ---- 趋势破坏型大跌 ----
    brk = None
    for i in range(max(32, n - 10), n):
        sd = float(np.nanstd(pct[i - 20:i]))
        if not (sd > 0):
            continue
        if not np.isfinite(pct[i]):
            continue                       # 接缝日不参与判定
        z = float(pct[i] / sd)
        r3i = float(close[i] / close[max(0, i - 3)] - 1.0)
        drop = float(1 - close[i] / close[i - 1])
        if abs(r3i) > 0.32:
            continue                       # 3 日窗口里含接缝，整段跳过
        # 「大跌之前是否处于上升趋势」：看大跌前 10 日内**是否曾有**收盘站上 MA20
        # 且当时 MA20 在上行。★不能用「大跌前一天」判：快意 10-29 大跌前一天
        # 已经跌破均线了，但 10-19~10-22 明明还在趋势上方。
        pre_ok = False
        for j in range(max(10, i - 10), i):
            if (np.isfinite(ma20[j]) and close[j] > ma20[j] and j >= 5
                    and np.isfinite(ma20[j - 5]) and ma20[j] > ma20[j - 5]):
                pre_ok = True
                break
        # 三种「大」：相对自身异常 / 3 日累计 / 单日跌停附近
        big = (z < -2.0) or (r3i < -0.08) or (drop >= 0.07)
        if pre_ok and big:
            if brk is None or drop > brk["drop"]:
                brk = {"days_ago": n - 1 - i, "z": round(z, 2),
                       "drop": round(drop * 100, 2), "r3": round(r3i * 100, 2)}
    mult = 1.6 if regime == "bear" else 1.0
    brk_s = (1.0 if brk else 0.0) * 25 * mult
    if brk and mult > 1:
        brk_s = min(brk_s, 40)      # 熊市加成后封顶 40（总分仍会被 100 截断）

    items = []
    if brk:
        items.append(f"趋势向上时突然大跌（{brk['days_ago']} 日前，跌 {brk['drop']}%，"
                     f"z={brk['z']}）")
    if sd20 >= 4:
        items.append(f"波动偏高（20 日 {sd20:.1f}%）")
    if dd <= -25:
        items.append(f"距 60 日高点 {dd:.1f}%")
    if 0 < amo20 < 0.5:
        items.append(f"成交清淡（日均 {amo20:.2f} 亿）")

    total = vol + dds + liq + brk_s
    return {"ok": True, "score": round(min(100.0, total), 1),
            "items": items,
            "break": brk,
            "detail": {"vol": round(vol, 1), "dd": round(dds, 1),
                       "liq": round(liq, 1), "break": round(brk_s, 1),
                       "sd20": round(sd20, 2), "dd_pct": round(dd, 1),
                       "amo_yi": round(amo20, 2)}}


def _seg1(x, pts) -> float:
    xs = np.array([p[0] for p in pts], dtype=float)
    ys = np.array([p[1] for p in pts], dtype=float)
    if x is None or not math.isfinite(float(x)):
        return 0.0
    return float(np.clip(np.interp(float(x), xs, ys), 0.0, 1.0))


# ============================================================================
# 市值 / 基本面
# ============================================================================
def market_cap_yi(close: float, outstanding_share) -> Optional[float]:
    """流通市值（亿元）。outstanding_share 单位是「股」。"""
    try:
        s = float(outstanding_share)
        c = float(close)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(s) and math.isfinite(c)) or s <= 0 or c <= 0:
        return None
    return round(c * s / 1e8, 1)


def fundamental_score(pe_pct: Optional[float], pb_pct: Optional[float],
                      div_yield: Optional[float], mcap_yi: Optional[float] = None) -> dict:
    """基本面分 0~100：估值分位（60）+ 股息率（40）。

    ★★ 市值**不计入**这个分数（`mcap_yi` 只作为展示字段带出去）。
      原因是有标定证据，不是口味问题（`scripts/_ta_fund_calib.py`，300 只 / 6 万时点）：

        熊市里「市值分位 vs 未来 20 日收益」秩相关 = **−0.221**
          小市值（分位 0-20）：胜率 76.8%、均值 +7.76%
          大市值（分位 80-100）：胜率 46.3%、均值 −1.20%

      也就是说「大市值稳健」在 A 股熊市里是**反的**。第一版把市值当加分项（+20 分），
      结果基本面分越高、后续收益越低（综合 ρ = −0.136），等于给排序帮了倒忙。
      市值仍然单独展示在榜上，让用户自己判断，但不混进「基本面」这个语义里。

    ★ 估值分位在熊市也很弱（PE +0.031 / PB −0.056），只在震荡市明显有效
      （PE −0.184 / PB −0.266）。所以熊市权重只给 8%，详见 ta.rank_top。

    缺数据的项记 0 分、**不重分配权重分母**（否则数据缺失的票会被动得高分）。
    """
    vals, ws = 0.0, 0.0
    if pe_pct is not None and math.isfinite(pe_pct):
        vals += _seg1(pe_pct, [(0, 1.0), (20, 0.85), (50, 0.50), (80, 0.20), (100, 0.0)]) * 35
        ws += 35
    if pb_pct is not None and math.isfinite(pb_pct):
        vals += _seg1(pb_pct, [(0, 1.0), (20, 0.85), (50, 0.50), (80, 0.20), (100, 0.0)]) * 25
        ws += 25
    if div_yield is not None and math.isfinite(div_yield):
        vals += _seg1(div_yield, [(0, 0.05), (1, 0.30), (2, 0.55),
                                  (3, 0.75), (5, 0.95), (8, 1.0)]) * 40
        ws += 40
    if ws <= 0:
        return {"ok": False, "score": None, "missing": ["pe", "pb", "div"]}
    return {"ok": True, "score": round(vals / ws * 100.0, 1), "covered": ws,
            "pe_pct": pe_pct, "pb_pct": pb_pct,
            "div_yield": div_yield, "mcap_yi": mcap_yi}


def snapshot_map(codes: List[str]) -> Dict[str, dict]:
    """一次取回 code → {pe_pct, pb_pct, div_yield, mcap_yi}。"""
    out: Dict[str, dict] = {}
    try:
        conn = db.reader()
        q = "SELECT code, pe_pct, pb_pct, div_yield, close FROM snapshot"
        if codes:
            marks = ",".join("?" * len(codes))
            q += " WHERE code IN (%s)" % marks
            rows = conn.execute(q, [str(c) for c in codes]).fetchall()
        else:
            rows = conn.execute(q).fetchall()
        for r in rows:
            out[str(r[0])] = {"pe_pct": r[1], "pb_pct": r[2],
                              "div_yield": r[3], "close": r[4]}
    except Exception:                          # noqa: BLE001
        return {}
    return out


def latest_shares(codes: List[str]) -> Dict[str, float]:
    """批量取每只股票最新一根的流通股本（股）。用于算流通市值。"""
    out: Dict[str, float] = {}
    if not codes:
        return out
    try:
        conn = db.reader()
        marks = ",".join("?" * len(codes))
        rows = conn.execute(
            "SELECT d.code, d.outstanding_share FROM daily d "
            "JOIN (SELECT code, MAX(date) md FROM daily WHERE code IN (%s) GROUP BY code) m "
            "ON d.code=m.code AND d.date=m.md" % marks,
            [str(c) for c in codes]).fetchall()
        for code, sh in rows:
            try:
                v = float(sh)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v) and v > 0:
                out[str(code)] = v
    except Exception:                          # noqa: BLE001
        pass
    return out
