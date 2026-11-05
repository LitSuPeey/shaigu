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


_TTL_VIX = 6 * 3600     # CBOE CSV（6 小时）


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
    """内盘期货 nf_：只在快照里取「最新价」，涨跌幅改用日K推算（不猜字段含义）。

    实测（2026-10-30 IF0 收盘 3928.0）：field[3] 与日K close 相等 → 即最新价。
    """
    return {"price": _f(parts[3]),
            "open": _f(parts[0]), "high": _f(parts[1]), "low": _f(parts[2]),
            "volume": _f(parts[4]), "amount": _f(parts[5]),
            "hold": _f(parts[6])}


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


def _cboe(sym: str = "VIX") -> list:
    """CBOE 官方日线 CSV。缓存 6 小时落盘（约 470KB，避免每次都拉）。"""
    name = "cboe_%s.json" % sym.lower()
    c = _cache_read(name, _TTL_VIX)
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
    ★ 腾讯会整体限流（2026-10-30 实测：hkHSI / sh000001 同时返回空），
      因此本函数只负责「把腾讯取到」，多级降级由 _fetch_hist 统一编排。
    """
    for nn in ([n] if n <= 2000 else [2000, 1200]):
        url = ("https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
               % (code, max(int(nn), 60)))
        for attempt in range(2):
            try:
                txt = _http(url, timeout=18)
                j = json.loads(txt or "{}")
            except Exception:  # noqa: BLE001
                j = {}
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
            if buf:
                return _bars(buf)
            if attempt == 0:
                time.sleep(0.5)
    return []


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


def _fetch_hist(a: dict) -> list:
    """多级降级取历史日K：按实时健康度在 雪球 / 腾讯 / 新浪 之间切换。"""
    k = a["kind"]
    if k == "cboe":
        return _cboe(a["sym"])
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

    bars = _fetch_hist(a)
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


# 首屏预热清单（并发拉取，避免 10 个源串行等 20s+）
_PREFETCH = ["vix", "vix9d", "brent", "a50", "hsi", "hstech",
             "sh000001", "sh000300", "sh000016", "sh000905", "sh000852",
             "sz399006", "sh000688", "IF0", "IH0", "IC0", "IM0", "T0"]


def prefetch(keys: Optional[list] = None, workers: int = 6) -> None:
    """并发预热历史日K（已缓存的 key 会直接命中，不发请求）。"""
    ks = keys or _PREFETCH
    todo = []
    now = time.time()
    for k in ks:
        with _LOCK:
            hit = _HIST_CACHE.get(k)
        if not (hit and now - hit[0] < _ttl_hist()):
            todo.append(k)
    if not todo:
        return
    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as ex:
            list(ex.map(history, todo))
    except Exception:  # noqa: BLE001 —— 预热失败不影响后续按需拉取
        pass


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


def _align_same(src_dates: list, src_vals: np.ndarray, targets: list) -> np.ndarray:
    """同日对齐（同市场同时段）。"""
    out = np.full(len(targets), np.nan)
    for i, d in enumerate(targets):
        j = bisect.bisect_left(src_dates, d)
        if j < len(src_dates) and src_dates[j] == d:
            out[i] = src_vals[j]
        elif j - 1 >= 0:
            out[i] = src_vals[j - 1]
    return out


def _basis(spot_bars: list, fut_bars: list) -> dict:
    """期现基差序列：同日对齐，返回 dict of arrays。

    主连换月跳空检测：当日基差相对前一日跳变 >1.2pp 且「期货涨跌幅 − 现货涨跌幅」
    的绝对值 <0.4pp（说明价格没动、是合约换了）→ 标记 rollover，信号不使用该点。
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
    rollover = np.zeros(n, dtype=bool)
    if n > 1:
        d_bs = np.abs(np.diff(basis_pct))
        d_px = np.abs(np.nan_to_num(fpct[1:] - spct[1:]))
        rollover[1:] = (np.nan_to_num(d_bs) > 1.2) & (d_px < 0.4)
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
        bars = history(key)
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
        bp = _align_same(b["dates"], b["basis_pct"], dates)
        sm = _align_same(b["dates"], b["smooth"], dates)
        roll = np.zeros(len(dates), dtype=bool)
        rd = b["dates"]
        for j, d in enumerate(rd):
            k = bisect.bisect_left(dates, d)
            if k < len(dates) and dates[k] == d:
                roll[k] = bool(b["rollover"][j])
        hold = _align_same(b["dates"], b["hold"], dates)
        env["basis"][fk] = {
            "pct": bp, "smooth": sm, "abs_rank": _basis_rank(bp), "roll": roll,
            "hold": hold, "hold_chg5": _ret(hold, 5),
            "fpct": _align_same(b["dates"], b["fpct"], dates),
            "spct": _align_same(b["dates"], b["spct"], dates),
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
    prefetch()
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
    return {"key": key, "name": a["name"], "short": a["short"], "grp": a["grp"],
            "dec": a.get("dec", 2), "unit": a.get("unit", ""), "desc": a.get("desc", ""),
            "price": price, "pct": pct, "q": qq,
            "date": (dates[-1] if dates else (q.get("date") or "")),
            "spark": spark,
            "state": _state_of(a, pct, price, qq)}


def panel(force: bool = False) -> dict:
    """一次拿全页面所需数据（悬浮窗与页签共用）。"""
    an = run_analysis(force=force)
    if not an.get("ok"):
        return an
    spot = fetch_spot(force=force)
    env = build_env()
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
            # 年化基差（按当月合约剩余天数近似 30 天）
            if st["basis_pct"] is not None:
                b["basis_ann"] = round(st["basis_pct"] * 365.0 / 30.0, 1)
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
            "基差 = 期货收盘 − 现货收盘，「年化基差」按剩余 30 天近似，仅作横向比较",
            "主连不是单一合约，换月前后基差不可比；本模块检测到跳空即标记并停用该点",
            "历史标定用沪深300 作基准，样本量 <15 的期限不给结论",
            "风险温度是多维度加权的结果，只表示「环境松紧」，不是买卖信号",
            "所有指数日K都会落盘缓存，网络故障时用旧数据继续服务并在页脚标注",
        ],
        "terms": {str(k): v for k, v in TERM_NAME.items()},
        "horizons": list(HORIZONS),
    }

