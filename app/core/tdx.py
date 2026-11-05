# -*- coding: utf-8 -*-
"""easy-tdx 快速同步通道（可选能力，缺库自动降级）。

为什么单独一个模块
==================
现有同步链路走 akshare / 东财 HTTP，逐只请求，全 A 约 30 分钟。
easy-tdx 直连通达信 TCP 协议（7709 端口），实测快一个数量级：

    单连接串行 300 只  7.0s（23.5 ms/只）→ 全市场 5554 只约 2.2 分钟

★三条实测结论（不要"优化"掉，都是我踩过的）
---------------------------------------------
1. **必须单连接串行**。8 连接并发跑 300 只，47 只异常（15.7%）；
   单连接串行 300 只只失败 1 只。并发只会让你重试，反而更慢。
2. **复权口径与本地库不完全一致**。末日收盘完全一致
   （茅台 1392.05 / 平安 11.64），但历史比值不恒定：
   平安银行近 120 天比值均值 0.981、极差 2.6%（两家对分红的处理不同）。
   所以合并时**必须做重叠期比值对齐**，否则历史会跳空。
3. **字段单位**：`vol` 是「股」、`amount` 是「元」、
   `float_shares` 是「万股」（×10000 才是股）。本地 `outstanding_share` 存的也是股。

安全边界
--------
· 只 UPSERT **TDX 返回的日期**，不删除任何本地行；
· 比值偏离 > 0.5% 时，把**新数据缩放到本地基准**（不动历史），并记录警告；
· 任何异常都只影响单只股票，继续下一只。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import db

# ---------------------------------------------------------------------------
# 可选依赖：没装就降级，不影响主流程
# ---------------------------------------------------------------------------
try:                                            # pragma: no cover
    from easy_tdx import Adjust, MacClient, Market, Period
    _AVAILABLE = True
    _IMPORT_ERR = None
except Exception as e:                          # noqa: BLE001
    _AVAILABLE = False
    _IMPORT_ERR = f"{type(e).__name__}: {e}"
    Adjust = MacClient = Market = Period = None  # type: ignore

# ★节流：不是"限制功能"，是"不给对方服务器添麻烦"。
#   不加节流时单只 23.5 ms（每秒约 42 只），全市场 2.6 分钟——对通达信服务器太密集，
#   实测并发 8 连接就有 15.7% 的请求被拒，说明服务器有保护机制在跑。
#   50 ms/只 相当于每秒约 14 只（连请求本身约 70ms/只），全市场约 6.5 分钟：
#   比 akshare 快 4.5 倍，且只有无限速时（42 只/秒）的三分之一密度。
#   这是「能用」和「不冒犯」之间的折中点——再往下调收益递减，被拒概率上升。
#   ⚠ 注意：节流降低的是「被服务器拒/封」的概率，**不是合规手段**。
#     合规边界在数据用途（个人研究 vs 对外服务/再分发），不在这里。
MIN_INTERVAL_MS = 50

# 比值偏离超过这个值就认为复权基准不同，需要缩放对齐
RATIO_TOL = 0.005
OVERLAP_DAYS = 20          # 用最近 20 个重叠日算比值中位数
DEFAULT_COUNT = 400        # 单次拉取的 K 线根数（覆盖一年多的交易日）


_last_call = [0.0]


def _throttle(interval_ms: int = MIN_INTERVAL_MS) -> None:
    """全局节流（单线程串行，不需要锁）。"""
    gap = max(0, int(interval_ms)) / 1000.0
    if gap <= 0:
        return
    import time as _t
    wait = gap - (_t.time() - _last_call[0])
    if wait > 0:
        _t.sleep(wait)
    _last_call[0] = _t.time()


def available() -> bool:
    return _AVAILABLE


def status() -> dict:
    return {"available": _AVAILABLE, "error": _IMPORT_ERR,
            "install": "pip install git+https://github.com/clong365/easy_tdx.git",
            "note": "PyPI 上目前没有 easy-tdx，只能从 GitHub 源码安装"}


def market_of(code: str):
    """代码 → easy_tdx 的 Market 枚举（与 exchange_of 同一套前缀规则）。"""
    c = str(code or "")
    if c[:2] in ("43", "83", "87", "88", "92"):
        return Market.BJ
    return Market.SH if c[:1] in ("6", "9", "5") else Market.SZ


# ---------------------------------------------------------------------------
# 单只拉取 + 列归一化
# ---------------------------------------------------------------------------
def fetch_kline(client, code: str, count: int = DEFAULT_COUNT) -> Optional[pd.DataFrame]:
    """拉一只票的日线（前复权），返回统一列名的 DataFrame。"""
    try:
        df = client.get_stock_kline(market_of(code), str(code),
                                    Period.DAILY, count=int(count),
                                    adjust=Adjust.QFQ)
    except Exception:                          # noqa: BLE001
        return None
    if df is None or not len(df):
        return None
    out = pd.DataFrame({
        "date": pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d"),
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df["vol"], errors="coerce"),
        "amount": pd.to_numeric(df["amount"], errors="coerce"),
    })
    # float_shares 单位是「万股」→ 股
    if "float_shares" in df.columns:
        out["outstanding_share"] = pd.to_numeric(df["float_shares"], errors="coerce") * 1e4
    else:
        out["outstanding_share"] = np.nan
    out = out.dropna(subset=["close"])
    out = out[(out["close"] > 0) & (out["high"] >= out["low"])]
    # ★TDX 内部是 float32，直接写库会留下 11.640000343322754 这种浮点尾巴。
    #   价格统一保留 3 位（A 股 2 位、部分基金 3 位），量/额取整。
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].astype(float).round(3)
    for col in ("volume", "amount", "outstanding_share"):
        out[col] = out[col].astype(float).round(0)
    return out.reset_index(drop=True) if len(out) else None


# ---------------------------------------------------------------------------
# 复权基准对齐
# ---------------------------------------------------------------------------
def _align_to_local(conn, code: str, new_df: pd.DataFrame) -> tuple:
    """把 TDX 的新数据对齐到本地库的复权基准。

    返回 (对齐后的 df, 比值, 是否缩放)
    · 用最近 20 个重叠日的 close 比值中位数
    · 偏离 > RATIO_TOL 就缩放新数据（**不动历史**），保证拼接处不跳空
    """
    old = pd.read_sql_query(
        "SELECT date, close FROM daily WHERE code=? ORDER BY date DESC LIMIT ?",
        conn, params=(str(code), OVERLAP_DAYS * 3))
    if old.empty:
        return new_df, 1.0, False
    m = new_df.merge(old, on="date", suffixes=("", "_old"), how="inner").tail(OVERLAP_DAYS)
    if len(m) < 3:
        return new_df, 1.0, False
    old_c = pd.to_numeric(m["close_old"], errors="coerce").to_numpy(dtype=float)
    new_c = pd.to_numeric(m["close"], errors="coerce").to_numpy(dtype=float)
    ok = np.isfinite(old_c) & np.isfinite(new_c) & (new_c > 0)
    if ok.sum() < 3:
        return new_df, 1.0, False
    ratio = float(np.median(old_c[ok] / new_c[ok]))
    if not math.isfinite(ratio) or ratio <= 0:
        return new_df, 1.0, False
    if abs(ratio - 1.0) <= RATIO_TOL:
        return new_df, ratio, False
    out = new_df.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].astype(float) * ratio
    return out, ratio, True


# ---------------------------------------------------------------------------
# 批量同步
# ---------------------------------------------------------------------------
def sync_daily(codes: List[str], count: int = DEFAULT_COUNT,
               progress_cb=None, dry_run: bool = False,
               interval_ms: int = MIN_INTERVAL_MS) -> dict:
    """用 easy-tdx 批量补日线。

    ★单连接串行（并发会掉 15% 的数据，见模块 docstring）。
    返回 {ok, failed, skipped, aligned, rows, elapsed, errors}
    """
    if not available():
        return {"ok": 0, "failed": 0, "skipped": len(codes or []), "rows": 0,
                "aligned": [], "elapsed": 0.0,
                "error": "easy-tdx 未安装", "hint": status()["install"]}

    import time as _t
    codes = [str(c) for c in (codes or []) if c]
    if not codes:
        return {"ok": 0, "failed": 0, "skipped": 0, "rows": 0,
                "aligned": [], "elapsed": 0.0, "error": "股票列表为空"}

    conn = db.reader()
    wconn = db.writer() if not dry_run else None
    ok = failed = skipped = rows = 0
    aligned: List[dict] = []
    errors: List[str] = []
    t0 = _t.time()

    try:
        with MacClient.from_best_host() as client:
            for i, code in enumerate(codes):
                # ★进度回调放循环开头：失败/跳过的票也要报进度
                if progress_cb and ((i + 1) % 50 == 0 or i + 1 == len(codes)):
                    eta = int((len(codes) - i - 1) * max(1, interval_ms) / 1000)
                    progress_cb(i + 1, len(codes),
                                f"备用通道同步 {i+1}/{len(codes)}（成功 {ok}，"
                                f"约剩 {eta // 60} 分 {eta % 60} 秒）")
                _throttle(interval_ms)
                df = fetch_kline(client, code, count)
                if df is None or not len(df):
                    skipped += 1
                    continue
                df2, ratio, scaled = _align_to_local(conn, code, df)
                if scaled:
                    aligned.append({"code": code, "ratio": round(ratio, 5)})
                if dry_run:
                    ok += 1
                    rows += len(df2)
                    continue
                try:
                    with db.write_lock():
                        wconn.executemany(
                            "INSERT OR REPLACE INTO daily"
                            "(code,date,open,high,low,close,volume,amount,turnover,outstanding_share)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?)",
                            [
                                (code, r.date, float(r.open), float(r.high),
                                 float(r.low), float(r.close), float(r.volume),
                                 float(r.amount),
                                 (float(r.volume) / float(r.outstanding_share)
                                  if r.outstanding_share and r.outstanding_share > 0 else None),
                                 (float(r.outstanding_share)
                                  if r.outstanding_share and math.isfinite(r.outstanding_share)
                                  else None))
                                for r in df2.itertuples(index=False)
                            ])
                        wconn.commit()
                    ok += 1
                    rows += len(df2)
                except Exception as e:         # noqa: BLE001
                    failed += 1
                    if len(errors) < 10:
                        errors.append(f"{code}: {type(e).__name__}: {str(e)[:80]}")
    except Exception as e:                     # noqa: BLE001
        return {"ok": ok, "failed": failed, "skipped": skipped, "rows": rows,
                "aligned": aligned, "elapsed": round(_t.time() - t0, 1),
                "error": f"连接失败：{type(e).__name__}: {str(e)[:160]}",
                "errors": errors}
    finally:
        if wconn is not None:
            try:
                wconn.commit()
            except Exception:                  # noqa: BLE001
                pass

    elapsed = round(_t.time() - t0, 1)
    return {
        "ok": ok, "failed": failed, "skipped": skipped, "rows": rows,
        "aligned": aligned[:30], "n_aligned": len(aligned),
        "elapsed": elapsed,
        "speed": round(ok / elapsed, 1) if elapsed > 0 else None,
        "dry_run": dry_run,
        "interval_ms": interval_ms,
        "role": "optional",
        "note": (f"通达信通道（节流 {interval_ms}ms/只）；{ok} 只成功、"
                 f"{skipped} 只无数据、{failed} 只写入失败。"
                 + (f" 其中 {len(aligned)} 只做了复权基准缩放（与本地不一致）。"
                    if aligned else "")),
        "errors": errors,
    }
