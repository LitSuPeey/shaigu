# -*- coding: utf-8 -*-
"""15 个技术/估值/分红筛选条件 + 多条件并行交集组合 + 预设组合。

含 2 个 BOLL（布林带）买入条件：
  ⑭ BOLL 下轨支撑低吸 —— 均值回归型，价格触及/跌破下轨后企稳
  ⑮ BOLL 中轨突破多头 —— 趋势启动型，价格放量上穿中轨且带宽开口

预设组合（PRESETS）：A 趋势启动 / B 超跌反弹，一键套用「条件 + 阈值」。
"""
import concurrent.futures
import threading
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from ..core import db

# ============================================================================
# 条件阈值默认值
# ============================================================================
DEFAULTS = {
    "CHANNEL_DAYS": 60,
    "CHANNEL_R2": 0.6,
    "PULLBACK_DAYS": 20,
    "SUPPORT_MA": 20,
    "PULLBACK_TOL": 2.0,
    "SMALL_YANG_DAYS": 5,
    "SMALL_YANG_MAX": 3.0,
    "W_BOTTOM_DAYS": 60,
    "W_BOTTOM_TOL": 3.0,
    "DIVERGENCE_DAYS": 60,
    "RSI_DAYS": 14,
    "VAL_YEARS": 10,
    "PE_PERCENTILE": 10.0,
    "PB_PERCENTILE": 10.0,
    "MIN_VAL_ROWS": 120,
    "DIV_YIELD_MIN": 3.0,
    "BAR_WINDOW": 300,
    # ---- 股票池通用选项（非条件阈值，但一起下发便于前端「恢复默认」统一管理） ----
    "EXCLUDE_ST": True,            # 剔除 ST/退市
    "NEW_STOCK_DAYS": 365,         # 次新股排除天数
    # ---- BOLL（布林带）参数 ----
    # ⑭ BOLL 下轨支撑低吸
    "BOLL_N": 20,                  # 布林中轨周期（MA20）
    "BOLL_K": 2.0,                 # 上下轨标准差倍数
    "BOLL_TOUCH_TOL": 1.0,         # 「触及下轨」容差(%)：最低价 ≤ 下轨×(1+tol/100)
    "BOLL_REBOUND_RATIO": 0.30,    # 收回下轨上方幅度 ≥ 带宽×该比例（确认企稳，排除贴着下轨阴跌）
    "BOLL_MAX_TREND_DAYS": 3,      # 信号须出现在最近 N 日内（贴近当下，避免追陈旧信号）
    # ⑮ BOLL 中轨突破多头
    "BOLL_BREAK_VOL": 1.20,        # 突破当日量比下限（成交量 / 前 N 日均量）
    "BOLL_BREAK_WIDEN": 0.95,      # 带宽开口下限：突破日带宽 / N 日前带宽 ≥ 该值
                                   #   （0.95 = 允许轻微收口，只排除明显缩口假突破；
                                   #    底部横盘收口是常态，卡到 1.0 会误杀真突破）
    "BOLL_BREAK_DAYS": 5,          # 「刚突破」观察天数：须在最近 N 日内首次上穿中轨
    "BOLL_BREAK_MA60": 1.0,        # 1=要求收盘同时站上 60 日均线（趋势过滤），0=不要求
    "BOLL_BREAK_LOOKBACK": 10,     # 判定「近期是否曾站上中轨」的回看天数
}


# ============================================================================
# 指标
# ============================================================================
def _pct_rank(hist, current):
    s = pd.Series(hist, dtype="float64").dropna()
    if s.empty or pd.isna(current):
        return None
    return float((s <= current).mean() * 100.0)


def _calc_macd(close):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif, dea, dif - dea


def _rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    ag = gain.rolling(n).mean()
    al = loss.rolling(n).mean()
    rsi = 100.0 - 100.0 / (1.0 + ag / al.replace(0, np.nan))
    rsi[(al == 0) & (ag > 0)] = 100.0
    rsi[(al == 0) & (ag == 0)] = 50.0
    return rsi


def _linreg_slope_r2(y):
    y = np.asarray(y, dtype=float)
    if len(y) < 2 or np.isnan(y).any():
        return None, None
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    y_pred = slope * x + intercept
    ss_res = float(((y - y_pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return float(slope), r2


# ============================================================================
# 15 条件（ctx = {"bars","val","div","meta","snap"}）
# ============================================================================
def cond_rising_channel(ctx, cfg):
    """① 上升通道：近 N 日收盘价线性回归斜率>0 且 R²≥阈值。"""
    bars = ctx["bars"]
    n = int(cfg["CHANNEL_DAYS"])
    if bars is None or bars.empty or len(bars) < n:
        return False
    close = bars["close"].astype(float).iloc[-n:]
    slope, r2 = _linreg_slope_r2(close)
    return slope is not None and slope > 0 and r2 >= float(cfg["CHANNEL_R2"])


def cond_pullback_support(ctx, cfg):
    """② 回踩支撑确认。"""
    bars = ctx["bars"]
    pn, ma_n = int(cfg["PULLBACK_DAYS"]), int(cfg["SUPPORT_MA"])
    if bars is None or bars.empty or len(bars) < pn + ma_n:
        return False
    close = bars["close"].astype(float)
    low = bars["low"].astype(float)
    ma = close.rolling(ma_n).mean()
    ma_w = ma.iloc[-pn:]
    low_w = low.iloc[-pn:]
    valid = (ma_w > 0) & ((low_w - ma_w).abs() / ma_w * 100.0
            <= float(cfg["PULLBACK_TOL"]))
    dipped = bool(valid.any())
    last_ok = (pd.notna(ma.iloc[-1]) and pd.notna(close.iloc[-1])
               and float(close.iloc[-1]) > float(ma.iloc[-1]))
    return dipped and last_ok


def cond_small_yang(ctx, cfg):
    """③ 碎步小阳。"""
    bars = ctx["bars"]
    n = int(cfg["SMALL_YANG_DAYS"])
    if bars is None or bars.empty or len(bars) < n + 1:
        return False
    seg = bars.iloc[-(n + 1):]
    close = seg["close"].astype(float)
    open_ = seg["open"].astype(float)
    yang = (close > open_).iloc[1:]
    pct = (close.pct_change() * 100.0).iloc[1:]
    return bool(yang.all()) and bool((pct <= float(cfg["SMALL_YANG_MAX"])).all())


def cond_w_bottom(ctx, cfg):
    """④ W底。"""
    bars = ctx["bars"]
    n = int(cfg["W_BOTTOM_DAYS"])
    if bars is None or bars.empty or len(bars) < max(n, 12):
        return False
    seg = bars.iloc[-n:]
    low = seg["low"].astype(float).to_numpy()
    high = seg["high"].astype(float).to_numpy()
    close_last = float(bars["close"].iloc[-1])
    tol = float(cfg["W_BOTTOM_TOL"]) / 100.0
    order = low.argsort()
    i1 = int(order[0])
    for k in range(1, len(order)):
        i2 = int(order[k])
        if abs(i1 - i2) < 10:
            continue
        base = float(low[i1])
        if base <= 0 or abs(float(low[i2]) - base) / base > tol:
            continue
        lo, hi = sorted((i1, i2))
        if hi - lo < 2:
            continue
        rebound = float(high[lo + 1:hi].max())
        if close_last > rebound:
            return True
    return False


def cond_three_lines_bloom(ctx, cfg):
    """⑤ 三线开花：5>10>20 均线多头排列，三线斜率均>0。"""
    bars = ctx["bars"]
    if bars is None or bars.empty or len(bars) < 22:
        return False
    close = bars["close"].astype(float)
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    if any(pd.isna(x) for x in (ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1],
                                ma5.iloc[-2], ma10.iloc[-2], ma20.iloc[-2])):
        return False
    aligned = ma5.iloc[-1] > ma10.iloc[-1] > ma20.iloc[-1]
    slopes = (ma5.iloc[-1] - ma5.iloc[-2] > 0 and
              ma10.iloc[-1] - ma10.iloc[-2] > 0 and
              ma20.iloc[-1] - ma20.iloc[-2] > 0)
    return bool(aligned and slopes)


def cond_divergence(ctx, cfg):
    """⑥ 日线底背离。"""
    bars = ctx["bars"]
    n, rn = int(cfg["DIVERGENCE_DAYS"]), int(cfg["RSI_DAYS"])
    if bars is None or bars.empty or len(bars) < n + 60:
        return False
    close = bars["close"].astype(float)
    low = bars["low"].astype(float)
    dif, _, _ = _calc_macd(close)
    rsi = _rsi(close, rn)
    seg = slice(-n, None)
    arr = low.iloc[seg].to_numpy()
    if len(arr) < 3:
        return False
    valleys = [i for i in range(1, len(arr) - 1)
               if arr[i] <= arr[i - 1] and arr[i] < arr[i + 1]]
    if arr[-1] < arr[-2]:
        valleys.append(len(arr) - 1)
    valleys = sorted(set(valleys))
    if len(valleys) < 2:
        return False
    j, i = valleys[-2], valleys[-1]
    if not (arr[i] < arr[j]):
        return False
    di, dj = dif.iloc[seg].iloc[i], dif.iloc[seg].iloc[j]
    ri, rj = rsi.iloc[seg].iloc[i], rsi.iloc[seg].iloc[j]
    if any(pd.isna(x) for x in (di, dj, ri, rj)):
        return False
    return bool(di > dj or ri > rj)


def cond_low_golden_cross(ctx, cfg):
    """⑦ 日线低位金叉。"""
    bars = ctx["bars"]
    if bars is None or bars.empty or len(bars) < 40:
        return False
    close = bars["close"].astype(float)
    dif, dea, _ = _calc_macd(close)
    d1, d0 = dif.iloc[-2], dif.iloc[-1]
    e1, e0 = dea.iloc[-2], dea.iloc[-1]
    if any(pd.isna(x) for x in (d1, d0, e1, e0)):
        return False
    return bool(d1 < e1 and d0 >= e0 and d0 < 0 and e0 < 0)


def _boll(close, n, k):
    """布林带：中轨 = MA(n)，上/下轨 = 中轨 ± k×总体标准差(n)。"""
    mid = close.rolling(n).mean()
    std = close.rolling(n).std(ddof=0)
    return mid, mid + k * std, mid - k * std


def cond_boll_lower_support(ctx, cfg):
    """⑭ BOLL 下轨支撑低吸（均值回归型买入）。

    判据（近 BOLL_MAX_TREND_DAYS 日内出现信号即命中）：
      ① 最低价触及/跌破下轨（容差 BOLL_TOUCH_TOL%）；
      ② 当日收盘重新收回下轨上方，且收回幅度 ≥ 带宽×BOLL_REBOUND_RATIO（确认企稳而非贴着下轨阴跌）；
      ③ 当日收盘仍在**中轨之下**（保证是低位低吸，而非已经从下轨反弹到中轨上方）。
    信号日为「最近一次满足上述三条的交易日」，须落在最近 N 日窗口内。
    """
    bars = ctx["bars"]
    n = int(cfg["BOLL_N"])
    k = float(cfg["BOLL_K"])
    win = int(cfg["BOLL_MAX_TREND_DAYS"])
    if bars is None or bars.empty or len(bars) < n + 2:
        return False
    close = bars["close"].astype(float)
    low = bars["low"].astype(float)
    mid, up, lo = _boll(close, n, k)
    tol = float(cfg["BOLL_TOUCH_TOL"]) / 100.0
    rb = float(cfg["BOLL_REBOUND_RATIO"])
    start = max(n, len(bars) - win)          # 只看最近 win 日
    for i in range(start, len(bars)):
        m, l, c, lo_w = mid.iloc[i], lo.iloc[i], close.iloc[i], low.iloc[i]
        if pd.isna(m) or pd.isna(l):
            continue
        width = float(m) - float(l)
        if width <= 0:
            continue
        if float(lo_w) > float(l) * (1.0 + tol):      # ① 未触及下轨
            continue
        if float(c) <= float(l):                       # ② 收盘仍在下轨之下
            continue
        if float(c) - float(l) < width * rb:           # ② 收回幅度不足
            continue
        if float(c) >= float(m):                       # ③ 已回到中轨上方 → 不是低吸位
            continue
        return True
    return False


def cond_boll_break_mid(ctx, cfg):
    """⑮ BOLL 中轨突破多头（趋势启动型买入）。

    判据：
      ① 最近 BOLL_BREAK_DAYS 日内出现「收盘上穿中轨」的突破日（前一日≤中轨，当日>中轨）；
      ② 突破日成交量 ≥ 前 n 日均量 × BOLL_BREAK_VOL（放量确认）；
      ③ 突破日带宽 / n 日前带宽 ≥ BOLL_BREAK_WIDEN（开口放大，排除缩口假突破）；
      ④ 突破日收盘必须高于前一日中轨（突破有效），且此前 BOLL_BREAK_LOOKBACK 日内未站上中轨（首次突破）；
      ⑤ 若 BOLL_BREAK_MA60=1，突破日收盘同时站上 60 日均线。
    """
    bars = ctx["bars"]
    n = int(cfg["BOLL_N"])
    k = float(cfg["BOLL_K"])
    days = int(cfg["BOLL_BREAK_DAYS"])
    if bars is None or bars.empty or len(bars) < n + 60:
        return False
    close = bars["close"].astype(float)
    vol = bars["volume"].astype(float)
    mid, up, lo = _boll(close, n, k)
    ma60 = close.rolling(60).mean()
    vol_min = float(cfg["BOLL_BREAK_VOL"])
    widen_min = float(cfg["BOLL_BREAK_WIDEN"])
    lookback = int(cfg["BOLL_BREAK_LOOKBACK"])
    need_ma60 = float(cfg.get("BOLL_BREAK_MA60", 1.0)) >= 0.5
    start = max(n + 1, len(bars) - days)
    for i in range(start, len(bars)):
        m0, m1 = mid.iloc[i - 1], mid.iloc[i]
        c0, c1 = close.iloc[i - 1], close.iloc[i]
        if any(pd.isna(x) for x in (m0, m1, c0, c1, lo.iloc[i])):
            continue
        cross = float(c0) <= float(m0) and float(c1) > float(m1)
        if not cross:                                  # ① 非上穿日
            continue
        # ② 放量
        j0 = max(0, i - n)
        vol_ma = float(vol.iloc[j0:i].mean()) if i > j0 else np.nan
        if not np.isfinite(vol_ma) or vol_ma <= 0:
            continue
        if float(vol.iloc[i]) < vol_ma * vol_min:
            continue
        # ③ 带宽开口
        w1 = float(m1) - float(lo.iloc[i])
        wi = i - n
        w0 = (float(mid.iloc[wi]) - float(lo.iloc[wi])) if wi >= 0 else np.nan
        if not np.isfinite(w0) or w0 <= 0:
            continue
        if w1 / w0 < widen_min:
            continue
        # ④ 首次突破：突破日之前 lookback 日内从未收盘站上中轨
        s = max(n, i - lookback - 1)
        prev_above = False
        for j in range(s, i - 1):
            if pd.notna(mid.iloc[j]) and float(close.iloc[j]) > float(mid.iloc[j]):
                prev_above = True
                break
        if prev_above:
            continue
        # ⑤ 60 日线过滤
        if need_ma60 and (pd.isna(ma60.iloc[i]) or float(c1) <= float(ma60.iloc[i])):
            continue
        return True
    return False


def cond_pe_low(ctx, cfg):
    """⑧ PE低估（走快照表，秒级）。"""
    snap = ctx.get("snap")
    if not snap:
        return False
    pe = snap.get("pe_ttm")
    pe_pct = snap.get("pe_pct")
    if pe is None or pe_pct is None:
        return False
    if pd.isna(pe) or pd.isna(pe_pct) or pe <= 0:
        return False
    return float(pe_pct) <= float(cfg["PE_PERCENTILE"])


def cond_pb_low(ctx, cfg):
    """⑨ PB低估（走快照表，秒级）。"""
    snap = ctx.get("snap")
    if not snap:
        return False
    pb = snap.get("pb")
    pb_pct = snap.get("pb_pct")
    if pb is None or pb_pct is None:
        return False
    if pd.isna(pb) or pd.isna(pb_pct) or pb <= 0:
        return False
    return float(pb_pct) <= float(cfg["PB_PERCENTILE"])


def cond_high_dividend(ctx, cfg):
    """⑩ 高股息率（走快照表，秒级）。"""
    snap = ctx.get("snap")
    if not snap:
        return False
    div = snap.get("div_yield")
    if div is None or pd.isna(div):
        return False
    return float(div) >= float(cfg["DIV_YIELD_MIN"])


def cond_exchange_sz(ctx, cfg):
    return ctx.get("meta", {}).get("exchange") == "SZ"


def cond_exchange_sh(ctx, cfg):
    return ctx.get("meta", {}).get("exchange") == "SH"


def cond_exchange_bj(ctx, cfg):
    return ctx.get("meta", {}).get("exchange") == "BJ"


CONDITIONS = {
    "rising_channel":   {"label": "① 上升通道",
                         "desc": "近60日收盘价线性回归斜率>0且R²≥0.6",
                         "fn": cond_rising_channel},
    "pullback_support": {"label": "② 回踩支撑确认",
                         "desc": "近20日内下探20日均线(2%容差)后收盘重新站上",
                         "fn": cond_pullback_support},
    "small_yang":       {"label": "③ 碎步小阳",
                         "desc": "近5日连续阳线且每日涨幅≤3%",
                         "fn": cond_small_yang},
    "w_bottom":         {"label": "④ 小步上扬W底",
                         "desc": "近60日双低点差≤3%、间隔≥10日且突破颈线",
                         "fn": cond_w_bottom},
    "three_lines_bloom": {"label": "⑤ 三线开花",
                          "desc": "5日>10日>20日均线多头排列且三线斜率均>0",
                          "fn": cond_three_lines_bloom},
    "divergence":       {"label": "⑥ 日线底背离",
                         "desc": "近60日股价新低但DIF/RSI未新低",
                         "fn": cond_divergence},
    "low_golden_cross": {"label": "⑦ 日线低位金叉",
                         "desc": "MACD在零轴下方金叉（DIF<0且DEA<0）",
                         "fn": cond_low_golden_cross},
    "pe_low":           {"label": "⑧ PE低估",
                         "desc": "PE-TTM近10年分位≤10%且PE>0",
                         "fn": cond_pe_low},
    "pb_low":           {"label": "⑨ PB低估",
                         "desc": "PB-MRQ近10年分位≤10%且PB>0",
                         "fn": cond_pb_low},
    "high_dividend":    {"label": "⑩ 高股息率",
                         "desc": "最近一年股息率≥3%",
                         "fn": cond_high_dividend},
    "exchange_sz":      {"label": "⑪ 深证",
                         "desc": "深交所股票（代码以0/3开头）",
                         "fn": cond_exchange_sz},
    "exchange_sh":      {"label": "⑫ 沪证",
                         "desc": "上交所股票（代码以6开头）",
                         "fn": cond_exchange_sh},
    "exchange_bj":      {"label": "⑬ 北证",
                         "desc": "北交所股票（代码以8/4/92开头）",
                         "fn": cond_exchange_bj},
    "boll_lower_support": {"label": "⑭ BOLL下轨支撑低吸",
                           "desc": "近3日触及/跌破布林下轨(20,2)后收盘收回下轨上方"
                                   "（收回幅度≥带宽30%）且仍在中轨之下",
                           "fn": cond_boll_lower_support},
    "boll_break_mid":   {"label": "⑮ BOLL中轨突破多头",
                         "desc": "近5日首次放量上穿布林中轨(20,2)，带宽开口扩大"
                                 "且收盘站上60日均线",
                         "fn": cond_boll_break_mid},
}

# 每个条件依赖的上下文字段（按需加载，跳过无关查询，大幅提速）
_COND_NEEDS = {
    "rising_channel":   {"bars"},
    "pullback_support": {"bars"},
    "small_yang":       {"bars"},
    "w_bottom":         {"bars"},
    "three_lines_bloom": {"bars"},
    "divergence":       {"bars"},
    "low_golden_cross": {"bars"},
    "pe_low":           {"snap"},
    "pb_low":           {"snap"},
    "high_dividend":    {"snap"},
    "exchange_sz":      {"meta"},
    "exchange_sh":      {"meta"},
    "exchange_bj":      {"meta"},
    "boll_lower_support": {"bars"},
    "boll_break_mid":   {"bars"},
}
_ALL_FIELDS = {"bars", "val", "div", "meta", "snap"}


# ============================================================================
# 加载单只股票上下文
# ============================================================================
def _load_ctx(code, cfg, cache, lock, needs=None):
    fields = frozenset(needs) if needs else frozenset(_ALL_FIELDS)
    key = (str(code), fields)
    with lock:
        if key in cache:
            return cache[key]
    rconn = db.reader()
    ctx = {}
    if "bars" in fields:
        # 保持逐只索引查询：本模块只需 300 根，逐只 SELECT 走 idx_daily_code，
        # 实测全市场 5554 只仅 3.18s；而批量取 300 交易日窗口有 ~4.3s 固定成本
        # （扫全库再 groupby 切分），任何规模下都更慢 → 不做批量。
        bars = pd.read_sql_query(
            "SELECT date, open, high, low, close, volume, amount "
            "FROM daily WHERE code=? ORDER BY date DESC LIMIT ?",
            rconn, params=(str(code), int(cfg.get("BAR_WINDOW", 300))))
        if not bars.empty:
            bars = bars.iloc[::-1].reset_index(drop=True)
            bars["date"] = pd.to_datetime(bars["date"])
        ctx["bars"] = bars
    if "val" in fields:
        val = pd.read_sql_query(
            "SELECT date, pe_ttm, pb FROM valuation WHERE code=? ORDER BY date",
            rconn, params=(str(code),))
        if not val.empty:
            val["date"] = pd.to_datetime(val["date"])
        ctx["val"] = val
    if "div" in fields:
        div_rows = rconn.execute(
            "SELECT ex_date, cash_per_10 FROM dividend WHERE code=? ORDER BY ex_date",
            (str(code),)).fetchall()
        div = []
        for d, c in div_rows:
            try:
                div.append((datetime.strptime(d, "%Y-%m-%d").date(), float(c)))
            except Exception:
                continue
        ctx["div"] = div
    if "meta" in fields:
        mrow = rconn.execute(
            "SELECT code, name, sector, exchange, listing_date FROM meta WHERE code=?",
            (str(code),)).fetchone()
        meta = {"code": code}
        if mrow:
            meta = {"code": mrow[0], "name": mrow[1], "sector": mrow[2],
                    "exchange": mrow[3], "listing_date": mrow[4]}
        ctx["meta"] = meta
    if "snap" in fields:
        srow = rconn.execute(
            "SELECT pe_ttm, pb, pe_pct, pb_pct, div_yield FROM snapshot WHERE code=?",
            (str(code),)).fetchone()
        ctx["snap"] = {
            "pe_ttm": srow[0], "pb": srow[1], "pe_pct": srow[2],
            "pb_pct": srow[3], "div_yield": srow[4],
        } if srow else {}
    with lock:
        cache[key] = ctx
    return ctx


def _compute_condition(key, codes, cfg, cache, lock, needs):
    fn = CONDITIONS[key]["fn"]
    hits = set()
    n_errors = 0
    for code in codes:
        try:
            ctx = _load_ctx(code, cfg, cache, lock, needs)
            if fn(ctx, cfg):
                hits.add(code)
        except Exception:
            n_errors += 1
    return key, hits, n_errors


def load_universe(exchange=None, sectors=None,
                  exclude_st: bool = True,
                  new_stock_days: int = 365):
    """股票池（范围 + 通用排除）。"""
    rconn = db.reader()
    meta = pd.read_sql_query(
        "SELECT code, name, sector, exchange, listing_date FROM meta", rconn)
    if exchange in ("SZ", "SH", "BJ"):
        meta = meta[meta["exchange"] == exchange]
    if sectors:
        meta = meta[meta["sector"].isin(set(sectors))]
    today = date.today()
    keep = []
    n_st = n_new = 0
    for _, r in meta.iterrows():
        name = str(r.get("name", ""))
        if exclude_st and "ST" in name.upper():
            n_st += 1
            continue
        ld = r.get("listing_date")
        if ld:
            try:
                ld_d = datetime.strptime(str(ld), "%Y-%m-%d").date()
                if (today - ld_d).days < new_stock_days:
                    n_new += 1
                    continue
            except ValueError:
                pass
        keep.append(str(r["code"]))
    return keep, {"st": n_st, "new": n_new}


def run(condition_keys: list[str], cfg: dict,
        exchange=None, sectors=None,
        max_workers: int = 16,
        progress_cb=None) -> pd.DataFrame:
    """组合筛选（多条件并行取交集），按 PE-TTM 由低到高排序。"""
    cfg = {**DEFAULTS, **(cfg or {})}
    keys = [k for k in condition_keys if k in CONDITIONS]
    if not keys:
        raise ValueError("请至少勾选一个筛选条件")
    codes, stats = load_universe(exchange, sectors,
                                 exclude_st=cfg.get("EXCLUDE_ST", True),
                                 new_stock_days=int(cfg.get("NEW_STOCK_DAYS", 365)))
    if not codes:
        return pd.DataFrame()
    cache, lock = {}, threading.Lock()
    cond_hits = {}
    # 注意：逐股 pandas 查询受 GIL 限制，多线程反而因锁竞争/上下文切换变慢。
    # 串行 + 共享 cache 是最快路径（多条件共享同一批 bars/val）。
    if progress_cb:
        progress_cb(0, len(keys), "计算条件")
    for k in keys:
        _, hits, errs = _compute_condition(
            k, codes, cfg, cache, lock, _COND_NEEDS.get(k, _ALL_FIELDS))
        cond_hits[k] = hits
        if progress_cb:
            progress_cb(len(cond_hits), len(keys),
                        f"条件 {CONDITIONS[k]['label']}: {len(hits)} 只命中")
    hit_codes = sorted(set.intersection(*(cond_hits.values())))
    rconn = db.reader()
    rows = []
    for code in hit_codes:
        bars = pd.read_sql_query(
            "SELECT date, close, volume, amount FROM daily WHERE code=? "
            "ORDER BY date DESC LIMIT 2",
            rconn, params=(str(code),))
        mrow = rconn.execute(
            "SELECT name, sector FROM meta WHERE code=?", (str(code),)).fetchone()
        name = (mrow[0] if mrow else None) or code
        sector = (mrow[1] if mrow else None) or "未分类"
        price = change = last_date = None
        if not bars.empty:
            bars = bars.iloc[::-1]
            price = float(bars["close"].iloc[-1])
            last_date = bars["date"].iloc[-1]
            if len(bars) >= 2:
                prev = float(bars["close"].iloc[-2])
                if prev > 0:
                    change = (price - prev) / prev * 100.0
        val = pd.read_sql_query(
            "SELECT pe_ttm FROM valuation WHERE code=? ORDER BY date DESC LIMIT 1",
            rconn, params=(str(code),))
        pe = float(val["pe_ttm"].iloc[0]) if not val.empty and pd.notna(
            val["pe_ttm"].iloc[0]) else None
        rows.append({
            "代码": code, "名称": name, "板块": sector,
            "当前价": price, "涨跌幅(%)": change, "PE-TTM": pe,
            "触发条件": "、".join(CONDITIONS[k]["label"] for k in keys),
            "数据日期": last_date,
        })
    out = pd.DataFrame(rows, columns=["代码", "名称", "板块", "当前价",
                                      "涨跌幅(%)", "PE-TTM",
                                      "触发条件", "数据日期"])
    if not out.empty:
        out = out.sort_values("PE-TTM", ascending=True,
                              na_position="last").reset_index(drop=True)
    return out


# ============================================================================
# 预设组合（一键套用「条件 + 阈值」）
# ============================================================================
# 设计说明
# --------
# 两个组合对应两种截然不同的买入风格，且**只使用本模块已有的条件与阈值键**，
# 不引入任何新参数，保证「一键套用」后前端控件与后端 cfg 完全对得上。
#
# A 趋势启动型：顺中期趋势，等突破确认后介入（右侧交易，持有期偏长）
# B 超跌反弹型：左侧低吸，等跌到估值/支撑极端位置再进（均值回归）
#
# 阈值取值依据（scripts/_boll_bt.py / _boll_tune*.py 事件研究回测）：
#   - 下轨低吸在 T+120 的胜率约 47~51%，均收益 +6.4%~+9.7%（跑赢基准 2~3pp）
#   - 中轨突破在 T+120 的胜率约 44~47%，均收益 +6.4%~+9.5%
#   两者**均未稳定超过 50% 胜率**，故组合只做「筛选漏斗」定位，
#   前端会明确披露实测胜率，不假装是「高胜率策略」。
#
# ★条件数量经实测标定：全市场跑一遍，条件交集过深会直接归零。
#   boll_break_mid 单独 31 只；× low_golden_cross(120) = 4 只；× three_lines_bloom(477) = 3 只；
#   boll_break_mid × pullback_support(1657) = 23 只 ← 用这个，既够严又能出票。
#   boll_lower_support 单独 951 只；× w_bottom = 49 只 ← 双底部形态互相确认，出票充足。
PRESETS = [
    {
        "key": "A",
        "name": "组合 A · 趋势启动",
        "style": "趋势跟随 · 右侧",
        "desc": "布林放量突破中轨 + 回踩 20 日线确认支撑，等突破回踩后再顺势介入，适合能拿住的中线。",
        "keys": ["boll_break_mid", "pullback_support"],
        "cfg": {
            "BOLL_N": 20, "BOLL_K": 2.0,
            "BOLL_BREAK_VOL": 1.30,      # 比默认 1.20 更严：要更明确的放量确认
            "BOLL_BREAK_WIDEN": 1.00,    # 要开口扩张，排除收口假突破
            "BOLL_BREAK_DAYS": 5,
            "BOLL_BREAK_LOOKBACK": 10,
            "BOLL_BREAK_MA60": 1,
            "PULLBACK_DAYS": 20, "SUPPORT_MA": 20, "PULLBACK_TOL": 2.0,
            "EXCLUDE_ST": True, "NEW_STOCK_DAYS": 365,
        },
        "backtest": {"horizon": 120, "win": 47.2, "ret": 9.54,
                     "note": "中轨突破 · T+120 实测（含强牛市场过滤）"},
    },
    {
        "key": "B",
        "name": "组合 B · 超跌反弹",
        "style": "均值回归 · 左侧",
        "desc": "布林下轨低吸 + W 底突破颈线，两个底部信号互相印证，适合左侧分批埋伏、耐心持有。",
        "keys": ["boll_lower_support", "w_bottom"],
        "cfg": {
            "BOLL_N": 20, "BOLL_K": 2.0,
            "BOLL_TOUCH_TOL": 1.5,       # 比默认 1.0 略宽：低吸不必苛求精确触轨
            "BOLL_REBOUND_RATIO": 0.35,  # 比默认 0.30 更严：要更实的收回（排除贴轨阴跌）
            "BOLL_MAX_TREND_DAYS": 3,
            "W_BOTTOM_DAYS": 60, "W_BOTTOM_TOL": 3.0,
            "EXCLUDE_ST": True, "NEW_STOCK_DAYS": 365,
        },
        "backtest": {"horizon": 120, "win": 50.7, "ret": 9.69,
                     "note": "下轨低吸 · T+120 实测（含强牛市场过滤）"},
    },
]


def preset_list() -> list[dict]:
    """预设组合清单（供 API 返回；cfg 为纯增量，前端与 DEFAULTS 合并后使用）。"""
    out = []
    for p in PRESETS:
        out.append({
            "key": p["key"], "name": p["name"], "style": p["style"],
            "desc": p["desc"], "keys": list(p["keys"]),
            "cfg": dict(p["cfg"]), "backtest": dict(p["backtest"]),
        })
    return out