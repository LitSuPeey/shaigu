# -*- coding: utf-8 -*-
"""大周期通道模块（可复用）—— 月K线 + 上升通道上下轨 + 周期高低位判定 + 长期趋势 + 建议。

数据源级联（诚实标注实际使用的源）：
  ① 同花顺 平均股价 830000（web K线接口 bk_830000；该代码目前 web 端未开放，
     若同花顺未来开放则自动生效）
  ② 腾讯月K（实测可用，长历史）：国证A指 399317（2003~，全市场）/
     中证全指 000985（2012~）/ 上证指数 000001（2000~）

分析方法：
  · 通道：窗口内线性回归中轨 + 残差分位上下轨（对离群月 robust）→ 通道位置 0-100%
  · 趋势：对数价格回归斜率 → 年化涨幅；月线 MA12/MA36 多空排列佐证
  · 位置：通道位置 + 全历史分位双口径；规则化建议（趋势 × 位置 六象限）

所有阈值可通过 cfg 覆盖（前端「🧭 大周期」参数窗口），默认值即稳健设计值。
网络层复用 alert 模块的 _ensure_ua / _net_call（浏览器 UA + 代理↔直连切换）。
"""
from __future__ import annotations

import datetime as _dt
import math
import re as _re
import time
from typing import Optional

import numpy as np
import pandas as pd

from .alert import _UA, _ak, _ensure_ua, _net_call
from .ticker import _sess

# ============ 可调参数默认值（前端参数窗口可覆盖） ============
DEFAULTS: dict = {
    "channel_window": 48,   # 通道拟合窗口（月）——默认看最近 4 年
    "trend_window": 120,    # 长期趋势回归窗口（月）——默认看最近 10 年
    "upper_q": 0.95,        # 上轨 = 中轨 + 残差 95 分位
    "lower_q": 0.05,        # 下轨 = 中轨 + 残差 5 分位
    "high_pos": 80.0,       # 通道位置 ≥ 此值(%) → 高位
    "low_pos": 20.0,        # 通道位置 ≤ 此值(%) → 低位
}

SOURCES: dict = {
    "ths830000": "平均股价 830000（同花顺）",
    "399317": "国证A指 399317（全市场 · 腾讯月K）",
    "000300": "沪深300 000300（腾讯月K）",
    "000985": "中证全指 000985（腾讯月K）",
    "000001": "上证指数 000001（腾讯月K）",
}
_TENCENT_CODE = {"399317": "sz399317", "000300": "sh000300",
                 "000985": "sh000985", "000001": "sh000001"}

# 同花顺 830000 探测失败缓存：失败后 1 小时内直接走降级，
# 避免每次点分析都空等探测超时（体验为"点了没反应/不可用"）
_THS_FAIL_UNTIL = 0.0
_THS_FAIL_TTL = 3600.0


# ============ 数据源 ============
def _ths_daily_830000(max_years: int = 24) -> pd.DataFrame:
    """同花顺 平均股价 830000 全部日线（v4/line/bk_830000/{year}.js）。
    先探当年文件，失败直接抛异常（避免逐年在坏接口上空耗）。"""
    import py_mini_racer
    from akshare.stock_feature.stock_board_industry_ths import _get_file_content_ths

    js = py_mini_racer.MiniRacer()
    js.eval(_get_file_content_ths("ths.js"))
    v = js.call("v")
    year = _dt.date.today().year
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36",
        "Referer": "http://q.10jqka.com.cn",
        "Cookie": f"v={v}",
    }
    url0 = f"https://d.10jqka.com.cn/v4/line/bk_830000/01/{year}.js"
    r0 = _net_call(lambda: _sess().get(url0, headers=headers, timeout=8))
    if "{" not in r0.text:
        raise RuntimeError(f"830000 未开放（http {r0.status_code}）")

    frames = []
    for y in range(year - max_years + 1, year + 1):
        url = f"https://d.10jqka.com.cn/v4/line/bk_830000/01/{y}.js"
        try:
            r = _net_call(lambda: _sess().get(url, headers=headers, timeout=8))
        except Exception:  # noqa: BLE001 —— 单年失败跳过
            continue
        m = _re.search(r'"data":"([^"]+)"', r.text)
        if not m:
            continue
        rows = [ln.split(",") for ln in m.group(1).split(";") if len(ln) > 12]
        if rows:
            frames.append(pd.DataFrame(rows))
    if not frames:
        raise RuntimeError("830000 无有效数据")
    df = pd.concat(frames, ignore_index=True)
    df = df.iloc[:, :7]
    df.columns = ["date", "open", "high", "low", "close", "volume", "amount"]
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _to_monthly(daily: pd.DataFrame) -> list[dict]:
    """日线 → 月K（开=首日开，高=月内最高，低=月内最低，收=末日收）。"""
    d = daily.dropna(subset=["close"]).copy()
    d["date"] = pd.to_datetime(d["date"].astype(str).str[:10])
    d = d.sort_values("date").set_index("date")
    m = pd.DataFrame({
        "open": d["open"].resample("ME").first(),
        "high": d["high"].resample("ME").max(),
        "low": d["low"].resample("ME").min(),
        "close": d["close"].resample("ME").last(),
    }).dropna(subset=["close"])
    return [{"date": idx.strftime("%Y-%m"), **{k: round(float(v), 3) for k, v in row.items()}}
            for idx, row in m.iterrows()]


def _tencent_monthly(tcode: str) -> list[dict]:
    """腾讯月K线（实测 399317 自 2003、000985 自 2012、000001 自 2000）。

    【2026-10-19】web.ifzq.gtimg.cn 被腾讯 WAF 整体拦截（501 拦截页 → JSON
    解析报 "Expecting value"），改用 ifzq.gtimg.cn / proxy.finance.qq.com
    双入口轮询（实测均正常）。"""
    qs = f"appstock/app/fqkline/get?param={tcode},month,,,400,qfq"
    j = None
    last_err = None
    for base in ("https://ifzq.gtimg.cn",
                 "https://proxy.finance.qq.com/ifzqgtimg"):
        try:
            r = _net_call(
                lambda b=base: _sess().get(f"{b}/{qs}",
                                           headers={"User-Agent": _UA},
                                           timeout=15))
            j = r.json()
            break
        except Exception as e:  # noqa: BLE001 —— 换备用入口
            last_err = e
    if j is None:
        raise RuntimeError(f"腾讯月K全部入口失败: {str(last_err)[:80]}")
    node = j.get("data", {}).get(tcode, {})
    rows = node.get("qfqmonth") or node.get("month") or []
    # 行格式：[日期, 开, 收, 高, 低, 量, ...]
    out = []
    for k in rows:
        try:
            out.append({"date": str(k[0]), "open": float(k[1]), "close": float(k[2]),
                        "high": float(k[3]), "low": float(k[4])})
        except (ValueError, TypeError, IndexError):
            continue
    if len(out) < 24:
        raise RuntimeError(f"{tcode} 月K样本不足({len(out)})")
    return out


def fetch_monthly(source: str = "ths830000") -> tuple[list[dict], str, list[str]]:
    """按数据源级联取月K：返回 (rows, 实际数据源标签, notes)。"""
    global _THS_FAIL_UNTIL
    notes: list = []
    src = source if source in SOURCES else "ths830000"
    if src == "ths830000":
        if time.time() < _THS_FAIL_UNTIL:
            notes.append("同花顺 830000 近期探测不可用（已缓存，1 小时后自动重试），直接降级国证A指月K")
            src = "399317"
        else:
            try:
                return _to_monthly(_ths_daily_830000()), SOURCES["ths830000"], notes
            except Exception as e:  # noqa: BLE001 —— 降级到腾讯月K
                _THS_FAIL_UNTIL = time.time() + _THS_FAIL_TTL
                notes.append(f"同花顺 830000 暂不可用（{str(e)[:70]}），已自动降级到国证A指月K")
                src = "399317"  # 标签与代码同步切换，避免「数据是国证A指、标签却写 830000」
    tcode = _TENCENT_CODE.get(src, "sz399317")
    rows = _net_call(lambda: _tencent_monthly(tcode))
    label = SOURCES.get(src, SOURCES["399317"])
    return rows, label, notes


# ============ 通道与趋势分析 ============
# ============ 多因子趋势引擎（六法加权合成） ============
_TREND_WEIGHTS = {"rsl": 0.25, "ma": 0.20, "stage": 0.20, "macd": 0.15, "roc": 0.15, "pos": 0.05}


def _ema_series(arr: np.ndarray, n: int) -> np.ndarray:
    """EMA（周期 n），以前 n 个值的 SMA 播种（MACD 标准口径）。

    原实现用「首值」播种：长序列无碍，但月线最短仅 24 个月时 EMA12/26 尚未收敛
    （残差约 0.85^24≈2%），该偏差会直接进入 MACD 的 DIF/DEA 判定与因子分。
    SMA 播种是通行做法且收敛更快。返回长度与入参一致（调用方按位相减），
    前 n-1 位用扩展窗口均值占位（调用方只取末尾值，占位值不参与判分）。
    """
    a = np.asarray(arr, dtype=float)
    m = len(a)
    if m == 0:
        return a
    k = 2.0 / (n + 1)
    if m < n:
        seed, i0 = float(a[0]), 0
    else:
        seed, i0 = float(np.mean(a[:n])), n - 1
    out = np.empty(m)
    for i in range(i0):
        out[i] = float(np.mean(a[:i + 1]))
    v = seed
    out[i0] = v
    for i in range(i0 + 1, m):
        v = float(a[i]) * k + v * (1 - k)
        out[i] = v
    return out


def _trend_engine(closes: np.ndarray, pos: float) -> dict:
    """六法合成趋势判定（每种逻辑独立打分 -1~+1，加权合成总分 -100~+100）：
    ① RSL 相对强度（价/MA12，Weinstein 口径）② 均线多空排列 MA6/12/24
    ③ Weinstein 四阶段（MA12 斜率×价格位置）④ 月线 MACD（EMA12/26+DEA9）
    ⑤ ROC 12 个月动量 ⑥ 通道位置（均值回归视角，低权重）。"""
    f: list = []
    last = float(closes[-1])
    n_all = len(closes)

    def _fmt(name: str, value: str, score: float, desc: str) -> None:
        s = max(-1.0, min(1.0, float(score)))
        f.append({"name": name, "value": value, "score": round(s, 2),
                  "judge": "看多" if s > 0.15 else ("看空" if s < -0.15 else "中性"),
                  "desc": desc})

    # ① RSL：价格相对一年线的偏离 + 3 个月 RSL 动量方向
    ma12 = float(np.mean(closes[-12:]))
    rsl = last / ma12 if ma12 else 1.0
    rsl3 = (float(closes[-4]) / float(np.mean(closes[-15:-3]))) if n_all >= 15 else rsl
    s_rsl = (rsl - 1) * 5 + (0.25 if rsl > rsl3 else -0.25)
    _fmt("① RSL 相对强度（价/MA12）", f"{rsl:.3f}", s_rsl,
         f"价格较一年线{'高' if rsl > 1 else '低'} {(abs(rsl - 1)) * 100:.1f}%，RSL 3 个月{'走强' if rsl > rsl3 else '走弱'}")

    # ② 均线排列：MA6/MA12/MA24 相对位置 + 价格与短均线关系
    ma6 = float(np.mean(closes[-6:]))
    ma24 = float(np.mean(closes[-24:])) if n_all >= 24 else ma12
    s_ma = 0.5 * np.sign(ma6 - ma12) + 0.3 * np.sign(ma12 - ma24) + 0.2 * np.sign(last - ma6)
    _fmt("② 均线排列（MA6/12/24）", f"{ma6:.0f}/{ma12:.0f}/{ma24:.0f}", s_ma,
         "三线多头" if ma6 > ma12 > ma24 else ("三线空头" if ma6 < ma12 < ma24 else "均线纠缠"))

    # ③ Weinstein 四阶段：MA12 斜率（近 12 个月）× 价格相对一年线位置
    ma12_prev = float(np.mean(closes[-24:-12])) if n_all >= 24 else ma12
    slope12 = (ma12 / ma12_prev - 1) * 100 if ma12_prev else 0.0
    flat = abs(slope12) < 2.0
    if ma12 >= ma12_prev and last > ma12:
        stage, s_stage = ("第②阶段·上升", 1.0 if not flat else 0.7)
    elif ma12 >= ma12_prev and last <= ma12:
        stage, s_stage = ("第③阶段·做头", -0.4)
    elif ma12 < ma12_prev and last > ma12:
        stage, s_stage = ("第①阶段·筑底", 0.2 if flat else 0.0)
    else:
        stage, s_stage = ("第④阶段·下降", -1.0 if not flat else -0.7)
    _fmt("③ Weinstein 四阶段", f"{stage}（斜率 {slope12:+.1f}%）", s_stage,
         "价格与 MA12 的相对位置 + 均线方向定阶段；|斜率|<2% 视为走平")

    # ④ 月线 MACD：DIF=EMA12-EMA26，DEA=EMA9(DIF)
    #   打分 = DIF 零轴主轴(±0.4) + 交叉位置(±0.3) + 柱动量(tanh 压缩 ±0.3)
    #   ——只看柱转正会把"深跌后的柱收敛"误判成多头，DIF 零轴位置才是主轴
    dif = _ema_series(closes, 12) - _ema_series(closes, 26)
    dea = _ema_series(dif, 9)
    hist_pct = (dif[-1] - dea[-1]) / last * 100
    s_macd = (0.4 * (1 if dif[-1] > 0 else -1)
              + 0.3 * (1 if dif[-1] > dea[-1] else -1)
              + 0.3 * math.tanh(hist_pct * 1.5))
    _fmt("④ 月线 MACD", f"DIF {dif[-1]:.0f} / DEA {dea[-1]:.0f}", s_macd,
         ("DIF 零轴上方" if dif[-1] > 0 else "DIF 零轴下方")
         + ("·金叉" if dif[-1] > dea[-1] else "·死叉")
         + f"，柱占价比 {hist_pct:+.2f}%（正=多头动能修复）")

    # ⑤ ROC 12 个月动量（±40% 归一）
    roc12 = (last / float(closes[-13]) - 1) * 100 if n_all >= 13 else 0.0
    s_roc = roc12 / 40
    _fmt("⑤ ROC 12 个月动量", f"{roc12:+.1f}%", s_roc, "年动量除以 ±40% 归一化")

    # ⑥ 通道位置（均值回归视角：高位回撤风险 / 低位修复空间）
    s_pos = (50 - pos) / 50 * 0.8
    _fmt("⑥ 通道位置（回归视角）", f"{pos:.0f}%", s_pos,
         "位置因子低权重：高位提示回撤风险、低位提示修复空间")

    keys = ["rsl", "ma", "stage", "macd", "roc", "pos"]
    total = sum(_TREND_WEIGHTS[k] * x["score"] for k, x in zip(keys, f)) * 100
    total = max(-100.0, min(100.0, total))
    if total >= 50:
        label = "强势上升趋势"
    elif total >= 20:
        label = "温和上升趋势"
    elif total > -20:
        label = "震荡筑底 · 方向待选"
    elif total > -50:
        label = "温和下降趋势"
    else:
        label = "强势下降趋势"
    return {"factors": f, "total": round(total, 1), "label": label,
            "weights": _TREND_WEIGHTS}


def _advise(trend_up: bool, zone: str, annual: float, ma_bull: bool) -> str:
    """趋势 × 位置 六象限规则建议。"""
    if trend_up and zone == "高位":
        return ("长期趋势向上，但价格贴近通道上轨——短期追高风险大；"
                "持仓者可分批止盈，空仓者等回踩中轨再介入")
    if trend_up and zone == "低位":
        return ("上升趋势中出现回踩通道下方的低位区，历史上多为较好的布局窗口，"
                "可分批建仓、以跌破下轨止损")
    if trend_up and zone == "中位":
        return ("趋势与位置皆健康：上升趋势中段，持股为主，"
                "回踩中轨附近可加仓，注意单月急涨后的节奏")
    if not trend_up and zone == "高位":
        return ("长期趋势向下，当前反弹至通道上沿——属反弹尾声的风险区，"
                "宜减仓防守，勿把反弹当反转")
    if not trend_up and zone == "低位":
        return ("长期下行 + 低位超跌，属磨底阶段：不必急抄底，"
                "可小仓位分批跟踪，等月线站上中轨/趋势斜率转正再加大力度")
    return ("长期趋势仍向下、位置中性：以反弹对待，控制总仓位，"
            "重点跟踪政策面与月线 MA12/MA36 何时修复")


def analyze(rows: list[dict], cfg: Optional[dict] = None,
            forecast_years: int = 3) -> dict:
    """月K → 通道 + 趋势 + 位置 + 建议（纯计算，可独立复用）。"""
    c = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if v is not None and k in c:
            c[k] = v
    notes: list = []

    df = (pd.DataFrame(rows).dropna(subset=["close"])
          .sort_values("date").reset_index(drop=True))
    n_all = len(df)
    if n_all < 24:
        raise ValueError(f"月K样本不足（{n_all} < 24 个月）")
    closes = df["close"].astype(float).to_numpy()
    last = float(closes[-1])

    # ---- 长期趋势：对数回归斜率（年化）----
    tw = int(min(max(int(c["trend_window"]), 36), n_all))
    seg = closes[-tw:]
    x = np.arange(len(seg), dtype=float)
    slope = float(np.polyfit(x, np.log(seg), 1)[0])
    annual = (math.exp(slope * 12) - 1) * 100
    trend_up = annual > 0
    ma12 = float(np.mean(closes[-12:]))
    ma36 = float(np.mean(closes[-36:])) if n_all >= 36 else None
    ma_bull = (ma36 is None) or (ma12 > ma36)

    # ---- 通道：回归中轨 + 残差分位上下轨 ----
    cw = int(min(max(int(c["channel_window"]), 24), n_all))
    seg2 = closes[-cw:]
    x2 = np.arange(cw, dtype=float)
    a, b = np.polyfit(x2, seg2, 1)
    mid = a * x2 + b
    resid = seg2 - mid
    uq = float(np.quantile(resid, min(max(float(c["upper_q"]), 0.5), 1.0)))
    lq = float(np.quantile(resid, min(max(float(c["lower_q"]), 0.0), 0.5)))
    upper, lower = mid + uq, mid + lq
    span = float(upper[-1] - lower[-1])
    pos_raw = (last - float(lower[-1])) / span * 100 if span > 0 else 50.0
    pos = min(max(pos_raw, 0.0), 100.0)
    if pos_raw > 100:
        notes.append(f"当前价已升破通道上轨（位置 {pos_raw:.0f}%）")
    elif pos_raw < 0:
        notes.append(f"当前价已跌破通道下轨（位置 {pos_raw:.0f}%）")
    dist_upper = (float(upper[-1]) / last - 1) * 100
    dist_lower = (float(lower[-1]) / last - 1) * 100

    # ---- 位置与动量口径 ----
    pct_hist = float((closes < last).mean() * 100)
    chg12 = (last / float(closes[-13]) - 1) * 100 if n_all >= 13 else None

    high_th, low_th = float(c["high_pos"]), float(c["low_pos"])
    zone = "高位" if pos >= high_th else ("低位" if pos <= low_th else "中位")

    # ---- 多因子趋势引擎（六法合成）----
    te = _trend_engine(closes, pos)
    trend_up = te["total"] > 0   # 合成口径定多空（原对数回归/MA 排列作为因子保留在引擎内）

    advice = [
        f"【趋势】多因子合成：{te['label']}（评分 {te['total']:+.0f}/100）；"
        f"对数年化 {annual:+.1f}%（近 {tw} 个月）、月线 MA12 {ma12:.0f} "
        f"{'>' if ma_bull else '<'} MA36 {ma36:.0f}（{'多头' if ma_bull else '空头'}排列）",
        f"【位置】当前周期（近 {cw} 个月）通道位置 {pos:.0f}%，判定 {zone}；"
        f"距上轨 {dist_upper:+.1f}%、距下轨 {dist_lower:+.1f}%（月斜率 {a:+.2f}）",
        f"【历史】当前价处于全部 {n_all} 个月历史的 {pct_hist:.0f}% 分位"
        + (f"；近 12 个月 {chg12:+.1f}%" if chg12 is not None else ""),
        f"【建议】{_advise(trend_up, zone, annual, ma_bull)}",
    ]

    pad = n_all - cw
    none_pad = [None] * pad

    # ---- 走势预测：通道回归线性外推（1~14 年，月度虚线）----
    # 方法：中轨沿通道斜率 a 外推，轨道宽度（残差分位 uq/lq）保持——
    # 与历史通道同一统计口径，只延展不发散；标注为统计外推而非投资建议。
    fy = int(min(max(int(forecast_years or 3), 1), 14))
    last_date = str(df["date"].iloc[-1])
    yy, mm = int(last_date[:4]), int(last_date[5:7])
    forecast = []
    for k in range(1, fy * 12 + 1):
        mm += 1
        if mm > 12:
            mm, yy = 1, yy + 1
        m_ext = float(a * (cw - 1 + k) + b)
        forecast.append({
            "date": f"{yy:04d}-{mm:02d}",
            "mid": round(m_ext, 2),
            "upper": round(m_ext + uq, 2),
            "lower": round(m_ext + lq, 2),
        })
    f_end = forecast[-1]
    f_chg_mid = (f_end["mid"] / last - 1) * 100

    return {
        "params": {k: c[k] for k in DEFAULTS},
        "source_rows": int(n_all),
        "monthly": [{"date": r["date"], "open": _rd(r.get("open")), "high": _rd(r.get("high")),
                     "low": _rd(r.get("low")), "close": _rd(r.get("close")),
                     "mid": None if i < pad else _rd(mid[i - pad]),
                     "upper": None if i < pad else _rd(upper[i - pad]),
                     "lower": None if i < pad else _rd(lower[i - pad])}
                    for i, r in df.iterrows()],
        "metrics": {
            "annualized_pct": round(annual, 2), "trend_up": bool(trend_up),
            "ma12": round(ma12, 2), "ma36": None if ma36 is None else round(ma36, 2),
            "ma_bull": bool(ma_bull), "channel_slope": round(float(a), 3),
            "position_pct": round(pos, 1), "position_raw": round(pos_raw, 1),
            "dist_upper_pct": round(dist_upper, 2), "dist_lower_pct": round(dist_lower, 2),
            "history_pct": round(pct_hist, 1),
            "chg_12m": None if chg12 is None else round(chg12, 2),
            "last": round(last, 2),
        },
        "verdict": {
            "trend": te["label"],
            "trend_up": bool(trend_up),
            "zone": zone, "high_th": high_th, "low_th": low_th,
            "channel_window": cw, "trend_window": tw,
            "engine_total": te["total"],
        },
        "trend_engine": te,
        "advice": advice,
        "forecast": forecast,
        "forecast_years": fy,
        "forecast_note": (
            f"按通道斜率外推 {fy} 年（{fy * 12} 个月）：中轨自 {last:,.0f} "
            f"至 {f_end['mid']:,.0f}（{f_chg_mid:+.1f}%），轨道宽度与历史一致；"
            "统计外推仅供研究参考，不构成投资建议"),
        "notes": notes,
    }


def _rd(v, nd=2):
    try:
        f = float(v)
        return None if f != f else round(f, nd)
    except (TypeError, ValueError):
        return None


def run(source: str = "ths830000", cfg: Optional[dict] = None,
        forecast_years: int = 3) -> dict:
    """模块入口：取数（级联降级）→ 分析 + 走势外推。供 /api/cycle/analyze 调用。"""
    rows, label, notes = fetch_monthly(source or "ths830000")
    out = analyze(rows, cfg, forecast_years=forecast_years)
    out["source_label"] = label
    out["notes"] = notes + [n for n in out.get("notes", []) if n not in notes]
    out["date"] = _dt.date.today().strftime("%Y-%m-%d")
    return out
