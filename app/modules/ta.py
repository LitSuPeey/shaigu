# -*- coding: utf-8 -*-
"""技术分析 · 多维度技术面评分（0-100）+ 技术信号事件回测。

本模块回答两个问题
==================
  ① 这只股票现在的技术面「健康度」多少分（0-100，6 个维度加权）
  ② 它现在命中的技术信号，历史上出现之后 5 个交易日表现如何
     （a）个股自身的长历史回测
     （b）最近一个月全市场「出现至少 2 个相同信号」的股票，信号后 5 日表现

★★ 必须先说的三件事（不是免责声明，是口径边界）★★
---------------------------------------------------
1. **分数是「形态健康度」，不是「预期收益率」。**
   本模块不承诺「80 分一定涨」。为此专门内置了「评分分档检验」：
   把该股历史上每一个交易日的分数都算出来，按分档统计未来 5 日收益。
   如果分档之间没有单调关系，前端会如实显示「本股历史上评分无区分度」。
   （已有前车之鉴：beton 的主榜分数对未来收益 IC≈0，就是因为没做这件事。）

2. **信号回测是「事件研究」，不是「策略回测」。**
   它不模拟仓位、手续费、滑点、涨停买不进、跌停卖不出。
   收益口径固定为「信号日次日开盘买入 → T+5 收盘卖出」，纯价格、不含分红。
   → 它衡量的是「信号出现后市场怎么走」，不是「照着做能赚多少」。

3. **信号的定义是离散的、可逐日判定的形态**，不是连续的指标值。
   例如「MACD金叉」= DIF 上穿 DEA 的当天，而不是「DIF 大于 DEA」的每一天。
   这样才能把「历史上哪一天出现过同样形态」对齐起来做回测。

数据口径
--------
- 长历史：`data/hist.db` 的 `daily_hist`（前复权，4360 只、最早 1990 年）
- 近端：`data/unified_data.db` 的 `daily`（前复权，最近约 288 个交易日）
- 两库拼接时**按重叠期末日比值整体缩放** hist 的价格，把复权基准搬到 unified 上，
  否则除权接缝会被当成巨幅回撤（详见 `_load_history` 的注释）。
- 横截面回测只用 unified（300 根足够算到 MA120/60 日新高），不顺带读 hist.db：
  5554 只逐只查长历史会把一次分析拖到分钟级。

无未来函数
----------
所有指标均为 trailing（cumsum 滑动 / rolling / ewm / shift 正向），
信号 mask[i] 只依赖 [0..i] 的数据。`scripts/test_ta.py` 用「截断不变性」
对每个原语做逐点验证：f(series)[T] == f(series[:T+1])[T]。
回测收益当然要用未来数据（那是被衡量对象），但**评分与信号绝不用**。
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..core import batchload, db

# ============================================================================
# 参数
# ============================================================================
DEFAULTS = {
    # 指标预热：少于该根数不出评分（MA60 + 60 日新高 + ATR 都要这段历史）
    "WARMUP": 70,
    # 回测持有期（交易日）——需求明确要求 5 个交易日
    "HORIZON": 5,
    # 事件冷却期：命中后 N 个交易日内不再重复计数，避免「连续 8 天同一信号算 8 个样本」
    "COOLDOWN": 5,
    # 「当前信号」判定回看：最近 N 个交易日内出现过即算当前成立
    "RECENT_DAYS": 3,
    # 可回测门槛：当前命中的信号数少于该值 → 判定「不可选」
    "MIN_SIGNALS": 2,
    # 横截面：回看最近 N 个交易日（约 1 个月）
    "CROSS_WINDOW": 21,
    # 横截面：单只股票最少 K 线（够算 MA120 与 60 日高点）
    "CROSS_MIN_BARS": 200,
    # 横截面：最多返回多少条明细（CSV 仍全量）
    "CROSS_MAX_ROWS": 300,
    # 个股回测：最多保留多少个历史事件明细（统计用全量）
    "STOCK_MAX_EVENTS": 400,
    # 是否默认做横截面回测（较慢，约 10-20 秒）
    "DO_CROSS": True,
    # 是否做评分分档检验
    "DO_SCORE_BT": True,
    # ---- 评分排行（Top N，可并列） ----
    "RANK_TOP": 10,          # 榜单长度
    "RANK_MIN_BARS": 180,    # 个股最少 K 线（够算 MA120 + 布林分位）
}

# 维度权重（合计 100）
WEIGHTS = {
    "trend":    {"label": "趋势",   "max": 25,
                 "desc": "均线排列 + MA20/MA60 斜率（趋势跟随口径）"},
    "momentum": {"label": "动量",   "max": 20,
                 "desc": "MACD 状态 + RSI 区间 + 20 日涨幅"},
    "position": {"label": "位置",   "max": 15,
                 "desc": "布林 %B + 距 60 日高点 + 相对 60 日成交量加权均价"},
    "volume":   {"label": "量能",   "max": 15,
                 "desc": "5/20 量比 + 量能趋势 + 涨跌日量能配合"},
    "signal":   {"label": "信号",   "max": 15,
                 "desc": "命中信号数量 + 是否含强信号"},
    "risk":     {"label": "风险",   "max": 10,
                 "desc": "波动适中度 + 短期回撤 + 流动性"},
}
WEIGHT_SUM = sum(v["max"] for v in WEIGHTS.values())
assert WEIGHT_SUM == 100, f"权重合计必须为 100，当前 {WEIGHT_SUM}"

# ----------------------------------------------------------------------------
# 反抽评分（第二把尺子）
# ----------------------------------------------------------------------------
# ★为什么必须再加一把尺子（用户 2026-10-24 提出，附实证）
# ----------------------------------------------------------------------------
# 上面的 6 维加权是**算术累加**：任何一个维度弱，都会直接扣掉它的份额，
# 且靠别的维度补不回来。可是真实市场里存在「弱趋势 + 强劲反抽」——
# 最典型的是暴跌之后的第一次放量反抽，此时趋势、动量、位置**同时**是残的
# （刚刚跌完，指标必然难看），唯一亮起来的是**量能**。
#
# 实证（688585，2025-04-07 ~ 2025-08-05 从 6.18 涨到 110.48，+1688%）：
#   2025-04-07 暴跌 −18.4% → 均衡分 34.7（趋势 11 / 动量 3 / 位置 4 / 量能 6.5）
#   2025-04-09 反抽 +6.5%  → 均衡分 36.0（趋势 9.5 / 动量 4.8 / **量能 10.0 满分**）
#   2025-04-11 反抽 +3.5%  → 均衡分 41.7，而此后 4 个月涨了 14 倍
# 也就是说：量能这一项已经打到满分，却只换来 36 分 —— 算术平均把「单点爆发」淹没了。
#
# 解法不是调权重（调权重仍然补不回来），而是**并列第二把尺子**：
#   · 均衡分（原 6 维）：回答「技术面整体健康吗」→ 趋势跟随口径
#   · 反抽分（新增 3 维 + 1 个门控）：回答「是不是超跌后的第一波反抽」
# 两把尺子**绝不混排、绝不加权合成**（项目铁律：双轨制不混排）。
# 反抽分用「超跌深度」当门控——没跌过的票，反抽分必然低，
# 这样就不会出现「任何一只上涨的票反抽分都高」的退化解。
REBOUND_DIMS = {
    "revive": {"label": "转强动能", "max": 35,
               "desc": "放量 + MACD 柱转强 + MA5 拐头 + KDJ 转多"},
    "hold":   {"label": "止跌确认", "max": 25,
               "desc": "近 3 日不再创新低 + 收盘站上 MA5 + 阳线占比"},
    "power":  {"label": "反抽力度", "max": 40,
               "desc": "当日涨幅 + 近 3 日累计涨幅 + 放量阳线占比"},
}
REBOUND_GATE = {
    "label": "超跌深度",
    "desc": "距 60 日高点回撤 / RSI 位置 / 布林 %B / 20 日跌幅 四项合成，"
            "作为门控系数（0~1）乘在反抽质量上",
}
# 形态类型判定阈值
# ★阈值来自 `scripts/_ta_rebound_calib.py` 的全市场长历史标定（400 只 / 139 万个时点）：
#   反抽分 30~50 档：20 日胜率 56.2%、均值 +3.04%（对照 <30 档 49.8% / +1.31%）
#   反抽分 ≥50 档  ：20 日胜率 72.3%、均值 +10.40%（样本 4714，仅占 0.34%）
#   所以「弱趋势强反抽」的门槛定在 **30**（有明确超额且不至于稀到看不见），
#   50 以上另标「强反抽」。最初凭直觉写 50，实测会漏掉绝大部分有效样本。
REBOUND_TYPES = [
    (65.0, 50.0, "both",   "趋势 + 反抽共振", "两把尺子同时亮：趋势健康且刚从低位启动"),
    (65.0, 0.0,  "trend",  "趋势型",          "均衡分高、反抽分低：趋势跟随口径的机会"),
    (0.0,  30.0, "rebound", "弱趋势强反抽",     "均衡分低但反抽分高——算术平均会低估这一类"),
    (0.0,  0.0,  "none",   "两类都不显著",      "两把尺子都没有明确信号"),
]


# ============================================================================
# 市场背景（牛 / 熊 / 震荡）
# ============================================================================
# ★为什么要做这个：同一套分数在不同市场状态下**方向是反的**
# ----------------------------------------------------------------------------
# 判据（只用当日及之前的数据）：全市场等权指数相对 MA250 的位置 + MA60/MA120 排列。
#   pos250 > +3% 且 MA60>MA120 → 牛；pos250 < −3% 且 MA60<MA120 → 熊；其余 → 震荡
# 历史占比：牛 47.2% / 熊 24.6% / 震荡 24.8%（其余 3.4% 数据不足）
#
# 用 500 只股票、171 万个历史时点，看 6 个维度与未来 20 日收益的秩相关：
#
#   维度        全样本     牛市      熊市      震荡
#   趋势       -0.041   -0.018   -0.124   -0.028
#   动量       -0.023   +0.004   -0.101   -0.005
#   位置       -0.035   -0.002   -0.104   -0.041
#   量能       +0.005   +0.004   +0.007   +0.006
#   信号       -0.020   -0.001   -0.064   -0.018
#   风险       -0.061   -0.034   -0.107   -0.086
#   反抽分     +0.020   -0.001   **+0.065**   +0.022
#
# 结论一：**熊市里六个维度全是负相关**（−0.06 ~ −0.12）。「调权重」救不了熊市——
#   所有权重都是负的，怎么配权总分都是负的。必须**换尺子**：熊市用反抽分，
#   并且知道均衡分高的票会补跌。
# 结论二：**反抽分在三种状态下都是正 IC**（−0.001 / +0.065 / +0.022），
#   这是本模块里最稳的选股因子；均衡分只在牛市的高分档（80-100）站得住。
# 结论三：牛市里均衡分整体 IC 是负的（−0.017）但分档呈 U 型（0-35 档 +1.77%、
#   80-100 档 +2.05%，中间 35-50 最差 +0.93%）——所以「按均衡分取 top10」在牛市
#   仍然成立（取到的都在右端），但**不能用它做中位筛**。
#
# 熊市分档（20 日）：均衡 0-35 → 55.8% / +3.22%，80-100 → 44.3% / +0.16%（反向）
#                   反抽 30-50 → 59.5% / +3.99%，≥50 → 75.8% / +11.18%
# 逐年：熊市「低分档(0-50) 优于 高分档(65-100)」**20/20 年全部成立**；
#       震荡 18/27 年（67%，不稳定）；牛市 11/25 年（44%，基本无方向）。
#       → 所以只有熊市的反向是「铁的」，另外两态的结论弱，页面必须分别说明。
#
# 口径与局限：
#   · 分档收益是「全市场横截面平均」，不是单只股票的保证；
#   · 反抽分 ≥50 档样本少（1200~3300 个），胜率高但不算大样本；
#   · 震荡/牛市的 IC 接近 0 且逐年不稳定，所以那两态只给「别指望选股」的提示，
#     不硬套一套看起来厉害的权重。
REGIMES: Dict[str, dict] = {
    "bull": {
        "label": "牛市", "short": "牛",
        "desc": "全市场等权指数在 MA250 上方 3% 以上，且 MA60 > MA120",
        "primary": "balance",
        "balance_ic": -0.017, "rebound_ic": -0.001,
        "sample": 869673, "year_pos": "11/25",
        "balance_bands": [(0, 35, "0-35", 52.4, 1.77), (35, 50, "35-50", 48.9, 0.93),
                          (50, 65, "50-65", 49.1, 1.19), (65, 80, "65-80", 49.7, 1.50),
                          (80, 101, "80-100", 49.1, 2.05)],
        "rebound_bands": [(0, 30, "0-30", 49.5, 1.51), (30, 50, "30-50", 52.5, 1.54),
                          (50, 101, "50+", 59.1, 4.16)],
        "verdict": ("牛市里均衡分整体 IC −0.017，但分档是 U 型：0-35 档 +1.77%、"
                    "80-100 档 +2.05%（最高），中间 35-50 档最差只有 +0.93%。"
                    "所以「取高分」仍可用，「按中间档筛」会吃亏。"
                    "反抽分 ≥50 档 +4.16%，但样本只有 1335 个。"),
        "advice": "牛市：高分档仍可用，别拿中间档当选股依据",
    },
    "bear": {
        "label": "熊市", "short": "熊",
        "desc": "全市场等权指数在 MA250 下方 3% 以上，且 MA60 < MA120",
        "primary": "rebound",
        "balance_ic": -0.124, "rebound_ic": 0.065,
        "sample": 397901, "year_pos": "20/20",
        "balance_bands": [(0, 35, "0-35", 55.8, 3.22), (35, 50, "35-50", 50.2, 1.36),
                          (50, 65, "50-65", 47.1, 0.62), (65, 80, "65-80", 44.8, 0.26),
                          (80, 101, "80-100", 44.3, 0.16)],
        "rebound_bands": [(0, 30, "0-30", 48.2, 1.03), (30, 50, "30-50", 59.5, 3.99),
                          (50, 101, "50+", 75.8, 11.18)],
        "verdict": ("熊市里均衡分**完全反向**：0-35 档 20 日 +3.22%、胜率 55.8%，"
                    "80-100 档只有 +0.16%、胜率 44.3%——技术面越强越容易补跌。"
                    "而且 **20/20 年都成立**，不是靠某几次股灾。"
                    "这时候该看反抽分：30-50 档 +3.99%、≥50 档 +11.18%。"),
        "advice": "熊市：均衡分反向看，主用反抽分找超跌",
    },
    "range": {
        "label": "震荡市", "short": "震",
        "desc": "不满足牛/熊条件（指数在 MA250 附近，或均线排列与位置不一致）",
        "primary": "rebound",
        "balance_ic": -0.028, "rebound_ic": 0.022,
        "sample": 450118, "year_pos": "18/27",
        "balance_bands": [(0, 35, "0-35", 50.6, 1.16), (35, 50, "35-50", 47.5, 0.62),
                          (50, 65, "50-65", 47.9, 0.79), (65, 80, "65-80", 47.9, 1.08),
                          (80, 101, "80-100", 46.1, 0.71)],
        "rebound_bands": [(0, 30, "0-30", 47.7, 0.80), (30, 50, "30-50", 51.8, 1.67),
                          (50, 101, "50+", 61.3, 6.06)],
        "verdict": ("震荡市里均衡分也是弱反向（IC −0.028）：0-35 档 +1.16% 反而最好，"
                    "80-100 档只有 +0.71%。但逐年只有 18/27 年成立（67%），"
                    "**不稳定**，别把它当成熊市那种铁律。反抽分略优（30-50 档 +1.67%）。"),
        "advice": "震荡：两把尺子都弱，反抽分略优，别下重注",
    },
}
REGIME_KEYS = ("bull", "bear", "range")
REGIME_CALIB_META = {
    "sample": "500 只股票 / 171 万个历史时点 / 未来 20 日",
    "source": "scripts/_ta_regime_calib.py",
    "year_pos": "熊市「低分档优于高分档」20/20 年成立（震荡 18/27、牛市 11/25）",
    "dist": {"bull": 47.2, "bear": 24.6, "range": 24.8},
}

# 评级分档（只用于展示与配色，不改变分数本身）
GRADES = [
    (80, "强势", "strong", "多数技术维度同向偏多"),
    (65, "偏强", "good", "技术面偏多，但有短板"),
    (50, "中性", "mid", "多空交织，没有明确方向"),
    (35, "偏弱", "weak", "多数维度偏空"),
    (0, "弱势", "bad", "技术面明确偏空"),
]

# ============================================================================
# 技术信号字典（14 个）
# ============================================================================
# ★为什么是「状态」而不是「当天发生的交叉」
# -------------------------------------------------
# 第一版把信号全定义成「当日发生的事件」（当日金叉 / 当日上穿中轨），结果
# 实测 5 只样本里有 4 只只命中 0~1 个信号 → 一律「不可选」。原因是事件型信号
# 天生稀疏：任何一天最多碰上 1~2 个交叉。
# 用户要的是「这只股票现在的技术形态，和历史上/别的股票哪些时刻相似」，
# 所以信号必须是**可持续的状态**（均线多头、MACD 多头、量能放大……），
# 只在少数几个真正形态级的信号上保留「近 N 日内发生」的事件语义。
#
# 共线性控制：每个信号代表不同技术侧面（趋势 / 动量 / 位置 / 量能 / 形态），
# 刻意避开「站上 MA20」「站上 MA60」这类高度同源的描述重复计数。
#
# strong=True 表示「形态级信号」：单靠它就足够有信息量，
# 在评分里额外加分，回测里也单独统计其贡献。
SIGNALS: Dict[str, dict] = {
    "ma_bull": {
        "label": "均线多头排列", "strong": False, "group": "趋势",
        "desc": "MA5>MA10>MA20>MA60 且 MA20 仍在上行",
    },
    "above_ma60": {
        "label": "站稳60日线", "strong": False, "group": "趋势",
        "desc": "收盘在 MA60 上方，且 MA60 相比 5 日前不再下行",
    },
    "macd_bull": {
        "label": "MACD多头", "strong": False, "group": "动量",
        "desc": "DIF 在 DEA 上方（红柱区）",
    },
    "macd_gold": {
        "label": "MACD近3日金叉", "strong": False, "group": "动量",
        "desc": "最近 3 个交易日内出现 DIF 上穿 DEA",
    },
    "kdj_bull": {
        "label": "KDJ多头", "strong": False, "group": "动量",
        "desc": "K 在 D 上方且 K<80（多头但未极端超买）",
    },
    "rsi_strong": {
        "label": "RSI强势区", "strong": False, "group": "动量",
        "desc": "RSI(14) 落在 50~72（强势而不超买）",
    },
    "boll_upper": {
        "label": "布林中轨上方", "strong": False, "group": "位置",
        "desc": "收盘在布林(20,2) 中轨之上（%B>50%）",
    },
    "near_high60": {
        "label": "逼近60日高点", "strong": False, "group": "位置",
        "desc": "收盘价 ≥ 近 60 日最高价的 95%",
    },
    "boll_squeeze": {
        "label": "布林带宽收口", "strong": False, "group": "位置",
        "desc": "带宽处于近 120 日的最低 20% 分位（变盘前夜）",
    },
    "vol_expand": {
        "label": "量能放大", "strong": False, "group": "量能",
        "desc": "5 日均量 ≥ 20 日均量的 1.2 倍",
    },
    "vol_break": {
        "label": "近3日放量突破", "strong": True, "group": "量能",
        "desc": "3 日内出现「涨幅≥3% + 量≥1.8×20日均量 + 收盘创20日新高」",
    },
    "boll_lower": {
        "label": "BOLL下轨支撑", "strong": True, "group": "形态",
        "desc": "3 日内触及/跌破下轨后收盘收回（收回幅度≥带宽30%）且仍在中轨下",
    },
    "w_bottom": {
        "label": "W底突破", "strong": True, "group": "形态",
        "desc": "5 日内出现「60日双低点差≤3%、间隔≥10日、收盘破颈线」",
    },
    "pullback_ma20": {
        "label": "回踩20日线", "strong": False, "group": "形态",
        "desc": "5 日内最低价触及 MA20（±2%）且当日收盘仍在其上",
    },
}

SIGNAL_KEYS = list(SIGNALS.keys())
STRONG_KEYS = {k for k, v in SIGNALS.items() if v["strong"]}


# ============================================================================
# 基础工具（全部 trailing，无未来函数）
# ============================================================================
def _r(v, nd: int = 2):
    """安全取整：NaN/Inf → None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, nd)


def _shift(x: np.ndarray, n: int = 1) -> np.ndarray:
    """向后平移 n 根（前 n 位补 NaN）。只用过去数据。"""
    out = np.full(len(x), np.nan)
    if n <= 0:
        out[:] = x
    elif n < len(x):
        out[n:] = x[:-n]
    return out


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    """简单移动平均（cumsum 实现，O(m)）。"""
    m = len(x)
    out = np.full(m, np.nan)
    if n <= 0 or m < n:
        return out
    cs = np.concatenate([[0.0], np.cumsum(x)])
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _sma_prev(x: np.ndarray, n: int) -> np.ndarray:
    """不含当日的 n 日均值（用于量比，避免当日成交量污染分母）。"""
    return _shift(_sma(x, n), 1)


def _rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    """含当日的 n 根最大值。"""
    if n <= 0 or len(x) < 1:
        return np.full(len(x), np.nan)
    return pd.Series(x).rolling(n, min_periods=n).max().to_numpy(dtype=float)


def _rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 0 or len(x) < 1:
        return np.full(len(x), np.nan)
    return pd.Series(x).rolling(n, min_periods=n).min().to_numpy(dtype=float)


def _rolling_sum(x: np.ndarray, n: int) -> np.ndarray:
    m = len(x)
    out = np.full(m, np.nan)
    if n <= 0 or m < n:
        return out
    cs = np.concatenate([[0.0], np.cumsum(x)])
    out[n - 1:] = cs[n:] - cs[:-n]
    return out


def _rolling_any(mask: np.ndarray, n: int) -> np.ndarray:
    """最近 n 根里是否有 True（含当日）。"""
    m = len(mask)
    out = np.zeros(m, dtype=bool)
    if n <= 0 or m == 0:
        return out
    cs = np.concatenate([[0], np.cumsum(mask.astype(np.int64))])
    k = min(n, m)
    out[k - 1:] = (cs[k:] - cs[:-k]) > 0
    if n > m:
        out[:] = (cs[m] > 0)
    return out


def _cross_up(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a 上穿 b（当日 a>b 且前一日 a<=b）。"""
    m = len(a)
    out = np.zeros(m, dtype=bool)
    if m < 2:
        return out
    prev_ok = np.isfinite(a[:-1]) & np.isfinite(b[:-1])
    cur_ok = np.isfinite(a[1:]) & np.isfinite(b[1:])
    out[1:] = prev_ok & cur_ok & (a[:-1] <= b[:-1]) & (a[1:] > b[1:])
    return out


def _seg(x, pts) -> np.ndarray:
    """分段线性映射到 [0,1]（越界钳到端点，NaN → 0）。

    pts 必须按 x 升序：[(x0,y0),(x1,y1),...]。
    用来把「斜率/分位/比率」这类连续量变成可加的分值，
    比纯线性映射更能表达「适中最好」的区间型偏好（如 ATR%）。
    """
    arr = np.asarray(x, dtype=float)
    xs = np.array([p[0] for p in pts], dtype=float)
    ys = np.array([p[1] for p in pts], dtype=float)
    with np.errstate(invalid="ignore"):
        out = np.interp(arr, xs, ys)
    out = np.where(np.isfinite(arr) & np.isfinite(out), out, 0.0)
    return np.clip(out, 0.0, 1.0)


# ============================================================================
# 指标计算
# ============================================================================
def _indicators(o, h, l, c, v, amo) -> Dict[str, np.ndarray]:
    """一次算出全部指标数组。所有值都是 trailing 的（第 i 位只依赖 0..i）。"""
    m = len(c)
    out: Dict[str, np.ndarray] = {"n": m}

    for n in (5, 10, 20, 60, 120):
        out[f"ma{n}"] = _sma(c, n)

    # --- MACD(12,26,9) ---
    # 用 pandas ewm（C 实现）而不是手写循环：5554 只 × 300 根时循环会慢一个量级
    sc = pd.Series(c)
    ema12 = sc.ewm(span=12, adjust=False).mean().to_numpy(dtype=float)
    ema26 = sc.ewm(span=26, adjust=False).mean().to_numpy(dtype=float)
    dif = ema12 - ema26
    dea = pd.Series(dif).ewm(span=9, adjust=False).mean().to_numpy(dtype=float)
    out["dif"], out["dea"] = dif, dea

    # --- RSI(14) Wilder ---
    # ★边界：区间内一次都没跌 → dn=0 → 除零。连板股必现。
    #   必须显式给 100（只涨）或 50（完全不动），不能留 NaN，
    #   否则会被当成「数据缺失」，把「极度强势」误读成「没数据」。
    diff = np.diff(c, prepend=np.nan)
    up = pd.Series(np.where(diff > 0, diff, 0.0)).ewm(alpha=1 / 14, adjust=False).mean().to_numpy(dtype=float)
    dn = pd.Series(np.where(diff < 0, -diff, 0.0)).ewm(alpha=1 / 14, adjust=False).mean().to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(dn > 0, up / np.where(dn > 0, dn, 1.0), np.nan)
        rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = np.where((dn <= 0) & (up > 0), 100.0, rsi)
    rsi = np.where((dn <= 0) & (up <= 0) & (np.arange(m) >= 14), 50.0, rsi)
    out["rsi"] = rsi

    # --- KDJ(9,3,3) ---
    low9 = _rolling_min(l, 9)
    high9 = _rolling_max(h, 9)
    rng = high9 - low9
    with np.errstate(divide="ignore", invalid="ignore"):
        rsv = np.where(rng > 0, (c - low9) / np.where(rng > 0, rng, 1.0) * 100.0, np.nan)
    # ★同样的一字板边界：9 日内最高=最低 → 分母 0，中性值 50
    rsv = np.where(np.isfinite(rsv), rsv, np.where(np.arange(m) >= 8, 50.0, np.nan))
    k = pd.Series(rsv).ewm(alpha=1 / 3, adjust=False).mean().to_numpy(dtype=float)
    d3 = pd.Series(k).ewm(alpha=1 / 3, adjust=False).mean().to_numpy(dtype=float)
    out["k"], out["kd"] = k, d3

    # --- BOLL(20,2)，总体标准差 ddof=0（与 conditions.py 同口径） ---
    mid = _sma(c, 20)
    m20 = _rolling_sum(c * c, 20) / 20.0
    var = np.maximum(m20 - mid * mid, 0.0)
    sd = np.sqrt(var)
    out["bmid"], out["bup"], out["blow"] = mid, mid + 2.0 * sd, mid - 2.0 * sd

    # --- ATR(14) ---
    tr = np.maximum.reduce([
        h - l,
        np.abs(h - _shift(c, 1)),
        np.abs(l - _shift(c, 1)),
    ])
    out["atr"] = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy(dtype=float)

    # --- 量能 ---
    out["v5"] = _sma(v, 5)
    out["v20"] = _sma(v, 20)
    out["v20_prev"] = _sma_prev(v, 20)
    out["amo20"] = _sma(amo, 20) if amo is not None else np.full(m, np.nan)

    # --- 滚动高低 ---
    for n in (20, 60, 120, 250):
        out[f"hh{n}"] = _rolling_max(h, n)
        out[f"ll{n}"] = _rolling_min(l, n)

    # --- 60 日成交量加权均价（筹码成本线） ---
    pv = c * v
    num = _rolling_sum(pv, 60)
    den = _rolling_sum(v, 60)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["vwap60"] = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)

    # --- 20 日涨幅（ROC） ---
    with np.errstate(divide="ignore", invalid="ignore"):
        out["roc20"] = np.where(_shift(c, 20) > 0, (c / _shift(c, 20) - 1.0) * 100.0, np.nan)

    # --- 20 日振幅（风险维度用） ---
    hh20, ll20 = out["hh20"], out["ll20"]
    with np.errstate(divide="ignore", invalid="ignore"):
        out["amp20"] = np.where(ll20 > 0, (hh20 - ll20) / np.where(ll20 > 0, ll20, 1.0) * 100.0, np.nan)

    return out


# ============================================================================
# 信号判定（返回 {key: bool ndarray}，第 i 位 = 第 i 日收盘时该形态成立）
# ============================================================================
def _w_bottom_mask(h, l, c, win: int = 60, tol: float = 0.03, gap: int = 10) -> np.ndarray:
    """W 底突破。只在「收盘创 20 日新高」的候选日做细节判定（控制复杂度）。

    判定：窗口内最低点 i1，找与它价差 ≤tol、间隔 ≥gap 的另一低点 i2，
    且当日收盘 > 两低点之间反弹高点（颈线）。
    """
    m = len(c)
    out = np.zeros(m, dtype=bool)
    if m < win + 2:
        return out
    hh20_prev = _shift(_rolling_max(h, 20), 1)
    cand = np.nonzero(np.isfinite(hh20_prev) & (c > hh20_prev))[0]
    for i in cand:
        if i < win:
            continue
        seg = l[i - win:i + 1]
        if seg.size < win + 1:
            continue
        i1 = int(np.argmin(seg))
        base = float(seg[i1])
        if not (base > 0):
            continue
        order = np.argsort(seg)
        for kk in order[:min(len(order), 40)]:
            i2 = int(kk)
            if abs(i1 - i2) < gap:
                continue
            if abs(float(seg[i2]) - base) / base > tol:
                continue
            lo_, hi_ = (i1, i2) if i1 < i2 else (i2, i1)
            if hi_ - lo_ < 2:
                continue
            neck = float(np.max(h[lo_ + 1:hi_]))
            if float(c[i]) > neck:
                out[i] = True
                break
    return out


def _signal_masks(ind, o, h, l, c, v) -> Dict[str, np.ndarray]:
    """14 个技术信号的逐日布尔 mask（第 i 位 = 第 i 日收盘时该状态成立）。

    状态型信号直接是当日判定；事件型信号（金叉/突破/触轨/W底）先算当日事件，
    再用 trailing 的 `_rolling_any` 展开成「近 N 日内发生过」——这样第 i 位
    仍然只依赖 [0..i]，不会引入未来函数。
    """
    m = len(c)
    ma5, ma10, ma20, ma60 = ind["ma5"], ind["ma10"], ind["ma20"], ind["ma60"]
    dif, dea, k, kd, rsi = ind["dif"], ind["dea"], ind["k"], ind["kd"], ind["rsi"]
    mid, up, lo = ind["bmid"], ind["bup"], ind["blow"]
    sig: Dict[str, np.ndarray] = {}

    # ---- 趋势 ----
    with np.errstate(invalid="ignore"):
        sig["ma_bull"] = ((ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)
                          & (ma20 > _shift(ma20, 1)))
        sig["above_ma60"] = ((c > ma60) & np.isfinite(ma60)
                             & (ma60 >= _shift(ma60, 5)))

    # ---- 动量 ----
    with np.errstate(invalid="ignore"):
        sig["macd_bull"] = np.isfinite(dif) & np.isfinite(dea) & (dif > dea)
    sig["macd_gold"] = _rolling_any(_cross_up(dif, dea), 3)
    with np.errstate(invalid="ignore"):
        sig["kdj_bull"] = np.isfinite(k) & np.isfinite(kd) & (k > kd) & (k < 80)
        sig["rsi_strong"] = np.isfinite(rsi) & (rsi >= 50.0) & (rsi <= 72.0)

    # ---- 位置 ----
    with np.errstate(invalid="ignore"):
        sig["boll_upper"] = np.isfinite(mid) & (c > mid)
        hh60 = ind["hh60"]
        sig["near_high60"] = np.isfinite(hh60) & (hh60 > 0) & (c >= hh60 * 0.95)

    # 带宽收口：带宽处于近 120 日最低 20% 分位
    width = mid - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        bw = np.where(np.isfinite(mid) & (mid > 0),
                      width / np.where(mid > 0, mid, 1.0) * 100.0, np.nan)
    # ★性能：一开始用 pandas rolling().apply(lambda) 逐窗算分位，
    #   5554 只 × 300 根要跑 166 万次 Python lambda（20 秒以上）。
    #   换成 numpy 滑窗视图后是纯向量化，全市场扫一遍不到 1 秒。
    rank = np.full(m, np.nan)
    fin = np.isfinite(bw)
    if fin.any():
        fv = int(np.argmax(fin))
        seg = bw[fv:]
        # 只在「从首个有效点起连续无缺口」时计算；有洞就整段不判定，
        # 绝不 np.isfinite 压缩（压缩会让标记与真实日期错位 = 静默错值）
        if seg.size >= 120 and np.isfinite(seg).all():
            win_view = np.lib.stride_tricks.sliding_window_view(seg, 120)
            r = (win_view <= win_view[:, -1:]).sum(axis=1) / 120.0 * 100.0
            rank[fv + 119:fv + 119 + r.size] = r
    with np.errstate(invalid="ignore"):
        sig["boll_squeeze"] = np.isfinite(rank) & (rank <= 20.0)

    # ---- 量能 ----
    v5, v20, v20p = ind["v5"], ind["v20"], ind["v20_prev"]
    with np.errstate(invalid="ignore", divide="ignore"):
        sig["vol_expand"] = (np.isfinite(v20) & (v20 > 0) & (v5 >= v20 * 1.2))

    prev_c = _shift(c, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        pct = np.where(prev_c > 0, (c / np.where(prev_c > 0, prev_c, 1.0) - 1.0) * 100.0, np.nan)
    vok = np.isfinite(v20p) & (v20p > 0) & (v >= v20p * 1.8)
    hh20_prev = _shift(ind["hh20"], 1)
    vb_day = (pct >= 3.0) & vok & np.isfinite(hh20_prev) & (c > hh20_prev)
    sig["vol_break"] = _rolling_any(vb_day, 3)

    # ---- 形态 ----
    with np.errstate(invalid="ignore", divide="ignore"):
        touch = np.isfinite(lo) & (width > 0) & (l <= lo * 1.01)
        recover = (c > lo) & ((c - lo) >= width * 0.30)
        below = c < mid
    sig["boll_lower"] = _rolling_any(touch & recover & below, 3)

    sig["w_bottom"] = _rolling_any(_w_bottom_mask(h, l, c), 5)

    with np.errstate(invalid="ignore", divide="ignore"):
        near = (np.isfinite(ma20) & (ma20 > 0)
                & (l <= ma20 * 1.02) & (l >= ma20 * 0.96))
        sig["pullback_ma20"] = _rolling_any(near & (c > ma20), 5)

    # ---- 兜底：键集必须恒定，缺的补 False ----
    for key in SIGNAL_KEYS:
        if key not in sig:
            sig[key] = np.zeros(m, dtype=bool)
        sig[key] = np.asarray(sig[key], dtype=bool)
    return sig


def _hit_count(sig: Dict[str, np.ndarray], m: int) -> np.ndarray:
    n = np.zeros(m, dtype=np.int16)
    for key in SIGNAL_KEYS:
        n += sig[key].astype(np.int16)
    return n


# ============================================================================
# 评分（向量化，返回每个维度的逐日得分数组）
# ============================================================================
def _score_parts(ind, sig, o, h, l, c, v, amo) -> Dict[str, np.ndarray]:
    """返回 {维度: 逐日得分 ndarray}。每项分值上限 = WEIGHTS[维度]['max']。"""
    m = len(c)
    ma5, ma10, ma20, ma60 = ind["ma5"], ind["ma10"], ind["ma20"], ind["ma60"]

    # ---------------- 趋势 25 ----------------
    a_full = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)
    a_half = (ma5 > ma10) & (ma10 > ma20) & ~a_full
    a_weak = (ma5 > ma20) & (ma20 > ma60) & ~a_full & ~a_half
    a_base = (ma20 > ma60) & ~a_full & ~a_half & ~a_weak
    align = np.zeros(m)
    align[a_base] = 0.38
    align[a_weak] = 0.55
    align[a_half] = 0.78
    align[a_full] = 1.00
    boost = (c > ma20) & (ma20 > _shift(ma20, 1))
    align = np.clip(align + np.where(boost, 0.12, 0.0), 0.0, 1.0)
    align[~np.isfinite(ma60)] = 0.0

    with np.errstate(divide="ignore", invalid="ignore"):
        sl20 = np.where(ma20 > 0, (ma20 - _shift(ma20, 5)) / np.where(ma20 > 0, ma20, 1.0) * 100.0, np.nan)
        sl60 = np.where(ma60 > 0, (ma60 - _shift(ma60, 20)) / np.where(ma60 > 0, ma60, 1.0) * 100.0, np.nan)
    s20 = _seg(sl20, [(-2.0, 0.0), (0.0, 0.35), (1.0, 0.75), (3.0, 1.0)])
    s60 = _seg(sl60, [(-4.0, 0.0), (0.0, 0.35), (2.0, 0.80), (5.0, 1.0)])
    trend = align * 10.0 + s20 * 8.0 + s60 * 7.0

    # ---------------- 动量 20 ----------------
    dif, dea = ind["dif"], ind["dea"]
    hist = (dif - dea) * 2.0
    mm = np.where((dif > dea) & (dif > 0), 1.00,
                  np.where((dif > dea) & (dif <= 0), 0.72,
                           np.where((dif <= dea) & (dif > 0), 0.38, 0.15)))
    hist_up = hist > _shift(hist, 5)
    mm = np.clip(mm + np.where(hist_up, 0.12, -0.06), 0.0, 1.0)
    macd_s = mm * 8.0

    rsi = ind["rsi"]
    rsi_s = _seg(rsi, [(0, 0.30), (20, 0.35), (30, 0.42), (40, 0.62),
                       (50, 0.85), (58, 1.00), (70, 0.92), (78, 0.62),
                       (88, 0.30), (100, 0.22)]) * 6.0

    roc_s = _seg(ind["roc20"], [(-15, 0.0), (-5, 0.30), (0, 0.50),
                                (5, 0.75), (15, 1.0), (30, 0.85)]) * 6.0
    momentum = macd_s + rsi_s + roc_s

    # ---------------- 位置 15 ----------------
    mid, up, lo = ind["bmid"], ind["bup"], ind["blow"]
    rng = up - lo
    with np.errstate(divide="ignore", invalid="ignore"):
        pctb = np.where(rng > 0, (c - lo) / np.where(rng > 0, rng, 1.0), np.nan)
    pb_s = _seg(pctb, [(0, 0.45), (0.15, 0.55), (0.35, 0.78), (0.55, 1.00),
                       (0.85, 0.95), (1.05, 0.65), (1.30, 0.35)]) * 5.0

    hh60 = ind["hh60"]
    with np.errstate(divide="ignore", invalid="ignore"):
        dh = np.where(hh60 > 0, c / np.where(hh60 > 0, hh60, 1.0), np.nan)
    dh_s = _seg(dh, [(0.70, 0.15), (0.85, 0.55), (0.95, 0.85), (1.00, 1.00)]) * 5.0

    vw = ind["vwap60"]
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = np.where(vw > 0, c / np.where(vw > 0, vw, 1.0), np.nan)
    vw_s = _seg(vr, [(0.85, 0.10), (0.95, 0.45), (1.00, 0.85),
                     (1.08, 1.00), (1.25, 0.70)]) * 5.0
    position = pb_s + dh_s + vw_s

    # ---------------- 量能 15 ----------------
    v5, v20 = ind["v5"], ind["v20"]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio5 = np.where(v20 > 0, v5 / np.where(v20 > 0, v20, 1.0), np.nan)
    vr_s = _seg(ratio5, [(0.5, 0.10), (0.8, 0.45), (1.0, 0.85),
                         (1.5, 1.00), (2.5, 0.75), (4.0, 0.40)]) * 6.0

    with np.errstate(divide="ignore", invalid="ignore"):
        vt = np.where(v20 > 0, (v20 - _shift(v20, 10)) / np.where(v20 > 0, v20, 1.0) * 100.0, np.nan)
    vt_s = _seg(vt, [(-30, 0.10), (-10, 0.35), (0, 0.55), (20, 0.85), (50, 1.0)]) * 5.0

    # 涨跌日量能配合：近 10 日「涨日成交量 / 跌日成交量」
    chg = c - _shift(c, 1)
    upv = np.where(chg > 0, v, 0.0)
    dnv = np.where(chg < 0, v, 0.0)
    upv10, dnv10 = _rolling_sum(upv, 10), _rolling_sum(dnv, 10)
    with np.errstate(divide="ignore", invalid="ignore"):
        pvr = np.where(dnv10 > 0, upv10 / np.where(dnv10 > 0, dnv10, 1.0), np.nan)
    pv_s = _seg(pvr, [(0.5, 0.05), (0.8, 0.35), (1.0, 0.70), (1.3, 1.00), (2.0, 0.90)]) * 4.0
    volume = vr_s + vt_s + pv_s

    # ---------------- 信号 15 ----------------
    nhit = _hit_count(sig, m).astype(float)
    # ★状态型信号天然命中更多（强势股常见 5~7 个），映射曲线相应右移：
    #   第一版按「0~5 个」映射，结果 5 个就满分，等于把「信号多」当成了「分高」。
    nh_s = _seg(nhit, [(0, 0.0), (1, 0.18), (2, 0.35), (3, 0.52),
                       (4, 0.68), (5, 0.82), (6, 0.92), (7, 1.0)]) * 12.0
    strong = np.zeros(m, dtype=bool)
    for kk in STRONG_KEYS:
        strong |= sig[kk]
    sh_s = np.where(strong, 3.0, np.where(nhit >= 2, 1.5, 0.0))
    signal_part = nh_s + sh_s

    # ---------------- 风险 10 ----------------
    with np.errstate(divide="ignore", invalid="ignore"):
        atr_pct = np.where(c > 0, ind["atr"] / np.where(c > 0, c, 1.0) * 100.0, np.nan)
    atr_s = _seg(atr_pct, [(0.8, 0.45), (1.8, 0.85), (2.5, 1.00),
                           (4.0, 0.75), (6.0, 0.40), (10, 0.10)]) * 4.0

    hh60_arr = ind["hh60"]
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(hh60_arr > 0, (c / np.where(hh60_arr > 0, hh60_arr, 1.0) - 1.0) * 100.0, np.nan)
    dd_s = _seg(dd, [(-30, 0.0), (-20, 0.25), (-10, 0.60), (-5, 0.90), (0, 1.00)]) * 3.0

    amo20 = ind.get("amo20")
    if amo20 is None or not np.isfinite(amo20).any():
        liq_s = np.full(m, 1.5)          # 缺失 → 中性半分（不重分配权重）
    else:
        amo_yi = np.where(np.isfinite(amo20), amo20 / 1e8, np.nan)
        liq_s = _seg(amo_yi, [(0.10, 0.05), (0.30, 0.30), (0.60, 0.60),
                              (1.50, 0.90), (3.00, 1.00)]) * 3.0
        # ★缺失值记 0 分而不是「重分配权重」：否则没成交额数据的票会被动得高分
        liq_s = np.where(np.isfinite(amo_yi), liq_s, 0.0)
    risk = atr_s + dd_s + liq_s

    return {"trend": trend, "momentum": momentum, "position": position,
            "volume": volume, "signal": signal_part, "risk": risk}


def _total_score(parts: Dict[str, np.ndarray]) -> np.ndarray:
    total = np.zeros(len(next(iter(parts.values()))), dtype=float)
    for k in WEIGHTS:
        total = total + np.clip(parts[k], 0.0, WEIGHTS[k]["max"])
    return np.clip(total, 0.0, 100.0)


# ============================================================================
# 反抽评分（第二把尺子；与均衡分并列，不合成）
# ============================================================================
def _rebound_parts(ind, o, h, l, c, v) -> Dict[str, np.ndarray]:
    """逐日反抽评分。

    结构：score = 反抽质量(0~100) × 超跌门控(0~1)

    · 反抽质量 = 转强动能 35 + 止跌确认 25 + 反抽力度 40
    · 超跌门控 = f(距60日高点回撤, RSI, 布林%B, 20日跌幅)，取 0.55 次方
      —— 用门控而不是直接加分，是为了避免「任何一只上涨的票反抽分都高」：
         没跌过的票门控接近 0，反抽质量再高也乘不出分。

    所有输入都是 trailing 的（shift 只向后、rolling 只看过去），无未来函数。
    """
    m = len(c)
    ma5 = ind["ma5"]
    rsi = ind["rsi"]
    dif, dea = ind["dif"], ind["dea"]
    k, kd = ind["k"], ind["kd"]
    up, lo, mid = ind["bup"], ind["blow"], ind["bmid"]
    hh60 = ind["hh60"]
    v20p = ind["v20_prev"]

    prev_c = _shift(c, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = np.where(prev_c > 0, (c / np.where(prev_c > 0, prev_c, 1.0) - 1.0) * 100.0, np.nan)

    # ---------------- 门控：超跌深度（4 个视角平均，避免单一口径失灵） ----------------
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(hh60 > 0, (c / np.where(hh60 > 0, hh60, 1.0) - 1.0) * 100.0, np.nan)
    d_dd = _seg(dd, [(-50, 1.00), (-35, 0.92), (-22, 0.72), (-12, 0.40), (-5, 0.10)])
    d_rsi = _seg(rsi, [(15, 1.00), (28, 0.85), (38, 0.55), (48, 0.20), (58, 0.0)])
    rng = up - lo
    with np.errstate(divide="ignore", invalid="ignore"):
        pctb = np.where(rng > 0, (c - lo) / np.where(rng > 0, rng, 1.0), np.nan)
    d_pb = _seg(pctb, [(-0.10, 1.00), (0.10, 0.90), (0.25, 0.65), (0.45, 0.25), (0.60, 0.0)])
    d_roc = _seg(ind["roc20"], [(-30, 1.00), (-20, 0.85), (-10, 0.55), (0, 0.20), (10, 0.0)])
    depth = (d_dd + d_rsi + d_pb + d_roc) / 4.0
    valid = (np.isfinite(hh60) & np.isfinite(rsi)
             & np.isfinite(pctb) & np.isfinite(ind["roc20"]))
    depth = np.where(valid, np.clip(depth, 0.0, 1.0), 0.0)

    # ---------------- ① 转强动能 35 ----------------
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = np.where(v20p > 0, v / np.where(v20p > 0, v20p, 1.0), np.nan)
    r_vol = _seg(vr, [(0.8, 0.0), (1.2, 0.25), (1.8, 0.60), (2.5, 0.85), (4.0, 1.0)])

    hist = (dif - dea) * 2.0
    h1, h2 = _shift(hist, 1), _shift(hist, 2)
    with np.errstate(invalid="ignore"):
        # 红柱=1.0；绿柱但连续两天缩短=0.72；只缩短一天=0.45；继续走弱=0.08
        r_macd = np.where(hist > 0, 1.00,
                          np.where((hist > h1) & (h1 > h2), 0.72,
                                   np.where(hist > h1, 0.45, 0.08)))

    m1, m2 = _shift(ma5, 1), _shift(ma5, 2)
    with np.errstate(invalid="ignore"):
        # 「拐头」比「已在上方」更有价值：从下行转上行 = 1.0
        r_ma5 = np.where((ma5 > m1) & (m1 <= m2), 1.00,
                         np.where(ma5 > m1, 0.55, 0.0))

    with np.errstate(invalid="ignore"):
        r_kdj = np.where(_cross_up(k, kd), 1.00,
                         np.where(k > kd, 0.60,
                                  np.where(k > _shift(k, 1), 0.30, 0.0)))

    revive = (r_vol * 0.35 + r_macd * 0.25 + r_ma5 * 0.20 + r_kdj * 0.20) * 35.0

    # ---------------- ② 止跌确认 25 ----------------
    low3 = _rolling_min(l, 3)
    low20_prev = _shift(_rolling_min(l, 20), 3)
    with np.errstate(invalid="ignore"):
        no_new_low = (np.isfinite(low20_prev) & (low3 > low20_prev)).astype(float)
        above5 = (np.isfinite(ma5) & (c > ma5)).astype(float)
    yang = (c > o).astype(float)
    yang3 = np.nan_to_num(_rolling_sum(yang, 3), nan=0.0) / 3.0
    hold = (no_new_low * 0.40 + above5 * 0.30 + yang3 * 0.30) * 25.0

    # ---------------- ③ 反抽力度 40 ----------------
    c3 = _shift(c, 3)
    with np.errstate(divide="ignore", invalid="ignore"):
        r3 = np.where(c3 > 0, (c / np.where(c3 > 0, c3, 1.0) - 1.0) * 100.0, np.nan)
    p_r3 = _seg(r3, [(-8, 0.0), (-2, 0.10), (2, 0.35), (6, 0.65), (12, 0.88), (20, 1.0)])
    p_d1 = _seg(pct, [(-4, 0.0), (0, 0.12), (3, 0.45), (6, 0.78), (10, 1.0)])
    # 「放量阳线」：收盘>开盘 且 量能 >= 1.3 倍 20 日均量
    with np.errstate(invalid="ignore"):
        vup = ((c > o) & np.isfinite(v20p) & (v20p > 0) & (v > v20p * 1.3)).astype(float)
    vup5 = np.nan_to_num(_rolling_sum(vup, 5), nan=0.0) / 5.0
    power = (p_r3 * 0.45 + p_d1 * 0.30 + vup5 * 0.25) * 40.0

    # ---------------- 聚合 ----------------
    quality = np.clip(revive + hold + power, 0.0, 100.0)
    gate = np.clip(depth, 0.0, 1.0) ** 0.55
    score = np.where(valid, np.clip(quality * gate, 0.0, 100.0), 0.0)

    return {"depth": depth, "revive": revive, "hold": hold,
            "power": power, "quality": quality, "score": score}


def _pattern_type(bal: float, reb: float) -> dict:
    """按两把尺子判定形态类型（顺序即优先级）。"""
    for bal_min, reb_min, key, label, desc in REBOUND_TYPES:
        if bal >= bal_min and reb >= reb_min:
            return {"key": key, "label": label, "desc": desc}
    return {"key": "none", "label": "两类都不显著", "desc": ""}


def _rebound_grade(score: float) -> dict:
    """反抽分单独分档（阈值与均衡分不同：反抽本来就是低胜率、高弹性的机会）。"""
    if score >= 70:
        return {"label": "强反抽", "key": "strong"}
    if score >= 50:
        return {"label": "成立", "key": "good"}
    if score >= 30:
        return {"label": "苗头", "key": "mid"}
    return {"label": "不成立", "key": "none"}


def _grade(score: float) -> dict:
    for lo, name, key, desc in GRADES:
        if score >= lo:
            return {"label": name, "key": key, "desc": desc, "min": lo}
    return {"label": "弱势", "key": "bad", "desc": "技术面明确偏空", "min": 0}


# ============================================================================
# 数据加载
# ============================================================================
def _hist_path() -> str:
    import os
    root = os.path.dirname(os.path.abspath(db.db_path()))
    return os.path.join(root, "hist.db").replace("\\", "/")


def _load_history(code: str) -> tuple[pd.DataFrame, dict]:
    """取单只股票的长历史，并把 hist.db 的复权基准对齐到 unified。

    ★为什么必须对齐
    ----------------
    hist.db 与 unified 都是前复权，但**复权锚点日不同**（一个同步于 9 月、
    一个同步于 10 月），同一根 K 线的 close 可能差几个百分点。直接 concat 会在
    拼接处留下一道假跳空，回测会把它当成一次真实暴跌统计进去。

    做法：取重叠期最后 20 个交易日的 close 比值中位数作为缩放因子，
    把 hist 的 O/H/L/C 整体乘上它（等比缩放 = 把锚点搬过去），
    同时记录残差离散度用于告警。成交量不缩放（前复权不调整量）。
    """
    out: dict = {"code": code, "src": [], "seam_note": None}
    hdf = pd.DataFrame()
    hp = _hist_path()
    try:
        conn = sqlite3.connect(f"file:{hp}?mode=ro", uri=True, timeout=30)
        conn.execute("PRAGMA query_only = ON")
        hdf = pd.read_sql_query(
            "SELECT date, open, high, low, close, volume, amount "
            "FROM daily_hist WHERE code=? ORDER BY date", conn, params=(str(code),))
        conn.close()
    except Exception as e:                     # noqa: BLE001
        out["seam_note"] = f"长历史库读取失败（{type(e).__name__}），仅用本地日线"
        hdf = pd.DataFrame()

    udf = pd.read_sql_query(
        "SELECT date, open, high, low, close, volume, amount "
        "FROM daily WHERE code=? ORDER BY date", db.reader(), params=(str(code),))

    for d in (hdf, udf):
        if not d.empty:
            d["date"] = pd.to_datetime(d["date"])

    if hdf.empty and udf.empty:
        return pd.DataFrame(), out

    if not hdf.empty and not udf.empty:
        hdf = hdf.drop_duplicates("date").set_index("date")
        udf = udf.drop_duplicates("date").set_index("date")
        common = hdf.index.intersection(udf.index)
        if len(common) >= 5:
            tail = common[-20:]
            ratio = (udf.loc[tail, "close"].astype(float)
                     / hdf.loc[tail, "close"].astype(float).replace(0, np.nan))
            ratio = ratio.replace([np.inf, -np.inf], np.nan).dropna()
            if len(ratio) >= 3:
                fac = float(ratio.median())
                spread = float((ratio / fac - 1).abs().max()) if fac else 0.0
                if fac > 0 and abs(fac - 1.0) > 0.005:
                    for col in ("open", "high", "low", "close"):
                        hdf[col] = hdf[col].astype(float) * fac
                    out["seam_note"] = (f"长历史复权锚点与本地库相差 {abs(fac-1)*100:.2f}%，"
                                        f"已整体缩放对齐（同一基准）")
                    out["src"].append("hist.db(已缩放)")
                else:
                    out["src"].append("hist.db")
                if spread > 0.02:
                    out["seam_note"] = ((out["seam_note"] or "")
                                        + f"；⚠ 重叠期比值离散度 {spread*100:.1f}%，"
                                          f"两库复权口径可能不一致，早期数据仅供参考")
        # hist 覆盖的日期用 hist，其余用 unified
        merged = pd.concat([hdf[hdf.index < udf.index.min()], udf]).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
    elif not hdf.empty:
        merged = hdf.sort_index()
        out["src"].append("hist.db")
    else:
        merged = udf.sort_index()
        out["src"].append("daily")

    # ★次新股会走到 else 分支（hist.db 没有它），此时 udf 的 index 是 RangeIndex、
    #   列里已经有 "date"；无条件 reset_index() 会造出第二列 date（0,1,2...），
    #   后面 df["date"] 拿到整个 DataFrame → `.tolist()` 报 AttributeError。
    #   实测 688411 / 688758 / 301252 全部中招。修法：有 date 列就只丢索引。
    if "date" in merged.columns:
        merged = merged.reset_index(drop=True)
    else:
        merged = merged.reset_index()
        if "index" in merged.columns:
            merged = merged.rename(columns={"index": "date"})
    # 兜底：任何情况下都不允许出现同名列
    merged = merged.loc[:, ~merged.columns.duplicated(keep="last")]
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col not in merged.columns:
            merged[col] = np.nan
        merged[col] = pd.to_numeric(merged[col], errors="coerce")
    merged = merged.dropna(subset=["close"])
    # 价格异常行（0 或负）直接剔除：hist.db 早期有少量脏数据
    merged = merged[(merged["close"] > 0) & (merged["high"] >= merged["low"])]
    merged = merged.reset_index(drop=True)
    out["bars"] = len(merged)
    out["first"], out["last"] = (
        (str(merged["date"].iloc[0])[:10], str(merged["date"].iloc[-1])[:10])
        if len(merged) else (None, None))
    return merged, out


def _arrays(df: pd.DataFrame):
    o = df["open"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    c = df["close"].astype(float).to_numpy()
    v = df["volume"].astype(float).to_numpy()
    # 开盘价缺失（部分源）用收盘价兜底，避免回测入场价 NaN 把事件全丢掉
    o = np.where(np.isfinite(o) & (o > 0), o, c)
    if "amount" in df.columns:
        amo = df["amount"].astype(float).to_numpy()
    else:
        amo = np.full(len(c), np.nan)
    return o, h, l, c, v, amo


def _meta_of(code: str) -> dict:
    try:
        row = db.reader().execute(
            "SELECT name, sector, exchange FROM meta WHERE code=?", (str(code),)).fetchone()
    except Exception:                          # noqa: BLE001
        row = None
    name = (row[0] if row else None) or code
    sector = (row[1] if row else None) or "未分类"
    exchange = (row[2] if row else None) or None
    return {"code": str(code), "name": name, "sector": sector, "exchange": exchange}


# ============================================================================
# 市场状态检测（牛 / 熊 / 震荡）
# ============================================================================
_REGIME_CACHE: Dict[str, object] = {}
_REGIME_LOCK = threading.Lock()
MIN_CROSS_SECTION = 500      # 当日至少这么多只股票才纳入等权指数


def _market_nav(days: int = 300):
    """全市场等权指数（近 days 个交易日）。

    ★只用主库（最近约 288 个交易日），不读 hist.db 的 1600 万行：
      判断「今天是什么市场」只需要最后一个点，MA250 要 250 根，288 根刚好够。
      复用 bollfilter._build_index() 能拿到 30 年序列，但首次要 30-60s，
      这里为了「打开页签就能看见状态」换成了轻量版。

    ★抽样 1/3：等权指数要的是「全市场平均日收益」，1800 只的抽样误差 <0.1%，
      但 SQL 返回行数从 180 万降到 60 万，首屏从 7.3s 降到 ~2s。
      抽样规则用 `CAST(code AS INTEGER) % 3`，稳定、与行业无关、每次一致。

    截尾 ±11%：主板涨跌停 10%，超过它只可能是复权接缝。
    """
    sig = _data_sig()
    with _REGIME_LOCK:
        c = _REGIME_CACHE.get("nav")
        if c and c["sig"] == sig:
            return c["dates"], c["nav"]
    try:
        conn = db.reader()
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily ORDER BY date DESC LIMIT ?",
            (int(days),)).fetchall()
        if len(rows) < 260:
            return [], np.array([])
        d0 = str(rows[-1][0])
        df = pd.read_sql_query(
            "SELECT code, date, close FROM daily WHERE date >= ? "
            "AND (CAST(code AS INTEGER) % 3) = 0 ORDER BY code, date",
            conn, params=(d0,))
    except Exception:                          # noqa: BLE001
        return [], np.array([])
    if df.empty or len(df) < 200000:
        # 抽样失败（比如代码不是纯数字）就退回全量
        try:
            df = pd.read_sql_query(
                "SELECT code, date, close FROM daily WHERE date >= ? ORDER BY code, date",
                db.reader(), params=(d0,))
        except Exception:                      # noqa: BLE001
            return [], np.array([])
    if df.empty:
        return [], np.array([])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df[df["close"] > 0]
    df["ret"] = df.groupby("code")["close"].pct_change()
    df = df[np.isfinite(df["ret"].to_numpy(dtype=float))]
    g = df.groupby("date")["ret"].agg(["mean", "count"])
    g = g[g["count"] >= MIN_CROSS_SECTION]
    if g.empty:
        return [], np.array([])
    r = np.clip(g["mean"].to_numpy(dtype=float), -0.11, 0.11)
    nav = np.cumprod(1.0 + r)
    dates = [str(x)[:10] for x in g.index.tolist()]
    with _REGIME_LOCK:
        _REGIME_CACHE["nav"] = {"sig": sig, "dates": dates, "nav": nav}
    return dates, nav


def _classify_regime_series(nav: np.ndarray) -> List[str]:
    """逐日判定三态（只用当日及之前的均线值，无未来函数）。

    ★判据只要求「离年线的位置」+「中期均线排列」，**不再要求 MA60 的斜率同号**。
      原因：跌了 15% 但最近 20 天横住，按老判据会被归成「震荡市」——这不符合直觉，
      市场上没人会把「低于年线 15% 且 MA60<MA120」叫震荡。
      （改判据后已重跑标定，结论方向不变，见 REGIMES 里的数字。）
    """
    n = len(nav)
    ma60, ma120, ma250 = _sma(nav, 60), _sma(nav, 120), _sma(nav, 250)
    out = ["unknown"] * n
    for i in range(n):
        if not (np.isfinite(ma60[i]) and np.isfinite(ma120[i]) and np.isfinite(ma250[i])):
            continue
        if ma250[i] <= 0:
            continue
        pos250 = nav[i] / ma250[i] - 1.0
        if pos250 > 0.03 and ma60[i] > ma120[i]:
            out[i] = "bull"
        elif pos250 < -0.03 and ma60[i] < ma120[i]:
            out[i] = "bear"
        else:
            out[i] = "range"
    return out


def detect_regime(asof: Optional[str] = None, days: int = 320) -> dict:
    """当前（或指定日）的市场状态。返回状态、判据数值、持续天数与标定结论。"""
    dates, nav = _market_nav(days)
    if len(nav) < 260:
        return {"key": "unknown", "label": "无法判定",
                "desc": "本地日线不足以算出 MA250（需至少 260 个交易日）",
                "asof": None, "auto": True, "primary": "balance",
                "advice": "数据不足，按震荡市处理", "calib": REGIME_CALIB_META}

    states = _classify_regime_series(nav)
    i = len(nav) - 1
    if asof and asof in dates:
        i = dates.index(asof)
    key = states[i]
    if key not in REGIMES:
        key = "range"

    # 该状态已持续多少个交易日
    j = i
    while j > 0 and states[j - 1] == key:
        j -= 1

    ma60, ma120, ma250 = _sma(nav, 60), _sma(nav, 120), _sma(nav, 250)
    pos250 = (nav[i] / ma250[i] - 1.0) * 100.0 if np.isfinite(ma250[i]) and ma250[i] > 0 else None
    slope60 = ((ma60[i] - ma60[i - 20]) / ma60[i - 20] * 100.0
               if i >= 20 and np.isfinite(ma60[i]) and np.isfinite(ma60[i - 20])
               and ma60[i - 20] > 0 else None)
    g = REGIMES[key]
    return {
        "key": key,
        "label": g["label"],
        "short": g["short"],
        "desc": g["desc"],
        "asof": dates[i],
        "auto": True,
        "nav": round(float(nav[i]), 4),
        "ma60": _r(ma60[i], 4), "ma120": _r(ma120[i], 4), "ma250": _r(ma250[i], 4),
        "pos250": _r(pos250, 2),
        "slope60": _r(slope60, 2),
        "since": dates[j],
        "days": i - j + 1,
        "primary": g["primary"],
        "advice": g["advice"],
        "verdict": g["verdict"],
        "coverage": {"first": dates[0], "last": dates[-1], "days": len(dates)},
        "calib": REGIME_CALIB_META,
    }


def _band_of(bands, v):
    if v is None or not math.isfinite(float(v)):
        return None
    x = float(v)
    for lo, hi, label, win, avg in bands:
        if lo <= x < hi:
            return {"band": label, "win": win, "avg": avg}
    return None


def regime_expect(regime_key: str, balance: float, rebound: float) -> dict:
    """把两个分数落到「当前市场状态下的历史分档」，给出胜率与均值。

    ★不是造第三个分数，而是**直接查标定表**——「你这个分数在当前市场里，
      历史上同档位的票后来 20 日是什么表现」。这样不改动原有两把尺子的语义。
    """
    g = REGIMES.get(regime_key)
    if not g:
        return {}
    return {
        "regime": regime_key,
        "label": g["label"],
        "primary": g["primary"],
        "advice": g["advice"],
        "verdict": g["verdict"],
        "balance": _band_of(g["balance_bands"], balance),
        "rebound": _band_of(g["rebound_bands"], rebound),
        "balance_ic": g["balance_ic"],
        "rebound_ic": g["rebound_ic"],
        "sample": g["sample"],
    }


# ============================================================================
# 回测
# ============================================================================
def _fwd_ret(o, c, i: int, horizon: int):
    """第 i 日收盘信号 → 第 i+1 日开盘买入 → 第 i+horizon 日收盘卖出。返回 % 或 None。"""
    # ★i<0 必须显式挡住：Python 负索引会静默取到序列尾部，
    #   把「找不到的日期」算成一个看似正常的收益（最隐蔽的一类错值）。
    if i is None or i < 0:
        return None
    j_in, j_out = i + 1, i + horizon
    if j_in >= len(c) or j_out >= len(c):
        return None
    entry = float(o[j_in])
    exit_ = float(c[j_out])
    if not (np.isfinite(entry) and np.isfinite(exit_)) or entry <= 0:
        return None
    return (exit_ / entry - 1.0) * 100.0


def _stat_block(rets: List[float], base: Optional[float] = None) -> dict:
    """一组收益的统计：样本、胜率、均值、中位、分位、均值置信区间。"""
    n = len(rets)
    if n == 0:
        return {"n": 0, "win": None, "avg": None, "med": None,
                "best": None, "worst": None, "std": None,
                "ci_low": None, "ci_high": None, "base": base, "alpha": None}
    a = np.asarray(rets, dtype=float)
    a = a[np.isfinite(a)]
    n = len(a)
    if n == 0:
        return {"n": 0, "win": None, "avg": None, "med": None,
                "best": None, "worst": None, "std": None,
                "ci_low": None, "ci_high": None, "base": base, "alpha": None}
    avg = float(a.mean())
    sd = float(a.std(ddof=1)) if n >= 2 else None
    se = (sd / math.sqrt(n)) if (sd is not None and n >= 2) else None
    # ★样本少时置信区间会极宽——这是**如实呈现**，不是 bug：
    #   小样本的均值几乎无法与 0 区分，用户必须看到这一点。
    return {
        "n": n,
        "win": round(float((a > 0).mean() * 100.0), 1),
        "avg": round(avg, 2),
        "med": round(float(np.median(a)), 2),
        "best": round(float(a.max()), 2),
        "worst": round(float(a.min()), 2),
        "std": _r(sd, 2),
        "ci_low": round(avg - 1.96 * se, 2) if se else None,
        "ci_high": round(avg + 1.96 * se, 2) if se else None,
        "base": _r(base, 2),
        "alpha": (round(avg - base, 2) if (base is not None
                                           and math.isfinite(base)) else None),
    }


def _bt_stock(dates, o, c, sig, ref_keys: set, cfg) -> dict:
    """个股长历史：找出与「当前信号集」至少有 MIN_SIGNALS 个交集的时点。

    ★两个口径同时算（这是本模块的核心诚实性设计）
    ------------------------------------------------
    参照股当前命中 7 个信号时，「交集 ≥2」会覆盖 40% 的交易日（茅台实测 741/1800），
    这时「相似形态」几乎等于「随便哪一天」，回测结果自然退化成「该股常态」。
    所以同时给出：
      · 宽松口径（用户要求）：交集 ≥2          → 样本大、可比性弱
      · 严格口径（高相似）：交集 ≥60%×|参照集|  → 样本小、但真的像
    两个口径并列展示，用户自己看「像」到底值不值钱。
    """
    m = len(c)
    horizon = int(cfg["HORIZON"])
    cooldown = int(cfg["COOLDOWN"])
    warm = int(cfg["WARMUP"])
    min_sig = int(cfg["MIN_SIGNALS"])
    ref_n = len(ref_keys)
    strict_n = max(min_sig, int(math.ceil(ref_n * 0.6)))

    # 逐日「命中了几个参照信号」
    inter_n = np.zeros(m, dtype=np.int16)
    for key in ref_keys:
        inter_n += sig[key].astype(np.int16)
    hit_n = _hit_count(sig, m)

    events, last_i = [], -10 ** 9
    strict_rets, last_s = [], -10 ** 9
    control: List[float] = []
    for i in range(warm, m):
        r = _fwd_ret(o, c, i, horizon)
        if r is None:
            continue
        k = int(inter_n[i])
        if k >= min_sig:
            if k >= strict_n and i - last_s > cooldown:
                last_s = i
                strict_rets.append(round(r, 2))
            if i - last_i <= cooldown:       # 冷却期内不重复计数
                continue
            last_i = i
            union = ref_n + int(hit_n[i]) - k
            events.append({
                "_i": i,                      # 内部索引，用于算多周期收益，出参前删除
                "date": str(dates[i])[:10],
                "n": k,
                "total": int(hit_n[i]),
                "sim": round(k / union, 3) if union > 0 else None,
                "keys": [x for x in ref_keys if bool(sig[x][i])],
                "ret": round(r, 2),
            })
        else:
            control.append(r)

    rets = [e["ret"] for e in events]
    base = float(np.mean(control)) if control else None
    st = _stat_block(rets, base=base)
    st["event_rate"] = round(len(events) / max(1, m - warm) * 100.0, 2)
    st["horizon"] = horizon
    st["n_total"] = len(events)
    st["control_n"] = len(control)
    st["base_type"] = "self"          # 基线 = 该股「没有相似信号」的日子（不是市场）
    st["ref_n"] = ref_n
    st["min_signals"] = min_sig
    st["strict_n"] = strict_n
    st["strict"] = _stat_block(strict_rets, base=base)

    # 多周期对比（只对事件日算）：1/3/5/10/20 日
    multi = {}
    for hz in (1, 3, 5, 10, 20):
        arr = [_fwd_ret(o, c, e["_i"], hz) for e in events]
        arr = [x for x in arr if x is not None]
        if len(arr) >= 5:
            multi[f"T+{hz}"] = {
                "n": len(arr),
                "win": round(float(np.mean([x > 0 for x in arr]) * 100.0), 1),
                "avg": round(float(np.mean(arr)), 2),
            }
    st["multi"] = multi

    # 明细（最近 N 条，倒序），并把内部索引剥掉
    keep = int(cfg.get("STOCK_MAX_EVENTS", 400))
    tail = events[-keep:][::-1]
    st["events"] = [{k: v for k, v in e.items() if k != "_i"} for e in tail]
    return st


def _score_buckets(parts, total, o, c, cfg) -> dict:
    """评分分档检验：历史每一天的分数 → 分档 → 未来 5 日收益。

    ★这是本模块最重要的诚实性检查。如果分档之间没有单调关系，
      说明「分数高」在这只股票上并不预示更好的短期表现，必须如实告诉用户。
    """
    m = len(c)
    horizon = int(cfg["HORIZON"])
    warm = int(cfg["WARMUP"])
    bands = [(80, 101, "80-100 强势"), (65, 80, "65-80 偏强"),
             (50, 65, "50-65 中性"), (35, 50, "35-50 偏弱"),
             (0, 35, "0-35 弱势")]
    buckets = {b[2]: [] for b in bands}
    for i in range(warm, m):
        r = _fwd_ret(o, c, i, horizon)
        if r is None:
            continue
        s = float(total[i])
        for lo, hi, name in bands:
            # ★区间左闭右开，但「0-35」要包含 0；用显式边界避免 35 分两张桶
            if lo <= s < hi or (hi == 101 and s >= 80):
                buckets[name].append(r)
                break
    out = []
    for lo, hi, name in bands:
        arr = buckets[name]
        if not arr:
            out.append({"label": name, "n": 0, "win": None, "avg": None})
            continue
        a = np.asarray(arr)
        out.append({
            "label": name, "n": len(a),
            "win": round(float((a > 0).mean() * 100.0), 1),
            "avg": round(float(a.mean()), 2),
        })
    # 单调性：从弱到强，均值应递增；用 Spearman 秩相关（无 scipy 时手算）
    usable = [(i, b) for i, b in enumerate(out) if b["n"] >= 30 and b["avg"] is not None]
    mono = None
    if len(usable) >= 3:
        xs = np.arange(len(usable), dtype=float)
        ys = np.array([b["avg"] for _, b in usable], dtype=float)
        # 桶顺序是「从强到弱」，所以期望 y 递减 → 相关系数应为负
        rx = pd.Series(xs).rank().to_numpy()
        ry = pd.Series(ys).rank().to_numpy()
        rho = float(np.corrcoef(rx, ry)[0, 1]) if rx.std() > 0 and ry.std() > 0 else 0.0
        mono = round(rho, 3)
    return {"buckets": out, "spearman": mono,
            "horizon": horizon,
            "verdict": _mono_verdict(mono, out)}


def _mono_verdict(rho, buckets) -> str:
    if rho is None:
        return "有效样本档位不足 3 档（每档 ≥30 天），无法判断评分是否有效"
    if rho <= -0.6:
        return "分档与未来收益同向（强档更好）——本股历史上评分有区分度"
    if rho >= 0.6:
        return "分档与未来收益反向（强档更差）——本股历史上高分反而跑输，务必谨慎"
    return "分档与未来收益关系不明确——本股历史上评分几乎没有区分度"


def _load_universe_codes(cfg, exchange=None, sectors=None) -> List[str]:
    """股票池：走 conditions 的同一口径（剔 ST / 次新），范围与板块由调用方指定。"""
    from . import conditions
    d = conditions.DEFAULTS
    codes, _ = conditions.load_universe(
        exchange, sectors,
        exclude_st=bool(d.get("EXCLUDE_ST", True)),
        new_stock_days=int(d.get("NEW_STOCK_DAYS", 365)))
    return [str(x) for x in codes]


def _bt_cross(code: str, name_map: dict, ref_keys: set, ref_score: float,
              cfg, progress_cb=None) -> dict:
    """横截面：最近 CROSS_WINDOW 个交易日内，全市场出现 ≥2 个相同信号的股票。

    ★基线必须「按日对齐」（第一版在这里踩过坑）
    ------------------------------------------------
    第一版用「最后一个可评估日入场」的全市场收益当基线，结果 2026-09~10 那波
    全市场急跌里，信号组 −5.11%、基线还是 +0.21%（因为最后 5 天正好反弹），
    直接把「大盘跌」算成了「信号无效」。
    正确做法：为**每个交易日**单独算全市场平均 5 日收益，事件的超额 =
    该事件收益 − 该事件信号日当天的市场平均。这样 alpha 才是横截面 alpha。
    """
    horizon = int(cfg["HORIZON"])
    win = int(cfg["CROSS_WINDOW"])
    min_bars = int(cfg["CROSS_MIN_BARS"])
    min_sig = int(cfg["MIN_SIGNALS"])

    codes = _load_universe_codes(cfg)
    if not codes:
        return {"ok": False, "note": "股票池为空（先同步数据）", "rows": [],
                "n": 0, "stat": _stat_block([])}
    ref_n = len(ref_keys)

    # 需要 120 根预热（MA120 / BOLL 分位）+ 观察窗 + 持有期
    need = max(min_bars, 120 + win + horizon + 5)
    bars_map = batchload.load_daily_map(window_days=max(need, 180), codes=codes)

    rows = []
    mkt: Dict[str, list] = {}          # date -> [sum, n]：全市场当日入场的 5 日收益
    scores_all: List[float] = []       # 全市场当日评分（用于算该股分位）
    excluded_incomplete = 0
    scanned = 0
    total = len(codes)
    for ci, cd in enumerate(codes):
        # ★进度回调放循环开头：池子里大量 skip 时尾部回调会永不触发
        if progress_cb and ((ci + 1) % 200 == 0 or ci + 1 == total):
            progress_cb(ci + 1, total, f"横截面扫描 {ci + 1}/{total}")
        df = bars_map.get(cd)
        if df is None or len(df) < min_bars:
            continue
        scanned += 1
        o, h, l, c, v, amo = _arrays(df)
        m = len(c)
        ind = _indicators(o, h, l, c, v, amo)
        sig = _signal_masks(ind, o, h, l, c, v)
        hit_n = _hit_count(sig, m)
        # 全市场评分（只为算分位，权重/口径与主流程完全一致）
        try:
            sc_now = float(_total_score(_score_parts(ind, sig, o, h, l, c, v, amo))[-1])
            if np.isfinite(sc_now):
                scores_all.append(sc_now)
        except Exception:                        # noqa: BLE001
            pass

        start = max(0, m - win)
        # ① 累积全市场当日基线（只用观察窗内的日期，控制成本）
        for i in range(start, m):
            r0 = _fwd_ret(o, c, i, horizon)
            if r0 is None:
                continue
            d = str(df["date"].iloc[i])[:10]
            cell = mkt.get(d)
            if cell is None:
                mkt[d] = [r0, 1]
            else:
                cell[0] += r0
                cell[1] += 1

        # ② 找该股最相似的一次信号
        best = None
        for i in range(start, m):
            hit = [k for k in ref_keys if bool(sig[k][i])]
            if len(hit) < min_sig:
                continue
            r = _fwd_ret(o, c, i, horizon)
            if r is None:
                excluded_incomplete += 1
                continue
            union = ref_n + int(hit_n[i]) - len(hit)
            sim = (len(hit) / union) if union > 0 else 0.0
            cand = {"i": i, "keys": hit, "ret": r, "sim": sim, "total": int(hit_n[i])}
            # 同一只股票多次命中：取「最相似」的一次（相似度相同则取更近的）
            if best is None or (cand["sim"], cand["i"]) > (best["sim"], best["i"]):
                best = cand
        if best is None:
            continue
        i = best["i"]
        chg = ((c[i] / c[i - 1] - 1.0) * 100.0) if i >= 1 and c[i - 1] > 0 else None
        rows.append({
            "代码": cd,
            "名称": name_map.get(cd) or cd,
            "信号日": str(df["date"].iloc[i])[:10],
            "共同信号数": len(best["keys"]),
            "相似度": round(best["sim"] * 100.0, 1),
            "共同信号": "、".join(SIGNALS[k]["label"] for k in best["keys"]),
            "signal_keys": best["keys"],
            "信号日涨幅(%)": _r(chg, 2),
            "信号日收盘": _r(c[i], 2),
            "后5日收益(%)": round(float(best["ret"]), 2),
        })

    # ③ 按日基线 → 逐事件超额
    mkt_avg = {d: s / n for d, (s, n) in mkt.items() if n >= 100}
    alphas, matched_base = [], []
    for r in rows:
        b = mkt_avg.get(r["信号日"])
        if b is None:
            r["同期市场(%)"] = None
            r["超额(%)"] = None
            continue
        r["同期市场(%)"] = round(b, 2)
        a = r["后5日收益(%)"] - b
        r["超额(%)"] = round(a, 2)
        alphas.append(a)
        matched_base.append(b)

    base = float(np.mean(matched_base)) if matched_base else None
    rets = [r["后5日收益(%)"] for r in rows]
    st = _stat_block(rets, base=base)
    st["horizon"] = horizon
    st["scanned"] = scanned
    st["excluded_incomplete"] = excluded_incomplete
    st["base_type"] = "market_daily"     # 基线 = 每个信号日当天的全市场平均（剔 beta）
    st["ref_n"] = ref_n
    st["mkt_days"] = len(mkt_avg)
    st["alpha_stat"] = _stat_block(alphas)

    # 高相似子集（Jaccard ≥60%）：样本更小，但才真正算「像」
    strict = [r for r in rows if (r.get("相似度") or 0) >= 60.0]
    st["strict"] = _stat_block([r["后5日收益(%)"] for r in strict], base=base)
    st["strict"]["alpha_stat"] = _stat_block(
        [r["超额(%)"] for r in strict if r.get("超额(%)") is not None])
    st["strict_k"] = 60

    rows.sort(key=lambda r: (-(r.get("相似度") or 0), -r["共同信号数"], -r["后5日收益(%)"]))
    max_rows = int(cfg.get("CROSS_MAX_ROWS", 300))
    # 观察窗的实际日期区间：取任一已加载股票的首尾（所有股票共用同一交易日历尾部）
    drange = None
    for cd in codes:
        d0 = bars_map.get(cd)
        if d0 is not None and len(d0) >= win + 1:
            drange = (f"{str(d0['date'].iloc[-win])[:10]} ~ "
                      f"{str(d0['date'].iloc[-1])[:10]}")
            break

    # 全市场分位：该股分数在当日全市场中排第几（越高越好）
    rank = None
    if scores_all:
        arr = np.asarray(scores_all, dtype=float)
        rank = {"of": int(arr.size),
                "pct": round(float((arr <= ref_score).mean() * 100.0), 1)}

    return {"ok": True, "rows": rows[:max_rows], "n": len(rows),
            "n_shown": min(len(rows), max_rows),
            "stat": st, "window": win, "date_range": drange,
            "score_rank": rank}


# ============================================================================
# 对外：范围内评分排行（Top N，可并列）
# ============================================================================
# ★缓存的是「全范围逐股分数」，不是「某一次的榜单」
# ----------------------------------------------------------------------------
# 一个范围扫 5200 只约 19s（每只都要算指标 + 两把尺子）。如果把 metric/top 也放进
# 缓存 key，用户切一下「均衡分榜 → 反抽分榜」就得再等 19s，等于白扫。
# 所以缓存的是 {code → 两个分数} 这张底表，排序与截断在内存里做（毫秒级）。
# 数据指纹变化（同步过数据）或范围变化才重扫。
_SCAN_CACHE: Dict[tuple, dict] = {}
_RANK_LOCK = threading.Lock()
RANK_TTL = 600          # 10 分钟；数据指纹变了会立即失效


def _data_sig() -> str:
    """数据指纹：最新交易日 + 行数。数据同步后自动让排行缓存失效。"""
    try:
        row = db.reader().execute("SELECT MAX(date), COUNT(*) FROM daily").fetchone()
        return f"{row[0]}|{row[1]}"
    except Exception:                          # noqa: BLE001
        return "?"


def _meta_map() -> Dict[str, dict]:
    """一次取回 code → {name, sector, exchange}，避免逐只查 meta。"""
    out: Dict[str, dict] = {}
    try:
        for r in db.reader().execute(
                "SELECT code, name, sector, exchange FROM meta").fetchall():
            out[str(r[0])] = {"name": r[1], "sector": r[2], "exchange": r[3]}
    except Exception:                          # noqa: BLE001
        pass
    return out


def _scan_scores(cfg, exchange, sectors, min_bars: int, progress_cb=None,
                 force: bool = False):
    """扫全范围，返回逐股两把尺子的分数。带缓存。

    返回 (rows, scanned, skipped, pool, cached)
    """
    import time as _t
    key = (exchange or "", tuple(sorted(sectors or [])), int(min_bars), _data_sig())
    if not force:
        with _RANK_LOCK:
            c = _SCAN_CACHE.get(key)
            if c and (_t.time() - c["ts"] < RANK_TTL):
                return c["rows"], c["scanned"], c["skipped"], c["pool"], True

    codes = _load_universe_codes(cfg, exchange, sectors)
    if not codes:
        return [], 0, 0, 0, False

    bars_map = batchload.load_daily_map(
        window_days=max(min_bars + 20, 200), codes=codes)
    mmap = _meta_map()
    # ★基本面一次查完：snapshot 5554 行 + 最新股本（一次 JOIN）。
    #   这两张表都很小，全量读比逐只查快几十倍。
    from . import ta_ctx
    snap = ta_ctx.snapshot_map([])
    shares = ta_ctx.latest_shares(codes)

    rows, scanned, skipped = [], 0, 0
    total = len(codes)
    for ci, cd in enumerate(codes):
        # ★进度回调放循环开头：数据不足的股票会 continue，放尾部就永远不报进度
        if progress_cb and ((ci + 1) % 200 == 0 or ci + 1 == total):
            progress_cb(ci + 1, total, f"评分扫描 {ci + 1}/{total}")
        df = bars_map.get(cd)
        if df is None or len(df) < min_bars:
            skipped += 1
            continue
        scanned += 1
        try:
            o, h, l, c, v, amo = _arrays(df)
            ind = _indicators(o, h, l, c, v, amo)
            sig = _signal_masks(ind, o, h, l, c, v)
            parts = _score_parts(ind, sig, o, h, l, c, v, amo)
            bal = float(_total_score(parts)[-1])
            reb = float(_rebound_parts(ind, o, h, l, c, v)["score"][-1])
        except Exception:                      # noqa: BLE001
            skipped += 1
            continue
        if not (math.isfinite(bal) and math.isfinite(reb)):
            skipped += 1
            continue

        last = len(c) - 1
        chg = ((c[last] / c[last - 1] - 1.0) * 100.0
               if last >= 1 and c[last - 1] > 0 else None)
        hit = [k for k in SIGNAL_KEYS if bool(sig[k][last])]
        meta = mmap.get(cd) or {}
        # 市值 + 基本面：熊市排序要用（用户要求「熊市稍微提高基本面权重」）
        mcap = ta_ctx.market_cap_yi(float(c[last]), shares.get(cd))
        sn = snap.get(cd) or {}
        fs = ta_ctx.fundamental_score(sn.get("pe_pct"), sn.get("pb_pct"),
                                      sn.get("div_yield"), mcap)
        rows.append({
            "code": cd,
            "name": meta.get("name") or cd,
            "sector": meta.get("sector") or "未分类",
            "exchange": meta.get("exchange"),
            "date": str(df["date"].iloc[last])[:10],
            "price": _r(c[last], 2),
            "change": _r(chg, 2),
            "balance": round(bal, 1),
            "rebound": round(reb, 1),
            "signals": len(hit),
            "signal_labels": [SIGNALS[k]["label"] for k in hit][:6],
            "mcap_yi": mcap,
            "fund": fs.get("score"),
            "pe_pct": _r(sn.get("pe_pct"), 1),
            "pb_pct": _r(sn.get("pb_pct"), 1),
            "div_yield": _r(sn.get("div_yield"), 2),
        })

    with _RANK_LOCK:
        _SCAN_CACHE[key] = {"ts": _t.time(), "rows": rows, "scanned": scanned,
                            "skipped": skipped, "pool": total}
        # 最多留 4 个范围组合，避免长期运行吃内存
        if len(_SCAN_CACHE) > 4:
            for k in sorted(_SCAN_CACHE, key=lambda x: _SCAN_CACHE[x]["ts"])[:-4]:
                _SCAN_CACHE.pop(k, None)
    return rows, scanned, skipped, total, False


_CTX_CACHE: Dict[tuple, dict] = {}
_CTX_TTL = 600


def _attach_context(rows: List[dict], regime_key: str, with_intraday: bool = True,
                    progress_cb=None) -> None:
    """给入选项补上下文指标：压制率 / 风险系数 / 市值（就地写入 rows）。

    ★为什么只对入选项算：全市场算一次压制率约 5000×0.05s = 250 秒，
      而用户真正要看的就是榜上那几只。排名本身不依赖压制率。
    ★分时（联网）用线程池并发，每个交易所的指数分时只拉一次。
    """
    import concurrent.futures as _cf
    from . import ta_ctx

    idx_cache: Dict[str, Optional[list]] = {}

    def _one(r):
        code = str(r["code"])
        ex = str(r.get("exchange") or ta_ctx.exchange_of(code)).upper()
        # ★缓存：切榜单类型时同一批股票会重算一遍，分时（联网）最贵。
        #   键里带日期，盘中跨日会自然失效；TTL 10 分钟兜底。
        ck = (code, str(r.get("date") or ""), regime_key)
        with _RANK_LOCK:
            c0 = _CTX_CACHE.get(ck)
            if c0 and (time.time() - c0["ts"] < _CTX_TTL):
                return code, c0["val"]
        out = {}
        try:
            df, _info = _load_history(code)
            # 30 根是 risk_coef 的最低要求（25）加一点余量；次新股也能算
            if df is None or len(df) < 30:
                return code, out
            dates = [str(x)[:10] for x in df["date"].tolist()]
            o, h, l, c, v, amo = _arrays(df)
            ind = _indicators(o, h, l, c, v, amo)
            out["suppress_local"] = ta_ctx.suppression_local(code, ex, dates, c)
            out["risk"] = ta_ctx.risk_coef(c, h, l, amo, ind["ma20"], regime=regime_key)
        except Exception as e:                 # noqa: BLE001
            out["err"] = f"{type(e).__name__}: {str(e)[:160]}"
            return code, out
        with _RANK_LOCK:
            _CTX_CACHE[ck] = {"ts": time.time(), "val": out}
            if len(_CTX_CACHE) > 600:
                for k in sorted(_CTX_CACHE, key=lambda x: _CTX_CACHE[x]["ts"])[:-600]:
                    _CTX_CACHE.pop(k, None)
        return code, out

    with _cf.ThreadPoolExecutor(max_workers=6) as pool:
        local = dict(pool.map(_one, rows))

    # 分时：每个交易所拉一次指数分时，再并发拉个股
    intra = {}
    if with_intraday:
        for ex in {str(r.get("exchange") or ta_ctx.exchange_of(r["code"])).upper()
                   for r in rows}:
            if ex not in idx_cache:
                idx_cache[ex] = ta_ctx.intraday_index_series(ex)

        def _one_i(r):
            code = str(r["code"])
            ex = str(r.get("exchange") or ta_ctx.exchange_of(code)).upper()
            try:
                return code, ta_ctx.intraday_suppression(code, ex, idx_cache.get(ex))
            except Exception:                  # noqa: BLE001
                return code, {"ok": False, "score": None, "err": "分时失败"}

        with _cf.ThreadPoolExecutor(max_workers=12) as pool:
            intra = dict(pool.map(_one_i, rows))

    for r in rows:
        code = str(r["code"])
        lo = local.get(code) or {}
        sup = ta_ctx.merge_suppression(lo.get("suppress_local") or {},
                                       intra.get(code))
        r["suppress"] = sup.get("score") if sup.get("ok") else None
        r["suppress_parts"] = sup.get("parts") if sup.get("ok") else None
        r["suppress_weights"] = sup.get("weights") if sup.get("ok") else None
        if lo.get("err"):
            r["ctx_err"] = lo["err"]
        rk = lo.get("risk") or {}
        r["risk"] = rk.get("score")
        r["risk_items"] = rk.get("items") or []
        r["risk_detail"] = rk.get("detail")
    if progress_cb:
        progress_cb(1, 1, "上下文指标完成")


def rank_top(cfg=None, exchange=None, sectors=None, metric="auto",
             top: int = 10, progress_cb=None, use_cache: bool = True,
             regime: str = "auto") -> dict:
    """扫描指定范围（交易所 + 板块），按评分取前 N 名，同分并列。

    metric: "balance" 均衡分 / "rebound" 反抽分 / "auto" 由市场状态决定（推荐）
    regime: "auto" 自动识别，或显式 "bull"/"bear"/"range"

    ★「按市场状态筛选」的实现是**换排序键**，不是把两个分加权成一个黑箱：
      熊市 → 主用反抽分（均衡分反向，高分票会补跌，榜上单独标红警示）
      牛市/震荡 → 主用均衡分
      这样每一步都能解释，也不会破坏「两把尺子不混排」。

    「可并列」有两层含义，都实现：
      ① 同分同名次（1, 2, 2, 4...）
      ② 第 N 名如有同分，全部纳入，所以返回条数可能 > N
    """
    import time as _t
    cfg = {**DEFAULTS, **(cfg or {})}
    top = max(1, min(100, int(top or 10)))
    min_bars = int(cfg.get("RANK_MIN_BARS", 180))

    try:
        auto_rg = detect_regime()
    except Exception:                          # noqa: BLE001
        auto_rg = {"key": "range", "label": "震荡市"}
    use_key = str(regime) if str(regime) in REGIME_KEYS else auto_rg.get("key", "range")
    if use_key not in REGIMES:
        use_key = "range"
    rginfo = REGIMES[use_key]

    if metric in ("balance", "rebound"):
        field, auto_pick = metric, False
    else:
        field, auto_pick = rginfo["primary"], True

    t0 = _t.time()
    scanned_rows, scanned, skipped, pool, cached = _scan_scores(
        cfg, exchange, sectors, min_bars, progress_cb, force=not use_cache)

    if not scanned_rows:
        return {"ok": False, "metric": field, "rows": [], "n": 0,
                "scanned": scanned, "skipped": skipped, "pool": pool,
                "note": ("股票池为空（先在顶栏选范围，或先同步数据）"
                         if not pool else
                         f"范围内没有足够历史的股票（需 ≥{min_bars} 根 K 线）")}

    # 同分时用代码做稳定的次级排序，保证多次点击顺序一致
    # ---- 排序键：熊市「反抽 85% + 基本面 15%」，其余按单把尺子 ----
    use_blend = (use_key == "bear" and field == "rebound")

    def _kval(r):
        if use_blend:
            f = r.get("fund")
            if f is None:
                return float(r["rebound"])      # 没基本面数据就退回纯反抽，不惩罚
            # ★权重按标定下调：熊市里估值分位 IC 仅 −0.035/+0.031、基本面无区分度，
            #   所以只给 8%（第一版拍脑袋写的 15%，标定后改掉）
            return float(r["rebound"]) * 0.92 + float(f) * 0.08
        return float(r[field])

    for r in scanned_rows:
        r["sort_val"] = round(_kval(r), 2)

    rows = sorted(scanned_rows, key=lambda r: (-r["sort_val"], r["code"]))
    if len(rows) >= top:
        cutoff = rows[top - 1]["sort_val"]
        kept = [r for r in rows if r["sort_val"] >= cutoff - 1e-9]
    else:
        cutoff = rows[-1]["sort_val"]
        kept = rows

    # 竞赛排名：同分同名次（1, 2, 2, 4...）
    prev, rank = None, 0
    for i, r in enumerate(kept):
        if prev is None or abs(r["sort_val"] - prev) > 1e-9:
            rank = i + 1
            prev = r["sort_val"]
        r["rank"] = rank
        r["grade"] = _grade(r["balance"])
        r["rebound_grade"] = _rebound_grade(r["rebound"])
        r["type"] = _pattern_type(r["balance"], r["rebound"])
        # ★熊市里均衡分高的票会补跌（IC −0.141，20/20 年成立）——
        #   榜上不隐藏它，但必须标出来，否则用户会把「技术很强」当成优点。
        r["warn"] = ("bear_high_balance"
                     if use_key == "bear" and r["balance"] >= 65 else None)

    # ---- 只对入选项补上下文（压制率/风险），并拷贝一份避免污染缓存 ----
    kept = [dict(r) for r in kept]
    try:
        _attach_context(kept, use_key)
    except Exception as e:                     # noqa: BLE001
        for r in kept:
            r.setdefault("suppress", None)
            r.setdefault("risk", None)
            r.setdefault("risk_items", [f"上下文计算失败:{type(e).__name__}"])

    ties = max(0, len(kept) - top)
    field_label = "均衡分" if field == "balance" else "反抽分"
    if use_blend:
        field_label = "熊市综合分（反抽 92% + 基本面 8%）"
    low_sig = sum(1 for r in kept if r["signals"] < int(cfg["MIN_SIGNALS"]))
    note = (f"{rginfo['label']}口径 · 按{field_label}排序"
            f"{'（自动匹配市场状态）' if auto_pick else '（手动指定）'}，"
            f"门槛 {round(cutoff, 1)} 分"
            + (f"；第 {top} 名同分 {ties + 1} 只，已全部列出，共 {len(kept)} 只"
               if ties > 0 else "")
            + f"。扫描 {scanned} 只（{skipped} 只因历史不足 {min_bars} 根跳过）。")
    if low_sig:
        note += (f" 其中 {low_sig} 只命中信号 <{int(cfg['MIN_SIGNALS'])} 个，"
                 f"点进去会提示「不可回测」——评分本身有效，只是没有足够信号做形态对比。")
    if use_key == "bear":
        nw = sum(1 for r in kept if r["warn"])
        if nw:
            note += f" 另有 {nw} 只均衡分 ≥65，熊市里这类票历史上有补跌倾向，已标红。"

    return {
        "ok": True,
        "metric": field,
        "metric_label": field_label,
        "auto_pick": auto_pick,
        "blend": ({"used": True, "rebound_w": 0.92, "fund_w": 0.08,
                   "reason": "熊市里均衡分反向（IC −0.124），主用反抽分。"
                             "基本面只给 8%：标定显示熊市里 PE 分位 IC +0.031、PB −0.056，"
                             "几乎没有区分度（`scripts/_ta_fund_calib.py`）"}
                  if use_blend else None),
        "regime": {
            "key": use_key,
            "label": rginfo["label"],
            "short": rginfo["short"],
            "primary": rginfo["primary"],
            "advice": rginfo["advice"],
            "verdict": rginfo["verdict"],
            "balance_ic": rginfo["balance_ic"],
            "rebound_ic": rginfo["rebound_ic"],
            "auto": str(regime) not in REGIME_KEYS,
            "auto_key": auto_rg.get("key"),
            "auto_label": auto_rg.get("label"),
        },
        "top": top,
        "cutoff": round(cutoff, 1),
        "n": len(kept),
        "ties": ties,
        "scanned": scanned,
        "skipped": skipped,
        "pool": pool,
        "asof": rows[0]["date"] if rows else None,
        "rows": kept,
        "elapsed": round(_t.time() - t0, 1),
        "cached": cached,
        "note": note,
        "disclosure": (
            "两个榜分开排、不混排：均衡分高 = 技术面整体偏多（但含追高风险，"
            "全市场秩相关 −0.038）；反抽分高 = 超跌后的第一波反抽（大牛市里会跑输）。"
            "「按市场状态」只切换用哪把尺子排序，不把两个分加权成一个黑箱。"
            "榜上分数是「当前时点的横截面排序」，不是收益预测。"
        ),
    }


# ============================================================================
# 对外：元信息
# ============================================================================
def meta_info() -> dict:
    return {
        "defaults": dict(DEFAULTS),
        "weights": {k: dict(v) for k, v in WEIGHTS.items()},
        "regimes": {k: {
            "label": v["label"], "short": v["short"], "desc": v["desc"],
            "primary": v["primary"], "advice": v["advice"], "verdict": v["verdict"],
            "balance_ic": v["balance_ic"], "rebound_ic": v["rebound_ic"],
            "sample": v["sample"],
            "balance_bands": [{"band": b[2], "win": b[3], "avg": b[4]}
                              for b in v["balance_bands"]],
            "rebound_bands": [{"band": b[2], "win": b[3], "avg": b[4]}
                              for b in v["rebound_bands"]],
        } for k, v in REGIMES.items()},
        "regime_calib": dict(REGIME_CALIB_META),
        "rebound_dims": {k: dict(v) for k, v in REBOUND_DIMS.items()},
        "rebound_gate": dict(REBOUND_GATE),
        "rebound_types": [{"balance_min": a, "rebound_min": b, "key": k,
                           "label": l, "desc": d} for a, b, k, l, d in REBOUND_TYPES],
        "signals": [{"key": k, "label": v["label"], "desc": v["desc"],
                     "strong": bool(v["strong"])} for k, v in SIGNALS.items()],
        "grades": [{"min": g[0], "label": g[1], "key": g[2], "desc": g[3]}
                   for g in GRADES],
        "disclosure": (
            "分数是「技术形态健康度」，不是收益率预测。"
            "信号回测为事件研究，不含手续费/滑点/涨跌停无法成交的约束。"
        ),
    }


# ============================================================================
# 对外：主流程
# ============================================================================
def run(code: str, cfg: dict | None = None, progress_cb=None,
        regime: str = "auto") -> dict:
    """单只股票的技术分析。

    regime: "auto" 自动识别当前市场状态；或显式指定 "bull"/"bear"/"range"。
            ★手动指定只影响「用哪张标定表解释分数」，**不会改变分数本身**——
              分数是客观的技术形态计算，市场状态只决定「这个分数在当下意味着什么」。
    """
    cfg = {**DEFAULTS, **(cfg or {})}
    code = str(code or "").strip()
    if not code:
        return {"ok": False, "reason": "请填写股票代码", "code": code}

    def _p(frac, msg):
        if progress_cb:
            progress_cb(int(frac * 100), 100, msg)

    _p(2, "读取历史行情")
    df, load_info = _load_history(code)
    if df is None or df.empty:
        return {"ok": False, "code": code,
                "reason": f"本地没有 {code} 的日线数据（请先在「数据同步」里补齐）"}

    o, h, l, c, v, amo = _arrays(df)
    dates = [str(x)[:10] for x in df["date"].tolist()]
    m = len(c)
    meta = _meta_of(code)

    if m < int(cfg["WARMUP"]) + 5:
        return {"ok": False, "code": code, "name": meta["name"],
                "reason": f"历史数据只有 {m} 根 K 线，不足 {int(cfg['WARMUP'])+5} 根，无法评分"}

    _p(10, "计算技术指标")
    ind = _indicators(o, h, l, c, v, amo)
    sig = _signal_masks(ind, o, h, l, c, v)
    parts = _score_parts(ind, sig, o, h, l, c, v, amo)
    total = _total_score(parts)

    # ---- 当前信号：状态型信号看最后一日是否成立；并回溯它已持续多少天 ----
    last = m - 1
    cur_keys, cur_info, cur_since = [], {}, {}
    for key in SIGNAL_KEYS:
        arr = sig[key]
        if not bool(arr[last]):
            continue
        j = last
        # 往前找该状态连续成立的起点（最多回看 250 根，避免超长状态把循环拖慢）
        floor = max(0, last - 250)
        while j > floor and bool(arr[j - 1]):
            j -= 1
        cur_keys.append(key)
        cur_info[key] = last
        cur_since[key] = j

    score_now = float(total[last])
    grade = _grade(score_now)
    dims = []
    for k, w in WEIGHTS.items():
        raw = float(parts[k][last])
        raw = max(0.0, min(w["max"], raw if math.isfinite(raw) else 0.0))
        dims.append({
            "key": k, "label": w["label"], "desc": w["desc"],
            "score": round(raw, 1), "max": w["max"],
            "pct": round(raw / w["max"] * 100.0, 1),
        })

    # ---- 第二把尺子：反抽分（与均衡分并列，不合成） ----
    rb_all = _rebound_parts(ind, o, h, l, c, v)
    reb_now = float(rb_all["score"][last]) if math.isfinite(rb_all["score"][last]) else 0.0
    rb_grade = _rebound_grade(reb_now)
    rtype = _pattern_type(score_now, reb_now)
    rb_dims = []
    for k, w in REBOUND_DIMS.items():
        raw = float(rb_all[k][last])
        raw = max(0.0, min(float(w["max"]), raw if math.isfinite(raw) else 0.0))
        rb_dims.append({
            "key": k, "label": w["label"], "desc": w["desc"],
            "score": round(raw, 1), "max": w["max"],
            "pct": round(raw / w["max"] * 100.0, 1),
        })
    gate_now = float(rb_all["depth"][last]) if math.isfinite(rb_all["depth"][last]) else 0.0
    rebound = {
        "score": round(reb_now, 1),
        "grade": rb_grade,
        "type": rtype,
        "dims": rb_dims,
        "gate": {"label": REBOUND_GATE["label"], "desc": REBOUND_GATE["desc"],
                 "value": round(max(0.0, min(1.0, gate_now)) * 100.0, 1)},
        # 标定基准（全市场长历史 139 万个时点，见 scripts/_ta_rebound_calib.py）
        "calib": {
            "sample": 1389649,
            "horizon": 20,
            "bands": [
                {"label": "反抽 <30", "win": 49.8, "avg": 1.31},
                {"label": "反抽 30-50", "win": 56.2, "avg": 3.04},
                {"label": "反抽 ≥50", "win": 72.3, "avg": 10.40},
            ],
            "note": "「均衡分<50」子样本内的分组对比；反抽分越高，20 日胜率与均值越高。",
            "year_pos": "29/35",
        },
    }

    signal_rows = []
    for key in SIGNAL_KEYS:
        d = SIGNALS[key]
        on = key in cur_keys
        j = cur_since.get(key)
        signal_rows.append({
            "key": key, "label": d["label"], "desc": d["desc"],
            "group": d.get("group", ""),
            "strong": bool(d["strong"]), "on": bool(on),
            "since": dates[j] if j is not None else None,
            "days": (last - j + 1) if j is not None else None,
        })

    price = float(c[last])
    chg = ((price / c[last - 1] - 1.0) * 100.0) if last >= 1 and c[last - 1] > 0 else None
    out = {
        "ok": True,
        "code": code, "name": meta["name"], "sector": meta["sector"],
        "asof": dates[last], "price": _r(price, 2), "change": _r(chg, 2),
        "bars": m, "first_date": dates[0],
        "source": " + ".join(load_info.get("src") or ["daily"]),
        "seam_note": load_info.get("seam_note"),
        "score": round(score_now, 1),
        "grade": grade,
        "dims": dims,
        "rebound": rebound,
        "signals": signal_rows,
        "signal_keys": cur_keys,
        "signal_count": len(cur_keys),
        "weights": {k: v["max"] for k, v in WEIGHTS.items()},
    }

    # ---- 市场背景：自动识别 + 手动覆盖 ----
    # ★手动指定只换「解释这张分数的标定表」，不改分数本身。
    #   分数是客观形态计算；市场状态决定「这个分数当下意味着什么」。
    try:
        auto_rg = detect_regime()
    except Exception as e:                     # noqa: BLE001
        auto_rg = {"key": "range", "label": "震荡市", "short": "震",
                   "desc": f"状态识别失败（{type(e).__name__}），按震荡处理",
                   "asof": None, "auto": True, "primary": "balance",
                   "advice": "按震荡市处理", "verdict": "", "calib": REGIME_CALIB_META}
    manual = str(regime or "auto") in REGIME_KEYS
    if manual:
        gsel = REGIMES[str(regime)]
        use_rg = dict(auto_rg)
        use_rg.update({
            "key": str(regime), "label": gsel["label"], "short": gsel["short"],
            "desc": gsel["desc"], "auto": False,
            "primary": gsel["primary"], "advice": gsel["advice"],
            "verdict": gsel["verdict"],
        })
    else:
        use_rg = auto_rg

    out["market"] = {
        "now": use_rg,
        "auto_key": auto_rg.get("key"),
        "auto_label": auto_rg.get("label"),
        "manual": manual,
        "expect": regime_expect(use_rg["key"], score_now, reb_now),
    }

    # ---- 横向上下文：市值 / 风险系数 / 受压制率 / 基本面 ----
    # ★压制率：日线+周线（本地，快）+ 分时（联网，约 0.5s）。单只分析可以承受；
    #   全市场排行只在入选的 Top N 上算（见 _attach_context）。
    try:
        from . import ta_ctx
        cex = str(meta.get("exchange") or ta_ctx.exchange_of(code)).upper()
        sup_l = ta_ctx.suppression_local(code, cex, dates, c)
        sup_i = None
        if cfg.get("DO_INTRADAY", True):
            sup_i = ta_ctx.intraday_suppression(code, cex)
        sup = ta_ctx.merge_suppression(sup_l, sup_i)
        rk = ta_ctx.risk_coef(c, h, l, amo, ind["ma20"], regime=use_rg["key"])
        sh = ta_ctx.latest_shares([code]).get(code)
        mcap = ta_ctx.market_cap_yi(price, sh)
        sn = (ta_ctx.snapshot_map([code]) or {}).get(code) or {}
        fund = ta_ctx.fundamental_score(sn.get("pe_pct"), sn.get("pb_pct"),
                                        sn.get("div_yield"), mcap)
        out["ctx"] = {
            "mcap_yi": mcap,
            "risk": rk.get("score"),
            "risk_items": rk.get("items") or [],
            "risk_detail": rk.get("detail"),
            "suppress": sup.get("score") if sup.get("ok") else None,
            "suppress_parts": sup.get("parts") if sup.get("ok") else None,
            "suppress_weights": sup.get("weights") if sup.get("ok") else None,
            "suppress_err": sup.get("err"),
            "fund": fund.get("score"),
            "fund_detail": {k: fund.get(k) for k in
                            ("pe_pct", "pb_pct", "div_yield", "mcap_yi", "covered")},
            "index_name": sup.get("parts", {}).get("index") if sup.get("ok") else None,
        }
    except Exception as e:                     # noqa: BLE001
        out["ctx"] = {"mcap_yi": None, "risk": None, "suppress": None,
                      "fund": None, "err": f"{type(e).__name__}: {e}"}

    # ---- 门槛：不可回测 ----
    min_sig = int(cfg["MIN_SIGNALS"])
    if len(cur_keys) < min_sig:
        out["ok"] = False
        out["selectable"] = False
        out["reason"] = (
            f"当前没有任何技术信号命中，不可回测" if len(cur_keys) == 0 else
            f"当前只命中 1 个技术信号（{SIGNALS[cur_keys[0]]['label']}），"
            f"少于 {min_sig} 个，不构成「相似技术形态」")
        out["hint"] = (f"回测需要至少 {min_sig} 个同时成立的信号才能定义出可比较的技术形态。"
                       f"单信号时样本几乎等于「该指标全历史样本」，失去比较意义。")
        # ★信号不足 ≠ 没机会：弱趋势强反抽的时点，信号数天然只有 0~2 个
        #   （趋势类信号全灭，只剩量能/力度）。所以这里再给一次「反抽形态」的判断，
        #   避免把「算术平均漏掉的机会」又用信号门槛漏第二次。
        if reb_now >= 30.0:
            out["rebound_hint"] = (
                f"但检测到弱趋势反抽形态：反抽分 {reb_now:.1f}/100"
                f"（{rb_grade['label']}）。这类时点趋势类信号天然稀少，"
                f"均衡分与信号数都会低估它，可单独参考反抽分。")
        else:
            out["rebound_hint"] = (
                f"反抽分 {reb_now:.1f}/100（{rb_grade['label']}），也没有超跌反抽迹象。")
        # 仍然返回评分与信号明细，让用户看清差在哪
        out["bt_stock"] = None
        out["bt_cross"] = None
        out["score_bt"] = None
        return out

    out["selectable"] = True
    ref_keys = set(cur_keys)

    # ---- 个股历史回测 ----
    _p(30, "个股历史回测")
    bt = _bt_stock(dates, o, c, sig, ref_keys, cfg)
    bt["ref_keys"] = sorted(ref_keys)
    bt["ref_labels"] = [SIGNALS[k]["label"] for k in sorted(ref_keys)]
    bt["uniq"] = bt.get("uniq")
    out["bt_stock"] = bt

    # ---- 评分分档检验 ----
    if cfg.get("DO_SCORE_BT", True):
        _p(45, "评分分档检验")
        try:
            out["score_bt"] = _score_buckets(parts, total, o, c, cfg)
        except Exception as e:                  # noqa: BLE001
            out["score_bt"] = {"buckets": [], "spearman": None,
                               "verdict": f"分档检验失败：{type(e).__name__}"}
    else:
        out["score_bt"] = None

    # ---- 横截面回测 ----
    if cfg.get("DO_CROSS", True):
        _p(55, "横截面扫描（全市场）")
        try:
            name_map = {}
            try:
                conn = db.reader()
                for r in conn.execute("SELECT code, name FROM meta").fetchall():
                    name_map[str(r[0])] = r[1]
            except Exception:                   # noqa: BLE001
                pass
            cr = _bt_cross(code, name_map, ref_keys, score_now, cfg,
                           progress_cb=lambda a, b, msg: _p(
                               55 + int(a / max(1, b) * 40), msg))
            cr["ref_keys"] = sorted(ref_keys)
            cr["ref_labels"] = [SIGNALS[k]["label"] for k in sorted(ref_keys)]
            out["bt_cross"] = cr
        except Exception as e:                  # noqa: BLE001
            out["bt_cross"] = {"ok": False, "rows": [], "n": 0,
                               "stat": _stat_block([]),
                               "note": f"横截面回测失败：{type(e).__name__}: {e}"}
    else:
        out["bt_cross"] = None

    _p(100, "完成")

    # ---- 结论性提示（不是推荐）----
    notes = []
    mk = out.get("market") or {}
    rgk = (mk.get("now") or {}).get("key")
    rg_manual = mk.get("manual")
    rg_tag = "（手动指定）" if rg_manual else "（自动识别）"
    if rgk == "bear":
        notes.append(
            f"当前市场状态：熊市{rg_tag}。**均衡分在熊市里是反向的**"
            f"（IC −0.124；0-35 档 20 日 +3.22%、胜率 55.8%，"
            f"80-100 档只有 +0.16%、胜率 44.3%，20/20 年成立）——"
            f"技术面越强的票越容易补跌，这时候应看反抽分。")
    elif rgk == "bull":
        notes.append(
            f"当前市场状态：牛市{rg_tag}。均衡分整体 IC −0.017，但分档呈 U 型："
            f"0-35 档 +1.77%、80-100 档 +2.05%，中间 35-50 档最差只有 +0.93%。"
            f"「取高分」仍可用，但别拿中间档当选股依据。")
    elif rgk == "range":
        notes.append(
            f"当前市场状态：震荡市{rg_tag}。均衡分也是弱反向（IC −0.028），"
            f"0-35 档 +1.16% 反而最好；但逐年只有 18/27 年成立（67%），**不稳定**，"
            f"别当成熊市那种铁律。反抽分略优（30-50 档 +1.67%），两把尺子都别下重注。")
    if rtype["key"] == "rebound":
        notes.append(
            f"形态类型为「{rtype['label']}」：均衡分 {score_now:.1f} 偏低，"
            f"但反抽分 {reb_now:.1f} 已达门槛。**算术平均会低估这一类机会**——"
            f"超跌反抽时趋势类信号天然全灭，正是 6 维加权最吃亏的场景。")
    elif rtype["key"] == "both":
        notes.append(f"形态类型为「{rtype['label']}」：两把尺子同时亮。")
    if bt["n"] < 10:
        notes.append(f"个股历史只找到 {bt['n']} 个相似事件，样本太小，"
                     f"胜率与均值都不稳定（看置信区间）。")
    if bt["n"] >= 10 and bt.get("alpha") is not None:
        if abs(bt["alpha"]) < 1.0:
            notes.append(f"个股历史的超额为 {bt['alpha']:+.2f}pp，几乎等于无信号时的常态表现。")
        else:
            notes.append(f"个股历史相对「无信号日」的超额为 {bt['alpha']:+.2f}pp。")
    sb = out.get("score_bt") or {}
    if sb.get("verdict"):
        notes.append(sb["verdict"])
    if out.get("bt_cross") and out["bt_cross"].get("ok"):
        ex = out["bt_cross"].get("stat", {}).get("excluded_incomplete") or 0
        if ex:
            notes.append(f"横截面有 {ex} 个事件因「信号日距今不足 5 个交易日」而未走完持有期，"
                         f"已剔除（否则会用到不完整的未来数据）。")
    out["notes"] = notes
    out["disclosure"] = (
        "两把尺子并列、不相加：均衡分（6 维加权）看「技术面整体健康度」，"
        "反抽分（3 维 + 超跌门控）看「是不是超跌后的第一波反抽」。"
        "全市场 139 万个时点标定：均衡分与未来 20 日收益的秩相关是 −0.038（轻微反向，"
        "即「已经很强势」本身含追高风险），而反抽分是 +0.020；"
        "在均衡分<50 的子样本里，反抽分≥50 的一组 20 日胜率 72.3%、均值 +10.40%，"
        "反抽分<30 的一组只有 49.8% / +1.31%，且 35 个年份里有 29 年成立。"
        "但 2009 / 2014 这类大牛市里反抽组反而跑输——牛市里强者更强。"
        "回测为事件研究：信号日次日开盘买入、T+5 收盘卖出，纯价格、不含费用与涨跌停约束。"
        "样本 <10 时结论不可信。"
    )
    return out
