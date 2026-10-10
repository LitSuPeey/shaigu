# -*- coding: utf-8 -*-
"""FastAPI 路由：覆盖范围/同步/五大模块/股票搜索。"""
import asyncio
import math
import os
import re
import sqlite3
from typing import Optional

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from ..core import db as core_db
from ..core import ranges
from ..core import sync as core_sync
from ..modules import (alert, ant, ant1000, antsim, beton, bili, conditions, cycle,
                       futures, pattern, reverse9, similar, snap, strategies, ticker)

router = APIRouter()


def _clean(v):
    """递归清洗 NaN/Inf/numpy 标量，输出 JSON 兼容值。"""
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    try:
        if hasattr(v, "item") and not hasattr(v, "__len__"):
            v = v.item()
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            return v
    except Exception:
        pass
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def clean_records(records):
    """对 to_dict(orient='records') 的结果做 NaN 清洗。"""
    return [_clean(r) for r in records]


def _norm_exchange(s: Optional[str]) -> Optional[str]:
    """统一范围字段：大写 SZ/SH/BJ，否则 None（全部）。修复前端小写 vs 后端大写的 bug。"""
    if not s:
        return None
    u = str(s).upper().strip()
    return u if u in ("SZ", "SH", "BJ") else None


# ============ 运行进度（供前端轮询 /api/progress 显示加载进度条） ============
import threading as _th
from ..core.taskctl import CANCEL, TaskCancelled
_RUN_LOCK = _th.Lock()
_RUN_STATE = {"status": "idle", "frac": 0.0, "msg": "", "task": ""}


def _task_start(name: str) -> None:
    global _RUN_STATE
    CANCEL.reset()
    with _RUN_LOCK:
        _RUN_STATE = {"status": "running", "frac": 0.0, "msg": name, "task": name}


def _task_done() -> None:
    with _RUN_LOCK:
        _RUN_STATE["status"] = "idle"
        _RUN_STATE["frac"] = 1.0
        _RUN_STATE["msg"] = "完成"


def _task_progress(a, b, msg) -> None:
    """统一进度回调：(a/b) 比例 + 文案；检测取消请求。"""
    with _RUN_LOCK:
        if _RUN_STATE["status"] == "running":
            _RUN_STATE["frac"] = (float(a) / float(b)) if b else 0.0
            _RUN_STATE["msg"] = str(msg)
    CANCEL.check()  # 取消时抛 TaskCancelled 终止计算


@router.get("/api/progress")
def run_progress():
    with _RUN_LOCK:
        return dict(_RUN_STATE)


@router.post("/api/cancel")
def cancel_running():
    """用户请求取消正在运行的筛选/扫描任务。"""
    CANCEL.set()
    with _RUN_LOCK:
        running = _RUN_STATE["status"] == "running"
        if running:
            _RUN_STATE["msg"] = "正在取消…"
    return {"ok": True, "running": running}


def _cancelled_response():
    return {"cancelled": True, "rows": [], "results": [], "msg": "已取消"}


# ============ 基础状态 / 配置 ============
@router.get("/api/status")
def status():
    conn = core_db.writer()
    core_db.ensure_schema(conn)
    s = core_db.db_stats(conn)
    # 上次同步时间（数据留存本地 + 增量同步的可视化佐证）
    try:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key='last_sync'").fetchone()
        s["last_sync"] = row[0] if row else None
    except Exception:  # noqa: BLE001 —— sync_state 尚未建立时不影响状态接口
        s["last_sync"] = None
    sync_snap = core_sync.STATE.snapshot()
    return {
        "db_path": core_db.db_path(),
        "stats": s,
        "sync": sync_snap,
        "sectors": ranges.sector_choices(),
    }


class SyncReq(BaseModel):
    exchange: Optional[str] = None  # SZ/SH/BJ/None
    sectors: Optional[list[str]] = None
    kinds: list[str] = ["daily", "valuation", "dividend"]
    with_valuation: bool = True  # 是否同步估值水平数据（PE/PB 等），默认开启
    with_dividend: bool = True   # 是否同步分红股息数据，默认开启
    force: bool = False
    max_workers: int = 12


@router.post("/api/sync/start")
def sync_start(req: SyncReq, bg: BackgroundTasks):
    if core_sync.STATE.status == "running":
        raise HTTPException(409, "已有同步任务在运行中")
    # 根据 with_valuation / with_dividend 过滤 kinds
    kinds = list(req.kinds)
    if not req.with_valuation and "valuation" in kinds:
        kinds.remove("valuation")
    if not req.with_dividend and "dividend" in kinds:
        kinds.remove("dividend")
    bg.add_task(
        core_sync.sync_all,
        _norm_exchange(req.exchange), req.sectors,
        tuple(k for k in kinds if k in ("daily", "valuation", "dividend")),
        req.force, req.max_workers,
    )
    return {"ok": True, "msg": "已启动同步任务"}


@router.post("/api/sync/stop")
def sync_stop():
    """停止当前正在运行的同步任务（已运行的少数请求会自然收尾，写入幂等无副作用）。"""
    core_sync.STATE.request_cancel()
    return {"ok": True, "msg": "已请求停止同步"}


@router.post("/api/sync/refresh_meta")
def refresh_meta(bg: BackgroundTasks):
    if core_sync.STATE.status == "running":
        raise HTTPException(409, "已有同步任务在运行中")
    bg.add_task(_refresh_meta_task)
    return {"ok": True, "msg": "已启动 meta 刷新任务"}


def _refresh_meta_task():
    core_sync.STATE.status = "running"
    core_sync.STATE.frac = 0.0
    core_sync.STATE.msg = "刷新 meta 与板块…"
    try:
        core_sync.refresh_meta_and_sectors(refresh_sector=False)
        core_sync.STATE.status = "done"
        core_sync.STATE.msg = "meta 刷新完成"
    except Exception as e:
        core_sync.STATE.status = "failed"
        core_sync.STATE.msg = f"meta 刷新失败: {e}"


@router.post("/api/snapshot/build")
def snapshot_build(bg: BackgroundTasks):
    """手动重建快照表（估值/股息/最新行情预计算）。"""
    from ..core import snapshot as _snap
    if core_sync.STATE.status == "running":
        raise HTTPException(409, "已有同步任务在运行中")
    bg.add_task(_build_snapshot_task)
    return {"ok": True, "msg": "已启动快照构建任务"}


@router.get("/api/snapshot/status")
def snapshot_status():
    from ..core import snapshot as _snap
    return {"ready": _snap.snapshot_ready()}


def _build_snapshot_task():
    from ..core import snapshot as _snap
    core_sync.STATE.status = "running"
    core_sync.STATE.frac = 0.0
    core_sync.STATE.msg = "构建快照表…"
    try:
        stats = _snap.build_snapshot(
            progress_cb=lambda f, t, m: setattr(core_sync.STATE, "frac", f) or
            setattr(core_sync.STATE, "msg", f"快照 {m}"))
        core_sync.STATE.status = "done"
        core_sync.STATE.msg = f"快照完成（{stats.get('rows', 0)} 行）"
    except Exception as e:
        core_sync.STATE.status = "failed"
        core_sync.STATE.msg = f"快照构建失败: {e}"


@router.get("/api/sync/progress")
def sync_progress():
    return core_sync.STATE.snapshot()


# ============ 分红/财报时点预测 ============
@router.get("/api/divtiming/week")
def divtiming_week(date_str: Optional[str] = None):
    """指定日期所在周的分红候选清单（默认今天）。date_str=YYYY-MM-DD。"""
    try:
        from ..modules import divtiming
        import datetime as _dt
        ws = None
        if date_str:
            ws = _dt.datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        ws = ws or _dt.date.today()
        conn = core_db.reader()
        r = divtiming.candidates(conn, ws)
        r.pop("profiles", None)      # 画像太大，接口不返回
        cand = r.get("candidates") or {}
        # 取股票名（meta），前端展示用
        name_map = {}
        try:
            for c, nm in conn.execute("SELECT code, name FROM meta"):
                name_map[c] = nm
        except Exception:  # noqa: BLE001
            pass
        items = [{"code": c, "name": name_map.get(c, ""), **v}
                 for c, v in cand.items()]
        items.sort(key=lambda x: (-(x.get("score") or 0), x["code"]))
        # 附近 9 周（含本周）候选数，供周切换（一次画像批量算）
        import datetime as _d2
        weeks = []
        monday = ws - _d2.timedelta(days=ws.weekday())
        we = divtiming.weeks_estimate(conn, ws, span=4)
        for k in range(-4, 5):
            st = monday + _d2.timedelta(days=7 * k)
            key = st.strftime("%Y-%m-%d")
            weeks.append({
                "start": key,
                "label": ("本周" if k == 0 else f"{st.month}/{st.day}"),
                "n": we.get(key, 0),
                "peak": st.month in (5, 6, 7),
            })
        notes = r.get("notes") or {}
        note_list = []
        if notes.get("never_dividend"):
            note_list.append(f"规则b：从未分红剔除 {notes['never_dividend']} 只")
        if notes.get("dropped_loss"):
            note_list.append(f"规则a：近{divtiming.DEFAULTS['loss_years']}年亏损剔除 {notes['dropped_loss']} 只")
        if notes.get("dropped_recent"):
            note_list.append(f"规则c：本周期已分红跳过 {notes['dropped_recent']} 只")
        return _clean({
            "ok": True, "week_start": ws.strftime("%Y-%m-%d"),
            "candidates": items, "notes": note_list,
            "weeks": weeks, "raw_notes": notes,
        })
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"候选计算失败：{e}", "candidates": [], "notes": [], "weeks": []}


@router.get("/api/divtiming/full_market")
def divtiming_full_market():
    """规则 d 状态：本年度是否已做「2 月底全市场增量」。"""
    try:
        from ..modules import divtiming
        conn = core_db.reader()
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key='last_div_full_market'").fetchone()
        active = divtiming.should_full_market(conn)
        last = row[0] if row else None
        note = ('每年 2 月底的「全市场增量」窗口已开启，下次同步将全市场拉取一次分红数据。'
                if active else
                (f"本年度已于 {last} 完成全市场增量；下次窗口为明年 2 月底。" if last
                 else "未到「每年 2 月底」全市场增量窗口（该窗口每年仅需一次）。"))
        return _clean({"ok": True, "active": active, "last_run": last, "note": note})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.get("/api/divtiming/stats")
def divtiming_stats():
    """分红习惯画像统计 + 全年各月预计候选数（用本地历史推断，不联网）。"""
    try:
        from ..modules import divtiming
        conn = core_db.reader()
        r = divtiming.stats(conn)
        return _clean({
            "ok": True,
            "profiles": r.get("profiles", 0),
            "kinds": r.get("kinds", {}),
            "weekly_estimate": r.get("weekly_estimate", {}),
            "typical_months": r.get("typical_months", {}),
        })
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"画像失败：{e}"}



# ============ 数据新鲜度（滞后提示，前端一天最多显示一次） ============
@router.get("/api/data/freshness")
def data_freshness(debug_stale: bool = False):
    """本地 000001 最新日线日期 vs 最近已完成交易日（含节假日/盘中判定）。

    stale=True 表示本地数据落后（建议同步）；日历降级或本地无 000001 时
    stale 恒为 False（宁可不提示也不在节假日误报）。debug_stale 仅供 UI 验证。"""
    local_max = None
    try:
        rconn = core_db.reader()
        row = rconn.execute(
            "SELECT MAX(date) FROM daily WHERE code='000001'").fetchone()
        local_max = row[0] if row and row[0] else None
    except Exception:  # noqa: BLE001
        local_max = None
    from ..core import trade_cal
    last_td = None
    try:
        last_td = trade_cal.last_finished_trade_date()
    except Exception:  # noqa: BLE001
        last_td = None
    stale = bool(debug_stale) or (
        local_max is not None and last_td is not None and str(local_max) < last_td)
    return _clean({"local_max": local_max, "last_trade_date": last_td,
                   "stale": stale, "degraded": trade_cal.degraded()})


# ============ 股票查询 ============
@router.get("/api/stocks")
def stocks(exchange: Optional[str] = None,
           sectors: Optional[str] = None,
           q: Optional[str] = None,
           limit: int = 200):
    """按范围 + 关键词查询股票。q 匹配代码/名称前缀。"""
    sec = [s for s in (sectors or "").split(",") if s] if sectors else None
    df = ranges.filter_meta_df(_norm_exchange(exchange), sec)
    if q:
        q = str(q).strip()
        df = df[df["code"].astype(str).str.contains(q, na=False)
                | df["name"].astype(str).str.contains(q, na=False)]
    df = df.head(int(limit))
    return df.to_dict(orient="records")


@router.get("/api/kline")
def kline(code: str, days: int = 120):
    rconn = core_db.reader()
    # 多取 60 根作均线预热段：MA60 需 60 根收盘价才可计算，
    # 若只取 days 根，显示区间前段 MA 线必然缺角。前端只显示最后 days 根，
    # MA 用全量（含预热段）计算，保证显示窗口内四条均线全程可画。
    df = pd.read_sql_query(
        f"SELECT date, open, high, low, close, volume, amount "
        f"FROM daily WHERE code=? ORDER BY date DESC LIMIT ?",
        rconn, params=(str(code).zfill(6), int(days) + 60))
    if df.empty:
        return {"code": code, "bars": []}
    df = df.iloc[::-1].reset_index(drop=True)
    return {
        "code": code,
        "display_days": int(days),
        "bars": df.assign(date=df["date"].astype(str)).to_dict(orient="records"),
    }


# ============ 策略选股 ============
# 未显式指定策略时的默认策略（避免「点一下就 6 个全跑」的隐性慢）
DEFAULT_STRATEGY = "turtle"


@router.get("/api/strategies/list")
def strategies_list():
    return [{"key": k, "label": v["label"]}
            for k, v in strategies.STRATEGIES.items()]


class StrategyReq(BaseModel):
    keys: Optional[list[str]] = None
    strategy: Optional[str] = None
    exchange: Optional[str] = None
    sectors: Optional[list[str]] = None
    limit: int = 0
    params: Optional[dict] = None
    max_workers: int = 8


@router.post("/api/strategies/run")
def strategies_run(req: StrategyReq):
    # 兼容前端两种写法：strategy="all"|单 key，或者 keys=[...]
    keys = req.keys or []
    if not keys and req.strategy and req.strategy != "all":
        keys = [req.strategy]
    if not keys:
        # 未指定时**只跑 1 个**默认策略，而不是全部 6 个。
        # 旧行为 list(STRATEGIES.keys()) 会让「随便点一下运行」= 6 个策略全跑
        # （实测约 28s），用户感知就是「分析变慢」。前端下拉默认选
        # 「全部一起跑」时仍会显式传 6 个 key，功能不受影响。
        keys = [DEFAULT_STRATEGY]
    keys = [k for k in keys if k in strategies.STRATEGIES] or [DEFAULT_STRATEGY]

    _task_start("策略扫描")
    try:
        out = {}
        exchange = _norm_exchange(req.exchange)
        # 一次性批量预取行情：6 个策略共享同一份日K缓存。
        # 原实现每策略各自「逐只 SELECT」→ 5554 只 × 6 策略 = 3.3 万次查询；
        # 现在只查 1 次，6 个策略全跑从 ~28s 降到 ~5s（实测见 .workbuddy/）。
        strategies.prepare_bars()
        for i, k in enumerate(keys):
            _task_progress(i, max(len(keys), 1),
                           f"策略 {strategies.STRATEGIES.get(k, {}).get('label', k)}")
            if k in strategies.STRATEGIES:
                try:
                    fn_kwargs = dict(exchange=exchange, sectors=req.sectors)
                    # limit/params 不同策略内部签名不同，先按需传入
                    if req.limit and req.limit > 0:
                        try:
                            rows = strategies.STRATEGIES[k]["fn"](limit=req.limit, **fn_kwargs)
                        except TypeError:
                            rows = strategies.STRATEGIES[k]["fn"](**fn_kwargs)
                    else:
                        rows = strategies.STRATEGIES[k]["fn"](**fn_kwargs)
                    if req.limit and req.limit > 0 and isinstance(rows, list):
                        rows = rows[: req.limit]
                    rows = clean_records(rows or [])
                    out[k] = {"label": strategies.STRATEGIES[k]["label"], "rows": rows}
                except TaskCancelled:
                    raise
                except Exception as e:
                    out[k] = {"label": strategies.STRATEGIES[k]["label"], "rows": [], "error": str(e)}
    except TaskCancelled:
        return _cancelled_response()
    finally:
        strategies.clear_bars()   # 释放行情缓存，避免长期占用内存
        _task_done()
    # 前端期望 rows/elapsed，汇总扁平返回
    flat = []
    for k, v in out.items():
        for r in v["rows"]:
            r = dict(r)
            r["strategy"] = v["label"].split(" ", 1)[-1] if " " in v["label"] else v["label"]
            flat.append(r)
    return {"rows": flat, "by_strategy": out}


# ============ 条件筛选 ============
@router.get("/api/conditions/list")
def conditions_list():
    out = []
    for k, v in conditions.CONDITIONS.items():
        out.append({"key": k, "label": v["label"], "desc": v["desc"]})
    return out


@router.get("/api/conditions/presets")
def conditions_presets():
    """预设组合（A/B）：返回条件 keys、推荐阈值 cfg、实测回测披露。"""
    return {"presets": conditions.preset_list(),
            "defaults": dict(conditions.DEFAULTS)}


@router.get("/api/conditions/defaults")
def conditions_defaults():
    """默认阈值（前端「恢复默认」按钮的权威来源，避免前端硬编码漂移）。"""
    return {"defaults": dict(conditions.DEFAULTS)}


class ConditionReq(BaseModel):
    keys: list[str]
    cfg: dict = {}
    exchange: Optional[str] = None
    range: Optional[str] = None
    sectors: Optional[list[str]] = None
    max_workers: int = 16


@router.post("/api/conditions/run")
def conditions_run(req: ConditionReq):
    if not req.keys:
        return {"rows": [], "hint": "未选择条件"}
    _task_start("条件筛选")
    try:
        df = conditions.run(req.keys, req.cfg,
                            exchange=_norm_exchange(req.exchange or req.range),
                            sectors=req.sectors,
                            max_workers=req.max_workers,
                            progress_cb=_task_progress)
        return {"rows": clean_records(df.to_dict(orient="records"))}
    except TaskCancelled:
        return _cancelled_response()
    finally:
        _task_done()


# ============ 形态打分 ============
class PatternReq(BaseModel):
    min_score: float = 45.0          # 「匹配度」(raw_score) 下限，底部/顶部同尺度
    only_bottom: bool = True
    only_top: bool = False           # 只看顶部形态（逃顶）；与 only_bottom 互斥时以其为准
    exchange: Optional[str] = None
    sectors: Optional[list[str]] = None
    max_workers: int = 8


@router.post("/api/pattern/run")
def pattern_run(req: PatternReq):
    _task_start("形态打分")
    try:
        exchange = _norm_exchange(req.exchange)
        results, stats = pattern.run(req.min_score, req.only_bottom,
                                     exchange, req.sectors,
                                     req.max_workers, req.only_top,
                                     progress_cb=_task_progress)
        results = _clean(results) if isinstance(results, dict) else _clean(results)
        stats = _clean(stats)
        return {"results": results, "skip_stats": stats}
    except TaskCancelled:
        return _cancelled_response()
    finally:
        _task_done()


# ============ 形态打分 · 短线快拍 ============
# 4 个子模式：A 周期震荡 / B 即将大涨 / C 反弹动能 / D 即将反弹，可多选。
# 门槛用全市场横截面分位数（不照搬研究报告的绝对阈值），年线一票否决可关。
class SnapReq(BaseModel):
    cfg: dict = {}
    exchange: Optional[str] = None
    sectors: Optional[list[str]] = None
    max_workers: int = 8


@router.get("/api/snap/meta")
def snap_meta():
    """子模式清单与默认参数（前端渲染用）。"""
    return {"modes": [{"key": k, "label": v} for k, v in snap.MODE_NAMES.items()],
            "defaults": dict(snap.DEFAULTS)}


@router.post("/api/snap/run")
def snap_run(req: SnapReq):
    _task_start("短线快拍")
    try:
        results, stats = snap.run(req.cfg, _norm_exchange(req.exchange),
                                  req.sectors, req.max_workers,
                                  progress_cb=_task_progress)
        return {"results": _clean(results), "skip_stats": _clean(stats)}
    except TaskCancelled:
        return _cancelled_response()
    finally:
        _task_done()


# ============ 9Reverse9 · 神奇九转「即将触发」+ 历史反转时点统计 ============
# 程序一：全市场筛「即将到达买入九转」（还差天数可调）；
# 程序二：对这些股票拉长历史，定位历史九转信号之后/之前真正反转的时间节点 n / m。
class Reverse9Req(BaseModel):
    cfg: dict = {}
    exchange: Optional[str] = None
    sectors: Optional[list[str]] = None


@router.get("/api/r9/meta")
def reverse9_meta():
    """默认参数与指标口径说明（前端渲染用）。"""
    return _clean(reverse9.meta_info())


@router.post("/api/r9/run")
def reverse9_run(req: Reverse9Req):
    _task_start("9Reverse9 扫描")
    try:
        return _clean(reverse9.run(req.cfg, _norm_exchange(req.exchange),
                                   req.sectors, progress_cb=_task_progress))
    except TaskCancelled:
        return {**_cancelled_response(), "results": [], "skip_stats": {}}
    except Exception as e:  # noqa: BLE001 —— 数据/文件异常返回 JSON 错误而非 500
        return {"error": f"9Reverse9 扫描失败: {e}", "results": [],
                "skip_stats": {}, "notes": [str(e)[:200]], "elapsed": 0.0}
    finally:
        _task_done()


# ============ Bet on · 长期布局（年度级趋势） ============
# 素材：用户提供的两份长周期量化研究档案（run1 识别手册 + run2 买入侧信号 + 两次独立复核）。
# 两轮研究的最终结论都是 RESEARCH_REJECTED，本模块把其中唯一未被否掉的「状态基线」
# （STATE_ABOVE 120 日胜率 67.94% / 最大不利偏移 −10.69%）当主锚，并把被证伪的
# 「启动前埋伏」档明确标注为低置信度 —— 详见 app/modules/beton.py 顶部说明。
class BetonReq(BaseModel):
    cfg: dict = {}
    exchange: Optional[str] = None
    sectors: Optional[list[str]] = None
    max_workers: int = 8


@router.get("/api/beton/meta")
def beton_meta():
    """状态表（含实测胜率）/ 买点档位 / 权重 / 默认参数（前端渲染与文档用）。"""
    try:
        return _clean(beton.meta_info())
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "states": [], "tiers": [], "weights": []}


@router.post("/api/beton/run")
def beton_run(req: BetonReq):
    """全市场长期布局扫描（长历史 + PIT 状态 + 历史周期 + 卖出纪律）。"""
    _task_start("Bet on 长期布局")
    try:
        return _clean(beton.run(req.cfg, _norm_exchange(req.exchange),
                                req.sectors, req.max_workers,
                                progress_cb=_task_progress))
    except TaskCancelled:
        return {**_cancelled_response(), "results": [], "skip_stats": {}, "stats": {}}
    except Exception as e:  # noqa: BLE001 —— 数据/文件异常返回 JSON 错误而非 500
        return {"error": f"Bet on 扫描失败: {e}", "results": [],
                "skip_stats": {}, "stats": {}, "notes": [str(e)[:300]], "elapsed": 0.0}
    finally:
        _task_done()


@router.get("/api/beton/kline")
def beton_kline(code: str = Query(...), years: int = 12):
    """单只股票的长期走势图数据：月线收盘 + 历史上涨段区间 + MA120/MA250。"""
    try:
        return _clean(beton.chart_data(code, {"hist_years": int(years)}))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "bars": [], "dates": []}


# ============ 蚂蚁呀欸 ============
class AntReq(BaseModel):
    cfg: dict = {}
    exchange: Optional[str] = None
    sectors: Optional[list[str]] = None
    only_pass: bool = True


@router.post("/api/ant/run")
def ant_run(req: AntReq):
    progress = {"frac": 0.0, "msg": "蚂蚁扫描启动"}
    logs = []

    def cb(done, total, msg):
        progress["frac"] = done / max(total, 1)
        progress["msg"] = msg
        _task_progress(done, total, msg)

    _task_start("蚂蚁扫描")
    try:
        df = ant.scan(req.cfg, _norm_exchange(req.exchange),
                      req.sectors, req.only_pass, progress_cb=cb, logs=logs)
        return {
            "rows": clean_records(df.to_dict(orient="records")),
            "logs": logs,
            "progress": _clean(progress),
        }
    except TaskCancelled:
        return _cancelled_response()
    finally:
        _task_done()


# ============ 蚂蚁呀欸 · 样本形态匹配（v2，核心样本为中心） ============
class AntsimReq(BaseModel):
    cfg: dict = {}
    strict: float = 100.0          # 严格程度 50~200（%）
    exchange: Optional[str] = None
    sectors: Optional[list[str]] = None


@router.post("/api/antsim/run")
def antsim_run(req: AntsimReq):
    """样本形态匹配：走势与样本相似 + 周期刚刚启动 / 周期进行中 分类。"""
    progress = {"frac": 0.0, "msg": "样本形态匹配启动"}
    logs = []

    def cb(done, total, msg):
        progress["frac"] = done / max(total, 1)
        progress["msg"] = msg
        _task_progress(done, total, msg)

    _task_start("蚂蚁样本形态匹配")
    try:
        out = antsim.run(req.cfg, _norm_exchange(req.exchange), req.sectors,
                         strict=req.strict, progress_cb=cb, logs=logs)
        out["logs"] = logs
        out["progress"] = _clean(progress)
        return _clean(out)
    except TaskCancelled:
        return _cancelled_response()
    except Exception as e:  # noqa: BLE001 —— 数据/文件异常返回 JSON 错误而非 500
        return {"error": f"样本形态匹配失败: {e}", "rows": [], "stats": {},
                "templates": [], "notes": [str(e)[:300]]}
    finally:
        _task_done()


@router.get("/api/antsim/samples")
def antsim_samples():
    """样本清单（三组：核心/多元/同期）与权重。"""
    try:
        return _clean(antsim.samples_info())
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "groups": []}


class AntsimSamplesReq(BaseModel):
    group: str
    text: str = ""


@router.post("/api/antsim/samples")
def antsim_samples_save(req: AntsimSamplesReq):
    """写回「多元」或「同期」样本文件（核心组为硬规则，只读）。"""
    try:
        return _clean(antsim.save_samples_text(req.group, req.text))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ============ 持有个1000年试试呢？（长周期版蚂蚁） ============
class Ant1000Req(BaseModel):
    cfg: dict = {}
    years: int = 20
    exchange: Optional[str] = None
    sectors: Optional[list[str]] = None
    only_pass: bool = True
    auto_fetch: bool = True


@router.post("/api/ant1000/run")
def ant1000_run(req: Ant1000Req):
    progress = {"frac": 0.0, "msg": "长周期扫描启动"}

    def cb(done, total, msg):
        progress["frac"] = done / max(total, 1)
        progress["msg"] = msg
        _task_progress(done, total, msg)

    _task_start("长周期扫描")
    try:
        df = ant1000.scan(req.cfg, req.years, _norm_exchange(req.exchange),
                          req.sectors, req.only_pass, req.auto_fetch,
                          progress_cb=cb)
        return {
            "rows": clean_records(df.to_dict(orient="records")),
            "progress": _clean(progress),
            "cache": _clean(ant1000.cache_status(req.cfg)),
        }
    except TaskCancelled:
        return _cancelled_response()
    finally:
        _task_done()


@router.get("/api/ant1000/cache")
def ant1000_cache():
    return _clean(ant1000.cache_status())


@router.get("/api/ant1000/kline")
def ant1000_kline(code: str = Query(...), years: int = 20):
    """单只股票长周期月线图数据（含循环顶底与指数对比）。"""
    return _clean(ant1000.chart_data(code, years))


# ============ 相似股 ============
class SimilarReq(BaseModel):
    target_code: Optional[str] = None
    target: Optional[str] = None
    ref_days: int = 30
    max_lag: int = 10
    top_n: int = 2
    vol_mode: str = "ignore"
    vol_threshold: float = 50.0
    markets: Optional[list[str]] = None
    range: Optional[str] = None
    exchange: Optional[str] = None
    exclude_st: bool = True


@router.post("/api/similar/run")
def similar_run(req: SimilarReq):
    progress = {"frac": 0.0, "msg": "启动"}

    def cb(frac, _total, msg):
        progress["frac"] = frac
        progress["msg"] = msg
        _task_progress(frac, 1.0, msg)

    target = req.target_code or req.target
    if not target:
        return {"target": {"code": "", "name": ""}, "a": [], "b": [], "c": [], "error": "请提供 target_code 或 target"}
    exchange = _norm_exchange(req.exchange or req.range)
    _task_start("相似股匹配")
    try:
        result = similar.find_similar(
            target,
            ref_days=req.ref_days,
            max_lag=req.max_lag,
            top_n=req.top_n,
            vol_mode=req.vol_mode,
            vol_threshold=req.vol_threshold,
            markets=[exchange] if exchange else req.markets,
            exclude_st=req.exclude_st,
            progress_cb=cb,
        )
        return _clean(result)
    except TaskCancelled:
        return _cancelled_response()
    finally:
        _task_done()


# ============ 前瞻预警 ============
class AlertReq(BaseModel):
    cfg: dict = {}
    sections: Optional[list[str]] = None  # 默认全跑


@router.post("/api/alert/run")
def alert_run(req: AlertReq):
    _task_start("前瞻预警")
    try:
        result = alert.scan(req.cfg, req.sections, progress_cb=_task_progress)
        return _clean(result)
    except TaskCancelled:
        return _cancelled_response()
    except Exception as e:  # 网络/数据源故障时返回 JSON 错误而非 500，前端可提示
        return {"error": f"前瞻预警失败: {e}", "notes": [str(e)[:200]],
                "date": None, "sections": [], "signals": [], "elapsed": 0.0}
    finally:
        _task_done()


class AlertReportReq(BaseModel):
    result: dict
    cfg: Optional[dict] = None


@router.post("/api/alert/report")
def alert_report(req: AlertReportReq):
    """把前端已取得的预警结果渲染成独立网页报告（2026 重制版模板）。"""
    try:
        return {"html": alert.build_report_html(req.result, cfg=req.cfg)}
    except Exception as e:
        raise HTTPException(500, f"报告生成失败: {e}")


# ============ 快讯轮播条 ============
@router.get("/api/ticker")
def ticker_items(refresh: bool = False):
    """横向轮播条数据：近日快讯 + 关键词 + 偶发个股/大盘行情（后端缓存 5 分钟）。
    refresh=1 时跳过缓存强制重建（供「刷新字幕」按钮使用）。"""
    try:
        return _clean(ticker.build_items({"cache_ttl": 0} if refresh else None))
    except Exception as e:  # noqa: BLE001 —— 轮播条失败不影响主功能
        return {"items": [], "notes": [f"快讯获取失败: {str(e)[:120]}"]}


# ============ 大周期通道（830000 平均股价，可复用模块） ============
class CycleReq(BaseModel):
    source: str = "ths830000"
    cfg: dict = {}
    forecast_years: int = 3   # 走势外推年数（1~14）


@router.post("/api/cycle/analyze")
def cycle_analyze(req: CycleReq):
    """月K线 + 上升通道上下轨 + 周期高低位/长期趋势判定 + 建议 + 未来走势虚线预测。"""
    try:
        return _clean(cycle.run(req.source, req.cfg, forecast_years=req.forecast_years))
    except Exception as e:  # noqa: BLE001
        return {"error": f"大周期分析失败: {e}", "notes": [str(e)[:150]]}


# ============ 期货视察（主连多因子打分） ============
class FuturesReq(BaseModel):
    cfg: dict = {}


@router.post("/api/futures/scan")
def futures_scan(req: FuturesReq):
    """期货视察：只拉各品种主连（主力合约），在最近 n 周窗口内多因子打分。"""
    _task_start("期货视察")
    try:
        result = futures.scan(req.cfg, progress_cb=_task_progress)
        return _clean(result)
    except TaskCancelled:
        return {**_cancelled_response(), "top": [], "bottom": [], "news": []}
    except Exception as e:  # noqa: BLE001 —— 网络/数据源故障时返回 JSON 错误而非 500
        return {"error": f"期货视察失败: {e}", "rows": [], "top": [], "bottom": [],
                "news": [], "sectors": [], "notes": [str(e)[:200]], "elapsed": 0.0}
    finally:
        _task_done()


@router.get("/api/futures/sectors")
def futures_sectors():
    """板块字典（前端筛选下拉用）。"""
    return [{"key": k, "label": v} for k, v in futures.SECTORS.items()]


# ============ 同花顺链接 ============
THS_URL = "https://stockpage.10jqka.com.cn/{code}/"


def ths_link(code: str, name: str) -> str:
    """同花顺个股页链接。"""
    url = THS_URL.format(code=code)
    from markupsafe import escape  # noqa
    name = str(name)
    return f'<a href="{url}" target="_blank" rel="noopener" class="ths-link">{name}</a>'

# ============ 仅供学习（B站 UP主内容挖掘） ============
# 安全约束（详见 app/modules/bili.py 顶部说明）：全程串行 + 最小间隔限速 +
# 单轮请求硬闸；不下载音视频、不做 ASR、不读会员/充电专属、不自动登录。
class BiliUpReq(BaseModel):
    uid: str


class BiliUpRenameReq(BaseModel):
    uid: str
    name: str = ""      # 留空 = 清除自定义名，回退到自动昵称


class BiliScanReq(BaseModel):
    uids: list[str] = []
    cfg: dict = {}


class BiliCredReq(BaseModel):
    sessdata: str = ""


@router.get("/api/bili/meta")
def bili_meta():
    """模块默认参数、安全设置、会话与凭据状态。"""
    return _clean(bili.meta_info())


@router.get("/api/bili/ups")
def bili_ups():
    """已保存的 UP主 列表（本地复用）。"""
    return _clean({"ups": bili.load_ups()})


@router.post("/api/bili/ups/add")
def bili_up_add(req: BiliUpReq):
    """保存/更新一个 UP主（会尝试读取昵称，失败也照样存档）。

    自定义名称统一走 POST /api/bili/ups/rename（对已保存的 UP主 命名）。
    """
    _task_start("保存 UP主")
    try:
        return _clean(bili.add_up(req.uid))
    finally:
        _task_done()


@router.post("/api/bili/ups/remove")
def bili_up_remove(req: BiliUpReq):
    return _clean(bili.remove_up(req.uid))


@router.post("/api/bili/ups/rename")
def bili_up_rename(req: BiliUpRenameReq):
    """给已保存的 UP主 设置/清除自定义名称（昵称偶尔读不出时手动命名）。"""
    return _clean(bili.rename_up(req.uid, req.name))


@router.post("/api/bili/scan")
def bili_scan(req: BiliScanReq):
    """抓取所选 UP主 在时间范围内的视频/动态/字幕/评论 → 股票提及排行。"""
    _task_start("仅供学习")
    try:
        res = bili.scan(req.uids, req.cfg, progress_cb=_task_progress)
        return _clean(res)
    except TaskCancelled:
        return {**_cancelled_response(), "stocks": [], "ups": [], "stats": {}}
    except Exception as e:  # noqa: BLE001 —— 网络/风控故障返回 JSON 错误而非 500
        return {"error": f"抓取失败：{e}", "stocks": [], "ups": [], "stats": {},
                "notes": [str(e)[:300]]}
    finally:
        _task_done()


@router.post("/api/bili/summary")
def bili_summary(req: BiliScanReq):
    """一键总结最近一次抓取结果（不重新联网）。"""
    _task_start("一键总结")
    try:
        return _clean(bili.summarize_last(req.cfg))
    except Exception as e:  # noqa: BLE001
        return {"error": f"总结失败：{e}", "lines": [], "stocks": [], "factors": {}}
    finally:
        _task_done()


@router.post("/api/bili/session/reset")
def bili_session_reset():
    """重建 B站 会话（换 buvid/ticket）。"""
    return _clean(bili.reset_session())


@router.post("/api/bili/cache/clear")
def bili_cache_clear():
    """清空抓取缓存。"""
    return _clean(bili.clear_cache())


@router.post("/api/bili/cred/set")
def bili_cred_set(req: BiliCredReq):
    """保存自己的 SESSDATA（可选，仅存本地；用于解锁视频字幕）。"""
    return _clean(bili.set_sessdata(req.sessdata))


@router.post("/api/bili/cred/clear")
def bili_cred_clear():
    return _clean(bili.clear_sessdata())


@router.get("/api/bili/asr/state")
def bili_asr_state():
    """本地视频转写环境状态（引擎 / 目录 / 缓存位置）。"""
    return _clean(bili.local_video_state())


@router.get("/api/bili/asr/help")
def bili_asr_help():
    """开启「本地视频文稿」所需的安装指引（按引擎给出命令）。"""
    return _clean(bili.asr_help())


class BiliArchiveClearReq(BaseModel):
    uid: str = ""


@router.get("/api/bili/archive")
def bili_archive():
    """本地存档概览：各 UP主 的视频/动态覆盖区间、提及数、占用空间。"""
    return _clean(bili.archive_stats())


@router.post("/api/bili/archive/clear")
def bili_archive_clear(req: BiliArchiveClearReq):
    """清空本地存档：指定 uid 只清该 UP主，否则全清。"""
    return _clean(bili.clear_archive(req.uid or None))
