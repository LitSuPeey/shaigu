# -*- coding: utf-8 -*-
"""盯盘（Watchtower）—— 选定股票/股票组的实时盘面透视。

设计目标
========
用户给 1~N 只股票（或一个已保存的盯盘组），一次返回 7 个维度：

  ① 实时快照   现价/涨跌/量比/换手/振幅/流通市值/涨跌停价/均价
  ② 分时       1 分钟走势（价 + 均价 + 量 + 额），含日内节奏拆解（早盘/午后/尾盘）
  ③ 买卖席位   龙虎榜个股上榜历史 + 最近一次买卖前五席位 + 最近两次上榜的席位进出变动
  ④ 资金占比   双口径并列（见下「口径边界」第 2 条）
  ⑤ ETF 情况   哪些 ETF 持有它（季报口径，精确到基金）+ 这些 ETF 的实时表现 + 环比增减仓
  ⑥ 技术特征   均线/RSI/MACD/KDJ/BOLL/ATR/量能/相对强弱
  ⑦ 关键节点   到各关键价位的距离与方向（均线/轨道/前高前低/缺口/筹码峰/整数关/涨跌停）
  + 信号清单   规则触发的多空信号（带方向、强度、触发依据）

素材来源（全部为公开行情接口，逐个实测可用）
============================================
  - 腾讯实时快照   http://qt.gtimg.cn/q=          批量、0.2s/全市场、含涨跌停价与量比
  - 东财分时       push2his trends2（1 分钟，ndays 可到 5）
  - 东财历史资金流 push2his fflow/daykline（近 120 交易日五档净流入）
  - 东财龙虎榜     datacenter-web RPT_DAILYBILLBOARD_DETAILSNEW（个股上榜历史）
  - 东财席位明细   datacenter-web RPT_BILLBOARD_DAILYDETAILSBUY / SELL
  - 东财基金持仓   datacenter-web RPT_MAINDATA_MAIN_POSITIONDETAILS（按 SECURITY_CODE 反查，
                   一次拿到所有持有该股的基金/ETF，含持仓市值与占净值比）
  - 新浪 ETF 行情  vip.stock.finance.sina.com.cn Market_Center.getHQNodeData?node=etf_hq_fund
  - 本地日线       unified_data.db daily（近 300 交易日，前复权口径，用于技术面）

采纳什么 / 否决什么
==================
采纳：
  - 「双口径资金画像」—— 逐笔分档（每天都有，但只能到单笔规模）+ 龙虎榜席位（能到席位身份，
    但只有异动上榜日才有）。两个口径并列，不合成一个数。
  - 「席位身份关键词规则」—— 机构专用 / 沪深股通 / 证金汇金 / 拉萨系 / 知名游资营业部 /
    其余营业部，六类。规则全部显式写在 _SEAT_RULES 里，前端可展开查看。
  - 「关键节点按距离排序」—— 不做加权评分，只给「离现价多远、在上方还是下方」，
    让用户自己判断哪个位置重要。
  - 「ETF 持仓用季报精确穿透」—— 相比「按行业名模糊匹配 ETF」，前者是真实持仓数据。

否决：
  - **否决「盯盘评分」**。把 7 个维度线性加权成一个分数需要各维度的收益贡献权重，
    本项目没有任何实证支持这些权重（beton 的教训：分数对未来收益 IC≈0）。
    改成分维度展示 + 规则化信号，不给总分。
  - **否决「主力控盘度」「资金进场意愿」这类合成指标**。它们没有统一定义，只能靠拍脑袋。
  - **否决「预测明日涨跌」**。信号只描述「当下发生了什么」，不预测。
  - **否决把逐笔大单等同于机构**。同一笔 100 万买单可能来自公募、游资或大户，
    数据本身没有身份字段，这是硬边界。

口径边界（必须让用户看到）
========================
1. **席位分类是关键词规则，不是官方口径**。交易所只披露营业部名称，不披露背后是机构还是游资。
   我们的六分类是基于名称的规则推断，会有误判（例如「机构专用」是官方席位类型，可信度最高；
   而「某营业部是知名游资」是市场长期流传的说法，可能已经换人）。
2. **逐笔分档无法识别身份**。东财的五档是「单笔成交金额」分档（超大单/大单/中单/小单），
   只能说明「大钱在买还是小钱在买」，不能说明「是谁在买」。
3. **龙虎榜只覆盖异动日**。涨幅偏离、换手异常、振幅异常才上榜，普通交易日没有席位数据。
   「最近一次上榜」可能是几周前甚至几年前（茅台 2007/2013 各一次），必须显示日期。
4. **ETF 持仓是季报口径，滞后 1~3 个月**。且基金季报只披露前十大重仓，非前十大不显示
   → 小市值股票的 ETF 覆盖会被低估。
5. **北向资金已无实时逐笔**（2024-08 起交易所停止盘中披露）。本模块的「北向」只来自
   龙虎榜的「沪股通/深股通专用」席位，覆盖度有限。
6. **技术面基于本地近 300 个交易日**。MA250 之外的更长周期（3 年线、历史筹码）不在本模块，
   那部分看「🧭 大周期」页签。
7. **分时是当日快照**，盘后拿到的就是收盘定格，不包含盘后固定价格交易。

免责声明
========
本模块只做数据聚合与规则化特征提取，不构成任何投资建议。所有「信号」都是对历史与
当下价量关系的机械描述，不预示未来收益。市场有风险，决策需独立判断。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from ..core import db as core_db
from .alert import _UA, _net_call

# ============ 可调参数（前端「⚙ 盯盘参数」镜像本表） ============
DEFAULTS: dict = {
    "intraday_days": 1,      # 分时天数 1/3/5
    "seat_lookback": 120,    # 龙虎榜回看自然日
    "seat_topn": 5,          # 每侧展示席位数
    "etf_topn": 8,           # 展示的 ETF 个数
    "node_tolerance": 1.0,   # 「贴近」阈值（%）
    "node_show": 14,         # 节点表最多展示条数
    "tech_bars": 250,        # 技术面回看的交易日数
    "max_codes": 30,         # 单次最多盯几只
}

_UA_JSON = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
_UA_DATA = {"User-Agent": _UA, "Referer": "https://data.eastmoney.com/"}

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_WATCHLIST_PATH = os.path.join(_ROOT, "data", "monitor_watchlist.json")
_WL_LOCK = threading.Lock()


def _ensure_db() -> None:
    """确保 core_db 已绑定数据库路径。

    服务进程里 main.py 已 set_db_path；但模块被独立脚本 / 单元测试 import 时
    路径为空 → core_db.reader() 会去打开当前工作目录而报错。这里做幂等兜底。
    """
    if core_db.db_path():
        return
    core_db.set_db_path(os.path.join(_ROOT, "data", "unified_data.db"))

_EM_UT = "7eea3edcaed734bea9cbfc24409ed989"   # sync.py 实测可用的公开 ut
_SESS = None


def _sess():
    """模块级共享 Session（东财无 cookie 依赖，多线程共享安全；同 sync._em_sess）。"""
    global _SESS
    if _SESS is None:
        import requests
        _SESS = requests.Session()
    return _SESS


def _get(url: str, params: dict, headers: dict | None = None, timeout: int = 12):
    """GET + JSON。走 alert._net_call（带代理/直连切换重试）。"""
    def _do():
        r = _sess().get(url, params=params, headers=headers or _UA_JSON, timeout=timeout)
        r.raise_for_status()
        return r.json()
    return _net_call(_do)


def _get_text(url: str, headers: dict | None = None, timeout: int = 12,
              encoding: str | None = None) -> str:
    def _do():
        r = _sess().get(url, headers=headers or {"User-Agent": _UA}, timeout=timeout)
        r.raise_for_status()
        if encoding:
            r.encoding = encoding
        return r.text
    return _net_call(_do)


# ============ 市场/代码工具 ============
def _secid(code: str) -> str:
    """6 位代码 → 东财 secid。

    沪市 = 1.：6xx（主板/科创板）、**5xx（ETF/LOF）**、9xx（沪 B 股）
    深市 = 0.：0xx（主板）、3xx（创业板）、**15x/16x（ETF/LOF）**、2xx（深 B 股）
    ★ 5 开头是沪市 ETF、1 开头是深市 ETF ——「只有 6 开头算沪市」的简化规则会把
    562660 / 561370 这类沪市 ETF 全判成深市（实测踩到：ETF 行情整列为空）。
    """
    c = str(code).zfill(6)
    # 北交所先判：92xxxx 虽以 9 开头，但东财走 0.（与 sync._fetch_daily_em 实测一致）
    if c.startswith(("43", "83", "87", "88", "92")):
        return f"0.{c}"
    return f"1.{c}" if c[0] in ("5", "6", "9") else f"0.{c}"


def _tx_symbol(code: str) -> str:
    """6 位代码 → 腾讯行情符号。

    北交所先判（43/83/87/88/92），防 920 被误判为沪市；
    之后沪市 = 5xx/6xx/9xx，其余深市（含 15x/16x ETF）。
    """
    c = str(code).zfill(6)
    if c.startswith(("43", "83", "87", "88", "92")):
        return "bj" + c
    if c[0] in ("5", "6", "9"):
        return "sh" + c
    return "sz" + c


def _is_bj(code: str) -> bool:
    return str(code).zfill(6).startswith(("43", "83", "87", "88", "92"))


def _f(v, default=None):
    """宽松转 float，非有限值返回 default。"""
    try:
        if v is None or v == "" or v == "-":
            return default
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _r(v, nd: int = 2):
    """四舍五入 + 非有限值转 None（防 NaN 进 JSON）。"""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return round(x, nd)


def _pct(a, b):
    """a 相对 b 的百分比变化。"""
    if b in (None, 0) or a is None:
        return None
    return (float(a) - float(b)) / float(b) * 100.0


# ============ 交易时段 ============
def _market_phase(now: datetime | None = None) -> dict:
    """返回 {phase, label, is_trading}。phase ∈ pre/open/lunch/close/holiday/off。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return {"phase": "holiday", "label": "周末休市", "is_trading": False}
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 15:
        return {"phase": "pre", "label": "盘前", "is_trading": False}
    if hm < 9 * 60 + 30:
        return {"phase": "auction", "label": "集合竞价", "is_trading": False}
    if hm <= 11 * 60 + 30:
        return {"phase": "open", "label": "盘中", "is_trading": True}
    if hm < 13 * 60:
        return {"phase": "lunch", "label": "午间休市", "is_trading": False}
    if hm <= 15 * 60:
        return {"phase": "open", "label": "盘中", "is_trading": True}
    return {"phase": "close", "label": "已收盘", "is_trading": False}


# ============ ① 实时快照（腾讯批量） ============
# 字段下标来自项目既有实现（core/sync.py _TX_SPOT_IDX 及 ticker.py 的实测索引）
_TX_IDX = {"name": 1, "code": 2, "price": 3, "prev_close": 4, "open": 5,
           "volume": 6, "time": 30, "chg": 31, "pct": 32, "high": 33, "low": 34,
           "amount_wan": 37, "turnover": 38, "amplitude": 43, "float_cap": 44,
           "total_cap": 45, "limit_up": 47, "limit_down": 48, "vol_ratio": 49,
           "avg_price": 51}


def _fetch_quotes(codes: list[str]) -> dict:
    """批量实时快照。返回 {code: {...}}；整体失败返回 {}（调用方降级到本地日线）。"""
    if not codes:
        return {}
    syms = ",".join(_tx_symbol(c) for c in codes)
    try:
        text = _get_text("http://qt.gtimg.cn/q=" + syms,
                         headers={"User-Agent": _UA}, encoding="gbk", timeout=10)
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or '="' not in line:
            continue
        f = line.partition('="')[2].rstrip('"').split("~")
        if len(f) < 52:
            continue
        code = str(f[_TX_IDX["code"]]).zfill(6)
        price = _f(f[_TX_IDX["price"]])
        prev = _f(f[_TX_IDX["prev_close"]])
        vol = _f(f[_TX_IDX["volume"]], 0.0) or 0.0
        # 腾讯成交量单位不统一（主板=手、科创板=股、北交所=股）→ 用「额/均价」自洽判定
        amt = _f(f[_TX_IDX["amount_wan"]], 0.0) or 0.0
        avg = _f(f[_TX_IDX["avg_price"]])
        vol_shares = vol * 100.0
        if amt > 0 and price:
            implied = amt * 10000.0 / price          # 按「手」换算出的股数
            if vol and abs(implied / (vol * 100.0) - 1) > 0.5 and abs(implied / vol - 1) < 0.5:
                vol_shares = vol                      # 实际单位是「股」
        out[code] = {
            "code": code,
            "name": str(f[_TX_IDX["name"]] or "").strip(),
            "price": price,
            "prev_close": prev,
            "open": _f(f[_TX_IDX["open"]]),
            "high": _f(f[_TX_IDX["high"]]),
            "low": _f(f[_TX_IDX["low"]]),
            "pct": _f(f[_TX_IDX["pct"]]),
            "chg": _f(f[_TX_IDX["chg"]]),
            "volume": vol_shares,
            "amount": amt * 10000.0,
            "turnover": _f(f[_TX_IDX["turnover"]]),
            "amplitude": _f(f[_TX_IDX["amplitude"]]),
            "float_cap": _f(f[_TX_IDX["float_cap"]]),
            "total_cap": _f(f[_TX_IDX["total_cap"]]),
            "limit_up": _f(f[_TX_IDX["limit_up"]]),
            "limit_down": _f(f[_TX_IDX["limit_down"]]),
            "vol_ratio": _f(f[_TX_IDX["vol_ratio"]]),
            "avg_price": avg,
            "quote_time": str(f[_TX_IDX["time"]] or ""),
        }
    return out


# ============ ② 分时 ============
def fetch_trends_raw(secid: str, days: int = 5):
    """通用分时拉取（个股或指数都能用）。返回 (points, name, pre_close)。

    ★指数与个股走同一个 trends2 接口，区别只在 secid：
      上证 1.000001 / 深证成指 0.399001 / 北证50 0.899050
      ta_ctx 的「受压制率」要用它拉指数分时，所以把这段单独抽出来复用。
    """
    days = max(1, min(5, int(days or 5)))
    j = _get("http://push2his.eastmoney.com/api/qt/stock/trends2/get",
             {"secid": secid, "ut": _EM_UT,
              "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
              "iscr": 0, "ndays": days})
    d = (j or {}).get("data") or {}
    rows = d.get("trends") or []
    pts = []
    for r in rows:
        f = str(r).split(",")
        if len(f) < 8:
            continue
        pts.append([f[0], _f(f[2]), _f(f[7]), _f(f[5], 0.0), _f(f[6], 0.0)])
    return pts, d.get("name"), _f(d.get("preClose"))


def secid_of(code: str) -> str:
    """对外暴露 secid 规则（ta_ctx 拉个股分时要用）。"""
    return _secid(code)


def fetch_intraday(code: str, days: int = 1) -> dict:
    """东财 1 分钟分时。返回 {date, pre_close, points:[[t,price,avg,vol,amt]], stats}。

    trends2 的 fields2 = f51..f58 → 时间,开,收,高,低,量(手),额(元),均价。
    ndays 支持 1~5，返回的是「最近 N 个交易日」的连续分钟序列（含跨日）。
    """
    days = max(1, min(5, int(days or 1)))
    try:
        j = _get("http://push2his.eastmoney.com/api/qt/stock/trends2/get",
                 {"secid": _secid(code), "ut": _EM_UT,
                  "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                  "iscr": 0, "ndays": days})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": f"分时拉取失败: {str(e)[:120]}", "points": []}
    d = (j or {}).get("data") or {}
    rows = d.get("trends") or []
    pts = []
    for r in rows:
        f = str(r).split(",")
        if len(f) < 8:
            continue
        pts.append([f[0], _f(f[2]), _f(f[7]), _f(f[5], 0.0), _f(f[6], 0.0)])
    if not pts:
        return {"ok": False, "err": "分时无数据（可能停牌）", "points": []}
    prices = [p[1] for p in pts if p[1] is not None]
    vols = [p[3] for p in pts if p[3] is not None]
    pre = _f(d.get("preClose"))
    # 日内节奏：按时间切片统计成交额占比，用于识别「早盘猛攻 / 尾盘偷袭」
    def _seg(t_lo, t_hi, same_day=True):
        a = 0.0
        for p in pts:
            ts = str(p[0])
            hhmm = ts[11:16] if len(ts) >= 16 else ""
            if not hhmm:
                continue
            if t_lo <= hhmm <= t_hi:
                a += (p[4] or 0.0)
        return a
    tot_amt = sum((p[4] or 0.0) for p in pts) or 0.0
    seg = {
        "早盘(9:30-10:30)": _seg("09:30", "10:30"),
        "午前(10:30-11:30)": _seg("10:30", "11:30"),
        "午后(13:00-14:00)": _seg("13:00", "14:00"),
        "尾盘(14:00-15:00)": _seg("14:00", "15:00"),
    }
    seg_pct = {k: _r(v / tot_amt * 100, 1) if tot_amt > 0 else None for k, v in seg.items()}
    # 每日切分（多日分时时前端按日期分组画）
    by_day: dict = {}
    for p in pts:
        by_day.setdefault(str(p[0])[:10], []).append(p[1])
    day_stats = []
    for dt, ps in by_day.items():
        valid = [x for x in ps if x is not None]
        if not valid:
            continue
        day_stats.append({"date": dt, "high": max(valid), "low": min(valid),
                          "last": valid[-1], "n": len(valid)})
    return {
        "ok": True,
        "pre_close": pre,
        "points": pts,
        "stats": {
            "high": _r(max(prices)) if prices else None,
            "low": _r(min(prices)) if prices else None,
            "last": _r(prices[-1]) if prices else None,
            "vwap": _r(sum(p[1] * p[3] for p in pts if p[1] and p[3]) /
                       max(1e-9, sum(p[3] for p in pts if p[3])) if tot_amt else None),
            "amp_pct": _r(_pct(max(prices), min(prices))) if prices else None,
            "seg_amount_pct": seg_pct,
            "tail_strong": bool(seg_pct.get("尾盘(14:00-15:00)") and
                                seg_pct["尾盘(14:00-15:00)"] > 25),
            "days": day_stats,
            "n_points": len(pts),
        },
    }


# ============ ③ 历史资金流（五档逐笔） ============
def fetch_fund_flow(code: str, days: int = 60) -> dict:
    """东财 push2his fflow/daykline。返回近 N 交易日五档净流入序列。

    fields2 实测顺序（与 fflow/kline 一致）：
      f51 日期, f52 主力净流入, f53 小单, f54 中单, f55 大单, f56 超大单,
      f57 主力净占比, f58 小单占比, f59 中单占比, f60 大单占比, f61 超大单占比,
      f62 收盘价, f63 涨跌幅
    校验：主力 = 大单 + 超大单（东财口径），实测逐日成立。
    """
    try:
        j = _get("http://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                 {"lmt": 0, "klt": 101, "secid": _secid(code), "ut": _EM_UT,
                  "fields1": "f1,f2,f3,f7",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": f"资金流拉取失败: {str(e)[:120]}", "rows": []}
    kl = ((j or {}).get("data") or {}).get("klines") or []
    rows = []
    for r in kl[-max(1, days):]:
        f = str(r).split(",")
        if len(f) < 13:
            continue
        rows.append({
            "date": f[0],
            "main": _f(f[1], 0.0),
            "small": _f(f[2], 0.0),
            "mid": _f(f[3], 0.0),
            "large": _f(f[4], 0.0),
            "xlarge": _f(f[5], 0.0),
            "main_pct": _f(f[6]),
            "close": _f(f[11]),
            "pct": _f(f[12]),
        })
    if not rows:
        return {"ok": False, "err": "资金流无数据", "rows": []}
    return {"ok": True, "rows": rows}


def _flow_summary(rows: list, n: int) -> dict:
    """近 n 日资金净流入合计（元）。"""
    if not rows:
        return {"main": None, "xlarge": None, "large": None, "mid": None,
                "small": None, "days": 0}
    sel = rows[-n:]
    return {
        "main": sum(x["main"] or 0 for x in sel),
        "xlarge": sum(x["xlarge"] or 0 for x in sel),
        "large": sum(x["large"] or 0 for x in sel),
        "mid": sum(x["mid"] or 0 for x in sel),
        "small": sum(x["small"] or 0 for x in sel),
        "days": len(sel),
    }


# ============ ④ 龙虎榜 + 席位 ============
_SEAT_RULES = [
    # (类别, 关键词列表, 说明) —— 顺序敏感：先匹配先归类
    ("机构", ["机构专用"], "交易所席位类型，代表公募/私募/险资/社保等机构专用通道"),
    ("国家队", ["中国证券金融", "中央汇金", "证金", "汇金", "国新投资"], "证金/汇金/国新系，市场统称国家队"),
    ("北向", ["沪股通", "深股通", "港股通", "陆股通"], "沪深港通专用席位（外资/北向）"),
    ("散户", ["东方财富证券股份有限公司拉萨", "西藏东方财富", "拉萨团结路", "拉萨东环路",
             "拉萨北京中路", "拉萨江苏东路", "拉萨八一路", "拉萨金珠西路"],
     "东方财富拉萨系营业部，市场俗称「拉萨天团」，以散户与量化小单为主"),
]
# 市场长期流传的知名游资营业部（非官方口径，可能已换人/迁移，仅作参考标签）
_HOT_MONEY_DEPTS = [
    "华鑫证券有限责任公司上海分公司", "中国银河证券股份有限公司绍兴证券营业部",
    "国泰君安证券股份有限公司上海江苏路证券营业部", "财通证券股份有限公司杭州上塘路证券营业部",
    "华泰证券股份有限公司深圳益田路荣超商务中心证券营业部",
    "中信证券股份有限公司上海溧阳路证券营业部", "东方证券股份有限公司上海浦东新区银城中路证券营业部",
    "中国中投证券有限责任公司无锡清扬路证券营业部", "光大证券股份有限公司佛山绿景路证券营业部",
    "国信证券股份有限公司深圳泰然九路证券营业部", "招商证券股份有限公司深圳蛇口工业七路证券营业部",
    "华泰证券股份有限公司厦门厦禾路证券营业部", "国泰君安证券股份有限公司成都北一环路证券营业部",
    "中信建投证券股份有限公司北京三里河路证券营业部", "海通证券股份有限公司上海建国西路证券营业部",
    "方正证券股份有限公司杭州延安路证券营业部", "长江证券股份有限公司上海世纪大道证券营业部",
    "中国银河证券股份有限公司北京中关村大街证券营业部", "安信证券股份有限公司上海虹桥路证券营业部",
    "国元证券股份有限公司上海中山北路证券营业部", "申万宏源证券有限公司上海闵行区东川路证券营业部",
    "华福证券有限责任公司福州湖东路证券营业部", "中山证券有限责任公司深圳深南大道证券营业部",
]


def classify_seat(dept: str) -> dict:
    """席位名称 → {cat, why}。六分类，规则见 _SEAT_RULES / _HOT_MONEY_DEPTS。"""
    name = str(dept or "").strip()
    if not name:
        return {"cat": "未知", "why": "席位名称为空"}
    for cat, kws, why in _SEAT_RULES:
        for kw in kws:
            if kw in name:
                return {"cat": cat, "why": why}
    for h in _HOT_MONEY_DEPTS:
        if h and (h in name or name in h):
            return {"cat": "游资", "why": "市场长期流传的知名游资营业部（非官方口径，可能已换人）"}
    return {"cat": "其他营业部", "why": "非机构专用、非沪深股通、非国家队、非拉萨系、未命中游资名单"}


def fetch_lhb(code: str, lookback_days: int = 120) -> dict:
    """个股龙虎榜上榜历史 + 最近一次买卖前五席位。

    返回 {ok, records:[...], latest:{date, buy:[], sell:[]}, notes:[]}
    """
    since = (datetime.now() - timedelta(days=max(7, int(lookback_days)))).strftime("%Y-%m-%d")
    try:
        j = _get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                 {"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL",
                  "filter": f'(SECURITY_CODE="{code}")(TRADE_DATE>=\'{since}\')',
                  "pageNumber": 1, "pageSize": 50,
                  "sortColumns": "TRADE_DATE", "sortTypes": "-1",
                  "source": "WEB", "client": "WEB"},
                 headers=_UA_DATA, timeout=15)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": f"龙虎榜拉取失败: {str(e)[:120]}",
                "records": [], "latest": None}
    data = ((j or {}).get("result") or {}).get("data") or []
    recs = []
    for x in data:
        recs.append({
            "date": str(x.get("TRADE_DATE") or "")[:10],
            "reason": str(x.get("EXPLAIN") or x.get("EXPLANATION") or "").strip(),
            "net": _f(x.get("BILLBOARD_NET_AMT")),
            "buy": _f(x.get("BILLBOARD_BUY_AMT")),
            "sell": _f(x.get("BILLBOARD_SELL_AMT")),
            "deal": _f(x.get("BILLBOARD_DEAL_AMT")),
            "deal_ratio": _f(x.get("DEAL_AMOUNT_RATIO")),
            "close": _f(x.get("CLOSE_PRICE")),
            "chg": _f(x.get("CHANGE_RATE")),
            "turnover": _f(x.get("TURNOVERRATE")),
            "after": {"d1": _f(x.get("D1_CLOSE_ADJCHRATE")),
                      "d2": _f(x.get("D2_CLOSE_ADJCHRATE")),
                      "d5": _f(x.get("D5_CLOSE_ADJCHRATE")),
                      "d10": _f(x.get("D10_CLOSE_ADJCHRATE"))},
        })
    out = {"ok": True, "records": recs, "latest": None, "prev": None,
           "tolerance_note": f"仅统计近 {lookback_days} 个自然日的上榜记录"}
    if not recs:
        return out
    # 去重取最近两个不同日期（同日多原因只取成交额最大的一条做席位查询）
    by_date: dict = {}
    for r in recs:
        if r["date"] not in by_date or (r["deal"] or 0) > (by_date[r["date"]]["deal"] or 0):
            by_date[r["date"]] = r
    dates = sorted(by_date.keys(), reverse=True)
    out["latest"] = _fetch_seat_day(code, dates[0])
    if out["latest"]:
        out["latest"]["record"] = by_date[dates[0]]
    if len(dates) > 1:
        out["prev"] = _fetch_seat_day(code, dates[1])
        if out["prev"]:
            out["prev"]["record"] = by_date[dates[1]]
    return out


def _fetch_seat_day(code: str, date: str) -> dict | None:
    """某日买卖前五席位。返回 {date, buy:[...], sell:[...], mix:{cat: {buy,sell,net,n}}}。"""
    out = {"date": date, "buy": [], "sell": []}
    for side, rpt in (("buy", "RPT_BILLBOARD_DAILYDETAILSBUY"),
                      ("sell", "RPT_BILLBOARD_DAILYDETAILSSELL")):
        try:
            j = _get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                     {"reportName": rpt, "columns": "ALL",
                      "filter": f'(SECURITY_CODE="{code}")(TRADE_DATE=\'{date}\')',
                      "pageNumber": 1, "pageSize": 20,
                      "source": "WEB", "client": "WEB"},
                     headers=_UA_DATA, timeout=15)
        except Exception:  # noqa: BLE001
            continue
        for x in (((j or {}).get("result") or {}).get("data") or []):
            dept = str(x.get("OPERATEDEPT_NAME") or "").strip()
            cls = classify_seat(dept)
            out[side].append({
                "dept": dept,
                "cat": cls["cat"],
                "why": cls["why"],
                "buy": _f(x.get("BUY"), 0.0),
                "sell": _f(x.get("SELL"), 0.0),
                "net": _f(x.get("NET"), 0.0),
                "buy_ratio": _f(x.get("TOTAL_BUYRIO")),
                "sell_ratio": _f(x.get("TOTAL_SELLRIO")),
                "rise_prob_3d": _f(x.get("RISE_PROBABILITY_3DAY")),
            })
        out[side].sort(key=lambda z: -((z["buy"] or 0) if side == "buy" else (z["sell"] or 0)))
    if not out["buy"] and not out["sell"]:
        return None
    # 六分类汇总（买卖两侧合并，净额 = 买 - 卖）
    mix: dict = {}
    for side in ("buy", "sell"):
        for s in out[side]:
            m = mix.setdefault(s["cat"], {"cat": s["cat"], "buy": 0.0, "sell": 0.0, "n": 0})
            m["buy"] += (s["buy"] or 0.0)
            m["sell"] += (s["sell"] or 0.0)
            m["n"] += 1
    for m in mix.values():
        m["net"] = m["buy"] - m["sell"]
    out["mix"] = sorted(mix.values(), key=lambda z: -abs(z["net"]))
    tot = sum(abs(m["net"]) for m in out["mix"]) or 0.0
    for m in out["mix"]:
        m["net_share"] = _r(abs(m["net"]) / tot * 100, 1) if tot else None
    return out


def _seat_changes(cur: dict | None, prev: dict | None) -> dict:
    """最近两次上榜的席位构成变动：新进 / 消失 / 留存。"""
    if not cur or not prev:
        return {"ok": False, "note": "上榜次数不足两次，无法对比变动"}
    def _key(side):
        return {s["dept"] for s in (cur.get(side) or [])}
    def _pkey(side):
        return {s["dept"] for s in (prev.get(side) or [])}
    b_new, b_out = _key("buy") - _pkey("buy"), _pkey("buy") - _key("buy")
    s_new, s_out = _key("sell") - _pkey("sell"), _pkey("sell") - _key("sell")
    b_keep, s_keep = _key("buy") & _pkey("buy"), _key("sell") & _pkey("sell")
    cat_cur = {m["cat"]: m for m in (cur.get("mix") or [])}
    cat_prev = {m["cat"]: m for m in (prev.get("mix") or [])}
    cat_delta = []
    for c in set(cat_cur) | set(cat_prev):
        a = (cat_cur.get(c) or {}).get("net", 0.0) or 0.0
        b = (cat_prev.get(c) or {}).get("net", 0.0) or 0.0
        cat_delta.append({"cat": c, "cur_net": a, "prev_net": b, "delta": a - b})
    cat_delta.sort(key=lambda z: -abs(z["delta"]))
    return {
        "ok": True,
        "prev_date": prev.get("date"),
        "cur_date": cur.get("date"),
        "buy_new": sorted(b_new), "buy_out": sorted(b_out), "buy_keep": sorted(b_keep),
        "sell_new": sorted(s_new), "sell_out": sorted(s_out), "sell_keep": sorted(s_keep),
        "cat_delta": cat_delta,
    }


# ============ ⑤ ETF 持仓（季报口径） ============
_REPORT_CACHE: dict = {}


def _latest_reports(today: datetime | None = None) -> list[str]:
    """最近两个已披露的报告期（YYYY-MM-DD），按新→旧。

    与 alert._latest_report_date 同规则：Q1→4/30、H1→8/31、Q3→10/31、年报→次年4/30。
    """
    today = today or datetime.now()
    done = []
    for y in (today.year, today.year - 1):
        for m, d, dl_m, dl_d, dl_y in ((3, 31, 4, 30, y), (6, 30, 8, 31, y),
                                       (9, 30, 10, 31, y), (12, 31, 4, 30, y + 1)):
            if datetime(dl_y, dl_m, dl_d) <= today:
                done.append(datetime(y, m, d).strftime("%Y-%m-%d"))
    done.sort(reverse=True)
    return done[:2]


def _etf_rows_for(code: str, report_date: str, max_pages: int = 2) -> list:
    """拉某报告期持有该股的全部股东（基金+法人），最多 max_pages 页（每页 500）。"""
    key = f"{code}|{report_date}"
    if key in _REPORT_CACHE:
        return _REPORT_CACHE[key]
    rows: list = []
    for page in range(1, max_pages + 1):
        try:
            j = _get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                     {"sortColumns": "HOLD_MARKET_CAP", "sortTypes": "-1",
                      "pageSize": 500, "pageNumber": page,
                      "reportName": "RPT_MAINDATA_MAIN_POSITIONDETAILS", "columns": "ALL",
                      "filter": f'(SECURITY_CODE="{code}")(REPORT_DATE=\'{report_date}\')',
                      "source": "WEB", "client": "WEB"},
                     headers=_UA_DATA, timeout=20)
        except Exception:  # noqa: BLE001
            break
        data = ((j or {}).get("result") or {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        if len(data) < 500:
            break
    _REPORT_CACHE[key] = rows
    return rows


def fetch_etf_holdings(code: str, topn: int = 8) -> dict:
    """该股被哪些 ETF 持有（季报口径）+ 环比上一报告期的增减仓。"""
    reps = _latest_reports()
    if not reps:
        return {"ok": False, "err": "无法确定报告期", "holdings": []}
    cur_rep = reps[0]
    prev_rep = reps[1] if len(reps) > 1 else None
    cur_rows = _etf_rows_for(code, cur_rep)
    if not cur_rows:
        return {"ok": False, "err": f"{cur_rep} 无持仓披露（可能未进入任何基金十大重仓）",
                "report_date": cur_rep, "holdings": []}
    prev_rows = _etf_rows_for(code, prev_rep) if prev_rep else []
    prev_map = {}
    for x in prev_rows:
        nm = str(x.get("HOLDER_NAME") or "")
        if "ETF" in nm:
            prev_map[nm] = _f(x.get("HOLD_MARKET_CAP"), 0.0) or 0.0

    def _is_etf(n): return "ETF" in str(n or "") and "联接" not in str(n or "")

    etfs = []
    for x in cur_rows:
        nm = str(x.get("HOLDER_NAME") or "").strip()
        if not _is_etf(nm):
            continue
        cap = _f(x.get("HOLD_MARKET_CAP"), 0.0) or 0.0
        etfs.append({
            "name": nm,
            "code": str(x.get("HOLDER_CODE") or "").strip(),
            "cap": cap,
            "shares_ratio": _f(x.get("TOTAL_SHARES_RATIO")),
            "free_ratio": _f(x.get("FREE_SHARES_RATIO")),
            "nav_ratio": _f(x.get("NETASSET_RATIO")),
            "prev_cap": prev_map.get(nm),
            "delta_cap": (cap - prev_map[nm]) if nm in prev_map else None,
        })
    etfs.sort(key=lambda z: -(z["cap"] or 0))
    total = sum(e["cap"] for e in etfs) or 0.0
    wide_kw = ("沪深300", "上证50", "中证500", "中证1000", "中证2000", "A500", "创业板", "科创50",
               "深证100", "上证180", "MSCI", "国证2000", "中证A")
    wide = [e for e in etfs if any(k in e["name"] for k in wide_kw)]
    # 宽基 ETF 的代码（6 位）用于取实时行情
    out_hold = etfs[:max(1, int(topn))]
    return {
        "ok": True,
        "report_date": cur_rep,
        "prev_report_date": prev_rep,
        "n_etf": len(etfs),
        "n_total_holders": len(cur_rows),
        "total_cap": total,
        "wide_cap": sum(e["cap"] for e in wide) or 0.0,
        "n_wide": len(wide),
        "holdings": out_hold,
        "delta_up": [e for e in sorted(etfs, key=lambda z: -(z["delta_cap"] or 0))
                     if (e["delta_cap"] or 0) > 0][:3],
        "delta_down": [e for e in sorted(etfs, key=lambda z: (z["delta_cap"] or 0))
                       if (e["delta_cap"] or 0) < 0][:3],
        "note": "基金季报只披露前十大重仓，未进前十大的持仓不显示；数据滞后 1~3 个月",
    }


# ---- ETF 实时行情：复用腾讯批量快照（一次请求，比新浪分页全量快 10 倍） ----
def attach_etf_quotes(hold: dict) -> dict:
    """给 ETF 持仓列表补实时行情；再汇总「同主题 ETF 今日表现」。

    行情源用腾讯批量快照（qt.gtimg.cn，支持 ETF，与个股同一套解析），
    避免新浪 ETF 列表接口「单页上限 100 条 → 全量要拉 12 页」的开销。
    """
    if not hold.get("ok"):
        return hold
    holds = hold.get("holdings") or []
    codes = [e["code"] for e in holds if re.fullmatch(r"\d{6}", str(e.get("code") or ""))]
    quotes = _fetch_quotes(codes) if codes else {}
    if not quotes:
        hold["quote_ok"] = False
        return hold
    pcts, amts = [], 0.0
    for e in holds:
        q = quotes.get(str(e.get("code") or ""))
        if q:
            e["pct"] = q["pct"]
            e["amount"] = q["amount"]
            e["turnover"] = q["turnover"]
            e["scale"] = (q["float_cap"] * 1e8) if q.get("float_cap") else None
            if q["pct"] is not None:
                pcts.append(q["pct"])
            amts += q["amount"] or 0.0
        else:
            e["pct"] = None
            e["amount"] = None
            e["turnover"] = None
            e["scale"] = None
    # 宽基 vs 主题拆分
    wide_kw = ("沪深300", "上证50", "中证500", "中证1000", "中证2000", "A500", "创业板", "科创50",
               "深证100", "上证180", "MSCI", "国证2000", "中证A")
    wide_p = [e["pct"] for e in holds if e.get("pct") is not None
              and any(k in e["name"] for k in wide_kw)]
    theme_p = [e["pct"] for e in holds if e.get("pct") is not None
               and not any(k in e["name"] for k in wide_kw)]
    hold["quote_ok"] = True
    hold["etf_avg_pct"] = _r(np.mean(pcts)) if pcts else None
    hold["wide_avg_pct"] = _r(np.mean(wide_p)) if wide_p else None
    hold["theme_avg_pct"] = _r(np.mean(theme_p)) if theme_p else None
    hold["etf_amount_sum"] = amts
    return hold


# ============ ⑥ 技术面（本地日线） ============
def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def compute_tech(df: pd.DataFrame, bars: int = 250) -> dict:
    """在日线上算技术特征。df 升序、含 date/open/high/low/close/volume/amount。

    所有指标都用「截至最后一根」的滚动窗口，不存在未来函数。
    """
    if df is None or len(df) < 20:
        return {"ok": False, "err": f"日线不足（{0 if df is None else len(df)} 根，至少 20 根）"}
    d = df.tail(max(60, bars)).reset_index(drop=True).copy()
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    n = len(d)
    out = {"ok": True, "bars": n, "last_date": str(d["date"].iloc[-1])[:10],
           "last_close": _r(c.iloc[-1])}

    # --- 均线 ---
    ma = {}
    for k in (5, 10, 20, 60, 120, 250):
        ma[f"ma{k}"] = _r(c.rolling(k).mean().iloc[-1]) if n >= k else None
    out["ma"] = ma
    vals = [ma[f"ma{k}"] for k in (5, 10, 20, 60) if ma[f"ma{k}"]]
    if len(vals) == 4:
        if ma["ma5"] > ma["ma10"] > ma["ma20"] > ma["ma60"]:
            out["ma_arrange"] = "多头排列"
        elif ma["ma5"] < ma["ma10"] < ma["ma20"] < ma["ma60"]:
            out["ma_arrange"] = "空头排列"
        else:
            out["ma_arrange"] = "纠缠"
    else:
        out["ma_arrange"] = "数据不足"

    # --- RSI(14) Wilder ---
    # ★边界：区间内一次都没跌时 dn=0 → rs=inf/NaN。真实行情里连板股就会这样，
    #   必须显式给 100（只涨不跌）或 50（完全没有波动），不能返回 None
    #   （否则前端显示「—」，把「极度强势」误读成「数据缺失」）。
    diff = c.diff()
    up = diff.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-diff.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.mask((dn <= 0) & (up > 0), 100.0)
    rsi = rsi.fillna(50.0)
    out["rsi14"] = _r(rsi.iloc[-1], 1)

    # --- MACD(12,26,9) ---
    dif = _ema(c, 12) - _ema(c, 26)
    dea = _ema(dif, 9)
    out["macd"] = {"dif": _r(dif.iloc[-1], 3), "dea": _r(dea.iloc[-1], 3),
                   "hist": _r((dif.iloc[-1] - dea.iloc[-1]) * 2, 3),
                   "cross": ("金叉" if dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2]
                             else "死叉" if dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2]
                             else "多头" if dif.iloc[-1] > dea.iloc[-1] else "空头")}

    # --- KDJ(9,3,3) ---
    low9 = l.rolling(9).min()
    high9 = h.rolling(9).max()
    rsv = (c - low9) / (high9 - low9).replace(0, np.nan) * 100
    # ★同样的边界：9 日内最高=最低（一字板/停牌）→ 分母 0。中性值 50，不能是 NaN
    rsv = rsv.fillna(50.0)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    dd = k.ewm(alpha=1 / 3, adjust=False).mean()
    out["kdj"] = {"k": _r(k.iloc[-1], 1), "d": _r(dd.iloc[-1], 1),
                  "j": _r(3 * k.iloc[-1] - 2 * dd.iloc[-1], 1)}

    # --- BOLL(20,2) ---
    m20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std(ddof=0)
    up_, dn_ = m20 + 2 * sd20, m20 - 2 * sd20
    out["boll"] = {"up": _r(up_.iloc[-1]), "mid": _r(m20.iloc[-1]), "low": _r(dn_.iloc[-1]),
                   "width_pct": _r((up_.iloc[-1] - dn_.iloc[-1]) / m20.iloc[-1] * 100, 1)
                   if m20.iloc[-1] else None}
    if out["boll"]["up"] and out["boll"]["low"]:
        rng = out["boll"]["up"] - out["boll"]["low"]
        out["boll"]["pos_pct"] = _r((c.iloc[-1] - out["boll"]["low"]) / rng * 100, 1) if rng else None

    # --- ATR(14) ---
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    out["atr14"] = _r(atr)
    out["atr_pct"] = _r(atr / c.iloc[-1] * 100, 2) if c.iloc[-1] else None

    # --- 量能 ---
    v5 = v.tail(5).mean()
    v20 = v.tail(20).mean()
    out["vol_ratio_5_20"] = _r(v5 / v20, 2) if v20 else None
    out["vol_last_ratio"] = _r(v.iloc[-1] / v20, 2) if v20 else None
    # 换手率分位（近 250 日）
    if "turnover" in d.columns and d["turnover"].notna().sum() > 30:
        t = d["turnover"].dropna()
        last_t = t.iloc[-1]
        out["turnover_pct_rank"] = _r((t < last_t).sum() / len(t) * 100, 1)
        out["turnover_last"] = _r(last_t * 100, 2)
    else:
        out["turnover_pct_rank"] = None
        out["turnover_last"] = None

    # --- 高低点 ---
    hl = {}
    for k in (20, 60, 120, 250):
        if n >= k:
            hl[f"h{k}"] = _r(h.tail(k).max())
            hl[f"l{k}"] = _r(l.tail(k).min())
    out["high_low"] = hl
    out["new_high_20"] = bool(n >= 21 and h.iloc[-1] >= h.tail(21).max())
    out["new_low_20"] = bool(n >= 21 and l.iloc[-1] <= l.tail(21).min())
    out["new_high_60"] = bool(n >= 61 and h.iloc[-1] >= h.tail(61).max())
    out["new_low_60"] = bool(n >= 61 and l.iloc[-1] <= l.tail(61).min())

    # --- 连阳连阴 ---
    chg = c.diff()
    streak, sign = 0, 0
    for i in range(len(chg) - 1, 0, -1):
        x = chg.iloc[i]
        if not np.isfinite(x) or x == 0:
            break
        s = 1 if x > 0 else -1
        if sign == 0:
            sign, streak = s, 1
        elif s == sign:
            streak += 1
        else:
            break
    out["streak"] = {"sign": "阳" if sign > 0 else ("阴" if sign < 0 else "—"), "n": streak}

    # --- 相对强弱（vs 沪深300，用本地 ant_index） ---
    out["rel"] = _rel_strength(d, c)

    # --- 近 60 日缺口（未回补） ---
    out["gaps"] = _unfilled_gaps(d)
    # --- 筹码峰（近 60 日成交量加权价格分布） ---
    out["cost_peak"] = _cost_peak(d)
    return out


def _rel_strength(d: pd.DataFrame, c: pd.Series) -> dict:
    """相对沪深300的超额（20/60 交易日）。取本地 ant_index 的 sh000300。"""
    try:
        conn = core_db.reader()
        idx = pd.read_sql_query(
            "SELECT date, close FROM ant_index WHERE symbol='sh000300' "
            "ORDER BY date DESC LIMIT 300", conn)
    except Exception:  # noqa: BLE001
        return {"ok": False, "note": "本地无沪深300指数数据"}
    if idx is None or idx.empty:
        return {"ok": False, "note": "本地无沪深300指数数据"}
    idx = idx.sort_values("date").reset_index(drop=True)
    idx["date"] = pd.to_datetime(idx["date"])
    ic = idx["close"].astype(float)
    out = {"ok": True}
    for k in (20, 60):
        if len(c) > k and len(ic) > k:
            r_stock = (c.iloc[-1] / c.iloc[-1 - k] - 1) * 100
            r_idx = (ic.iloc[-1] / ic.iloc[-1 - k] - 1) * 100
            out[f"excess{k}"] = _r(r_stock - r_idx, 2)
            out[f"stock{k}"] = _r(r_stock, 2)
            out[f"index{k}"] = _r(r_idx, 2)
    return out


def _unfilled_gaps(d: pd.DataFrame, lookback: int = 60) -> list:
    """近 lookback 日内的跳空缺口，且截至最后一根未回补。"""
    if len(d) < 5:
        return []
    sub = d.tail(lookback).reset_index(drop=True)
    out = []
    last_close = sub["close"].iloc[-1]
    for i in range(1, len(sub)):
        prev_h, prev_l = sub["high"].iloc[i - 1], sub["low"].iloc[i - 1]
        cur_l, cur_h = sub["low"].iloc[i], sub["high"].iloc[i]
        if cur_l > prev_h:      # 向上跳空
            lo, hi = prev_h, cur_l
            filled = (sub["low"].iloc[i + 1:] <= lo).any() if i + 1 < len(sub) else False
            if not filled:
                out.append({"kind": "向上缺口", "lo": _r(lo), "hi": _r(hi),
                            "date": str(sub["date"].iloc[i])[:10],
                            "dist_pct": _r(_pct(last_close, (lo + hi) / 2), 2)})
        elif cur_h < prev_l:    # 向下跳空
            lo, hi = cur_h, prev_l
            filled = (sub["high"].iloc[i + 1:] >= hi).any() if i + 1 < len(sub) else False
            if not filled:
                out.append({"kind": "向下缺口", "lo": _r(lo), "hi": _r(hi),
                            "date": str(sub["date"].iloc[i])[:10],
                            "dist_pct": _r(_pct(last_close, (lo + hi) / 2), 2)})
    out.sort(key=lambda z: abs(z.get("dist_pct") or 999))
    return out[:4]


def _cost_peak(d: pd.DataFrame, lookback: int = 60, buckets: int = 24) -> dict | None:
    """筹码峰：近 lookback 日按价格分桶做成交量加权，取权重最大的桶中心。"""
    sub = d.tail(lookback)
    if len(sub) < 10:
        return None
    lo, hi = float(sub["low"].min()), float(sub["high"].max())
    if not (hi > lo):
        return None
    step = (hi - lo) / buckets
    if step <= 0:
        return None
    w = np.zeros(buckets)
    for _, r in sub.iterrows():
        p = (r["high"] + r["low"] + r["close"]) / 3.0
        i = int((p - lo) / step)
        i = max(0, min(buckets - 1, i))
        w[i] += float(r["volume"] or 0)
    if w.sum() <= 0:
        return None
    i = int(np.argmax(w))
    center = lo + (i + 0.5) * step
    # 峰区宽度：权重 ≥ 峰值 60% 的连续区间
    thr = w[i] * 0.6
    lo_i, hi_i = i, i
    while lo_i > 0 and w[lo_i - 1] >= thr:
        lo_i -= 1
    while hi_i < buckets - 1 and w[hi_i + 1] >= thr:
        hi_i += 1
    return {"price": _r(center), "zone_lo": _r(lo + lo_i * step),
            "zone_hi": _r(lo + (hi_i + 1) * step),
            "weight_share": _r(w[i] / w.sum() * 100, 1),
            "lookback": lookback}


# ============ ⑦ 关键节点距离 ============
def _round_step(p: float) -> float:
    """按价格量级选整数关口步长（A 股习惯：低价股看 0.5/1 元，高价股看 10/50 元）。"""
    if p < 5:
        return 0.5
    if p < 20:
        return 1.0
    if p < 100:
        return 5.0
    if p < 500:
        return 10.0
    return 50.0


def build_nodes(price: float, tech: dict, quote: dict | None) -> list:
    """把技术位转成「节点列表」：每个节点给 价格 / 距离% / 上下方 / 状态。

    状态定义（tolerance 由调用方注入到 nodes_meta）：
      贴近 : |dist| <= tol       —— 正在测试这个位置
      站上 : dist > tol          —— 节点在现价下方（支撑）
      压制 : dist < -tol         —— 节点在现价上方（阻力）
    """
    if not price or not tech.get("ok"):
        return []
    nodes = []

    def add(name, level, kind, note=""):
        if level is None:
            return
        lv = float(level)
        if lv <= 0:
            return
        nodes.append({"name": name, "price": _r(lv), "kind": kind,
                      "dist_pct": _r(_pct(price, lv), 2), "note": note})

    ma = tech.get("ma") or {}
    for k in (5, 10, 20, 60, 120, 250):
        if ma.get(f"ma{k}"):
            add(f"MA{k}", ma[f"ma{k}"], "均线", "简单移动平均，常用作动态支撑/压力")
    boll = tech.get("boll") or {}
    if boll.get("up"):
        add("BOLL 上轨", boll["up"], "轨道", "20 日均线 + 2σ，超买边界")
    if boll.get("mid"):
        add("BOLL 中轨", boll["mid"], "轨道", "20 日均线，多空分水岭")
    if boll.get("low"):
        add("BOLL 下轨", boll["low"], "轨道", "20 日均线 - 2σ，超卖边界")

    hl = tech.get("high_low") or {}
    for k, label in ((20, "20 日高"), (60, "60 日高"), (120, "120 日高")):
        if hl.get(f"h{k}"):
            add(label, hl[f"h{k}"], "前高", "近期最高价，突破需要放量")
    for k, label in ((20, "20 日低"), (60, "60 日低")):
        if hl.get(f"l{k}"):
            add(label, hl[f"l{k}"], "前低", "近期最低价，跌破意味着趋势转弱")

    cp = tech.get("cost_peak")
    if cp and cp.get("price"):
        add("筹码峰", cp["price"], "筹码", f"近 {cp.get('lookback')} 日成交量最密集价位")
        if cp.get("zone_lo"):
            add("筹码峰下沿", cp["zone_lo"], "筹码", "密集区下边界")
        if cp.get("zone_hi"):
            add("筹码峰上沿", cp["zone_hi"], "筹码", "密集区上边界")

    for g in (tech.get("gaps") or [])[:2]:
        if g.get("lo"):
            add(f"{g['kind']}下沿", g["lo"], "缺口", f"{g.get('date')} 跳空，未回补")
        if g.get("hi"):
            add(f"{g['kind']}上沿", g["hi"], "缺口", f"{g.get('date')} 跳空，未回补")

    # 整数关口：现价上下各取 2 个
    step = _round_step(price)
    base = math.floor(price / step) * step
    for i in (-2, -1, 1, 2):
        lv = base + i * step
        if lv > 0:
            add(f"整数关 {lv:g}", lv, "整数关", f"{step:g} 元一档的心理价位")

    if quote:
        if quote.get("limit_up"):
            add("涨停价", quote["limit_up"], "涨跌停", "当日涨停价")
        if quote.get("limit_down"):
            add("跌停价", quote["limit_down"], "涨跌停", "当日跌停价")
    # 去重（同一价位可能被多个来源命中）
    seen, uniq = set(), []
    for x in nodes:
        key = round(float(x["price"]), 2)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(x)
    uniq.sort(key=lambda z: abs(z["dist_pct"]) if z.get("dist_pct") is not None else 999)
    return uniq


def tag_nodes(nodes: list, tol: float) -> list:
    """给节点打上「贴近/站上/压制」状态，并附一句人话描述。"""
    tol = float(tol or 1.0)
    for x in nodes:
        d = x.get("dist_pct")
        if d is None:
            x["state"] = "—"
            x["desc"] = ""
            continue
        if abs(d) <= tol:
            x["state"] = "贴近"
            x["desc"] = f"现价距此仅 {abs(d):.2f}%，正在测试"
        elif d > 0:
            x["state"] = "站上"
            x["desc"] = f"现价在其上方 {d:.2f}%，回踩时可能是支撑"
        else:
            x["state"] = "压制"
            x["desc"] = f"现价在其下方 {abs(d):.2f}%，上攻时可能是阻力"
    return nodes


# ============ ⑧ 信号 ============
def build_signals(tech: dict, quote: dict | None, flow: dict, etf: dict,
                  seats: dict) -> list:
    """规则化信号。每条给 {name, dir, level, desc, evidence}。

    level 的定义（写死，避免主观）：
      强 = 该信号在日线上是「当日新发生」（金叉/死叉/突破/跌破当天成立）
      中 = 状态性信号（排列、位置、偏离），持续多日
      弱 = 边缘提示（接近但未触发）
    dir ∈ 多 / 空 / 中性
    """
    out = []
    if not tech or not tech.get("ok"):
        return out
    c = tech.get("last_close")
    ma = tech.get("ma") or {}
    macd = tech.get("macd") or {}
    kdj = tech.get("kdj") or {}
    boll = tech.get("boll") or {}
    hl = tech.get("high_low") or {}

    def add(name, d, level, desc, ev=""):
        out.append({"name": name, "dir": d, "level": level, "desc": desc, "evidence": ev})

    # 均线排列
    arr = tech.get("ma_arrange")
    if arr == "多头排列":
        add("均线多头排列", "多", "中", "MA5>MA10>MA20>MA60，趋势结构完好",
            f"MA5={ma.get('ma5')} MA10={ma.get('ma10')} MA20={ma.get('ma20')} MA60={ma.get('ma60')}")
    elif arr == "空头排列":
        add("均线空头排列", "空", "中", "MA5<MA10<MA20<MA60，反弹容易遇均线压制",
            f"MA5={ma.get('ma5')} MA10={ma.get('ma10')} MA20={ma.get('ma20')} MA60={ma.get('ma60')}")

    # 价格相对 MA20
    if c and ma.get("ma20"):
        dd = _pct(c, ma["ma20"])
        if dd is not None:
            if dd > 0:
                add("站上 MA20", "多", "中", f"现价高于 20 日线 {dd:.2f}%", f"MA20={ma['ma20']}")
            else:
                add("跌破 MA20", "空", "中", f"现价低于 20 日线 {abs(dd):.2f}%", f"MA20={ma['ma20']}")

    # MACD
    if macd.get("cross") == "金叉":
        add("MACD 金叉", "多", "强", "DIF 上穿 DEA，当日发生", f"DIF={macd.get('dif')} DEA={macd.get('dea')}")
    elif macd.get("cross") == "死叉":
        add("MACD 死叉", "空", "强", "DIF 下穿 DEA，当日发生", f"DIF={macd.get('dif')} DEA={macd.get('dea')}")

    # RSI
    rsi = tech.get("rsi14")
    if rsi is not None:
        if rsi >= 80:
            add("RSI 严重超买", "空", "中", f"RSI14={rsi}，短线过热", "RSI>=80 属于极端区间")
        elif rsi >= 70:
            add("RSI 超买", "空", "弱", f"RSI14={rsi}，注意回踩", "RSI>=70")
        elif rsi <= 20:
            add("RSI 严重超卖", "多", "中", f"RSI14={rsi}，杀跌动能可能衰竭", "RSI<=20 属于极端区间")
        elif rsi <= 30:
            add("RSI 超卖", "多", "弱", f"RSI14={rsi}", "RSI<=30")

    # KDJ
    k, d_ = kdj.get("k"), kdj.get("d")
    if k is not None and d_ is not None:
        if k > 80 and d_ > 80:
            add("KDJ 高位", "空", "弱", f"K={k} D={d_}，高位钝化需配合量能看", "K、D 同时 >80")
        elif k < 20 and d_ < 20:
            add("KDJ 低位", "多", "弱", f"K={k} D={d_}", "K、D 同时 <20")

    # BOLL
    pos = boll.get("pos_pct")
    if pos is not None:
        if pos > 100:
            add("突破 BOLL 上轨", "多", "强", "收盘站到 20 日 +2σ 之外，强势但易回抽",
                f"上轨={boll.get('up')}")
        elif pos < 0:
            add("跌破 BOLL 下轨", "空", "强", "收盘跌到 20 日 -2σ 之外", f"下轨={boll.get('low')}")
        elif pos > 85:
            add("贴上轨运行", "多", "中", f"位于布林带 {pos}% 分位", "带宽上沿")
        elif pos < 15:
            add("贴下轨运行", "空", "中", f"位于布林带 {pos}% 分位", "带宽下沿")
    if boll.get("width_pct") is not None and boll["width_pct"] < 6:
        add("布林带收口", "中性", "中", f"带宽仅 {boll['width_pct']}%，波动压缩，方向选择临近",
            "带宽 <6% 属于收敛")

    # 量能
    vlr = tech.get("vol_last_ratio")
    if vlr is not None:
        if vlr >= 2.0:
            add("放量", "中性", "强", f"当日量为 20 日均量的 {vlr} 倍，多空分歧加大",
                "量比 >=2 视为显著放量")
        elif vlr <= 0.5:
            add("缩量", "中性", "弱", f"当日量仅为 20 日均量的 {vlr} 倍", "量能萎缩")
    vr = tech.get("vol_ratio_5_20")
    if vr is not None and vr >= 1.5:
        add("5 日量能抬升", "多", "中", f"5 日均量为 20 日均量的 {vr} 倍", "资金关注度上升")

    # 新高新低
    if tech.get("new_high_60"):
        add("创 60 日新高", "多", "强", "突破近 60 个交易日最高价，趋势加速", "")
    elif tech.get("new_high_20"):
        add("创 20 日新高", "多", "中", "突破近 20 个交易日最高价", "")
    if tech.get("new_low_60"):
        add("创 60 日新低", "空", "强", "跌破近 60 个交易日最低价，趋势转弱", "")
    elif tech.get("new_low_20"):
        add("创 20 日新低", "空", "中", "跌破近 20 个交易日最低价", "")

    # 连阳连阴
    st = tech.get("streak") or {}
    if st.get("n", 0) >= 4:
        if st.get("sign") == "阳":
            add(f"连续 {st['n']} 连阳", "多", "中", "短期买盘持续占优，注意追高风险", "")
        elif st.get("sign") == "阴":
            add(f"连续 {st['n']} 连阴", "空", "中", "短期卖压持续，等待止跌信号", "")

    # 缺口
    gaps = tech.get("gaps") or []
    if gaps:
        g = gaps[0]
        add(f"{g['kind']}未回补", "多" if "向上" in g["kind"] else "空", "中",
            f"{g['date']} 跳空 {g['lo']}~{g['hi']}，至今未回补", "缺口常被当作支撑/压力")

    # 相对强弱
    rel = tech.get("rel") or {}
    if rel.get("excess20") is not None:
        e = rel["excess20"]
        if e > 10:
            add("显著跑赢大盘", "多", "中", f"20 日超额 {e}%（个股 {rel.get('stock20')}% vs 沪深300 {rel.get('index20')}%）", "")
        elif e < -10:
            add("显著跑输大盘", "空", "中", f"20 日超额 {e}%（个股 {rel.get('stock20')}% vs 沪深300 {rel.get('index20')}%）", "")

    # 资金流（来自五档逐笔）
    if flow.get("ok"):
        rows = flow.get("rows") or []
        s5 = _flow_summary(rows, 5)
        s20 = _flow_summary(rows, 20)
        if s5.get("main") is not None and s5["days"] >= 3:
            v = s5["main"] / 1e8
            if v >= 1.0:
                add("5 日主力净流入", "多", "中", f"近 5 日主力（超大+大单）净流入 {v:.2f} 亿",
                    f"20 日累计 {(s20['main'] or 0) / 1e8:.2f} 亿")
            elif v <= -1.0:
                add("5 日主力净流出", "空", "中", f"近 5 日主力净流出 {abs(v):.2f} 亿",
                    f"20 日累计 {(s20['main'] or 0) / 1e8:.2f} 亿")
        # 量价配合：涨且主力流出 = 拉高出货嫌疑
        up = (quote or {}).get("pct")
        if up is not None and s5.get("main") is not None and s5["days"] >= 3:
            if up > 2 and s5["main"] < -0.5e8:
                add("上涨但主力流出", "空", "中",
                    f"当日涨 {up:.2f}% 而主力净流出 {abs(s5['main']) / 1e8:.2f} 亿，拉高出货嫌疑", "")
            if up < -2 and s5["main"] > 0.5e8:
                add("下跌但主力流入", "多", "中",
                    f"当日跌 {up:.2f}% 而主力净流入 {s5['main'] / 1e8:.2f} 亿，逆势承接", "")

    # 席位（龙虎榜）
    if seats.get("ok") and seats.get("latest"):
        lt = seats["latest"]
        mix = lt.get("mix") or []
        net_sum = sum(m["net"] for m in mix)
        inst = next((m for m in mix if m["cat"] == "机构"), None)
        if inst and abs(inst["net"]) > 3e7:
            add("机构席位净" + ("买入" if inst["net"] > 0 else "卖出"),
                "多" if inst["net"] > 0 else "空", "强",
                f"{lt['date']} 龙虎榜机构专用席位净{'买' if inst['net'] > 0 else '卖'} "
                f"{abs(inst['net']) / 1e8:.2f} 亿",
                f"买 {inst['buy'] / 1e8:.2f} 亿 / 卖 {inst['sell'] / 1e8:.2f} 亿")
        hm = next((m for m in mix if m["cat"] == "游资"), None)
        if hm and abs(hm["net"]) > 3e7:
            add("游资席位活跃", "多" if hm["net"] > 0 else "空", "中",
                f"{lt['date']} 游资类席位净{'买' if hm['net'] > 0 else '卖'} {abs(hm['net']) / 1e8:.2f} 亿", "")
        if abs(net_sum) > 5e7:
            add("龙虎榜合计净" + ("买入" if net_sum > 0 else "卖出"),
                "多" if net_sum > 0 else "空", "强",
                f"{lt['date']} 榜单席位合计净{'买' if net_sum > 0 else '卖'} {abs(net_sum) / 1e8:.2f} 亿",
                "汇总买卖两侧全部席位")

    # ETF 被动资金
    if etf.get("ok"):
        n = etf.get("n_etf") or 0
        cap = etf.get("total_cap") or 0
        if n >= 5 and cap > 5e8:
            add("ETF 被动持仓厚", "中性", "中",
                f"{n} 只 ETF 持有，合计 {cap / 1e8:.1f} 亿（{etf.get('report_date')} 季报）",
                "被动资金提供一定承接力，也意味着指数调仓时会被动卖出")
        up = etf.get("delta_up") or []
        if up and (up[0].get("delta_cap") or 0) > 1e8:
            add("ETF 环比增持", "多", "中",
                f"{up[0]['name']} 环比增持 {up[0]['delta_cap'] / 1e8:.2f} 亿（{etf.get('prev_report_date')} → {etf.get('report_date')}）", "")

    # 排序：强→中→弱，同级别多空分开
    order = {"强": 0, "中": 1, "弱": 2}
    out.sort(key=lambda x: (order.get(x["level"], 3), x["dir"] != "空"))
    return out


# ============ 主流程 ============
def _merge_cfg(cfg: dict | None) -> dict:
    """前端 cfg 收敛：类型/范围兜底，非法值回退默认。"""
    c = dict(DEFAULTS)
    if not isinstance(cfg, dict):
        return c
    for k, v in cfg.items():
        if k not in DEFAULTS:
            continue
        dflt = DEFAULTS[k]
        try:
            if k == "intraday_days":
                iv = int(v)
                c[k] = 1 if iv not in (1, 3, 5) else iv
            elif isinstance(dflt, int):
                c[k] = max(1, int(v))
            elif isinstance(dflt, float):
                c[k] = float(v)
            else:
                c[k] = v
        except (TypeError, ValueError):
            c[k] = dflt
    # 边界收敛
    c["seat_topn"] = max(3, min(10, c["seat_topn"]))
    c["etf_topn"] = max(3, min(20, c["etf_topn"]))
    c["node_show"] = max(5, min(40, c["node_show"]))
    c["node_tolerance"] = max(0.1, min(10.0, c["node_tolerance"]))
    c["tech_bars"] = max(120, min(400, c["tech_bars"]))
    c["max_codes"] = max(1, min(60, c["max_codes"]))
    return c


def _load_daily_local(codes: list[str], bars: int) -> dict:
    """只取这几只股票的近 bars 个交易日日线（dict: code → DataFrame）。

    为什么不复用 batchload.load_daily_map：那个函数是「一次取全市场再切片」的设计，
    为全市场筛选优化；盯盘只查几只票时会把 169 万行全读进内存（实测多花 6~8 秒）。
    这里走 idx_daily_code 索引直查，3 只票 6ms。
    """
    if not codes:
        return {}
    conn = core_db.reader()
    cut_rows = conn.execute(
        "SELECT DISTINCT date FROM daily ORDER BY date DESC LIMIT ?",
        (max(1, int(bars)),)).fetchall()
    if not cut_rows:
        return {}
    cut = min(str(r[0]) for r in cut_rows)
    ph = ",".join("?" * len(codes))
    df = pd.read_sql_query(
        f"SELECT code,date,open,high,low,close,volume,amount,turnover "
        f"FROM daily WHERE code IN ({ph}) AND date>=? ORDER BY code,date",
        conn, params=list(codes) + [cut])
    if df.empty:
        return {}
    df["code"] = df["code"].astype(str)
    df["date"] = pd.to_datetime(df["date"])
    return {str(c): g.drop(columns="code").reset_index(drop=True)
            for c, g in df.groupby("code", sort=False)}


def _one_stock(code: str, cfg: dict, q: dict | None) -> dict:
    """单只股票的全部维度（网络部分）。异常不抛出，降级为 notes。"""
    notes: list = []
    row = {"code": code, "name": (q or {}).get("name") or code, "quote": q or {},
           "notes": notes}
    _ensure_db()
    # 元信息（行业/交易所）
    try:
        conn = core_db.reader()
        m = conn.execute("SELECT name, sector, exchange FROM meta WHERE code=?",
                         (code,)).fetchone()
        if m:
            row["name"] = row["name"] if row["name"] != code else (m[0] or code)
            row["sector"] = m[1]
            row["exchange"] = m[2]
    except Exception:  # noqa: BLE001
        pass

    # 本地日线 → 技术面
    tech = {}
    try:
        dm = _load_daily_local([code], cfg["tech_bars"])
        df = dm.get(code)
        if df is None or len(df) < 20:
            notes.append("本地日线不足 20 根，技术面不可用（可能未同步或新股）")
        else:
            tech = compute_tech(df, bars=cfg["tech_bars"])
    except Exception as e:  # noqa: BLE001
        notes.append(f"技术面计算失败: {str(e)[:80]}")
    row["tech"] = tech

    # 资金流
    flow = fetch_fund_flow(code, days=60)
    if not flow.get("ok"):
        notes.append(flow.get("err") or "资金流不可用")
    row["flow"] = flow

    # 龙虎榜
    seats = fetch_lhb(code, lookback_days=cfg["seat_lookback"])
    if not seats.get("ok"):
        notes.append(seats.get("err") or "龙虎榜不可用")
    elif not seats.get("records"):
        notes.append(f"近 {cfg['seat_lookback']} 天未上龙虎榜（无异动记录）")
    row["seats"] = seats
    row["seat_change"] = _seat_changes(seats.get("latest"), seats.get("prev"))

    # ETF
    etf = fetch_etf_holdings(code, topn=cfg["etf_topn"])
    if not etf.get("ok"):
        notes.append(etf.get("err") or "ETF 持仓不可用")
    else:
        etf = attach_etf_quotes(etf)
    row["etf"] = etf

    # 节点 + 信号
    price = (q or {}).get("price") or tech.get("last_close")
    nodes = build_nodes(price, tech, q)
    row["nodes"] = tag_nodes(nodes, cfg["node_tolerance"])
    row["nodes_meta"] = {"tolerance": cfg["node_tolerance"], "total": len(nodes),
                         "shown": min(len(nodes), cfg["node_show"])}
    row["signals"] = build_signals(tech, q, flow, etf, seats)

    # 资金画像（双口径）
    row["fund_mix"] = _build_fund_mix(flow, seats, etf, q)
    return row


def _build_fund_mix(flow: dict, seats: dict, etf: dict, q: dict | None) -> dict:
    """双口径资金画像。

    口径 A（逐笔分档，每日可得）：超大单 / 大单 / 中单 / 小单 当日净流入 + 近 5 日。
    口径 B（龙虎榜席位，仅上榜日）：机构 / 游资 / 散户 / 国家队 / 北向 / 其他 的买卖净额。
    两者不合成、不互相校准 —— 因为「大单」和「机构」不是同一个概念。
    """
    out = {"tick": None, "seat": None, "notes": []}
    if flow.get("ok") and flow.get("rows"):
        last = flow["rows"][-1]
        s5 = _flow_summary(flow["rows"], 5)
        amt = (q or {}).get("amount")
        tot = sum(abs(last.get(k) or 0) for k in ("xlarge", "large", "mid", "small")) or 0
        out["tick"] = {
            "date": last.get("date"),
            "today": {k: last.get(k) for k in ("xlarge", "large", "mid", "small", "main")},
            "today_pct": {k: (last.get(k) or 0) / tot * 100 if tot else None
                          for k in ("xlarge", "large", "mid", "small")},
            "main_pct_of_amount": (last.get("main") or 0) / amt * 100 if amt else last.get("main_pct"),
            "d5": s5,
            "series": [{"date": r["date"], "main": r["main"], "close": r["close"],
                        "pct": r["pct"]} for r in flow["rows"][-20:]],
            "note": "按单笔成交金额分档（东财口径）：超大单≥100万/50万股，大单20-100万，"
                    "中单4-20万，小单<4万。只能说明「大钱还是小钱在买」，不能说明身份。",
        }
    else:
        out["notes"].append("逐笔资金分档不可用（当日无成交或接口失败）")

    if seats.get("ok") and seats.get("latest"):
        lt = seats["latest"]
        mix = lt.get("mix") or []
        tot = sum(abs(m["net"]) for m in mix) or 0
        out["seat"] = {
            "date": lt.get("date"),
            "rows": [{"cat": m["cat"], "net": m["net"], "buy": m["buy"], "sell": m["sell"],
                      "n": m["n"],
                      "share": (abs(m["net"]) / tot * 100) if tot else None} for m in mix],
            "note": "席位身份为关键词规则推断（机构专用/沪深股通/证金汇金/拉萨系/游资名单/其他），"
                    "交易所只披露营业部名称，不披露真实身份。",
            "coverage": "仅覆盖上榜日，普通交易日无数据",
        }
    else:
        out["notes"].append("无龙虎榜席位数据（该股近期未触发上榜条件）")

    if etf.get("ok"):
        out["etf"] = {
            "n": etf.get("n_etf"), "cap": etf.get("total_cap"),
            "wide_cap": etf.get("wide_cap"), "report_date": etf.get("report_date"),
            "avg_pct": etf.get("etf_avg_pct"), "wide_avg_pct": etf.get("wide_avg_pct"),
            "theme_avg_pct": etf.get("theme_avg_pct"),
            "note": "ETF 为季报口径的被动持仓，不是实时申赎",
        }
    return out


def run(codes, cfg: dict | None = None, progress_cb=None, max_workers: int = 6) -> dict:
    """盯盘主入口。

    参数
    ----
    codes : str | list[str]   股票代码（支持逗号/空格/换行分隔的字符串）
    cfg   : dict              前端参数，见 DEFAULTS
    progress_cb : (a, b, msg) 进度回调（项目统一三元组签名）

    返回 {ok, asof, phase, rows:[...], summary:{...}, params, notes}
    """
    c = _merge_cfg(cfg)
    _ensure_db()

    def prog(a, b, m):
        if progress_cb:
            try:
                progress_cb(a, b, m)
            except Exception:  # noqa: BLE001 —— 进度回调失败不应中断任务
                pass

    # 解析代码
    if isinstance(codes, str):
        raw = re.split(r"[,，\s;；\|]+", codes)
    else:
        raw = list(codes or [])
    parsed, bad = [], []
    for x in raw:
        s = re.sub(r"^(sh|sz|bj)", "", str(x).strip().lower())
        if re.fullmatch(r"\d{6}", s):
            if s not in parsed:
                parsed.append(s)
        elif str(x).strip():
            bad.append(str(x).strip())
    if not parsed:
        return {"ok": False, "err": "没有识别到有效股票代码（需 6 位数字，如 600519）",
                "rows": [], "summary": {}, "notes": []}
    if len(parsed) > c["max_codes"]:
        bad.append(f"超出单次上限 {c['max_codes']} 只，已截断")
        parsed = parsed[:c["max_codes"]]

    phase = _market_phase()
    prog(0.02, 1, f"读取实时行情（{len(parsed)} 只）…")
    quotes = _fetch_quotes(parsed)
    if not quotes:
        prog(0.06, 1, "实时行情不可用，降级为本地收盘数据")

    rows = []
    n = len(parsed)
    for i, code in enumerate(parsed):
        prog(0.08 + 0.86 * i / max(1, n), 1,
             f"{i + 1}/{n} · {code} 分时/席位/资金/ETF/技术面")
        try:
            rows.append(_one_stock(code, c, quotes.get(code)))
        except Exception as e:  # noqa: BLE001 —— 单只失败不拖垮整组
            rows.append({"code": code, "name": code, "quote": quotes.get(code) or {},
                         "error": f"{type(e).__name__}: {str(e)[:120]}",
                         "notes": ["该股处理失败，其余股票不受影响"],
                         "tech": {}, "flow": {}, "seats": {}, "etf": {}, "nodes": [],
                         "signals": [], "fund_mix": {}})
    prog(0.98, 1, "汇总…")
    summary = _summarize(rows)
    asof = ""
    for r in rows:
        t = (r.get("quote") or {}).get("quote_time") or ""
        if t:
            asof = max(asof, t)
    return {
        "ok": True,
        "asof": asof,
        "phase": phase,
        "rows": rows,
        "summary": summary,
        "params": c,
        "bad_codes": bad,
        "notes": [
            "所有行情为公开接口实时拉取，非交易所授权数据，可能存在延迟或缺失。",
            "席位分类、资金分档、ETF 持仓的口径边界见页面「口径说明」。",
            "本页只做数据聚合与规则化特征描述，不构成投资建议。",
        ],
    }


def _summarize(rows: list) -> dict:
    """组级汇总：涨跌、资金、信号分布、异动名单。"""
    ok_rows = [r for r in rows if not r.get("error")]
    pcts = [(r.get("quote") or {}).get("pct") for r in ok_rows]
    pcts = [x for x in pcts if x is not None]
    flows = []
    for r in ok_rows:
        fm = r.get("fund_mix") or {}
        tk = fm.get("tick") or {}
        v = (tk.get("d5") or {}).get("main")
        if v is not None:
            flows.append({"code": r["code"], "name": r.get("name"), "main5": v})
    flows.sort(key=lambda z: -(z["main5"] or 0))
    sig_cnt = {"多": 0, "空": 0, "中性": 0}
    strong = []
    for r in ok_rows:
        for s in (r.get("signals") or []):
            sig_cnt[s["dir"]] = sig_cnt.get(s["dir"], 0) + 1
            if s["level"] == "强":
                strong.append({"code": r["code"], "name": r.get("name"),
                               "signal": s["name"], "dir": s["dir"]})
    # 谁最强/最弱
    ranked = sorted([r for r in ok_rows if (r.get("quote") or {}).get("pct") is not None],
                    key=lambda r: -r["quote"]["pct"])
    return {
        "n": len(rows),
        "n_ok": len(ok_rows),
        "avg_pct": _r(np.mean(pcts)) if pcts else None,
        "up": sum(1 for x in pcts if x > 0),
        "down": sum(1 for x in pcts if x < 0),
        "flat": sum(1 for x in pcts if x == 0),
        "top_gain": ({"code": ranked[0]["code"], "name": ranked[0].get("name"),
                      "pct": ranked[0]["quote"]["pct"]} if ranked else None),
        "top_loss": ({"code": ranked[-1]["code"], "name": ranked[-1].get("name"),
                      "pct": ranked[-1]["quote"]["pct"]} if ranked else None),
        "flow_in": flows[:3],
        "flow_out": [{"code": x["code"], "name": x["name"], "main5": x["main5"]}
                     for x in flows[-3:]][::-1] if flows else [],
        "signal_count": sig_cnt,
        "strong_signals": strong[:12],
    }


# ============ 盯盘组持久化 ============
def _load_wl() -> dict:
    try:
        with open(_WATCHLIST_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("groups"), list):
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"groups": []}


def _save_wl(d: dict) -> None:
    """原子写：先写 .tmp 再 replace，避免中途崩溃留下半个 JSON。"""
    os.makedirs(os.path.dirname(_WATCHLIST_PATH), exist_ok=True)
    tmp = _WATCHLIST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _WATCHLIST_PATH)


def watchlist_list() -> dict:
    with _WL_LOCK:
        return _load_wl()


def watchlist_save(name: str, codes) -> dict:
    """保存/覆盖同名盯盘组。codes 支持字符串或列表。"""
    name = str(name or "").strip()[:24]
    if not name:
        return {"ok": False, "err": "组名不能为空"}
    if isinstance(codes, str):
        raw = re.split(r"[,，\s;；\|]+", codes)
    else:
        raw = list(codes or [])
    clean = []
    for x in raw:
        s = re.sub(r"^(sh|sz|bj)", "", str(x).strip().lower())
        if re.fullmatch(r"\d{6}", s) and s not in clean:
            clean.append(s)
    if not clean:
        return {"ok": False, "err": "没有有效代码"}
    if len(clean) > DEFAULTS["max_codes"]:
        clean = clean[:DEFAULTS["max_codes"]]
    with _WL_LOCK:
        d = _load_wl()
        found = False
        for g in d["groups"]:
            if g.get("name") == name:
                g["codes"] = clean
                g["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                found = True
                break
        if not found:
            d["groups"].append({"name": name, "codes": clean,
                                "created": datetime.now().strftime("%Y-%m-%d %H:%M")})
        d["groups"] = d["groups"][:40]   # 最多 40 组
        _save_wl(d)
        return {"ok": True, "groups": d["groups"]}


def watchlist_delete(name: str) -> dict:
    with _WL_LOCK:
        d = _load_wl()
        before = len(d["groups"])
        d["groups"] = [g for g in d["groups"] if g.get("name") != name]
        if len(d["groups"]) == before:
            return {"ok": False, "err": f"未找到分组「{name}」"}
        _save_wl(d)
        return {"ok": True, "groups": d["groups"]}


# ============ 元信息 ============
def meta_info() -> dict:
    return {
        "defaults": DEFAULTS,
        "phases": ["盘前", "集合竞价", "盘中", "午间休市", "已收盘", "周末休市"],
        "seat_rules": [{"cat": c, "keywords": k, "why": w} for c, k, w in _SEAT_RULES],
        "hot_money_note": "知名游资营业部名单为市场长期流传口径，非交易所披露，可能已换人",
        "hot_money_count": len(_HOT_MONEY_DEPTS),
        "report_dates": _latest_reports(),
        "disclaimer": ("本模块只做数据聚合与规则化特征提取，不构成投资建议。"
                       "所有「信号」都是对历史与当下价量关系的机械描述，不预示未来收益。"),
        "caveats": [
            "席位分类是名称关键词规则，非官方身份披露；「机构专用」可信度最高，游资名单仅供参考。",
            "逐笔五档只能说明单笔金额大小，不能说明交易者身份（同一笔大单可能是公募也可能是游资）。",
            "龙虎榜只在异动日有数据，普通交易日无席位信息；最近一次上榜可能是数周甚至数年前。",
            "ETF 持仓为基金季报口径，滞后 1~3 个月，且只披露前十大重仓。",
            "北向资金自 2024-08 起无盘中逐笔披露，本模块北向仅来自龙虎榜专用席位。",
            "技术面基于本地近 300 个交易日，更长周期请看「大周期」页签。",
        ],
    }


# ============ 对外别名（路由层调用；下划线开头的不宜跨模块引用） ============
fetch_quotes = _fetch_quotes
market_phase = _market_phase
