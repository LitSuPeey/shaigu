# -*- coding: utf-8 -*-
"""多源增量同步：按各源优势组合拉取基础信息 + 日K/估值/分红。

========================================================================
【2026-09-07 架构与效率优化日志】（内部开发记录，未随仓库发布）
------------------------------------------------------------------------
重要更正："akshare × adata 联动" 已被证伪——本项目**从不 import adata**：
adata 的 kline 与本项目的东财直连是同一接口(EM push2his)，而本项目实现
更优（共享 Session / 8 vs 13 字段 / 超时+熔断 / 停牌返回 None）；adata 的
分红(Baidu) 5/5 失败、akshare 的 all_code / trade_calendar 更快，且 adata
会在 import 时加载 V8（违规模块规则、本环境直接崩溃）。故所有"联动"收益
均为 0，真实提速来自下面三条组合方案（均已实测，非理论）：

  A) 估值批量：东财 datacenter-web 支持 `SECURITY_CODE in (...)`，一次请求
     覆盖多只。实测 300 只 批量(chunk50/4线程) 1.03s vs 逐股(8线程) 4.85s
     → 4.7×；40 只样本 PE/PB 逐日逐值比对零差异。已接入 _sync_one + sync_all。
  B) 分红批量：东财 datacenter-web `RPT_SHAREBONUS_DET`（纯 JSON 无 V8）一次
     覆盖全市场，替代逐股 cninfo(5554次×0.35s≈32分钟+V8风险)。
     **v2(2026-09-10)**：改「按报告期(10期/28请求/9.4s)」为「按除权除息日窗口
     (11页/并发/1.5s)」= 6.0×，窗口内结果与旧实现完全等价（新有旧无 0 条）。
     窗口只取过去时段，未来预案除权日天然排除（旧「按报告期」口径无日期上界，实测会
     带回 109 条未来除权日、被 snapshot 的 TTM 股息率提前计入）；且分析只需近 365 天
     股息率，400 天窗口足够。降级链：窗口 → 按报告期 → 本轮跳过（不锤 cninfo）。
     分红表**任何模式下都不清空**（只增不删），避免丢掉东财源缺失的中期/特别
     分红（近一年实测 12 条）——那才是真正的反向更新/数据丢失。
  C) 日K：当前 fallback 链 新浪(stock_zh_a_daily, 单位恒为股) → 腾讯
     (stock_zh_a_hist_tx, 单位不统一, 已加 _autofix_volume_unit 自洽校验)。
     东财 push2his 目前被 WAF 整体封锁(~8h冷却)，故"东财批量快照补最新一根"
     暂不可行，待解封后按既有验证方案再接入。

口径统一（出口硬约束）：volume=股、amount=元、turnover=小数、outstanding=股。
历史曾两次踩坑：①成交额误×100 ②腾讯「手」当「股」写库(量小100倍)，均会让
量能/换手/流动性类分析整体失真——见 _autofix_volume_unit + _normalize 双保险。
========================================================================

各源职责：
  清单：沪深京官方列表（4 请求）；估值：东财 datacenter-web（日期下推）；
  分红：cninfo（3 天频率门控）+ 东财 fhps 批量加速；
  日K：东财直连(日期下推+纯JSON,无V8) → 新浪 → 腾讯 兜底，配连续失败熔断器。
板块成分并行拉取。"""
import logging as _logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import pandas as pd

from . import db, ranges

_log = _logging.getLogger("sync")
logging = _log  # alias

# akshare 懒加载：不在 import 时加载 V8(py_mini_racer)。
# 否则 ProcessPool spawn 子进程会重新 import 本模块，并在子进程里初始化
# V8 JS 引擎，Windows 多进程环境下会崩溃（mini_racer.dll）。
# 注意：PEP 562 模块级 __getattr__ 只对 `sync.ak` 外部属性访问生效，
# 对本模块内部代码的裸名 `ak` 无效（NameError 不走模块属性钩子），
# 因此这里用惰性代理对象：模块内/外统一 `ak.xxx`，首次访问才真正 import。
_ak = None


class _AkLazy:
    def __getattr__(self, name: str):
        global _ak
        if _ak is None:
            try:
                import akshare
                _ak = akshare
            except ImportError:  # pragma: no cover
                _ak = None
        if _ak is None:
            raise RuntimeError("akshare 未安装：pip install akshare")
        return getattr(_ak, name)


ak = _AkLazy()


def __getattr__(name):
    if name == "ak":  # 兼容外部 sync.ak 访问
        return ak
    raise AttributeError(f"module 'sync' has no attribute {name!r}")


_V8_LOCK = threading.Lock()
_V8_PATCHED = {"v": False}


def _patch_v8_lock() -> bool:
    """给 py_mini_racer(MiniRacer) 的构造与执行套全局锁（进程级一次）。

    根因（2026-09-07 实证）：akshare 的 stock_dividend_cninfo / stock_zh_a_daily
    内部用 V8 执行 JS；warmup 只加载了 DLL 而未初始化 V8 平台，多线程并发首次
    构造 MiniRacer 时在 V8 平台初始化处竞争 → 0x80000003 无声崩溃（整个进程）。
    V8 的 JS 解密是毫秒级操作，全局串行无感知性能损失。
    """
    global _V8_PATCHED
    if _V8_PATCHED["v"]:
        return True
    with _V8_LOCK:
        if _V8_PATCHED["v"]:
            return True
        try:
            import py_mini_racer

            # RLock：MiniRacer.__init__ 内部可能再调 self.eval（可重入场景），
            # 用普通 Lock 会自死锁（2026-09-07 启动卡死实证）
            lock = threading.RLock()
            orig_init = py_mini_racer.MiniRacer.__init__

            def safe_init(self, *a, **kw):
                with lock:
                    orig_init(self, *a, **kw)

            py_mini_racer.MiniRacer.__init__ = safe_init
            for _m in ("eval", "call"):
                if hasattr(py_mini_racer.MiniRacer, _m):
                    _orig = getattr(py_mini_racer.MiniRacer, _m)

                    def _wrap(f):
                        def safe(self, *a, **kw):
                            with lock:
                                return f(self, *a, **kw)
                        return safe

                    setattr(py_mini_racer.MiniRacer, _m, _wrap(_orig))
            _V8_PATCHED["v"] = True
            return True
        except Exception:  # noqa: BLE001 —— 未装 py_mini_racer 时静默跳过
            return False


def warmup_ak():
    """主线程预热 akshare + 真正初始化 V8 平台（连带 py_mini_racer DLL 首次加载）。

    两层保险：① DLL/V8 平台初始化在主线程完成（后台线程首次初始化会无声崩溃）；
    ② _patch_v8_lock 让后续任何线程的 V8 构造/执行串行化（防并发竞争）。"""
    _patch_v8_lock()
    _ = ak.stock_info_a_code_name  # 触发 akshare 真实 import + 属性解析
    try:  # 主线程真正跑一次 V8（构造 Context + eval），彻底完成平台初始化
        import py_mini_racer
        _mr = py_mini_racer.MiniRacer()   # 构造/执行已在 patch 内的锁保护下
        _ = _mr.eval("1+1")
    except Exception:  # noqa: BLE001 —— V8 缺失时各接口自行降级，不阻塞启动
        pass
    return True


# ========== 进度回调 ==========
class SyncCancelled(Exception):
    """用户请求停止同步时，在拉取链路上抛出的中断信号。

    ⚠ 与普通 Exception 区分：`_call_ak` 的 `except Exception` 重试分支绝不能吞掉它
    （否则「停止」后还会傻等 15s 超时 ×3 重试 = 45s 才退出）。"""


# 同步专用取消闸门：**仅在 sync_all 执行期间非 None**（开始时创建、结束时清空）。
# 这样 alert 等非同步流程偶发调用 fetch_spot/_call_ak 时不会被上一轮同步的残留取消信号误伤。
_SYNC_CANCEL_EV: Optional[threading.Event] = None


class _SyncState:
    def __init__(self):
        self.lock = threading.Lock()
        self.frac = 0.0
        self.msg = "尚未开始"
        self.status = "idle"   # idle/running/done/failed/cancelled
        self.logs: list[str] = []
        self.last_counters: Optional[dict] = None
        self.cancel = False    # 用户请求停止

    def request_cancel(self):
        with self.lock:
            self.cancel = True
        # 同时拉响「同步专用闸门」：正在跑的拉取线程会在下一个检查点（≤0.2s）退出，
        # 而不是傻等到 HTTP 超时/重试跑完。
        ev = _SYNC_CANCEL_EV
        if ev is not None:
            ev.set()

    def cb(self, done: int, total: int, msg: str):
        with self.lock:
            self.frac = done / max(total, 1)
            self.msg = msg
            if len(self.logs) < 200:
                self.logs.append(msg)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "frac": self.frac,
                "msg": self.msg,
                "logs": list(self.logs[-20:]),
                "counters": self.last_counters,
            }


STATE = _SyncState()


# ========== 工具 ==========
# 常驻线程池：给 akshare 网络请求加硬超时（requests 默认无超时会无限卡住）
import concurrent.futures as _cf
_AK_POOL = _cf.ThreadPoolExecutor(max_workers=24, thread_name_prefix="ak")


def _call_ak(name: str, fn, retries: int = 2, timeout: int = 15, delay: float = 0.0):
    """调用 akshare 接口，带真正的硬超时（超时后重试，避免网络卡死）。

    【2026-10-12 停止同步修复】同步期间（_SYNC_CANCEL_EV 非 None）额外提供两个
    中断点，让「停止同步」真正立即生效：
      ① 每次重试前检查取消闸门 → 已停止则不再重试（否则单标的要等到 15s×3=45s）；
      ② 等待结果时按 0.2s 分段轮询闸门 → 已停止则立刻放弃等待并抛 SyncCancelled。
    底层 HTTP 请求本身无法强行掐断，但它会在后台自行超时结束，写入是幂等的
    INSERT OR REPLACE，中途放弃不会破坏数据。"""
    _ev = _SYNC_CANCEL_EV
    last = None
    for i in range(max(1, retries + 1)):
        if _ev is not None and _ev.is_set():
            raise SyncCancelled(f"{name}: 已停止（用户请求）")
        try:
            fut = _AK_POOL.submit(fn)
            if _ev is None:
                r = fut.result(timeout=timeout)      # 非同步路径：行为与旧版完全一致
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        r = fut.result(timeout=0.2)
                        break
                    except _cf.TimeoutError:
                        if _ev.is_set():
                            fut.cancel()
                            raise SyncCancelled(f"{name}: 已停止（用户请求）")
                        if time.monotonic() >= deadline:
                            raise                       # → 落到下面超时分支重试/报错
            if delay:
                time.sleep(delay)
            return r
        except SyncCancelled:
            raise                                       # ★ 取消：不重试、不吞
        except _cf.TimeoutError:
            last = TimeoutError(f"{name} 超时({timeout}s)")
        except Exception as e:
            last = e
        if _ev is not None and _ev.is_set():
            raise SyncCancelled(f"{name}: 已停止（用户请求）")
        time.sleep(min(2.0, 0.4 * (i + 1)))
    raise RuntimeError(f"{name}: {type(last).__name__}: {last}")


def _norm_code(code: str) -> str:
    return re.sub(r"^[a-zA-Z]+", "", str(code)).strip()


# ========== 全市场快照 / 上市日期 / 板块 ==========
def fetch_spot(timeout: int = 15) -> pd.DataFrame:
    """全市场股票清单，三级源按成本升序降级：
    1. 东财全市场快照 stock_zh_a_spot_em（1 次请求，含最新价/涨跌幅）
    2. 沪深京官方列表 stock_info_a_code_name（4 次请求，无价格）
    3. 新浪逐页快照 stock_zh_a_spot（80 只/页 ≈ 70+ 次请求，官方文档明示易封 IP，
       仅作最后兜底；旧版曾以它为主源，是 meta 刷新慢的主因之一）"""
    def _norm(df, code_col="code"):
        df = df.copy()
        df[code_col] = df[code_col].apply(_norm_code)
        df["name"] = df["name"].astype(str).str.strip()
        if "price" not in df.columns:
            df["price"] = float("nan")
        if "change_pct" not in df.columns:
            df["change_pct"] = float("nan")
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce")
        return df[["code", "name", "price", "change_pct"]].reset_index(drop=True)

    try:  # ① 东财快照：1 次请求，代码/名称/最新价/涨跌幅
        df = _call_ak("东财全市场快照", lambda: ak.stock_zh_a_spot_em(),
                      retries=1, timeout=timeout)
        df = df.rename(columns={"代码": "code", "名称": "name",
                                "最新价": "price", "涨跌幅": "change_pct"})
        if not all(c in df.columns for c in ("code", "name")):
            raise RuntimeError("列缺失")
        df = df[df["code"].astype(str).str.match(r"^\d{6}$", na=False)]
        return _norm(df)
    except Exception:  # noqa: BLE001
        pass
    try:  # ② 沪深京官方列表：4 次请求
        df = _call_ak("名称代码表", lambda: ak.stock_info_a_code_name(),
                      retries=2, timeout=timeout)
        return _norm(df)
    except Exception:  # noqa: BLE001
        pass
    # ③ 新浪逐页快照（慢，兜底）
    df = _call_ak("全市场快照", lambda: ak.stock_zh_a_spot(), retries=2,
                  timeout=timeout)
    df = df.rename(columns={"代码": "raw_code", "名称": "name",
                            "最新价": "price", "涨跌幅": "change_pct"})
    df = df[df["raw_code"].astype(str).str.match(r"^(sh|sz|bj)\d{6}$", na=False)]
    df = df.rename(columns={"raw_code": "code"})
    return _norm(df)


def fetch_listing_dates(timeout: int = 15) -> dict:
    """{6位代码: datetime.date}（上交所/深交所/北交所官方）。"""
    mapping = {}
    if ak is None:
        return mapping
    for sym in ("主板A股", "科创板"):
        try:
            df = _call_ak(f"上交所列表({sym})",
                          lambda s=sym: ak.stock_info_sh_name_code(symbol=s),
                          retries=2, timeout=timeout)
            if "证券代码" in df.columns and "上市日期" in df.columns:
                for _, r in df.iterrows():
                    d = pd.to_datetime(r["上市日期"], errors="coerce")
                    if pd.notna(d):
                        mapping[_norm_code(r["证券代码"])] = d.date()
        except Exception:
            pass
    try:
        df = _call_ak("深交所A股列表",
                      lambda: ak.stock_info_sz_name_code(symbol="A股列表"),
                      retries=2, timeout=timeout)
        if "A股代码" in df.columns and "A股上市日期" in df.columns:
            for _, r in df.iterrows():
                d = pd.to_datetime(r["A股上市日期"], errors="coerce")
                if pd.notna(d):
                    mapping[_norm_code(r["A股代码"])] = d.date()
    except Exception:
        pass
    try:
        df = _call_ak("北交所列表", lambda: ak.stock_info_bj_name_code(),
                      retries=2, timeout=timeout)
        if "证券代码" in df.columns and "上市日期" in df.columns:
            for _, r in df.iterrows():
                d = pd.to_datetime(r["上市日期"], errors="coerce")
                if pd.notna(d):
                    mapping[_norm_code(r["证券代码"])] = d.date()
    except Exception:
        pass
    return mapping


_SW_FALLBACK = {
    "801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁", "801050": "有色金属",
    "801080": "电子", "801110": "家用电器", "801120": "食品饮料", "801130": "纺织服饰",
    "801140": "轻工制造", "801150": "医药生物", "801160": "公用事业", "801170": "交通运输",
    "801180": "房地产", "801200": "商贸零售", "801210": "社会服务", "801230": "综合",
    "801710": "建筑材料", "801720": "建筑装饰", "801730": "电力设备", "801740": "国防军工",
    "801750": "计算机", "801760": "传媒", "801770": "通信", "801780": "银行", "801790": "非银金融",
    "801880": "汽车", "801890": "机械设备", "801950": "煤炭", "801960": "石油石化",
    "801970": "环保", "801980": "美容护理",
}


def fetch_board_list() -> list[tuple[str, str]]:
    """申万一级行业列表 [(板块代码, 板块名)]。"""
    if ak is None:
        return list(_SW_FALLBACK.items())
    try:
        info = _call_ak("申万一级行业", lambda: ak.sw_index_first_info(),
                        retries=2, timeout=15)
        code_col = next((c for c in info.columns if "代码" in str(c)), None)
        name_col = next((c for c in info.columns if "名称" in str(c)), None)
        if code_col and name_col:
            return [(str(r[code_col]).split(".")[0], str(r[name_col]))
                    for _, r in info.iterrows()]
    except Exception:
        pass
    return list(_SW_FALLBACK.items())


def fetch_sector_members(board_name: str, board_code: str, timeout: int = 15) -> dict:
    """申万行业成分 {code: name}。"""
    if ak is None:
        return {}
    try:
        df = _call_ak(f"申万成分({board_name})",
                      lambda c=board_code: ak.index_component_sw(symbol=c),
                      retries=2, timeout=timeout)
        cc = next((c for c in df.columns if "代码" in str(c)), None)
        if cc:
            return {_norm_code(r[cc]): board_name for _, r in df.iterrows()}
    except Exception:
        pass
    return {}


# ========== 单股日K / 估值 / 分红 ==========
# 东财接口共享 Session（K线 + 估值同源复用：全市场数千次请求省去
# 每次 100~300ms 握手；东财无 cookie 依赖，多线程共享安全——ticker._sess 同款用法）
_EM_SESSION = None


def _em_sess():
    global _EM_SESSION
    if _EM_SESSION is None:
        import requests
        _EM_SESSION = requests.Session()
    return _EM_SESSION


# ---- 东财 K线熔断器：东财偶发断连/限流（RemoteDisconnected），连续失败后
# 本轮直接跳过东财走兜底源，避免每股白等超时；冷却后自动恢复尝试 ----
_EM_KLINE = {"fails": 0, "blocked_until": 0.0}
_EM_KLINE_LOCK = threading.Lock()
_EM_KLINE_MAX_FAILS = 6     # 连续失败阈值
_EM_KLINE_COOLDOWN = 120.0  # 熔断冷却（秒）


def _em_kline_blocked() -> bool:
    with _EM_KLINE_LOCK:
        return time.time() < _EM_KLINE["blocked_until"]


def _em_kline_record(ok: bool) -> None:
    with _EM_KLINE_LOCK:
        if ok:
            _EM_KLINE["fails"] = 0
            return
        _EM_KLINE["fails"] += 1
        if _EM_KLINE["fails"] >= _EM_KLINE_MAX_FAILS:
            _EM_KLINE["blocked_until"] = time.time() + _EM_KLINE_COOLDOWN
            _EM_KLINE["fails"] = 0


def reset_em_kline_breaker() -> None:
    """每轮同步开始时重置熔断器（重新给东财机会）。"""
    with _EM_KLINE_LOCK:
        _EM_KLINE["fails"] = 0
        _EM_KLINE["blocked_until"] = 0.0


def _fetch_daily_em(code: str, start_date: date, end_date: date,
                    timeout: int = 10) -> Optional[pd.DataFrame]:
    """东财 K线直连（adata StockMarketEast 同款策略，原生移植）：
    日期范围服务端下推（增量只回传所需几根K线）、纯 JSON 无 V8 解密。
    返回 DataFrame；范围确无数据（停牌）返回 None（无需再试兜底源）；
    网络异常抛出（由 fetch_daily 走腾讯/新浪兜底，并计入熔断）。"""
    if _em_kline_blocked():
        raise RuntimeError("东财K线熔断中，本轮走兜底源")
    # secid 市场：6 开头（沪主板/科创板）=1，其余（深主板/创业板/北交所）=0，
    # 北交所 92xxxx/43xxxx/83xxxx 实测均走 0（adata 同款规则）
    secid = f"1.{code}" if str(code).startswith("6") else f"0.{code}"
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        # f51日期 f52开 f53收 f54高 f55低 f56量(手) f57额(元) f61换手率(%)
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f61",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": "101", "fqt": "1",  # 日K / 前复权
        "secid": secid,
        "beg": start_date.strftime("%Y%m%d"),
        "end": end_date.strftime("%Y%m%d"),
    }

    def _do():
        r = _em_sess().get(
            "http://push2his.eastmoney.com/api/qt/stock/kline/get",
            params=params, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://quote.eastmoney.com/"})
        r.raise_for_status()
        return r.json()

    try:
        # retries=0：失败立即降级腾讯/新浪（每股只付一次超时代价，连续失败由熔断器兜住）
        data = _call_ak(f"东财K线({code})", _do, retries=0, timeout=timeout)
    except Exception:
        _em_kline_record(False)
        raise
    _em_kline_record(True)
    klines = ((data or {}).get("data") or {}).get("klines") or []
    if not klines:  # 响应正常但范围内无K线（停牌）→ 权威空，不试兜底
        return None
    rows = [k.split(",") for k in klines if len(k.split(",")) >= 8]
    df = pd.DataFrame(rows,
                      columns=["date", "open", "close", "high", "low",
                               "volume", "amount", "turnover_pct"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("open", "close", "high", "low", "volume", "amount", "turnover_pct"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = df["volume"] * 100.0          # 手 → 股（与新浪/腾讯口径一致）
    df["turnover"] = df["turnover_pct"] / 100.0  # % → 小数（与库内新浪口径一致）
    # 流通股本（股）由 换手率=量/流通股 反推；换手率为 0（f61 精度截断）时置空
    os_mask = df["turnover"] > 0
    df["outstanding_share"] = pd.NA
    df.loc[os_mask, "outstanding_share"] = \
        df.loc[os_mask, "volume"] / df.loc[os_mask, "turnover"]
    return df[["date", "open", "high", "low", "close", "volume",
               "amount", "turnover", "outstanding_share"]]


def _autofix_volume_unit(df: pd.DataFrame, code: str) -> pd.Series:
    """按自洽比判定成交量单位，必要时把「手」换算成「股」后返回新列。

    判据：ratio = 成交额 ÷ (成交量 × 收盘价)，取中位数。
      · 单位已是「股」→ ratio ≈ 1（前复权会让价格低于原始价，略大于 1 属正常）
      · 单位是「手」  → ratio ≈ 100
    只有中位数落在 [40, 250] 这个明确的「百倍量级」区间才换算，
    区间外一律原样返回——宁可不动，也不做没把握的改写（防止反向污染）。
    """
    vol = df["volume"]
    try:
        if "amount" not in df.columns or "close" not in df.columns:
            return vol
        base = vol * df["close"]
        ratio = (df["amount"] / base.replace(0, pd.NA)).median()
        if ratio is None or pd.isna(ratio):
            return vol
        if 40.0 <= float(ratio) <= 250.0:
            _log.info(f"日K({code}) 成交量单位判定为「手」，已 ×100 转股"
                      f"（自洽比中位数 {float(ratio):.1f}）")
            return vol * 100.0
        if float(ratio) > 250.0:
            _log.warning(f"日K({code}) 自洽比异常({float(ratio):.1f})，"
                         f"不做单位换算以免误改数据")
    except Exception:  # noqa: BLE001 —— 判定失败不影响主流程，按「股」原样返回
        pass
    return vol


def fetch_daily(code: str, start_date: date, end_date: date,
                timeout: int = 10) -> Optional[pd.DataFrame]:
    """日K线（前复权），返回 DataFrame 或 None。

    源优先级（2026-09-07 实测标定）：
    1. 东财直连（日期下推+纯JSON，最快；f56 单位为手，函数内已 ×100）
    2. 新浪 stock_zh_a_daily（1.30s/股；实测 10 只样本自洽比恒为 1.00，
       单位稳定为「股」，且原生提供流通股本 → 首选兜底）
    3. 腾讯 stock_zh_a_hist_tx（0.98s/股；**单位不统一**：深市主板 000/001
       开头返回「手」，其余返回「股」，实测 000001 自洽比 100.17、
       600519 为 0.997 → 只能靠自洽校验动态判定，故降为最后兜底）

    【口径统一】出口恒为：volume=股、amount=元、turnover=小数、
    outstanding_share=股。历史上一版曾把成交额误乘 100、另一版又把腾讯
    「手」当「股」写库（成交量小 100 倍），两者都会让量能/换手/流动性
    类分析整体失真，故此处改为按源标定 + 自洽校验双保险。"""
    if ak is None:
        return None
    symbol = ranges.code_to_sina_symbol(code)
    start = start_date.strftime("%Y%m%d")
    end = end_date.strftime("%Y%m%d")

    def _normalize(df, vol_unit: str = "share"):
        """归一化日K。

        vol_unit: 'share' 源单位为股（东财/新浪）；'lot' 源单位为手（已×100）；
                  'auto' 由自洽比判定（腾讯——单位不统一，见 fetch_daily 文档）。
        """
        if df is None or "date" not in df.columns or df.empty:
            return None
        out = df.copy()
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        for c in ("open", "high", "low", "close", "volume",
                  "amount", "outstanding_share", "turnover"):
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")
        if "volume" not in out.columns:
            return None
        if vol_unit == "lot":
            out["volume"] = out["volume"] * 100.0
        elif vol_unit == "auto":
            # 自洽校验：成交额(元) ÷ (成交量 × 收盘价)。单位正确时该比值≈1；
            # 源以「手」返回时整体偏高约 100 倍。取中位数抗单日异常。
            out["volume"] = _autofix_volume_unit(out, code)
        keep = ["date", "open", "high", "low", "close", "volume"]
        for c in ("amount", "turnover", "outstanding_share"):
            if c in out.columns:
                keep.append(c)
        out = out[[c for c in keep if c in out.columns]].dropna(
            subset=["date", "close"]).sort_values("date").reset_index(drop=True)
        if "amount" not in out.columns:
            # 估算兜底：volume 此时已是「股」，成交额≈量×收盘价。
            # 注意：绝不在此处再乘 100（历史一版误乘导致全库成交额放大 100 倍）。
            out["amount"] = out["volume"] * out["close"]
        # 流通股本（股）：源未原生提供时，由 换手率=成交量/流通股 反推
        # （腾讯源无该列，反推后与东财源口径一致；新浪原生提供，不覆盖）
        if "outstanding_share" not in out.columns:
            out["outstanding_share"] = pd.NA
        if "turnover" in out.columns:
            need = (out["outstanding_share"].isna()
                    & (out["turnover"] > 0) & (out["volume"] > 0))
            out.loc[need, "outstanding_share"] = \
                out.loc[need, "volume"] / out.loc[need, "turnover"]
        return out if len(out) else None

    # 东财直连（正常响应但范围内停牌返回 None，直接结束；网络异常走兜底）
    try:
        em = _fetch_daily_em(code, start_date, end_date, timeout=timeout)
        if em is not None:
            norm = _normalize(em)
            if norm is not None and len(norm):
                return norm
        else:
            return None
    except Exception:
        pass

    # (取数函数, 成交量单位标定) —— 标定依据见 fetch_daily 文档。
    # 顺序：新浪（单位稳定、字段全）→ 腾讯（更快但单位不统一，走 auto 判定）
    for fn, vol_unit in (
        (lambda: ak.stock_zh_a_daily(symbol=symbol, start_date=start,
                                     end_date=end, adjust="qfq"), "share"),
        (lambda: ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start,
                                       end_date=end, adjust="qfq"), "auto"),
    ):
        try:
            df = _call_ak(f"日K({code})", fn, retries=1, timeout=timeout)
            norm = _normalize(df, vol_unit=vol_unit)
            if norm is not None and len(norm):
                return norm
        except Exception:
            continue
    return None


def _fetch_value_em(code: str, start: Optional[str], timeout: int) -> pd.DataFrame:
    """东财估值直连（只取需要 3 列 + 可下推日期范围）。

    原 ak.stock_value_em 固定 pageSize=5000 全量拉（columns=ALL 十余列），
    增量同步也全量拉 → 5000 行 JSON 下载+解析是慢的主因之一。
    此处用 filter 下推 start，响应从 5000 行降到近期几十行。"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    filt = f'(SECURITY_CODE="{code}")'
    if start:
        filt += f"(TRADE_DATE>='{start}')"
    params = {
        "sortColumns": "TRADE_DATE", "sortTypes": "-1",
        "pageSize": "5000", "pageNumber": "1",
        "reportName": "RPT_VALUEANALYSIS_DET", "columns": "ALL",
        "quoteColumns": "", "source": "WEB", "client": "WEB",
        "filter": filt,
    }
    r = _em_sess().get(url, params=params,
                       headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    rows = ((r.json().get("result") or {}).get("data")) or []
    if not rows:
        raise RuntimeError("无估值数据")
    df = pd.DataFrame(rows).rename(
        columns={"TRADE_DATE": "数据日期", "PE_TTM": "PE(TTM)", "PB_MRQ": "市净率"})
    need = ["数据日期", "PE(TTM)", "市净率"]
    if not all(c in df.columns for c in need):
        raise RuntimeError("列缺失")
    out = df[need].copy()
    out["数据日期"] = pd.to_datetime(out["数据日期"], errors="coerce")
    for c in ("PE(TTM)", "市净率"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["数据日期"]).sort_values(
        "数据日期").reset_index(drop=True)


def _fetch_value_em_bulk(codes: list[str], start: Optional[str],
                         timeout: int) -> dict[str, pd.DataFrame]:
    """东财估值批量拉取：一次请求覆盖多只，返回 {code: DataFrame}。

    实测（2026-09-07）：filter 支持 `(SECURITY_CODE in ("c1","c2",...))`，
    5 只 0.44s vs 单只 0.13s —— 请求数从 N 降到 N/50，握手与排队开销大幅摊薄。

    仅用于增量场景（start 非空，每只只回传近期几行）。全历史场景回传量可达
    数十万行，仍走单只 + 单只翻页逻辑，避免批量把单请求撑爆。
    """
    if not codes:
        return {}
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    quoted = ",".join(f'"{c}"' for c in codes)
    filt = f"(SECURITY_CODE in ({quoted}))"
    if start:
        filt += f"(TRADE_DATE>='{start}')"
    all_rows: list[dict] = []
    page = 1
    while page <= 10:                       # 上限 10 页（5 万行）防失控
        # ★ 停止同步检查点：本函数不走 _call_ak（直接用 _em_sess），必须自查，
        #   否则「停止」后仍会把剩余分页全部拉完（实测全市场规模下约 12s）。
        if _cancelled():
            raise SyncCancelled("估值批量：已停止（用户请求）")
        params = {
            "sortColumns": "TRADE_DATE", "sortTypes": "-1",
            "pageSize": "5000", "pageNumber": str(page),
            "reportName": "RPT_VALUEANALYSIS_DET", "columns": "ALL",
            "quoteColumns": "", "source": "WEB", "client": "WEB",
            "filter": filt,
        }
        r = _em_sess().get(url, params=params,
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        r.raise_for_status()
        rows = ((r.json().get("result") or {}).get("data")) or []
        all_rows.extend(rows)
        if len(rows) < 5000:
            break
        page += 1
    if not all_rows:
        raise RuntimeError("无估值数据")
    df = pd.DataFrame(all_rows)
    if "SECURITY_CODE" not in df.columns:
        raise RuntimeError("响应缺少 SECURITY_CODE，无法按股拆分")
    df = df.rename(columns={"TRADE_DATE": "数据日期", "PE_TTM": "PE(TTM)",
                            "PB_MRQ": "市净率", "SECURITY_CODE": "_code"})
    need = ["_code", "数据日期", "PE(TTM)", "市净率"]
    if not all(c in df.columns for c in need):
        raise RuntimeError("列缺失")
    df = df[need].copy()
    df["数据日期"] = pd.to_datetime(df["数据日期"], errors="coerce")
    for c in ("PE(TTM)", "市净率"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["数据日期"])
    out: dict[str, pd.DataFrame] = {}
    for code, g in df.groupby("_code"):
        g = g.drop(columns=["_code"]).sort_values(
            "数据日期").reset_index(drop=True)
        out[str(code).zfill(6)] = g
    return out


def fetch_valuation(code: str, timeout: int = 15,
                    start_date: Optional[date] = None) -> Optional[pd.DataFrame]:
    """估值历史 PE-TTM/PB-MRQ（东财 F10，失败回退百度）。

    start_date：增量场景只拉 >=该日期（东财 filter 下推，响应从 5000 行降到几十行）；
    首次/全量（None）拉全历史——PE/PB 分位计算依赖本地全量历史，增量补近期不影响分位。"""
    if ak is None:
        return None
    code = _norm_code(code)
    start = start_date.strftime("%Y-%m-%d") if start_date else None

    def _loader_main():
        return _call_ak(f"估值({code})",
                        lambda: _fetch_value_em(code, start, timeout),
                        retries=2, timeout=timeout)

    try:
        return _call_ak(f"估值({code})", _loader_main, retries=1, timeout=timeout)
    except Exception:
        pass
    try:
        pe = _call_ak("百度估值PE",
                      lambda: ak.stock_zh_valuation_baidu(
                          symbol=code, indicator="市盈率(TTM)", period="全部"),
                      retries=1, timeout=timeout)
        pb = _call_ak("百度估值PB",
                      lambda: ak.stock_zh_valuation_baidu(
                          symbol=code, indicator="市净率", period="全部"),
                      retries=1, timeout=timeout)
        pe = pe.rename(columns={"date": "数据日期", "value": "PE(TTM)"})
        pb = pb.rename(columns={"date": "数据日期", "value": "市净率"})
        out = pe.merge(pb, on="数据日期", how="outer").sort_values("数据日期")
        out["数据日期"] = pd.to_datetime(out["数据日期"], errors="coerce")
        for c in ("PE(TTM)", "市净率"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        return out.dropna(subset=["数据日期"]).reset_index(drop=True)
    except Exception:
        return None


def fetch_dividend(code: str, timeout: int = 15) -> Optional[list]:
    """分红事件 [(date, 每10股派息)]。"""
    if ak is None:
        return None
    code = _norm_code(code)

    try:
        df = _call_ak(f"分红({code})",
                      lambda: ak.stock_dividend_cninfo(symbol=code),
                      retries=2, timeout=timeout)
        if df is None or df.empty or "派息比例" not in df.columns:
            return []
        out = []
        for _, r in df.iterrows():
            cash = pd.to_numeric(r["派息比例"], errors="coerce")
            if pd.isna(cash) or cash <= 0:
                continue
            d = pd.to_datetime(r.get("除权日"), errors="coerce")
            if pd.isna(d):
                d = pd.to_datetime(r.get("派息日"), errors="coerce")
            if pd.notna(d):
                out.append((d.date(), float(cash)))
        return sorted(out)
    except Exception:
        pass
    try:
        df = _call_ak(f"分红·新浪({code})",
                      lambda: ak.stock_history_dividend_detail(
                          symbol=code, indicator="分红"),
                      retries=2, timeout=timeout)
        if df is None or df.empty or "除权除息日" not in df.columns \
                or "派息" not in df.columns:
            return []
        out = []
        for _, r in df.iterrows():
            if "进度" in df.columns and str(r.get("进度") or "").strip() \
                    not in ("实施", ""):
                continue
            d = pd.to_datetime(r["除权除息日"], errors="coerce")
            cash = pd.to_numeric(r["派息"], errors="coerce")
            if pd.notna(d) and pd.notna(cash) and cash > 0:
                out.append((d.date(), float(cash)))
        return sorted(out)
    except Exception:
        return None


# ---- 分红批量：东财 datacenter-web RPT_SHAREBONUS_DET（纯 JSON，无 V8）----
# v2（2026-09-10）把「按报告期逐期串行取页」改为「按除权除息日窗口 + 并发取页」。
#   全市场实测：旧 10 期 / 28 请求 / 9.36s → 新 11 页 / 并发 / 1.56s = 6.0×，
#   且窗口内结果集与旧实现完全等价（旧有新无仅窗口外的更早记录，新有旧无 0 条）。
#   细节见本模块顶部 docstring 的「架构与效率优化日志」小节。
_FHPS_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_FHPS_SESS = None
_FHPS_SESS_LOCK = threading.Lock()
_FHPS_PAGE_SIZE = 500      # 服务端硬上限：实测传 5000/10000 仍只回 500 行
_FHPS_TIMEOUT = 20
_FHPS_WORKERS = 8          # 并发取页数（纯 HTTP，不涉 V8，多线程安全）
_FHPS_PAGE_TRIES = 3
# 只取需要的 4 个字段（columns=ALL 时单页响应 447KB → 61KB，并发全量 1.08s → 0.60s）。
# 若字段被源端改名，则全部行会被下面的校验跳过 → out 为空 → 自动降级到按报告期，
# 属可自检的安全失效，不会静默写出残缺数据。
_FHPS_COLUMNS = ("SECURITY_CODE,EX_DIVIDEND_DATE,"
                 "PRETAX_BONUS_RMB,ASSIGN_PROGRESS")
_DIV_LOOKBACK_DAYS = 400       # 增量窗口：分析只需近 365 天股息率，留 35 天缓冲
_DIV_FORCE_LOOKBACK_DAYS = 1100  # force：约 3 年窗口（覆盖原报告期口径的历史深度）


def _fhps_sess():
    """分红接口专用 Session（连接池 16/32，避免并发下连接被丢弃重建）。"""
    global _FHPS_SESS
    with _FHPS_SESS_LOCK:
        if _FHPS_SESS is None:
            import requests
            s = requests.Session()
            s.headers.update(
                {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            ad = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=32)
            s.mount("https://", ad)
            s.mount("http://", ad)
            _FHPS_SESS = s
        return _FHPS_SESS


def _fhps_page(filt: str, page_number: int, timeout: int = _FHPS_TIMEOUT):
    """取分红数据一页 → (总页数, 行列表)。"""
    params = {
        "sortColumns": "EX_DIVIDEND_DATE", "sortTypes": "-1",
        "pageSize": str(_FHPS_PAGE_SIZE), "pageNumber": str(page_number),
        "reportName": "RPT_SHAREBONUS_DET", "columns": _FHPS_COLUMNS,
        "quoteColumns": "", "source": "WEB", "client": "WEB",
        "filter": filt,
    }
    r = _fhps_sess().get(_FHPS_URL, params=params, timeout=timeout)
    r.raise_for_status()
    res = r.json().get("result") or {}
    return int(res.get("pages") or 0), (res.get("data") or [])


def _fetch_dividend_window(start: date, end: date, timeout: int = _FHPS_TIMEOUT,
                           workers: int = _FHPS_WORKERS,
                           codes: Optional[list] = None,
                           chunk: int = 500) -> Optional[dict]:
    """按「除权除息日」窗口并发抓全市场分红。

    返回 {code: {(YYYY-MM-DD, 每10股派息)}}；None = 整体失败（调用方降级）。

    为什么按除权日而不是报告期：
      · 报告期与除权日相差约半年（2024 年报 → 2025-06 除权），要覆盖近期除权
        就得把 10 个报告期全查一遍（28 次请求）；而分析只要「近一年除权事件」，
        按除权日窗口一次查询即可（11 页），请求数少 60% 且天然按需取数。
      · 窗口取 [今天-N, 今天]，**未来计划除权日天然被排除**：实测华泰证券那条
        EX_DIVIDEND_DATE=2026-10-23 / ASSIGN_PROGRESS=董事会决议通过 落在窗口外，
        不会把未实施的预案当成已派息写库。
      · 字段口径与库内 cash_per_10 完全一致：PRETAX_BONUS_RMB（每10股税前派息），
        实测贵州茅台 308.76 / 276.73 / 280.2423 与库内逐值吻合。

    codes：**候选股模式**（2026-09-10 新增，配合 divtiming 时点预测）。
      传入 code 列表时用 `SECURITY_CODE in (...)` 收窄查询，分块并发（实测
      500 codes/块 → 2 页 / 0.31s）。语义与全市场一致，只是请求量按候选规模
      线性下降；**结果只含候选股**，未入选的股不会出现在返回值里，也就不会被
      写库（它们的本地历史保持原样，零删除）。
    """
    if codes:
        return _fetch_dividend_by_codes(start, end, codes,
                                        timeout=timeout, workers=workers,
                                        chunk=chunk)
    filt = (f"(EX_DIVIDEND_DATE>='{start.isoformat()}')"
            f"(EX_DIVIDEND_DATE<='{end.isoformat()}')")
    if _cancelled():        # ★ 停止同步：首页请求也不再发出
        return None
    try:
        pages, rows = _fhps_page(filt, 1, timeout)
    except Exception as e:  # noqa: BLE001
        _log.warning(f"分红窗口首页请求失败: {e}")
        return None
    failed = 0
    if pages > 1:
        from concurrent.futures import ThreadPoolExecutor
        rows = list(rows)

        def _one(pn: int):
            last = None
            for attempt in range(_FHPS_PAGE_TRIES):
                if _cancelled():          # ★ 停止同步：放弃剩余页（不重试）
                    return pn, None
                try:
                    return pn, _fhps_page(filt, pn, timeout)[1]
                except Exception as e:  # noqa: BLE001
                    last = e
                    time.sleep(0.3 * (attempt + 1))
            _log.warning(f"分红窗口第 {pn}/{pages} 页重试 {_FHPS_PAGE_TRIES} 次仍失败: {last}")
            return pn, None

        with ThreadPoolExecutor(max_workers=max(1, min(workers, pages - 1))) as ex:
            for _pn, rs in ex.map(_one, range(2, pages + 1)):
                if rs is None:
                    failed += 1
                else:
                    rows.extend(rs)

    out: dict = {}
    got = 0
    for r in rows:
        code = _norm_code(r.get("SECURITY_CODE") or "")
        if not re.match(r"^\d{6}$", code):
            continue
        # 只要「已实施」：窗口本身是过去时段，此处为双保险（与旧实现同口径）
        if "实施" not in str(r.get("ASSIGN_PROGRESS") or ""):
            continue
        ex = str(r.get("EX_DIVIDEND_DATE") or "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", ex):
            continue
        try:
            cash = float(r.get("PRETAX_BONUS_RMB"))
        except (TypeError, ValueError):
            continue
        if cash <= 0:
            continue
        out.setdefault(code, set()).add((ex, round(cash, 6)))
        got += 1
    if not out:
        return None
    if failed:
        # 缺页只影响少量股票的少量事件；窗口每次向前滑动，下轮会自动补齐
        _log.warning(f"分红窗口 {failed}/{pages} 页失败，本轮少量缺行（下轮自动补齐）")
    _log.info(f"分红窗口就绪：{len(out)} 只 / {got} 条 / {pages} 页（{start}~{end}）")
    return out


def _fetch_dividend_by_codes(start: date, end: date, codes: list,
                             timeout: int = _FHPS_TIMEOUT,
                             workers: int = _FHPS_WORKERS,
                             chunk: int = 400) -> Optional[dict]:
    """**候选股模式**：只对给定 code 列表按 `SECURITY_CODE in (...)` 查分红。

    与全市场窗口同一 reportName / 同一列 / 同一「实施分配」过滤，故口径完全一致；
    区别只是把检索范围收窄到候选股，请求量随候选规模线性下降。

    实现：codes 分块（默认 400/块）并发，每块内部若 pages>1 再并发取余页。
    任一块彻底失败只丢该块的股（下轮窗口前移自动补齐），不影响其它块。
    None 仅当**所有块**都失败（调用方据此降级/跳过）。

    ⚠ 分块大小有硬上限（2026-09-10 实测）：东财对 URL 长度敏感，
      450 只(URL≈7092 字符) 正常，480 只起 HTTP 400，560 只 HTTP 414。
      故 default chunk=400 留足余量；**不要把 chunk 调到 450 以上**。
    """
    codes = [c for c in (codes or []) if re.match(r"^\d{6}$", str(c))]
    if not codes:
        return {}
    from concurrent.futures import ThreadPoolExecutor
    blocks = [codes[i:i + chunk] for i in range(0, len(codes), chunk)]

    def _block(bl: list):
        if _cancelled():            # ★ 停止同步：未启动的块直接放弃（不进网络请求）
            return None
        cs = '","'.join(bl)
        filt = (f"(EX_DIVIDEND_DATE>='{start.isoformat()}')"
                f"(EX_DIVIDEND_DATE<='{end.isoformat()}')"
                f'(SECURITY_CODE in ("{cs}"))')
        try:
            pages, rows = _fhps_page(filt, 1, timeout)
        except Exception as e:  # noqa: BLE001
            _log.warning(f"分红候选块首页失败({len(bl)}只): {e}")
            return None
        rows = list(rows)
        if pages > 1:
            def _one(pn: int):
                for attempt in range(_FHPS_PAGE_TRIES):
                    if _cancelled():      # ★ 停止同步：放弃剩余页（不重试）
                        return None
                    try:
                        return _fhps_page(filt, pn, timeout)[1]
                    except Exception:  # noqa: BLE001
                        time.sleep(0.3 * (attempt + 1))
                return None
            with ThreadPoolExecutor(max_workers=max(1, min(workers, pages - 1))) as ex:
                for rs in ex.map(_one, range(2, pages + 1)):
                    if rs:
                        rows.extend(rs)
        return rows

    out: dict = {}
    got = 0
    ok_blocks = 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(blocks)))) as ex:
        for rows in ex.map(_block, blocks):
            if rows is None:
                continue
            ok_blocks += 1
            for r in rows:
                code = _norm_code(r.get("SECURITY_CODE") or "")
                if not re.match(r"^\d{6}$", code):
                    continue
                if "实施" not in str(r.get("ASSIGN_PROGRESS") or ""):
                    continue
                exd = str(r.get("EX_DIVIDEND_DATE") or "")[:10]
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", exd):
                    continue
                try:
                    cash = float(r.get("PRETAX_BONUS_RMB"))
                except (TypeError, ValueError):
                    continue
                if cash <= 0:
                    continue
                out.setdefault(code, set()).add((exd, round(cash, 6)))
                got += 1
    if ok_blocks == 0:
        return None
    _log.info(f"分红候选模式就绪：候选 {len(codes)} 只 → 命中 {len(out)} 只 / {got} 条"
              f"（{len(blocks)} 块，{start}~{end}）")
    return out


def _fetch_dividend_by_period(years: int = 2, timeout: int = 25) -> Optional[dict]:
    """[降级路径] 按报告期逐期串行拉取 —— 即加速前的原实现，语义保持不变。

    仅当「除权日窗口」路径失败/覆盖异常时启用，保证不会因新路径故障而无分红数据。

    数据源 ak.stock_fhps_em(报告期)，2026-09-07 实测：
      · 2024年报 → 3676 只 / 8s；接口已天然只返回「实施分配」
      · 含「除权除息日」与「现金分红-现金分红比例」
      · 与现用 cninfo「派息比例」口径完全一致：
        贵州茅台 276.73、工商银行 1.646、五粮液 31.69（三源对齐）

    注意：报告期与除权日存在约半年滞后（2024年报 → 2025-06 除权），
    因此需回溯 years 年的报告期才能覆盖近期除权事件。

    返回 None 表示批量失败（调用方应改用窗口路径，或本轮跳过分红而非逐股 cninfo）。
    """
    if ak is None:
        return None
    periods: list[str] = []
    y = date.today().year
    for yy in range(y - years, y + 1):
        for md in ("0331", "0630", "0930", "1231"):
            p = date(yy, int(md[:2]), int(md[2:]))
            if p <= date.today():
                periods.append(p.strftime("%Y%m%d"))
    out: dict[str, list] = {}
    got = 0
    for p in periods:
        try:
            df = _call_ak(f"批量分红({p})",
                          lambda p=p: ak.stock_fhps_em(date=p),
                          retries=1, timeout=timeout)
        except Exception:  # noqa: BLE001 —— 单期失败不影响其余期，最终按总数判定
            continue
        if df is None or not len(df):
            continue
        cash_col, ex_col = "现金分红-现金分红比例", "除权除息日"
        if cash_col not in df.columns or ex_col not in df.columns:
            continue
        if "方案进度" in df.columns:  # 只取已实施（接口通常已过滤，此处双保险）
            df = df[df["方案进度"].astype(str).str.contains("实施", na=False)]
        # 中文列名无法用 itertuples 属性访问（pandas 会改名成 _1/_2），先改 ASCII 名
        d = df.rename(columns={"代码": "code", cash_col: "cash", ex_col: "ex"})
        d = d[["code", "cash", "ex"]].copy()
        d["cash"] = pd.to_numeric(d["cash"], errors="coerce")
        d["ex"] = pd.to_datetime(d["ex"], errors="coerce")
        for r in d.itertuples(index=False):
            code = _norm_code(r.code)
            if not re.match(r"^\d{6}$", code):
                continue
            if pd.isna(r.cash) or float(r.cash) <= 0 or pd.isna(r.ex):
                continue
            out.setdefault(code, []).append((r.ex.date(), float(r.cash)))
            got += 1
    if not got:
        return None
    for k in list(out.keys()):
        out[k] = sorted(set(out[k]))
    _log.info(f"批量分红就绪：{len(out)} 只 / {got} 条（{len(periods)} 个报告期）")
    return out


def fetch_dividend_bulk(lookback_days: int = _DIV_LOOKBACK_DAYS,
                        timeout: int = _FHPS_TIMEOUT,
                        workers: int = _FHPS_WORKERS,
                        years: Optional[int] = None) -> Optional[dict]:
    """批量分红 v2：按除权除息日窗口并发拉取全市场。

    返回 {code: [(除权除息日 date, 每10股派息 float), ...]}（与旧实现同形），
    None 表示批量失败（调用方应降级到 _fetch_dividend_by_period）。

    lookback_days：窗口长度（天）。增量默认 400 天（分析只需近 365 天股息率）；
      force 传 _DIV_FORCE_LOOKBACK_DAYS 重建更深历史。
    years：兼容旧调用 fetch_dividend_bulk(years=2)，等价于 lookback_days=732。
    """
    if years is not None:
        lookback_days = max(int(lookback_days), int(years * 366))
    end = date.today()
    start = end - timedelta(days=max(1, int(lookback_days)))
    res = _fetch_dividend_window(start, end, timeout=timeout, workers=workers)
    if res is None:
        return None
    return {c: sorted((datetime.strptime(e, "%Y-%m-%d").date(), float(v))
                      for e, v in s)
            for c, s in res.items()}


def _div_lookback_days(conn, force: bool) -> int:
    """决定本轮分红查询窗口长度（天）。

    force → 3 年；增量 → max(400, 距上次分红检查的天数 + 45)，
    即长时间未同步后自动放宽窗口，避免漏掉停机期间新产生的除权事件。
    """
    if force:
        return _DIV_FORCE_LOOKBACK_DAYS
    days = _DIV_LOOKBACK_DAYS
    try:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key='last_div_check'").fetchone()
        if row:
            last = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            days = max(days, (datetime.now() - last).days + 45)
    except Exception:  # noqa: BLE001
        pass
    return days


def _dividend_bulk_stage(conn, force: bool) -> tuple[Optional[dict], bool]:
    """分红批量阶段：候选股窗口(或全市场) → 全市场回落 → 按报告期 → 放弃本轮。

    返回 (bulk_div, ok)。ok=False 时调用方应把 dividend 从本轮 kinds 中摘除：
    宁可不更新分红，也不退回逐股 cninfo（5554 次请求 ≈ 32 分钟，且 V8 并发有
    崩溃前科）——窗口每轮向前滑动，失败的那轮下轮自动补齐，零丢失。

    2026-09-10 新增「时点预测候选」：
      · 默认走 divtiming 候选模式（只查本周可能除权的股，实测请求量约为全市场
        的 1/4~1/5，旺季候选覆盖 ~71%）；
      · force 或命中「每年 2 月底全市场增量」规则 → 全市场模式；
      · 候选模式失败 → 自动回落全市场 → 再失败按报告期 → 最后才跳过本轮
        （**任何分支都不清空分红表**，只做 upsert 增量）。
    """
    if _cancelled():          # ★ 停止同步：不再进入分红阶段（含不可中断的候选计算）
        return None, False
    lb = _div_lookback_days(conn, force)
    STATE.cb(0, 1, f"分红：并发拉取近 {lb} 天除权除息…")
    # ---- 时点预测：决定本轮走「候选股模式」还是「全市场模式」----
    # 规则 d：每年 2 月底全市场拉一次（每年仅一次）；其余各周按 divtiming 候选。
    # 候选模式只减少请求，**绝不删除任何本地历史**（分红表永不清空）。
    cand_codes = None
    cand_note = ""
    try:
        from ..modules import divtiming
        ws = date.today() - timedelta(days=date.today().weekday())   # 本周一
        if force:
            _log.info("分红 force：重建窗口，走全市场模式")
        elif divtiming.should_full_market(conn, ws):
            _log.info("分红：命中「每年 2 月底全市场增量」规则 → 全市场模式")
            cand_note = "2月底全市场增量"
        else:
            r = divtiming.candidates(conn, ws)
            cand = r["candidates"]
            no = r["notes"]
            if cand:
                cand_codes = list(cand)
                cand_note = (f"候选 {len(cand)} 只（全市场 {no['total_meta']}"
                             f"，剔除从未分红 {no['never_dividend']}"
                             f" / 持续亏损 {no['dropped_loss']}"
                             f" / 本周期已分 {no['dropped_recent']}）")
                _log.info(f"分红时点预测：{cand_note}")
            else:
                _log.warning("分红时点预测候选为空 → 回落全市场模式（宁多查勿漏）")
    except Exception as e:  # noqa: BLE001
        _log.warning(f"分红时点预测不可用，回落全市场模式: {e}")
        cand_codes = None

    if _cancelled():          # ★ 停止同步：候选计算已完成但用户已点停 → 不再发请求
        return None, False
    res = None
    try:
        if cand_codes:
            res = _fetch_dividend_window(
                date.today() - timedelta(days=lb), date.today(), codes=cand_codes)
            # 候选模式命中数天然少（只查候选），用「非 None」判定成功即可
            if res is not None:
                STATE.cb(0, 1, f"分红：候选模式命中 {len(res)} 只有新事件"
                               f"（{cand_note}）")
        else:
            res = fetch_dividend_bulk(lookback_days=lb)
    except Exception as e:  # noqa: BLE001
        _log.warning(f"分红窗口批量异常: {e}")
    # 判定：候选模式命中少是预期内的；全市场模式沿用 ≥300 只的异常判定
    if res is not None and (cand_codes is not None or len(res) >= 300):
        return res, True
    if res is not None and cand_codes is None:
        _log.warning(f"分红窗口仅覆盖 {len(res)} 只，判定异常，降级按报告期拉取")
    if cand_codes is not None:
        if _cancelled():      # ★ 停止同步：不再做「全市场回落」（它要发 11 页请求）
            _log.info("分红：已请求停止同步，放弃全市场回落")
            return None, False
        # 候选模式失败：宁可回落全市场，也不能漏掉本周分红
        _log.warning("分红候选模式失败 → 回落全市场模式重试")
        try:
            res = fetch_dividend_bulk(lookback_days=lb)
        except Exception as e:  # noqa: BLE001
            _log.warning(f"分红全市场回落也失败: {e}")
            res = None
        if res is not None and len(res) >= 300:
            return res, True
    if _cancelled():          # ★ 停止同步：不再做「按报告期」降级（28 请求 / 9s）
        _log.info("分红：已请求停止同步，放弃按报告期降级")
        return None, False
    try:
        res2 = _fetch_dividend_by_period(years=2)
    except Exception as e:  # noqa: BLE001
        _log.warning(f"分红按报告期降级也失败: {e}")
        res2 = None
    if res2 is not None and len(res2) >= 300:
        _log.warning("分红已降级为「按报告期」拉取（可接受，但比窗口路径慢）")
        return res2, True
    _log.warning("分红批量全部失败，本轮跳过分红（不改动任何已有数据，下轮重试）")
    return None, False


# ========== 增量同步主流程 ==========
def _iso(d) -> str | None:
    """date/datetime/str → 'YYYY-MM-DD'（失败 None）。"""
    if d is None:
        return None
    try:
        return d.strftime("%Y-%m-%d")
    except AttributeError:
        s = str(d)[:10]
        return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else None


def _cancelled() -> bool:
    """是否已请求停止本轮同步（只有同步进行期间可能为 True）。"""
    ev = _SYNC_CANCEL_EV
    return bool(ev is not None and ev.is_set())


def _sync_one(code: str, row: dict, kinds: tuple, wconn, counters: dict,
              last_daily: dict | None = None,
              last_valuation: dict | None = None,
              last_trade: str | None = None,
              div_known: set | None = None,
              bulk_div: dict | None = None,
              bulk_val: dict | None = None):
    """单股增量同步。last_daily/last_valuation：批量预取的 {code: date}；
    last_trade：最近已完成交易日（None=日历降级，退回旧判定逻辑，不跳过）；
    div_known：本地已有分红事件 {(code, 'YYYY-MM-DD')}（调用方恒传入，**force 也传**：
    分红表只增不删，过滤掉已存在键可避免同 (code, ex_date) 被源端不同口径覆盖）；
    bulk_div：批量分红结果 {code: [(date, cash)]}（非 None 时该股零请求取分红）；
    bulk_val：批量估值结果 {code: DataFrame}（仅含「本地已有历史、只需补近期」的股；
    未覆盖到的股自动退回逐股 fetch_valuation，不会因为批量而漏数据）。"""
    today = date.today()
    listing = None
    if row.get("listing_date"):
        try:
            listing = datetime.strptime(str(row["listing_date"]), "%Y-%m-%d").date()
        except Exception:
            listing = None
    last_daily = last_daily or {}
    last_valuation = last_valuation or {}
    # 拉取终点收敛：盘中（<20:00）新浪/东财会返回当日未收盘 K 线/实时估值，
    # 写库后收盘价不再更新（增量起点已越过）→ 数据永久失真。
    # 收敛到最近已完成交易日，与 freshness 提示的 20:00 阈值口径一致。
    end = today
    if last_trade:
        try:
            lt = datetime.strptime(last_trade, "%Y-%m-%d").date()
            if lt < end:
                end = lt
        except Exception:  # noqa: BLE001
            pass
    try:
        if "daily" in kinds:
            last_d = last_daily.get(code)
            if last_d is None:
                start = listing or (today - timedelta(days=400))
                bars = fetch_daily(code, start, end)
                if bars is not None and len(bars):
                    with db.write_lock():
                        _upsert_daily(wconn, code, bars)
                    counters["daily_new"] += len(bars)
                    counters["daily_full"] += 1
                elif bars is not None:
                    counters["daily_none"] += 1
                else:
                    counters["daily_fail"] += 1
            elif last_trade and _iso(last_d) is not None and _iso(last_d) >= last_trade:
                counters["daily_skip"] += 1  # 本地已含最近完成交易日（节假日/盘中零请求）
            elif (today - last_d).days >= 1:
                start = last_d + timedelta(days=1)
                if start >= today:
                    counters["daily_skip"] += 1
                else:
                    bars = fetch_daily(code, start, end)
                    if bars is not None and len(bars):
                        with db.write_lock():
                            _upsert_daily(wconn, code, bars)
                        counters["daily_new"] += len(bars)
                    elif bars is not None:
                        counters["daily_none"] += 1
                    else:
                        counters["daily_fail"] += 1
            else:
                counters["daily_skip"] += 1

        if _cancelled():          # ★ 停止同步检查点（维度边界，≥0.2s 内生效）
            return
        if "valuation" in kinds:
            last_v = last_valuation.get(code)
            # 注意 _iso(None) 返回 None；无估值历史的股票 last_v 为 None，
            # 必须显式判空，否则 `None >= last_trade(字符串)` 会抛 TypeError
            # 使整只股票同步失败（实测 601123 等无估值行个股触发）。
            lv_iso = _iso(last_v)
            if last_trade and lv_iso is not None and lv_iso >= last_trade:
                counters["val_skip"] += 1  # 估值已最新 → 零请求（原实现无条件全量拉，慢的主因）
            else:
                # 增量只拉 >=本地最新 的近期估值（东财 filter 下推，省 5000 行全量下载）；
                # 缓冲 10 天避免数据源修正导致边界漏数据
                start_val = None
                last_d = None
                if last_v:
                    try:
                        last_d = datetime.strptime(str(last_v)[:10], "%Y-%m-%d").date()
                        start_val = last_d - timedelta(days=10)
                    except Exception:  # noqa: BLE001
                        start_val = None
                val = None
                if bulk_val is not None and code in bulk_val:
                    # 批量命中：本股零请求。数据仍走下面的 >last_d / <=end 过滤，
                    # 与逐股路径写入口径完全一致（实测 40 只逐日逐值比对零差异）。
                    val = bulk_val[code]
                    counters["val_bulk"] += 1
                else:
                    # 未覆盖（无本地历史需全量 / 该组批量失败）→ 原逐股逻辑兜底
                    val = fetch_valuation(code, start_date=start_val)
                if val is not None and len(val):
                    if last_d:
                        val = val[(val["数据日期"].dt.date > last_d) & (val["数据日期"].dt.date <= end)]
                    if len(val):
                        with db.write_lock():
                            _upsert_valuation(wconn, code, val)
                        counters["val_new"] += len(val)
                    counters["val_full"] += 1
                else:
                    counters["val_fail"] += 1

        if _cancelled():          # ★ 停止同步检查点（维度边界）
            return
        if "dividend" in kinds:
            # 批量优先：一次覆盖全市场，本股零请求（bulk 未覆盖到=该期无分红）
            events = bulk_div.get(code, []) if bulk_div is not None \
                else fetch_dividend(code)
            if bulk_div is not None:
                counters["div_bulk"] += 1
            if events is None:
                counters["div_fail"] += 1
            elif events:
                if div_known is not None:  # 只 upsert 本地没有的事件（div_new 计数才真实）
                    events = [(d, c) for d, c in events
                              if (code, d.strftime("%Y-%m-%d")) not in div_known]
                if events:
                    with db.write_lock():
                        _upsert_dividend(wconn, code, events)
                    counters["div_new"] += len(events)
                else:
                    counters["div_none"] += 1
            else:
                counters["div_none"] += 1
    except SyncCancelled:
        return                       # 用户停止：静默退出，不计入 errors（不是失败）
    except Exception as e:
        counters["err"] += 1
        counters.setdefault("errors", []).append(f"{code}: {e}")


def _upsert_daily(wconn, code: str, df: pd.DataFrame) -> None:
    # itertuples 比 iterrows 快 ~10 倍（全量同步每股 400 行 × 全市场收益显著）；
    # 列名均为 ASCII 可直接属性访问，缺列先补齐（fetch_daily 各源列不齐）
    for c in ("amount", "turnover", "outstanding_share"):
        if c not in df.columns:
            df[c] = None
    rows = [(
        str(code), r.date.strftime("%Y-%m-%d"),
        float(r.open) if pd.notna(r.open) else None,
        float(r.high) if pd.notna(r.high) else None,
        float(r.low) if pd.notna(r.low) else None,
        float(r.close) if pd.notna(r.close) else None,
        float(r.volume) if pd.notna(r.volume) else None,
        float(r.amount) if pd.notna(r.amount) else None,
        float(r.turnover) if pd.notna(r.turnover) else None,
        float(r.outstanding_share) if pd.notna(r.outstanding_share) else None,
    ) for r in df.itertuples(index=False)]
    wconn.executemany(
        "INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    wconn.commit()


def _upsert_valuation(wconn, code: str, df: pd.DataFrame) -> None:
    # 中文列名不能直接 itertuples 属性访问（pandas 会改名成 _1/_2），先重命名
    d = df.rename(columns={"数据日期": "d", "PE(TTM)": "pe", "市净率": "pb"})
    rows = [(
        str(code), r.d.strftime("%Y-%m-%d"),
        float(r.pe) if pd.notna(r.pe) else None,
        float(r.pb) if pd.notna(r.pb) else None,
    ) for r in d.itertuples(index=False)]
    wconn.executemany(
        "INSERT OR REPLACE INTO valuation VALUES (?,?,?,?)", rows)
    wconn.commit()


def _upsert_dividend(wconn, code: str, events: list) -> None:
    rows = [(str(code), d.strftime("%Y-%m-%d"), float(c)) for d, c in events]
    wconn.executemany(
        "INSERT OR REPLACE INTO dividend VALUES (?,?,?)", rows)
    wconn.commit()


def refresh_meta_and_sectors(refresh_sector: bool = False) -> dict:
    """刷新 meta 表（基于全市场快照 + 板块映射）。"""
    STATE.cb(0, 4, "准备基础数据：加载全市场快照")
    spot = fetch_spot()
    STATE.cb(1, 4, "准备基础数据：加载上市日期")
    listing = fetch_listing_dates()
    STATE.cb(2, 4, "准备基础数据：加载板块映射")
    sectors = {}
    try:
        boards = fetch_board_list()
        conn = db.writer()
        existing = set(r[0] for r in conn.execute(
            "SELECT name FROM sector_boards").fetchall())
        stored_names = list(existing)
        stored_map = {r[0]: r[1] for r in conn.execute(
            "SELECT code, sector FROM sector_map").fetchall()}
        if refresh_sector:
            conn.execute("DELETE FROM sector_map")
            conn.execute("DELETE FROM sector_boards")
            conn.commit()
            existing = set()
            stored_names = []
            stored_map = {}
        missing = [(bname, bcode) for bname, bcode in boards
                   if bname not in existing]
        # 成分接口逐板块一次请求（申万一级 31 个）：并行拉取（纯网络，8 并发），
        # 拉到即串行写库（单写者），整体从 30s+ 降到 ~5s
        if missing:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            done_boards = 0
            with ThreadPoolExecutor(max_workers=8,
                                    thread_name_prefix="sw") as pex:
                futs = {pex.submit(fetch_sector_members, bname, bcode): bname
                        for bname, bcode in missing}
                for fut in as_completed(futs):
                    bname = futs[fut]
                    try:
                        members = fut.result()
                    except Exception:  # noqa: BLE001 —— 单板块失败不影响其余
                        members = {}
                    done_boards += 1
                    if members:
                        with db.write_lock():
                            conn.executemany(
                                "INSERT OR REPLACE INTO sector_map(code, sector) "
                                "VALUES (?,?)", list(members.items()))
                            conn.execute(
                                "INSERT OR IGNORE INTO sector_boards(name) VALUES (?)",
                                (bname,))
                            conn.commit()
                        stored_map.update(members)
                        stored_names.append(bname)
                    STATE.cb(2, 4, f"准备基础数据：板块 {done_boards}/{len(missing)} {bname}")
        sectors = stored_map
    except Exception as e:
        _log.warning(f"板块映射获取失败: {e}")
    STATE.cb(3, 4, "准备基础数据：写入 meta")
    rows = []
    for _, r in spot.iterrows():
        code, name = str(r["code"]), str(r["name"])
        rows.append({
            "code": code, "name": name,
            "sector": sectors.get(code, "未分类"),
            "exchange": ranges.exchange_of(code),
            "listing_date": listing.get(code),
        })
    meta = pd.DataFrame(rows, columns=["code", "name", "sector",
                                       "exchange", "listing_date"])
    meta = meta.sort_values("code").reset_index(drop=True)
    conn = db.writer()
    conn.execute("DELETE FROM meta")
    conn.executemany(
        "INSERT OR REPLACE INTO meta VALUES (?,?,?,?,?)",
        [(r["code"], r["name"], r["sector"], r["exchange"],
          r["listing_date"].strftime("%Y-%m-%d") if r["listing_date"] else None)
         for r in meta.to_dict("records")])
    conn.commit()
    STATE.cb(4, 4, "基础信息就绪")
    return {"stocks": len(meta)}


def sync_all(
    exchange: Optional[str] = None,
    sectors: Optional[list] = None,
    kinds: tuple = ("daily", "valuation", "dividend"),
    force: bool = False,
    max_workers: int = 12,
    div_authoritative: bool = False,
) -> dict:
    """按范围增量同步（**带取消闸门生命周期管理**的入口）。

    【2026-10-12 停止同步修复】取消闸门 `_SYNC_CANCEL_EV` 的语义是「本轮同步的
    中断开关」：这里负责创建 / 销毁，`request_cancel()` 负责拉响。用 try/finally
    保证**任何返回路径**（正常完成 / 被取消 / 异常 / 提前 return）都会清空闸门，
    否则残留的 Event 会误伤之后 alert 等非同步流程对 fetch_spot/_call_ak 的调用。
    """
    global _SYNC_CANCEL_EV
    _SYNC_CANCEL_EV = threading.Event()
    try:
        return _sync_all_impl(exchange=exchange, sectors=sectors, kinds=kinds,
                              force=force, max_workers=max_workers,
                              div_authoritative=div_authoritative)
    finally:
        _SYNC_CANCEL_EV = None


def _sync_all_impl(
    exchange: Optional[str] = None,
    sectors: Optional[list] = None,
    kinds: tuple = ("daily", "valuation", "dividend"),
    force: bool = False,
    max_workers: int = 12,
    div_authoritative: bool = False,
) -> dict:
    """按范围增量同步。

    force=True 时清空当前范围内 daily/valuation 后重拉。
    **dividend 表在 force 下也不清空**：分红是只增不删的事实数据，清空后重拉
    反而会丢掉东财源缺失的中期/特别分红（实测近一年 12 条）——那是真实的
    「反向更新/数据丢失」。故分红恒为「只补新增」，force 仅把窗口放宽到 3 年。
    div_authoritative=True 时改走逐股 cninfo（权威完整但约 30 分钟），仅调试用。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with STATE.lock:
        if STATE.status == "running":
            return {"ok": False, "msg": "已有同步任务在运行中"}
        STATE.status = "running"
        STATE.frac = 0.0
        STATE.msg = "准备中…"
        STATE.logs.clear()
        STATE.last_counters = None
        STATE.cancel = False

    try:
        # ★ 停止同步检查点（最早入口）：用户可能在「准备中…」就点了停止
        if _cancelled():
            STATE.status = "cancelled"
            STATE.msg = "同步已停止"
            return {"ok": True, "cancelled": True, "counters": {}}
        conn = db.writer()
        db.ensure_schema(conn)
        reset_em_kline_breaker()  # 新一轮同步重新给东财直连机会
        # ★ 停止同步检查点（准备阶段入口）
        if _cancelled():
            STATE.status = "cancelled"
            STATE.msg = "同步已停止"
            return {"ok": True, "cancelled": True, "counters": {}}
        if exchange or sectors:
            codes = ranges.filter_meta_codes(exchange, sectors)
        else:
            codes = ranges.filter_meta_codes()
        meta_df = ranges.filter_meta_df(exchange, sectors)
        if force:
            # 注意：分红表不参与清空重拉（见 sync_all docstring）。分红只增不删，
            # force 仅通过 3 年窗口做更深回补，绝不删除已有事件。
            for t in kinds:
                if t in ("daily", "valuation"):
                    if not codes:
                        continue
                    for i in range(0, len(codes), 500):
                        chunk = codes[i:i + 500]
                        conn.execute(
                            f"DELETE FROM {t} WHERE code IN "
                            f"({','.join('?'*len(chunk))})", chunk)
                    conn.commit()
        counters = {"daily_new": 0, "daily_full": 0, "daily_none": 0,
                    "daily_fail": 0, "daily_skip": 0, "daily_bulk": 0,
                    "val_new": 0, "val_full": 0, "val_fail": 0, "val_skip": 0,
                    "val_bulk": 0,
                    "div_new": 0, "div_fail": 0, "div_none": 0, "div_bulk": 0,
                    "err": 0, "errors": []}
        total = len(codes)
        if total == 0:
            STATE.status = "done"
            STATE.msg = "范围内无股票"
            return {"ok": True, "counters": counters, "scope": exchange or "ALL"}

        # 分红检查频率控制：分红事件极低频（全市场日均除权除息仅数十只），
        # 且批量源有请求成本。3 天内检查过则本轮整体跳过（force=True 时强制检查；
        # 跳过只省无谓请求，不改动任何已有数据）。
        run_kinds = tuple(kinds)
        if "dividend" in kinds and not force:
            try:
                row = conn.execute(
                    "SELECT value FROM sync_state WHERE key='last_div_check'").fetchone()
                if row:
                    last_dc = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                    if (datetime.now() - last_dc).days < 3:
                        run_kinds = tuple(k for k in kinds if k != "dividend")
                        STATE.cb(0, total, "分红 3 天内已检查过，本轮跳过（日线/估值照常）")
            except Exception:  # noqa: BLE001
                pass

        # 交易日历：最近已完成交易日（节假日/盘中不误判；降级时为 None → 退回旧逻辑）
        last_trade = None
        try:
            from . import trade_cal
            last_trade = trade_cal.last_finished_trade_date()
        except Exception:  # noqa: BLE001
            last_trade = None

        # 批量预取各股最新日期（替代原来每股 2 次 MAX(date) 查询；主键索引下毫秒级）
        # 注意 MAX(date) 从 SQLite 返回的是 TEXT，须转 date 对象（daily 分支要做日期减法）
        last_daily: dict = {}
        last_valuation: dict = {}
        if not force:
            # 按需扫描（2026-10-12）：valuation 表 950 万行，GROUP BY 实测 ~830ms，
            # 在 kinds 不含该维度时纯属浪费，也拉长了「停止同步」的不可中断窗口。
            if "daily" in run_kinds:
                for c, d in conn.execute("SELECT code, MAX(date) FROM daily GROUP BY code"):
                    try:
                        last_daily[c] = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
                    except Exception:  # noqa: BLE001
                        pass
                # ★ 停止同步检查点（两个重扫描之间）：单次 GROUP BY 不可中断，
                #   拆开可把「取消后最多再等一次扫描」从 ~1s 降到 ~0.8s
                if _cancelled():
                    STATE.status = "cancelled"
                    STATE.msg = "同步已停止"
                    STATE.last_counters = counters
                    return {"ok": True, "cancelled": True, "counters": counters}
            if "valuation" in run_kinds:
                for c, d in conn.execute("SELECT code, MAX(date) FROM valuation GROUP BY code"):
                    last_valuation[c] = d
        # div_known 恒加载（含 force）：分红表永不清空，过滤掉本地已有事件即可
        # 保证「只增不删」，同时避免同 (code, ex_date) 被源端不同口径覆盖（反向更新）。
        div_known: set = set()
        for c, d in conn.execute("SELECT code, ex_date FROM dividend"):
            div_known.add((c, str(d)[:10]))

        # ---- 分红：批量预取（替代逐股 cninfo）----
        # v2：按「除权除息日窗口」并发拉取（11 页 ≈ 1.5s），替代原「按报告期」
        # （10 期 / 28 请求 ≈ 9.4s）。两级降级：窗口 → 按报告期 → 本轮跳过。
        bulk_div: Optional[dict] = None
        div_ran = False
        if "dividend" in run_kinds:
            if div_authoritative:
                # 逐股 cninfo：权威完整但约 30 分钟，仅调试/极端场景显式启用
                _log.warning("分红走权威逐股 cninfo（慢）")
                div_ran = True
            else:
                bulk_div, div_ran = _dividend_bulk_stage(conn, force)
                if not div_ran:
                    run_kinds = tuple(k for k in run_kinds if k != "dividend")
                    STATE.cb(0, total, "分红批量不可用，本轮跳过分红（下轮自动重试）")
        # ★ 停止同步检查点（分红批量阶段之后）：分红分页最长 ~2.5s，取消后不再继续
        if _cancelled():
            STATE.status = "cancelled"
            STATE.msg = "同步已停止"
            STATE.last_counters = counters
            return {"ok": True, "cancelled": True, "counters": counters}

        # ---- 估值：批量预取（东财 datacenter-web 支持 SECURITY_CODE in (...)）----
        # 实测（2026-09-07，100/300 只随机样本）：
        #   逐股 8 线程 4.85s vs 批量 50 只/组 1.03s → 4.7×；
        #   40 只样本 PE/PB 逐日逐值比对，批量与逐股 100% 一致，无缺行。
        # 只覆盖「本地已有估值历史、只需补近期」的股（start 可下推）；
        # 无本地历史需全量下载的仍走逐股 + 翻页，避免单请求被撑爆。
        # 任一组失败 → 该组不进 bulk_val → _sync_one 自动退回逐股，不会漏数据。
        bulk_val: Optional[dict] = None
        if "valuation" in run_kinds and last_trade:
            try:
                need: list = []
                for c in codes:
                    lv = last_valuation.get(c)
                    if lv and _iso(lv) < last_trade:
                        try:
                            sd = datetime.strptime(str(lv)[:10], "%Y-%m-%d").date() \
                                - timedelta(days=10)
                        except Exception:  # noqa: BLE001
                            continue
                        need.append((c, sd))
                if need:
                    STATE.cb(0, total, f"估值：批量预取 {len(need)} 只…")
                    # 按 start 升序分组：同组内 start 接近 → 取组内最早 start，
                    # 冗余回传最少（组内日期跨度小，多拉的行数可忽略）
                    need.sort(key=lambda x: x[1])
                    chunks = [need[i:i + 50] for i in range(0, len(need), 50)]
                    bulk_val = {}
                    _vlock = threading.Lock()

                    def _val_chunk(ch):
                        if _cancelled():      # ★ 取消后不再发起新的批量请求（快速排空队列）
                            return 0
                        st = min(x[1] for x in ch).strftime("%Y-%m-%d")
                        try:
                            d = _fetch_value_em_bulk([x[0] for x in ch], st, 25)
                        except Exception as e:  # noqa: BLE001
                            _log.warning(f"估值批量组失败({len(ch)}只/{st}): {e}，退回逐股")
                            return 0
                        with _vlock:
                            bulk_val.update(d)
                        return len(d)

                    with ThreadPoolExecutor(max_workers=4) as _vp:
                        list(_vp.map(_val_chunk, chunks))
                    _log.info(f"估值批量就绪：{len(bulk_val)} 只 / {len(chunks)} 个请求"
                              f"（原需 {len(need)} 次）")
                    if len(bulk_val) < len(need) * 0.5:
                        _log.warning(f"估值批量覆盖偏低 {len(bulk_val)}/{len(need)}，"
                                     f"差额部分自动退回逐股")
            except Exception as e:  # noqa: BLE001
                _log.warning(f"估值批量预取异常，全部退回逐股: {e}")
                bulk_val = None

        # ★ 停止同步检查点（估值批量阶段之后）：估值批量是准备阶段最重的一步
        if _cancelled():
            STATE.status = "cancelled"
            STATE.msg = "同步已停止"
            STATE.last_counters = counters
            return {"ok": True, "cancelled": True, "counters": counters}
        STATE.cb(0, total, f"数据同步：{'全量' if force else '增量'}，"
                            f"范围 {exchange or 'ALL'}，{total} 只"
                            f"{'（分红批量）' if bulk_div is not None else ''}"
                            f"{'（估值批量）' if bulk_val is not None else ''}")
        rows = meta_df.to_dict("records")
        cancelled = False
        ex = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {ex.submit(_sync_one, r["code"], r, run_kinds, conn, counters,
                                 last_daily, last_valuation, last_trade,
                                 div_known,
                                 bulk_div, bulk_val): r["code"]
                       for r in rows}
            done = 0
            # 【2026-10-12 停止同步修复】原实现直接 `for fut in as_completed(futures)`：
            # as_completed 是**阻塞**的，必须等「下一个任务完成」才轮到循环体检查
            # STATE.cancel —— 单个任务最长 15s 超时 ×3 重试 ≈ 45s，于是「停止同步」
            # 要等几十秒才生效。改为 0.25s 超时的轮询：取消请求最多 0.25s 被主循环看到，
            # 随即 break 并取消队列中尚未启动的任务（已在跑的少数任务由 _sync_one /
            # _call_ak 内的检查点在下一个维度边界/轮询窗口自行退出）。
            pending = set(futures)
            reported = 0
            while pending:
                if STATE.cancel:
                    cancelled = True
                    break
                done_now, pending = _cf.wait(pending, timeout=0.25,
                                             return_when=_cf.FIRST_COMPLETED)
                if done_now:
                    done += len(done_now)
                    if done - reported >= 5 or done == total:
                        reported = done
                        STATE.cb(done, total,
                                 f"数据同步 {done}/{total} | "
                                 f"日K+{counters['daily_new']} 估值+{counters['val_new']}")
        finally:
            # 停止：立即返回，取消尚未执行的 future；已运行中的少数任务会尽快自行退出
            # （写入为 INSERT OR REPLACE 幂等，不破坏数据）
            ex.shutdown(wait=False, cancel_futures=True)
        if cancelled:
            STATE.status = "cancelled"
            STATE.msg = "同步已停止"
            STATE.last_counters = counters
            return {"ok": True, "cancelled": True, "counters": counters}
        STATE.last_counters = counters
        STATE.cb(total, total, "数据同步完成")
        # 分红本轮实际检查过 → 记录检查时间（供 3 天频率控制）
        if "dividend" in run_kinds:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?,?)",
                    ("last_div_check", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            except Exception:  # noqa: BLE001
                pass
        # 同步后刷新快照表（~15s）：本轮确有新数据才值得重建，纯跳过轮直接省掉
        new_rows = (counters["daily_new"] + counters["val_new"] + counters["div_new"])
        if new_rows > 0:
            try:
                from . import snapshot as _snap
                STATE.msg = "刷新快照表…"
                _snap.build_snapshot(
                    progress_cb=lambda f, t, m: STATE.cb(f, t, f"快照 {m}"))
            except Exception as e:
                _log.warning(f"快照构建失败: {e}")
        else:
            STATE.cb(total, total, "数据已是最新，跳过快照重建")
        STATE.status = "done"
        # 标记同步状态
        try:
            conn.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES (?,?)",
                         ("last_sync", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
        except Exception:
            pass
        return {"ok": True, "counters": counters,
                "scope": exchange or "ALL", "total": total}
    except Exception as e:
        STATE.status = "failed"
        STATE.msg = f"同步失败: {e}"
        _log.exception(e)
        return {"ok": False, "msg": str(e)}


def start_sync_thread(exchange, sectors, kinds, force=False, max_workers=12):
    """后台启动一次同步任务。

    用 daemon=False：主进程退出时要等同步跑完再退，避免「关窗口 = 同步
    被拦腰砍断」导致数据写一半、下次又要全量重跑。uvicorn 正常退出
    （Ctrl+C）时线程仍会跑完当前一轮。
    """
    if STATE.status == "running":
        return False
    t = threading.Thread(
        target=sync_all,
        args=(exchange, sectors, kinds),
        kwargs={"force": force, "max_workers": max_workers},
        daemon=False,
        name="sync-all",
    )
    t.start()
    return True