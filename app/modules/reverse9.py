# -*- coding: utf-8 -*-
"""9Reverse9 —— 神奇九转「即将触发」筛选 + 历史反转时点统计（日线 / 周线 / 月线）。

指标定义（神奇九转 / TD Sequential Setup）
==========================================
以 4 个周期前的收盘价为基准（ref_gap=4）：
  买入设置：close[t] < close[t-4]，从首次满足起连续计数；连续满 9 个周期 → **买入九转**（低九）
  卖出设置：close[t] > close[t-4]，连续满 9 个周期 → **卖出九转**（高九）
计数在条件中断时归零；满 9 后计数复位，此后可再走出新的 9（第 18、27 天同为信号）。
本模块全程只使用 t 时刻及之前的收盘数据，不含任何未来函数。

**周期（periods）**：day 日线 / week 周线 / month 月线。三者是同一套指标在不同
K 线粒度上的实例，各自独立成立：
  · 周线一根 = 该自然周内**最后一个交易日**的收盘（周内最高/最低为周内极值）；
  · 月线一根 = 该自然月内最后一个交易日的收盘。
  · 九转比较的「4 个周期前」：日线 = 4 个交易日、周线 = 4 周、月线 = 4 个月。
  · n / m 的单位随之改变：日线记「天」、周线记「周」、月线记「月」。

**多周期叠加（关键设计）**
========================
几个周期可以叠加一起筛选，但叠加是**充分非必要**条件 —— 满足任一周期即可入选（并集），
满足两个及以上只是额外信息，不是入选门槛。因此**绝不允许**把某一周期的参数套到另一
周期上，实现上一律：

    for P in periods:                       # 每个周期各自独立跑完整流程
        cands_P = screen(P, cfg_P)          #   程序一：用 P 自己的 near_remaining（单位 = P 的 bar）
        for c in cands_P: _analyze(..., P)  #   程序二：用 P 自己的 match_window / confirm_bars
    merged = 按 code 合并(各周期候选)         # **最后**才整体并集

反例（必须避免）：用日线的「离九转还剩 2 日」去限制月线九转 —— 月线的「还差 2」
指 2 个月，量纲完全不同，混用会把月线信号全部误杀。本模块用
「每周期一个独立 cfg 副本」（`_period_cfg`）从根本上杜绝该问题。

程序一：筛选「即将到达买入九转」的股票（逐周期独立执行）
======================================================
当前买入计数 c ∈ [9-N, 8]（N = near_remaining，默认 2 → 即 c ∈ {7, 8}）。
N=1 只留「还差 1 天」；N=3 放宽到「还差 3 天以内」。c=9（今日刚好达成）默认不计入
（可开 include_triggered），因为它已经不是「即将」而是「到达」。
「当前」统一锚定在**参考日 ref_date**：覆盖率 ≥80% 的最新交易日。
（unified_data.db 最近两天常常只有部分股票同步完成，若各股各用最后一根，
 计数口径会不一致 → 用覆盖率闸门挑一个全市场都有的日期，口径统一。）

程序二：历史反转时点统计（对程序一筛出的股票）
============================================
拉取长历史（hist.db 全历史 ∪ unified daily 补齐最新，两者同为前复权口径，
实测重叠区间逐行相等），找出历史上全部买入九转 / 卖出九转信号日 i，
再在**对称窗口** [i-W, i+W] 内定位「股票真正开始反转」的时间节点 j：
  买入九转：j = 窗口内**最低价**所在日  → n = j - i
             n > 0 「9Rev n天后开始真正反弹」/ n = 0 信号日即底 / n < 0「9Rev 反弹起点在信号前|n|天」
  卖出九转：j = 窗口内**最高价**所在日  → m = i - j
             m > 0 「9Rev 提前m天开始下行」/ m = 0 信号日即顶 / m < 0「9Rev 信号后|m|天开始下行」
界内判据采用用户原话的「绝对值 ≤ W」（W = match_window，默认 5）。

**为什么必须用对称窗口**（2026-09-21 实测，900 只 / 17070 个买入信号 / 13546 个卖出信号）：
  买入九转：真底在信号之前的占 24.3%，当天或之后的占 75.7%；
  卖出九转：真顶在信号之后的占 **57.6%**，之前的只占 42.4%。
只把窗口锁在一侧（买入只看信号后、卖出只看信号前）会把另一侧的真实拐点
错报成「窗口内的次极值」，等于报了一个并不成立的「真正反转节点」。

**反转确认**：从真拐点 j 之后 confirm_bars 根内，必须出现 ≥ confirm_pct 的反向运动
（买入看涨幅、卖出看跌幅），否则判为「未确认」丢弃。没有这道闸，
单边下跌途中的任意一根 K 线都会被算成「地板」，统计会整体失真。
靠近数据末端、拐点之后数据不足以确认的信号同样按「待确认」剔除，不污染统计。

排序（严格照用户给定的优先级，逐级生效）
======================================
 1st 历史买入九转的 n 越小越靠前   → 主键 = **仅统计 n ≥ 0 样本**的平均 n 升序。
      （n < 0 表示信号发出时反弹已经开始、属滞后信号，混进均值会让
        「信号越滞后排得越前」，与用户意图相反，故单列展示、不计入主键。）
      并对样本量做**收缩**：n_rank = (Σn + K×全市场均值) / (m + K)。
      理由：用户说的是「历史上**每次** 9 转的 n 越小越靠前」，而实测首版 Top-1
      只靠 **1 个** n=0 样本就以 mean=0 霸榜（全量 413 只里有 21 只样本数 ≤1），
      单点样本谈不上「每次」。收缩后样本多的股票胜出，样本少的被拉回全市场均值。
      K = `shrink_k`（默认 3，可调；设 0 即退回纯平均 n 的字面口径）。
 2nd 一年内卖出九转信号越多越靠前   → 次键 = 近一年卖出九转条数降序
 3rd 代码升序（仅作稳定收尾）
多周期叠加时：主键取各命中周期中**最优（最小）**的排序分，次键取各周期卖出九转条数之和，
并在前端把「命中周期」与「各周期的 n/m」分别列出，不把不同量纲的 n 混在一个均值里。

性能
====
不使用进程池：程序一用 batchload 一次性取回全市场近 N 个交易日的日K（1 次查询），
周/月线由该日线在内存里 resample 得到（不额外查库），用 numpy 向量化算连续计数；
程序二只对候选股（通常几十到几百只）做「hist.db 单只索引查询 + 极值定位」，
每只 2 次查询、约 3ms，周/月线同样由取回的日线内存重采样。
多周期只增加「重复一遍向量化计数」的成本，不增加数据库往返。
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from ..core import batchload
from ..core import db

# ---- 常量 ----
RECENT_WINDOW = 60        # 程序一取数窗口（日线根数）：够 20 日均额 + 日线九转计数
MIN_RECENT_BARS = 15      # 少于该根数无法算九转计数，直接跳过
MIN_HIST_BARS = 60        # 程序二回看下限（**换算到各周期的 bar 数**）：太短没必要统计「历史」
FULL_COVERAGE = 0.80      # 参考日覆盖率闸门（80% 的股票都有当天数据）
MAIN_LEN = 9              # 神奇九转周期长度

# ---- 周期定义 ----
# 每个周期一个独立小配置：取数窗口、最少 bar 数、九转基准的默认值、n/m 的单位名。
# 关键：`窗口` 与 `最少根数` 都以**该周期自己的 bar** 计，天然杜绝量纲混用。
PERIODS = {
    "day": {
        "label": "日线", "unit": "天",
        "recent_bars": 60,      # 需 ≥ ref_gap(4) + 9 + 20 日均额
        "factor": 1,            # 1 个日线 bar = 1 个交易日（用于把 hist_years 换算成 bar 数）
        "min_recent": 15,       # 少于 15 根日线无法算计数
        "min_hist": 60,         # 历史至少 60 根日线
        "rule": "weekday",
    },
    "week": {
        "label": "周线", "unit": "周",
        "recent_bars": 40,      # 需 ≥ 4(基准) + 9 + 20(均额) 根**周**线 ≈ 40 周
        "factor": 5,            # 1 个周线 bar ≈ 5 个交易日
        "min_recent": 15,
        "min_hist": 40,
        "rule": "W-FRI",
    },
    "month": {
        "label": "月线", "unit": "月",
        "recent_bars": 36,      # 4 + 9 + 20 根月线；36 个月 = 3 年，兼顾新股
        "factor": 21,           # 1 个月线 bar ≈ 21 个交易日
        "min_recent": 14,
        "min_hist": 24,         # 历史至少 24 根月线（2 年）
        "rule": "ME",
    },
}
PERIOD_ORDER = ("day", "week", "month")

DEFAULTS = {
    # —— 周期选择 ——
    "periods": ["day"],         # 可叠加：["day","week","month"] 表示并集筛选
    # —— 程序一：「即将」定义（单位 = 各周期自己的 bar）——
    "near_remaining": 2,        # 距买入九转还差 ≤ N 个周期（2 → 当前计数 ∈ {7,8}）
    "include_triggered": False,  # 是否把「刚达成第 9 个周期」也纳入
    "ref_gap": 4,               # 九转比较基准：close[t] vs close[t-ref_gap]（单位同周期）
    # —— 程序二：反转定位与确认（单位 = 各周期自己的 bar）——
    "match_window": 5,          # |n|、|m| 上限（默认 5，可调；单位同周期）
    "confirm_pct": 3.0,         # 反转确认幅度（%）
    "confirm_bars": 10,         # 反转确认观察窗口（**该周期的 bar 数**）
    "hist_years": 5,            # 历史回看年数（内部按周期换算成 bar 数）
    # —— 风控类硬门槛（不做条件的股票直接不参与筛选）——
    "exclude_st": True,
    "min_price": 2.0,
    "max_price": 2000.0,
    "min_amt20_yi": 0.5,        # 20 日均成交额下限（亿元）
    "min_listed_days": 120,
    # —— 输出控制 ——
    "max_results": 600,         # 最多返回多少只
    "show_records": 40,         # 每只股票每周期每侧最多回传多少条历史明细
    # —— 排序 ——
    # 用户规则「历史上每次 9 转的 n 越小越靠前」隐含要求有足够的历史样本。
    # 实测首版 Top-1 只靠 1 个 n=0 样本就以 mean=0 霸榜，与「每次」的语义不符，
    # 故对样本量做收缩：n_rank = (Σn + K×全市场均值) / (m + K)。
    # K=0 即退回「纯平均 n」（完全照字面口径）；K 越大越保守。
    "shrink_k": 3.0,
}

# 说明文案（供 /api/r9/meta 与前端 tooltip 复用）
DOC_LINES = [
    "神奇九转：close 连续 9 个周期低于 / 高于 4 个周期前 → 买入 / 卖出九转。",
    "周期可选 日线 / 周线 / 月线，各自独立成立；「4 个周期前」在周线是 4 周、月线是 4 个月。",
    "程序一：按每个**选中的周期**分别筛出「即将到达买入九转」的股票，再整体并集（叠加非门槛）。",
    "程序二：对候选股拉长历史，找出该周期历史上全部九转信号，在对称窗口内定位真正的顶 / 底。",
    "买入九转：n = 真底 − 信号日；卖出九转：m = 信号日 − 真顶。单位随周期变（日 / 周 / 月）。",
    "叠加时各周期**先分别筛选、再合并**，参数不跨周期复用（避免用「还差 2 日」限制月线）。",
    "反转需被拐点之后的行情确认（否则剔除）；排序主键只统计 n ≥ 0 的样本。",
]


# ================================================================ 九转计数
def _runlen(mask: np.ndarray) -> np.ndarray:
    """连续 True 的游程长度（第 t 位 = 以 t 结尾的连续 True 个数）。O(n) 向量化。"""
    n = int(mask.size)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    idx = np.where(~mask, np.arange(n), -1).astype(np.int64)
    np.maximum.accumulate(idx, out=idx)
    return np.arange(n, dtype=np.int64) - idx


def nine_state(close: np.ndarray, gap: int = 4) -> tuple:
    """返回 (买入信号布尔数组, 卖出信号布尔数组, 买入游程, 卖出游程)。

    信号 = 游程长度 % 9 == 0（满 9 记一次信号并复位，18、27… 同样记）。
    """
    n = int(close.size)
    zero = np.zeros(n, dtype=bool)
    if n <= gap:
        return zero, zero, np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)
    cond_b = np.zeros(n, dtype=bool)
    cond_s = np.zeros(n, dtype=bool)
    cond_b[gap:] = close[gap:] < close[:-gap]
    cond_s[gap:] = close[gap:] > close[:-gap]
    rb = _runlen(cond_b)
    rs = _runlen(cond_s)
    sig_b = (rb > 0) & (rb % MAIN_LEN == 0)
    sig_s = (rs > 0) & (rs % MAIN_LEN == 0)
    return sig_b, sig_s, rb, rs


def current_count(run_last: int) -> tuple:
    """把「截至最后一根的游程」换算成当前计数。

    returns (count, just_triggered)：
      run_last=0            → (0, False)   不在设置中
      run_last=8            → (8, False)   还差 1 天
      run_last=9            → (9, True)    今日刚好达成九转
      run_last=11           → (2, False)   已达成过，新一轮的第 2 天
    """
    r = int(run_last)
    if r <= 0:
        return 0, False
    if r % MAIN_LEN == 0:
        return MAIN_LEN, True
    return r % MAIN_LEN, False


# ================================================================ 周期重采样
def resample_ohlc(dates: list, o: np.ndarray, h: np.ndarray, l: np.ndarray,
                  c: np.ndarray, period: str) -> tuple:
    """把日线 OHLC 重采样成周线 / 月线（period="day" 时原样返回）。

    约定（与「周线是本周最后一天收盘」的通行口径一致）：
      · 一根 bar 的收盘 = 该周期内**最后一个**交易日收盘；
      · 开盘 = 该周期内**第一个**交易日开盘；
      · 最高 / 最低 = 该周期内极值；
      · 该 bar 的日期 = 该周期内**最后一个**交易日（信号发生在周期末才算成立，
        与「周线收盘后才能确认本周九转」的现实一致，不含未来函数）。
    标签：周线用 W-FRI（周五收盘的周归属；节假日提前则归到该周最后交易日），
          月线用 ME（月末）。日期一律取组内真实最后一个交易日，不用标签日，
          因为标签日可能是节假日（非交易日会给前端展示带来困扰）。
    """
    if period == "day" or not dates:
        return list(dates), o, h, l, c
    rule = PERIODS.get(period, {}).get("rule")
    if not rule:
        return list(dates), o, h, l, c
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    df = pd.DataFrame({"o": o, "h": h, "l": l, "c": c}, index=idx)
    g = df.resample(rule)
    agg = g.agg({"o": "first", "h": "max", "l": "min", "c": "last"}).dropna()
    if agg.empty:
        return [], np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0)
    # 每组的真实最后交易日 = 原始索引按组取 max
    last_day = g.apply(lambda x: x.index[-1] if len(x) else pd.NaT)["c"].dropna()
    out_dates = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in last_day.values]
    # 长度可能与 agg 不完全一致（空组已 dropna），按 agg 索引对齐
    if len(out_dates) != len(agg):
        out_dates = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in agg.index]
    return (out_dates,
            agg["o"].to_numpy(dtype=float), agg["h"].to_numpy(dtype=float),
            agg["l"].to_numpy(dtype=float), agg["c"].to_numpy(dtype=float))


def period_bars(years: float, period: str) -> int:
    """把「历史回看年数」换算成该周期的 bar 数（用 PERIODS.factor，年按 250 交易日）。"""
    factor = int(PERIODS.get(period, {}).get("factor", 1)) or 1
    return max(int(years * 250 / factor), PERIODS.get(period, {}).get("min_hist", 60))


# ================================================================ 参考日
def _coverage_ref_date(present: list, bars_map: dict) -> Optional[str]:
    """在**所选范围**内挑「覆盖率 ≥80% 的最新交易日」作为参考日。

    注意不能拿「全市场某日的行数」去比对「当前范围股票数」：那样选北交所
    （仅 270 只）时，任何一天的全市场行数都会 ≥ 216，参考日会被误判成最新
    那天，而当天可能根本没有北交所数据。这里改为逐日统计**范围内**真实覆盖。
    """
    cov: dict = {}
    for c in present:
        for d in set(bars_map[c]["date"]):
            cov[d] = cov.get(d, 0) + 1
    if not cov:
        return None
    thr = max(1, int(len(present) * FULL_COVERAGE))
    for d in sorted(cov.keys(), reverse=True):
        if cov[d] >= thr:
            return pd.Timestamp(d).strftime("%Y-%m-%d")
    return pd.Timestamp(max(cov.keys())).strftime("%Y-%m-%d")


# ================================================================ 程序一
def _prefilter(sub: pd.DataFrame, meta: dict, cfg: dict) -> Optional[str]:
    """风控类硬门槛（**只用日线**，与周期无关）。返回跳过原因，通过返回 None。

    流动性 / 价格 / ST / 次新这类门槛是「这只票本身能不能碰」，跟用哪个周期看九转
    无关，所以统一在日线上判一次即可，不必也不可能按周线判（周线没有 amount）。
    """
    if len(sub) < MIN_RECENT_BARS:
        return "数据不足"
    close = float(sub["close"].iloc[-1])
    if not (cfg["min_price"] <= close <= cfg["max_price"]):
        return "价格区间"
    name = str(meta.get("name") or "")
    if cfg["exclude_st"] and ("ST" in name.upper() or "退" in name):
        return "ST/退市"
    amt = sub["amount"].tail(20)
    amt20 = float(amt.mean()) if len(amt) and np.isfinite(amt.mean()) else 0.0
    if amt20 < float(cfg["min_amt20_yi"]) * 1e8:
        return "流动性不足"
    ld = str(meta.get("listing_date") or "")
    if len(ld) >= 10:
        try:
            days = (datetime.now().date() - datetime.strptime(ld[:10], "%Y-%m-%d").date()).days
            if days < int(cfg["min_listed_days"]):
                return "次新股"
        except Exception:  # noqa: BLE001 —— 上市日异常时不剔
            pass
    return None


def _period_cfg(cfg: dict, period: str) -> dict:
    """为一个周期生成**独立的** cfg 副本。

    这是「避免量纲混用」的落点：每个周期拿到自己的 near_remaining / ref_gap /
    match_window / confirm_bars，互不干扰（单位天然 = 该周期的 bar）。
    允许调用方用 `per_period` 字段为某周期单独覆盖参数，例如：
        {"periods": ["day","month"], "per_period": {"month": {"near_remaining": 1}}}
    """
    c = dict(cfg)
    override = (cfg.get("per_period") or {}).get(period) or {}
    for k, v in override.items():
        if v is not None:
            c[k] = v
    return c


def screen(cfg: dict, exchange: Optional[str], sectors: Optional[list],
           progress_cb=None, period: str = "day", bars_map=None,
           ref_date: Optional[str] = None, rows=None) -> tuple:
    """程序一（**单周期**）：返回 (候选列表, 跳过统计, 参考日)。

    period="day" 时取全市场近 N 交易日的日K；"week"/"month" 时把同一份日K
    在内存里重采样成周/月线，**不额外查库**（bars_map / ref_date / rows 可由
    上层复用传入，避免多周期重复查库）。

    每一项的含义都以 `period` 为单位：
      cur_count  当前买入计数（还差 9-cnt  **个该周期 bar** 到达九转）
      remain     还差几个该周期 bar
    """
    pdef = PERIODS.get(period) or PERIODS["day"]
    # 日线窗口必须足够长，才能重采样出足够的周/月线 bar：
    # 例如月线要 36 根 bar ≈ 36 个月 ≈ 750 个交易日，取 60 天日线是绝对不够的。
    need_bars = int(pdef["recent_bars"])
    day_window = max(RECENT_WINDOW, need_bars * int(pdef["factor"]))

    if rows is None:
        rconn = db.reader()
        where, args = "", []
        if exchange in ("SZ", "SH", "BJ"):
            where = " WHERE exchange=?"
            args.append(exchange)
        if sectors:
            where += (" AND " if where else " WHERE ") + \
                f"sector IN ({','.join('?' * len(sectors))})"
            args.extend(sectors)
        rows = rconn.execute(
            f"SELECT code, name, sector, exchange, listing_date FROM meta{where}",
            tuple(args)).fetchall()
    if not rows:
        return [], {}, None

    # 一次性取回全市场日K（1 次查询，多周期共享同一份内存缓存）
    if bars_map is None:
        bars_map = batchload.load_daily_map(window_days=day_window)
    if not bars_map:
        return [], {"无行情数据": len(rows)}, None
    present = [str(r[0]) for r in rows
               if str(r[0]) in bars_map and not bars_map[str(r[0])].empty]

    # 参考日只认**日线**口径（覆盖率闸门），与周期无关：它是「当前」的锚点，
    # 周/月线只是把它当作重采样的右端点，保证所有周期共用同一个「今天」。
    if ref_date is None:
        ref_date = _coverage_ref_date(present, bars_map)
    if not ref_date:
        return [], {"无行情数据": len(rows)}, None
    ref_ts = pd.Timestamp(ref_date)

    gap = int(cfg["ref_gap"])
    near = int(cfg["near_remaining"])
    lo_cnt = max(1, MAIN_LEN - near)
    min_recent = int(pdef["min_recent"])

    out: list = []
    skip: dict = {}
    total = len(rows)
    for i, r in enumerate(rows):
        if progress_cb and (i % 500 == 0 or i == total - 1):
            progress_cb(i + 1, total,
                        f"程序一 · {pdef['label']}九转计数 {i + 1}/{total}")
        code = str(r[0])
        meta = {"name": r[1], "sector": r[2], "listing_date": r[4]}
        df = bars_map.get(code)
        if df is None or df.empty:
            skip["无行情"] = skip.get("无行情", 0) + 1
            continue
        sub = df[df["date"] <= ref_ts]
        if len(sub) < MIN_RECENT_BARS:
            skip["数据不足"] = skip.get("数据不足", 0) + 1
            continue
        # 参考日当天必须真的有行情：只按 <= ref_date 过滤会让长期停牌股拿 N 天前的
        # 旧K线参与「当前计数」，与其它股票口径不一致（等于在推荐一只停牌的票）。
        if str(sub["date"].iloc[-1])[:10] != ref_date:
            skip["参考日无行情"] = skip.get("参考日无行情", 0) + 1
            continue
        # 风控门槛只在日线上判一次（价格/流动性/ST/次新都与观察周期无关）
        reason = _prefilter(sub, meta, cfg)
        if reason:
            skip[reason] = skip.get(reason, 0) + 1
            continue

        dts = [str(x)[:10] for x in sub["date"]]
        o_ = pd.to_numeric(sub["open"], errors="coerce").to_numpy(dtype=float)
        h_ = pd.to_numeric(sub["high"], errors="coerce").to_numpy(dtype=float)
        l_ = pd.to_numeric(sub["low"], errors="coerce").to_numpy(dtype=float)
        cl_ = pd.to_numeric(sub["close"], errors="coerce").to_numpy(dtype=float)
        if period != "day":
            dts, o_, h_, l_, cl_ = resample_ohlc(dts, o_, h_, l_, cl_, period)
        if len(cl_) < min_recent:
            skip[f"{pdef['label']}不足"] = skip.get(f"{pdef['label']}不足", 0) + 1
            continue
        if not np.all(np.isfinite(cl_[-min(len(cl_), gap + 1):])):
            skip["数据异常"] = skip.get("数据异常", 0) + 1
            continue
        _sb, _ss, rb, _rs = nine_state(cl_, gap)
        cnt, trig = current_count(int(rb[-1]))
        ok = (trig and cfg["include_triggered"]) or (lo_cnt <= cnt <= MAIN_LEN - 1)
        if not ok:
            continue
        out.append({
            "code": code,
            "name": r[1] or "",
            "sector": r[2] or "",
            "exchange": r[3] or "",
            "period": period,
            "period_label": pdef["label"],
            "unit": pdef["unit"],
            "bar_date": dts[-1] if dts else ref_date,   # 该周期最后一根 bar 的日期
            "cur_count": int(cnt),
            "remain": int(MAIN_LEN - cnt) if not trig else 0,
            "triggered": bool(trig),
            "close": round(float(cl_[-1]), 2),
        })
    return out, skip, ref_date


# ================================================================ 程序二
def _load_history(rconn: sqlite3.Connection, code: str, hist_bars: int,
                  ref_date: str) -> tuple:
    """长历史：hist.db（全历史，前复权）∪ unified daily（补齐最新）。

    实测两库重叠区间逐行相等（同为前复权），可安全合并；unified 侧只取
    hist 最后日期之后的增量，避免重复行。
    **两库都必须截到 ref_date**：hist.db 的部分股票已同步到 ref_date 之后，
    若不过滤，历史会越过「当前计数」的基准日，凭空多出一段相对未来，
    与程序一的口径自相矛盾。
    """
    dates: list = []
    o: list = []
    h: list = []
    l: list = []
    c: list = []
    root = os.path.dirname(os.path.abspath(db.db_path()))
    hist_path = os.path.join(root, "hist.db")
    last_hist = ""
    if os.path.exists(hist_path):
        try:
            hp = os.path.abspath(hist_path).replace("\\", "/")
            hc = sqlite3.connect(f"file:{hp}?mode=ro", uri=True, timeout=30)
            try:
                hs = hc.execute(
                    "SELECT date, open, high, low, close FROM daily_hist "
                    "WHERE code=? AND date<=? ORDER BY date DESC LIMIT ?",
                    (code, ref_date, int(hist_bars))).fetchall()
            finally:
                hc.close()
            for d, oo, hh, ll, cc in reversed(hs):
                dates.append(str(d)[:10]); o.append(oo); h.append(hh)
                l.append(ll); c.append(cc)
            if dates:
                last_hist = dates[-1]
        except Exception:  # noqa: BLE001 —— hist.db 缺失/损坏时退回主库
            dates, o, h, l, c = [], [], [], [], []
            last_hist = ""
    if last_hist:
        us = rconn.execute(
            "SELECT date, open, high, low, close FROM daily "
            "WHERE code=? AND date>? AND date<=? ORDER BY date",
            (code, last_hist, ref_date)).fetchall()
    else:
        us = rconn.execute(
            "SELECT date, open, high, low, close FROM daily "
            "WHERE code=? AND date<=? ORDER BY date DESC LIMIT ?",
            (code, ref_date, int(hist_bars))).fetchall()
        us = list(reversed(us))
    for d, oo, hh, ll, cc in us:
        dates.append(str(d)[:10]); o.append(oo); h.append(hh)
        l.append(ll); c.append(cc)
    if not dates:
        return ([], np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0))
    return (dates,
            np.asarray(o, dtype=float), np.asarray(h, dtype=float),
            np.asarray(l, dtype=float), np.asarray(c, dtype=float))


def _analyze(dates: list, high: np.ndarray, low: np.ndarray, close: np.ndarray,
             cfg: dict, period: str = "day") -> dict:
    """在单只股票的（已重采样到 `period` 的）长历史上提取九转信号 + 真正反转节点。

    **单位随 period 变**：窗口 W、确认窗口 cbars、n/m 全部以该周期的 bar 计，
    文案里的「天 / 周 / 月」也相应替换（由 `unit` 决定），避免出现「周线上
    说 9Rev 3 天」这种量纲错乱的表述。

    极值搜索用**对称窗口** [i-W, i+W]：真拐点既可能在信号之后、也可能在之前，
    只锁一侧会把另一侧的真实拐点错报成"窗口内的次极值"。实测（日线 900 只 /
    17070 个买入信号 / 13546 个卖出信号）：
        买入九转 24.3% 的真底落在信号之前（信号滞后），75.7% 在当天或之后；
        卖出九转 57.6% 的真顶落在信号之后（信号其实是提前预警），42.4% 在之前。
    所以 n / m 允许为负，界内判据是用户原话的「绝对值 ≤ W」：
        n = 真底 − 信号日   （n>0 反弹在信号后；n=0 信号日即底；n<0 底在信号前）
        m = 信号日 − 真顶   （m>0 真顶在信号前「提前」；m<0 真顶在信号后）
    确认闸从**真拐点 j 之后**起算（而非从信号日起算）：只有拐点之后确实反转了才算数。
    """
    unit = (PERIODS.get(period) or {}).get("unit", "天")
    gap = int(cfg["ref_gap"])
    W = max(1, int(cfg["match_window"]))
    cpct = float(cfg["confirm_pct"]) / 100.0
    cbars = max(1, int(cfg["confirm_bars"]))
    n = int(close.size)
    sig_b, sig_s, _rb, _rs = nine_state(close, gap)

    buy: list = []
    sell: list = []
    pend_b = pend_s = 0

    def _extreme(seg: np.ndarray, base: int, sig: int, is_min: bool) -> int:
        """窗口内取极值；平局时取**离信号日最近**的那一天（不偏袒任何一侧）。"""
        m = float(seg.min()) if is_min else float(seg.max())
        cand = np.flatnonzero(seg == m) + base
        return int(cand[np.argmin(np.abs(cand - sig))])

    for i in np.flatnonzero(sig_b):
        i = int(i)
        lo, hi = max(0, i - W), min(n - 1, i + W)
        j = _extreme(low[lo:hi + 1], lo, i, True)
        k2 = min(n - 1, j + cbars)
        if k2 <= j:                       # 拐点之后数据不足 → 待确认
            pend_b += 1
            continue
        if float(np.max(close[j + 1:k2 + 1])) < float(close[j]) * (1.0 + cpct):
            continue                       # 拐点后未出现有效反弹 → 丢弃
        nn = j - i
        if abs(nn) > W:
            continue
        buy.append({
            "date": dates[i], "rev_date": dates[j], "n": int(nn),
            "price": round(float(close[i]), 2),
            "rev_price": round(float(low[j]), 2),
            "text": (f"9Rev {nn}{unit}后开始真正反弹" if nn >= 0
                     else f"9Rev 反弹起点在信号前{-nn}{unit}"),
        })

    for i in np.flatnonzero(sig_s):
        i = int(i)
        lo, hi = max(0, i - W), min(n - 1, i + W)
        j = _extreme(high[lo:hi + 1], lo, i, False)
        k2 = min(n - 1, j + cbars)
        if k2 <= j:                       # 拐点之后数据不足 → 待确认
            pend_s += 1
            continue
        if float(np.min(close[j + 1:k2 + 1])) > float(close[j]) * (1.0 - cpct):
            continue                       # 拐点后未出现有效下行 → 丢弃
        mm = i - j
        if abs(mm) > W:
            continue
        sell.append({
            "date": dates[i], "rev_date": dates[j], "m": int(mm),
            "price": round(float(close[i]), 2),
            "rev_price": round(float(high[j]), 2),
            "text": (f"9Rev 提前{mm}{unit}开始下行" if mm >= 0
                     else f"9Rev 信号后{-mm}{unit}开始下行"),
        })

    return {"buy": buy, "sell": sell, "pending_buy": pend_b, "pending_sell": pend_s}


def _period_row(it: dict, period: str) -> dict:
    """把一个周期候选压缩成前端分周期展示用的一行（含单位，避免量纲混淆）。"""
    pdef = PERIODS.get(period) or {}
    return {
        "period": period,
        "label": pdef.get("label", period),
        "unit": pdef.get("unit", "天"),
        "bar_date": it.get("bar_date"),
        "cur_count": it.get("cur_count"),
        "remain": it.get("remain"),
        "triggered": it.get("triggered"),
        "close": it.get("close"),
        "bars_hist": it.get("bars_hist"),
        "buy_n": it.get("buy_n"),
        "sell_n": it.get("sell_n"),
        "n_pos": it.get("n_pos"),
        "n_lag": it.get("n_lag"),
        "n_mean": it.get("n_mean"),
        "n_min": it.get("n_min"),
        "n_max": it.get("n_max"),
        "m_pos": it.get("m_pos"),
        "m_neg": it.get("m_neg"),
        "sell_1y": it.get("sell_1y"),
        "n_rank": it.get("n_rank"),
        "note": it.get("note"),
        "buy_history": it.get("buy_history") or [],
        "sell_history": it.get("sell_history") or [],
    }


# ================================================================ 主入口
def _clamp_cfg(c: dict) -> dict:
    """参数钳制（对齐各字段的合法区间）。"""
    c["near_remaining"] = int(max(1, min(8, int(c["near_remaining"]))))
    c["ref_gap"] = int(max(1, min(10, int(c["ref_gap"]))))
    c["match_window"] = int(max(1, min(10, int(c["match_window"]))))
    c["confirm_bars"] = int(max(1, min(60, int(c["confirm_bars"]))))
    c["confirm_pct"] = float(max(0.0, min(50.0, float(c["confirm_pct"]))))
    c["hist_years"] = float(max(0.5, min(25.0, float(c["hist_years"]))))
    c["show_records"] = int(max(3, min(200, int(c["show_records"]))))
    c["max_results"] = int(max(1, min(3000, int(c["max_results"]))))
    c["shrink_k"] = float(max(0.0, min(50.0, float(c.get("shrink_k", 3.0)))))
    return c


def _norm_periods(periods) -> list:
    """规整周期列表：只保留受支持的、去重、按固定顺序。空 → 退回 ["day"]。"""
    if isinstance(periods, str):
        periods = [periods]
    out: list = []
    for p in (periods or []):
        p = str(p).strip().lower()
        if p in PERIODS and p not in out:
            out.append(p)
    if not out:
        out = ["day"]
    return [p for p in PERIOD_ORDER if p in out]


def _per_period_stats(cands: list, cut_1y: str, show_records: int) -> tuple:
    """给**同一周期**的候选做统计汇总（n 样本、滞后数、近一年卖出数…）。

    返回 (总买入样本n≥0数, 滞后数, 卖出样本总数, 真顶在信号之后数)。
    只统计该周期自己的样本，绝不与其他周期相加 —— 量纲不同，相加无意义。
    """
    tot_n = tot_lag = tot_s = tot_mneg = 0
    for it in cands:
        tot_n += it.get("n_pos") or 0
        tot_lag += it.get("n_lag") or 0
        tot_s += it.get("sell_n") or 0
        tot_mneg += it.get("m_neg") or 0
    return tot_n, tot_lag, tot_s, tot_mneg


def run(cfg: Optional[dict] = None, exchange: Optional[str] = None,
        sectors: Optional[list] = None, progress_cb=None) -> dict:
    """9Reverse9 全流程（多周期）：逐周期独立筛 → 各自统计 → 合并 → 排序。

    **核心约束**：几个周期叠加时，必须「先分别筛选，再整体合并」，且每个周期用
    自己的 cfg 副本（见 `_period_cfg`），避免用日线的「还差 2 日」去限制月线九转。
    合并按 code 取并集；同一只股票命中多个周期时，各周期的 n/m 记录**分别保留**
    （它们单位不同，不能混算），主排序键取各周期中**最优（最小）**的排序分。
    """
    c = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if v is not None:
            c[k] = v
    c = _clamp_cfg(c)
    periods = _norm_periods(c.get("periods"))

    t0 = time.time()
    rconn = db.reader()

    # ---- ① 准备一份共享的「全市场元信息 + 日K」，供所有周期复用（只查一次库）----
    where, args = "", []
    if exchange in ("SZ", "SH", "BJ"):
        where = " WHERE exchange=?"
        args.append(exchange)
    if sectors:
        where += (" AND " if where else " WHERE ") + \
            f"sector IN ({','.join('?' * len(sectors))})"
        args.extend(sectors)
    rows = rconn.execute(
        f"SELECT code, name, sector, exchange, listing_date FROM meta{where}",
        tuple(args)).fetchall()
    if not rows:
        return {"results": [], "skip_stats": {}, "ref_date": None, "cut_1y": "",
                "stats": {}, "params": {}, "candidates": 0, "elapsed": 0.0,
                "periods": periods}

    # 日线窗口 = 各周期需求的最大值（月线要 36 个月 ≈ 750 个交易日）
    need_days = max(RECENT_WINDOW,
                    max(PERIODS[p]["recent_bars"] * int(PERIODS[p]["factor"])
                        for p in periods))
    # 程序二还要看 hist_years 年的历史，日线取数窗口取二者较大者，
    # 这样周/月线重采样后有足够 bar 供「历史」统计。
    hist_days = int(c["hist_years"] * 250)
    bars_map = batchload.load_daily_map(window_days=max(need_days, min(hist_days, 3000)))
    if not bars_map:
        return {"results": [], "skip_stats": {"无行情数据": len(rows)}, "ref_date": None,
                "cut_1y": "", "stats": {}, "params": {}, "candidates": 0,
                "elapsed": round(time.time() - t0, 2), "periods": periods}

    present = [str(r[0]) for r in rows
               if str(r[0]) in bars_map and not bars_map[str(r[0])].empty]
    ref_date = _coverage_ref_date(present, bars_map)
    if not ref_date:
        return {"results": [], "skip_stats": {"无行情数据": len(rows)}, "ref_date": None,
                "cut_1y": "", "stats": {}, "params": {}, "candidates": 0,
                "elapsed": round(time.time() - t0, 2), "periods": periods}
    try:
        cut_1y = (datetime.strptime(ref_date, "%Y-%m-%d") - timedelta(days=365)
                  ).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        cut_1y = ""

    # ---- ② 逐周期独立筛选 + 独立统计（每周期一份 cfg 副本，单位互不干扰）----
    per_cands: dict = {}
    skip_stats: dict = {}
    period_meta: dict = {}
    for p in periods:
        pdef = PERIODS[p]
        cp = _period_cfg(c, p)                       # ← 独立 cfg 副本（关键）
        cp = _clamp_cfg(cp)
        if progress_cb:
            progress_cb(0, max(len(rows), 1),
                        f"程序一 · {pdef['label']}：开始筛选（还差 ≤ "
                        f"{cp['near_remaining']} 个{pdef['unit']}）")
        cs, sk, _rd = screen(cp, exchange, sectors, progress_cb, period=p,
                             bars_map=bars_map, ref_date=ref_date, rows=rows)
        # 跳过统计按周期分区展示（各周期数据量级不同，混在一起看不清）
        for k, v in sk.items():
            skip_stats[f"[{pdef['label']}]{k}"] = v

        hist_bars = period_bars(cp["hist_years"], p)
        # ⚠ _load_history 的 limit 是**日线行数**（hist.db / unified daily 都只有日线），
        # 而 hist_bars 是「该周期的 bar 数」。周线要 250 根周 bar 就得取 250×5 个交易日，
        # 月线要 59 根月 bar 就得取 59×21 个交易日；直接拿 hist_bars 当日线行数用，
        # 月线只能拿到 3 个月数据，会被 min_hist 全部剔除（analyzed=0）。
        day_rows = int(hist_bars * int(pdef["factor"]))
        min_hist = int(pdef["min_hist"])
        if progress_cb:
            progress_cb(1, 1, f"程序二 · {pdef['label']}：候选 {len(cs)} 只，拉取历史…")

        total = len(cs)
        for done, it in enumerate(cs, 1):
            if progress_cb and (done % 10 == 0 or done == total):
                progress_cb(done, max(total, 1),
                            f"程序二 · {pdef['label']} 历史九转统计 {done}/{total}")
            it["period"] = p
            it["period_label"] = pdef["label"]
            it["unit"] = pdef["unit"]
            try:
                dates, o, h, l, cl = _load_history(rconn, it["code"], day_rows, ref_date)
                if p != "day" and dates:
                    dates, o, h, l, cl = resample_ohlc(dates, o, h, l, cl, p)
                if len(dates) < min_hist:
                    _blank_hist(it, len(dates), f"历史不足（{pdef['label']}）")
                    continue
                res = _analyze(dates, h, l, cl, cp, period=p)
                _fill_hist(it, res, dates, cut_1y, cp["show_records"])
            except Exception as e:  # noqa: BLE001 —— 单只失败不影响整体
                _blank_hist(it, 0, f"异常:{type(e).__name__}:{e}")
        per_cands[p] = cs
        n_hit = sum(1 for x in cs if not x.get("note"))
        period_meta[p] = {
            "label": pdef["label"], "unit": pdef["unit"],
            "near_remaining": cp["near_remaining"], "match_window": cp["match_window"],
            "confirm_bars": cp["confirm_bars"], "ref_gap": cp["ref_gap"],
            "hist_years": cp["hist_years"], "hist_bars": hist_bars,
            "candidates": len(cs), "analyzed": n_hit,
        }

    # ---- ③ 逐周期算「收缩后的排序分」（必须在合并之前算，因为 prior 是该周期自己的）----
    # 排序依据（用户口径，逐级生效）：
    #   1st 历史买入九转 n 越小越靠前 → 各命中周期排序分的**最小值**
    #        （收缩公式与单周期版本一致，但**逐周期各算各的 prior**：
    #         「日线的平均 n」与「月线的平均 n」量纲不同，绝不能合并求均值）
    #   2nd 一年内卖出九转越多越靠前 → 各周期之和（计数无量纲，可加）
    #   3rd 代码升序（稳定收尾）
    K = float(c["shrink_k"])
    prior_by: dict = {}          # 每个周期自己的全市场 n 均值（收缩目标）
    thin_by: dict = {}
    for p in periods:
        cs = per_cands.get(p) or []
        ts = sum(x.get("n_pos_sum") or 0 for x in cs)
        tc = sum(x.get("n_pos") or 0 for x in cs)
        prior_by[p] = round(float(ts) / tc, 4) if tc else 0.0
        thin_by[p] = sum(1 for x in cs if (x.get("n_pos") or 0) == 1)
        for x in cs:
            m = x.get("n_pos") or 0
            if not m:
                x["n_rank"] = None                      # 没有任何 n≥0 样本 → 垫底
            elif K > 0:
                x["n_rank"] = round((float(x.get("n_pos_sum") or 0)
                                     + K * prior_by[p]) / (m + K), 4)
            else:
                x["n_rank"] = x.get("n_mean")

    # ---- ④ 整体合并（并集）：按 code 汇集各周期命中信息 ----
    # 合并规则：以**日线优先**的顺序把各周期字段摊到顶层（保证老前端/CSV 仍能读到
    # bars_hist / buy_n / n_mean 等字段），同时把每个周期的完整记录放进 `by_period`
    # 与 `period_rows`（含单位），供前端分周期展示明细。**不同周期的 n 绝不合并求均值**。
    TOP_FIELDS = ("cur_count", "remain", "triggered", "bar_date", "close",
                  "bars_hist", "buy_n", "sell_n", "pending_buy", "pending_sell",
                  "n_pos", "n_lag", "n_pos_sum", "n_mean", "n_min", "n_max",
                  "m_pos", "m_neg", "sell_1y", "note")
    merged: dict = {}
    for p in periods:
        for it in per_cands.get(p) or []:
            code = it["code"]
            slot = merged.get(code)
            if slot is None:
                slot = {
                    "code": code, "name": it["name"], "sector": it["sector"],
                    "exchange": it["exchange"], "close": it["close"],
                    "periods": [], "by_period": {}, "period_rows": [],
                }
                merged[code] = slot
            slot["periods"].append(p)
            slot["by_period"][p] = it
            slot["period_rows"].append(_period_row(it, p))
            # 顶层字段按「日线优先」覆盖（周/月线只在没有日线时才填）
            if p == "day" or "day" not in slot["by_period"]:
                for f in TOP_FIELDS:
                    slot[f] = it.get(f)
                slot["close"] = it["close"]
                slot["period"] = p
                slot["period_label"] = PERIODS[p]["label"]
                slot["unit"] = PERIODS[p]["unit"]
                slot["buy_history"] = it.get("buy_history") or []
                slot["sell_history"] = it.get("sell_history") or []
            elif "day" not in slot["periods"]:
                # 无日线命中时，第一顺位周期（week → month）填顶层
                if slot["periods"][0] == p:
                    for f in TOP_FIELDS:
                        slot[f] = it.get(f)
                    slot["period"] = p
                    slot["period_label"] = PERIODS[p]["label"]
                    slot["unit"] = PERIODS[p]["unit"]
                    slot["buy_history"] = it.get("buy_history") or []
                    slot["sell_history"] = it.get("sell_history") or []

    # ---- ⑤ 合并各周期排序分，得到顶层排序键 ----
    INF = 9e9
    for slot in merged.values():
        ranks = [slot["by_period"][p].get("n_rank") for p in slot["periods"]
                 if slot["by_period"][p].get("n_rank") is not None]
        slot["n_rank"] = min(ranks) if ranks else None
        slot["best_period"] = None
        if ranks:
            best = min(ranks)
            for p in slot["periods"]:
                if slot["by_period"][p].get("n_rank") == best:
                    slot["best_period"] = p
                    break
        slot["sell_1y"] = sum(slot["by_period"][p].get("sell_1y") or 0
                              for p in slot["periods"])
        slot["sell_n"] = sum(slot["by_period"][p].get("sell_n") or 0
                             for p in slot["periods"])
        # 命中周期标签串（供前端一行展示）
        slot["period_labels"] = [PERIODS[p]["label"] for p in slot["periods"]]

    cands = sorted(merged.values(), key=lambda r: (
        r.get("n_rank") if r.get("n_rank") is not None else INF,
        -(r.get("sell_1y") or 0),
        r["code"],
    ))
    for i, r in enumerate(cands, 1):
        r["rank"] = i
        # 状态文案：单周期沿用原文案；多周期叠加时逐周期罗列（单位各自标注）
        parts = []
        for p in r["periods"]:
            it = r["by_period"][p]
            lb, un = PERIODS[p]["label"], PERIODS[p]["unit"]
            if it.get("triggered"):
                parts.append(f"{lb}已达成买入九转（第9个{un}）")
            else:
                parts.append(f"{lb}即将 · 还差{it.get('remain', 0)}{un}"
                             f"（{it.get('cur_count', 0)}/9）")
        r["status"] = " ｜ ".join(parts)

    results = cands[:c["max_results"]]

    # ---- ⑤ 汇总统计（逐周期分开报，不跨周期相加）----
    pstats: dict = {}
    for p in periods:
        cs = per_cands.get(p) or []
        tn, tl, ts, tm = _per_period_stats(cs, cut_1y, c["show_records"])
        pstats[p] = {
            "label": PERIODS[p]["label"], "unit": PERIODS[p]["unit"],
            "candidates": len(cs),
            "analyzable": sum(1 for x in cs if not x.get("note")),
            "buy_total": tn + tl,
            "buy_lag": tl,
            "buy_lag_pct": round(100.0 * tl / (tn + tl), 1) if (tn + tl) else None,
            "n_prior": prior_by.get(p, 0.0),
            "thin": thin_by.get(p, 0),
            "sell_total": ts,
            "sell_top_after": tm,
            "sell_top_after_pct": round(100.0 * tm / ts, 1) if ts else None,
        }

    return {
        "results": results,
        "skip_stats": skip_stats,
        "ref_date": ref_date,
        "cut_1y": cut_1y,
        "periods": periods,
        "period_meta": period_meta,
        "period_stats": pstats,
        "stats": {
            # 顶层 stats 保留「日线优先」的兼容字段（老前端/CSV 直接读）
            **(pstats.get("day") or (pstats[periods[0]] if periods else {})),
            "shrink_k": K,
            "by_period": pstats,
        },
        "params": {
            "periods": periods,
            "near_remaining": c["near_remaining"], "ref_gap": c["ref_gap"],
            "match_window": c["match_window"], "confirm_pct": c["confirm_pct"],
            "confirm_bars": c["confirm_bars"], "hist_years": c["hist_years"],
            "include_triggered": bool(c["include_triggered"]),
            "exclude_st": bool(c["exclude_st"]),
            "shrink_k": c["shrink_k"],
            "min_price": c["min_price"], "max_price": c["max_price"],
            "min_amt20_yi": c["min_amt20_yi"], "min_listed_days": c["min_listed_days"],
        },
        "candidates": len(cands),
        "elapsed": round(time.time() - t0, 2),
    }


def _blank_hist(it: dict, bars: int, note: str) -> None:
    """把一只股票的历史统计字段清空（历史不足 / 异常时用）。"""
    it["buy_history"] = []
    it["sell_history"] = []
    it["buy_n"] = it["sell_n"] = it["sell_1y"] = 0
    it["n_pos"] = it["n_lag"] = it["m_pos"] = it["m_neg"] = 0
    it["n_pos_sum"] = 0
    it["n_mean"] = it["n_min"] = it["n_max"] = None
    it["pending_buy"] = it["pending_sell"] = 0
    it["bars_hist"] = bars
    it["note"] = note


def _fill_hist(it: dict, res: dict, dates: list, cut_1y: str,
               show_records: int) -> None:
    """把 `_analyze` 的结果落到候选 dict 上（含排序所需的中间量）。"""
    buy = res["buy"]
    sell = res["sell"]
    it["bars_hist"] = len(dates)
    it["buy_n"] = len(buy)
    it["sell_n"] = len(sell)
    it["pending_buy"] = res["pending_buy"]
    it["pending_sell"] = res["pending_sell"]
    # 排序主键只用 n ≥ 0 的样本：n < 0 表示信号发出时反弹已经开始（滞后信号），
    # 若混进均值，就会出现「信号越滞后、平均 n 越小、排得越前」的反直觉结果。
    pos_n = [b["n"] for b in buy if b["n"] >= 0]
    it["n_pos"] = len(pos_n)
    it["n_lag"] = len(buy) - len(pos_n)
    it["n_pos_sum"] = int(sum(pos_n))
    if pos_n:
        it["n_mean"] = round(float(np.mean(pos_n)), 2)
        it["n_min"] = int(min(pos_n))
        it["n_max"] = int(max(pos_n))
    else:
        it["n_mean"] = it["n_min"] = it["n_max"] = None
    it["m_pos"] = sum(1 for s in sell if s["m"] > 0)
    it["m_neg"] = sum(1 for s in sell if s["m"] < 0)
    it["sell_1y"] = sum(1 for s in sell if cut_1y and s["date"] >= cut_1y)
    # 明细按时间倒序，仅回传最近 show_records 条（统计仍基于全量）
    it["buy_history"] = list(reversed(buy))[:show_records]
    it["sell_history"] = list(reversed(sell))[:show_records]
    it["note"] = None


def meta_info() -> dict:
    """默认参数与说明（前端渲染用）。"""
    return {
        "defaults": dict(DEFAULTS),
        "doc": list(DOC_LINES),
        "main_len": MAIN_LEN,
        "recent_window": RECENT_WINDOW,
        "periods": [
            {"key": p, "label": PERIODS[p]["label"], "unit": PERIODS[p]["unit"],
             "factor": PERIODS[p]["factor"], "hist_min": PERIODS[p]["min_hist"],
             "recent_bars": PERIODS[p]["recent_bars"]}
            for p in PERIOD_ORDER
        ],
        "period_order": list(PERIOD_ORDER),
    }
