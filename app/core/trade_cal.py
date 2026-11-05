# -*- coding: utf-8 -*-
"""交易日历工具：判定「最近已完成交易日」，供数据新鲜度提示与增量同步跳过判定。

数据源：akshare tool_trade_date_hist_sina（新浪交易日历，一次请求含 1990~今年）。
本地缓存 data/trade_cal.json（30 天刷新；过期缓存仍可兜底使用——
宁可少提示也不误报，节假日误报比数据滞后漏报更影响体验）。

「最近已完成交易日」定义（关键：日线数据源盘后更新有延迟）：
- 今天是交易日 且 本地时间 >= 20:00（保守覆盖数据源更新延迟）→ 今天
- 否则（盘中 / 收盘初期 / 周末 / 节假日）→ 严格早于今天的最近一个交易日
示例：周一 10:00 盘中 → 上周五；周六任意时刻 → 周五；国庆 10/3 → 9/30。
"""
from __future__ import annotations

import bisect
import json
import os
import threading
import time
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CAL_PATH = os.path.join(_ROOT, "data", "trade_cal.json")

_CAL_TTL = 30 * 86400      # 日历文件 30 天刷新一次
_FETCH_TIMEOUT = 8         # 日历下载硬超时（秒）
_DATA_READY_HOUR = 20      # 当日日线视为「已可拉取」的本地时刻（数据源盘后更新延迟兜底）

_lock = threading.Lock()
_DATES: list[str] | None = None     # 升序 YYYY-MM-DD
_DATES_TS: float = 0.0
_LAST_LOAD = {"ok": False}          # 最近一次加载是否拿到真实日历（False=降级）


def degraded() -> bool:
    """最近一次日历加载是否降级（无真实日历，放弃滞后判定以避免节假日误报）。"""
    return not _LAST_LOAD["ok"]


def _fetch_dates() -> list[str] | None:
    """从新浪交易日历拉取全部交易日（硬超时保护）。"""
    box: list = []

    def _run():
        import akshare  # 主进程启动时已 warmup，此处复用已加载模块
        df = akshare.tool_trade_date_hist_sina()
        import pandas as pd
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        dates = pd.to_datetime(df[col]).dt.strftime("%Y-%m-%d")
        box.append(sorted(dates.unique()))

    import concurrent.futures as _cf
    ex = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="cal")
    try:
        fut = ex.submit(_run)
        fut.result(timeout=_FETCH_TIMEOUT)
        return box[0] if box else None
    except Exception:  # noqa: BLE001 —— 网络/解析失败一律降级
        return None
    finally:
        ex.shutdown(wait=False)


def _load_dates(force_refresh: bool = False) -> list[str] | None:
    """日历加载：内存 → 缓存文件 → 网络。返回 None 表示完全无日历（降级）。"""
    global _DATES, _DATES_TS
    now = time.time()
    with _lock:
        if _DATES is not None and not force_refresh and now - _DATES_TS < _CAL_TTL:
            _LAST_LOAD["ok"] = True
            return _DATES

    dates: list[str] | None = None
    ts = 0.0
    try:  # ② 本地缓存文件（未过期直接用；过期也先兜底）
        with open(_CAL_PATH, encoding="utf-8") as f:
            j = json.load(f)
        dates, ts = list(j.get("dates") or []), float(j.get("ts") or 0)
    except Exception:  # noqa: BLE001
        dates, ts = None, 0.0

    if dates and not force_refresh and now - ts < _CAL_TTL:
        with _lock:
            _DATES, _DATES_TS = dates, ts
            _LAST_LOAD["ok"] = True
        return dates

    if dates is None or now - ts >= _CAL_TTL:  # ③ 网络刷新（无文件或已过期）
        fresh = _fetch_dates()
        if fresh:
            try:
                os.makedirs(os.path.dirname(_CAL_PATH), exist_ok=True)
                with open(_CAL_PATH, "w", encoding="utf-8") as f:
                    json.dump({"ts": now, "dates": fresh}, f, ensure_ascii=False)
            except Exception:  # noqa: BLE001 —— 落盘失败不影响本次使用
                pass
            with _lock:
                _DATES, _DATES_TS = fresh, now
                _LAST_LOAD["ok"] = True
            return fresh

    if dates:  # ④ 过期缓存兜底（网络失败时宁用旧日历）
        with _lock:
            _DATES, _DATES_TS = dates, ts
            _LAST_LOAD["ok"] = True
        return dates

    _LAST_LOAD["ok"] = False  # 完全无日历 → 降级（调用方放弃判定，不误报）
    return None


def last_finished_trade_date(now: datetime | None = None) -> str | None:
    """最近已完成交易日（YYYY-MM-DD）。无日历时返回 None（调用方跳过判定）。"""
    dates = _load_dates()
    if not dates:
        return None
    now = now or datetime.now()
    t = now.strftime("%Y-%m-%d")
    i = bisect.bisect_left(dates, t)
    if i < len(dates) and dates[i] == t and now.hour >= _DATA_READY_HOUR:
        return t  # 今天是交易日且已过数据就绪时刻
    return dates[i - 1] if i > 0 else None  # 严格早于今天的最近交易日


def prev_trade_date(d) -> str | None:
    """严格早于 d 的最近一个交易日（YYYY-MM-DD）。无日历 → None。

    d 可为 'YYYY-MM-DD' 字符串或 date/datetime；d 本身是否是交易日均可
    （bisect 定位后取前一项）。供「快照补最新一根」判定候选股使用。
    """
    dates = _load_dates()
    if not dates:
        return None
    t = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
    i = bisect.bisect_left(dates, t)
    return dates[i - 1] if i > 0 else None


def spot_ready_date(now: datetime | None = None) -> str | None:
    """实时行情快照（腾讯 qt 等）当前所代表的「静态收盘交易日」；盘中/不确定 → None。

    与 last_finished_trade_date 的口径区别：后者按**日线接口**的保守更新延迟
    （20:00 阈值）判定；实时快照接口在 15:00 收盘即定格为完整数据。本函数
    只描述快照本身，**是否可用于补写日K由调用方另做对齐与连续性校验**
    （见 sync._spot_backfill_daily 的三层防错）。

    - 交易日 15:05 后 → 今日；交易日 9:00 前 → 上一交易日（快照仍显示昨收定格）
    - 交易日 9:00~15:05（含集合竞价）→ None（盘中未定格）
    - 非交易日全天 → 最近交易日（严格早于今天）
    """
    dates = _load_dates()
    if not dates:
        return None
    now = now or datetime.now()
    t = now.strftime("%Y-%m-%d")
    i = bisect.bisect_left(dates, t)
    if not (i < len(dates) and dates[i] == t):   # 今天非交易日 → 快照=最近收盘日
        return dates[i - 1] if i > 0 else None
    hhmm = now.hour * 100 + now.minute
    if hhmm < 900:                                # 盘前：快照=上一交易日定格
        return dates[i - 1] if i > 0 else None
    if hhmm >= 1505:                              # 收盘后：快照=今日完整数据
        return t
    return None                                   # 盘中（含竞价）：未定格


def is_stale(local_max: str | None, now: datetime | None = None) -> bool:
    """本地最新日期是否落后于最近已完成交易日。local_max 为 None / 日历降级 → False。"""
    if not local_max:
        return False
    last = last_finished_trade_date(now)
    if not last:
        return False
    return str(local_max) < last
