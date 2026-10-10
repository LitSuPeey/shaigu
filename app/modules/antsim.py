# -*- coding: utf-8 -*-
"""蚂蚁呀欸 · 样本形态匹配 v2.1（核心样本为中心 + 多时期样本加权 + 分位数门槛）。

修订要点
========
v2（2026-10-02）
  用户指出**原样本（样本.txt，48 只）全部取自同一波行情**（2026/05~08），
  信息高度冗余、不构成独立形态依据 → 改成**三层样本组 + 分组权重**：

    组    | 来源                          | 权重 | 作用
    ------|-------------------------------|------|------------------------------
    核心  | 000759.SZ 2026/06/04~08/13    | 1.00 | 整个筛选的**唯一中心**（硬规则）
    多元  | data/ant_samples_diverse.txt  | 0.75 | 跨 2021/22/23/25/26 五个时期，提供形态普适性
    同期  | data/ant_samples.txt          | 0.60 | 原样本（同一波行情），削弱后作参考

    综合相似度 = Σ(组权重 × 组内最佳相似度) / Σ(组权重)   ← 线性加权平均，可逐项复核

v2.1（同日实测后修正，两处真缺陷）
  ① **固定门槛曲线失效**：三组平均把综合分压缩到 84.8 封顶，
     原 `sim_min = 100 − 20×(100/strict)` 在 strict ≥ 160% 时门槛 87.5 > 上限
     → **滑块上半段恒为 0 命中**，等于废掉。
     → 改为**横截面分位数门槛**（与项目 snap.py 的既有惯例一致：
       研究报告的绝对阈值不可照搬，一律用全市场分位数）：
         keep(strict) = clamp(keep_base × (100/strict)^2, min, max)   ← 严格程度 = 保留比例
         门槛 = 该阶段桶内综合分的 (1 − keep) 分位数
       于是 strict 越大保留越少，50%~200% 全程单调可用。
  ② **分类被高分档吃掉**：strict=100% 时命中的 11 只**全是「周期进行中」**。
     因为「刚刚启动」窗口短（m = 样本bar数 × f，f ≤ 0.5），分数天然偏低而被先筛掉。
     → 改为**分阶段各自取前 keep%**（阶段桶内分位数），保证 刚启动 / 进行中 两类
       在任何严格程度下都有代表，分类不再形同虚设。

v2.2（同日代码复审后修，10 处）
  复审（读代码 + 独立重算，不只看测试是否通过）发现：
  ① ★`sim_mat` 初值 0 同时承担「未评分」与「相似度恰为 0」两种语义 ——
     后者是**合法取值**（候选与模板高度反向：cr/mp/cv 全被 clip 到 0 → raw=0），
     于是这类候选会被误当「没评过」而漏掉。→ 补回显式 `has_mat` 布尔矩阵。
  ② `stage_arr` 对未评分候选默认判为「周期刚刚启动」→ 改用哨兵 `"—"`，
     消除「忘带 scored 掩码就出错」的隐患（所有使用点现在天然安全）。
  ③ 上报门槛原为分位数原始值，**插值可能得 80.46 而最低入选行显示 80.45** ——
     判定一直用 raw 值从未错，但展示经不起肉眼复核。→ 上报「实际入选集合最小值」，
     分位数门槛移入 internal_notes 保留可追溯性。
  ④ 明细里的窗口根数 m 原由 `round(bars×f)` **二次推导**，推导式一旦与打分循环
     不同步就静默偏一位 → 改为循环内直接记录 `m_mat`，从结构上杜绝。
  ⑤ `max_results` 截断后 `hits` 只反映截断后的数 → 增加 `passed`/`truncated` 并写入 notes。
  ⑥ 硬过滤的参照日原取「第一只非空股票的最后一根」（遇长期停牌股会偏早，
     使上市天数判定整体偏松）→ 改取全市场最大日期。
  ⑦ 组级缺失（候选 bar 不足、与该组任何模板都比不了）时「多元/同期相似度」
     原显示 0.0（读起来像「完全不相似」）→ 改显示 `—`（不可比），并新增 `missing_group` 计数。
  ⑧ `hist` 段对齐只用**均匀中位比**，遇到分段口径差异（如 603456 在 2026-09-08
     前后的阶跃）无法完全对齐 → 新增重叠区比值离散度检测，超阈值时明确告警
     「疑似分段口径差异，请人工复核」，不再暗示已对齐。当前样本清单不含跨阶跃窗口。
  ⑨ 样本区间起止写反原会因 SQL BETWEEN 取空而被误报成「无行情数据」→ 显式报「起止颠倒」。
  ⑩ `_gsample` 死判断与未使用的 `f_mat` 参数、明细里不可达的 `else cmat[i]` 兜底分支
     （一旦可达会取到含 NaN 的整行）—— 一并清理。

筛选口径（最终）
================
1. 模板：样本区间收盘价 → 重采样 RESAMPLE_POINTS 点 → 取对数 → 行内 z-score，
   与价格高低、涨跌幅度无关，只剩「形状」；成交量同法处理。
2. 候选：每只股票**最近 m 根 bar** 对模板的**前 m 根**，m = round(样本bar数 × f)，
   f ∈ FRACTIONS。f 小 = 只走完样本前段；f 大 = 走完中后段。
   阶段取自**核心样本**的最佳 f：f ≤ start_frac_max → 周期刚刚启动，否则 周期进行中。
3. 单模板相似度 = 100 × (w_shape×价形相关 + w_amp×幅度接近度 + w_vol×量形相关)。
4. 硬门槛 = 综合相似度 ≥ 本阶段桶门槛 **且** 核心相似度 ≥ 本阶段桶门槛。
   （「以核心为核心」：核心自己都不像的股票，靠多元/同期组拉不进来。）
5. strict > 180 → 只用核心样本（后端硬规则，不在前端展示）。
   三层退化为单层，综合 ≡ 核心，门槛与阶段仍按上述规则生效，自洽。

为什么必须换后端而不是只调 ant.py 参数
======================================
ant.py 的判据是「60日振幅落在 [20%,45%]」「回归 p 值 > 0.05」这类**形状无关**的
标量门槛，任何一段震荡都会通过，数学上无法表达「走势像不像这个样本」。
样本相似是**路径级**逐点比对问题，故新增本模块；ant.py 原样保留为次级子页签。

数据口径（全部实测，2026-10-02）
================================
- 候选行情：unified_data.db（最近 WINDOW_DAYS 个交易日）。
- 模板行情：unified 优先；老时期（2021/22/23、2025 上半年）回退 data/hist.db。
  实测重叠区多数逐行完全相同；603456 存在 1.0067 的阶跃（unified 于 2026-09-08
  做了一次前复权、hist 未做），**但该样本区间内 hist 行会被全部丢弃，不影响结果**；
  仅当 hist 段确有保留行时才按重叠区中位比对齐，避免把无害差异写成告警。
- hist 样本段确认无单日 >15% 跳变；3 个样本区间内含除权除息日
  （002474/300631 2021-05-28、600499 2025-05-26），该日涨跌幅均正常（无跳空）
  → hist 为前复权，窗口内形状干净。
- 920270.BJ 无长历史数据（长历史源不支持北交所）→ 标记不可用，**不静默丢弃**。

短窗口偏置
==========
窗口越短，z-score 相关越容易虚高（点少 → 噪声大 → 极值大）。故对 m < ref_len 的
窗口按 len_penalty × (1 − m/ref_len) 扣分，使不同长度窗口分数可比。
该惩罚**同时用于排序与显示**（排序键 == 显示值，不出现两套口径）。

口径声明（研究用，不构成投资建议）
==================================
- 按用户要求**不做**样本外/过拟合显著性检验（「允许过拟合」）；排名不代表未来收益。
- 样本量小（核心 1 + 多元 20 + 同期 48），生存者偏差明显，请勿据此下单。
- 全部数值只来自本地真实行情库，无合成数据、无模型记忆补数。
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import date
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..core import batchload, db

# ---- 常量 ----
RESAMPLE_POINTS = 32
FRACTIONS = (0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00)
MIN_BARS = 12
MAX_BARS = 160
WINDOW_DAYS = 160
START_FRAC_MAX = 0.50
STRICT_MIN, STRICT_MAX = 50.0, 200.0
STRICT_CORE_ONLY = 180.0          # 严格程度超过此值 → 只用核心样本（后端硬规则）

GROUP_CORE, GROUP_DIVERSE, GROUP_LEGACY = "核心", "多元", "同期"
GROUP_ORDER = (GROUP_CORE, GROUP_DIVERSE, GROUP_LEGACY)

# 核心样本：整个筛选模式的中心（硬规则，不在前端展示）
CORE_SAMPLE = {"code": "000759", "exchange": "SZ",
               "start": "2026-06-04", "end": "2026-08-13"}
GROUP_WEIGHTS_DEFAULT = {GROUP_CORE: 1.00, GROUP_DIVERSE: 0.75, GROUP_LEGACY: 0.60}

SAMPLE_FILE_LEGACY = "ant_samples.txt"           # data/ 下：原同期样本
SAMPLE_FILE_DIVERSE = "ant_samples_diverse.txt"  # data/ 下：多时期样本

STAGE_START = "周期刚刚启动"
STAGE_ONGOING = "周期进行中"
STAGE_ALL = (STAGE_START, STAGE_ONGOING)

ANTSIM_CONFIG = {
    "strict": 100.0,
    # 分位数门槛：严格程度 = 保留比例
    "keep_at_100": 0.012,     # strict=100% 时每个阶段桶保留前 1.2%
    "keep_exp": 2.0,          # keep ∝ (100/strict)^exp
    "keep_min": 0.0008,
    "keep_max": 0.50,
    # strict > 180 进入「仅核心样本」后，候选×模板对由 69 组降到 1 组，
    # 单口径只有一道门槛，会比「综合 ∩ 核心」双门槛宽松 → 实测命中数会**反弹**
    # （180% 时 8 只 → 181% 时 18 只），违反「越严越少」。故保留比例再乘本系数。
    "core_only_keep_factor": 0.30,
    # 单模板相似度权重
    "w_shape": 0.65, "w_amp": 0.20, "w_vol": 0.15,
    "ref_len": 36.0, "len_penalty": 6.0,
    "min_bars": MIN_BARS,
    "start_frac_max": START_FRAC_MAX,
    "group_weights": dict(GROUP_WEIGHTS_DEFAULT),
    "back_q": 0.99,           # 「背书」= 单模板相似度进入全体候选对的前 1%
    "hard_filter": {
        "remove_st": True,
        "min_listed_days": 120,
        "min_amt20_yi": 0.30,
        "min_price": 2.0,
        "max_price": 3000.0,
    },
    "max_results": 300,
}

OUTPUT_COLS = ["代码", "名称", "板块", "周期阶段", "综合相似度",
               "核心相似度", "核心进度%", "覆盖天数",
               "多元相似度", "多元样本", "同期相似度", "同期样本",
               "背书样本数", "样本涨幅%", "自身涨幅%",
               "价形相关", "量形相关", "匹配样本", "收盘", "末端日期"]


# ============ 严格程度 → 保留比例 / 门槛 ============
def clamp_strict(strict: float) -> float:
    try:
        v = float(strict)
    except (TypeError, ValueError):
        v = 100.0
    if not np.isfinite(v):
        v = 100.0
    return float(min(STRICT_MAX, max(STRICT_MIN, v)))


def keep_ratio(strict: float, at100: float = 0.012, exp: float = 2.0,
               lo: float = 0.0008, hi: float = 0.50) -> float:
    """严格程度 → 每阶段保留比例。strict 越大保留越少（容差 ∝ (100/strict)^exp）。"""
    t = max(1e-6, clamp_strict(strict))
    return float(min(hi, max(lo, float(at100) * (100.0 / t) ** float(exp))))


def effective_keep(conf: Dict) -> float:
    """实际保留比例：进入「仅核心样本」模式时再收紧，保持「越严越少」单调。"""
    k = keep_ratio(conf["strict"], conf["keep_at_100"], conf["keep_exp"],
                   conf["keep_min"], conf["keep_max"])
    if core_only(conf["strict"]):
        k = max(float(conf["keep_min"]),
                k * float(conf.get("core_only_keep_factor", 0.30)))
    return float(k)


def length_penalty(m: int, ref_len: float = 36.0, k: float = 6.0) -> float:
    """短窗口扣分：m ≥ ref_len 不扣；m 越小扣越多。"""
    if ref_len <= 0:
        return 0.0
    return float(k * max(0.0, 1.0 - float(m) / float(ref_len)))


def stage_of(f: float, start_frac_max: float = START_FRAC_MAX) -> str:
    return STAGE_START if float(f) <= float(start_frac_max) + 1e-9 else STAGE_ONGOING


def core_only(strict: float) -> bool:
    """严格程度 > 180 时只用核心样本（后端硬规则）。"""
    return clamp_strict(strict) > STRICT_CORE_ONLY


# ============ 数值工具（按行批量化） ============
def _resample_rows(mat: np.ndarray, points: int) -> np.ndarray:
    n, m = mat.shape
    if m == points:
        return np.asarray(mat, dtype=float).copy()
    idx = np.linspace(0.0, float(m - 1), int(points))
    i0 = np.floor(idx).astype(int)
    i1 = np.minimum(i0 + 1, m - 1)
    w = (idx - i0)[None, :]
    return mat[:, i0] * (1.0 - w) + mat[:, i1] * w


def _z_rows(mat: np.ndarray, use_log: bool = True) -> np.ndarray:
    x = np.asarray(mat, dtype=float)
    if use_log:
        x = np.log(np.maximum(x, 1e-9))
    mu = x.mean(axis=1, keepdims=True)
    sd = x.std(axis=1, keepdims=True)
    sd = np.where(sd < 1e-12, 1.0, sd)
    return (x - mu) / sd


def _build_vector(vec: np.ndarray, points: int, use_log: bool = True) -> np.ndarray:
    """单条序列 → 重采样 + z-score（与候选同一口径，相关可直接比较）。"""
    arr = np.asarray(vec, dtype=float).reshape(1, -1)
    r = _resample_rows(arr, points)[0]
    x = np.log(np.maximum(r, 1e-9)) if use_log else r
    sd = float(x.std())
    if sd < 1e-12:
        sd = 1.0
    return (x - float(x.mean())) / sd


# ============ 样本清单 ============
_TOKEN_SPLIT = re.compile(r"[,;\n\r\t，；、]+")
_RANGE_RE = re.compile(
    r"^(\d{4}[/-]\d{1,2}[/-]\d{1,2})\s*(?:-|~|—|－|至|到)\s*"
    r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})$")


def normalize_code(raw: str) -> Tuple[str, str]:
    s = str(raw or "").strip().upper().replace(" ", "")
    m = re.match(r"^(\d{6})\.?(SH|SZ|BJ)$", s)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^(SH|SZ|BJ)\.?(\d{6})$", s)
    if m:
        return m.group(2), m.group(1)
    m = re.match(r"^(\d{6})$", s)
    if m:
        return m.group(1), ""
    return s, ""


def parse_samples(text: str) -> List[Dict[str, str]]:
    """解析样本清单：`CODE:起-止`，支持「沿用上一代码」续写；分隔 逗号/换行/分号。

    例：``000759.SZ:2026/05/26-2026/08/13,2026/06/04-2026/08/13`` → 同代码两区间。
    """
    items: List[Dict[str, str]] = []
    cur_code: Optional[str] = None
    for tok in _TOKEN_SPLIT.split(str(text or "")):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok or "：" in tok:
            head, tail = re.split(r"[:：]", tok, maxsplit=1)
            cur_code, rng = head.strip(), tail.strip()
        else:
            rng = tok
        if not cur_code:
            continue
        m = _RANGE_RE.match(rng)
        if not m:
            continue
        code6, exch = normalize_code(cur_code)
        items.append({"raw": cur_code, "code": code6, "exchange": exch,
                      "start": m.group(1).replace("/", "-"),
                      "end": m.group(2).replace("/", "-")})
    seen, out = set(), []
    for it in items:
        key = (it["code"], it["start"], it["end"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def data_dir() -> str:
    return os.path.dirname(os.path.abspath(db.db_path())) if db.db_path() else ""


def _read_text(name: str) -> str:
    d = data_dir()
    if not d:
        return ""
    p = os.path.join(d, name)
    if not os.path.isfile(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:  # noqa: BLE001
        return ""


def load_groups() -> Tuple[Dict[str, List[Dict]], Dict[str, str]]:
    """读三组样本：核心（硬规则）> 多元 > 同期，按优先级去重。"""
    core = dict(CORE_SAMPLE)
    core["raw"] = f"{CORE_SAMPLE['code']}.{CORE_SAMPLE['exchange']}"
    diverse = parse_samples(_read_text(SAMPLE_FILE_DIVERSE))
    legacy = parse_samples(_read_text(SAMPLE_FILE_LEGACY))
    seen = {(core["code"], core["start"], core["end"])}
    out = {GROUP_CORE: [core], GROUP_DIVERSE: [], GROUP_LEGACY: []}
    for g, src in ((GROUP_DIVERSE, diverse), (GROUP_LEGACY, legacy)):
        for it in src:
            key = (it["code"], it["start"], it["end"])
            if key in seen:
                continue          # 已被核心/更高优先组占用 → 不重复计数
            seen.add(key)
            out[g].append(it)
    texts = {GROUP_DIVERSE: _read_text(SAMPLE_FILE_DIVERSE),
             GROUP_LEGACY: _read_text(SAMPLE_FILE_LEGACY)}
    return out, texts


def save_samples_text(group: str, text: str) -> Dict:
    """写回某组的样本文件（核心组只读）。"""
    name = {GROUP_DIVERSE: SAMPLE_FILE_DIVERSE,
            GROUP_LEGACY: SAMPLE_FILE_LEGACY}.get(group)
    if not name:
        return {"ok": False, "error": f"未知/只读样本组：{group}"}
    d = data_dir()
    if not d:
        return {"ok": False, "error": "数据库路径未设置"}
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        fh.write(str(text or "").strip() + "\n")
    batchload.bump_version()
    return {"ok": True, "file": name, "count": len(parse_samples(text)), "group": group}


def samples_info() -> Dict:
    groups, texts = load_groups()
    payload = []
    for g in GROUP_ORDER:
        items = groups[g]
        payload.append({
            "group": g,
            "weight": GROUP_WEIGHTS_DEFAULT[g],
            "count": len(items),
            "readonly": g == GROUP_CORE,
            "text": texts.get(g, ""),
            "items": [f"{it['code']}.{it['exchange']}:{it['start']}~{it['end']}"
                      for it in items],
        })
    return {"groups": payload,
            "core": f"{CORE_SAMPLE['code']}.{CORE_SAMPLE['exchange']}:"
                    f"{CORE_SAMPLE['start']}~{CORE_SAMPLE['end']}",
            "core_only_over": STRICT_CORE_ONLY}


# ============ 行情加载（unified 优先 → hist 回退） ============
_HIST_LOCK = threading.Lock()
_HIST_CONN = None


def _hist_conn() -> Optional[sqlite3.Connection]:
    global _HIST_CONN
    d = data_dir()
    if not d:
        return None
    path = os.path.join(d, "hist.db")
    if not os.path.isfile(path):
        return None
    with _HIST_LOCK:
        if _HIST_CONN is None:
            abs_path = os.path.abspath(path).replace("\\", "/")
            conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True, timeout=60)
            conn.execute("PRAGMA cache_size = -65536")
            conn.execute("PRAGMA query_only = ON")
            _HIST_CONN = conn
        return _HIST_CONN


def _q_unified(code: str, start: str, end: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT date, close, volume FROM daily WHERE code=? "
        "AND date>=? AND date<=? ORDER BY date",
        db.reader(), params=(str(code), start, end))


def _q_hist(code: str, start: str, end: str) -> pd.DataFrame:
    conn = _hist_conn()
    if conn is None:
        return pd.DataFrame(columns=["date", "close", "volume"])
    try:
        return pd.read_sql_query(
            "SELECT date, close, volume FROM daily_hist WHERE code=? "
            "AND date>=? AND date<=? ORDER BY date",
            conn, params=(str(code), start, end))
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["date", "close", "volume"])


def _load_window(code: str, start: str, end: str,
                 notes: Optional[List[str]] = None) -> pd.DataFrame:
    """取 code 在 [start, end] 的行情：unified 优先，unified 未覆盖的段从 hist 补。

    v2.1 修正：只有**确实保留了 hist 行**时才做口径对齐与告警。
    原实现在重叠区无条件计算比值并写告警，对「样本区间完全落在 unified 内」
    的常见情况（如 603456 因 2026-09-08 前复权差异比值 1.0068）会产生
    **无害却刺眼的误报**——那批 hist 行随后就被整段丢弃，比对结果毫无影响。
    """
    du = _q_unified(code, start, end)
    dh = _q_hist(code, start, end)
    if dh.empty:
        return du
    if du.empty:
        return dh
    # 先剔除与 unified 重叠的 hist 行；无剩余则直接返回 unified
    have = set(du["date"].astype(str))
    dh_keep = dh[~dh["date"].astype(str).isin(have)]
    if dh_keep.empty:
        return du
    # 确有 hist 保留行 → 用重叠区中位比做口径对齐（两库同基准时为 1.0）
    ov = du.merge(dh, on="date", suffixes=("_u", "_h"))
    if len(ov):
        cu = ov["close_u"].to_numpy(dtype=float)
        ch = ov["close_h"].to_numpy(dtype=float)
        ok = (np.abs(ch) > 1e-9) & (np.abs(cu) > 1e-9)
        if ok.any():
            rr = cu[ok] / ch[ok]
            r = float(np.median(rr))
            # ★比值本身的离散度：若两库差异是**阶跃**（某日做了前复权、另一库没做，
            #   如 603456 在 2026-09-08 前恒 1.0067 / 之后恒 1.0），中位比只能对齐
            #   多数一侧，跨阶跃的那一段仍会偏。此处只告警，不假装已完全对齐。
            spread = float(np.std(rr) / abs(r)) if abs(r) > 1e-12 else 0.0
            if np.isfinite(r) and abs(r - 1.0) > 0.005:
                dh_keep = dh_keep.copy()
                dh_keep["close"] = dh_keep["close"] * r
                if notes is not None:
                    notes.append(f"⚠ {code} 两库价格口径比 {r:.4f} ≠ 1，"
                                 f"已按重叠区中位比对齐 hist 段（{start}~{end}）")
            if spread > 0.001 and notes is not None:
                notes.append(f"⚠ {code} 重叠区两库比值离散度 {spread*100:.2f}% 偏大"
                             f"（{start}~{end}）→ 疑似分段口径差异，均匀中位比"
                             f"无法完全对齐，该样本结果请人工复核")
    return pd.concat([dh_keep, du], ignore_index=True) \
             .sort_values("date").reset_index(drop=True)


# ============ 模板 ============
def build_templates(groups: Dict[str, List[Dict]], notes: Optional[List[str]] = None,
                    min_bars: int = MIN_BARS) -> List[Dict]:
    """建模板；数据不足的样本标记 ok=False 并给出原因（不静默丢弃）。"""
    temps: List[Dict] = []
    for g in GROUP_ORDER:
        for it in groups.get(g, []):
            base = {"group": g, "code": it["code"], "exchange": it.get("exchange", ""),
                    "start": it["start"], "end": it["end"], "ok": False, "reason": ""}
            # 起止颠倒必须先报出来：否则 SQL BETWEEN 取不到行，会被误报成「无行情数据」
            if str(it["start"]) > str(it["end"]):
                base["reason"] = f"区间起止颠倒（{it['start']} > {it['end']}）"
                temps.append(base)
                continue
            try:
                d = _load_window(it["code"], it["start"], it["end"], notes)
            except Exception as e:  # noqa: BLE001
                base["reason"] = f"读取失败：{type(e).__name__}"
                temps.append(base)
                continue
            if d is None or d.empty:
                base["reason"] = "无行情数据"
                temps.append(base)
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.dropna(subset=["close"])
            n = int(len(d))
            if n < int(min_bars):
                base["reason"] = f"样本区间仅 {n} 根 bar（< {min_bars}）"
                temps.append(base)
                continue
            if n > MAX_BARS:
                d = d.tail(MAX_BARS)
                n = int(len(d))
            close = d["close"].to_numpy(dtype=float)
            vol = pd.to_numeric(d["volume"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
            if not np.all(np.isfinite(close)) or close.min() <= 0:
                base["reason"] = "样本价格含缺失/非正"
                temps.append(base)
                continue
            temps.append({
                "group": g, "code": it["code"], "exchange": it.get("exchange", ""),
                "start": str(d["date"].iloc[0].date()),
                "end": str(d["date"].iloc[-1].date()),
                "ok": True, "reason": "", "bars": n,
                "close": close, "vol": vol,
                "ret": float(close[-1] / close[0] - 1.0),
            })
    return temps


def _template_prefixes(t: Dict, fractions=FRACTIONS, points: int = RESAMPLE_POINTS,
                       min_bars: int = MIN_BARS) -> List[Dict]:
    """模板 → 各进度档位的前缀向量（m = round(bars × f)）。"""
    out = []
    bars = int(t["bars"])
    close, vol = t["close"], t["vol"]
    for f in fractions:
        m = int(round(bars * float(f)))
        if m < int(min_bars):
            continue
        m = min(m, bars)
        out.append({"f": float(f), "m": m,
                    "vec_px": _build_vector(close[:m], points, use_log=True),
                    "vec_vol": _build_vector(vol[:m], points, use_log=True),
                    "ret": float(close[m - 1] / close[0] - 1.0)})
    return out


def active_groups(temps: List[Dict], strict: float) -> List[str]:
    """当前严格程度下激活的样本组；strict > 180 → 只用核心组（硬规则）。"""
    if core_only(strict):
        return [GROUP_CORE]
    return [g for g in GROUP_ORDER if any(t["ok"] and t["group"] == g for t in temps)]


# ============ 候选硬过滤 ============
def _hard_filter(meta: pd.DataFrame, daily_map: Dict[str, pd.DataFrame],
                 cfg: Dict) -> Dict[str, str]:
    hf = cfg["hard_filter"]
    bad: Dict[str, str] = {}
    # 参照日必须取**全市场最后一根 bar 的最大日期**：
    # 原先「取第一只非空股票的最后一根就 break」在遇到长期停牌股时会拿到偏早的日期，
    # 使 min_listed_days（上市天数）判定整体偏松。
    _lasts = [pd.Timestamp(df["date"].iloc[-1]).date()
              for df in daily_map.values() if df is not None and len(df)]
    ref = max(_lasts) if _lasts else date.today()
    for _, row in meta.iterrows():
        code = str(row["code"])
        name = str(row.get("name") or "")
        if hf.get("remove_st", True) and re.search(r"ST|退", name.upper()):
            bad[code] = "ST/退市"
            continue
        ld = str(row.get("listing_date") or "")
        if ld:
            try:
                if (ref - pd.Timestamp(ld).date()).days < int(hf.get("min_listed_days", 120)):
                    bad[code] = "上市不足"
                    continue
            except Exception:  # noqa: BLE001
                pass
        df = daily_map.get(code)
        if df is None or len(df) < MIN_BARS:
            bad[code] = "行情不足"
            continue
        px = float(df["close"].iloc[-1])
        if px < float(hf.get("min_price", 0)) or px > float(hf.get("max_price", 1e9)):
            bad[code] = "价格越界"
            continue
        amt = df["amount"].tail(20)
        if not len(amt) or float(amt.mean()) / 1e8 < float(hf.get("min_amt20_yi", 0)):
            bad[code] = "流动性不足"
            continue
    return bad


# ============ 主流程 ============
def run(cfg: Optional[Dict] = None, exchange: Optional[str] = None,
        sectors: Optional[List[str]] = None, strict: Optional[float] = None,
        progress_cb=None, logs: Optional[List[str]] = None) -> Dict:
    """样本形态匹配主入口。返回 {rows, stats, templates, notes}。"""
    conf = dict(ANTSIM_CONFIG)
    conf.update({k: v for k, v in (cfg or {}).items() if k in ANTSIM_CONFIG})
    hf = dict(ANTSIM_CONFIG["hard_filter"])
    hf.update((cfg or {}).get("hard_filter") or {})
    conf["hard_filter"] = hf
    gw = dict(GROUP_WEIGHTS_DEFAULT)
    gw.update(conf.get("group_weights") or {})
    conf["group_weights"] = gw
    if strict is not None:
        conf["strict"] = strict
    conf["strict"] = clamp_strict(conf["strict"])
    keep = effective_keep(conf)
    logs = [] if logs is None else logs
    notes: List[str] = []

    if progress_cb:
        progress_cb(0, 1, "样本形态匹配：加载样本与行情…")

    # ---- 样本 → 模板 ----
    groups, _texts = load_groups()
    all_temps = build_templates(groups, notes, conf["min_bars"])
    acts = active_groups(all_temps, conf["strict"])
    ok_temps = [t for t in all_temps if t["ok"] and t["group"] in acts]
    bad_temps = [t for t in all_temps if (not t["ok"]) and t["group"] in acts]
    if bad_temps:
        notes.append("以下样本数据不足未参与匹配：" +
                     "；".join(f"[{t['group']}]{t['code']} {t['start']}~{t['end']}"
                               f"（{t['reason']}）" for t in bad_temps)[:400])
    empty = {"rows": [], "templates": _temp_brief(all_temps), "notes": notes}
    if GROUP_CORE not in acts:
        notes.append(f"核心样本 {CORE_SAMPLE['code']} {CORE_SAMPLE['start']}~"
                     f"{CORE_SAMPLE['end']} 不可用；本模块以核心样本为中心，故不产出结果")
        return {**empty, "stats": {"strict": conf["strict"], "keep": keep,
                                  "templates": len(ok_temps), "groups": acts,
                                  "core_missing": True}}
    if not ok_temps:
        notes.append("没有任何可用样本模板，无法匹配")
        return {**empty, "stats": {"strict": conf["strict"], "keep": keep,
                                  "templates": 0, "groups": acts}}

    # ---- 模板前缀池：按 m 归组，同一 m 的候选窗口只重采样一次 ----
    pfx_by_m: Dict[int, List[Dict]] = {}
    for ti, t in enumerate(ok_temps):
        for p in _template_prefixes(t, FRACTIONS, RESAMPLE_POINTS, conf["min_bars"]):
            pfx_by_m.setdefault(p["m"], []).append(dict(p, ti=ti, group=t["group"]))
    m_list = sorted(pfx_by_m.keys())
    if not m_list:
        notes.append("样本窗口过短，未生成可用进度档位")
        return {**empty, "stats": {"strict": conf["strict"], "keep": keep,
                                  "templates": len(ok_temps), "groups": acts}}

    # ---- 候选池 ----
    meta = pd.read_sql_query(
        "SELECT code, name, sector, exchange, listing_date FROM meta", db.reader())
    meta["code"] = meta["code"].astype(str)
    if exchange in ("SZ", "SH", "BJ"):
        meta = meta[meta["exchange"] == exchange]
    if sectors:
        meta = meta[meta["sector"].isin(set(sectors))]
    daily_map = batchload.load_daily_map(window_days=WINDOW_DAYS)
    bad_map = _hard_filter(meta, daily_map, conf)
    meta = meta[~meta["code"].isin(set(bad_map.keys()))]
    codes = meta["code"].tolist()
    n_all = len(codes)
    if n_all == 0:
        notes.append("硬过滤后无可比股票")
        return {**empty, "stats": {"strict": conf["strict"], "keep": keep,
                                  "templates": len(ok_temps), "candidates": 0,
                                  "groups": acts}}

    wmax = min(MAX_BARS, max(max(m_list), max(int(t["bars"]) for t in ok_temps)))
    cmat = np.full((n_all, wmax), np.nan)
    vmat = np.full((n_all, wmax), np.nan)
    avail = np.zeros(n_all, dtype=int)
    for i, code in enumerate(codes):
        df = daily_map.get(code)
        if df is None or df.empty:
            continue
        cl = df["close"].to_numpy(dtype=float)[-wmax:]
        vo = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0).to_numpy(dtype=float)[-wmax:]
        k = len(cl)
        cmat[i, wmax - k:] = cl
        vmat[i, wmax - k:] = vo
        avail[i] = k

    # ---- 逐 m 批量化打分：sim_mat[i, t] = 候选 i 对模板 t 的最佳相似度 ----
    # ★ has_mat 是必须的：sim_mat 初值为 0，而「相似度恰好为 0」是**合法取值**
    #   （候选与模板高度反向：cr≤0 → 0、幅度反向 → 0、cv≤0 → 0 → raw=0）。
    #   若只用 sim_mat > 0 判「是否评分过」，就会把这类候选与「bar 不够、根本没评」
    #   混为一谈（重构时曾丢过这个布尔标记）。has_mat 把两种语义彻底分开。
    pts = RESAMPLE_POINTS
    w_sh, w_am, w_vo = conf["w_shape"], conf["w_amp"], conf["w_vol"]
    n_t = len(ok_temps)
    sim_mat = np.zeros((n_all, n_t))
    f_mat = np.zeros((n_all, n_t))
    m_mat = np.zeros((n_all, n_t), dtype=int)   # 记录取到最佳的**窗口根数**，供明细直接读用
    has_mat = np.zeros((n_all, n_t), dtype=bool)
    total = len(m_list)
    for step, m in enumerate(m_list, 1):
        if progress_cb:
            progress_cb(step, total, f"样本匹配 {step}/{total}（窗口 {m} 根）")
        idx = np.where(avail >= m)[0]
        if idx.size == 0:
            continue
        win = cmat[idx][:, -m:]
        gok = np.all(np.isfinite(win), axis=1) & np.all(win > 0, axis=1)
        idx = idx[gok]
        if idx.size == 0:
            continue
        win = cmat[idx][:, -m:]
        z_px = _z_rows(_resample_rows(win, pts), use_log=True)
        z_vo = _z_rows(_resample_rows(np.maximum(vmat[idx][:, -m:], 0.0), pts), use_log=True)
        rc = win[:, -1] / win[:, 0] - 1.0
        pen = length_penalty(m, conf["ref_len"], conf["len_penalty"])
        for p in pfx_by_m[m]:
            ti = p["ti"]
            cr = (z_px @ p["vec_px"]) / pts
            cv = (z_vo @ p["vec_vol"]) / pts
            rt = float(p["ret"])
            denom = np.maximum(np.maximum(np.abs(rc), abs(rt)), 1e-9)
            amp = np.where(np.sign(rc) == np.sign(rt),
                           np.minimum(np.abs(rc), abs(rt)) / denom, 0.0)
            raw = 100.0 * (w_sh * np.clip(cr, 0.0, 1.0) + w_am * amp
                           + w_vo * np.clip(cv, 0.0, 1.0))
            sc = np.clip(raw - pen, 0.0, 100.0)
            cur = sim_mat[idx, ti]
            better = sc > cur
            if better.any():
                gi = idx[better]
                sim_mat[gi, ti] = sc[better]
                f_mat[gi, ti] = p["f"]
                m_mat[gi, ti] = m
                has_mat[gi, ti] = True

    # ---- 组内最佳 → 综合相似度 ----
    gcols = {g: np.array([i for i, t in enumerate(ok_temps) if t["group"] == g], dtype=int)
             for g in acts}
    best = {g: (sim_mat[:, c].max(axis=1) if c.size else np.zeros(n_all)) for g, c in gcols.items()}
    ghas = {g: (has_mat[:, c].any(axis=1) if c.size else np.zeros(n_all, dtype=bool))
            for g, c in gcols.items()}
    core_cols = gcols[GROUP_CORE]
    core_best = sim_mat[:, core_cols].max(axis=1)
    core_ti = core_cols[np.argmax(sim_mat[:, core_cols], axis=1)]
    core_f = f_mat[np.arange(n_all), core_ti]
    scored = ghas[GROUP_CORE]                     # 真评过分（含「相似度恰为 0」）
    # 阶段：未评分的用哨兵 "—"，避免被误读成「周期刚刚启动」
    stage_arr = np.full(n_all, "—", dtype=object)
    stage_arr[scored] = np.where(
        core_f[scored] <= float(conf["start_frac_max"]) + 1e-9, STAGE_START, STAGE_ONGOING)
    # 组级缺失（候选 bar 不足、与该组任何模板都比不了）→ 综合分里按 0 计入，不重分配权重
    miss_group = {g: int((~ghas[g]).sum()) for g in acts}

    wsum = float(sum(conf["group_weights"][g] for g in acts)) or 1.0
    combo = np.zeros(n_all)
    for g in acts:
        combo += conf["group_weights"][g] * best[g]
    combo = combo / wsum

    # ---- 分阶段分位数门槛 ----
    # q_c / q_k 是**分位数原始门槛**（判定用，内部可追溯）；
    # thr_c / thr_k 是对外上报的门槛 = 该阶段**实际入选集合的最小值**。
    # 两者等价，但上报最小值能避免「分位数插值出 80.46、而最低入选行显示 80.45」
    # 这种展示自相矛盾（判定一直用 raw 值，从未错，但展示口径要经得起肉眼复核）。
    q_c, q_k, thr_c, thr_k = {}, {}, {}, {}
    for s in STAGE_ALL:
        msk = scored & (stage_arr == s)
        if int(msk.sum()) < 1:
            q_c[s] = q_k[s] = thr_c[s] = thr_k[s] = None
            continue
        q_c[s] = float(np.quantile(combo[msk], 1.0 - keep))
        q_k[s] = float(np.quantile(core_best[msk], 1.0 - keep))
        acc = msk & (combo >= q_c[s]) & (core_best >= q_k[s])
        thr_c[s] = float(combo[acc].min()) if acc.any() else None
        thr_k[s] = float(core_best[acc].min()) if acc.any() else None
    pass_pool = np.zeros(n_all, dtype=bool)
    for s in STAGE_ALL:
        if q_c[s] is None:
            continue
        msk = scored & (stage_arr == s)
        pass_pool |= msk & (combo >= q_c[s]) & (core_best >= q_k[s])
    # ---- 背书：单模板相似度进入全体「候选×模板」相似度的前列 ----
    # 只统计**确实评过分**的候选×模板对。退化（无有效对）时置 0 而不是取 0 分位 ——
    # 否则 back_thr=0 会让所有候选都被记成满 n_t 个背书，完全失真。
    sub_sim = sim_mat[scored][:, np.arange(n_t)]
    pair = sub_sim[has_mat[scored]]
    back_thr = float(np.quantile(pair, float(conf["back_q"]))) if pair.size else 0.0
    back = ((sim_mat >= back_thr) & has_mat).sum(axis=1) if back_thr > 0 \
        else np.zeros(n_all, dtype=int)

    hit = np.where(pass_pool)[0]
    name_map = dict(zip(meta["code"], meta["name"]))
    sector_map = dict(zip(meta["code"], meta["sector"]))
    rows = []
    for i in hit:
        code = codes[i]
        ct = ok_temps[int(core_ti[i])]
        infl = None
        for g in acts:
            v = conf["group_weights"][g] * float(best[g][i])
            if infl is None or v > infl[0]:
                infl = (v, g)
        g_ti = int(np.argmax(sim_mat[i, gcols[infl[1]]])) if infl else 0
        it = ok_temps[int(gcols[infl[1]][g_ti])] if infl else ct
        # 核心维度的明细：窗口根数 m 直接取打分循环记录的值（**不再二次推导**
        # round(bars×f)，避免推导式与循环不同步时静默偏一位）
        f_core = float(core_f[i])
        m_core = int(m_mat[i, core_ti[i]])
        cf = ct["close"]
        cr_v = _build_vector(cf[:m_core], RESAMPLE_POINTS, True)
        cv_v = _build_vector(ct["vol"][:m_core], RESAMPLE_POINTS, True)
        win = cmat[i, -m_core:]
        zc = _build_vector(win, RESAMPLE_POINTS, True)
        dfv = daily_map.get(code)
        rows.append({
            "代码": code,
            "名称": name_map.get(code, code),
            "板块": sector_map.get(code, "未分类"),
            "周期阶段": str(stage_arr[i]),
            "综合相似度": round(float(combo[i]), 2),
            "核心相似度": round(float(core_best[i]), 2),
            "核心进度%": int(round(f_core * 100)),
            "覆盖天数": m_core,
            # 组不激活 / 该股票无法与该组任何模板比较 → None（=「不可比」），
            # 不能写成 0.0 —— 那会被读成「完全不相似」，是两回事
            "多元相似度": (round(float(best[GROUP_DIVERSE][i]), 2)
                           if (GROUP_DIVERSE in ghas and bool(ghas[GROUP_DIVERSE][i]))
                           else None),
            "多元样本": _gsample(ok_temps, sim_mat, has_mat, gcols, GROUP_DIVERSE, i),
            "同期相似度": (round(float(best[GROUP_LEGACY][i]), 2)
                           if (GROUP_LEGACY in ghas and bool(ghas[GROUP_LEGACY][i]))
                           else None),
            "同期样本": _gsample(ok_temps, sim_mat, has_mat, gcols, GROUP_LEGACY, i),
            "背书样本数": int(back[i]),
            "样本涨幅%": round(float(cf[m_core - 1] / cf[0] - 1.0) * 100.0, 2),
            "自身涨幅%": round(float(win[-1] / win[0] - 1.0) * 100.0, 2),
            "价形相关": round(float(np.mean(zc * cr_v)), 4),
            "量形相关": round(float(np.mean(
                _build_vector(np.maximum(vmat[i, -m_core:], 0.0), RESAMPLE_POINTS, True)
                * cv_v)), 4),
            "匹配样本": f"{ct['code']}.{ct['exchange']} {ct['start']}~{ct['end']}",
            "收盘": round(float(cmat[i, -1]), 3),
            "末端日期": str(pd.Timestamp(dfv["date"].iloc[-1]).date())
                       if dfv is not None and len(dfv) else "",
        })
    rows.sort(key=lambda r: (-r["综合相似度"], -r["核心相似度"], r["代码"]))
    rows = rows[:int(conf.get("max_results", 300) or 300)]

    n_start = sum(1 for r in rows if r["周期阶段"] == STAGE_START)
    pool_cnt = {s: int((scored & (stage_arr == s)).sum()) for s in STAGE_ALL}
    n_passed = int(pass_pool.sum())
    stats = {
        "strict": conf["strict"],
        "keep": keep,
        "thr_combo": {s: (None if thr_c[s] is None else round(thr_c[s], 2)) for s in STAGE_ALL},
        "thr_core": {s: (None if thr_k[s] is None else round(thr_k[s], 2)) for s in STAGE_ALL},
        # 分位数原始门槛（判定用）；上报给前端的是「实际入选最小值」，此处保留以便追溯
        "thr_quantile": {s: (None if q_c[s] is None else round(q_c[s], 2)) for s in STAGE_ALL},
        "thr_quantile_core": {s: (None if q_k[s] is None else round(q_k[s], 2)) for s in STAGE_ALL},
        "groups": acts,
        "group_weights": {g: conf["group_weights"][g] for g in acts},
        "templates": len(ok_temps),
        "templates_total": len(all_temps),
        "templates_unavailable": len(bad_temps),
        "candidates": n_all,
        "filtered": len(bad_map),
        "scored": int(scored.sum()),
        "unscored": int((~scored).sum()),
        "missing_group": miss_group,
        "pool": pool_cnt,
        "passed": n_passed,
        "truncated": bool(n_passed > len(rows)),
        "hits": len(rows),
        "start_hits": n_start,
        "ongoing_hits": len(rows) - n_start,
        "back_thr": round(back_thr, 2),
        "m_windows": m_list,
        "window_days": WINDOW_DAYS,
        "core_only": core_only(conf["strict"]),
    }
    notes.append(
        f"严格程度 {conf['strict']:.0f}% → 每阶段保留前 {keep*100:.3g}%（候选池 "
        f"刚启动 {pool_cnt[STAGE_START]} / 进行中 {pool_cnt[STAGE_ONGOING]} 只）；"
        f"门槛 综合≥[{_fmt_thr(thr_c)}] · 核心≥[{_fmt_thr(thr_k)}]")
    notes.append(
        f"激活组 {acts}（权重 {stats['group_weights']}）· 可用模板 {len(ok_temps)}"
        f"（不可用 {len(bad_temps)}）· 候选 {n_all} 只（硬过滤剔除 {len(bad_map)}）"
        f"· 命中 {len(rows)} 只（刚启动 {n_start} / 进行中 {len(rows)-n_start}）")
    if stats["truncated"]:
        notes.append(f"⚠ 达标 {n_passed} 只 > 展示上限 {len(rows)} 只，表格/CSV 只含前 "
                     f"{len(rows)} 只；把严格程度往右调可看到更完整的名单。")
    if any(v > 0 for v in miss_group.values()):
        notes.append("组级缺失（该股票可用 bar 不足、无法与某组任何模板比较，"
                     "综合分按 0 计入且不重分配权重）：" +
                     "、".join(f"{g} 缺 {v} 只" for g, v in miss_group.items() if v) +
                     f"；完全未评上核心模板 {stats['unscored']} 只已排除。")
    notes.append("本模块按需求**不做**样本外/过拟合检验（「允许过拟合」）；"
                 "样本量小、生存者偏差明显，排名不代表未来收益。")
    # ⚠ 方法论披露：同期组是一波行情，且采集期与候选评估期相邻 → 时间同质偏差。
    #   这是「为什么必须加多元组」的原因，必须让使用者看到（不是 bug）。
    if GROUP_LEGACY in acts:
        _ends = [pd.Timestamp(df["date"].iloc[-1]).date() for df in daily_map.values()
                 if df is not None and len(df)]
        # 样本期区间要用「最早起始 ~ 最晚结束」，不能只取结束日区间（会把
        # 2026-05-28~2026-08-13 的样本期误报成 2026-07-10~2026-08-13）
        _leg_s = [t["start"] for t in ok_temps if t["group"] == GROUP_LEGACY]
        _leg_e = [t["end"] for t in ok_temps if t["group"] == GROUP_LEGACY]
        if _ends and _leg_s:
            _cand_end = max(_ends)
            _gap = (_cand_end - date.fromisoformat(max(_leg_e))).days
            notes.append(
                f"⚠ 同期组 {len(_leg_s)} 个样本全部取自 {min(_leg_s)}~{max(_leg_e)} 的"
                f"**同一波行情**，其采集期距候选评估末端（{_cand_end}）仅 {_gap} 天 → "
                f"存在时间同质偏差（已把权重削弱到 {conf['group_weights'][GROUP_LEGACY]}）；"
                f"这正是要加入跨时期「多元组」的原因。")
    # ⚠ 严禁进入 notes：notes 会被前端摘要栏直接渲染，而 >180% 的对标样本硬规则
    #   按需求**不写入前端显示**。故只放在 internal_notes 里（接口可追溯、前端不渲染）。
    internal = []
    internal.append(f"分位数门槛（判定用）：综合≥{_fmt_thr(q_c)} · 核心≥{_fmt_thr(q_k)}；"
                    f"对外上报门槛＝该阶段实际入选集合最小值 综合≥{_fmt_thr(thr_c)} · "
                    f"核心≥{_fmt_thr(thr_k)}")
    if core_only(conf["strict"]):
        internal.append(f"严格程度 > {STRICT_CORE_ONLY:.0f}%：已切换为仅核心样本模式"
                        f"（对标样本固定为 {CORE_SAMPLE['code']}."
                        f"{CORE_SAMPLE['exchange']} "
                        f"{CORE_SAMPLE['start']}~{CORE_SAMPLE['end']}，后端硬规则）；"
                        f"因双门槛退化为单门槛会更宽松，保留比例额外乘 "
                        f"{conf['core_only_keep_factor']}，故界面上的「保留比例」在 "
                        f"180%→181% 处会出现一次跳降。")
    if progress_cb:
        progress_cb(total, total, "样本形态匹配完成")
    return {"rows": rows, "stats": stats, "templates": _temp_brief(all_temps),
            "notes": notes, "internal_notes": internal}


def _fmt_thr(thr: Dict) -> str:
    """门槛文案：`档名 值`，与 stats 同精度（2 位小数）。"""
    return " · ".join(f"{s} {'—' if thr[s] is None else format(thr[s], '.2f')}"
                      for s in STAGE_ALL)


def _gsample(ok_temps: List[Dict], sim_mat: np.ndarray, has_mat: np.ndarray,
             gcols: Dict, g: str, i: int) -> str:
    """该组内为该候选贡献最佳相似度的样本（未评过分则返回空串）。

    gcols[g] 已按组过滤，故无需再校验模板所属组（原实现那句是死判断）；
    f_mat 也从未被用到，已从签名移除。
    """
    cols = gcols.get(g)
    if cols is None or cols.size == 0 or not has_mat[i, cols].any():
        return ""
    t = ok_temps[int(cols[int(np.argmax(sim_mat[i, cols]))])]
    return f"{t['code']}.{t['exchange']} {t['start']}~{t['end']}"


def _temp_brief(temps: List[Dict]) -> List[Dict]:
    return [{"组": t.get("group", ""), "代码": t["code"],
             "交易所": t.get("exchange", ""),
             "区间": f"{t['start']}~{t['end']}",
             "bar数": t.get("bars", 0),
             "涨幅%": round(float(t.get("ret") or 0) * 100, 2),
             "可用": bool(t.get("ok")), "原因": t.get("reason", "")}
            for t in temps]
