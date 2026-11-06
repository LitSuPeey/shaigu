# -*- coding: utf-8 -*-
"""期权链与隐含波动率曲面（Index Option Surface）。

为什么单独一个模块
------------------
期货页签原来只有 IF/IH/IC/IM 的基差，缺「波动率」这条腿。期权不是期货的附属品：
它定价的是**分布**（尾部风险、偏斜、期限溢价），而期货只定价**中枢**。
两者合起来才看得到「市场怕什么」。

数据源（2026-11-06 实测确定）
------------------------------
· 中金所股指期权 IO（沪深300）/ HO（上证50）/ MO（中证1000）：
  官方日行情 CSV `cffex.com.cn/sj/hqsj/rtj/{YYYYMM}/{DD}/{YYYYMMDD}_1.csv`，
  ★ 同文件里同时含 IF/IH/IC/IM/T 期货行情 → 可拿真实合约代码与同期期货价。
  ★ 官方只给 Delta（不给 IV），所以 IV 必须自己反解 —— 这也正好可以用
    「反解出的 IV 回代算 Delta，与官方 Delta 比对」来验证反解是否正确。
  ★ 该文件是**日频**：盘中访问当日 URL 是 404，只能拿到上一交易日 → 页面必须标 T-1。
· 沪市 300ETF 期权（510300）：akshare `option_finance_board`（东财口径），
  实时性好（当天 16:29 就有收盘价），但只有价格、无希腊字母，IV 同样自己反解。

IV 口径（这里是「一两行算不对」的地方，逐条说明）
--------------------------------------------------
1) **远期价格不能用期货价硬套**。期货有基差（IC/IM 常年贴水 1%+），
   拿期货当 F 会把贴水算进 IV，系统性低估波动率。
   正确做法是先从期权自身用 put-call parity 反推 F：
       C − P = e^(−rT)·(F − K)   ⇒   F = K + e^(rT)·(C − P)
   对每个行权价各算一个 F，取中位数（剔除深度实值/虚值的流动性噪声）。
   这样 F 与 IV 自洽，且 F_opt 与 F_future 之差本身就是一条信息（隐含基差）。
2) **r 用固定 1.5%**（1 年期国债附近）。T < 1 年时折现因子对 r 极不敏感，
   但必须显式写出来，不能假装"无风险利率"是个已知常数。
3) **价格用结算价而非收盘价**：中金所官方 Delta 是基于结算价算的，
   用收盘价反解会与官方 Delta 对不上（收盘价常受尾盘一两笔影响）。
4) **到期日不是"月末"**：中金所股指期权 = 到期月第三个星期五，
   上交所 ETF 期权 = 到期月第四个星期三，遇法定节假日顺延。
   T 用 (到期日 − 今日) / 365（ACT/365），不是"剩 30 天"。
5) **只取虚值端建曲面**：实值期权流动性差、价格对 IV 不敏感（vega 小），
   同到期月里 call 取 K ≥ F 的、put 取 K ≤ F 的，避免 ITM 噪声污染偏斜。
6) 25Δ 偏斜在 **delta 空间**插值，不在行权价空间插值 ——
   行权价空间等距插值会把「深度虚值那一堆 K」的密度差异带进偏斜。

★ 反未来函数：本模块只做当日横截面 + 历史 IV 分位（分位用滚动窗口），
  不把当日 IV 回填到历史。
★ 不合成单一结论：ATM IV / 偏斜 / 期限结构 三者并列，各自给读数，
  绝不压成一个「恐惑指数」（VIX 是那个东西，这里不重复造）。
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np

from .idxhub import (_cache_file, _cache_read, _cache_read_any, _cache_write,
                     _http, _is_trade_hours, _f)

# ============ 品种定义 ============
# kind: cffex（中金所股指期权）/ etf（上交所 ETF 期权）
PRODUCTS: list = [
    {"key": "io", "name": "沪深300 股指期权", "short": "IO", "kind": "cffex",
     "pre": "IO", "future": "IF", "under_idx": "sh000300", "mult": 100,
     "board": "中金所", "hero": 1,
     "desc": "沪深300 的波动率定价，机构对冲盘最集中的期权市场"},
    {"key": "ho", "name": "上证50 股指期权", "short": "HO", "kind": "cffex",
     "pre": "HO", "future": "IH", "under_idx": "sh000016", "mult": 100,
     "board": "中金所",
     "desc": "大盘蓝筹的波动率，与 IO 的价差反映风格分歧"},
    {"key": "mo", "name": "中证1000 股指期权", "short": "MO", "kind": "cffex",
     "pre": "MO", "future": "IM", "under_idx": "sh000852", "mult": 100,
     "board": "中金所",
     "desc": "小盘波动率，贴水结构下最容易被拿来买保护"},
    {"key": "etf300", "name": "沪市 300ETF 期权", "short": "300ETF", "kind": "etf",
     "ak_name": "华泰柏瑞沪深300ETF期权", "code": "510300",
     "under_idx": "sh510300", "mult": 10000, "board": "上交所",
     "desc": "场内 ETF 期权，散户与做市商同场，偏斜常比股指期权更陡"},
]
PMAP = {p["key"]: p for p in PRODUCTS}

RISK_FREE = 0.015          # 无风险利率（固定，显式披露）
_TTL_CHAIN = 120           # 当日链（盘中）/ 落盘后盘后 6 小时
_TTL_CAL = 7 * 86400       # 交易日历

_LOCK = threading.RLock()
_CHAIN_CACHE: dict = {}     # {product_key: (ts, data)}
_CAL_CACHE: dict = {"ts": 0.0, "set": None}


# ============ 基础数学（Black-76，期货/远期期权）============
_SQRT2 = math.sqrt(2.0)


def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT2)


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def b76_price(F: float, K: float, T: float, r: float, sig: float, is_call: bool) -> float:
    """Black-76 理论价（以远期 F 为标的，折现 e^{-rT}）。"""
    if T <= 0 or sig <= 0:
        return max(0.0, (F - K) if is_call else (K - F))
    sT = sig * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sT * sT) / sT
    d2 = d1 - sT
    df = math.exp(-r * T)
    if is_call:
        return df * (F * _ncdf(d1) - K * _ncdf(d2))
    return df * (K * _ncdf(-d2) - F * _ncdf(-d1))


def b76_delta(F: float, K: float, T: float, r: float, sig: float, is_call: bool) -> float:
    """远期口径 Delta（∂V/∂F，折现后）。看涨 ∈ (0,1)，看跌 ∈ (−1,0)。"""
    if T <= 0 or sig <= 0:
        if is_call:
            return math.exp(-r * max(T, 0)) * (1.0 if F > K else 0.0)
        return math.exp(-r * max(T, 0)) * (-1.0 if F < K else 0.0)
    sT = sig * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sT * sT) / sT
    return math.exp(-r * T) * (_ncdf(d1) if is_call else _ncdf(d1) - 1.0)


def implied_vol(price: float, F: float, K: float, T: float, r: float,
                is_call: bool) -> Optional[float]:
    """二分法反解隐含波动率，返回年化 σ（如 0.185 = 18.5%）。

    ★ 先在价格上界外做一次夹逼：深度虚值期权价格极低时，1e-4 的初始下界
      已经高于理论价，直接二分会返回边界值（假成功），这里显式判掉。
    """
    if not (price > 0 and F > 0 and K > 0 and T > 0):
        return None
    df = math.exp(-r * T)
    intrinsic = df * max(0.0, (F - K) if is_call else (K - F))
    if price <= intrinsic + 1e-9:
        return None
    lo, hi = 1e-4, 5.0
    if b76_price(F, K, T, r, lo, is_call) > price:
        return None                      # 连 0.01% 波动率都撑不起这个价 → 数据异常
    if b76_price(F, K, T, r, hi, is_call) < price:
        return None                      # 价格高于 500% 波动率 → 数据异常
    for _ in range(90):
        mid = 0.5 * (lo + hi)
        if b76_price(F, K, T, r, mid, is_call) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


# ============ 交易日历与到期日 ============
def _trade_days() -> Optional[set]:
    """A 股交易日集合（'YYYY-MM-DD'）。拿不到返回 None，退化为「跳周末」。"""
    now = time.time()
    with _LOCK:
        if _CAL_CACHE["set"] is not None and now - _CAL_CACHE["ts"] < _TTL_CAL:
            return _CAL_CACHE["set"]
    cached = _cache_read("trade_cal.json", _TTL_CAL)
    if cached:
        s = set(cached)
        with _LOCK:
            _CAL_CACHE["ts"], _CAL_CACHE["set"] = now, s
        return s
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        s = {str(x)[:10] for x in df["trade_date"].tolist()}
        if s:
            _cache_write("trade_cal.json", sorted(s))
            with _LOCK:
                _CAL_CACHE["ts"], _CAL_CACHE["set"] = now, s
            return s
    except Exception:  # noqa: BLE001 —— 日历拿不到就退化为工作日近似
        pass
    return None


def _cal_bounds(cal: Optional[set]) -> Optional[str]:
    """日历覆盖的最后一天。★ 新浪交易日历只到「今天」，未来日期不在集合里，
    若直接 `d in cal` 判断未来到期日会一直判 False → 无限顺延（实测把
    2027-01-15 推成 2027-01-30）。所以必须区分「日历覆盖内」与「覆盖外」。"""
    return max(cal) if cal else None


def _nth_weekday(year: int, month: int, weekday: int, n: int, cal: Optional[set]) -> _dt.date:
    """某月第 n 个星期 weekday（0=周一），遇非交易日顺延。"""
    d = _dt.date(year, month, 1)
    d += _dt.timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
    last = _cal_bounds(cal)
    for _ in range(15):
        if cal and last:
            if d.isoformat() > last:
                # 超出日历覆盖 → 只能跳周末（未来法定节假日未纳入，见 caveats）
                if d.weekday() < 5:
                    return d
            elif d.isoformat() in cal:
                return d
        else:
            if d.weekday() < 5:
                return d
        d += _dt.timedelta(days=1)
    return d


def expiry_date(kind: str, yymm: str, cal: Optional[set] = None) -> Optional[_dt.date]:
    """到期日：中金所股指期权 = 第三个星期五；上交所 ETF 期权 = 第四个星期三。"""
    try:
        y, m = 2000 + int(yymm[:2]), int(yymm[2:4])
        if not (1 <= m <= 12):
            return None
        if kind == "etf":
            return _nth_weekday(y, m, 2, 4, cal)     # 周三=2，第 4 个
        return _nth_weekday(y, m, 4, 3, cal)         # 周五=4，第 3 个
    except Exception:  # noqa: BLE001
        return None


def _ttm(exp: Optional[_dt.date], today: Optional[_dt.date] = None) -> Optional[float]:
    """剩余年限 ACT/365。到期日当天返回 1/365（不能返回 0，否则 IV 无解）。"""
    if not exp:
        return None
    today = today or _dt.date.today()
    days = (exp - today).days
    if days < 0:
        return None
    return max(days, 1) / 365.0


# ============ 中金所日行情 ============
_CFFEX_URL = "http://www.cffex.com.cn/sj/hqsj/rtj/%s/%s/%s_1.csv"
_CFFEX_HEAD = "合约代码"


def _cffex_fetch(date: _dt.date) -> Optional[str]:
    """拉某交易日的中金所日行情 CSV（gbk）。当日无数据返回 None。"""
    ymd = date.strftime("%Y%m%d")
    url = _CFFEX_URL % (ymd[:6], ymd[6:8], ymd)
    try:
        txt = _http(url, headers={"Referer": "http://www.cffex.com.cn/"}, timeout=20,
                    enc="gbk")
    except Exception:  # noqa: BLE001
        return None
    if not txt or _CFFEX_HEAD not in txt:
        return None
    return txt


def _num(x) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


_OPT_CODE = re.compile(r"^([A-Z]+)(\d{4})[-_]?([CP])[-_]?(\d+(?:\.\d+)?)$")
_FUT_CODE = re.compile(r"^([A-Z]+)(\d{4})$")


def _parse_cffex(txt: str, asof: str) -> dict:
    """解析中金所日行情 → {futures:{IF:[...]}, options:{IO:{2611:[rows]}}}。

    字段：合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,
          今收盘,今结算,前结算,涨跌1,涨跌2,Delta
    """
    fut: dict = {}
    opt: dict = {}
    for line in (txt or "").splitlines():
        line = line.strip()
        if not line or line.startswith(_CFFEX_HEAD) or line.startswith("小计") \
                or line.startswith("合计"):
            continue
        p = line.split(",")
        if len(p) < 14:
            continue
        code = p[0].strip().upper()
        mo = _OPT_CODE.match(code)
        if mo:
            pre, yymm, cp, strike = mo.group(1), mo.group(2), mo.group(3), mo.group(4)
            rec = {"code": code, "K": _num(strike),
                   "close": _num(p[8]), "settle": _num(p[9]),
                   "vol": _num(p[4]) or 0.0, "oi": _num(p[6]) or 0.0,
                   "delta": _num(p[13])}
            if rec["K"] is None:
                continue
            opt.setdefault(pre, {}).setdefault(yymm, []).append(rec)
            continue
        mf = _FUT_CODE.match(code)
        if mf:
            pre, yymm = mf.group(1), mf.group(2)
            fut.setdefault(pre, []).append({
                "code": code, "yymm": yymm,
                "close": _num(p[8]), "settle": _num(p[9]),
                "vol": _num(p[4]) or 0.0, "oi": _num(p[6]) or 0.0,
            })
    return {"asof": asof, "futures": fut, "options": opt}


_CFFEX_MEM: dict = {"ts": 0.0, "data": None}


def cffex_day(force: bool = False) -> dict:
    """最近一个可用的中金所交易日数据（带落盘缓存）。

    ★ 中金所日行情是**日频**：盘中没有当日文件（404），只能回溯到上一交易日。
      因此返回值里的 asof 可能不是今天 —— 调用方必须原样展示，不许改写成今天。
    """
    now = time.time()
    with _LOCK:
        if not force and _CFFEX_MEM["data"] and now - _CFFEX_MEM["ts"] < (600 if _is_trade_hours() else 3600):
            return _CFFEX_MEM["data"]

    today = _dt.date.today()
    for back in range(0, 12):
        d = today - _dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        name = "cffex_%s.json" % d.strftime("%Y%m%d")
        cache = _cache_read_any(name) if force else _cache_read(
            name, 300 if _is_trade_hours() else 6 * 3600)
        if cache:
            return cache
        txt = _cffex_fetch(d)
        if not txt:
            continue
        data = _parse_cffex(txt, d.isoformat())
        if not data["options"]:
            continue
        _cache_write(name, data)
        with _LOCK:
            _CFFEX_MEM["ts"], _CFFEX_MEM["data"] = now, data
        return data

    fallback = {k: v for k, v in _CFFEX_MEM.items()}
    return fallback.get("data") or {}


# ============ 沪市 ETF 期权（akshare）============
def _etf_months(n: int = 4) -> list:
    """未来 n 个自然月（含当月）的 yymm，按 ETF 期权实际挂牌月份过滤。"""
    today = _dt.date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(n + 2):
        out.append("%02d%02d" % (y % 100, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out[: n + 2]


def etf_chain(product: dict, force: bool = False) -> dict:
    """拉 ETF 期权某品种全部挂牌月份的 T 型报价。"""
    key = product["key"]
    now = time.time()
    with _LOCK:
        c = _CHAIN_CACHE.get(key)
        if not force and c and now - c[0] < (_TTL_CHAIN if _is_trade_hours() else 1800):
            return c[1]
    name = "etf_%s.json" % key
    if not force:
        cached = _cache_read(name, _TTL_CHAIN if _is_trade_hours() else 1800)
        if cached:
            with _LOCK:
                _CHAIN_CACHE[key] = (now, cached)
            return cached

    months: dict = {}
    try:
        import akshare as ak
    except Exception:  # noqa: BLE001
        return {}
    for yymm in _etf_months():
        try:
            df = ak.option_finance_board(symbol=product["ak_name"], end_month=yymm)
        except Exception:  # noqa: BLE001 —— 未挂牌月份直接跳过
            continue
        if df is None or getattr(df, "empty", True):
            continue
        rows = []
        for _, r in df.iterrows():
            code = str(r.get("合约交易代码") or "")
            m = re.match(r"^(\d{6})([CP])(\d{4})M(\d+)$", code)
            if not m:
                continue
            rows.append({"code": code, "cp": m.group(2),
                         "K": _num(r.get("行权价")),
                         "close": _num(r.get("当前价")),
                         "prev": _num(r.get("前结价")),
                         "pct": _num(r.get("涨跌幅")),
                         "vol": None, "oi": None, "delta": None})
        if rows:
            months[yymm] = rows
    data = {"asof": _dt.date.today().isoformat(), "months": months,
            "source": "东方财富期权行情（akshare option_finance_board）"}
    # ★ 空结果绝不落盘：网络抖一次就缓存半小时「不可用」，比不缓存更糟。
    #   只有真拿到合约才写；失败的下次调用会重新试。
    if months:
        _cache_write(name, data)
    with _LOCK:
        _CHAIN_CACHE[key] = (now, data)
    return data


# ============ 盘中实时快照（中金所 quote_*.txt）============
# 日行情 CSV 是**日频**：盘中访问当日文件是 404，只能拿到 T-1。
# 但中金所另有延时行情快照 quote_IO.txt / quote_HO.txt / quote_MO.txt，
# 盘中会持续更新（2026-11-06 实测 200 + 14KB，字段：合约/持仓/成交量/最新价/涨跌/买价/买量/卖价/卖量）。
# 有了它，盘中算的就是**当日**的 IV，而不是把昨天的 IV 挂上「实时」两个字。
_QUOTE_TTL = 30.0
_QUOTE_CACHE: dict = {}


def _is_opt_hours() -> bool:
    """中金所股指期权交易时段：9:30-11:30 / 13:00-15:00。

    ★ 环境变量 IDXOPT_FORCE_LIVE=1 可强制走实时分支 —— 仅用于验收「实时角标」，
      默认关闭，生产不会误开。
    """
    if os.environ.get("IDXOPT_FORCE_LIVE") == "1":
        return True
    now = _dt.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= hm <= (15 * 60 + 5)


def _quote_chain(pre: str) -> dict:
    """实时期权快照 → {合约代码: {px, bid, ask, last, vol, oi}}。失败返回 {}。"""
    now = time.time()
    with _LOCK:
        c = _QUOTE_CACHE.get(pre)
        if c and now - c[0] < _QUOTE_TTL:
            return c[1]
    url = "http://www.cffex.com.cn/quote_%s.txt" % pre
    try:
        txt = _http(url, headers={"Referer": "http://www.cffex.com.cn/"},
                    timeout=8, enc="gbk")
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for line in (txt or "").splitlines():
        parts = line.strip().split(",")
        if len(parts) < 9 or parts[0].strip().lower() == "instrument":
            continue
        code = parts[0].strip().upper()
        last, bp, sp = _num(parts[3]), _num(parts[5]), _num(parts[7])
        # ★ 优先用买卖中价：最后成交价可能是几分钟前的一笔，中价更接近当下的公允价。
        #   但价差过宽（>40%）说明这一档根本没流动性，中价也是假的 → 退回最新价。
        px = None
        if bp and sp and bp > 0 and sp > 0:
            mid = (bp + sp) / 2.0
            if sp > 0 and (sp - bp) / sp <= 0.40:
                px = mid
        if not px or px <= 0:
            px = last
        if not px or px <= 0:
            continue
        out[code] = {"px": px, "bid": bp, "ask": sp, "last": last,
                     "vol": _num(parts[2]) or 0.0, "oi": _num(parts[1]) or 0.0}
    with _LOCK:
        _QUOTE_CACHE[pre] = (now, out)
    return out


def _rows_from_quote(live: dict, pre: str) -> dict:
    """把实时快照整理成与日线 CSV 同构的 {月份: [合约行]}，好复用 _build_slice。"""
    by_m: dict = {}
    for code, q in live.items():
        m = _OPT_CODE.match(code)
        if not m or m.group(1) != pre:
            continue
        yymm, cp = m.group(2), m.group(3)
        K = _num(m.group(4))
        if not K:
            continue
        by_m.setdefault(yymm, []).append({
            "code": code, "K": K, "cp": cp,
            "close": q["px"], "settle": None,
            "vol": q.get("vol"), "oi": q.get("oi"), "delta": None})
    return by_m


# ============ 远期与 IV ============
def _parity_forward(calls: list, puts: list, T: float, r: float) -> Optional[float]:
    """put-call parity 反推远期：F = K + e^{rT}(C − P)，取各行权价的中位数。

    ★ 只保留有价格、且价差不过分大的行权价：深度实值/虚值那几档
      买卖价差相对价格太宽，反推的 F 噪声大。
    """
    cmap = {round(c["K"], 6): c for c in calls if c.get("px")}
    out = []
    for p in puts:
        if not p.get("px"):
            continue
        c = cmap.get(round(p["K"], 6))
        if not c:
            continue
        diff = c["px"] - p["px"]
        # 价差必须落在合理区间：过大的 |diff| 说明行权价远离远期或数据错配
        if abs(diff) > 0.35 * p["K"]:
            continue
        out.append(p["K"] + math.exp(r * T) * diff)
    if len(out) < 3:
        return None
    return float(np.median(out))


def _build_slice(rows: list, yymm: str, kind: str, cal: Optional[set],
                 fut_px: Optional[float], asof: Optional[_dt.date] = None) -> Optional[dict]:
    """把一个到期月的 C/P 报价加工成 IV 微笑切片。

    ★ T 必须从**数据日期**算，不能用 today：中金所盘中只能拿到 T-1 的文件，
      若按 today 算 T 会把剩余期限多减 1 天 → IV 系统性偏高。
    """
    exp = expiry_date(kind, yymm, cal)
    T = _ttm(exp, asof)
    if not T or not rows:
        return None
    # ★ 只能用 _cp_of 判方向：中金所的行没有 cp 字段，
    #   若写成 x.get("cp", "C") == "C"，缺字段会被默认成看涨 → 所有 put 都被当成 call。
    allrows = [dict(x, px=(x.get("settle") if x.get("settle") else x.get("close")))
               for x in rows]
    calls = [x for x in allrows if _cp_of(x) == "C"]
    puts = [x for x in allrows if _cp_of(x) == "P"]
    calls = [c for c in calls if c["px"] and c["px"] > 0]
    puts = [p for p in puts if p["px"] and p["px"] > 0]
    if len(calls) < 3 or len(puts) < 3:
        return None

    F_opt = _parity_forward(calls, puts, T, RISK_FREE)
    if not F_opt or F_opt <= 0:
        return None

    pts = []
    for grp, is_call in ((calls, True), (puts, False)):
        for o in grp:
            px = o["px"]
            K = o["K"]
            if px <= 0 or K <= 0:
                continue
            # ★ 价格太低的合约不算 IV：离最小变动价位只有几个跳动时，
            #   买卖价差能占到价格的 50%，反解出来的 IV 是噪声不是信息
            #   （不过滤时 IO 微笑的右端会被几十个 0.2 元的虚值 call 顶到 46%）。
            if px < F_opt * 5e-4:
                continue
            iv = implied_vol(px, F_opt, K, T, RISK_FREE, is_call)
            if iv is None or not (0.01 <= iv <= 5.0):
                continue
            dl = b76_delta(F_opt, K, T, RISK_FREE, iv, is_call)
            # |Δ| 过小的极端虚值档同理：vega 接近 0，价格动一个跳动 IV 就跳几十个点
            if abs(dl) < 0.02:
                continue
            pts.append({"K": K, "cp": "C" if is_call else "P", "px": px,
                        "iv": iv, "delta": dl,
                        "vol": o.get("vol"), "oi": o.get("oi"),
                        "m": K / F_opt,
                        "off_delta": o.get("delta")})
    if len(pts) < 5:
        return None

    # 曲面只用虚值端：call 取 K ≥ F，put 取 K ≤ F（vega 大、流动性好）
    otm = [p for p in pts if (p["cp"] == "C" and p["K"] >= F_opt)
           or (p["cp"] == "P" and p["K"] <= F_opt)]
    if len(otm) < 4:
        otm = pts

    atm = _atm_iv(pts, F_opt)
    skew = _skew25(pts)
    return {"yymm": yymm, "expiry": exp.isoformat() if exp else None,
            "days": int(round(T * 365)), "T": round(T, 6),
            "fwd_opt": round(F_opt, 4), "fwd_fut": round(fut_px, 4) if fut_px else None,
            "fwd_gap_pct": round((F_opt / fut_px - 1) * 100, 3) if fut_px else None,
            # ★ 存比例、保留 4 位小数：早先存 round(x,2) 会把 21.6% 压成 22.00%，
            #   期限结构看上去像一台阶梯，实际上丢的全是有效信息。
            "atm_iv": round(atm[0], 4) if atm else None,
            "atm_strike": atm[1] if atm else None,
            "skew25": round(skew * 100, 2) if skew is not None else None,
            "points": sorted(otm, key=lambda x: x["K"]),
            "n_points": len(otm)}


def _cp_of(row: dict) -> str:
    """统一取看涨/看跌标记：中金所从合约代码取，ETF 从 cp 字段取。"""
    cp = row.get("cp")
    if cp in ("C", "P"):
        return cp
    code = str(row.get("code") or "")
    if "-C-" in code or "C2" in code or re.search(r"C\d{4}M", code):
        return "C"
    if "-P-" in code or "P2" in code or re.search(r"P\d{4}M", code):
        return "P"
    return "C"


def _atm_iv(pts: list, F: float) -> Optional[tuple]:
    """平值 IV：在 log(K/F) 空间对最近的几档做线性插值（不是取最近的单点）。

    ★ 单点取值会被某一档的报价噪声带偏；插值至少用两侧各一档。
    """
    up = sorted([p for p in pts if p["K"] >= F], key=lambda x: x["K"])
    dn = sorted([p for p in pts if p["K"] < F], key=lambda x: -x["K"])
    if up and abs(up[0]["K"] - F) < 1e-9:
        return up[0]["iv"], up[0]["K"]
    if up and dn:
        a, b = dn[0], up[0]
        la, lb = math.log(a["K"] / F), math.log(b["K"] / F)
        if abs(lb - la) < 1e-9:
            return a["iv"], a["K"]
        w = (0 - la) / (lb - la)
        return a["iv"] + w * (b["iv"] - a["iv"]), a["K"] if w < 0.5 else b["K"]
    one = (up or dn)
    return (one[0]["iv"], one[0]["K"]) if one else None


def _skew25(pts: list) -> Optional[float]:
    """25Δ 偏斜 = IV(25Δ put) − IV(25Δ call)。

    ★ 在 **delta 空间**插值：把每个点的 |delta| 当自变量、IV 当因变量。
      看跌的 delta 是负的，用 |delta| 统一后即可与看涨同轴比较。
      正值 = 看跌更贵（恐慌溢价），负值 = 看涨更贵（追涨情绪）。
    """
    ps = sorted([(abs(p["delta"]), p["iv"]) for p in pts if p["cp"] == "P"], key=lambda x: x[0])
    cs = sorted([(abs(p["delta"]), p["iv"]) for p in pts if p["cp"] == "C"], key=lambda x: x[0])

    def _interp(arr, target):
        if len(arr) < 2:
            return None
        for i in range(len(arr) - 1):
            a, b = arr[i], arr[i + 1]
            if a[0] <= target <= b[0]:
                if abs(b[0] - a[0]) < 1e-9:
                    return a[1]
                w = (target - a[0]) / (b[0] - a[0])
                return a[1] + w * (b[1] - a[1])
        return None

    pv, cv = _interp(ps, 0.25), _interp(cs, 0.25)
    if pv is None or cv is None:
        return None
    return pv - cv


def _cross_check(slice_: dict) -> dict:
    """用反解出的 IV 回代算 Delta，与中金所官方 Delta 比对。

    ★ 这是本模块唯一的「外部真值」校验：如果 F 取错（比如错用期货价），
      反解的 IV 会系统性偏移，回代的 Delta 就会与官方值大面积对不上。
      中位数误差 < 0.02 视为通过；> 0.05 说明口径有问题，必须在页面上标注。
    """
    diffs = [abs(p["delta"] - p["off_delta"]) for p in slice_["points"]
             if p.get("off_delta") is not None]
    if not diffs:
        return {}
    arr = np.array(diffs, dtype=float)
    return {"n": int(len(arr)), "med_abs": round(float(np.median(arr)), 4),
            "p90_abs": round(float(np.percentile(arr, 90)), 4),
            "pass": bool(np.median(arr) < 0.02)}


# ============ 组装 ============
def surface(product_key: str, force: bool = False) -> dict:
    """单品种 IV 曲面：各到期月切片 + 期限结构 + 交叉验证。"""
    p = PMAP.get(product_key)
    if not p:
        return {"ok": False, "err": "未知品种"}
    cal = _trade_days()
    slices = []

    if p["kind"] == "cffex":
        # 盘中优先用实时快照：日线 CSV 当日文件根本不存在（404），只能拿 T-1。
        live = _quote_chain(p["pre"]) if _is_opt_hours() else {}
        if live:
            rows_by_month = _rows_from_quote(live, p["pre"])
            asof_d = _dt.date.today()
            asof_s = asof_d.isoformat()
            for yymm, rows in sorted(rows_by_month.items()):
                # 实时模式下不查 T-1 期货：对照价是昨天的，挂上去反而误导
                s = _build_slice(rows, yymm, "cffex", cal, None, asof_d)
                if s:
                    slices.append(s)
            src = "中金所延时快照（盘中实时价 · 买卖中价 · %s）" % time.strftime("%H:%M")
            asof = asof_s
            fut_info = {"code": None, "settle": None, "vol": None, "live": True}
            data_mode = "live"
        else:
            day = cffex_day(force=force)
            if not day or not day.get("options"):
                return {"ok": False, "err": "中金所行情暂不可用（盘后才有当日文件）",
                        "key": product_key, "name": p["name"]}
            asof_s = day.get("asof", "")
            try:
                asof_d = _dt.date.fromisoformat(asof_s)
            except (TypeError, ValueError):
                asof_d = None
            rows_by_month = day["options"].get(p["pre"], {})
            fut_list = day["futures"].get(p["future"], [])
            # 期货：成交量最大的即主力，用它的结算价做对照（不是主连）
            fut_main = max(fut_list, key=lambda x: x.get("vol") or 0) if fut_list else None
            for yymm, rows in sorted(rows_by_month.items()):
                fut_px = None
                for f in fut_list:
                    if f["yymm"] == yymm:
                        fut_px = f.get("settle") or f.get("close")
                        break
                s = _build_slice(rows, yymm, "cffex", cal, fut_px, asof_d)
                if s:
                    slices.append(s)
            src = "中金所官方日行情（日频，%s 收盘）" % asof_s
            asof = asof_s
            fut_info = {"code": fut_main["code"] if fut_main else None,
                        "settle": fut_main.get("settle") if fut_main else None,
                        "vol": fut_main.get("vol") if fut_main else None}
            data_mode = "eod"
    else:
        ch = etf_chain(p, force=force)
        if not ch or not ch.get("months"):
            return {"ok": False, "err": "ETF 期权行情暂不可用",
                    "key": product_key, "name": p["name"]}
        asof = ch.get("asof", "")
        try:
            asof_d = _dt.date.fromisoformat(asof)
        except (TypeError, ValueError):
            asof_d = None
        for yymm, rows in sorted(ch["months"].items()):
            s = _build_slice(rows, yymm, "etf", cal, None, asof_d)
            if s:
                slices.append(s)
        src = ch.get("source", "东财期权行情")
        fut_info = {"code": p.get("code"), "settle": None, "vol": None}
        data_mode = "etf"

    if not slices:
        return {"ok": False, "err": "当日无可用的期权报价（可能全部到期或数据缺失）",
                "key": product_key, "name": p["name"]}
    slices.sort(key=lambda x: x["days"])
    front = slices[0]
    # 顶层 future 要与「近月」对齐：成交量主力在近月快到期时常常已经移到次月，
    # 直接展示主力会让「IO2611 的 ATM IV」旁边挂着 IF2612 的价格，自相矛盾。
    if p["kind"] == "cffex" and front.get("fwd_fut"):
        for f in (cffex_day(force=False).get("futures", {}).get(p["future"], []) or []):
            if f["yymm"] == front["yymm"]:
                fut_info = {"code": f["code"], "settle": f.get("settle"),
                            "vol": f.get("vol"), "is_main": False}
                break

    # 期限结构：各月 ATM IV
    term = [{"yymm": s["yymm"], "expiry": s["expiry"], "days": s["days"],
             "atm_iv": s["atm_iv"], "skew25": s["skew25"],
             "fwd_gap_pct": s["fwd_gap_pct"]} for s in slices]
    checks = _cross_check(front) if p["kind"] == "cffex" else {}

    # 期限斜率：近月 → 次月 ATM IV 变化（正值 = 远端更贵 = 曲线陡峭化）
    slope = None
    if len(slices) >= 2 and slices[0]["atm_iv"] and slices[1]["atm_iv"]:
        slope = round(slices[1]["atm_iv"] - slices[0]["atm_iv"], 3)

    return {"ok": True, "key": product_key, "name": p["name"], "short": p["short"],
            "board": p.get("board", ""), "kind": p["kind"],
            "asof": asof, "source": src, "future": fut_info, "data_mode": data_mode,
            "slices": slices, "term": term, "slope": slope,
            "front": {"yymm": front["yymm"], "expiry": front["expiry"],
                      "days": front["days"], "atm_iv": front["atm_iv"],
                      "skew25": front["skew25"]},
            "checks": checks,
            "risk_free": RISK_FREE}


# ============ 历史 IV（固定 30 天期限） ============
# 为什么必须做「固定期限」而不是「近月」
# ------------------------------------
# 近月合约到期后会自动切到下一月，剩余天数从 5 天跳到 40 天。
# 而 IV 的期限结构通常是近月贵、远月便宜，直接串「近月」序列会在换月日
# 出现 3~6 个百分点的假跳变 —— 那不是波动率变了，是期限变了。
# 正确做法：每天在期限结构上插值到固定 30 天（constant maturity），
# 这样不同日期之间才可比，分位数才有意义。
#
# 数据来源：**同一天的同一个中金所 CSV 里已含 IO/HO/MO 三个品种**，
# 所以回溯 N 天只需 N 次请求，不是 3N 次。
_IVH_TTL = 3600
_IVH_CACHE: dict = {"ts": 0.0, "data": None}
# ★ 并发闸门：首次回溯要发 120 个请求，重试/连点会让它跑两遍
_IVH_BUSY = threading.Lock()
_IVH_FILE = "ivhist.json"
CM_DAYS = 30.0


def _cm_iv(slices: list, target: float = CM_DAYS) -> Optional[float]:
    """在期限结构上插值出固定 30 天的 ATM IV。超出范围时用最近端并标记。"""
    pts = sorted([(s["days"], s["atm_iv"]) for s in slices
                  if s.get("atm_iv") and s.get("days") is not None])
    if not pts:
        return None
    if len(pts) == 1:
        return pts[0][1]
    for i in range(len(pts) - 1):
        d0, v0 = pts[i]
        d1, v1 = pts[i + 1]
        if d0 <= target <= d1:
            if d1 == d0:
                return v0
            w = (target - d0) / (d1 - d0)
            return v0 + w * (v1 - v0)
    return pts[0][1] if target < pts[0][0] else pts[-1][1]


# ============ 中国波指 QVIX（补 ETF 期权的历史分位）============
# 为什么需要它：ETF 期权没有历史 CSV（东财只给当日），自建序列要从启用当天才开始攒。
# 而交易所口径的「中国波指」（QVIX）有 2015 年至今的日频序列，正好补上这一段的空白。
# ★ 口径不同，不能混着比：
#   · 本模块的 ATM IV 是**近月**（十几到几十天）的平值隐含波动率；
#   · QVIX 是官方编制的 **30 天恒定期限**波指（更接近方差互换的算法）。
#   所以 ETF 卡片主数字仍用自算 ATM IV，QVIX 只用来回答「当前贵不贵」这一个问题，
#   并且明确标注是官方波指口径。拿 QVIX 的分位去套自己的 ATM IV 是偷换口径。
_QVIX_CACHE: dict = {}
_QVIX_TTL = 12 * 3600
_QVIX_FN = {"300etf": "index_option_300etf_qvix",
            "50etf": "index_option_50etf_qvix"}


def qvix(name: str = "300etf", force: bool = False, win: int = 1250) -> dict:
    """中国波指（QVIX）历史序列 + 当前分位。win 默认 1250 个交易日（约 5 年）。"""
    now = time.time()
    with _LOCK:
        c = _QVIX_CACHE.get(name)
        if not force and c and now - c[0] < _QVIX_TTL:
            return c[1]
    fn = _QVIX_FN.get(name)
    if not fn:
        return {"ok": False, "err": "未知品种", "name": name}
    try:
        import akshare as ak
        df = getattr(ak, fn)()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": "QVIX 拉取失败：%s" % str(e)[:120], "name": name}
    try:
        import pandas as pd
        d = pd.to_datetime(df["date"], errors="coerce")
        v = pd.to_numeric(df["close"], errors="coerce")
        tmp = pd.DataFrame({"d": d, "v": v}).dropna()
        # ★ 同一天可能返回两条（收盘前后各一次更新），保留最后一条；
        #   实测 2026-11-02 就有 17.82 与 19.27 两个值。
        tmp = tmp.drop_duplicates("d", keep="last").sort_values("d")
        if tmp.empty:
            return {"ok": False, "err": "QVIX 全为空值", "name": name}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": "QVIX 解析失败：%s" % str(e)[:120], "name": name}

    vals = tmp["v"].to_numpy(dtype=float)
    dates = [str(x)[:10] for x in tmp["d"].tolist()]
    cur = float(vals[-1])
    w = vals[-win:] if len(vals) > win else vals
    pct = float((w <= cur).mean() * 100)
    last_d = dates[-1]
    lag = None
    try:
        lag = (_dt.date.today() - _dt.date.fromisoformat(last_d)).days
    except ValueError:
        pass
    step = max(1, len(dates) // 160)
    series = [{"d": dates[i], "v": round(float(vals[i]), 2)}
              for i in range(len(dates))][::step][-160:]
    res = {"ok": True, "name": name, "date": last_d, "close": round(cur, 2),
           "pct": round(pct, 1), "n": int(len(w)),
           "p20": round(float(np.percentile(w, 20)), 2),
           "p80": round(float(np.percentile(w, 80)), 2),
           "med": round(float(np.median(w)), 2),
           "lag_days": lag, "series": series,
           "source": "中国波指 QVIX（官方编制，30 天恒定期限口径）",
           # 滞后超过 7 天说明数据源没在更新，不能装作是当下的读数
           "stale": bool(lag is not None and lag > 7),
           # 用 QVIX 自己的分位判贵贱（口径自洽：拿它自己的历史比它自己）
           "state": _iv_state(cur / 100.0, pct),
           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    with _LOCK:
        _QVIX_CACHE[name] = (now, res)
    return res


def _ivh_load() -> dict:
    d = _cache_read_any(_IVH_FILE)
    return d if isinstance(d, dict) else {}


def _ivh_save(hist: dict) -> None:
    # 只保留最近 400 个交易日，防止文件无限增长
    for k in list(hist.keys()):
        ser = hist.get(k) or {}
        if len(ser) > 400:
            keep = sorted(ser.keys())[-400:]
            hist[k] = {d: ser[d] for d in keep}
    _cache_write(_IVH_FILE, hist)


def _ivh_one_day(d: str, cal: Optional[set]) -> tuple:
    """拉并解析某一个交易日 → ({品种: 当日读数}, None)。"""
    try:
        dt = _dt.date.fromisoformat(d)
    except ValueError:
        return d, None
    txt = _cffex_fetch(dt)
    if not txt:
        return d, None
    try:
        day = _parse_cffex(txt, d)
    except Exception:  # noqa: BLE001
        return d, None
    out = {}
    for p in PRODUCTS:
        if p["kind"] != "cffex":
            continue
        rows_by_month = day["options"].get(p["pre"]) or {}
        if not rows_by_month:
            continue
        slices = []
        for yymm, rr in sorted(rows_by_month.items()):
            # 历史日期不传期货价：F 由 parity 从期权自身反推即可，且能省掉期货匹配
            s = _build_slice(rr, yymm, "cffex", cal, None, dt)
            if s:
                slices.append(s)
        if not slices:
            continue
        slices.sort(key=lambda x: x["days"])
        front = slices[0]
        cm = _cm_iv(slices)
        out[p["key"]] = {"cm30": round(cm, 6) if cm is not None else None,
                         "skew": front.get("skew25"),
                         "front_atm": front.get("atm_iv"),
                         "front_days": front.get("days"),
                         "n_month": len(slices)}
    return d, (out or None)


def iv_history(force: bool = False, back: int = 120, workers: int = 10) -> dict:
    """补齐历史 IV 序列（只补缺失的交易日）。返回 {key: {date: {...}}} 与统计。

    ★ 首次会拉 back 个交易日的 CSV（实测 25 天/5 并发 = 21s，120 天/10 并发约 15~25s）；
      之后每天只补 1 个。
      所以这个接口**不能并进 /api/idxopt/panel**，前端拿到当日横截面后再异步取。

    ★ 加 in-flight 闸门：这个函数要发 120 个请求，前端如果因为超时重试、
      或者用户连点两次「刷新」，会并发跑两遍 → 240 个请求打在中金所身上。
      重复进来时直接返回上一次的结果（哪怕不完整），不发第二轮网络。
    """
    now = time.time()
    with _LOCK:
        c = _IVH_CACHE.get("data")
        if not force and c and now - _IVH_CACHE["ts"] < _IVH_TTL:
            return c
    if not _IVH_BUSY.acquire(blocking=False):
        with _LOCK:
            prev = _IVH_CACHE.get("data")
        if prev:
            out = dict(prev)
            out["busy"] = True
            return out
        return {"ok": True, "busy": True, "stats": {}, "series": {},
                "msg": "历史 IV 正在回溯中，请稍候（首次约 20 秒）"}
    try:
        return _iv_history_impl(force, back, workers, now)
    finally:
        _IVH_BUSY.release()


def _iv_history_impl(force: bool, back: int, workers: int, now: float) -> dict:
    hist = _ivh_load()
    cal = _trade_days()
    if not cal:
        return {"ok": False, "err": "交易日历不可用，无法回溯", "stats": {}}

    today = _dt.date.today().isoformat()
    days = sorted([d for d in cal if d <= today])[-back:]
    if not days:
        return {"ok": False, "err": "无可用交易日", "stats": {}}

    have = set((hist.get("io") or {}).keys())
    missing = [d for d in days if d not in have]
    fetched = 0
    if missing:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for d, out in ex.map(lambda x: _ivh_one_day(x, cal), missing):
                if not out:
                    continue
                for k, v in out.items():
                    hist.setdefault(k, {})[d] = v
                fetched += 1
        if fetched:
            _ivh_save(hist)

    # 先把落盘结果留在内存里，再算统计（统计只依赖 hist）
    res = {"ok": True, "from": days[0] if days else "", "to": days[-1] if days else "",
           "n_days": len(days), "fetched": fetched, "stats": {}, "series": {},
           "fetch_fail": len(missing) - fetched}
    for p in PRODUCTS:
        k = p["key"]
        ser = hist.get(k) or {}
        vals = []
        for d in sorted(ser.keys()):
            v = (ser[d] or {}).get("cm30")
            if v:
                vals.append((d, v))
        if not vals:
            res["stats"][k] = {"n": 0}
            res["series"][k] = []
            continue
        arr = np.array([v for _, v in vals], dtype=float)
        cur = arr[-1]
        pct = float((arr <= cur).mean() * 100)
        # 近 120 个交易日（约半年）的分位 —— 太长会把 2018 年那种极端环境算进来
        w = arr[-120:] if len(arr) > 120 else arr
        pct_w = float((w <= cur).mean() * 100)
        res["stats"][k] = {
            "n": len(arr), "cur": round(cur, 4),
            "pct": round(pct, 1), "pct120": round(pct_w, 1),
            "min": round(float(arr.min()), 4), "max": round(float(arr.max()), 4),
            "med": round(float(np.median(arr)), 4),
            "p20": round(float(np.percentile(arr, 20)), 4),
            "p80": round(float(np.percentile(arr, 80)), 4),
            "first": vals[0][0], "last": vals[-1][0],
            # 分位样本不足时说清楚，不能拿 20 个点冒充历史分位
            "enough": len(arr) >= 60,
        }
        res["stats"][k]["state"] = _iv_state(cur, pct_w if len(arr) >= 60 else None)
        # 前端画分位带用：每 3 天取一个点，控制体积
        step = max(1, len(vals) // 120)
        res["series"][k] = [{"d": d, "v": round(v, 4)} for d, v in vals[::step]][-120:]
    res["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        _IVH_CACHE["ts"], _IVH_CACHE["data"] = now, res
    return res


def _iv_state(iv: Optional[float], pct: Optional[float] = None) -> dict:
    """IV 状态。**优先用历史分位**，没有分位才退回绝对刻度。

    ★ 绝对刻度（15/22/30）是跨品种的经验值，对 IO 这种常年 15~25% 的品种
      会把「21.5%」判成「偏高」，可它其实就在中位数上。所以分位一旦可用，
      就以分位为准，绝对值只作为文案补充。
    """
    if iv is None:
        return {"label": "—", "tone": "calm", "basis": "none"}
    if pct is not None:
        if pct >= 80:
            return {"label": "高位", "tone": "crisis", "basis": "pct"}
        if pct >= 60:
            return {"label": "偏高", "tone": "warn", "basis": "pct"}
        if pct >= 40:
            return {"label": "中位", "tone": "calm", "basis": "pct"}
        if pct >= 20:
            return {"label": "偏低", "tone": "up", "basis": "pct"}
        return {"label": "低位", "tone": "up", "basis": "pct"}
    if iv >= 0.30:
        return {"label": "极高", "tone": "crisis", "basis": "abs"}
    if iv >= 0.22:
        return {"label": "偏高", "tone": "warn", "basis": "abs"}
    if iv >= 0.15:
        return {"label": "常态", "tone": "calm", "basis": "abs"}
    return {"label": "低迷", "tone": "up", "basis": "abs"}


def _skew_state(s: Optional[float]) -> dict:
    """偏斜分档：> 3pp 说明看跌保护被抢，< 0 说明追涨。"""
    if s is None:
        return {"label": "—", "tone": "calm"}
    if s >= 4.0:
        return {"label": "恐慌溢价", "tone": "crisis"}
    if s >= 2.0:
        return {"label": "偏防守", "tone": "warn"}
    if s >= 0.0:
        return {"label": "中性", "tone": "calm"}
    return {"label": "追涨", "tone": "up"}


def panel(force: bool = False) -> dict:
    """全部品种的期权面板（前端异步独立加载，不阻塞主面板）。"""
    t0 = time.time()
    items = []
    for p in PRODUCTS:
        try:
            s = surface(p["key"], force=force)
        except Exception as e:  # noqa: BLE001 —— 单品种失败不影响其它
            s = {"ok": False, "err": str(e)[:160], "key": p["key"], "name": p["name"]}
        if s.get("ok"):
            s["atm_state"] = _iv_state((s.get("front") or {}).get("atm_iv"))
            s["skew_state"] = _skew_state((s.get("front") or {}).get("skew25"))
            s["desc"] = p.get("desc", "")
        items.append(s)

    ok = [x for x in items if x.get("ok")]
    return {"ok": bool(ok), "items": items,
            "asof": max([x.get("asof") or "" for x in ok], default=""),
            "elapsed": round(time.time() - t0, 2),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "err": "" if ok else "全部品种暂不可用",
            "caveats": [
                "中金所日行情是日频文件，盘中访问当日会 404；交易时段自动改用"
                "延时快照（quote_*.txt 的买卖中价）算当日 IV，「实时」角标即此意。"
                "非交易时段仍为 T-1 收盘口径",
                "IV 由本模块用 Black-76 二分反解，不是交易所直接给出；"
                "沪深300ETF 期权无官方希腊字母，无法交叉验证",
                "远期 F 由 put-call parity 从期权自身反推（不是拿期货价硬套），"
                "F_opt − F_fut 的差即隐含基差，两者不一致属正常",
                "风险利率固定 1.5%：T<1 年时折现因子对 r 不敏感，但这是一个假设",
                "曲线只用虚值端拟合，深度实值档位因 vega 低、买卖价差宽而未纳入",
                "到期日按「第三个周五 / 第四个周三」推算：日历源只覆盖到当日，"
                "未来月份的法定节假日顺延未纳入，极端情况下 T 可能差 1-3 天",
            ]}


def meta_info() -> dict:
    """期权口径披露。"""
    return {
        "products": [{"key": p["key"], "name": p["name"], "board": p.get("board"),
                      "kind": p["kind"], "desc": p.get("desc", ""),
                      "expiry_rule": ("到期月第四个星期三（遇节假日顺延）" if p["kind"] == "etf"
                                      else "到期月第三个星期五（遇节假日顺延）"),
                      "mult": p.get("mult")} for p in PRODUCTS],
        "method": [
            "远期 F：put-call parity  F = K + e^(rT)(C − P)，逐行权价计算后取中位数",
            "隐含波动率：Black-76 模型二分反解，容差 1e-6",
            "Delta：∂V/∂F，折现后；用于与中金所官方 Delta 交叉验证",
            "ATM IV：在 log(K/F) 空间两侧插值，不取单点",
            "25Δ 偏斜：在 delta 空间插值  IV(25Δ put) − IV(25Δ call)",
            "T：ACT/365，到期日按交易所规则推算并顺延节假日",
        ],
        "risk_free": RISK_FREE,
        "caveats": [
            "中金所日行情文件是日频：交易时段改走延时快照 quote_*.txt（买卖中价），"
            "非交易时段走日线 CSV —— 卡片上的「实时」角标即表示前者",
            "ETF 期权无官方 IV，本模块结果无法交叉验证，只能靠微笑平滑性自检",
            "期权只覆盖股指/ETF 期权，不含商品期权与个股期权",
            "IV 历史分位由逐日回溯中金所 CSV 得到，且在期限结构上插值到**固定 30 天**"
            "（不串「近月」，否则换月日会凭空跳 3~6 个百分点）；首次 15~30s",
            "ETF 期权没有历史 CSV，历史分位改用官方中国波指 QVIX："
            "那是 30 天恒定期限口径，与本模块自算的近月 ATM IV **不是同一口径**，"
            "只用来判断贵贱，不能直接相减",
        ],
    }
