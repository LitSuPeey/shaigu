# -*- coding: utf-8 -*-
"""快照表构建：把每只股票的最新行情/估值/分位/股息率预计算成一张 5554 行小表。

筛选模块（PE/PB 低估、高股息、最新行情）直接查这张表，避免每次扫描 940 万行估值。
构建是一次性（数据同步后刷新一次），可接受数十秒与较大内存。

★ 2026-11-05 两处口径修复
--------------------------
1) `div_yield` 以前存的是**每股分红（元）**，没除以股价，却按「股息率 %」使用：
   条件⑩「股息率≥3%」实际选出的是 13 只高价股（茅台 51.98、吉比特 13.0…），
   真正股息率≥3% 的 516 只全被漏掉；TA 基本面分的股息维度也几乎恒为 0。
   现在按 `每股分红 / 收盘价 × 100` 存**百分比**（与前端口径、ta_ctx 的分段一致）。
2) 估值分位以前只有「全部历史」一档，导致条件筛选页的「估值回看年数」
   「最少估值样本数」两个输入框无论怎么调都不改变结果。
   现在预计算 1/3/5/10 年四档（pe_pct_* / pb_pct_*）与各档样本数 val_n_*，
   由 conditions 按用户选择取用；`pe_pct`/`pb_pct` 保留为 10 年档，向后兼容。
"""
import time
from typing import Optional

import numpy as np
import pandas as pd

from . import db

# 可选估值回看窗口（年）。10 年档写入 pe_pct/pb_pct 主列（历史兼容）。
VAL_WINDOWS = (1, 3, 5, 10)

SNAP_COLS = [
    "code", "date", "close", "prev_close", "pct_chg",
    "volume", "amount", "turnover",
    "pe_ttm", "pb", "pe_pct", "pb_pct", "div_yield",
    "pe_pct_1y", "pe_pct_3y", "pe_pct_5y",
    "pb_pct_1y", "pb_pct_3y", "pb_pct_5y",
    "val_n_1y", "val_n_3y", "val_n_5y", "val_n_10y",
]


def ensure_cols(conn) -> None:
    """老库补列：snapshot 是派生表，新增档位只加列不重建，避免丢数据。"""
    have = {r[1] for r in conn.execute("PRAGMA table_info(snapshot)")}
    for c in SNAP_COLS:
        if c not in have:
            conn.execute(f"ALTER TABLE snapshot ADD COLUMN {c} REAL")
    conn.commit()


# 兼容旧名（模块内部调用）
_ensure_cols = ensure_cols


def needs_rebuild(conn) -> bool:
    """老快照需要重算：新增列还没填过（val_n_10y 全空）说明是旧版构建的。

    不重算的话，新列全是 NULL，PE/PB 条件会静默返回 0 只——比报错更难发现。
    """
    try:
        n = conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
        if n == 0:
            return True
        filled = conn.execute(
            "SELECT COUNT(*) FROM snapshot WHERE val_n_10y IS NOT NULL").fetchone()[0]
        return filled == 0
    except Exception:      # noqa: BLE001
        return True


def _val_window_block(val: pd.DataFrame, latest: pd.Series, years: int) -> pd.DataFrame:
    """单个回看窗口：每只股票「最新估值在其窗口内的分位」+ 窗口内样本数。

    只取每个 code 的最后一行（当前估值），因此不存在未来函数。
    """
    cutoff = latest - pd.DateOffset(years=years)
    sub = val.loc[val["date"] >= cutoff, ["code", "date", "pe_ttm", "pb"]]
    g = sub.groupby("code", observed=True)
    n_pe = g["pe_ttm"].transform("count")
    n_pb = g["pb"].transform("count")
    sub = sub.assign(
        pe_pct=g["pe_ttm"].rank() / n_pe.replace(0, np.nan) * 100.0,
        pb_pct=g["pb"].rank() / n_pb.replace(0, np.nan) * 100.0,
        val_n=n_pe,
    )
    last = sub.groupby("code", observed=True).tail(1)
    suffix = "10y" if years >= 10 else f"{years}y"
    out = last[["code", "pe_pct", "pb_pct", "val_n"]].copy()
    out.columns = ["code", f"pe_pct_{suffix}", f"pb_pct_{suffix}", f"val_n_{suffix}"]
    return out


def build_snapshot(progress_cb=None) -> dict:
    """构建/刷新 snapshot 表。返回统计信息。"""
    t0 = time.time()
    rconn = db.reader()
    wconn = db.writer()
    db.ensure_schema(wconn)  # 确保 snapshot 表存在
    _ensure_cols(wconn)

    def cb(frac, msg):
        if progress_cb:
            progress_cb(frac, 1.0, msg)

    # ---------- 1. 最新日线 + 前收（纯 SQL，快） ----------
    cb(0.04, "日线最新快照")
    daily = pd.read_sql_query(
        "SELECT d.code, d.date, d.close, d.volume, d.amount, d.turnover, "
        "       (SELECT d2.close FROM daily d2 "
        "        WHERE d2.code=d.code AND d2.date<d.date "
        "        ORDER BY d2.date DESC LIMIT 1) AS prev_close "
        "FROM daily d "
        "WHERE d.date = (SELECT MAX(date) FROM daily WHERE code=d.code)",
        rconn)

    # ---------- 2. 最新估值 + 多窗口 PE/PB 分位 ----------
    cb(0.12, "读取估值历史")
    val = pd.read_sql_query(
        "SELECT code, date, pe_ttm, pb FROM valuation", rconn)
    empty_val = val.empty
    if not empty_val:
        val["code"] = val["code"].astype("category")
        val["pe_ttm"] = pd.to_numeric(val["pe_ttm"], errors="coerce").astype("float32")
        val["pb"] = pd.to_numeric(val["pb"], errors="coerce").astype("float32")
        val["date"] = pd.to_datetime(val["date"], errors="coerce")
        val = val.sort_values(["code", "date"])
        latest = val.groupby("code", observed=True)["date"].transform("max")

        blocks = []
        for i, w in enumerate(VAL_WINDOWS):
            cb(0.15 + 0.10 * i, f"估值分位 {w} 年")
            blocks.append(_val_window_block(val, latest, w))
        val_latest = blocks[-1]                      # 10 年档（含主列）
        for b in blocks[:-1]:
            val_latest = val_latest.merge(b, on="code", how="left")
        # 主列 = 10 年档，历史读取方（beton/ta_ctx）不用改
        val_latest = val_latest.rename(
            columns={"pe_pct_10y": "pe_pct", "pb_pct_10y": "pb_pct"})
        # 最新一档的原始 PE/PB（条件筛选用）
        last_vals = (val.groupby("code", observed=True).tail(1)[["code", "pe_ttm", "pb"]]
                     .copy())
        last_vals["code"] = last_vals["code"].astype(str)
        val_latest["code"] = val_latest["code"].astype(str)
        val_latest = val_latest.merge(last_vals, on="code", how="left")
        del val
    else:
        val_latest = pd.DataFrame(columns=["code", "pe_ttm", "pb", "pe_pct", "pb_pct"])

    # ---------- 3. 最近一年股息率（dividend 仅 5 万行，轻） ----------
    cb(0.62, "股息率计算")
    latest_date = daily["date"].max() if not daily.empty else None
    div = pd.read_sql_query(
        "SELECT code, ex_date, cash_per_10 FROM dividend", rconn)
    div_snap = pd.DataFrame(columns=["code", "div_dps"])
    if not div.empty and latest_date:
        import datetime as _dt
        try:
            cutoff = (_dt.datetime.strptime(str(latest_date), "%Y-%m-%d")
                      - _dt.timedelta(days=365)).strftime("%Y-%m-%d")
        except Exception:
            cutoff = None
        if cutoff:
            div_recent = div[div["ex_date"] >= cutoff]
            dps = div_recent.groupby("code")["cash_per_10"].sum() / 10.0
            div_snap = pd.DataFrame({
                "code": dps.index.astype(str),
                "div_dps": dps.values,
            })

    # ---------- 4. 合并 ----------
    cb(0.72, "合并写入")
    snap = daily.merge(val_latest, on="code", how="left")
    snap = snap.merge(div_snap, on="code", how="left")
    snap["code"] = snap["code"].astype(str)
    if "prev_close" in snap.columns:
        snap["pct_chg"] = np.where(
            snap["prev_close"].notna() & (snap["prev_close"] != 0),
            (snap["close"] - snap["prev_close"]) / snap["prev_close"] * 100.0,
            np.nan)
    else:
        snap["pct_chg"] = np.nan

    # ★ 股息率 = 每股分红 / 收盘价 × 100（百分比）。旧版漏了除法。
    if "div_dps" in snap.columns:
        close = pd.to_numeric(snap["close"], errors="coerce")
        snap["div_yield"] = np.where(close > 0,
                                     snap["div_dps"] / close * 100.0, np.nan)
    else:
        snap["div_yield"] = np.nan

    for c in SNAP_COLS:
        if c not in snap.columns:
            snap[c] = np.nan
    snap = snap[SNAP_COLS]

    # ---------- 5. 写入（先清空再批量写；列名显式，兼容 ALTER 追加的列） ----------
    cb(0.85, "写入数据库")
    snap = snap.where(pd.notna(snap), None)
    records = [tuple(None if (isinstance(v, float) and (v != v)) else v
                     for v in r) for r in snap.itertuples(index=False, name=None)]
    placeholders = ",".join("?" * len(SNAP_COLS))
    with db.write_lock():
        wconn.execute("DELETE FROM snapshot")
        wconn.executemany(
            f"INSERT OR REPLACE INTO snapshot ({','.join(SNAP_COLS)}) "
            f"VALUES ({placeholders})", records)
        wconn.commit()

    cb(1.0, "完成")
    return {
        "rows": len(snap),
        "elapsed": round(time.time() - t0, 2),
    }


def latest_rows(limit: int = 500) -> list[dict]:
    """读取快照表（供筛选模块直接查）。"""
    rconn = db.reader()
    df = pd.read_sql_query(
        "SELECT * FROM snapshot ORDER BY code LIMIT ?", rconn,
        params=(int(limit),))
    return df.where(pd.notna(df), None).to_dict(orient="records")


def snapshot_ready() -> bool:
    """快照表是否已构建。"""
    rconn = db.reader()
    try:
        n = rconn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
        return n > 0
    except Exception:
        return False
