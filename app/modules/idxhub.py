# -*- coding: utf-8 -*-
"""指数指标（Index Hub）—— 全球风险锚 × 中国资产期现结构 × 传导分析。

为什么单独一个模块
------------------
原有 15 个页签都盯着「个股」，缺一个从「宏观/指数/期现」自上而下看市场的入口。
本模块只做三件事，且都不与既有模块共享状态（零侵入）：
  1) 全球风险锚：VIX / VIX9D / 布伦特原油 / 富时中国A50 / 恒生 / 恒生科技
  2) 中国资产结构：境内宽基现货（上证/沪深300/上证50/中证500/中证1000/创业板/科创50）
     + 股指期货主连（IF/IH/IC/IM）的基差、年化基差、持仓变化
  3) 联动分析：规则化信号（只输出触发的）

数据源与口径（2026-10-30 实测确定，勿随意替换）
--------------------------------------------
· VIX / VIX9D：CBOE 官方 CSV（`cdn.cboe.com`），1990 起，美东交易日收盘。
  ★ 美东 T 日收盘 ≈ 北京 T+1 凌晨 → A 股 T+1 日开盘前已知，故对齐时**滞后一根**。
  ★ 腾讯 `usVIX` 是坏数据（价格长期恒 21.67），新浪 `gb_$vix` 返回空，均不可用。
· 布伦特 / 富时A50：新浪外盘期货（`hf_OIL` / `hf_CHA50CFD` + GlobalFutures 日K），
  连续报价、无换月跳空，历史自 2016-10 起。
· 恒生 / 恒生科技 / 境内指数：**雪球 → 腾讯 ifzq → 新浪** 三级自动切换。
  ★ 实测（2026-10-30）雪球最快（~0.08s/个，A股+港股都覆盖，2000 根），腾讯 ~0.2s 但会
    「整体返回空」（hkHSI 与 sh000001 同时挂数十分钟），新浪 ~1.4s 最稳但只覆盖 A 股。
    因此不按厂商名气定主源，而是按**实时健康度**排序：失败 2 次冷却 10 分钟后自动降级，
    成功 1 次即恢复。页面页脚会标注当前实际用到的源（`fallbacks` 字段）。
· 股指期货 IF/IH/IC/IM：新浪主连（`InnerFuturesNewService.getDailyKLine`），
  ★ 主连是「主力合约拼接」口径，**换月日基差会跳空** → 本模块检测后剔除，不参与信号。
· 东财 push2his / push2 在 2026-10-30 实测整体连不通（RemoteDisconnected），故不依赖东财。

★ 反未来函数：所有历史标定都按「当日只能看到当日及之前」的序列逐日重算，
  三处滞后处理（VIX 滞后一根、分位用滚动窗口而非全样本、基差用当日已知收盘）。

★ 不合成单一决策分：风险温度只是温度计（且明示动态权重），
  每条信号都独立给结论，绝不把多因子压成一个「买卖分」（与 monitor 同规矩）。
"""
from __future__ import annotations

import bisect
import datetime as _dt
import json
import math
import os
import re
import threading
import time
from typing import Callable, Optional

import numpy as np

from .alert import _UA, _ensure_ua, _net_call

ProgressCb = Optional[Callable[[float, float, str], None]]

# ============ 资产定义 ============
# kind: cboe / sina_gf(新浪外盘) / tx(腾讯指数) / sina_inner(新浪内盘期货)
ASSETS: list = [
    # ---- 全球风险锚（悬浮窗上方四个，顺序即用户要求）----
    {"key": "vix", "name": "VIX 恐慌指数", "short": "VIX", "grp": "overseas",
     "kind": "cboe", "sym": "VIX", "dec": 2, "unit": "", "hero": 1,
     "desc": "标普500 30日隐含波动率，全球风险偏好的第一温度计"},
    {"key": "a50", "name": "富时中国A50 期货", "short": "A50期货", "grp": "offshore",
     "kind": "sina_gf", "sym": "CHA50CFD", "spot": "hf_CHA50CFD", "dec": 2, "unit": "", "hero": 2,
     "desc": "新交所离岸 A 股定价，A 股开盘前的先行指标"},
    {"key": "brent", "name": "布伦特原油", "short": "布伦特", "grp": "overseas",
     "kind": "sina_gf", "sym": "OIL", "spot": "hf_OIL", "dec": 2, "unit": "美元", "hero": 3,
     "desc": "输入型通胀与地缘风险的定价锚"},
    {"key": "hsi", "name": "恒生指数", "short": "恒生", "grp": "offshore",
     "kind": "tx", "sym": "hkHSI", "spot": "rt_hkHSI", "dec": 2, "unit": "", "hero": 4,
     "desc": "离岸中国资产的风险偏好，外资态度的即时读数"},
    # ---- 辅助 ----
    {"key": "vix9d", "name": "VIX9D（9日波动率）", "short": "VIX9D", "grp": "overseas",
     "kind": "cboe", "sym": "VIX9D", "dec": 2, "unit": "",
     "desc": "9 日隐含波动率；与 VIX 比较可读出「近端事件风险」是否急升"},
    {"key": "hstech", "name": "恒生科技指数", "short": "恒生科技", "grp": "offshore",
     "kind": "tx", "sym": "hkHSTECH", "spot": "rt_hkHSTECH", "dec": 2, "unit": "",
     "desc": "港股成长/科技风格，对南向资金最敏感"},
    # ---- 境内现货 ----
    {"key": "sh000001", "name": "上证指数", "short": "上证", "grp": "onshore",
     "kind": "tx", "sym": "sh000001", "spot": "s_sh000001", "dec": 2, "unit": ""},
    {"key": "sh000300", "name": "沪深300", "short": "沪深300", "grp": "onshore",
     "kind": "tx", "sym": "sh000300", "spot": "s_sh000300", "dec": 2, "unit": "", "core": 1,
     "desc": "A 股核心宽基，期指 IF 的现货标的"},
    {"key": "sh000016", "name": "上证50", "short": "上证50", "grp": "onshore",
     "kind": "tx", "sym": "sh000016", "spot": "s_sh000016", "dec": 2, "unit": ""},
    {"key": "sh000905", "name": "中证500", "short": "中证500", "grp": "onshore",
     "kind": "tx", "sym": "sh000905", "spot": "s_sh000905", "dec": 2, "unit": ""},
    {"key": "sh000852", "name": "中证1000", "short": "中证1000", "grp": "onshore",
     "kind": "tx", "sym": "sh000852", "spot": "s_sh000852", "dec": 2, "unit": ""},
    {"key": "sz399006", "name": "创业板指", "short": "创业板", "grp": "onshore",
     "kind": "tx", "sym": "sz399006", "spot": "s_sz399006", "dec": 2, "unit": ""},
    {"key": "sh000688", "name": "科创50", "short": "科创50", "grp": "onshore",
     "kind": "tx", "sym": "sh000688", "spot": "s_sh000688", "dec": 2, "unit": ""},
    # ---- 境内期货（主连）----
    {"key": "IF0", "name": "沪深300 股指期货（主连）", "short": "IF 主连", "grp": "futures",
     "kind": "sina_inner", "sym": "IF0", "spot": "nf_IF0", "dec": 1, "unit": "",
     "under": "sh000300", "mult": 300, "board": "中金所"},
    {"key": "IH0", "name": "上证50 股指期货（主连）", "short": "IH 主连", "grp": "futures",
     "kind": "sina_inner", "sym": "IH0", "spot": "nf_IH0", "dec": 1, "unit": "",
     "under": "sh000016", "mult": 300, "board": "中金所"},
    {"key": "IC0", "name": "中证500 股指期货（主连）", "short": "IC 主连", "grp": "futures",
     "kind": "sina_inner", "sym": "IC0", "spot": "nf_IC0", "dec": 1, "unit": "",
     "under": "sh000905", "mult": 200, "board": "中金所"},
    {"key": "IM0", "name": "中证1000 股指期货（主连）", "short": "IM 主连", "grp": "futures",
     "kind": "sina_inner", "sym": "IM0", "spot": "nf_IM0", "dec": 1, "unit": "",
     "under": "sh000852", "mult": 200, "board": "中金所"},
    # ---- 利率 / 流动性（政策面的可观测代理）----
    {"key": "T0", "name": "10 年期国债期货（主连）", "short": "T 债主连", "grp": "rates",
     "kind": "sina_inner", "sym": "T0", "spot": "nf_T0", "dec": 3, "unit": "",
     "board": "中金所",
     "desc": "国内利率与流动性的即时读数：价格涨 = 收益率下行 = 宽货币"},
]
AMAP = {a["key"]: a for a in ASSETS}

HORIZONS = (5, 20, 60)
TERM_NAME = {5: "短期（1 周）", 20: "中期（1 月）", 60: "长期（1 季）"}

# ============ 缓存 ============
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE_DIR = os.path.join(_ROOT, "data", "idx_cache")
_LOCK = threading.RLock()
_SPOT_CACHE: dict = {"ts": 0.0, "data": None}
_HIST_CACHE: dict = {}
_AN_CACHE: dict = {"ts": 0.0, "data": None}
_SESS = None

_TTL_SPOT = 45          # 实时快照


def _is_trade_hours() -> bool:
    now = _dt.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 + 10 <= hm <= 15 * 60 + 10


def _ttl_hist() -> float:
    """历史日K 缓存时长 —— 分时段。

    ★ 日K 的最后一根在盘中就是「实时价」。固定 30 分钟会让页面显示半小时前的行情，
      这是一个很容易被忽略的体验缺陷：交易时段必须压到 60 秒。
    """
    if _is_trade_hours():
        return 60.0
    now = _dt.datetime.now()
    hm = now.hour * 60 + now.minute
    if now.weekday() < 5 and 20 * 60 + 50 <= hm <= 23 * 60 + 59:
        return 120.0        # 外盘夜盘（布伦特 / A50）在动
    return 1800.0


def _ttl_env() -> float:
    return 120.0 if _is_trade_hours() else 600.0


def _ttl_an() -> float:
    return 60.0 if _is_trade_hours() else 180.0


_TTL_VIX = 3600        # CBOE CSV（1 小时；force 可穿透）


def _sess():
    global _SESS
    if _SESS is None:
        import requests
        from requests.adapters import HTTPAdapter
        s = requests.Session()
        s.trust_env = False
        ad = HTTPAdapter(pool_connections=12, pool_maxsize=24, max_retries=0)
        s.mount("https://", ad)
        s.mount("http://", ad)
        _SESS = s
    return _SESS


def _http(url: str, headers: Optional[dict] = None, timeout: int = 15,
          enc: Optional[str] = None) -> str:
    _ensure_ua()

    def _get():
        h = {"User-Agent": _UA}
        if headers:
            h.update(headers)
        r = _sess().get(url, headers=h, timeout=timeout)
        if enc:
            r.encoding = enc
        return r.text

    return _net_call(_get, retries=2, pause=0.4)


# ============ 落盘缓存（只读缓存，绝不碰 data 下的正式数据文件） ============
def _cache_file(name: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, name)


def _cache_read(name: str, ttl: float):
    p = _cache_file(name)
    try:
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:  # noqa: BLE001 —— 缓存坏了就当没有
        pass
    return None


def _cache_read_any(name: str):
    """忽略 TTL 读缓存（网络故障时的兜底）。"""
    p = _cache_file(name)
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:  # noqa: BLE001
        pass
    return None


def _cache_write(name: str, obj) -> None:
    try:
        p = _cache_file(name)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


# ============ 实时快照（新浪批量，一次请求覆盖全部） ============
def _parse_hf(parts: list) -> dict:
    """外盘期货 hf_：0 最新 2 卖价 3 昨收 4 最高 5 最低 6 时间 7 昨结算 8 开盘 14 成交量。"""
    px = _f(parts[0])
    prev = _f(parts[3]) or _f(parts[7])
    return {"price": px, "prev": prev, "open": _f(parts[8]),
            "high": _f(parts[4]), "low": _f(parts[5]),
            "pct": (px / prev - 1) * 100 if (px and prev) else None,
            "time": (parts[6] if len(parts) > 6 else ""),
            "date": (parts[12] if len(parts) > 12 else "")}


def _parse_rt_hk(parts: list) -> dict:
    """港股 rt_hk：1 名称 2 今开 3 昨收 4 最高 5 最低 6 现价 7 涨跌 8 涨跌幅。"""
    px = _f(parts[6])
    prev = _f(parts[3])
    pct = _f(parts[8]) if len(parts) > 8 else None
    if pct is None and px and prev:
        pct = (px / prev - 1) * 100
    return {"price": px, "prev": prev, "open": _f(parts[2]),
            "high": _f(parts[4]), "low": _f(parts[5]), "pct": pct,
            "date": (parts[17] if len(parts) > 17 else ""),
            "time": (parts[18] if len(parts) > 18 else "")}


def _parse_s_idx(parts: list) -> dict:
    """新浪指数 s_：1 名称 2 现价 3 涨跌额 4 涨跌幅 5 成交量(手) 6 成交额(万)。"""
    return {"price": _f(parts[1]), "chg": _f(parts[2]), "pct": _f(parts[3]),
            "volume": _f(parts[4]), "amount": _f(parts[5])}


def _parse_nf(parts: list) -> dict:
    """内盘期货 nf_ 快照。

    实测字段（2026-11-06 T0 验证）：[0]开 [1]高 [2]低 [3]最新 [4]量 [5]额 [6]持仓
    [13]昨结算（与日K上一根 close 完全一致）[36]日期 [37]时间。
    ★ 有了 [13] 就能直接算当日涨跌，不必依赖「日K 是否含今天」——
      新浪内盘日K 盘中不生成当天K线，只靠日K 会把涨跌幅显示成昨天的。
    """
    px = _f(parts[3])
    prev = _f(parts[13]) if len(parts) > 13 else None
    return {"price": px, "prev": prev,
            "open": _f(parts[0]), "high": _f(parts[1]), "low": _f(parts[2]),
            "volume": _f(parts[4]), "amount": _f(parts[5]), "hold": _f(parts[6]),
            "pct": (px / prev - 1) * 100 if (px and prev) else None,
            "date": (parts[36] if len(parts) > 36 else ""),
            "time": (parts[37] if len(parts) > 37 else "")}


def _f(v) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def fetch_spot(force: bool = False) -> dict:
    """一次请求拿全部实时快照 → {key: {price, pct, ...}}。"""
    now = time.time()
    with _LOCK:
        if not force and _SPOT_CACHE["data"] and now - _SPOT_CACHE["ts"] < _TTL_SPOT:
            return _SPOT_CACHE["data"]

    syms = []
    for a in ASSETS:
        s = a.get("spot")
        if s:
            syms.append(s)
    out: dict = {}
    if syms:
        try:
            txt = _http("https://hq.sinajs.cn/list=" + ",".join(syms),
                        headers={"Referer": "https://finance.sina.com.cn"}, enc="gbk")
            for line in (txt or "").split("\n"):
                m = re.match(r'var hq_str_([^=]+)="(.*)";', line.strip())
                if not m:
                    continue
                sym, body = m.group(1), m.group(2)
                if not body:
                    continue
                parts = body.split(",")
                key = next((a["key"] for a in ASSETS if a.get("spot") == sym), None)
                if not key:
                    continue
                try:
                    if sym.startswith("hf_"):
                        out[key] = _parse_hf(parts)
                    elif sym.startswith("rt_hk"):
                        out[key] = _parse_rt_hk(parts)
                    elif sym.startswith("s_"):
                        out[key] = _parse_s_idx(parts)
                    elif sym.startswith("nf_"):
                        out[key] = _parse_nf(parts)
                except Exception:  # noqa: BLE001 —— 单条解析失败不影响其它
                    continue
            out["__ts__"] = now
            out["__ok__"] = True
        except Exception as e:  # noqa: BLE001
            out = {"__ts__": now, "__ok__": False, "__err__": str(e)[:200]}

    with _LOCK:
        _SPOT_CACHE["ts"], _SPOT_CACHE["data"] = now, out
    return out


# ============ 历史日K ============
_JSONP = re.compile(r"\((\[.*\])\)", re.S)


def _bars(rows: list) -> list:
    out = []
    for r in rows:
        try:
            c = float(r["close"])
            if not (c > 0):
                continue
            out.append({"date": str(r["date"])[:10],
                        "open": float(r.get("open") or c),
                        "high": float(r.get("high") or c),
                        "low": float(r.get("low") or c),
                        "close": c,
                        "volume": float(r.get("volume") or 0),
                        "hold": float(r.get("hold") or 0)})
        except (TypeError, ValueError, KeyError):
            continue
    out.sort(key=lambda x: x["date"])
    return out


def _cboe(sym: str = "VIX", force: bool = False) -> list:
    """CBOE 官方日线 CSV。缓存落盘，避免每次拉 470KB。

    ★ force 必须能穿透落盘缓存：否则「刷新」按钮永远拿不到当日新数据
      （2026-11-06 实测：CBOE 已更新到 11/05、VIX 从 16.6 跳到 28.2 暴涨 70%，
       而页面还显示 11/04 的旧值 —— 就是被 6 小时落盘缓存挡住）。
    """
    name = "cboe_%s.json" % sym.lower()
    if not force:
        c = _cache_read(name, _TTL_VIX)
        if c:
            return c
    else:
        # ★ force 也给 10 分钟的保护：这两个 CSV 各 ~470KB / 200KB，单次要 5-6 秒。
        #   CBOE 每天只更新一次，连续点「刷新」没必要反复下整份历史。
        c = _cache_read(name, 600)
        if c:
            return c
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/%s_History.csv" % sym
    rows = []
    try:
        txt = _http(url, timeout=25)
        lines = (txt or "").strip().split("\n")
        for ln in lines[1:]:
            p = ln.strip().split(",")
            if len(p) < 5:
                continue
            d, o, h, l, cl = p[0], p[1], p[2], p[3], p[4]
            try:
                mm, dd, yy = d.split("/")
                iso = "%s-%s-%s" % (yy, mm.zfill(2), dd.zfill(2))
                rows.append({"date": iso, "open": float(o), "high": float(h),
                             "low": float(l), "close": float(cl), "volume": 0.0, "hold": 0.0})
            except (ValueError, TypeError):
                continue
    except Exception:  # noqa: BLE001
        return []
    rows.sort(key=lambda x: x["date"])
    rows = rows[-3200:]     # 约 12 年，够覆盖 IF 主连全历史
    if rows:
        _cache_write(name, rows)
    return rows


def _sina_global(sym: str) -> list:
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/v1.5/"
           "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=%s" % sym)
    try:
        txt = _http(url, headers={"Referer": "https://finance.sina.com.cn"})
    except Exception:  # noqa: BLE001
        return []
    m = _JSONP.search(txt or "")
    if not m:
        return []
    try:
        raw = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return []
    return _bars([{"date": x.get("date"), "open": x.get("open"), "high": x.get("high"),
                   "low": x.get("low"), "close": x.get("close"),
                   "volume": x.get("volume"), "hold": x.get("position")} for x in raw])


def _sina_inner(sym: str) -> list:
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/v1.5/"
           "InnerFuturesNewService.getDailyKLine?symbol=%s" % sym)
    try:
        txt = _http(url, headers={"Referer": "https://finance.sina.com.cn"})
    except Exception:  # noqa: BLE001
        return []
    m = _JSONP.search(txt or "")
    if not m:
        return []
    try:
        raw = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return []
    return _bars([{"date": x.get("d"), "open": x.get("o"), "high": x.get("h"),
                   "low": x.get("l"), "close": x.get("c"),
                   "volume": x.get("v"), "hold": x.get("p")} for x in raw])


def _sina_index_kline(symbol: str, n: int = 2000) -> list:
    """新浪 A 股指数日K（CN_MarketData）。实测 datalen=2000 → 2018-07 起，与腾讯同口径。

    ★ 2026-10-30 实测：腾讯 ifzq 会整体返回空（疑似限流），此时本接口是 A 股指数的
      第 2 级备源，必须保留。
    """
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d"
           % (symbol, max(int(n), 60)))
    try:
        txt = _http(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=20)
        raw = json.loads(txt or "[]")
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(raw, list):
        return []
    return _bars([{"date": x.get("day"), "open": x.get("open"), "high": x.get("high"),
                   "low": x.get("low"), "close": x.get("close"),
                   "volume": x.get("volume")} for x in raw])


# ---------------- 雪球（第 1 顺位：实测最快且 A股+港股全覆盖） ----------------
_XQ_SESS = None
_XQ = {"auth_ts": 0.0}
_XQ_LOCK = threading.Lock()
_XQ_SYM = {"sh000001": "SH000001", "sh000300": "SH000300", "sh000016": "SH000016",
           "sh000905": "SH000905", "sh000852": "SH000852", "sz399006": "SZ399006",
           "sh000688": "SH000688", "hsi": "HKHSI", "hstech": "HKHSTECH"}


def _xq_sess():
    """雪球要求先访问首页拿 xq_a_token cookie（无 token 会返回 400）。

    ★ 必须整体加锁：prefetch 是 6 线程并发，若不锁会有 6 个线程同时建 session +
      同时认证 → 互相把 cookie 冲掉，表现为「单独测 100% 成功、并发时全失败」。
    """
    global _XQ_SESS
    with _XQ_LOCK:
        if _XQ_SESS is None:
            import requests
            from requests.adapters import HTTPAdapter
            s = requests.Session()
            s.trust_env = False
            ad = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
            s.mount("https://", ad)
            s.mount("http://", ad)
            _XQ_SESS = s
        if time.time() - _XQ["auth_ts"] > 1800:
            try:
                _XQ_SESS.get("https://xueqiu.com/", headers={"User-Agent": _UA}, timeout=12)
                _XQ["auth_ts"] = time.time()
            except Exception:  # noqa: BLE001 —— 认证失败也记个短冷却，避免线程风暴
                _XQ["auth_ts"] = time.time() - 1730
        return _XQ_SESS


def _xueqiu_kline(code: str, n: int = 2000) -> list:
    """雪球日K（前复权）。symbol 形如 SH000300 / HKHSI。

    返回列：timestamp, volume, open, high, low, close, chg, percent
    """
    sym = _XQ_SYM.get(code)
    if not sym:
        return []
    cnt = min(max(int(n), 60), 2000)
    url = ("https://stock.xueqiu.com/v5/stock/chart/kline.json?symbol=%s"
           "&begin=%d&period=day&type=before&count=-%d&indicator=kline"
           % (sym, int(time.time() * 1000), cnt))
    try:
        r = _xq_sess().get(url, headers={"User-Agent": _UA,
                                         "Referer": "https://xueqiu.com/"}, timeout=18)
        j = json.loads(r.text or "{}")
    except Exception:  # noqa: BLE001
        return []
    d = j.get("data") or {}
    cols = d.get("column") or []
    items = d.get("item") or []
    if not items or not cols:
        return []
    try:
        i_t, i_v = cols.index("timestamp"), cols.index("volume")
        i_o, i_h, i_l, i_c = (cols.index("open"), cols.index("high"),
                              cols.index("low"), cols.index("close"))
    except ValueError:
        return []
    tz = _dt.timezone(_dt.timedelta(hours=8))
    rows = []
    for it in items:
        try:
            ts = int(it[i_t]) / 1000.0
            day = _dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
            rows.append({"date": day, "open": float(it[i_o]), "high": float(it[i_h]),
                         "low": float(it[i_l]), "close": float(it[i_c]),
                         "volume": float(it[i_v] or 0), "hold": 0.0})
        except (TypeError, ValueError, IndexError):
            continue
    return _bars(rows)


def _tx_kline(code: str, n: int = 2000) -> list:
    """腾讯 ifzq 日K（字段序 date,open,close,high,low,volume）—— 第 1 级源。

    ★ 实测上限 2000 根：n>2000 时接口返回空（不是截断），故超限自动降级到 1200。
    ★ **不做内部重试**：腾讯限流时是「整体返回空」，重试只会白等 0.5s/次。
      实测 force 冷启动 18 个源原本要 23s，去掉重试后降到 ~10s；
      失败由 `_fetch_hist` 的多级降级链接手（雪球/新浪），那里才有健康度记忆。
    """
    nn = n if n <= 2000 else 2000
    url = ("https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
           % (code, max(int(nn), 60)))
    try:
        txt = _http(url, timeout=12)
        j = json.loads(txt or "{}")
    except Exception:  # noqa: BLE001
        return []
    node = (j.get("data") or {}).get(code) or {}
    rows = node.get("qfqday") or node.get("day") or []
    buf = []
    for r in rows:
        try:
            buf.append({"date": str(r[0])[:10], "open": float(r[1]), "close": float(r[2]),
                        "high": float(r[3]), "low": float(r[4]),
                        "volume": float(r[5]) if len(r) > 5 else 0.0, "hold": 0.0})
        except (TypeError, ValueError, IndexError):
            continue
    return _bars(buf)


_HIST_STALE: dict = {}
_FALLBACK: dict = {}

# 指数日K源优先级 —— 按 2026-10-30 实测「有效性 + 效率」排序，而不是按厂商名气：
#   雪球   : ~0.08s/个（首次认证 1.3s），A 股+港股都覆盖，2000 根，现货口径 ← 最优
#   腾讯   : ~0.2s/个，但会整体返回空（实测 hkHSI/sh000001 同时挂数十分钟）
#   新浪   : ~1.4s/个，稳，仅 A 股，2000 根
# 失败 2 次 → 冷却 10 分钟自动降到末位；成功即清零。这样「谁现在好用就用谁」。
_SRC_ORDER = ("xq", "tx", "sina")
_SRC_FAILS = {k: 0 for k in _SRC_ORDER}
_SRC_COOL = {k: 0.0 for k in _SRC_ORDER}
_SRC_LOCK = threading.Lock()
_SRC_LABEL = {"xq": "雪球", "tx": "腾讯", "sina": "新浪"}


def _src_try(name: str, a: dict):
    """返回 bars=成功 / [] =失败 / **None = 该源不适用**（不记失败，避免误冷却）。"""
    if name == "xq":
        if a["key"] not in _XQ_SYM:
            return None
        return _xueqiu_kline(a["key"])
    if name == "tx":
        return _tx_kline(a["sym"])
    if name == "sina":
        if not re.fullmatch(r"(sh|sz)\d{6}", a["sym"]):
            return None
        return _sina_index_kline(a["sym"])
    return None


def _src_order(now: float) -> list:
    with _SRC_LOCK:
        return sorted(_SRC_ORDER, key=lambda s: (1 if _SRC_COOL[s] > now else 0,
                                                 _SRC_FAILS[s],
                                                 _SRC_ORDER.index(s)))


def _src_note(name: str, ok: bool, now: float) -> None:
    with _SRC_LOCK:
        if ok:
            _SRC_FAILS[name] = 0
            _SRC_COOL[name] = 0.0
        else:
            _SRC_FAILS[name] += 1
            if _SRC_FAILS[name] >= 2:
                _SRC_COOL[name] = now + 600.0


def _fetch_hist(a: dict, force: bool = False) -> list:
    """多级降级取历史日K：按实时健康度在 雪球 / 腾讯 / 新浪 之间切换。"""
    k = a["kind"]
    if k == "cboe":
        return _cboe(a["sym"], force=force)
    if k == "sina_gf":
        return _sina_global(a["sym"])
    if k == "sina_inner":
        return _sina_inner(a["sym"])

    now = time.time()
    order = _src_order(now)
    for s in order:
        bars = _src_try(s, a)
        if bars is None:      # 该源不覆盖这个标的，跳过且不计失败
            continue
        _src_note(s, bool(bars), now)
        if bars:
            with _LOCK:
                if s == order[0]:
                    _FALLBACK.pop(a["key"], None)
                else:
                    _FALLBACK[a["key"]] = "日K走%s" % _SRC_LABEL[s]
            return bars
    # 港股最后兜底：恒指期货连续（口径略差）
    if a["key"] == "hsi":
        bars = _sina_global("HSI")
        if bars:
            with _LOCK:
                _FALLBACK["hsi"] = "日K走新浪恒指期货"
            return bars
    return []


def history(key: str, force: bool = False) -> list:
    """统一历史日K（升序）。30 分钟内存缓存 + 落盘兜底。

    ★ 三级降级：内存 → 网络（含各源自身降级）→ 磁盘缓存。
      网络失败时用磁盘里的旧数据继续服务（标 _HIST_STALE），
      绝不能因为一个数据源抖动就把整个页面打成空白。
    """
    a = AMAP.get(key)
    if not a:
        return []
    now = time.time()
    with _LOCK:
        hit = _HIST_CACHE.get(key)
        if hit and not force and now - hit[0] < _ttl_hist():
            return hit[1]

    bars = _fetch_hist(a, force=force)
    if bars:
        _cache_write("hist_%s.json" % key, bars[-2400:])
        with _LOCK:
            _HIST_CACHE[key] = (now, bars)
            _HIST_STALE.pop(key, None)
        return bars

    with _LOCK:
        hit = _HIST_CACHE.get(key)
    if hit:
        with _LOCK:
            _HIST_STALE[key] = True
        return hit[1]

    disk = _cache_read_any("hist_%s.json" % key)
    if disk:
        with _LOCK:
            _HIST_CACHE[key] = (now, disk)
            _HIST_STALE[key] = True
        return disk
    return []


# ============ 分时（分钟线）============
# 为什么必须补这一层
# ----------------
# A50 与布伦特是**跨时段**品种：A 股 15:00 收盘后它们还在动，而日线要到次日
# 才生成新的一根。只靠「快照价 vs 昨收」看不出「收盘后这段涨了多少」，
# 而那恰恰是次日开盘最直接的先行信息。
#
# 数据源（2026-11-06 实测）：
# · 外盘：GlobalFuturesService.getGlobalFuturesMinLine（1 分钟，含 T+1 时段）
#   ★ 首行是 10 字段的「段落头」（日期/交易所/起始时间/…），后续行才是 6 字段，
#     直接按 6 字段解析会把首行错位成 ["2026-11-07", "12752.000", ...]。
# · 内盘：InnerFuturesNewService.getFewMinLine?type=1（1 分钟 K，返回最近几天）
_MIN_JSONP = re.compile(r"var\s+_\s*=\s*\((.*)\)\s*;?\s*$", re.S)
_MIN_CACHE: dict = {}
_TTL_MIN = 60.0


def _jsonp(txt: str):
    if not txt:
        return None
    m = _MIN_JSONP.search(txt.strip())
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


def _min_outer(sym: str) -> list:
    # ★ 这里不能用 % 格式化：URL 里的 `var%20_=` 含 %2，会被当成格式符报
    #   "unsupported format character '_'"。用字符串拼接。
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
           "GlobalFuturesService.getGlobalFuturesMinLine?symbol=" + sym)
    txt = _http(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=15)
    data = _jsonp(txt)
    if not isinstance(data, dict):
        return []
    rows = data.get("minLine_1d") or []
    out = []
    for r in rows:
        if not isinstance(r, list) or len(r) < 2:
            continue
        if len(r) >= 10:
            # 段落头：["2026-11-07", 开?, "sgxq", "", "17:00", 价, 量, 持仓, 均价, 时间戳]
            px, tm, ts = _f(r[5]), str(r[4]), str(r[9])
        elif len(r) >= 6:
            # 常规行：["17:01", 价, 量, 持仓, 均价, 时间戳]
            px, tm, ts = _f(r[1]), str(r[0]), str(r[5])
        else:
            continue
        if px is None:
            continue
        out.append({"t": tm[-5:] if len(tm) >= 5 else tm, "p": px, "ts": ts})
    return out


def _min_inner(sym: str) -> list:
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
           "InnerFuturesNewService.getFewMinLine?symbol=" + sym + "&type=1")
    txt = _http(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=15)
    data = _jsonp(txt)
    if not isinstance(data, list):
        return []
    out = []
    for r in data:
        if not isinstance(r, dict):
            continue
        d = str(r.get("d") or "")
        px = _f(r.get("c"))
        if not d or px is None:
            continue
        out.append({"t": d[11:16], "p": px, "ts": d, "date": d[:10],
                    "v": _f(r.get("v"))})
    return out


def _minute_of(t) -> Optional[int]:
    """把 "15:00" / "9:05" 转成当日分钟数。解析不了返回 None。

    ★ 不要直接拿字符串比大小：`"9:05" >= "15:00"` 在字典序下是 **True**
      （因为 '9' > '1'），一条没补零的时间戳会被判成「下午三点之后」，
      整个隔夜段的基准点就跑偏了。转成分钟数比较才稳。
    """
    try:
        parts = str(t or "").strip().split(":")
        if len(parts) < 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 24 and 0 <= m < 60):
            return None
        return h * 60 + m
    except (ValueError, TypeError):
        return None


def _overnight(points: list) -> Optional[dict]:
    """A 股收盘（15:00）之后到最新一笔的涨跌 —— 这才是「隔夜漂移」。

    ★ 不能用「最后一个日期」来圈定范围：布伦特 ICE 时段是 09:00 → 次日 06:00（北京时间），
      凌晨以后数据里的 `ts[:10]` 已经是**第二天**，而第二天当然没有 15:00 的点，
      于是每天 00:00~15:00 这段时间里「隔夜漂移」会凭空消失（实测 00:15 踩到）。
      正确做法：找「时间从 <15:00 跨进 >=15:00」的那个边界点，取最后一个。
      这样无论数据跨不跨日，基准都落在最近的 15:00（或该时段的第一个点，如 A50 的 17:00）。
    """
    if not points:
        return None
    base = None
    prev_mm = -1
    for i, p in enumerate(points):
        mm = _minute_of(p.get("t"))
        if mm is None:
            continue
        if mm >= 900 and prev_mm < 900:      # 900 = 15:00
            base = p
        prev_mm = mm
    if base is None:
        return None
    last = points[-1]
    bp, lp = base["p"], last["p"]
    if not (bp and lp):
        return None
    hours = None
    try:
        a = _dt.datetime.strptime(str(base.get("ts"))[:19], "%Y-%m-%d %H:%M:%S")
        b = _dt.datetime.strptime(str(last.get("ts"))[:19], "%Y-%m-%d %H:%M:%S")
        hours = round((b - a).total_seconds() / 3600.0, 1)
    except (ValueError, TypeError):
        pass
    return {"base_time": base["t"], "base_price": bp, "last_price": lp,
            "pct": round((lp / bp - 1) * 100, 3), "hours": hours,
            "base_date": str(base.get("ts") or "")[:10],
            "date": str(last.get("ts") or "")[:10]}


def mins(key: str, force: bool = False) -> dict:
    """单指标分时。A50 / 布伦特走外盘接口，IF/IH/IC/IM/T 走内盘接口。"""
    a = AMAP.get(key)
    if not a:
        return {"ok": False, "err": "未知指标", "points": []}
    sym = a.get("sym") or ""
    if a["kind"] not in ("sina_gf", "sina_inner"):
        return {"ok": False, "err": "%s 无分时数据（指数日线口径）" % a["short"],
                "points": []}
    now = time.time()
    with _LOCK:
        hit = _MIN_CACHE.get(key)
        if hit and not force and now - hit[0] < _TTL_MIN:
            return hit[1]

    try:
        pts = _min_outer(sym) if a["kind"] == "sina_gf" else _min_inner(sym)
    except Exception as e:  # noqa: BLE001
        pts = []
        err = str(e)[:120]
    else:
        err = ""

    if not pts:
        return {"ok": False, "err": err or "分时源暂不可用", "key": key,
                "name": a["name"], "short": a["short"], "points": []}

    # 内盘接口返回好几天 → 只留最后一个交易日
    if a["kind"] == "sina_inner":
        last_day = str(pts[-1].get("ts") or "")[:10]
        pts = [p for p in pts if str(p.get("ts") or "")[:10] == last_day]

    # 降采样到 ≤240 点，前端 sparkline 不需要 1000 个点
    step = max(1, len(pts) // 240)
    thin = pts[::step]
    if thin and thin[-1] is not pts[-1]:
        thin.append(pts[-1])

    first_p = pts[0]["p"]
    ov = _overnight(pts) if a["kind"] == "sina_gf" else None
    res = {"ok": True, "key": key, "name": a["name"], "short": a["short"],
           "dec": a.get("dec", 2), "unit": a.get("unit", ""),
           "points": [{"t": p["t"], "p": round(p["p"], a.get("dec", 2) + 1)}
                      for p in thin],
           "n": len(pts),
           "day": str(pts[-1].get("ts") or "")[:10],
           "first": first_p, "last": pts[-1]["p"],
           "intraday_pct": round((pts[-1]["p"] / first_p - 1) * 100, 3) if first_p else None,
           "overnight": ov,
           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    with _LOCK:
        _MIN_CACHE[key] = (now, res)
    return res


# 首屏预热清单（并发拉取，避免 10 个源串行等 20s+）
_PREFETCH = ["vix", "vix9d", "brent", "a50", "hsi", "hstech",
             "sh000001", "sh000300", "sh000016", "sh000905", "sh000852",
             "sz399006", "sh000688", "IF0", "IH0", "IC0", "IM0", "T0"]


def prefetch(keys: Optional[list] = None, workers: int = 8, force: bool = False) -> None:
    """并发预热历史日K（已缓存的 key 会直接命中，不发请求）。

    ★ 每完成一个源就更新一次进度，前端轮询 `/api/idxhub/progress` 画进度条。
      用户看不到「卡在 0% 三秒」的空白，才知道程序在干活。
    """
    ks = keys or _PREFETCH
    todo = []
    now = time.time()
    for k in ks:
        with _LOCK:
            hit = _HIST_CACHE.get(k)
        if force or not (hit and now - hit[0] < _ttl_hist()):
            todo.append(k)
    if not todo:
        _prog(1, 1, "本地缓存已就绪", active=False)
        return
    total = len(todo)
    _prog(0, total, "准备拉取 %d 个数据源" % total)
    done = [0]
    lk = threading.Lock()

    def _one(k):
        try:
            history(k, force=force)
        finally:
            with lk:
                done[0] += 1
                _prog(done[0], total, "已拉取 %d/%d · %s"
                      % (done[0], total, AMAP[k]["short"]))

    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(max_workers=min(workers, total)) as ex:
            list(ex.map(_one, todo))
    except Exception:  # noqa: BLE001 —— 预热失败不影响后续按需拉取
        pass
    _prog(total, total, "数据已就绪", active=False)


# ============ 拉取进度（前端轮询画进度条） ============
_PROG = {"done": 0, "total": 0, "msg": "", "ts": 0.0, "active": False}


def _prog(done: int, total: int, msg: str, active: bool = True) -> None:
    with _LOCK:
        _PROG.update({"done": int(done), "total": int(total), "msg": str(msg),
                      "ts": time.time(), "active": bool(active)})


def progress() -> dict:
    with _LOCK:
        p = dict(_PROG)
    if p["active"] and time.time() - p["ts"] > 40:
        p["active"] = False          # 异常中断的兜底：40 秒没动静就当结束
    p["pct"] = round(p["done"] / p["total"] * 100, 1) if p["total"] else 0.0
    return p


def mark_start(msg: str = "正在连接数据源…") -> None:
    """请求一进来就把进度置为 active。

    ★ 不置的话，前端第一次轮询（220ms）读到的还是「上一次已完成」的状态
      → 进度条刚显示就被收起，用户根本看不到（2026-11-06 实测踩到）。
    """
    _prog(0, 1, msg)


# ============ 指标计算 ============
def _series(bars: list) -> tuple:
    dates = [b["date"] for b in bars]
    close = np.array([b["close"] for b in bars], dtype=float)
    high = np.array([b["high"] for b in bars], dtype=float)
    low = np.array([b["low"] for b in bars], dtype=float)
    hold = np.array([b.get("hold") or 0 for b in bars], dtype=float)
    return dates, close, high, low, hold


def _ret(arr: np.ndarray, n: int) -> np.ndarray:
    """n 日收益率（%），前 n 个为 nan。★ 分母为 0（如期货持仓缺失）必须显式置 nan，
    否则 numpy 会算成 inf 并一路污染分位数与打分。"""
    out = np.full(len(arr), np.nan)
    if len(arr) > n:
        den = arr[:-n].astype(float).copy()
        den[den == 0] = np.nan
        out[n:] = (arr[n:] / den - 1) * 100
    return out


def _roll_pctile(arr: np.ndarray, win: int = 750) -> np.ndarray:
    """滚动分位（0~100）：当前值在过去 win 根内的百分位。无未来函数。"""
    n = len(arr)
    out = np.full(n, np.nan)
    for i in range(n):
        v = arr[i]
        if not math.isfinite(v):
            continue
        s = arr[max(0, i - win + 1): i + 1]
        s = s[np.isfinite(s)]
        if len(s) < 20:
            continue
        out[i] = float((s <= v).mean() * 100)
    return out


def _sma(arr: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    if len(arr) >= n:
        c = np.cumsum(np.insert(arr, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _align_prev(src_dates: list, src_vals: np.ndarray, targets: list) -> np.ndarray:
    """把 src 序列按「严格早于 target」的最后一根对齐（隔夜已知口径，防未来函数）。"""
    out = np.full(len(targets), np.nan)
    if not src_dates:
        return out
    for i, d in enumerate(targets):
        j = bisect.bisect_left(src_dates, d) - 1     # 严格小于 d 的最后一个
        if j >= 0:
            out[i] = src_vals[j]
    return out


def _align_same(src_dates: list, src_vals: np.ndarray, targets: list,
                ffill: bool = True) -> np.ndarray:
    """同日对齐。

    `ffill=True`（默认）：目标日找不到就用**前一个交易日的值**。
      这是跨市场对齐必须的 —— 港股休市而 A 股开市时，港股的价格就是前收，
      留 NaN 会让 HSI/恒生科技整条序列断掉。
    `ffill=False`：严格同日，找不到就 NaN。
      ★ 期现基差必须用这个：期货缺一天却拿前一天的期货价去配当天的现货价，
        算出来的「基差」是两个不同时点的价差，比缺数据更糟。
    """
    out = np.full(len(targets), np.nan)
    for i, d in enumerate(targets):
        j = bisect.bisect_left(src_dates, d)
        if j < len(src_dates) and src_dates[j] == d:
            out[i] = src_vals[j]
        elif ffill and j - 1 >= 0:
            out[i] = src_vals[j - 1]
    return out


def _basis(spot_bars: list, fut_bars: list) -> dict:
    """期现基差序列：同日对齐，返回 dict of arrays。

    主连换月跳空检测
    ----------------
    ★ 旧判据 `|Δbasis| > 1.2 且 |fpct − spct| < 0.4` 是**恒不成立**的：
      因为 basis_pct = (F/S − 1)×100，一阶展开就有 Δbasis_pct ≈ fpct − spct，
      两个条件互相排斥。实测四个品种各 2000 个交易日，命中 **0 次** ——
      也就是说「换月跳空会被检测并剔除」这句话从来没兑现过，
      `_sig_basis_extreme` 里那句 `if b["roll"][i]: continue` 一直是死代码。

    新判据：换月是「基差水平的**持久位移**」，而真实的大波动会回归。
      跳变日之后 4 天的基差中位数 − 之前 4 天的中位数，若与跳变同号、
      且保留了跳变幅度的 70% 以上，才判为换月。
      实测命中 IF 6 / IH 7 / IC 9 / IM 12 次（2000 天），
      即「价差大到足以污染基差」的那几次，而不是每月无差别标记。
    """
    sd, sp, *_ = _series(spot_bars)
    fd, fp, _fh, _fl, fhold = _series(fut_bars)
    idx = {d: i for i, d in enumerate(sd)}
    dates, s_arr, f_arr, f_hold = [], [], [], []
    for j, d in enumerate(fd):
        i = idx.get(d)
        if i is None:
            continue
        dates.append(d)
        s_arr.append(sp[i])
        f_arr.append(fp[j])
        f_hold.append(fhold[j])
    s_arr = np.array(s_arr, dtype=float)
    f_arr = np.array(f_arr, dtype=float)
    f_hold = np.array(f_hold, dtype=float)
    n = len(dates)
    basis = np.full(n, np.nan)
    basis_pct = np.full(n, np.nan)
    ok = s_arr > 0
    basis[ok] = f_arr[ok] - s_arr[ok]
    basis_pct[ok] = (f_arr[ok] / s_arr[ok] - 1) * 100
    spct = np.full(n, np.nan)
    fpct = np.full(n, np.nan)
    if n > 1:
        spct[1:] = (s_arr[1:] / s_arr[:-1] - 1) * 100
        fpct[1:] = (f_arr[1:] / f_arr[:-1] - 1) * 100

    ROLL_THR = 1.2          # 基差跳变阈值（pp）
    ROLL_SUSTAIN = 0.7      # 位移至少保留跳变幅度的 70%
    ROLL_WIN = 4
    rollover = np.zeros(n, dtype=bool)
    for i in range(1, n):
        jump = basis_pct[i] - basis_pct[i - 1]
        if not np.isfinite(jump) or abs(jump) < ROLL_THR:
            continue
        pre = basis_pct[max(0, i - ROLL_WIN):i]
        post = basis_pct[i:min(n, i + ROLL_WIN)]
        pre = pre[np.isfinite(pre)]
        post = post[np.isfinite(post)]
        if len(pre) < 2 or len(post) < 2:
            # 最后两天没有「未来窗口」，只能用更严的单边阈值兜底
            if abs(jump) > 2.0:
                rollover[i] = True
            continue
        shift = float(np.median(post) - np.median(pre))
        if np.sign(shift) == np.sign(jump) and abs(shift) >= abs(jump) * ROLL_SUSTAIN:
            rollover[i] = True

    # 平滑基差（5 日中位数）抑制换月残余噪声
    smooth = np.full(n, np.nan)
    for i in range(n):
        s = basis_pct[max(0, i - 4): i + 1]
        s = s[np.isfinite(s)]
        if len(s):
            smooth[i] = float(np.median(s))
    return {"dates": dates, "basis": basis, "basis_pct": basis_pct,
            "smooth": smooth, "rollover": rollover, "spot": s_arr, "fut": f_arr,
            "hold": f_hold, "spct": spct, "fpct": fpct}


def _basis_rank(bp: np.ndarray, win: int = 750) -> np.ndarray:
    """基差分位的「绝对强度」：|basis_pct| 在历史上的分位（0~100）。"""
    ab = np.abs(bp)
    return _roll_pctile(ab, win)


# ============ 合约到期日（年化基差的分母）============
# ★ 原来按「剩余 30 天」固定近似，对 IF/IH/IC/IM 是同一个分母 —— 但四个品种的
#   到期日相同（都是第三个周五），真正的错误在于**忽略了当月/次月的切换**：
#   月初主力是当月（剩 20 天）、月末主力已移到次月（剩 40 天），
#   一律用 30 天会让年化基差在换月前后凭空跳 ±30%。
#   正确做法：① 有中金所当日成交数据 → 用成交量最大的合约（真主力）；
#             ② 没有 → 用「第三个周五」+ 提前 7 个自然日移仓的规则推算。
def _nth_friday(year: int, month: int, n: int = 3) -> _dt.date:
    d = _dt.date(year, month, 1)
    return d + _dt.timedelta(days=(4 - d.weekday()) % 7 + 7 * (n - 1))


def _next_month(year: int, month: int) -> tuple:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _cffex_cached() -> Optional[dict]:
    """读中金所日行情的**落盘缓存**（绝不触网）。期权模块拉过一次就有了。"""
    today = _dt.date.today()
    for back in range(0, 12):
        d = today - _dt.timedelta(days=back)
        c = _cache_read_any("cffex_%s.json" % d.strftime("%Y%m%d"))
        if c:
            return c
    return None


def _contract_of(fk: str) -> dict:
    """当前主力合约与剩余到期天数。

    返回 {code, yymm, expiry, days, src}
    src = 'cffex'（成交量最大，实测口径）或 'rule'（第三个周五规则推算）
    """
    today = _dt.date.today()
    pre = fk.rstrip("0")            # IF0 → IF
    cached = _cffex_cached()
    if cached:
        fl = (cached.get("futures") or {}).get(pre, [])
        if fl:
            main = max(fl, key=lambda x: x.get("vol") or 0)
            yymm = main.get("yymm") or ""
            try:
                y, m = 2000 + int(yymm[:2]), int(yymm[2:4])
                exp = _nth_friday(y, m)
                # 已过到期日 → 说明缓存是旧的，退回规则推算
                if (exp - today).days >= 0:
                    return {"code": main.get("code"), "yymm": yymm,
                            "expiry": exp.isoformat(), "days": (exp - today).days,
                            "src": "cffex"}
            except (ValueError, IndexError):
                pass
    # 规则推算：当月第三个周五前 7 天内视为已移仓到次月
    exp = _nth_friday(today.year, today.month)
    roll = 7
    if (exp - today).days < roll:
        y, m = _next_month(today.year, today.month)
        exp = _nth_friday(y, m)
    return {"code": "%s%02d%02d" % (pre, exp.year % 100, exp.month), "yymm": "",
            "expiry": exp.isoformat(), "days": (exp - today).days, "src": "rule"}


def _ann_basis(basis_pct: Optional[float], days: Optional[int]) -> tuple:
    """年化基差（线性，ACT/365）。返回 (值, 是否可展示)。

    ★ 剩余 <3 个自然日时年化会把噪声放大 100 倍以上（0.05% 的基差能算出 6%），
      此时年化没有意义 → 返回 None，由前端显示「临近交割」而不是一个假数字。
    """
    if basis_pct is None or not days or days < 3:
        return None, False
    return round(basis_pct * 365.0 / days, 2), True


# ============ 维度打分（0~100，越高越危险） ============
def _nz(x, d=50.0) -> float:
    return float(x) if (x is not None and math.isfinite(float(x))) else d


def _dim_scores(env: dict, i: int) -> dict:
    vix_q = _nz(env["vix_pct"][i])
    vix_r5 = _nz(env["vix_ret5"][i], 0.0)
    vix_spd = _nz(env["vix_ret5_pct"][i])
    oil_q = _nz(env["brent_pct"][i])
    oil_r20 = _nz(env["brent_ret20"][i], 0.0)
    oil_spd = _nz(env["brent_ret20_pct"][i])

    panic = 0.55 * vix_q + 0.25 * vix_spd + 0.20 * min(max(vix_r5 * 2.0 + 50, 0), 100)

    commodity = 0.50 * oil_q + 0.30 * oil_spd + 0.20 * min(max(oil_r20 * 1.6 + 50, 0), 100)

    # 期现结构：贴水越深/升水越高 → 越紧张（用四品种 |基差| 分位均值）
    ranks = [env["basis"][k]["abs_rank"][i] for k in env["basis"]]
    ranks = [r for r in ranks if math.isfinite(r)]
    basis_s = float(np.mean(ranks)) if ranks else 50.0

    # 跨境分歧：恒生-沪深300 差值、A50-沪深300 差值、VIX 与 A 股反向
    gap_hk = _nz(env["hsi_ret1"][i], 0.0) - _nz(env["hs300_ret1"][i], 0.0)
    gap_a50 = _nz(env["a50_ret1"][i], 0.0) - _nz(env["hs300_ret1"][i], 0.0)
    cross = min(abs(gap_hk) * 25 + abs(gap_a50) * 25, 100)

    ma20 = _nz(env["hs300_ma20"][i], 0.0)
    ma60 = _nz(env["hs300_ma60"][i], 0.0)
    px = _nz(env["hs300"][i], 0.0)
    pxv = px if px > 0 else 1.0
    below = 0.0
    if ma20 > 0 and px < ma20:
        below += 25
    if ma60 > 0 and px < ma60:
        below += 30
    if ma20 > 0 and ma60 > 0 and ma20 < ma60:
        below += 20
    mom20 = _nz(env["hs300_ret20"][i], 0.0)
    # ★ 不要直接相加：below 最高 75 + 动量项最高 50 会轻易顶到 100（永远「满格」），
    # 分项加权后最大约 90，保留区分度。
    trend = min(max(below * 0.62 + (25 - mom20 * 1.6), 0), 100)

    # 政策/流动性：10Y 国债期货 20 日动量（涨 = 收益率下行 = 宽货币）× 股市方向
    # ★ 用连续打分而不是阶梯阈值：国债期货 20 日涨 0.2% 也算「偏松」，
    #   若硬卡 0.4% 会把「温和宽松 + 股市大跌」这种最常见的传导失效漏掉。
    ease = _nz(env["t_ret20"][i], 0.0) if env.get("t_ret20") is not None else 0.0
    risk = _nz(env["onshore_ret20"][i], 0.0) if env.get("onshore_ret20") is not None else 0.0
    ease_z = max(-1.0, min(1.0, ease / 1.5))     # 国债期货 ±1.5% → ±1
    risk_z = max(-1.0, min(1.0, risk / 10.0))    # 宽基 ±10% → ±1
    if ease_z * risk_z < 0:
        sev = abs(ease_z) * 0.5 + abs(risk_z) * 0.5
        policy = (62.0 if ease_z > 0 else 58.0) + sev * 32.0   # 宽松+跌=失效 / 收紧+涨=脆弱
    elif risk_z > 0:
        policy = 38.0 - ease_z * 10.0                          # 同向上涨：货币越松越健康
    else:
        policy = 46.0 - ease_z * 6.0                           # 同向下跌：货币越松越可缓冲
    policy = min(max(policy, 0), 100)

    return {"panic": round(panic, 1), "commodity": round(commodity, 1),
            "basis": round(basis_s, 1), "cross": round(cross, 1),
            "trend": round(trend, 1), "policy": round(policy, 1)}


BASE_W = {"panic": 0.20, "commodity": 0.13, "basis": 0.17,
          "cross": 0.18, "trend": 0.22, "policy": 0.10}
DIM_NAME = {"panic": "恐慌情绪", "commodity": "商品冲击", "basis": "期现结构",
            "cross": "跨境分歧", "trend": "境内趋势", "policy": "政策流动性"}
DIM_KEYS = ("panic", "commodity", "basis", "cross", "trend", "policy")


# ============ 权重标定（把「经验设定」换成「可核对的数字」）============
# 原本 6 维权重是拍出来的。拍出来的东西不一定错，但**必须知道它到底有没有信息**。
# 这里的做法与局限都写清楚：
#   · 目标函数：温度与未来 20 日沪深300 收益的 Spearman 秩相关（IC）。
#     温度越高越危险 ⇒ IC 应当为**负**。IC ≈ 0 就是没有预测力。
#   · 搜索：在 6 维单纯形上做 Dirichlet 随机采样（混合「全空间」与「经验值附近」），
#     取训练集 |IC| 最大者。
#   · 验证：**时间序列样本外**（前 70% 训练 / 后 30% 测试，中间留 20 日隔离带，
#     避免训练集的未来收益窗口与测试集重叠造成信息泄漏）。
#   · 若样本外 IC 没有改善、或符号翻转，就直说「权重不可辨识」，
#     并把经验权重继续当作默认 —— 不拿样本内最优冒充有效。
def _rank_np(x: np.ndarray) -> np.ndarray:
    """向量化秩（并列取平均秩），用于 Spearman。"""
    n = len(x)
    if n == 0:
        return x
    order = np.argsort(x, kind="mergesort")
    sx = x[order]
    diff = np.r_[True, sx[1:] != sx[:-1]]
    gidx = np.cumsum(diff) - 1
    counts = np.bincount(gidx)
    cum = np.cumsum(counts)
    start = cum - counts
    avg = start + (counts - 1) / 2.0
    out = np.empty(n, dtype=float)
    out[order] = avg[gidx]
    return out


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    # ★ 长度不等要返回 nan 而不是让 numpy 抛 ValueError：
    #   这个函数被 _ic / _corr 调用，上游理论上已对齐，但边界处不该靠上游保证。
    if len(a) < 3 or len(a) != len(b):
        return float("nan")
    sa, sb = np.std(a), np.std(b)
    if sa < 1e-12 or sb < 1e-12:
        return float("nan")
    return float(np.mean((a - a.mean()) * (b - b.mean())) / (sa * sb))


def _ic(temp: np.ndarray, ret_rank: np.ndarray, mask: np.ndarray) -> float:
    """温度与未来收益的秩相关（只用 mask 内的样本）。"""
    t = temp[mask]
    r = ret_rank[mask]
    if len(t) < 50:
        return float("nan")
    return _pearson(_rank_np(t), r)


def _dim_matrix(env: dict, min_i: int = 260) -> tuple:
    """逐日算出 6 维分数矩阵 D（n×6）。与实时判定共用 _dim_scores，口径一致。"""
    n = len(env["dates"])
    D = np.full((n, len(DIM_KEYS)), np.nan)
    for i in range(min_i, n):
        try:
            d = _dim_scores(env, i)
        except Exception:  # noqa: BLE001
            continue
        for j, k in enumerate(DIM_KEYS):
            D[i, j] = d.get(k, np.nan)
    return D


_CALIB_CACHE: dict = {"ts": 0.0, "data": None}
_CALIB_TTL = 12 * 3600
# ★ 缓存文件名带版本：结果结构变了必须换名，否则 12 小时里读到的还是旧结论
#   （加多期限/滚动折时踩过：新判决写了但页面显示的还是旧的「标定能掰正」）
_CALIB_FILE = "calib_weights_v3.json"


def calibrate(force: bool = False, n_search: int = 4000, horizon: int = 20) -> dict:
    """风险温度权重的历史标定（含样本外检验）。结果落盘，12 小时有效。"""
    now = time.time()
    with _LOCK:
        c = _CALIB_CACHE["data"]
        if not force and c and now - _CALIB_CACHE["ts"] < _CALIB_TTL:
            return c
    if not force:
        disk = _cache_read(_CALIB_FILE, _CALIB_TTL)
        if disk:
            with _LOCK:
                _CALIB_CACHE["ts"], _CALIB_CACHE["data"] = now, disk
            return disk

    t0 = time.time()
    env = build_env(force=force)
    if not env:
        return {"ok": False, "err": "历史数据不足，无法标定"}
    close = env["hs300"]
    n = len(close)
    D = _dim_matrix(env)
    # 未来 horizon 日收益（只用当日及之后可知的数据，不构成未来函数）
    fwd = np.full(n, np.nan)
    if n > horizon:
        fwd[: n - horizon] = close[horizon:] / close[: n - horizon] - 1
    # ★ 多期限：5 / 20 / 60 日各算一条。同一组权重在不同期限上的表现可能**方向相反**
    #   （20 日偏反转、60 日偏趋势），只报一个期限等于挑好看的给用户看。
    FWDS = {}
    for _h in (5, 20, 60):
        _f = np.full(n, np.nan)
        if n > _h:
            _f[: n - _h] = close[_h:] / close[: n - _h] - 1
        FWDS[_h] = _f
    valid = np.isfinite(fwd) & np.all(np.isfinite(D), axis=1)
    if valid.sum() < 400:
        return {"ok": False, "err": "有效样本不足（%d 天），无法标定" % int(valid.sum())}

    ret_rank = _rank_np(np.nan_to_num(fwd))
    idx_valid = np.where(valid)[0]
    cut = int(len(idx_valid) * 0.7)
    train_end = idx_valid[cut - 1]
    # ★ 隔离带：训练集最后一个样本的未来窗口不能压到测试集上
    train_mask = np.zeros(n, dtype=bool)
    train_mask[idx_valid[0]: max(train_end - horizon, idx_valid[0] + 1)] = True
    test_mask = np.zeros(n, dtype=bool)
    test_mask[idx_valid[cut]: idx_valid[-1] + 1] = True

    base_w = np.array([BASE_W[k] for k in DIM_KEYS], dtype=float)
    base_t = D @ base_w
    base_in = _ic(base_t, ret_rank, train_mask)
    base_out = _ic(base_t, ret_rank, test_mask)
    base_all = _ic(base_t, ret_rank, valid)

    # Dirichlet 采样：一半全空间、一半贴近经验值（避免采到全是一维的极端权重）
    rng = np.random.default_rng(20261106)
    half = n_search // 2
    W1 = rng.dirichlet(np.ones(len(DIM_KEYS)), size=half)
    W2 = rng.dirichlet(base_w * 40.0, size=n_search - half)
    W = np.vstack([W1, W2])
    # ★ 单维权重上限 0.35。不加约束时最优解会把 82% 押在 commodity 一个维度上
    #   （2026-11-06 实测），那是过拟合的典型形态：样本外 IC 好看，但换一段数据就散。
    MAX_W = 0.35
    keep = W.max(axis=1) <= MAX_W
    if keep.sum() < 300:                     # 采到的合规解太少 → 补采
        extra = rng.dirichlet(np.ones(len(DIM_KEYS)), size=n_search)
        W = np.vstack([W[keep], extra[extra.max(axis=1) <= MAX_W]])
    else:
        W = W[keep]
    if len(W) < 50:
        return {"ok": False, "err": "权重采样失败（约束过严）"}

    Dtr = D[train_mask]
    rtr = ret_rank[train_mask]
    if len(Dtr) < 200:
        return {"ok": False, "err": "训练样本不足"}
    temp_all = Dtr @ W.T                        # (n_train, n_search)
    # 逐列算秩相关：样本量不大（~1300），直接循环比造三维数组更省内存
    ics = np.full(W.shape[0], np.nan)
    for j in range(W.shape[0]):
        ics[j] = _pearson(_rank_np(temp_all[:, j]), rtr)
    if not np.isfinite(ics).any():
        return {"ok": False, "err": "权重搜索全部失效（数据退化）"}
    # ★ 温度越高越危险 ⇒ IC 应为负，所以取最小（最负）的那组
    best_j = int(np.nanargmin(ics))
    best_w = W[best_j]
    best_t = D @ best_w
    best_in = _ic(best_t, ret_rank, train_mask)
    best_out = _ic(best_t, ret_rank, test_mask)
    best_all = _ic(best_t, ret_rank, valid)

    # 分档检验（比 IC 更好懂）：按温度分 5 档，看未来 20 日平均收益是否单调
    def _buckets(temp):
        tt = temp[valid]
        rr = np.nan_to_num(fwd)[valid] * 100
        qs = np.quantile(tt, [0.2, 0.4, 0.6, 0.8])
        lab = np.digitize(tt, qs)
        out = []
        for b in range(5):
            m = lab == b
            out.append({"n": int(m.sum()),
                        "avg": round(float(rr[m].mean()), 2) if m.sum() else None,
                        "win": round(float((rr[m] > 0).mean() * 100), 1) if m.sum() else None})
        return out

    # 单维 IC：哪个维度真的有信息，哪个是纯噪声 —— 这个是标定最该给出的东西
    single = []
    for j, k in enumerate(DIM_KEYS):
        dj = D[:, j]
        m = valid & np.isfinite(dj)
        single.append({"key": k, "name": DIM_NAME[k],
                       "ic_all": round(_ic(dj, ret_rank, m), 4),
                       "ic_out": round(_ic(dj, ret_rank, m & test_mask), 4)})
    single.sort(key=lambda x: x["ic_all"])

    # 分年度 IC（看稳定性，而不是只看全样本一个数）
    years = np.array([int(str(d)[:4]) for d in env["dates"]])
    yearly = []
    for y in sorted(set(years[valid].tolist())):
        m = valid & (years == y)
        if m.sum() < 60:
            continue
        yearly.append({"year": y, "n": int(m.sum()),
                       "ic": round(_ic(base_t, ret_rank, m), 3),
                       "ic_best": round(_ic(best_t, ret_rank, m), 3)})

    # ---- 多期限：同一组权重在 5/20/60 日上的 IC ----
    def _split_masks(mh, h):
        """按某期限自己的有效样本重新切 70/30，隔离带宽度 = 该期限 h。"""
        iv = np.where(mh)[0]
        if len(iv) < 300:
            return None, None
        c = int(len(iv) * 0.7)
        te_end = iv[c - 1]
        tr = np.zeros(n, dtype=bool)
        tr[iv[0]: max(te_end - h, iv[0] + 1)] = True
        tm = np.zeros(n, dtype=bool)
        tm[iv[c]: iv[-1] + 1] = True
        return tr, tm

    multi = []
    for h in (5, 20, 60):
        fh = FWDS[h]
        mh = np.isfinite(fh) & np.all(np.isfinite(D), axis=1)
        rh = _rank_np(np.nan_to_num(fh))
        tr_m, te_m = _split_masks(mh, h)
        if tr_m is None:
            multi.append({"h": h, "n": int(mh.sum())})
            continue
        multi.append({"h": h, "n": int(mh.sum()),
                      "base_in": round(_ic(base_t, rh, tr_m), 4),
                      "base_out": round(_ic(base_t, rh, te_m), 4),
                      "base_all": round(_ic(base_t, rh, mh), 4),
                      "best_in": round(_ic(best_t, rh, tr_m), 4),
                      "best_out": round(_ic(best_t, rh, te_m), 4),
                      "best_all": round(_ic(best_t, rh, mh), 4)})

    # ---- 滚动多折（expanding window，3 折）----
    # 单一段 70/30 的结论很容易是「刚好这一段如此」。这里把测试期切成 3 段，
    # 每段都用它之前的数据当训练，看 best 权重是否**每一折都同号**。
    walk = []
    iv = np.where(valid)[0]
    m_all = len(iv)
    if m_all >= 500:
        edges = [int(m_all * (0.45 + 0.15 * i)) for i in range(4)]
        for i in range(3):
            a, b = edges[i], min(edges[i + 1], m_all - 1)
            if b - a < 40:
                continue
            tr_end = max(a - horizon, 1)
            te = np.zeros(n, dtype=bool)
            te[iv[a]: iv[b] + 1] = True
            walk.append({
                "fold": i + 1,
                "train": [env["dates"][int(iv[0])], env["dates"][int(iv[tr_end])]],
                "test": [env["dates"][int(iv[a])], env["dates"][int(iv[b])]],
                "n_test": int(te.sum()),
                "base_out": round(_ic(base_t, ret_rank, te), 4),
                "best_out": round(_ic(best_t, ret_rank, te), 4),
            })

    # ---- 判决：先看经验权重的**方向**，再谈优化 ----
    b0, b5 = _buckets(base_t)[0], _buckets(base_t)[-1]
    flipped = bool(np.isfinite(base_all) and base_all > 0.03)
    walk_n = len(walk)
    walk_best_neg = sum(1 for w in walk if (w.get("best_out") or 0) < 0)
    walk_base_pos = sum(1 for w in walk if (w.get("base_out") or 0) > 0)
    best_stable = (walk_n >= 2 and walk_best_neg >= walk_n - 1)
    base_stable = (walk_n >= 2 and walk_base_pos == walk_n)
    wb = " / ".join("%+.3f" % (w.get("best_out") or 0) for w in walk) if walk else "—"
    wbase = " / ".join("%+.3f" % (w.get("base_out") or 0) for w in walk) if walk else "—"

    if not np.isfinite(best_out) or abs(best_out) < 0.03:
        grade = "none"
        verdict = ("无论怎么调权重，样本外 IC 都接近零（best %+.3f）。"
                   "结论：这 6 个维度拼起来对未来 20 日收益**没有可辨识的预测力**，"
                   "温度只能当环境读数。" % (best_out if np.isfinite(best_out) else 0.0))
    elif flipped:
        grade = "flipped"
        core = ("**经验权重的方向是反的，而且在滚动 %d 折里%s**：温度越高，未来 20 日收益"
                "反而越好（全样本 IC %+.3f；最低温档均 %+.2f%%、胜率 %.0f%%，"
                "最高温档均 %+.2f%%、胜率 %.0f%%；滚动测试期 IC 分别 %s）。"
                "它在 A 股更像**均值回归指标**（恐慌尾部往往是反弹起点），"
                "不是「越高越危险」的风险计。"
                % (walk_n, "每一折都成立" if base_stable else "多数折成立",
                   base_all, b0["avg"] or 0, b0["win"] or 0,
                   b5["avg"] or 0, b5["win"] or 0, wbase))
        if walk_n and not best_stable:
            verdict = core + (
                "但「用标定权重把方向掰正」这件事**没通过检验**：同样这 %d 折里，"
                "标定权重的 IC 是 %s —— 只有 %d/%d 折是负的。"
                "单一的 70/30 分割给出 %+.3f，那只是恰好落在唯一那段有效区间上。"
                "所以结论是：温度可以当**反向读数**看，但**别用这组标定权重去做择时**。"
                % (walk_n, wb, walk_best_neg, walk_n, best_out))
        else:
            verdict = core + (
                "标定权重在 %d 折里同号且改善（%s），可以当作「把这套读数掰成能用的方向」"
                "的一个参考，但绝对水平仍弱（样本外 %+.3f），不足以单独择时。"
                % (walk_n, wb, best_out))
    elif abs(best_out) > abs(base_out) * 1.15 and np.sign(best_out) == np.sign(base_out):
        grade = "weak"
        verdict = ("标定权重在样本外同号且改善（IC %+.3f → %+.3f），但绝对水平仍弱，"
                   "只能作为环境读数的加权参考，不足以单独择时。"
                   % (base_out, best_out))
    else:
        grade = "unstable"
        verdict = ("标定权重在样本外**没有**跑赢经验权重（IC %+.3f → %+.3f），"
                   "通常意味着维度间共线、或信号在样本外衰减。"
                   "结论是「权重不可辨识」：继续用经验权重，但不要再声称它被优化过。"
                   % (base_out, best_out))

    # ---- 温度历史序列：让「71.3 高危」这个数字有参照 ----
    # ★ 71.3 是不是「高」，取决于它在自己历史上排第几，而不是我拍的 70 分线。
    #   这条序列用的是同一套 base 权重、同一份 env，所以与面板上的温度完全同源。
    _all = [i for i in range(n) if np.isfinite(base_t[i])]
    _tail = _all[-500:] if len(_all) > 500 else _all
    temp_series = [{"d": env["dates"][int(i)], "v": round(float(base_t[int(i)]), 1)}
                   for i in _tail]
    cur_t = float(base_t[-1]) if np.isfinite(base_t[-1]) else None
    temp_pct = temp_med = None
    if cur_t is not None and temp_series:
        tv = np.array([x["v"] for x in temp_series], dtype=float)
        temp_pct = float((tv <= cur_t).mean() * 100)
        temp_med = float(np.median(tv))

    res = {
        "ok": True,
        "n_days": int(valid.sum()), "horizon": horizon,
        "range": [env["dates"][int(idx_valid[0])], env["dates"][int(idx_valid[-1])]],
        "split": {"train": [env["dates"][int(idx_valid[0])], env["dates"][int(train_end)]],
                  "test": [env["dates"][int(idx_valid[cut])], env["dates"][int(idx_valid[-1])]],
                  "gap_days": horizon},
        "base": {"weights": {k: BASE_W[k] for k in DIM_KEYS},
                 "ic_in": round(base_in, 4), "ic_out": round(base_out, 4),
                 "ic_all": round(base_all, 4), "buckets": _buckets(base_t)},
        "best": {"weights": {k: round(float(best_w[j]), 4) for j, k in enumerate(DIM_KEYS)},
                 "ic_in": round(best_in, 4), "ic_out": round(best_out, 4),
                 "ic_all": round(best_all, 4), "buckets": _buckets(best_t)},
        "single": single,
        "multi": multi,
        "walk": walk,
        "temp": {"now": round(cur_t, 1) if cur_t is not None else None,
                 "pct": round(temp_pct, 1) if temp_pct is not None else None,
                 "med": round(temp_med, 1) if temp_med is not None else None,
                 "n": len(temp_series),
                 "series": temp_series},
        "max_weight": MAX_W,
        "yearly": yearly,
        "grade": grade, "verdict": verdict,
        "n_search": int(n_search),
        "elapsed": round(time.time() - t0, 2),
        "method": [
            "目标：温度与未来 N 日沪深300 收益的 Spearman IC（温度越高越危险 ⇒ 应为负）",
            "搜索：6 维单纯形上 %d 组 Dirichlet 采样（半数为全空间、半数为经验值附近），"
            "单维权重上限 %.0f%%" % (n_search, MAX_W * 100),
            "验证①：时间序列 70/30 分割，中间留 %d 日隔离带，防止训练集的未来收益窗口压到测试集" % horizon,
            "验证②：5 / 20 / 60 三个期限各算一遍 —— 方向一致才说明不是某个期限的偶然产物",
            "验证③：滚动 3 折 expanding-window，看同一组权重是不是**每一折**都同号",
            "判定：样本外 |IC| 需改善 15% 以上且同号，且滚动折不能大面积翻转",
        ],
        "caveats": [
            "IC 是**全样本统计**，不等于任何单一时点可用；IC=-0.1 也意味着 45% 的时间方向是错的",
            "5 / 20 / 60 日三个期限并列给出：只有方向一致，才说明信号不是某个期限的偶然产物；"
            "若三个期限符号互相打架，就不存在「一个温度」这种说法",
            "样本外给了 3 折 expanding-window（不再是单段），但折与折之间高度重叠，"
            "独立性有限，仍不足以称为稳健",
            "标定用的是「当前这 6 个维度」的组合，维度本身选得对不对，这里没有回答",
        ],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _cache_write(_CALIB_FILE, res)
    with _LOCK:
        _CALIB_CACHE["ts"], _CALIB_CACHE["data"] = now, res
    return res


def _weights(dims: dict, boost: dict) -> dict:
    """动态权重：某维度极端时上浮（倍率），其余等比压缩后归一。"""
    raw = {k: BASE_W[k] * float(boost.get(k, 1.0)) for k in BASE_W}
    tot = sum(raw.values())
    return {k: round(v / tot, 4) for k, v in raw.items()}


# ============ 环境构建（历史标定与当前判定共用同一份 env，保证口径一致） ============
_ENV_CACHE: dict = {"ts": 0.0, "data": None}


def build_env(force: bool = False) -> dict:
    now = time.time()
    with _LOCK:
        c = _ENV_CACHE["data"]
        if not force and c and now - _ENV_CACHE["ts"] < _ttl_env():
            return c
    base = history("sh000300")
    if not base:
        return {}
    dates = [b["date"] for b in base]
    _, hs300, _, _, _ = _series(base)

    def _prep(key):
        bars = history(key, force=force)
        if not bars:
            z = np.full(len(dates), np.nan)
            return z, z, z
        d, c, *_ = _series(bars)
        v = _align_same(d, c, dates) if key in ("sh000300",) else (
            _align_prev(d, c, dates) if key in ("vix", "vix9d", "brent") else _align_same(d, c, dates))
        return v, _ret(v, 1), _ret(v, 5)

    vix, vix_r1, vix_r5 = _prep("vix")
    vix9d, _, _ = _prep("vix9d")
    brent, brent_r1, brent_r5 = _prep("brent")
    brent_r20 = _ret(brent, 20)
    a50, a50_r1, _ = _prep("a50")
    hsi, hsi_r1, _ = _prep("hsi")
    hstech, hstech_r1, _ = _prep("hstech")
    # 政策/流动性代理：10 年期国债期货（价格涨 = 收益率下行 = 宽货币）
    t_bond, _, _ = _prep("T0")
    t_r5 = _ret(t_bond, 5)
    t_r20 = _ret(t_bond, 20)
    # 「境内宽基」综合（用户说的「宽金融指数」不能只看沪深300）：四个宽基等权
    ons = [hs300]
    for _k in ("sh000016", "sh000905", "sh000852"):
        _v, _, _ = _prep(_k)
        if np.isfinite(_v).sum() > 100:
            ons.append(_v)
    onshore = np.nanmean(np.vstack(ons), axis=0) if len(ons) > 1 else hs300

    env = {
        "dates": dates, "hs300": hs300, "hs300_ret1": _ret(hs300, 1),
        "hs300_ret5": _ret(hs300, 5), "hs300_ret20": _ret(hs300, 20),
        "hs300_ma20": _sma(hs300, 20), "hs300_ma60": _sma(hs300, 60),
        "hs300_ma120": _sma(hs300, 120),
        "onshore": onshore, "onshore_ret1": _ret(onshore, 1),
        "onshore_ret20": _ret(onshore, 20),
        "t_bond": t_bond, "t_ret5": t_r5, "t_ret20": t_r20,
        "vix": vix, "vix_ret1": vix_r1, "vix_ret5": vix_r5,
        "vix_pct": _roll_pctile(vix), "vix_ret5_pct": _roll_pctile(vix_r5),
        "vix9d": vix9d,
        "brent": brent, "brent_ret1": brent_r1, "brent_ret5": brent_r5,
        "brent_ret20": brent_r20, "brent_pct": _roll_pctile(brent),
        "brent_ret20_pct": _roll_pctile(brent_r20),
        "a50": a50, "a50_ret1": a50_r1, "hsi": hsi, "hsi_ret1": hsi_r1,
        "hstech": hstech, "hstech_ret1": hstech_r1,
        "basis": {},
    }
    for fk in ("IF0", "IH0", "IC0", "IM0"):
        ub = AMAP[fk]["under"]
        sb = history(ub)
        fb = history(fk)
        if not sb or not fb:
            continue
        b = _basis(sb, fb)
        # 把基差序列对齐到 A 股日期轴（期货与现货同市场，长度基本一致）
        bp = _align_same(b["dates"], b["basis_pct"], dates, ffill=False)
        sm = _align_same(b["dates"], b["smooth"], dates, ffill=False)
        roll = np.zeros(len(dates), dtype=bool)
        rd = b["dates"]
        for j, d in enumerate(rd):
            k = bisect.bisect_left(dates, d)
            if k < len(dates) and dates[k] == d:
                roll[k] = bool(b["rollover"][j])
        hold = _align_same(b["dates"], b["hold"], dates, ffill=False)
        env["basis"][fk] = {
            "pct": bp, "smooth": sm, "abs_rank": _basis_rank(bp), "roll": roll,
            "hold": hold, "hold_chg5": _ret(hold, 5),
            "fpct": _align_same(b["dates"], b["fpct"], dates, ffill=False),
            "spct": _align_same(b["dates"], b["spct"], dates, ffill=False),
            "under": ub,
        }
    with _LOCK:
        _ENV_CACHE["ts"], _ENV_CACHE["data"] = now, env
    return env


# ============ 历史标定 ============
def _bt(mask: np.ndarray, close: np.ndarray, min_n: int = 15) -> dict:
    """条件触发后的未来收益分布（用沪深300 作基准）。"""
    out = {}
    n = len(close)
    for h in HORIZONS:
        m = mask[: max(n - h, 0)]
        idx = np.where(m)[0]
        if len(idx) == 0:
            out[h] = {"n": 0}
            continue
        r = close[idx + h] / close[idx] - 1
        r = r[np.isfinite(r)] * 100
        if len(r) == 0:
            out[h] = {"n": 0}
            continue
        out[h] = {"n": int(len(r)), "win": round(float((r > 0).mean() * 100), 1),
                  "avg": round(float(r.mean()), 2), "med": round(float(np.median(r)), 2)}
    out["_min_n"] = min_n
    return out


def _pick_term(stats: dict, min_n: int = 15) -> tuple:
    """主期限 = |均值| 最大且样本够的期限；返回 (期限名, 说明, 期限小时长)。"""
    best_h, best_v = None, 0.0
    for h in HORIZONS:
        s = stats.get(h) or {}
        if s.get("n", 0) < min_n:
            continue
        v = abs(s.get("avg", 0.0))
        if v > best_v:
            best_h, best_v = h, v
    if best_h is None:
        return "短期", "历史同类样本不足，未做期限标定", 20
    s = stats[best_h]
    return (TERM_NAME[best_h].split("（")[0],
            "历史 %d 个同类时点：%d 日后胜率 %.0f%%、均值 %+.2f%%"
            % (s["n"], best_h, s["win"], s["avg"]), best_h)


def _calib(stats: dict, min_n: int = 15, term_h: Optional[int] = None) -> dict:
    """把历史标定翻成「可核对的证据 + 方向倾向」，供前端独立展示。

    ★ 铁律①：方向倾向只由统计决定，不靠行文感觉。样本 <min_n 一律说「不足」。
    ★ 铁律②：判断方向用的期限必须**与 term 相同**，否则会出现
      「期限标长期、结论却按 20 日说」的自相矛盾。
    """
    parts, n_max = [], 0
    for h in HORIZONS:
        s = stats.get(h) or {}
        n = s.get("n", 0)
        if n < min_n:
            continue
        n_max = max(n_max, n)
        parts.append("%d 日 胜率 %.0f%% / 均值 %+.2f%%"
                     % (h, s["win"], s["avg"]))
    if not parts:
        return {"text": "同类历史样本不足 %d 个，本次不做统计结论" % min_n,
                "bias": "unknown", "n": 0, "items": [], "h": term_h or 20}
    h = term_h if (term_h and (stats.get(term_h) or {}).get("n", 0) >= min_n) else 20
    s = stats.get(h) or {}
    win, avg = s.get("win", 50.0), s.get("avg", 0.0)
    bias, note = "flat", "统计上无显著方向，属于噪音区，别单独拿它做买卖依据"
    if win >= 55 and avg > 0:
        bias, note = "repair", "%d 日维度偏向修复：这类时点更像「风险已释放」而非「趋势刚开始」" % h
    elif win <= 45 and avg < 0:
        bias, note = "weaken", "%d 日维度偏向继续走弱：别把反弹当反转，需要额外催化才成立" % h
    elif win >= 55 and avg <= 0:
        bias, note = "choppy", "胜率不低但均值为负：反弹频繁却赚不到钱，属于「越反弹越卖」的结构"
    return {"text": "；".join(parts), "bias": bias, "n": n_max,
            "items": [{"h": hh, **(stats.get(hh) or {})} for hh in HORIZONS],
            "h": h, "note": note}


# ============ 信号定义 ============
# 每个信号：id / level(crisis,warn,watch) / tag / 判定函数(env,i) / 文字模板
def _ok(*vals) -> bool:
    for v in vals:
        if v is None or not math.isfinite(float(v)):
            return False
    return True


def _sig_vix_high(e, i):
    q, v = e["vix_pct"][i], e["vix"][i]
    return _ok(q, v) and (q >= 85 or v >= 28.0)


def _sig_vix_spike(e, i):
    r1, r5, q = e["vix_ret1"][i], e["vix_ret5"][i], e["vix_pct"][i]
    if not _ok(r1, r5, q):
        return False
    return (r1 >= 12.0 or r5 >= 25.0) and q >= 60


def _sig_vix_term(e, i):
    v, v9 = e["vix"][i], e["vix9d"][i]
    return _ok(v, v9) and v9 > v * 1.02 and v9 >= 18


def _sig_oil_high(e, i):
    q, v = e["brent_pct"][i], e["brent"][i]
    return _ok(q, v) and (q >= 85 or v >= 95.0)


def _sig_oil_spike(e, i):
    r5, r20, q = e["brent_ret5"][i], e["brent_ret20"][i], e["brent_pct"][i]
    if not _ok(r5, r20, q):
        return False
    return (r5 >= 9.0 or r20 >= 20.0) and q >= 60


def _sig_basis_extreme(e, i):
    """基差极端。★ 分品种设门槛：IC/IM 的小盘贴水本来就是常态（长期 1-2%），
    用同一个 92 分位会让「任一品种」命中 24% 的交易日 → 退化。"""
    for k, b in e["basis"].items():
        if b["roll"][i]:
            continue
        r = b["abs_rank"][i]
        if not _ok(r):
            continue
        thr = 92 if k in ("IF0", "IH0") else 96
        if r >= thr:
            return True
    return False


def _sig_fs_diverge(e, i):
    """期现涨跌方向相反 / 幅度差过大。"""
    for k, b in e["basis"].items():
        if b["roll"][i]:
            continue
        fp, sp = b["fpct"][i], b["spct"][i]
        if not _ok(fp, sp):
            continue
        d = fp - sp
        if (fp * sp < 0 and abs(d) >= 0.35) or abs(d) >= 0.90:
            return True
    return False


def _sig_vix_cn_inv(e, i):
    """VIX 与中国金融现货（沪深300 或 恒生）涨跌相反（VIX 取隔夜）。

    ★ 用户要求「中国（含恒生）」，所以两个市场都要查：只查 A 股会漏掉
      「美股恐慌 → 港股跌 → A 股独立」这类典型场景。
    """
    vr = e["vix_ret1"][i]
    if not _ok(vr) or abs(vr) < 4.0:
        return False
    for arr in (e["hs300_ret1"], e["hsi_ret1"]):
        r = arr[i]
        if _ok(r) and vr * r < 0 and abs(r) >= 0.30:
            return True
    return False


def _sig_cn_hk_inv(e, i):
    """境内宽基（四宽基等权）与恒生背离。

    ★ 用「宽基综合」而不是单看沪深300：用户要的是「宽金融指数」，
      单指数容易被某一风格的极端值带偏。
    """
    hr, gr = e["onshore_ret1"][i], e["hsi_ret1"][i]
    if not _ok(hr, gr):
        return False
    return (hr <= -0.30 and gr >= 0.50) or (hr >= 0.50 and gr <= -0.30)


def _sig_policy_stuck(e, i):
    """「宽货币未传导」：收益率没有上行（货币没在收紧），股指 20 日却大跌 ≥8%。

    ★ 阈值不能卡「国债期货必须大涨 1.2%」：现实中更多是「温和宽松 + 股市大跌」，
      只要货币不在收紧、股市又跌了 8% 以上，传导就是失效的。
    """
    t, r = e["t_ret20"][i], e["onshore_ret20"][i]
    if not _ok(t, r):
        return False
    return t >= 0.2 and r <= -8.0


def _sig_policy_tight(e, i):
    """「紧货币下的上涨」：国债期货 20 日明显下跌（收益率上行），股指 20 日却在涨。"""
    t, r = e["t_ret20"][i], e["onshore_ret20"][i]
    if not _ok(t, r):
        return False
    return t <= -1.2 and r >= 3.0


def _sig_a50_inv(e, i):
    """A50（离岸定价）与沪深300 背离。"""
    ar, hr = e["a50_ret1"][i], e["hs300_ret1"][i]
    if not _ok(ar, hr):
        return False
    d = ar - hr
    return abs(d) >= 0.80


def _sig_oi_surge(e, i):
    """期指持仓 5 日异动（增量资金进场/离场）。"""
    for k, b in e["basis"].items():
        c = b["hold_chg5"][i]
        if _ok(c) and abs(c) >= 10.0:
            return True
    return False


def _sig_trend_break(e, i):
    """沪深300 跌破 MA60 且 MA20 在 MA60 下方。

    ★ 这是「环境状态」而非「当日异动」：历史上约 38% 的交易日成立，
      所以定为 watch 级、前端默认折叠，避免长期占屏。
    """
    px, m20, m60 = e["hs300"][i], e["hs300_ma20"][i], e["hs300_ma60"][i]
    if not _ok(px, m20, m60):
        return False
    return px < m60 and m20 < m60


SIGNALS = [
    {"id": "vix_spike", "tag": "恐慌", "level": "crisis", "fn": _sig_vix_spike,
     "title": "VIX 恐慌指数急升"},
    {"id": "vix_high", "tag": "恐慌", "level": "warn", "fn": _sig_vix_high,
     "title": "VIX 处于自身高位"},
    {"id": "vix_term", "tag": "恐慌", "level": "warn", "fn": _sig_vix_term,
     "title": "VIX9D 高于 VIX（近端事件风险急升）"},
    {"id": "oil_spike", "tag": "商品", "level": "crisis", "fn": _sig_oil_spike,
     "title": "布伦特原油快速拉升"},
    {"id": "oil_high", "tag": "商品", "level": "warn", "fn": _sig_oil_high,
     "title": "布伦特原油处于自身高位"},
    {"id": "basis_extreme", "tag": "期现", "level": "warn", "fn": _sig_basis_extreme,
     "title": "股指期货基差处于历史极端"},
    {"id": "fs_diverge", "tag": "期现", "level": "warn", "fn": _sig_fs_diverge,
     "title": "期现走势背离"},
    {"id": "vix_cn_inv", "tag": "跨市场", "level": "warn", "fn": _sig_vix_cn_inv,
     "title": "VIX 与 A 股涨跌相反"},
    {"id": "cn_hk_inv", "tag": "跨市场", "level": "warn", "fn": _sig_cn_hk_inv,
     "title": "境内宽基与恒生背离"},
    {"id": "a50_inv", "tag": "跨市场", "level": "watch", "fn": _sig_a50_inv,
     "title": "A50 期货与 A 股背离"},
    {"id": "oi_surge", "tag": "期现", "level": "watch", "fn": _sig_oi_surge,
     "title": "期指持仓量异动"},
    {"id": "policy_stuck", "tag": "政策", "level": "warn", "fn": _sig_policy_stuck,
     "title": "宽货币尚未传导到股市"},
    {"id": "policy_tight", "tag": "政策", "level": "watch", "fn": _sig_policy_tight,
     "title": "紧货币下的股市上涨（脆弱）"},
    {"id": "trend_break", "tag": "趋势", "level": "watch", "fn": _sig_trend_break,
     "title": "沪深300 中期趋势走坏（环境状态）"},
]


# ============ 汇率与中美利差 ============
# 为什么补这块
# ------------
# 原有「跨境分歧」维度只有「恒生与 A50 的日涨跌差」，那回答不了「外资为什么走」。
# 汇率与中美利差才是跨境资金流动的直接约束：中美 10Y 倒挂 300bp 时，
# 套息方向本身就决定了外资的倾向，港股一天的涨跌只是结果。
#
# ★ 这一块**不并入风险温度的 6 维权重**。理由很实在：改维度定义 = 推翻已有标定，
#   而标定刚刚才用滚动 3 折验过、结论是不稳的。再动一次只会让前后两个版本
#   的数字没法对比。所以它作为**独立环境读数**存在，并自带一条历史相关性检验，
#   由用户自己判断要不要采信。
_FX_CACHE: dict = {"ts": 0.0, "data": None}
_FX_TTL = 6 * 3600


def _fx_spot() -> dict:
    """新浪外汇实时（在岸 fx_susdcny / 离岸 fx_susdcnh）。

    字段：[0]时间 [1]买 [2]卖 [3]昨收 [4]量 [5]开 [6]高 [7]低 [8]最新 [9]名称
          [10]涨跌幅% [11]涨跌额
    """
    out = {}
    try:
        txt = _http("https://hq.sinajs.cn/list=fx_susdcny,fx_susdcnh",
                    headers={"Referer": "https://finance.sina.com.cn"}, enc="gbk", timeout=10)
    except Exception:  # noqa: BLE001
        return out
    for line in (txt or "").split("\n"):
        m = re.match(r'var hq_str_(fx_s\w+)="(.*)";', line.strip())
        if not m:
            continue
        sym, body = m.group(1), m.group(2)
        if not body:
            continue
        p = body.split(",")
        if len(p) < 12:
            continue
        px, prev = _f(p[8]) or _f(p[2]), _f(p[3])
        key = "cnh" if sym.endswith("nh") else "cny"
        out[key] = {"price": px, "prev": prev,
                    "open": _f(p[5]), "high": _f(p[6]), "low": _f(p[7]),
                    "pct": _f(p[10]), "name": p[9] if len(p) > 9 else "",
                    "time": p[0], "date": p[17] if len(p) > 17 else "",
                    # ★ USDCNY 上涨 = 人民币贬值，方向要在文案里讲清楚，
                    #   不能让用户自己猜「涨」是人民币强还是弱。
                    "chg": (px / prev - 1) * 100 if (px and prev) else None}
    return out


def _quiet(fn, *a, **kw):
    """akshare 的 tqdm 进度条写 stderr，会刷进服务日志 —— 屏蔽掉。"""
    import contextlib
    import io as _io
    try:
        with contextlib.redirect_stderr(_io.StringIO()):
            return fn(*a, **kw)
    except Exception:  # noqa: BLE001
        return None


def _fx_mid_hist() -> list:
    """央行/中行美元中间价日线（升序）。单位从「每 100 美元」换成「每 1 美元」。"""
    import akshare as ak
    start = (_dt.date.today() - _dt.timedelta(days=800)).strftime("%Y%m%d")
    end = _dt.date.today().strftime("%Y%m%d")
    df = _quiet(ak.currency_boc_sina, symbol="美元", start_date=start, end_date=end)
    if df is None or getattr(df, "empty", True):
        return []
    col = "央行中间价" if "央行中间价" in df.columns else "中行折算价"
    bars = []
    for _, r in df.iterrows():
        d = str(r.get("日期"))[:10].replace("/", "-")
        v = _f(r.get(col))
        if not d or v is None or v <= 0:
            continue
        bars.append({"d": d, "c": v / 100.0})
    bars.sort(key=lambda x: x["d"])
    return bars


def _cn_us_rate() -> dict:
    """中美国债收益率与 10Y 利差（%）。"""
    import akshare as ak
    start = (_dt.date.today() - _dt.timedelta(days=900)).strftime("%Y%m%d")
    df = _quiet(ak.bond_zh_us_rate, start_date=start)
    if df is None or getattr(df, "empty", True):
        return {}
    rows = []
    for _, r in df.iterrows():
        d = str(r.get("日期"))[:10]
        cn = _f(r.get("中国国债收益率10年"))
        us = _f(r.get("美国国债收益率10年"))
        if not d:
            continue
        rows.append({"d": d, "cn": cn, "us": us,
                     "gap": (cn - us) if (cn is not None and us is not None) else None})
    rows.sort(key=lambda x: x["d"])
    if not rows:
        return {}
    last_gap = next((x for x in reversed(rows) if x["gap"] is not None), None)
    last_cn = next((x for x in reversed(rows) if x["cn"] is not None), None)
    last_us = next((x for x in reversed(rows) if x["us"] is not None), None)
    gaps = [x for x in rows if x["gap"] is not None]
    gap20 = None
    if len(gaps) > 21:
        gap20 = gaps[-1]["gap"] - gaps[-21]["gap"]      # 20 个交易日的变化（bp 需 ×100）
    return {"rows": rows[-600:], "cn10y": last_cn, "us10y": last_us,
            "last_gap": last_gap, "gap_chg20": round(gap20, 3) if gap20 is not None else None,
            "asof": rows[-1]["d"]}


def _corr(a: list, b: list) -> Optional[float]:
    """两条等长序列的 Pearson 相关。"""
    if len(a) < 30 or len(a) != len(b):
        return None
    x = np.array(a, dtype=float)
    y = np.array(b, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 30:
        return None
    x, y = x[m], y[m]
    if x.std() < 1e-12 or y.std() < 1e-12:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def fx_block(force: bool = False) -> dict:
    """汇率 + 中美利差 + 它们与沪深300 的历史相关性。"""
    now = time.time()
    with _LOCK:
        c = _FX_CACHE["data"]
        if not force and c and now - _FX_CACHE["ts"] < _FX_TTL:
            return c

    t0 = time.time()
    notes = []
    spot = _fx_spot()
    if not spot:
        notes.append("汇率实时快照不可用")
    mid = _fx_mid_hist()
    if not mid:
        notes.append("央行中间价日线不可用")
    rate = _cn_us_rate()
    if not rate:
        notes.append("中美国债收益率不可用")

    # 汇率变化：用中间价日线（比实时价连续，且是权威口径）
    chg5 = chg20 = pct250 = None
    fx_series = []
    if mid:
        vals = np.array([x["c"] for x in mid], dtype=float)
        dates = [x["d"] for x in mid]
        if len(vals) > 6:
            chg5 = (vals[-1] / vals[-6] - 1) * 100
        if len(vals) > 21:
            chg20 = (vals[-1] / vals[-21] - 1) * 100
        w = vals[-250:] if len(vals) > 250 else vals
        pct250 = float((w <= vals[-1]).mean() * 100)
        step = max(1, len(dates) // 120)
        fx_series = [{"d": dates[i], "v": round(float(vals[i]), 4)}
                     for i in range(len(dates))][::step][-120:]

    # 离岸-在岸价差：正值 = 离岸更弱 = 贬值预期
    cg = None
    if spot.get("cnh", {}).get("price") and spot.get("cny", {}).get("price"):
        cnh, cny = spot["cnh"]["price"], spot["cny"]["price"]
        if cny:
            cg = (cnh / cny - 1) * 100

    # 历史相关性：汇率 20 日变化 / 利差 20 日变化 分别对沪深300 未来 20 日收益
    # 历史相关性：汇率 N 日变化 对沪深300 未来 N 日收益（5/20/60 三档）
    # ★ 只报一个期限等于挑好看的；三个期限方向一致才说明这条关系不是窗口的偶然。
    corr_fx: dict = {}
    try:
        hs = history("sh000300")
        if hs and mid:
            hd = {b["date"]: b["close"] for b in hs}
            hkeys = sorted(hd.keys())
            for h in (5, 20, 60):
                xs, ys = [], []
                for i in range(len(mid) - h):
                    d, d2 = mid[i]["d"], mid[i + h]["d"]
                    if d not in hd:
                        continue
                    # 用 bisect 找「d2 之后的第一个交易日」，避免 O(n·m) 遍历
                    k = bisect.bisect_left(hkeys, d2)
                    if k >= len(hkeys):
                        break
                    xs.append((mid[i + h]["c"] / mid[i]["c"] - 1) * 100)
                    ys.append((hd[hkeys[k]] / hd[d] - 1) * 100)
                c = _corr(xs, ys)
                corr_fx[str(h)] = round(c, 3) if c is not None else None
    except Exception:  # noqa: BLE001
        pass

    res = {
        "ok": bool(spot or mid or rate),
        "spot": spot,
        "mid": {"value": mid[-1]["c"] if mid else None,
                "date": mid[-1]["d"] if mid else None,
                "chg5": round(chg5, 2) if chg5 is not None else None,
                "chg20": round(chg20, 2) if chg20 is not None else None,
                "pct250": round(pct250, 1) if pct250 is not None else None,
                "n": len(mid)},
        "cnh_gap": round(cg, 3) if cg is not None else None,
        "rate": {"cn10y": rate.get("cn10y", {}).get("cn"),
                 "cn_date": rate.get("cn10y", {}).get("d"),
                 "us10y": rate.get("us10y", {}).get("us"),
                 "us_date": rate.get("us10y", {}).get("d"),
                 "gap": rate.get("last_gap", {}).get("gap"),
                 "gap_date": rate.get("last_gap", {}).get("d"),
                 "gap_chg20": (round(rate["gap_chg20"] * 100, 1)
                               if rate.get("gap_chg20") is not None else None)},
        "series": {"fx": fx_series,
                   "gap": [{"d": x["d"], "v": round(x["gap"], 3)}
                           for x in (rate.get("rows") or []) if x["gap"] is not None][-120:]},
        "corr": {"fx": corr_fx},
        "notes": notes,
        "elapsed": round(time.time() - t0, 2),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "caveats": [
            "USDCNY 数值上涨 = 人民币贬值（美元变贵），文案里已按这个方向写，别按股市红绿直觉读",
            "中间价是央行授权公布的官方价，离岸 CNH 是市场价；两者之差反映贬值预期，"
            "但它也受流动性影响，不等于「官方要贬」",
            "中美利差 = 中国 10Y − 美国 10Y，负值即倒挂。它是跨境资金方向的**约束条件**，"
            "不是买卖信号：倒挂也能涨（看国内政策和风险偏好）",
            "汇率与 A 股的相关性在样本内可能很强，但那是**同期**关系；"
            "这里算的是「汇率变化 → 未来同期限收益」，三个期限（5/20/60 日）并列给出，"
            "常不同号，也可能全都接近 0 —— 那就是「这条关系在这段样本里不成立」，照实看数值即可",
            "本区块**未纳入风险温度的 6 维权重**，只是并列读数",
        ],
    }
    with _LOCK:
        _FX_CACHE["ts"], _FX_CACHE["data"] = now, res
    return res


# ============ 展示辅助 ============
def _n(x, dec: int = 2, dash: str = "—") -> str:
    if x is None:
        return dash
    try:
        v = float(x)
    except (TypeError, ValueError):
        return dash
    if not math.isfinite(v):
        return dash
    return ("%." + str(dec) + "f") % v


def _p(x, dec: int = 2) -> str:
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    return ("%+." + str(dec) + "f%%") % v


def _arr_last(a: np.ndarray, i: int):
    """安全取数组第 i 个元素（nan → None）。"""
    try:
        v = a[i]
    except (IndexError, TypeError):
        return None
    if v is None:
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _basis_state(b: dict, i: int) -> dict:
    """单品种期现结构读数。"""
    bp = _arr_last(b["pct"], i)
    sm = _arr_last(b["smooth"], i)
    rk = _arr_last(b["abs_rank"], i)
    fp = _arr_last(b["fpct"], i)
    sp = _arr_last(b["spct"], i)
    hold = _arr_last(b["hold"], i)
    hc = _arr_last(b["hold_chg5"], i)
    return {"basis_pct": bp, "smooth": sm, "rank": rk, "fut_pct": fp,
            "spot_pct": sp, "diff_pp": (fp - sp) if (fp is not None and sp is not None) else None,
            "hold": hold, "hold_chg5": hc,
            "rollover": bool(b["roll"][i]) if i < len(b["roll"]) else False}


def _basis_read(bp, rank, diff_pp, hc, rollover: bool) -> tuple:
    """把基差读数翻译成「现象 + 期限 + 形态」。返回 (现象, 期限, 形态)。

    ★ 判「深/高」以**历史分位为主**、绝对值为辅：IC/IM 常年贴水 1.2%，
      用固定的 -0.8% 阈值会把「常态」误判成「极端」。
    """
    if rollover:
        return ("主连换月跳空，当日基差不可比", "短期", "中性")
    if bp is None:
        return ("基差数据缺失", "短期", "中性")
    extreme = rank is not None and rank >= 88
    adding = hc is not None and hc >= 5.0
    cutting = hc is not None and hc <= -5.0
    if bp < 0:
        deep = extreme or bp <= -1.5
        if deep and cutting:
            return ("贴水分位偏高 + 持仓下降（空头平仓）", "短期", "反转")
        if deep and adding:
            return ("贴水分位偏高 + 持仓上升（空头加码）", "中期", "逆向")
        if deep:
            return ("贴水处于历史偏深位置", "中期", "逆向")
        return ("贴水走阔但未到极端", "短期", "减缓" if diff_pp is not None and diff_pp < 0 else "中性")
    if bp > 0:
        prem = extreme or bp >= 1.5
        if prem and adding:
            return ("升水扩大 + 持仓上升（多头抢跑）", "短期", "加速")
        if prem:
            return ("升水处于历史偏高位置", "中期", "减缓")
        return ("升水收敛", "短期", "减缓")
    return ("基差贴近零轴", "短期", "中性")


# ============ 信号文案（与判定函数分离，便于独立复核文字口径） ============
def _explain(sid: str, e: dict, i: int, stats: dict) -> dict:
    vix = _arr_last(e["vix"], i)
    vix_r1 = _arr_last(e["vix_ret1"], i)
    vix_r5 = _arr_last(e["vix_ret5"], i)
    vix_q = _arr_last(e["vix_pct"], i)
    vix9d = _arr_last(e["vix9d"], i)
    oil = _arr_last(e["brent"], i)
    oil_r5 = _arr_last(e["brent_ret5"], i)
    oil_r20 = _arr_last(e["brent_ret20"], i)
    oil_q = _arr_last(e["brent_pct"], i)
    hs = _arr_last(e["hs300"], i)
    hsr = _arr_last(e["hs300_ret1"], i)
    hkr = _arr_last(e["hsi_ret1"], i)
    ar = _arr_last(e["a50_ret1"], i)
    onr = _arr_last(e["onshore_ret1"], i)
    on20 = _arr_last(e["onshore_ret20"], i)
    t20 = _arr_last(e["t_ret20"], i)
    term, term_why, term_h = _pick_term(stats)
    out = {"term": term, "term_why": term_why, "pattern": "中性", "facts": [],
           "_term_h": term_h}

    if sid == "vix_spike":
        out["facts"] = [
            "VIX %s（当日 %s，5 日 %s）" % (_n(vix), _p(vix_r1), _p(vix_r5)),
            "自身近 3 年分位 %s" % _n(vix_q, 0),
        ]
        out["verdict"] = ("全球风险偏好正在快速收缩：VIX 的抬升速度快于绝对水位，"
                          "这类「斜率型」恐慌通常先冲击外资重仓与高估值成长，"
                          "对低波红利/内需的杀伤相对滞后。")
        out["playbook"] = ("① 恐慌急升的头 1-3 天是流动性最差的时候，别在缺口里追跌；"
                           "② 若 VIX 冲高后 5 个交易日内回落 20% 以上，往往对应 A 股的短期底部区域；"
                           "③ 此时恐慌维度权重已上调，个股技术信号要让位于波动率方向。")
        out["pattern"] = "加速"
    elif sid == "vix_high":
        out["facts"] = ["VIX %s" % _n(vix), "近 3 年分位 %s（≥85 视为高位）" % _n(vix_q, 0)]
        out["verdict"] = ("VIX 停在自身高位意味着风险溢价被系统性抬高，"
                          "分母端（贴现率/风险补偿）压制估值，指数上行需要更强的盈利或政策对冲。")
        out["playbook"] = ("① 高位钝化是常态，不必因 VIX 高就空仓，但要降低高 beta 暴露；"
                           "② 关注 VIX 与 A 股实际波动率的差——若 A 股波动率低而 VIX 高，"
                           "说明恐慌是「外部输入」，内部筹码并未松动。")
    elif sid == "vix_term":
        out["facts"] = ["VIX9D %s > VIX %s（近端高于 30 日）" % (_n(vix9d), _n(vix))]
        out["verdict"] = ("短端隐含波动率反超长端，通常对应 1-2 周内的确定性事件"
                          "（议息、非农、重要会议、财报或地缘节点），属于「事件型恐慌」而非趋势型。")
        out["playbook"] = ("① 事件落地前不宜加大方向性押注；② 事件过后 VIX9D 会快速回落，"
                           "若现货未破位，往往是一次可交易的错杀。")
        out["pattern"] = "加速"
    elif sid == "oil_spike":
        out["facts"] = ["布伦特 %s 美元（5 日 %s，20 日 %s）" % (_n(oil), _p(oil_r5), _p(oil_r20)),
                        "自身近 3 年分位 %s" % _n(oil_q, 0)]
        out["verdict"] = ("油价快速拉升是成本端冲击：上游（石油石化/油服/煤炭）受益，"
                          "中下游（航空/化纤/物流/汽车/包装）毛利被压缩；"
                          "若同时美元走强，还会通过新兴市场资金流出的渠道压制 A 股估值。")
        out["playbook"] = ("① 关注国内成品油调价与 PPI 的传导时滞（通常 1-2 个月）；"
                           "② 油价单周涨超 9% 时，市场先交易「通胀→紧缩」逻辑，"
                           "成长风格承压而资源风格占优；③ 商品维度权重已上调。")
        out["pattern"] = "加速"
    elif sid == "oil_high":
        out["facts"] = ["布伦特 %s 美元" % _n(oil), "近 3 年分位 %s（≥85 视为高位）" % _n(oil_q, 0)]
        out["verdict"] = ("油价停在自身高位构成持续的输入型通胀压力，"
                          "会限制国内货币政策的宽松空间，并对中游制造形成长期毛利挤压。")
        out["playbook"] = ("① 高位油价下，配置重心从「成本敏感」转向「资源+定价权」；"
                           "② 若油价高位而 A 股震荡，属于典型的滞胀交易结构，"
                           "此时债券与红利资产的性价比上升。")
    elif sid == "basis_extreme":
        rows, worst = [], None
        for k in ("IF0", "IH0", "IC0", "IM0"):
            b = e["basis"].get(k)
            if not b:
                continue
            st = _basis_state(b, i)
            if st["rollover"] or st["basis_pct"] is None:
                continue
            rows.append("%s 基差 %s（分位 %s）" % (k, _p(st["basis_pct"]), _n(st["rank"], 0)))
            if worst is None or abs(st["basis_pct"]) > abs(worst[1]):
                worst = (k, st["basis_pct"], st)
        out["facts"] = rows[:4]
        if worst:
            phen, _tm, pat = _basis_read(worst[1], worst[2]["rank"], worst[2]["diff_pp"],
                                         worst[2]["hold_chg5"], False)
            out["pattern"] = pat
            out["verdict"] = ("%s 的基差已到历史极端：%s。基差是「期货定价 vs 现货定价」的裂缝——"
                              "套保/对冲盘把期货压（或推）到现货之外，这个裂缝通常先于现货情绪修复。"
                              "注意方向：贴水极值是「空头拥挤」的证据，不等于「要涨」；"
                              "它只说明现价里已经含了较多悲观预期。" % (worst[0], phen))
            out["playbook"] = ("① 深度贴水时，用期货替代现货做多可额外赚「基差收敛」的钱；"
                               "② 贴水极值 + 持仓下降 = 空头离场，往往是阶段性底部的前兆；"
                               "③ 贴水极值 + 持仓上升 = 空头加码，中期压力未解，别急着抄底。")
    elif sid == "fs_diverge":
        rows, worst = [], None
        for k in ("IF0", "IH0", "IC0", "IM0"):
            b = e["basis"].get(k)
            if not b:
                continue
            st = _basis_state(b, i)
            if st["rollover"] or st["diff_pp"] is None:
                continue
            rows.append("%s 期货 %s vs 现货 %s（差 %s）"
                        % (k, _p(st["fut_pct"]), _p(st["spot_pct"]), _p(st["diff_pp"])))
            if worst is None or abs(st["diff_pp"]) > abs(worst[1]):
                worst = (k, st["diff_pp"], st)
        out["facts"] = rows[:4]
        if worst:
            d = worst[1]
            if d > 0:
                out["verdict"] = ("%s 的期货强于现货 %.2f 个百分点：期货端在抢跑预期（政策/流动性/外部环境），"
                                  "而现货资金仍在观望。这种「预期先行、资金滞后」的结构，"
                                  "短期容易由现货补涨来完成修复，但若 2-3 日内现货不跟，期货会回落收敛。"
                                  % (worst[0], d))
                out["playbook"] = ("① 观察次日现货能否放量跟上；② 若持仓同步上升，说明是增量多头而非空头回补，"
                                   "持续性更好；③ 若持仓下降，多为平空反弹，追高性价比低。")
                out["pattern"] = "加速"
            else:
                out["verdict"] = ("%s 的期货弱于现货 %.2f 个百分点：期货端出现主动卖压（套保增加或空头进攻），"
                                  "而现货尚由存量资金支撑。历史上这种结构多是「现货补跌」收场，"
                                  "除非有增量资金在现货端接力。" % (worst[0], abs(d)))
                out["playbook"] = ("① 关注基差是否同步走阔——走阔说明是套保盘而非投机盘；"
                                   "② 若次日现货下跌而基差收敛，说明空头在平仓，跌势接近尾声；"
                                   "③ 此时不宜在现货端逆势加仓。")
                out["pattern"] = "逆向"
    elif sid == "vix_cn_inv":
        cn_inv = (vix_r1 is not None and hsr is not None
                  and (vix_r1 * hsr) < 0 and abs(hsr) >= 0.30)
        hk_inv = (vix_r1 is not None and hkr is not None
                  and (vix_r1 * hkr) < 0 and abs(hkr) >= 0.30)
        who = "、".join([n for n, f in (("A 股", cn_inv), ("港股", hk_inv)) if f]) or "中国市场"
        out["facts"] = ["VIX 隔夜 %s" % _p(vix_r1), "沪深300 当日 %s" % _p(hsr),
                        "恒生 当日 %s" % _p(hkr)]
        if (vix_r1 or 0) > 0:
            out["verdict"] = ("海外恐慌升温（VIX 隔夜 %s）而%s收涨，说明本轮风险定价没有传导过去："
                              "要么外部事件与中国基本面无关，要么是内部有政策/流动性托底。"
                              "这种「脱钩」短期是强势特征，但它依赖内部资金持续接力——"
                              "一旦 VIX 回落而中国资产开始补跌，往往已是补跌尾声。" % (_p(vix_r1), who))
            out["playbook"] = ("① 跟踪北向/南向资金是否同向：外资仍在流出的话，脱钩撑不久；"
                               "② 用 10 年期国债期货确认「内部托底」是否真在（价格涨 = 收益率下行 = 宽货币）；"
                               "③ 若两边都涨而 VIX 掉头向下，风险偏好修复确认，可适度提高 beta。")
        else:
            out["verdict"] = ("海外恐慌回落（VIX 隔夜 %s）而%s下跌，说明杀跌动力来自内部"
                              "（流动性、政策预期或盈利），不是外部风险偏好问题。"
                              "内部因素主导的调整通常更磨人，但可控性也更强。" % (_p(vix_r1), who))
            out["playbook"] = ("① 重点看国内流动性与政策口径，而不是盯 VIX；"
                               "② 国债期货若同步走强说明是「缺风险偏好」而非「缺钱」，"
                               "这类下跌的修复需要赚钱效应或政策催化；"
                               "③ 内部因素导致的下跌，通常等一个明确的数据或政策拐点。")
    elif sid == "cn_hk_inv":
        out["facts"] = ["境内宽基（四指数等权） %s" % _p(onr), "沪深300 %s" % _p(hsr),
                        "恒生 %s" % _p(hkr)]
        if (onr if onr is not None else (hsr or 0)) < 0:
            out["verdict"] = ("A 股走弱而港股走强：定价权暂时在离岸，南向/外资在买中国资产，"
                              "但境内流动性或风险偏好没跟上。关键是「谁向谁收敛」——"
                              "要么 A 股补涨追平，要么港股回落。别预设方向，看下面的统计证据。")
            out["playbook"] = ("① 看南向资金是否连续净买入；② 看恒生科技与 A 股成长风格的相对强弱；"
                               "③ 若港股上涨由红利/金融驱动而非科技，对 A 股的指示意义较弱。")
        else:
            out["verdict"] = ("A 股走强而港股走弱：外资在离岸市场减仓中国资产，"
                              "而境内资金推动 A 股。这种「两头定价权分家」的结构常见于"
                              "外部流动性收紧或地缘扰动期，A 股的独立性通常难以长期维持。")
            out["playbook"] = ("① 关注美元指数与港股通额度使用；② A 股若靠题材/小盘驱动，"
                               "要警惕外资流出传导到权重股后指数补跌。")
    elif sid == "a50_inv":
        out["facts"] = ["A50 期货 %s" % _p(ar), "沪深300 %s" % _p(hsr),
                        "差值 %s" % _p((ar - hsr) if (ar is not None and hsr is not None) else None)]
        if (ar or 0) > (hsr or 0):
            out["verdict"] = ("离岸 A50 强于在岸沪深300：外资对政策/经济预期的定价更乐观，"
                              "或在提前布局。A50 是 A 股开盘前的先行指标，"
                              "这种「离岸领先」在历史上多对应 A 股次日的补涨。")
            out["playbook"] = ("① 次日开盘半小时的方向确认很关键；"
                               "② 若 A 股高开后回落，说明离岸预期被证伪，注意反向。")
            out["pattern"] = "加速"
        else:
            out["verdict"] = ("离岸 A50 弱于在岸沪深300：外资比境内更谨慎，"
                              "通常对应外资流出或对政策落地的怀疑。")
            out["playbook"] = ("① 关注陆股通/北向的净流向；② 若连续 3 日离岸弱于在岸，"
                               "权重股承压概率上升。")
            out["pattern"] = "逆向"
    elif sid == "oi_surge":
        rows = []
        for k in ("IF0", "IH0", "IC0", "IM0"):
            b = e["basis"].get(k)
            if not b:
                continue
            st = _basis_state(b, i)
            if st["hold_chg5"] is None:
                continue
            rows.append("%s 持仓 %s 手（5 日 %s）"
                        % (k, _n(st["hold"], 0), _p(st["hold_chg5"])))
        out["facts"] = rows[:4]
        out["verdict"] = ("股指期货持仓量在 5 个交易日内明显变化，代表增量资金（而非存量博弈）"
                          "进场或离场。持仓与价格同向 = 趋势确认；持仓升而价格跌 = 空头增量；"
                          "持仓骤降 = 趋势衰竭、以平仓为主。")
        out["playbook"] = ("① 持仓激增 + 期指涨 = 趋势加速，顺势；"
                           "② 持仓激增 + 期指跌 = 下跌有增量空头，反弹要等持仓回落；"
                           "③ 持仓骤降 + 期指涨 = 空头回补，涨势难以持续。")
    elif sid == "policy_stuck":
        out["facts"] = ["10Y 国债期货 20 日 %s" % _p(t20),
                        "境内宽基 20 日 %s" % _p(on20)]
        out["verdict"] = ("国债期货 20 日明显上涨（收益率下行 = 货币在放松），但境内宽基反而跌了 3% 以上。"
                          "「宽货币 + 弱股市」说明钱没有流进风险资产，卡在了传导环节："
                          "要么是信用扩张没起来（实体不敢借），要么是资金停在存款/理财里空转。"
                          "这类结构下，指数缺的是「风险偏好」，不是「流动性」。")
        out["playbook"] = ("① 等赚钱效应或政策催化（降准只是必要条件，不是充分条件）；"
                           "② 关注国债期货是否见顶回落——那是资金开始从避险转向风险资产的先行信号；"
                           "③ 此阶段高股息/红利仍占优，成长需要更强的产业催化。")
        out["pattern"] = "减缓"
    elif sid == "policy_tight":
        out["facts"] = ["10Y 国债期货 20 日 %s" % _p(t20),
                        "境内宽基 20 日 %s" % _p(on20)]
        out["verdict"] = ("国债期货 20 日明显下跌（收益率上行 = 货币在收紧），但境内宽基反而涨了 3% 以上。"
                          "「紧货币 + 强股市」通常靠风险偏好或增量资金硬撑，估值扩张缺乏利率配合，"
                          "属于脆弱结构——一旦资金面再收紧，回撤会很快。")
        out["playbook"] = ("① 盯住银行间资金价格与央行公开市场操作，这是最直接的验证；"
                           "② 上涨靠估值而非盈利时，降低久期敏感型成长股的权重；"
                           "③ 若国债期货继续下跌而股市不跌，说明是真增量资金进场，可以再等一等。")
        out["pattern"] = "加速"
    elif sid == "trend_break":
        m20 = _arr_last(e["hs300_ma20"], i)
        m60 = _arr_last(e["hs300_ma60"], i)
        r20 = _arr_last(e["hs300_ret20"], i)
        out["facts"] = ["沪深300 %s（MA20 %s / MA60 %s）" % (_n(hs), _n(m20), _n(m60)),
                        "20 日动量 %s" % _p(r20)]
        out["verdict"] = ("沪深300 跌破 MA60 且 MA20 位于 MA60 下方，中期均线结构转空。"
                          "均线破坏最先压制的是 beta 与杠杆资金，个股胜率会被系统性拉低；"
                          "但指数级别的破位往往也是分批布局的窗口期——"
                          "具体偏「继续磨」还是「等修复」，看下方多期限统计，别预设方向。")
        out["playbook"] = ("① 短期防守优先，降低杠杆与高 beta 暴露；"
                           "② 反弹到 MA20/MA60 附近是检验成色的窗口（能否放量站上）；"
                           "③ 等 MA20 重新上穿 MA60、且量能配合，再谈趋势级进攻。")
    return out


# ============ 主分析 ============
def _theme_of(hits: list) -> Optional[dict]:
    """把互相印证的信号串成一条「主线」。只在 ≥2 个信号同时成立时给出。"""
    ids = {h["id"] for h in hits}
    if len(ids) < 2:
        return None
    panic = bool(ids & {"vix_spike", "vix_high"})
    oil = bool(ids & {"oil_spike", "oil_high"})
    if panic and "trend_break" in ids and "basis_extreme" in ids:
        return {"tone": "crisis", "title": "三重压制：外部恐慌 × 趋势破坏 × 期指贴水拥挤",
                "text": ("VIX 抬升压制风险偏好、沪深300 均线结构转空、期指又被空头压出深度贴水——"
                         "三条线索指向同一个结论：这是「风险正在释放」的中段，不是起点也不是终点。"
                         "历史上同时满足三者的时段，短期（5 日）多数还要再磨，真正的低点通常出现在"
                         "「VIX 回落 + 贴水收敛」同时发生之后。")}
    if oil and "trend_break" in ids:
        return {"tone": "warn", "title": "滞胀式杀估值：油价高企 × 指数走弱",
                "text": ("成本端（油价）向上、需求端（指数）向下的组合，是典型的滞胀交易结构。"
                         "这种环境下，宽松预期会被通胀掣肘，估值扩张很难，"
                         "资金会往「有定价权 + 现金流稳」的资产躲，而不是往高估值成长冲。")}
    if "cn_hk_inv" in ids and "a50_inv" in ids:
        return {"tone": "warn", "title": "离岸定价与在岸情绪分家",
                "text": ("A50 与恒生同时表现出比 A 股更强的定价，说明外资对中国的预期好于境内资金。"
                         "「离岸先动、在岸后知」是常见节奏，但方向取决于境内是否有增量资金接力——"
                         "若只有离岸强、A 股成交不放大，背离会以港股回落的方式收敛。")}
    if "fs_diverge" in ids and "oi_surge" in ids:
        return {"tone": "warn", "title": "筹码在换手：期现背离 × 持仓异动",
                "text": ("期指与现货走势分家，同时持仓量明显变化，说明这不是情绪噪音，"
                         "而是真金白银在换手：增量资金进场（持仓升）还是空头平仓（持仓降），"
                         "决定了这波背离是「趋势的起点」还是「反弹的尾声」。")}
    if "policy_stuck" in ids and (panic or "trend_break" in ids):
        return {"tone": "warn", "title": "宽货币未传导：钱在，但不敢进股市",
                "text": ("国债期货在涨（收益率下行、货币在放松）而股指在跌，同时外部风险也在抬升——"
                         "这是典型的「流动性陷阱」式组合：钱不缺，缺的是风险偏好和赚钱效应。"
                         "这类环境下，政策的作用是「托底」而不是「催涨」，"
                         "真正的拐点通常出现在「国债期货见顶回落 + 股市放量」同时发生时。")}
    if "policy_tight" in ids and "basis_extreme" in ids:
        return {"tone": "warn", "title": "紧货币下的上涨：靠情绪撑，不靠利率",
                "text": ("国债期货下跌（收益率上行、货币边际收紧）而股市上涨，同时期指贴水还很深。"
                         "这说明上涨靠的是风险偏好而非流动性，且期指端的对冲需求没有减少——"
                         "结构上是「情绪领先、资金没跟上」，一旦情绪退潮回撤会很快。")}
    if panic and "basis_extreme" in ids:
        return {"tone": "warn", "title": "双极端：恐慌高位 × 基差极值",
                "text": ("VIX 高位同时期指贴水也到历史极端，意味着「情绪恐慌」和「对冲拥挤」叠在一起。"
                         "这类组合的反向意义强于顺势意义——但需要等一个确认信号："
                         "VIX 见顶回落，或基差开始收敛。")}
    strong = [h for h in hits if h["level"] in ("crisis", "warn")]
    if len(strong) >= 2:
        tags = "、".join(sorted({h["tag"] for h in strong}))
        return {"tone": "warn", "title": "%d 项预警同时成立（%s）" % (len(strong), tags),
                "text": ("多个维度同时报警，说明这不是单一扰动，而是「环境级别的收紧」。"
                         "此时不要逐条去挑哪个指标更重要——先看权重最高的那一维（见下方权重条），"
                         "它决定了当前市场的主要矛盾；其余维度更多是验证或放大。")}
    return None


def run_analysis(force: bool = False) -> dict:
    now = time.time()
    with _LOCK:
        if not force and _AN_CACHE["data"] and now - _AN_CACHE["ts"] < _ttl_an():
            return _AN_CACHE["data"]

    t0 = time.time()
    prefetch(force=force)
    env = build_env(force=force)
    if not env or not env.get("dates"):
        return {"ok": False, "err": "沪深300 历史数据不可用，无法建立分析基准", "signals": []}
    dates = env["dates"]
    i = len(dates) - 1
    hs300 = env["hs300"]

    # 逐日跑一遍所有信号（回测与当前判定同源）
    hits = []
    for s in SIGNALS:
        try:
            mask = np.array([bool(s["fn"](env, k)) for k in range(len(dates))], dtype=bool)
        except Exception:  # noqa: BLE001 —— 单信号算不动不影响其它
            mask = np.zeros(len(dates), dtype=bool)
        is_now = bool(mask[i]) if i < len(mask) else False
        if not is_now:
            continue
        stats = _bt(mask, hs300)
        text = _explain(s["id"], env, i, stats)
        calib = _calib(stats, term_h=text.pop("_term_h", None))
        hits.append({
            "id": s["id"], "tag": s["tag"], "level": s["level"], "title": s["title"],
            "verdict": text.get("verdict", ""), "facts": text.get("facts", []),
            "term": text.get("term", "短期"), "term_why": text.get("term_why", ""),
            "pattern": text.get("pattern", "中性"), "playbook": text.get("playbook", ""),
            "calib": calib,
            "stats": {str(h): stats.get(h, {"n": 0}) for h in HORIZONS},
            "hist_n": stats.get(5, {}).get("n", 0),
        })

    # 组合主线：把互相印证的信号串成一条判断（用户要求「全部内容密切关联」）
    theme = _theme_of(hits)

    # 维度打分 + 动态权重
    dims = _dim_scores(env, i)
    boost = {"panic": 1.0, "commodity": 1.0, "basis": 1.0, "cross": 1.0, "trend": 1.0}
    boost_note = []
    has = {h["id"] for h in hits}
    if "vix_spike" in has or "vix_high" in has:
        boost["panic"] = 1.70
        boost_note.append("VIX 高位/急升 → 恐慌维度权重 ×1.70")
    if "oil_spike" in has or "oil_high" in has:
        boost["commodity"] = 1.70
        boost_note.append("油价高位/急升 → 商品维度权重 ×1.70")
    if "basis_extreme" in has or "fs_diverge" in has:
        boost["basis"] = 1.35
        boost_note.append("基差异常 → 期现结构权重 ×1.35")
    if "cn_hk_inv" in has or "a50_inv" in has:
        boost["cross"] = 1.30
        boost_note.append("跨境背离 → 跨境维度权重 ×1.30")
    weights = _weights(dims, boost)
    temp = sum(dims[k] * weights[k] for k in weights)

    # 分档
    if temp >= 70:
        level, tone = "高危", "crisis"
    elif temp >= 50:
        level, tone = "偏紧", "warn"
    else:
        level, tone = "平稳", "calm"

    # 排序：crisis → warn → watch
    order = {"crisis": 0, "warn": 1, "watch": 2}
    hits.sort(key=lambda x: (order.get(x["level"], 3), -abs(x.get("hist_n", 0))))

    res = {
        "ok": True,
        "asof": dates[i],
        "elapsed": round(time.time() - t0, 2),
        "signals": hits,
        "dims": dims,
        "dim_names": DIM_NAME,
        "weights": weights,
        "boost_note": boost_note,
        "theme": theme,
        "temp": round(temp, 1),
        "level": level,
        "tone": tone,
        "crisis_count": len([h for h in hits if h["level"] == "crisis"]),
        "warn_count": len([h for h in hits if h["level"] == "warn"]),
    }
    if not hits:
        res["summary"] = "当前无联动异动（环境%s）" % level
    elif res["crisis_count"]:
        res["summary"] = "%s · %s" % (level, hits[0]["title"])
    else:
        res["summary"] = "%s · %s 等 %d 项" % (level, hits[0]["title"], len(hits))
    with _LOCK:
        _AN_CACHE["ts"], _AN_CACHE["data"] = now, res
    return res


# ============ 资产快照（hero 卡 + 境内矩阵） ============
def _state_of(a: dict, pct, price, q=None) -> dict:
    """给一张「状态标签」。只做粗分档，避免过度解读。

    VIX 四档（2026-11-01 按用户定的措辞）：
      ≥28 恐慌 · 20~28 待选择 · 15~20 常态 · <15 乐观
    """
    k = a["key"]
    if k == "vix":
        if price is None:
            return {"label": "—", "tone": "calm"}
        if price >= 28:
            return {"label": "恐慌", "tone": "crisis"}
        if price >= 20:
            return {"label": "待选择", "tone": "warn"}
        if price >= 15:
            return {"label": "常态", "tone": "calm"}
        return {"label": "乐观", "tone": "up"}
    if k == "brent":
        if q is not None and q >= 85:
            return {"label": "高位", "tone": "warn"}
        if q is not None and q <= 20:
            return {"label": "低位", "tone": "calm"}
        return {"label": "中性", "tone": "calm"}
    if pct is None:
        return {"label": "—", "tone": "calm"}
    if pct >= 1.5:
        return {"label": "强势", "tone": "up"}
    if pct <= -1.5:
        return {"label": "弱势", "tone": "down"}
    return {"label": "震荡", "tone": "calm"}


def _quote_block(key: str, spot: dict, env: Optional[dict] = None) -> dict:
    a = AMAP[key]
    bars = history(key)
    dates, close, high, low, hold = _series(bars) if bars else ([], np.array([]), None, None, None)
    q = dict(spot.get(key) or {})
    price = q.get("price")
    pct = q.get("pct")
    has_spot = price is not None
    if price is None and len(close):
        price = float(close[-1])
    # 涨跌幅：① 快照自带最可信 ② 没有则用日K 两根对比
    if pct is None and len(close) >= 2:
        pct = float((close[-1] / close[-2] - 1) * 100)
    # ③ 仅当「日K 明显落后于实时价」（=盘中还没生成今日K线）才用实时价推算。
    #    ★ 不能用 last_date < today 判断：过了 0 点 today 就变了，会把正常收盘
    #      算成 price/close-1 ≈ 0.00%（2026-10-31 凌晨实测踩到：恒生与沪深300 全变 0.00%）。
    if has_spot and len(close):
        try:
            drift = abs(float(price) / float(close[-1]) - 1.0)
        except (TypeError, ValueError, ZeroDivisionError):
            drift = 0.0
        if drift > 0.003:
            pct = (float(price) / float(close[-1]) - 1) * 100
    qq = None
    if env and key in ("vix", "brent"):
        arr = env.get("vix_pct" if key == "vix" else "brent_pct")
        if arr is not None and len(arr):
            qq = _arr_last(arr, len(arr) - 1)
    spark = [round(float(x), 4) for x in close[-120:]] if len(close) else []
    # 日期优先用快照自带的（盘中日K还没生成当天那根，用日K会显示成昨天）
    qd = str(q.get("date") or "").replace("/", "-")[:10]
    return {"key": key, "name": a["name"], "short": a["short"], "grp": a["grp"],
            "dec": a.get("dec", 2), "unit": a.get("unit", ""), "desc": a.get("desc", ""),
            "price": price, "pct": pct, "q": qq,
            "date": qd or (dates[-1] if dates else ""),
            "spark": spark,
            "state": _state_of(a, pct, price, qq)}


def panel(force: bool = False) -> dict:
    """一次拿全页面所需数据（悬浮窗与页签共用）。"""
    an = run_analysis(force=force)
    if not an.get("ok"):
        return an
    spot = fetch_spot(force=force)
    env = build_env(force=force)
    i = len(env["dates"]) - 1 if env else 0

    hero_keys = ["vix", "a50", "brent", "hsi"]
    hero = [_quote_block(k, spot, env) for k in hero_keys]
    aux = [_quote_block(k, spot, env) for k in ("vix9d", "hstech")]
    # 利率/流动性（政策面的可观测代理）
    rates = [_quote_block(k, spot, env) for k in ("T0",)]

    onshore = []
    for k in ("sh000001", "sh000300", "sh000016", "sh000905", "sh000852",
              "sz399006", "sh000688"):
        b = _quote_block(k, spot, env)
        if k == "sh000300":
            b["core"] = True
        onshore.append(b)

    fut = []
    for k in ("IF0", "IH0", "IC0", "IM0"):
        b = _quote_block(k, spot, env)
        bb = env.get("basis", {}).get(k) if env else None
        if bb:
            st = _basis_state(bb, i)
            b.update({"basis_pct": st["basis_pct"], "basis_rank": st["rank"],
                      "diff_pp": st["diff_pp"], "hold": st["hold"],
                      "hold_chg5": st["hold_chg5"], "rollover": st["rollover"],
                      "under": AMAP[k]["under"],
                      "under_name": AMAP[AMAP[k]["under"]]["name"]})
            # 年化基差：按**该品种当前主力合约的实际剩余天数**，不是固定 30 天
            ct = _contract_of(k)
            ann, ok_ann = _ann_basis(st["basis_pct"], ct.get("days"))
            b["basis_ann"] = ann
            b["basis_ann_ok"] = ok_ann
            b["contract"] = ct.get("code")
            b["contract_expiry"] = ct.get("expiry")
            b["contract_days"] = ct.get("days")
            b["contract_src"] = ct.get("src")
        fut.append(b)

    # 期限结构
    v, v9 = None, None
    if env:
        v = _arr_last(env["vix"], i)
        v9 = _arr_last(env["vix9d"], i)
    ts = {"vix": v, "vix9d": v9, "ratio": (v / v9) if (v and v9) else None}

    res = {"ok": True, "asof": an["asof"], "hero": hero, "aux": aux, "rates": rates,
           "onshore": onshore, "futures": fut, "term_structure": ts,
           "signals": an["signals"], "dims": an["dims"], "dim_names": an["dim_names"],
           "weights": an["weights"], "boost_note": an["boost_note"],
           "theme": an.get("theme"), "summary": an.get("summary"),
           "temp": an["temp"], "level": an["level"], "tone": an["tone"],
           "crisis_count": an["crisis_count"], "warn_count": an["warn_count"],
           "stale": sorted(k for k in _HIST_STALE if _HIST_STALE.get(k)),
           "fallbacks": dict(_FALLBACK),
           "spot_ok": spot.get("__ok__", False),
           "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "elapsed": an.get("elapsed")}
    return res


def kline(key: str, days: int = 260) -> dict:
    """K 线数据（含 MA5/20/60 与 BOLL，前端自绘）。"""
    a = AMAP.get(key)
    if not a:
        return {"ok": False, "err": "未知指标", "bars": []}
    bars = history(key)
    if not bars:
        return {"ok": False, "err": "暂无历史数据（数据源可能不可用）", "bars": []}
    days = max(60, min(int(days or 260), 1200))
    seg = bars[-days:]
    d, c, h, l, _ = _series(seg)
    ma = {}
    for n in (5, 20, 60):
        s = _sma(c, n)
        ma[n] = [None if not math.isfinite(x) else round(float(x), 3) for x in s]
    out = []
    for idx_ in range(len(d)):
        out.append({"d": d[idx_],
                    "o": round(float(seg[idx_]["open"]), 3),
                    "h": round(float(seg[idx_]["high"]), 3),
                    "l": round(float(seg[idx_]["low"]), 3),
                    "c": round(float(seg[idx_]["close"]), 3),
                    "v": round(float(seg[idx_]["volume"]), 1),
                    "ma5": ma[5][idx_], "ma20": ma[20][idx_], "ma60": ma[60][idx_],
                    "hold": round(float(seg[idx_]["hold"]), 1)})
    return {"ok": True, "key": key, "name": a["name"], "dec": a.get("dec", 2),
            "bars": out, "n": len(out)}


def meta_info() -> dict:
    """口径披露（前端「ⓘ 口径」展开）。"""
    return {
        "assets": [{"key": a["key"], "name": a["name"], "grp": a["grp"],
                    "kind": a["kind"], "desc": a.get("desc", ""),
                    "under": a.get("under"), "mult": a.get("mult")} for a in ASSETS],
        "groups": [{"k": "overseas", "n": "海外情绪"},
                   {"k": "offshore", "n": "离岸中国"},
                   {"k": "onshore", "n": "境内现货"},
                   {"k": "futures", "n": "境内期货"}],
        "sources": [
            "VIX / VIX9D：CBOE 官方日线 CSV（美东交易日收盘，北京次日凌晨可用，分析时滞后一根）",
            "布伦特原油 / 富时中国A50：新浪外盘期货连续报价（无换月跳空）",
            "恒生 / 恒生科技 / 境内指数：雪球 → 腾讯 ifzq → 新浪 三级自动切换，"
            "按实时健康度排序（失败 2 次冷却 10 分钟），页面会标注当前实际用到的源",
            "股指期货 IF/IH/IC/IM：新浪主连（主力合约拼接，换月日基差跳空已检测剔除）",
        ],
        "caveats": [
            "基差 = 期货收盘 − 现货收盘；「年化基差」按**该品种主力合约的实际剩余"
            "到期天数**线性年化（ACT/365），不再用固定 30 天",
            "主力合约的判定：优先用中金所当日成交量最大的合约；拿不到成交数据时"
            "按「当月第三个周五、提前 7 日移仓」的规则推算（节假日顺延未纳入，≤3 天误差）",
            "剩余不足 3 个自然日时年化基差不再展示：分母趋零会把噪声放大上百倍，"
            "那个数字没有意义",
            "主连不是单一合约。换月跳空按「基差水平的**持久位移**」识别："
            "跳变 ≥1.2pp，且跳变后 4 日的基差中位数仍保留该幅度的 70% 以上"
            "（真实的大波动会回归，换月不会）。四个品种在 2000 天里分别命中 6/7/9/12 次，"
            "只标真正的合约切换，不误伤日常波动",
            "历史标定用沪深300 作基准，样本量 <15 的期限不给结论",
            "风险温度是多维度加权的结果，只表示「环境松紧」，不是买卖信号；"
            "权重已做历史标定，结果与局限见「权重标定」区块",
            "所有指数日K都会落盘缓存，网络故障时用旧数据继续服务并在页脚标注",
        ],
        "terms": {str(k): v for k, v in TERM_NAME.items()},
        "horizons": list(HORIZONS),
    }

