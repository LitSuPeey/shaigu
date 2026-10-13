# -*- coding: utf-8 -*-
"""前瞻预警模块 —— 以「提前量」为核心的 A 股预警系统（源自《新功能.txt》方案）。

四重领先机制：
  ① 日历预热     月度主线 + 固定事件日历 + 埋伏窗口（事件前 N 天开始提示）
  ② 产业链传导   上游涨价 → 中游提价 → 业绩兑现存在时滞，监测上游提前布局中游
  ③ 资金领先价格 5日涨幅 + 主力净流入 + 换手未放量 → 疑似资金提前埋伏
  ④ 拥挤度预警   换手分位 / 量能分位 / 20日涨幅 / 资金流出，多项触发 → 高点逃离
  ⑤ 特殊概念     名字玄学：日期彩头（9·18「就要发」×中华「华」字辈）、生肖谐音
                 （当前年＋即将到来的生肖年，如羊＝羊/洋/扬/阳）、动物字辈、代码吉利等

模块清单（对应方案 engine/ 六件套）：
  get_calendar_alerts()  规律引擎（日历预热 + 埋伏窗口）
  _special_scan()        特殊概念（名字玄学 / 谐音梗 / 生肖字辈，规则表可热更新）
  scan()                 主调度：采集 → 分析 → 汇总（供 /api/alert/run）
  build_report_html()    独立网页报告（深色模板，涨红跌绿）

所有阈值均可通过 cfg 覆盖（前端「⚙ 预警参数」窗口），默认值 = 方案原版设计值。
数据源：akshare 东财接口（联网）；akshare 采用函数内懒加载，避免拖慢服务启动
（与 ant.py 筹码模块同一约定：绝不放模块顶层 / 子进程）。
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import os as _os
import time as _time
from typing import Callable, Optional

import pandas as pd

ProgressCb = Optional[Callable[[float, float, str], None]]

# ============ 网络韧性层：系统代理抖动时自动改直连重试 ============
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
               "ALL_PROXY", "all_proxy")
_PREFER_DIRECT = {"v": False}   # 进程内记忆上次成功的连接模式
_UA_PATCHED = {"v": False}
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _ensure_ua() -> None:
    """东财 WAF 会直接掐断 python-requests 默认 UA 的连接（RemoteDisconnected），
    统一为本进程的所有 requests 请求注入浏览器 UA（仅当调用方未显式指定时）。"""
    if _UA_PATCHED["v"]:
        return
    try:
        import requests as _rq
        _orig = _rq.Session.request

        def _patched(self, method, url, **kw):
            try:
                headers = dict(kw.get("headers") or {})
                if not any(k.lower() == "user-agent" for k in headers):
                    headers["User-Agent"] = _UA
                    kw["headers"] = headers
            except Exception:  # noqa: BLE001 —— 注入失败不改变原行为
                pass
            return _orig(self, method, url, **kw)

        _rq.Session.request = _patched
        _UA_PATCHED["v"] = True
    except Exception:  # noqa: BLE001
        pass


def _net_call(fn, retries: int = 3, pause: float = 0.8):
    """带重试的网络调用。首次按上次成功模式（代理/直连），失败后切换模式重试。
    某些本地代理（如 127.0.0.1:xxxx）会对东财接口断连，直连即可恢复。"""
    last = None
    saved = {k: _os.environ.get(k) for k in _PROXY_KEYS}
    prefer_direct = _PREFER_DIRECT["v"]
    plans = [prefer_direct, not prefer_direct, prefer_direct][:max(int(retries), 1)]

    def _set_env(direct: bool) -> None:
        if direct:
            for k in _PROXY_KEYS:
                _os.environ.pop(k, None)
        else:
            for k, v in saved.items():
                if v is not None:
                    _os.environ[k] = v

    try:
        for i, direct in enumerate(plans):
            _set_env(direct)
            try:
                out = fn()
                if direct != prefer_direct:
                    _PREFER_DIRECT["v"] = direct
                return out
            except Exception as e:  # noqa: BLE001 —— 记录并切换模式重试
                last = e
                if i < len(plans) - 1:
                    _time.sleep(pause)
        raise last
    finally:
        _set_env(False)  # 还原环境变量，避免污染进程内其它库
        for k, v in saved.items():
            if v is not None:
                _os.environ[k] = v


# ============ 可调参数默认值（前端「⚙ 预警参数」窗口可覆盖） ============
DEFAULTS: dict = {
    "calendar": {"horizon_days": 35},          # 日历前瞻天数
    "stealth": {
        "gain_min": 6.0,                       # 异动埋伏：5日板块涨幅下限(%)
        "inflow_min": 3.0,                     # 异动埋伏：5日主力净流入下限(亿)
        "turnover_pct_max": 60.0,              # 异动埋伏：换手分位上限(%)——尚未放量=热度未起
        "hot_gain_min": 20.0,                  # 出货嫌疑：5日板块涨幅下限(%)
        "hot_turnover_pct_min": 80.0,          # 出货嫌疑：换手分位下限(%)
    },
    "cold": {
        "window": 60,                          # 冷门观察窗口(交易日)
        "top_n": 3,                            # 冷门板块 Top N
        "w_amount": 0.35,                      # 冷度权重：成交额占比
        "w_turnover": 0.35,                    # 冷度权重：换手率
        "w_gain": 0.30,                        # 冷度权重：窗口涨幅（替代方案的新闻热度，见说明）
    },
    "national": {"days": 5, "groups": None},   # 国家队：groups 为 None 时四列表全开（etf/huijin/ssf/zhengjin）
    "crowding": {
        "turnover_pct": 0.90,                  # 拥挤：换手率分位阈值（0-1）
        "amount_pct": 0.90,                    # 拥挤：成交额分位阈值（0-1）
        "gain20_min": 15.0,                    # 拥挤：20日涨幅下限(%)（动量代理）
        "trigger_n": 2,                        # 拥挤触发项数（方案原版为3；因融资/估值分位
        #                                        无板块级数据源，改用涨幅+资金流出代理后
        #                                        放宽为 2，可在参数窗口调回 3）
    },
    "foreign": {"top_n": 10},                  # 外资重仓 Top N
    "special": {
        "window": 3,                           # 日期彩头 ±窗口(天)
        "zodiac_lead_days": 150,               # 下一生肖提前多少天进入监控
        "min_hits": 5,                         # 概念至少命中几只才展示
        "top_n": 15,                           # 每个概念明细展示只数（按涨幅降序）
        "excess_min": 1.5,                     # 常驻字辈触发①：命中组均涨 − 全市场均涨(百分点)
        "surge_pct": 5.0,                      # 记作「大涨」的涨幅阈值(%)
        "surge_n_min": 3,                      # 常驻字辈触发①：大涨只数下限
        "limitup_min": 2,                      # 常驻字辈触发②：涨停只数下限
        "limitup_ratio_min": 2.0,              # 常驻字辈触发②：涨停密度 ÷ 全市场涨停密度
        "density_breadth_floor": 0.0,          # 触发②额外要求：组内上涨家数占比 − 全市场上涨
        #                                        家数占比（百分点）不得低于此值。0 = 跟涨家数
        #                                        至少不能低于全市场（即「确有批量跟涨」）。
        #                                        改用宽度而非「均涨跑输」是因为后者在 0 附近
        #                                        有刀锋效应（−0.16% 与 +0.01% 结论相反）。
        "standing_top_n": 3,                   # 未触发的常驻字辈按超额取前 N 行作「观察」
    },
}

SECTIONS = ["calendar", "stealth", "cold", "national", "foreign", "crowding", "chains",
            "macro", "special"]
SECTION_LABELS = {
    "calendar": "日历预警", "stealth": "异动侦测", "cold": "冷门板块",
    "national": "国家队资金", "foreign": "外资重仓", "crowding": "拥挤度监控",
    # 【2026-10-12 修复】原实现只有 "chains"（标签「传导链与宏观」）一个开关同时控制
    # 产业链传导 + 宏观雷达 —— 前端点卡片标题「选入/踢出」时两者永远联动。
    # 现拆分为两个独立 section：chains（产业链传导）、macro（宏观雷达）。
    "chains": "产业链传导", "macro": "宏观雷达", "special": "特殊概念",
}


def _merge_cfg(cfg: Optional[dict]) -> dict:
    base = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for g, kv in (cfg or {}).items():
        if isinstance(kv, dict) and isinstance(base.get(g), dict):
            base[g].update({k: v for k, v in kv.items() if v is not None})
    return base


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _r(x, nd=1):
    v = _f(x)
    return None if v is None else round(v, nd)


def _ak():
    """akshare 懒加载（绝不放模块顶层，避免拖慢启动 / 子进程崩溃）。"""
    import akshare as ak
    return ak


# ==================================================================
# 一、规律引擎（编码自《规律.txt》框架，表格均为数据结构可热更新）
# ==================================================================
MONTHLY_THEMES = {
    1: ["农业政策", "年报预告", "春节前消费"],
    2: ["春节消费数据", "两会预热"],
    3: ["两会政策", "一季报预期"],
    4: ["年报/一季报密集披露", "政治局会议"],
    5: ["五一消费", "基建开工"],
    6: ["半年末流动性", "中报预告"],
    7: ["年中政策定调", "中报行情"],
    8: ["中报密集", "科技新品周期"],
    9: ["军工", "国庆消费预热", "三季报前瞻"],
    10: ["国庆消费数据", "三季报"],
    11: ["双十一", "年报高送转预期"],
    12: ["中央经济工作会议", "供暖", "跨年行情"],
}

# 埋伏窗口表：主题 → (提前起始天数, 提前结束天数)。核心反马后炮机制。
LEAD_WINDOWS = {
    "军工": (30, 10),            # 国庆催化前 30 天进入埋伏窗口
    "国庆消费预热": (25, 8),
    "三季报前瞻": (35, 15),      # 9 月初即提示预增方向埋伏
    "双十一": (40, 15),          # 10 月初提示物流/电商/美妆
    "中央经济工作会议": (25, 5),
    "两会预热": (35, 10),
}

# 主题 → 关联事件关键词（修复方案原稿的主题匹配歧义）
THEME_EVENT_HINT = {
    "军工": "国庆", "国庆消费预热": "国庆", "三季报前瞻": "三季报",
    "双十一": "双十一", "中央经济工作会议": "中央经济工作会议", "两会预热": "两会",
}

# 年度固定事件日历（自动计算距今天数）
FIXED_EVENTS = [
    (3, 5, "全国两会", "全年政策主线定调，两会预热 35 天前启动"),
    (9, 17, "美联储9月议息会议", "关注降息节奏与点阵图，影响美债收益率与外资流向"),
    (9, 18, "日本央行9月议息", "美日货币政策分化，警惕套息平仓引发全球 risk-off"),
    (9, 30, "国庆长假前最后埋伏窗口", "军工/消费/出行应在 9 月中下旬完成布局"),
    (10, 1, "国庆黄金周", "节前埋伏、节后兑现数据"),
    (10, 31, "三季报披露截止", "预增公告通常提前 2-4 周发布"),
    (11, 11, "双十一", "电商/物流/美妆景气催化"),
    (12, 10, "中央经济工作会议（惯例）", "供暖/顺周期/次年主线定调"),
]

# 宏观雷达主题表：对近端快讯做主题归类扫描（有回波才显示，无静态知识卡）
_MACRO_TOPICS = [
    {"主题": "美联储与利率", "关键词": ["美联储", "FOMC", "联邦基金", "点阵图", "鲍威尔", "议息",
                                    "非农", "就业数据", "失业率", "通胀", "CPI", "PCE", "加息", "降息"],
     "关注": "利率路径决定全球流动性与成长股估值"},
    {"主题": "美债与美元", "关键词": ["美债", "十年期美债", "美债收益率", "美元指数", "美元"],
     "关注": "贴现率与外资风险偏好"},
    {"主题": "人民币汇率", "关键词": ["人民币", "离岸人民币", "汇率", "中间价"],
     "关注": "外资流入流出的汇兑视角"},
    {"主题": "日本央行与套息", "关键词": ["日本央行", "日元", "日央行", "套息"],
     "关注": "套息平仓是全球 risk-off 的引信"},
    {"主题": "黄金与大宗", "关键词": ["黄金", "金价", "COMEX", "白银", "原油", "铜价", "大宗商品"],
     "关注": "避险情绪与通胀预期的实物映射"},
]

# 宏观信号词（方向敏感：区分「预期升温」与「预期降温/排除」，避免把历史报道误读为当前预期）
_MACRO_SIGNAL_PATTERNS = [
    ("降息预期升温", "偏宽松", [r"降息预期[^。]{0,6}(升温|增强|强化|加大|扩大)",
                              r"(押注|定价|计价)[^。]{0,8}降息",
                              r"(加大|增强)降息(幅度|押注|预期)", r"降息\s*\d+\s*个基点"]),
    ("降息预期降温", "偏紧缩", [r"降息预期[^。]{0,6}(降温|减弱|回落|降低)",
                              r"排除[^。]{0,6}降息", r"(推迟|暂停|放弃)降息",
                              r"不(再|会)降息", r"降息[^。]{0,6}(落空|渺茫|无望|存疑)"]),
    ("紧缩信号", "偏紧缩", [r"(通胀|非农|就业)[^。]{0,10}(超预期|强劲|反弹|火热)",
                           r"(重启|恢复)加息", r"(维持|更高)[^。]{0,6}利率[^。]{0,6}(更久|高位)"]),
    ("宽松信号", "偏宽松", [r"(经济|就业|非农)[^。]{0,10}(疲软|走弱|不及预期|恶化|难看)",
                           r"(衰退|降息周期|宽松周期)"]),
]
_EASE_PAT = [p for _, d, ps in _MACRO_SIGNAL_PATTERNS if d == "偏宽松" for p in ps]
_HAWK_PAT = [p for _, d, ps in _MACRO_SIGNAL_PATTERNS if d == "偏紧缩" for p in ps]


def _macro_radar() -> tuple[str, list]:
    """宏观雷达：对近端快讯做主题归类扫描 + 方向信号识别（纯新闻驱动，无静态知识卡）。
    每个有回波的主题输出一行动态总结（相关报道 + 方向小结）；无回波不显示。"""
    import re as _re2
    try:
        from .ticker import _news_items
        news = _news_items()[:120]
    except Exception as e:  # noqa: BLE001 —— 新闻源挂掉时明确说明而非静默
        return f"新闻源不可用，宏观雷达扫描跳过（{str(e)[:40]}）", []

    docs = [f"{it.get('title', '')} {it.get('text', '')}" for it in news]
    stamps = [str(it.get('time') or '')[:10] for it in news]
    titles = [str(it.get('title') or '')[:44] for it in news]

    rows: list = []
    dove = hawk = 0
    # —— 全局方向信号（跨主题，方向敏感）——
    for label, direction, patterns in _MACRO_SIGNAL_PATTERNS:
        ev = [f"「{titles[i]}」（{stamps[i]}）" for i, d in enumerate(docs)
              if any(_re2.search(p, d) for p in patterns)]
        if not ev:
            continue
        if direction == "偏宽松":
            dove += len(ev)
        else:
            hawk += len(ev)
        rows.append({"类型": "📡 方向信号", "触发": label, "细节": " ｜ ".join(ev[:3])
                     + (f" 等 {len(ev)} 条" if len(ev) > 3 else ""),
                     "方向": direction, "应对": "以美联储官网/点阵图/发布会实录为准"})

    # —— 主题扫描（有回波才显示）——
    for topic in _MACRO_TOPICS:
        idx = [i for i, d in enumerate(docs)
               if any(kw in d for kw in topic["关键词"])]
        if not idx:
            continue
        e_cnt = sum(1 for i in idx if any(_re2.search(p, docs[i]) for p in _EASE_PAT))
        h_cnt = sum(1 for i in idx if any(_re2.search(p, docs[i]) for p in _HAWK_PAT))
        if e_cnt > h_cnt:
            direction, summary = "偏宽松", f"近端报道偏宽松叙事（{e_cnt}/{len(idx)} 条含宽松信号词）"
        elif h_cnt > e_cnt:
            direction, summary = "偏紧缩", f"近端报道偏紧缩叙事（{h_cnt}/{len(idx)} 条含紧缩信号词）"
        else:
            direction, summary = "中性", f"近端报道 {len(idx)} 条，未见明确方向词"
        ev = " ｜ ".join(f"「{titles[i]}」（{stamps[i]}）" for i in idx[:3])
        rows.append({"类型": "🛰 主题扫描", "触发": topic["主题"],
                     "细节": f"{summary}：{ev}" + (f" 等 {len(idx)} 条" if len(idx) > 3 else ""),
                     "方向": direction, "应对": f"关注点：{topic['关注']}"})

    if not rows:
        return "近端快讯未见宏观主题相关报道——雷达无回波，不给方向结论", []
    if dove == 0 and hawk == 0:
        tail = "方向信号：未检出明确的宽松/紧缩表述"
    elif dove > hawk:
        tail = f"方向信号：偏宽松 {dove} 条 vs 偏紧缩 {hawk} 条（新闻词频粗筛，非利率预测）"
    elif hawk > dove:
        tail = f"方向信号：偏紧缩 {hawk} 条 vs 偏宽松 {dove} 条（新闻词频粗筛，非利率预测）"
    else:
        tail = f"方向信号：宽松与紧缩并存（{dove} vs {hawk}），分歧大——建议人工核对美联储官网与点阵图"
    return tail, rows

# 产业链传导链（上游涨价 → 中游滞后受益；上游板块/中游板块 = 东财行业名，用于实时进展）
# 实战验证案例：2026年钨精矿涨359% → 8月1日京瓷刀具全线提价25% → 中游硬质合金业绩兑现
TRANSMIT_CHAINS = [
    {"上游": "钨精矿", "监测指标": "65%WO均价同比", "上游板块": ["小金属"],
     "中游": ["硬质合金刀具", "PCB钻针"], "中游板块": ["通用设备", "电子元件"],
     "逻辑": "上游资源品暴涨 → 日韩刀具大厂被迫提价 → 中游量价齐升"},
    {"上游": "氧化镨钕", "监测指标": "轻稀土价格同比", "上游板块": ["能源金属"],
     "中游": ["钕铁硼永磁"], "下游": ["人形机器人", "新能源车"],
     "中游板块": ["汽车零部件", "电池"],
     "逻辑": "稀土供给配额收紧+缅甸雨季 → 永磁材料传导"},
    {"上游": "碳酸锂", "监测指标": "现货均价", "上游板块": ["能源金属"],
     "中游": ["电池材料", "储能"], "中游板块": ["电池"],
     "逻辑": "旺季补库 → 中游排产回升"},
    {"上游": "电力/算力", "监测指标": "AI算力订单", "上游板块": ["电力"],
     "中游": ["电网设备", "液冷", "光模块"], "中游板块": ["电网设备", "通信设备", "半导体"],
     "逻辑": "算力扩张的底层约束是电力 → 电力设备订单领先业绩 2-3 个季度"},
]


def get_calendar_alerts(cfg=None, today=None) -> dict:
    """日历预警：未来 N 天事件 + 本月主线 + 处于各主题埋伏窗口的提示。"""
    c = _merge_cfg({"calendar": cfg or {}})["calendar"]
    today = today or _dt.date.today()
    horizon = int(c.get("horizon_days", 35))
    events = []
    for m, d, name, note in FIXED_EVENTS:
        ed = _dt.date(today.year, m, d)
        if ed < today:
            ed = ed.replace(year=today.year + 1)
        delta = (ed - today).days
        events.append({"事件": name, "日期": ed.strftime("%m-%d"),
                       "距今(天)": delta, "备注": note})
    events.sort(key=lambda r: r["距今(天)"])
    alerts = [e for e in events if e["距今(天)"] <= horizon]
    # 埋伏窗口判断
    windows = []
    for theme, (start, end) in LEAD_WINDOWS.items():
        hint = THEME_EVENT_HINT.get(theme, "")
        for m, d, name, _ in FIXED_EVENTS:
            if hint and hint in name:
                ed = _dt.date(today.year, m, d)
                if ed < today:
                    ed = ed.replace(year=today.year + 1)
                delta = (ed - today).days
                if end <= delta <= start:
                    windows.append({"主题": theme, "关联事件": name,
                                    "事件日期": ed.strftime("%m-%d"),
                                    "距事件(天)": delta,
                                    "规律窗口": f"提前{start}~{end}天",
                                    "备注": "处于规律埋伏窗口内，建议分批布局"})
                break
    return {"本月主线": MONTHLY_THEMES.get(today.month, []),
            "日历预警": alerts, "埋伏窗口": windows}


# ==================================================================
# 二、数据采集（东财接口，全部带防御：缺列/缺数据则降级跳过）
# ==================================================================
def _industry_basic() -> pd.DataFrame:
    """行业板块今日行情：板块名称/涨跌幅/换手率/总市值。"""
    return _ak().stock_board_industry_name_em()


def _sector_flow() -> pd.DataFrame:
    """行业资金流 5 日排行：5日涨跌幅 / 5日主力净流入-净额。"""
    return _ak().stock_sector_fund_flow_rank(indicator="5日", sector_type="行业资金流")


def _sector_hist(name: str, days: int) -> Optional[pd.DataFrame]:
    """单板块日线历史（东财）：日期/收盘/成交量/成交额/换手率。"""
    end = _dt.date.today().strftime("%Y%m%d")
    start = (_dt.date.today() - _dt.timedelta(days=int(days) + 10)).strftime("%Y%m%d")
    df = _ak().stock_board_industry_hist_em(
        symbol=name, period="日k", start_date=start, end_date=end, adjust="")
    return df if df is not None and not df.empty else None


def _merge_basic_flow(basic: pd.DataFrame, flow: pd.DataFrame) -> pd.DataFrame:
    keep_b = [c for c in ("板块名称", "涨跌幅", "换手率") if c in basic.columns]
    keep_f = [c for c in ("名称", "5日涨跌幅", "5日主力净流入-净额") if c in flow.columns]
    b = basic[keep_b].copy()
    f = flow[keep_f].copy()
    df = b.merge(f, left_on="板块名称", right_on="名称", how="left")
    if "名称" in df.columns:
        df = df.drop(columns=["名称"])
    return df


def _pctile(hist: pd.Series, cur) -> Optional[float]:
    """cur 在 hist 中的分位（0-100）。样本 < 20 返回 None。"""
    s = pd.to_numeric(hist, errors="coerce").dropna()
    if len(s) < 20:
        return None
    cur = _f(cur)
    if cur is None:
        return None
    return round(float((s < cur).mean() * 100), 1)


def _flow5_of(merged: pd.DataFrame, name: str) -> Optional[float]:
    """板块 5 日主力净流入（亿元）。"""
    row = merged.loc[merged["板块名称"] == name]
    if row.empty:
        return None
    return _r(_f(row.iloc[0].get("5日主力净流入-净额")) / 1e8 if _f(row.iloc[0].get("5日主力净流入-净额")) is not None else None, 2)


# ==================================================================
# 三、六大功能模块
# ==================================================================
def _stealth_scan(merged: pd.DataFrame, c: dict, hist_get, notes: list):
    """异动板块侦测：疑似资金提前埋伏 / 高位借利好出货。"""
    stealth, selling = [], []
    if "5日涨跌幅" not in merged.columns or "5日主力净流入-净额" not in merged.columns:
        notes.append("资金流接口缺 5 日列，异动侦测已跳过")
        return stealth, selling
    sc = c["stealth"]
    for _, r in merged.iterrows():
        name = r.get("板块名称")
        g5 = _f(r.get("5日涨跌幅"))
        f5 = _f(r.get("5日主力净流入-净额"))
        if name is None or g5 is None or f5 is None:
            continue
        f5e = f5 / 1e8
        # ① 涨了 + 钱先动 + 尚未放量（价格涨了但热度未起 → 可疑的提前埋伏）
        if g5 > sc["gain_min"] and f5e > sc["inflow_min"]:
            hist = hist_get(name)
            t_pct = None
            if hist is not None and "换手率" in hist.columns and len(hist):
                t_pct = _pctile(hist["换手率"].iloc[:-1], hist["换手率"].iloc[-1])
            if t_pct is None or t_pct <= sc["turnover_pct_max"]:
                stealth.append({"板块": name, "5日涨幅%": _r(g5),
                                "5日主力净流入(亿)": _r(f5e, 2),
                                "换手分位%": t_pct,
                                "判定": "疑似资金提前埋伏（无公开催化、热度未起），可小仓位跟随并设止损"})
        # ② 高位放量 + 资金流出 → 警惕借利好出货
        if g5 > sc["hot_gain_min"] and f5e < 0:
            hist = hist_get(name)
            t_pct = None
            if hist is not None and "换手率" in hist.columns and len(hist):
                t_pct = _pctile(hist["换手率"].iloc[:-1], hist["换手率"].iloc[-1])
            if t_pct is None or t_pct >= sc["hot_turnover_pct_min"]:
                selling.append({"板块": name, "5日涨幅%": _r(g5),
                                "5日主力净流入(亿)": _r(f5e, 2),
                                "换手分位%": t_pct,
                                "判定": "高位放量+利好轰炸+资金流出，警惕借利好出货"})
    stealth.sort(key=lambda x: -(x["5日主力净流入(亿)"] or 0))
    selling.sort(key=lambda x: -(x["5日涨幅%"] or 0))
    return stealth, selling


def _cold_scan(hist_all: dict, c: dict, notes: list) -> list:
    """长期最冷门板块 Top N：冷度 = w1×成交额占比分位 + w2×换手率分位 + w3×涨幅分位。"""
    cc = c["cold"]
    window = int(cc["window"])
    rows = []
    for name, df in hist_all.items():
        if df is None or len(df) < 10:
            continue
        d = df.tail(window)
        g0 = _f(d["收盘"].iloc[0])
        rows.append({
            "板块": name,
            "额占比": _f(d["成交额"].sum()),
            "换手": _f(pd.to_numeric(d["换手率"], errors="coerce").mean()),
            "涨幅": (_f(d["收盘"].iloc[-1]) / g0 - 1) * 100 if g0 and g0 != 0 else None,
        })
    if len(rows) < 5:
        notes.append("冷门板块样本不足，已跳过")
        return []
    s = pd.DataFrame(rows)
    total = s["额占比"].sum() or 1.0
    s["成交额占比%"] = s["额占比"] / total * 100
    s["窗口换手%"] = s["换手"]
    s["窗口涨幅%"] = s["涨幅"]

    def _pr(col: str) -> pd.Series:
        """分位（0~1，越低越冷门）。缺失记 0.5 = 中性。

        原来直接 rank(pct=True)：某板块「期初价缺失」→ 窗口涨幅% 为 None → 该行
        加权和为 NaN。实测 pandas `nsmallest` 把 NaN 当作**最小值排到最后**，于是：
          · 冷度得分显示为空（_r(NaN)→None）；
          · 该板块被当成"最冷"垫底；当板块总数 > top_n 时直接被截掉 ——
            即"最冷门 Top N"里反而看不见它，且无任何提示。
        改为中性 0.5 后该板块按另两个维度正常参与排名，缺失信息只影响一个维度，
        并在 notes 里说明有几个板块受影响（可解释、不静默）。"""
        return pd.to_numeric(s[col], errors="coerce").rank(pct=True).fillna(0.5)

    s["冷度得分"] = (float(cc["w_amount"]) * _pr("成交额占比%")
                    + float(cc["w_turnover"]) * _pr("窗口换手%")
                    + float(cc["w_gain"]) * _pr("窗口涨幅%"))
    _n_gain_miss = int(pd.to_numeric(s["窗口涨幅%"], errors="coerce").isna().sum())
    if _n_gain_miss:
        notes.append(f"{_n_gain_miss} 个板块缺期初价，其涨幅维度按中性计分")
    # 分位升序：分位越低越冷门
    s = s.nsmallest(int(cc["top_n"]), "冷度得分")
    return [{"板块": r["板块"],
             "冷度得分": _r(r["冷度得分"], 3),
             "成交额占比%": _r(r["成交额占比%"], 2),
             f"{window}日换手%": _r(r["窗口换手%"], 2),
             f"{window}日涨幅%": _r(r["窗口涨幅%"], 2),
             "提示": "极度冷门——关注政策催化与均值回归，埋伏成本低但需耐心"}
            for _, r in s.iterrows()]


def _crowding_scan(hist_all: dict, merged: pd.DataFrame, c: dict) -> list:
    """板块拥挤度监控（高点逃离核心）。"""
    cc = c["crowding"]
    rows = []
    for name, df in hist_all.items():
        if df is None or len(df) < 30:
            continue
        last = df.iloc[-1]
        t_pct = _pctile(df["换手率"].iloc[:-1], last["换手率"])
        a_pct = _pctile(df["成交额"].iloc[:-1], last["成交额"])
        g20 = None
        if len(df) >= 21:
            c0 = _f(df["收盘"].iloc[-21])
            c1 = _f(last["收盘"])
            if c0 and c0 != 0 and c1:
                g20 = (c1 / c0 - 1) * 100
        flow5 = _flow5_of(merged, name)
        n = 0
        if t_pct is not None and t_pct >= float(cc["turnover_pct"]) * 100:
            n += 1
        if a_pct is not None and a_pct >= float(cc["amount_pct"]) * 100:
            n += 1
        if g20 is not None and g20 >= float(cc["gain20_min"]):
            n += 1
        if flow5 is not None and flow5 < 0:
            n += 1
        if n >= int(cc["trigger_n"]):
            rows.append({"板块": name, "触发项数": n,
                         "换手分位%": t_pct, "量能分位%": a_pct,
                         "20日涨幅%": _r(g20), "5日主力净流入(亿)": flow5,
                         "判定": "拥挤过热，提示分批止盈（参考2026年7月科技拥挤出清教训）"})
    rows.sort(key=lambda r: -r["触发项数"])
    return rows


# ============ 国家队资金：前十大股东名单口径（ETF/汇金/社保/证金 四列表） ============
_NT_MEM: dict = {"key": None, "df": None}   # 报告期级内存缓存（磁盘缓存见 _fetch_top10_holders）

_NT_GROUPS = [   # (key, 标题, 识别函数：股东名称 → bool)
    ("etf", "国家队 ETF 持仓（宽基）", lambda n: "ETF" in n and "联接" not in n),
    ("huijin", "中央汇金", lambda n: "汇金" in n),
    ("ssf", "社保基金", lambda n: ("社保" in n) or ("社会保障" in n) or ("养老" in n)),
    ("zhengjin", "中国证金", lambda n: "证券金融" in n),
]
_NT_ETF_WIDE = ("沪深300", "上证50", "中证500", "中证1000", "A500")   # 国家队常用宽基关键词
_NT_ETF_LIST = [   # 国家队（汇金/证金）常增持的宽基 ETF 白名单（基金代码, 名称）
    ("510300", "华泰柏瑞沪深300ETF"), ("510050", "华夏上证50ETF"),
    ("510310", "易方达沪深300ETF"), ("510330", "华夏沪深300ETF"),
    ("159919", "嘉实沪深300ETF"), ("510500", "南方中证500ETF"),
    ("512100", "南方中证1000ETF"), ("563300", "华泰柏瑞中证A500ETF"),
]


def _fetch_etf_holdings(end_date: str, prog: Callable, step: float,
                        total_steps: float, notes: list) -> list:
    """国家队宽基 ETF 的个股持仓聚合（基金季报口径：HOLDER_CODE=基金代码直查，单只 1 页秒回）。
    输出：被这些 ETF 持有的个股列表（按合计持股市值降序，Top 50）。"""
    import json as _json
    import requests

    root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    cache = _os.path.join(root, "data", f"etf_holdings_{end_date}.json")
    if _os.path.exists(cache) and _time.time() - _os.path.getmtime(cache) < 30 * 86400:
        try:
            with open(cache, encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            pass
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    agg: dict = {}
    for i, (code, name) in enumerate(_NT_ETF_LIST):
        prog(step + i / len(_NT_ETF_LIST) * 0.5, total_steps, f"国家队 ETF 持仓 {name}")
        params = {
            "sortColumns": "SECURITY_CODE", "sortTypes": "-1", "pageSize": "500", "pageNumber": "1",
            "reportName": "RPT_MAINDATA_MAIN_POSITIONDETAILS", "columns": "ALL", "quoteColumns": "",
            "filter": f'(HOLDER_CODE="{code}")(REPORT_DATE=\'{end_date}\')',
            "source": "WEB", "client": "WEB",
        }
        try:
            def fn():
                r = requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=25)
                return r.json()
            j = _net_call(fn)
        except Exception:  # 单只失败不影响整体
            notes.append(f"{name} 持仓拉取失败，已跳过")
            continue
        if not j.get("result"):
            continue
        for d in j["result"]["data"]:
            sc = d.get("SECURITY_CODE")
            if not sc:
                continue
            cap = pd.to_numeric(d.get("HOLD_MARKET_CAP"), errors="coerce")
            a = agg.setdefault(sc, {"code": sc, "name": str(d.get("SECURITY_NAME_ABBR") or ""),
                                    "n": 0, "cap": 0.0, "detail": []})
            a["n"] += 1
            a["cap"] += float(cap) if pd.notna(cap) else 0.0
            a["detail"].append(f"{name} {cap / 1e8:.1f}亿" if pd.notna(cap) else name)
    agg = sorted(agg.values(), key=lambda x: -x["cap"])[:50]
    out = [{"代码": a["code"], "简称": a["name"], "命中ETF只数": a["n"],
            "合计持仓市值(亿)": round(a["cap"] / 1e8, 1),
            "持有ETF明细": "；".join(a["detail"][:3]) + ("…" if len(a["detail"]) > 3 else "")}
           for a in agg]
    try:
        with open(cache, "w", encoding="utf-8") as f:
            _json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    return out


def _latest_report_date() -> str:
    """最近一个已完成披露的报告期（Q1→4/30、H1→8/31、Q3→10/31、年报→次年4/30）。"""
    today = _dt.date.today()
    best = None
    for y in (today.year, today.year - 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            rd = _dt.date(y, m, d)
            deadline = {3: _dt.date(y, 4, 30), 6: _dt.date(y, 8, 31),
                        9: _dt.date(y, 10, 31), 12: _dt.date(y + 1, 4, 30)}[m]
            if deadline <= today and (best is None or rd > best):
                best = rd
    return best.strftime("%Y-%m-%d") if best else f"{today.year - 1}-09-30"


def _fetch_top10_holders(end_date: str, prog: Callable, step: float,
                         total_steps: float, notes: list) -> Optional[pd.DataFrame]:
    """全市场十大股东明细（东财 datacenter 报表，持股≥1000 万股口径以压缩行数）。
    报告期级磁盘缓存 data/gdfx_holders_<date>.csv（30 天有效）：一个季度只联网拉一次。"""
    import requests

    root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    cache = _os.path.join(root, "data", f"gdfx_holders_{end_date}.csv")
    if _os.path.exists(cache) and _time.time() - _os.path.getmtime(cache) < 30 * 86400:
        try:
            return pd.read_csv(cache, dtype={"SECURITY_CODE": str})
        except Exception:  # 缓存损坏则重拉
            pass
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    base = {
        "sortColumns": "NOTICE_DATE,SECURITY_CODE,RANK", "sortTypes": "-1,1,1",
        "pageSize": "500", "pageNumber": "1",
        "reportName": "RPT_CUSTOM_DMSK_HOLDERS_JOIN_HOLDER_SHAREANALYSIS",
        "columns": "ALL", "source": "WEB", "client": "WEB",
        "filter": f"(END_DATE='{end_date}')(HOLD_NUM>10000000)",
    }

    def _get(page: int) -> dict:
        def fn():
            r = requests.get(url, params={**base, "pageNumber": page},
                             headers={"User-Agent": _UA}, timeout=25)
            return r.json()
        return _net_call(fn)

    first = _get(1)
    if not first.get("result"):
        notes.append(f"十大股东接口无返回（{str(first.get('message', '未知'))[:60]}），国家队名单已跳过")
        return None
    pages = int(first["result"]["pages"])
    rows = list(first["result"]["data"])
    for p in range(2, pages + 1):
        prog(step + (p - 1) / pages * 0.9, total_steps, f"十大股东明细 {p}/{pages} 页")
        j = _get(p)
        if not j.get("result"):
            notes.append(f"十大股东第 {p} 页拉取失败，已用前 {len(rows)} 条继续")
            break
        rows.extend(j["result"]["data"])
        _time.sleep(0.12)
    df = pd.DataFrame(rows)
    try:
        df.to_csv(cache, index=False, encoding="utf-8-sig")
    except Exception:  # 缓存写失败不影响结果
        pass
    return df


def _national_holders(c: dict, prog: Callable, step: float,
                      total_steps: float, notes: list) -> dict:
    """国家队机构出现在最新报告期前十大股东名单的股票，按 ETF/汇金/社保/证金 四列表分组。"""
    groups_cfg = c.get("groups")
    if groups_cfg is None:   # 未传 = 四列表全开；注意空数组 [] 表示用户全不勾，不能被 or 回退成全开
        groups_cfg = ["etf", "huijin", "ssf", "zhengjin"]
    end_date = _latest_report_date()
    key = f"{end_date}:{','.join(groups_cfg)}"
    if _NT_MEM["key"] == key and _NT_MEM["df"] is not None:
        df = _NT_MEM["df"]
    else:
        prog(step, total_steps, "十大股东名单（新报告期首次约 1~2 分钟，之后读缓存秒开）…")
        df = _fetch_top10_holders(end_date, prog, step, total_steps, notes)
        if df is None or df.empty:
            return {"date": end_date, "groups": {}}
        _NT_MEM.update(key=key, df=df)

    def _fmt_rows(g: pd.DataFrame) -> list:
        g = g.sort_values("HOLDER_MARKET_CAP", ascending=False).head(50)
        out = []
        for _, r in g.iterrows():
            num = pd.to_numeric(r.get("HOLD_NUM"), errors="coerce")
            cap = pd.to_numeric(r.get("HOLDER_MARKET_CAP"), errors="coerce")
            ratio = pd.to_numeric(r.get("HOLD_RATIO"), errors="coerce")
            rank = pd.to_numeric(r.get("RANK"), errors="coerce")
            out.append({
                "代码": str(r.get("SECURITY_CODE", "")),
                "简称": str(r.get("SECURITY_NAME_ABBR", "")),
                "股东名称": str(r.get("HOLDER_NAME", "")),
                "持股(万股)": round(num / 1e4) if pd.notna(num) else None,
                "占总股本%": round(ratio, 2) if pd.notna(ratio) else None,
                "市值(亿)": round(cap / 1e8, 1) if pd.notna(cap) else None,
                "变动": str(r.get("HOLDNUM_CHANGE_NAME") or "—"),
                "排名": int(rank) if pd.notna(rank) else None,
            })
        return out

    names = df["HOLDER_NAME"].astype(str)
    groups: dict = {}
    for key_, title, matcher in _NT_GROUPS:
        if key_ not in groups_cfg:
            continue
        if key_ == "etf":
            # ETF 不在股东名册报表中（基金走独立披露体系）→ 用宽基 ETF 基金季报持仓聚合
            rows = _fetch_etf_holdings(end_date, prog, step + 0.3, total_steps, notes)
            groups[key_] = {"title": title, "rows": rows}
            continue
        mask = names.map(matcher)
        groups[key_] = {"title": title, "rows": _fmt_rows(df[mask])}
    if groups and not any(g["rows"] for g in groups.values()):
        notes.append(f"{end_date} 报告期未发现国家队持仓记录（十大股东口径持股≥1000 万股）")
    return {"date": end_date, "groups": groups}


def _foreign_top(c: dict, notes: list) -> list:
    """外资（北向）重仓 Top N。披露口径若调整导致无数据则降级跳过。"""
    try:
        df = _ak().stock_hsgt_hold_stock_em(market="北向", indicator="今日排行")
    except Exception as e:
        notes.append(f"北向个股持仓获取失败：{e}")
        return []
    if df is None or df.empty:
        notes.append("北向个股持仓接口无数据（披露口径调整），外资重仓已跳过")
        return []
    col_amt, col_pct = "今日估算-持股市值", "今日估算-持股占比"
    if col_amt not in df.columns:
        notes.append("北向接口列名变化，外资重仓已跳过")
        return []
    top = df.nlargest(int(c["foreign"]["top_n"]), col_amt)
    out = []
    for _, r in top.iterrows():
        pct = _f(r.get(col_pct))
        out.append({"名称": r.get("名称"), "代码": str(r.get("编码", "")).zfill(6),
                    "持股市值(亿)": _r(_f(r.get(col_amt)) / 1e8),
                    "持股占比%": (_r(pct * 100, 2) if pct is not None and pct < 1
                                  else _r(pct, 2))})
    return out


def _chain_radar(merged: pd.DataFrame, c: dict) -> list:
    """产业链传导雷达：上游/中游板块实时 5 日涨幅与资金进展。"""
    rows = []
    idx = merged.set_index("板块名称") if "板块名称" in merged.columns else None
    for ch in TRANSMIT_CHAINS:
        for role, names in (("上游", ch.get("上游板块", [])),
                            ("中游", ch.get("中游板块", []))):
            for nm in names:
                g5, f5e = None, None
                if idx is not None and nm in idx.index:
                    row = idx.loc[nm]
                    if isinstance(row, pd.DataFrame):
                        row = row.iloc[0]
                    f5 = _f(row.get("5日主力净流入-净额"))
                    g5, f5e = _f(row.get("5日涨跌幅")), (f5 / 1e8 if f5 is not None else None)
                rows.append({"传导链": ch["上游"], "环节": role, "板块": nm,
                             "5日涨幅%": _r(g5), "5日主力净流入(亿)": _r(f5e, 2),
                             "监测指标": ch.get("监测指标", "") if role == "上游" else "",
                             "逻辑": ch.get("逻辑", "")})
    return rows


# ==================================================================
# 三·五、特殊概念（名字玄学 / 谐音梗 / 生肖字辈 / 日期彩头）
# ------------------------------------------------------------------
# A 股几乎每年都会轮动一轮「无厘头题材」：名字带某个字、谐音某个生肖、撞上某个
# 纪念日的彩头，就会被资金选作情绪载体（「名字带龙就涨」「代码吉利就炒」
# 「逢生肖年就疯」）。本模块把它做成一张可热更新的规则表，两条触发口径：
#   · 窗口类（日期彩头 / 生肖）—— 进入窗口即列出，事件本身即是信号；
#   · 常驻类（字辈 / 动物 / 代码）—— 平时不显示，只有当该字辈显著跑赢全市场
#     （命中组均涨 − 全市场均涨 ≥ excess_min，且有大涨/涨停个股）时才报警，
#     避免「天天都有字辈涨停」把雷达刷成白噪音。
# 规则表是纯数据结构（SPECIAL_STANDING / SPECIAL_DATES / SPECIAL_CODE），
# 可随时增删；输出一律为「情绪观察提示」，不是买入建议。
# ==================================================================

# 农历春节 → 属相（边界＝春节当日，春节后进入新属相）。用于生肖概念的年份判定。
SPRING_FESTIVAL = [
    ("2020-01-25", "鼠"), ("2021-02-12", "牛"), ("2022-02-01", "虎"), ("2023-01-22", "兔"),
    ("2024-02-10", "龙"), ("2025-01-29", "蛇"), ("2026-02-17", "马"), ("2027-02-06", "羊"),
    ("2028-01-26", "猴"), ("2029-02-13", "鸡"), ("2030-02-03", "狗"), ("2031-01-23", "猪"),
    ("2032-02-11", "鼠"), ("2033-01-31", "牛"), ("2034-02-19", "虎"), ("2035-02-08", "兔"),
    ("2036-01-28", "龙"), ("2037-02-15", "蛇"), ("2038-02-04", "马"), ("2039-01-24", "羊"),
    ("2040-02-12", "猴"),
]

# 生肖 → 名称命中字（正字 + 市面公认的谐音字）。举一反三时只需改这里。
ZODIAC_HOMOPHONE = {
    "鼠": ["鼠", "蜀"],
    "牛": ["牛", "犇"],
    "虎": ["虎", "琥"],
    "兔": ["兔"],
    "龙": ["龙", "珑", "辰"],
    "蛇": ["蛇"],
    "马": ["马", "玛", "码"],
    "羊": ["羊", "洋", "扬", "阳"],
    "猴": ["猴"],
    "鸡": ["鸡", "吉"],
    "狗": ["狗"],
    "猪": ["猪", "珠", "朱"],
}

# 生肖 → 一句民俗背景（写进报告，说明为什么是这几个字）
ZODIAC_LORE = {
    "鼠": "A 股无「鼠」字标的，靠「蜀」等近音扩圈，生肖行情里最弱的一年",
    "牛": "「牛来」谐音「牛市来」——中信证券点名过的集体许愿梗（罗牛山、金牛化工）",
    "虎": "「虎」字标的稀缺，靠「琥」等近音扩圈",
    "兔": "2023 兔年兔宝宝涨近 300%，三个月后跌回起点（最经典的生肖记忆）",
    "龙": "「名字带龙就涨」——生肖＋图腾双重加持，龙字辈数量最多、最易扩圈",
    "蛇": "A 股几乎无「蛇」字标的，多数年份无行情",
    "马": "「马到成功」；马年主线，靠「玛/码」同音扩圈（福龙马、万里马、马矿股份）",
    "羊": "「三羊开泰」；丁未水羊年，带水旁的「洋」五行最贴，阳＝羊（三羊马、水羊股份、澳洋健康）",
    "猴": "「猴」字标的极少，靠「侯」字扩圈",
    "鸡": "「鸡」字标的极少，靠「吉」谐音扩圈",
    "狗": "A 股无「狗」字标的，多数年份无行情",
    "猪": "「珠/朱/株」谐音扩圈，猪周期与名字双驱动",
}

# ---- 常驻字辈（平时隐藏，显著跑赢全市场才报警）----
# 精简原则：只保留「历史上被反复炒作、且字辈本身有独立含义」的组；同义组必须合并，
# 否则「中字辈」与「国字辈」命中重叠 69 只（占中字辈 21%），两张表会给出同义结论。
SPECIAL_STANDING = [
    {"id": "hua", "概念": "华字辈 · 中华概念", "字符": ["华"],
     "依据": "「华」＝中华/华夏，重大纪念日与民族情绪节点最易被选作情绪载体"
             "（2026-09-18 华软科技、华天科技、华鑫股份等集体涨停）"},
    {"id": "zhongzi", "概念": "中字头央企（中/国）", "字符": ["中", "国"],
     "依据": "「中/国」双字头基本盘是央企国企，兼具家国彩头与中特估逻辑。"
             "两字原本分列，但交集达 69 只（中国神华、中国银行、中国中免…）→ 已合并为一组，避免同义重复"},
    {"id": "zhaocai", "概念": "招财字辈（发/财/金/鑫/银/富）", "字符": ["发", "财", "金", "鑫", "银", "富"],
     "依据": "纯彩头字辈：名字里带「发/财/金/鑫」天然讨喜，游资偏爱"},
    {"id": "jiqing", "概念": "吉庆字辈（福/泰/吉/祥/旺/盛/隆/兴）",
     "字符": ["福", "泰", "吉", "祥", "旺", "盛", "隆", "兴"],
     "依据": "传统吉语字辈，弱势行情里常作为「讨彩头」的抱团方向"},
    {"id": "animal", "概念": "动物字辈（炒动物行情）",
     "字符": ["龙", "马", "羊", "牛", "虎", "兔", "狼", "象", "鹿", "鹏", "凤", "麒麟",
             "豹", "鹰", "鹤", "燕", "鱼", "猪", "蛇", "鸡", "狗", "鼠", "猫", "猴",
             "骆驼", "鲸", "蜂"],
     "依据": "A 股「传统保留节目」：名字带动物的个股每年都会集体冲板"
             "（2026-09-18 大亚圣象、七匹狼、鹿山新材、福龙马等）"},
]

# ---- 代码玄学（尾号彩头）----
SPECIAL_CODE = [
    {"id": "lucky_code", "概念": "代码吉利（尾号 888/168/518/666/999/918）",
     "代码尾": ["888", "168", "518", "666", "999", "918", "188", "198"],
     "依据": "「代码吉利就炒」——尾号谐音（发发发 / 一路发 / 我要发 / 就要发）"},
]

# 已下线的常驻组（保留记录，说明为何精简；不要重新加回，除非有新的实证催化）
SPECIAL_RETIRED = {
    "dongfang": "「东方系」仅 30 只、字符过窄（只认「东方」二字，不含「东」），"
                "实测超额长期在 0 附近，属于「列了也不说明问题」的行 → 下线",
    "shuzi": "「数字字辈」把三/五/七/八/九/百/千/万全收进来共 234 只，"
             "与吉庆/招财/动物大量交叉；数字本身缺乏统一催化，属噪声 → 下线"
             "（真正的生肖数字梗如「三羊马」已由生肖组覆盖）",
}

# ---- 日期彩头（±window 天窗口内列出）----
# 精简原则：只保留「谐音彩头 + 情绪催化」双条件成立的日期；纯节日（教师节/青年节/
# 520）命中面窄且历史上无稳定集体行情，会稀释信号 → 下线。
SPECIAL_DATES = [
    {"id": "d0918", "日期": (9, 18), "概念": "9·18「就要发」× 中华概念", "字符": ["华", "发"],
     "依据": "9·18 谐音「就要发」，叠加九一八纪念日的民族情绪，"
             "「华」字辈常被当日的「中华概念」情绪载体集体拉抬"},
    {"id": "d1001", "日期": (10, 1), "概念": "国庆「国/庆/华」", "字符": ["国", "庆", "华"],
     "依据": "国庆长假前后消费＋家国叙事双催化，名字带「国/庆/华」的标的易被点名"},
    {"id": "d1213", "日期": (12, 13), "概念": "国家公祭日「国/华」", "字符": ["国", "华"],
     "依据": "国家公祭日的家国情绪窗口，情绪强度弱于 9·18"},
    {"id": "d0801", "日期": (8, 1), "概念": "八一建军「军/兵/武」", "字符": ["军", "兵", "武"],
     "依据": "建军节的军工情绪窗口，名字带「军/兵」的标的常被顺带炒作"},
    {"id": "d0701", "日期": (7, 1), "概念": "七一建党「党/国/华」", "字符": ["党", "国", "华"],
     "依据": "建党纪念日的家国情绪窗口"},
    {"id": "d0808", "日期": (8, 8), "概念": "8·8「发发」", "字符": ["发", "八"],
     "依据": "双八谐音「发发」，彩头字辈的日子型催化"},
]

# 已下线的日期彩头（保留说明，避免以后被当成漏写补回来）
SPECIAL_DATES_RETIRED = {
    "d0504": "五四青年节「青/年」——「年」「青」命中面窄且无稳定集体行情",
    "d0910": "教师节「教/育/师」——命中少，主要由教育板块政策驱动而非日期彩头",
    "d0520": "5·20「爱/情/心」——「心」字命中过宽且与题材无关",
    "d0101": "元旦「元/新」——「新」字命中过宽（新能源/新材料…），噪声大于信号",
    "d0909": "9·9「久久」/重阳——与「数字字辈」重复，且九字辈已下线",
    "d0903": "9·3 抗战胜利纪念——紧邻 9·18，家国窗口重叠，只保留强度更高的 9·18",
}

_SPOT_MEM: dict = {"ts": 0.0, "df": None, "src": ""}   # 全市场快照内存缓存（120s）

# 腾讯行情 qt.gtimg.cn 字段下标（实测核验：华软科技 +10.10%／成交额 43249 万／换手 13.02）
_TX_IDX = {"名称": 1, "代码": 2, "最新价": 3, "昨收": 4, "成交量": 6,
           "涨跌额": 31, "涨跌幅": 32, "最高": 33, "最低": 34,
           "成交额(万)": 37, "换手率": 38, "流通市值(亿)": 44, "总市值(亿)": 45,
           "市净率": 46, "涨停价": 47, "跌停价": 48, "量比": 49}
_BJ_PREFIX = ("43", "83", "87", "88", "92")   # 北交所代码段


def _tx_symbol(code: str) -> str:
    """6 位代码 → 腾讯行情前缀（sh/sz/bj）。北交所需先判，否则 920xxx 会被误判为沪市。"""
    c = str(code).zfill(6)
    if c.startswith(_BJ_PREFIX):
        return "bj" + c
    return ("sh" if c[0] in ("6", "9") else "sz") + c


def _all_codes() -> dict:
    """全市场 {6位代码: 简称}：交易所官方名表（含沪深，落盘缓存 7 天）
    + 本地库 meta 表（补北交所）。两个源都拿不到时返回 {}。"""
    out: dict = {}
    try:
        from .ticker import _name_map
        out.update({str(k).zfill(6): str(v) for k, v in (_name_map() or {}).items()})
    except Exception:  # noqa: BLE001 —— 名表失败不致命，下面还有本地库
        pass
    try:
        import sqlite3
        root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        p = _os.path.join(root, "data", "unified_data.db")
        if _os.path.exists(p):
            with sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=15) as con:
                for c, n in con.execute("SELECT code, name FROM meta"):
                    out.setdefault(str(c).zfill(6), str(n or ""))
    except Exception:  # noqa: BLE001
        pass
    return {k: v for k, v in out.items() if len(k) == 6 and k.isdigit()}


def _tencent_spot(codes: list, batch: int = 800) -> pd.DataFrame:
    """腾讯批量行情：800 只/请求，全市场约 7 个请求（实测 <2s，含科创/创业/北交所）。"""
    import requests as _rq
    rows = []
    for i in range(0, len(codes), batch):
        chunk = [_tx_symbol(c) for c in codes[i:i + batch]]
        r = _rq.get("http://qt.gtimg.cn/q=" + ",".join(chunk),
                    headers={"User-Agent": _UA}, timeout=15)
        r.encoding = "gbk"
        for line in r.text.split(";"):
            line = line.strip()
            if not line.startswith("v_") or '="' not in line:
                continue
            f = line.partition('="')[2].rstrip('"').split("~")
            if len(f) <= max(_TX_IDX.values()):
                continue
            try:
                vol = float(f[_TX_IDX["成交量"]] or 0)
            except ValueError:
                vol = 0.0
            if vol <= 0:            # 停牌/无成交：不计入涨跌统计（否则会污染「命中/上涨」口径）
                continue
            rows.append({
                "代码": str(f[_TX_IDX["代码"]]).zfill(6),
                "名称": str(f[_TX_IDX["名称"]]).strip(),
                "最新价": _f(f[_TX_IDX["最新价"]]),
                "涨跌幅": _f(f[_TX_IDX["涨跌幅"]]),
                "成交额": (_f(f[_TX_IDX["成交额(万)"]]) or 0) * 1e4,
                "换手率": _f(f[_TX_IDX["换手率"]]),
                "量比": _f(f[_TX_IDX["量比"]]),
            })
    return pd.DataFrame(rows)


def _market_spot(notes: list) -> tuple:
    """全市场快照（代码/名称/最新价/涨跌幅/成交额(元)/换手率/量比）。
    主源＝腾讯批量行情；备源＝东财 stock_zh_a_spot_em（push2 被 WAF 封时自动跳过）；
    末源＝core.sync 官方名表（只有名称、无行情，本模块会据此如实跳过）。
    返回 (df | None, 数据源名)。"""
    if _SPOT_MEM["df"] is not None and _time.time() - _SPOT_MEM["ts"] < 120:
        return _SPOT_MEM["df"], _SPOT_MEM["src"]
    df, src = None, ""
    codes = _all_codes()
    if len(codes) >= 1000:
        try:
            df = _tencent_spot(sorted(codes), batch=800)
            if df is not None and not df.empty:
                src = f"腾讯批量行情（{len(df)} 只有成交，含沪深京）"
            else:
                df = None
        except Exception as e:  # noqa: BLE001
            notes.append(f"腾讯批量行情失败（{str(e)[:50]}），改用备用源")
            df = None
    if df is None:
        try:
            raw = _net_call(lambda: _ak().stock_zh_a_spot_em(), retries=2)
            if raw is not None and not raw.empty and {"代码", "名称"} <= set(raw.columns):
                keep = [c for c in ("代码", "名称", "最新价", "涨跌幅",
                                    "成交额", "换手率", "量比") if c in raw.columns]
                df = raw[keep].copy()
                src = f"东财全市场快照（{len(df)} 只）"
        except Exception as e:  # noqa: BLE001 —— WAF/代理抖动时降级，不中断整次预警
            notes.append(f"东财全市场快照失败（{str(e)[:50]}），改用备用源")
    if df is None:
        try:
            from ..core.sync import fetch_spot   # 惰性导入：避免模块级循环依赖
            sp = fetch_spot()
            if sp is not None and not sp.empty:
                df = sp.rename(columns={"code": "代码", "name": "名称",
                                        "price": "最新价", "change_pct": "涨跌幅"})
                src = "备用名表（沪深京官方列表 / 新浪，无成交额与换手）"
        except Exception as e:  # noqa: BLE001
            notes.append(f"备用快照亦不可用：{str(e)[:50]}")
    if df is not None:
        df = df.copy()
        df["代码"] = df["代码"].astype(str).str.extract(r"(\d{6})", expand=False).fillna("")
        df["名称"] = df["名称"].astype(str).str.strip()
        for col in ("最新价", "涨跌幅", "成交额", "换手率", "量比"):
            df[col] = pd.to_numeric(df.get(col), errors="coerce")
        df = df[df["代码"] != ""].reset_index(drop=True)
        _SPOT_MEM.update(ts=_time.time(), df=df, src=src)
    return df, src



def _limit_pct(code: str, name: str) -> float:
    """该股的涨停幅度阈值(%)：创业板/科创板 20、北交所 30、主板 ST 5、其余 10。"""
    c = str(code or "")
    if c.startswith(("300", "301", "302", "688", "689")):
        return 20.0
    if c.startswith(("43", "83", "87", "88", "92")):   # 北交所
        return 30.0
    return 5.0 if "ST" in str(name or "").upper() else 10.0


_ZODIAC_ORDER = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]


def _zodiac_now(today: _dt.date) -> tuple:
    """返回 (当前属相, 下一属相, 距下一个春节的天数, 下一个春节日期, 当前属相起始日)。

    2020-2040 用逐年的精确春节表；表外按 12 年周期外推（春节日 ≈ 锚点 + n×365.2425 天，
    误差 ±2 天，仅影响极远期年份的概念判定，不影响生肖本身）。"""
    table = [(_dt.datetime.strptime(d, "%Y-%m-%d").date(), z) for d, z in SPRING_FESTIVAL]
    if today < table[0][0] or today >= table[-1][0] + _dt.timedelta(days=400):
        anchor = table[6][0]                       # 2026-02-17 = 马年
        steps = today.year - anchor.year
        cur_z = _ZODIAC_ORDER[(6 + steps) % 12]
        start = _dt.date(today.year, anchor.month, anchor.day)
        if start > today:
            start = start.replace(year=start.year - 1)
        nxt_start = _dt.date(start.year + 1, anchor.month, anchor.day)
        nxt_z = _ZODIAC_ORDER[(_ZODIAC_ORDER.index(cur_z) + 1) % 12]
        return cur_z, nxt_z, (nxt_start - today).days, nxt_start, start
    cur = table[0]
    for d, z in table:
        if d <= today:
            cur = (d, z)
        else:
            break
    i = table.index(cur)
    nxt = table[i + 1] if i + 1 < len(table) else \
        (cur[0].replace(year=cur[0].year + 12), cur[1])   # 12 年一轮回
    return cur[1], nxt[1], (nxt[0] - today).days, nxt[0], cur[0]



def _special_rules(today: _dt.date, cfg: dict) -> list:
    """按今天日期/生肖筛出本次生效的概念规则（含窗口依据文案）。"""
    out: list = []
    for r in SPECIAL_STANDING:
        out.append({**r, "类型": "字辈", "窗口依据": "常驻（跑赢全市场才报警）"})
    for r in SPECIAL_CODE:
        out.append({**r, "类型": "代码", "窗口依据": "常驻（跑赢全市场才报警）"})
    win = int(cfg.get("window", 3) or 3)
    for r in SPECIAL_DATES:
        mm, dd = r["日期"]
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                ed = _dt.date(year, mm, dd)
            except ValueError:
                continue
            delta = (ed - today).days
            if abs(delta) <= win:
                when = "今日" if delta == 0 else (f"还有 {delta} 天" if delta > 0 else f"已过 {-delta} 天")
                out.append({**r, "类型": "日期彩头",
                            "窗口依据": f"{ed.strftime('%Y-%m-%d')}（{when}）"})
                break
    cur_z, nxt_z, days_next, next_date, cur_start = _zodiac_now(today)
    out.append({"id": f"zodiac_{cur_z}", "概念": f"{cur_z}字辈 · 当前生肖年",
                "字符": list(ZODIAC_HOMOPHONE.get(cur_z, [cur_z])), "类型": "生肖",
                "窗口依据": f"{cur_z}年（{cur_start.strftime('%Y-%m-%d')} 春节起）",
                "_lead_days": 10 ** 9,   # 当前生肖：全年在窗，离高潮最远
                "依据": ZODIAC_LORE.get(cur_z, "")})
    lead = int(cfg.get("zodiac_lead_days", 150) or 150)
    if days_next <= lead:
        out.append({"id": f"zodiac_{nxt_z}", "概念": f"{nxt_z}字辈 · {nxt_z}年即将到来",
                    "字符": list(ZODIAC_HOMOPHONE.get(nxt_z, [nxt_z])), "类型": "生肖",
                    "窗口依据": f"{nxt_z}年春节 {next_date.strftime('%Y-%m-%d')}（还有 {days_next} 天）",
                    "_lead_days": days_next,   # 距春节越近，情绪越容易起来
                    "依据": ZODIAC_LORE.get(nxt_z, "")})
    return out


def _rule_mask(df: pd.DataFrame, rule: dict) -> pd.Series:
    """规则 → 名称/代码命中掩码。"""
    m = pd.Series(False, index=df.index)
    for ch in (rule.get("字符") or []):
        m |= df["名称"].str.contains(ch, regex=False, na=False)
    for tail in (rule.get("代码尾") or []):
        m |= df["代码"].str.endswith(str(tail))
    return m


def _special_scan(c: dict, notes: list) -> dict:
    """特殊概念扫描：全市场名称/代码匹配 → 概念内涨幅结构 vs 全市场基准。"""
    cfg = c["special"]
    today = _dt.date.today()
    empty = {"date": today.strftime("%Y-%m-%d"), "src": "", "concepts": [], "stocks": [],
             "zodiac": {}, "market_avg": None}
    spot, src = _market_spot(notes)
    if spot is None or spot.empty:
        notes.append("全市场快照不可用，特殊概念已跳过")
        return empty

    df = spot.rename(columns={"涨跌幅": "涨跌幅%", "换手率": "换手率%"}).copy()
    if "涨跌幅%" not in df.columns or df["涨跌幅%"].notna().sum() == 0:
        notes.append("快照缺涨跌幅字段（降级源无行情），特殊概念已跳过")
        return {**empty, "src": src}
    df["成交额(亿)"] = pd.to_numeric(df.get("成交额"), errors="coerce") / 1e8
    df["涨停"] = [bool(p >= _limit_pct(cc, nn) - 0.2)
                 for p, cc, nn in zip(df["涨跌幅%"].fillna(-99), df["代码"], df["名称"])]
    valid = df[df["涨跌幅%"].notna()]
    mkt_avg = float(valid["涨跌幅%"].mean()) if len(valid) else 0.0
    mkt_lu = float(valid["涨停"].mean() * 100) if len(valid) else 0.0   # 全市场涨停率(%)
    up_ratio_mkt = float((valid["涨跌幅%"] > 0).mean()) if len(valid) else 0.0  # 全市场上涨家数占比

    surge = float(cfg.get("surge_pct", 5.0) or 5.0)
    min_hits = int(cfg.get("min_hits", 5) or 5)
    top_n = int(cfg.get("top_n", 15) or 15)
    excess_min = float(cfg.get("excess_min", 1.5) or 1.5)
    surge_n_min = int(cfg.get("surge_n_min", 3) or 3)
    lu_min = int(cfg.get("limitup_min", 2) or 2)
    lu_ratio_min = float(cfg.get("limitup_ratio_min", 2.0) or 2.0)
    # 密度型触发（gate_b）的辅助门槛：仅靠「涨停密度高」就报触发，会被「少数个股冲板、
    # 整体在跌」的组骗到（实测华字辈 307 只 8 涨停、密度 2.58×，但均涨跑输、宽度 −1.7pp）。
    # 因此密度型触发额外要求「组内上涨家数占比不显著低于全市场」——用宽度（breadth）这个
    # 结构性口径，而不是拿超额均涨去比一个近乎 0 的小数（−0.16% 与 +0.01% 会得出相反结论，
    # 属于典型的「刀锋效应」，没有统计意义）。默认 −5pp 容差，即允许小幅落后。
    density_breadth_floor = float(cfg.get("density_breadth_floor", 0.0))
    standing_n = int(cfg.get("standing_top_n", 3) or 0)

    concepts, stocks, observed, window_cold = [], [], [], []
    for rule in _special_rules(today, cfg):
        g = df[_rule_mask(df, rule)]
        hit = int(len(g))
        if hit < min_hits:
            continue
        gv = g[g["涨跌幅%"].notna()]
        if not len(gv):
            continue
        n_up = int((gv["涨跌幅%"] > 0).sum())
        n_lu = int(gv["涨停"].sum())
        n_surge = int((gv["涨跌幅%"] >= surge).sum())
        avg = float(gv["涨跌幅%"].mean())
        excess = avg - mkt_avg
        lu_pct = n_lu / len(gv) * 100
        lu_ratio = (lu_pct / mkt_lu) if mkt_lu > 0 else 0.0
        # 宽度：组内上涨家数占比 − 全市场上涨家数占比（百分点）。这是与「均涨幅」互补的
        # 独立维度：均涨幅会被少数暴涨股拉高，宽度看的是「有多少票在跟」。
        breadth = (n_up / len(gv) * 100) - (up_ratio_mkt * 100)
        is_window = rule["类型"] in ("日期彩头", "生肖")
        # 触发①：整体跑赢全市场 且 有大涨/涨停；触发②：涨停密度显著高于全市场（批量冲板）
        gate_a = excess >= excess_min and (n_lu >= 1 or n_surge >= surge_n_min)
        # gate_b（批量冲板型触发）：涨停密度显著高于全市场，**且组内跟涨的家数不塌方**。
        # 只用密度 + 超额均涨判断会被「少数个股涨停」骗到：华字辈 307 只里 8 只涨停
        # （密度 2.58×）但宽度 −1.7pp、均涨跑输，报成"批量冲板触发"就是给用户相反信号。
        # 这里改用宽度（breadth，与均涨互补的独立维度）守门：min(n_lu, lu_min) 保证确有
        # 批量涨停，宽度门槛保证"确有批量跟涨"而不是孤立几只。
        gate_b = (n_lu >= lu_min and lu_ratio >= lu_ratio_min
                  and breadth >= density_breadth_floor)
        # 窗口类（日期彩头 / 生肖）原先"进窗即列"，实测马字辈全年在列、超额 −1.01、
        # 0 涨停 0 大涨 —— 这就是用户说的"太多太杂"。现改为：
        #   有热度（gate_a 或 gate_b）→ 正常列出；
        #   无热度 → 只保留「强势窗口」（宽度显著高于全市场）或临近高潮的窗口，
        #   其余归入 notes 一行带过，不再占用正式表格。
        strong_window = gate_a or gate_b
        near_peak = rule["类型"] == "日期彩头" or (
            rule["类型"] == "生肖" and rule.get("_lead_days", 10 ** 9) <= 45)
        # 第三档「异动」：涨停密度够高、但**跟涨家数没跟上**（宽度显著落后全市场）→
        # 说明是「少数个股冲板」而非「整个字辈在动」。这既不能报「触发」（会误导），
        # 也不该丢掉（8 只涨停是真实信息）→ 单列一档，前端与信号合成只把它当观察项，
        # 不做方向结论。
        lopsided = (not strong_window) and n_lu >= lu_min and lu_ratio >= lu_ratio_min
        active = strong_window or (is_window and (breadth >= 0 or near_peak))
        lead = gv.sort_values("涨跌幅%", ascending=False).iloc[0]
        row = {
            "概念": rule["概念"], "类型": rule["类型"], "窗口依据": rule["窗口依据"],
            "命中": hit, "上涨": n_up, "涨停": n_lu, "大涨": n_surge,
            "涨停%": _r(lu_pct, 2), "均涨幅%": _r(avg, 2), "超额%": _r(excess, 2),
            "宽度%": _r(breadth, 1), "涨停密度×": _r(lu_ratio, 2),
            "最强": f"{lead['名称']} {_r(lead['涨跌幅%'], 2)}%",
            "触发": bool(active), "异动": bool(lopsided or (is_window and not active)),
            "判定": ("批量冲板 %.1f× · 宽度 %+.1fpp" % (lu_ratio, breadth)) if gate_b else
                    ("跑赢全市场 %+.2f" % excess) if gate_a else
                    ("涨停 %.1f× 但宽度 %+.1fpp（仅少数个股冲板，跟涨家数未扩散）"
                     % (lu_ratio, breadth)) if lopsided else
                    ("窗口内 · 未达热度门槛" if is_window else "观察（未达阈值）"),
            "依据": rule.get("依据", ""),
        }
        if active or lopsided:
            concepts.append(row)
            # 明细只留「真的在动」的票：上涨 且（大涨 或 量比>1.5）。原先只取涨幅前 N，
            # 会把 +0.01% 的僵尸票也列进去，与「该概念在动」的结论自相矛盾。
            top = gv.sort_values("涨跌幅%", ascending=False)
            movers = top[(top["涨跌幅%"] > 0) &
                         ((top["涨跌幅%"] >= surge) |
                          (pd.to_numeric(top["量比"], errors="coerce").fillna(0) >= 1.5))]
            if movers.empty:      # 极端情况（全组无量比数据）退回原口径，避免明细整片空白
                movers = top[top["涨跌幅%"] > 0]
            for _, r in movers.head(top_n).iterrows():
                stocks.append({
                    "概念": rule["概念"], "代码": r["代码"], "简称": r["名称"],
                    "涨跌幅%": _r(r["涨跌幅%"], 2), "最新价": _r(r["最新价"], 2),
                    "成交额(亿)": _r(r["成交额(亿)"], 2), "换手率%": _r(r["换手率%"], 2),
                    "量比": _r(r["量比"], 2),
                    "涨停": "涨停" if r["涨停"] else "",
                })
        elif rule["类型"] in ("字辈", "代码"):
            observed.append(row)
        elif is_window:
            window_cold.append(row)
    # 未触发的常驻字辈：只保留超额最高的 N 行作「观察」，让人看见今日最强的字辈是谁
    if standing_n > 0:
        observed.sort(key=lambda x: -(x["超额%"] or -999))
        concepts.extend(observed[:standing_n])
        if len(observed) > standing_n:
            notes.append("特殊概念·其余字辈本次未列示：" + "、".join(
                f"{o['概念']} {o['超额%']:+.2f}" for o in observed[standing_n:]))
    # 窗口类但无热度的（如全年在窗的生肖）：不占表格，合并成一行 notes，避免刷屏
    if window_cold:
        window_cold.sort(key=lambda x: -(x["宽度%"] or -999))
        notes.append("特殊概念·窗口内但无热度的概念（未列入表格）：" + "、".join(
            f"{o['概念']}（均涨{o['均涨幅%']}% 宽度{o['宽度%']}pp）" for o in window_cold))
    # 排序：正式触发 > 异动(少数冲板) > 观察；组内按涨停数、再看超额
    def _rank(x):
        return (2 if x.get("触发") else (1 if x.get("异动") else 0)) * -1
    concepts.sort(key=lambda x: (_rank(x), -x["涨停"], -(x["超额%"] or -999)))
    if len(stocks) > 200:   # 明细总量闸门，避免前端口径过载
        stocks = sorted(stocks, key=lambda s: -(s["涨跌幅%"] or 0))[:200]
    if not concepts:
        notes.append(f"特殊概念：本次窗口内无概念达到展示门槛（命中≥{min_hits}）")
    cur_z, nxt_z, days_next, next_date, cur_start = _zodiac_now(today)
    return {"date": today.strftime("%Y-%m-%d"), "src": src, "concepts": concepts,
            "stocks": stocks, "market_avg": _r(mkt_avg, 2), "surge": surge,
            "market_lu%": _r(mkt_lu, 2), "market_up%": _r(up_ratio_mkt * 100, 1),
            "zodiac": {"当前": cur_z, "下一": nxt_z, "距春节(天)": days_next,
                       "春节日期": next_date.strftime("%Y-%m-%d")}}



# ==================================================================
# 四、信号合成（埋伏 / 逃离 / 趋势 三类）
# ==================================================================
def _synthesize(out: dict) -> list:
    """把各模块结论合成为「动作导向」的信号表。

    定位（避免与各模块表格重复）：**本表只回答「现在该做什么」**，字段压缩为
    对象 / 动作 / 时效 / 来源模块 + 一行最关键的量化依据。各模块表格负责
    「为什么」与全部明细 —— 因此这里不再复述整段口径描述。
    """
    sig = []

    def add(kind, obj, act, ttl, why, src):
        if not obj:
            return
        sig.append({"类型": kind, "对象": str(obj), "动作": act, "时效": ttl,
                    "依据": why, "来源": src})

    cal = out.get("calendar") or {}
    for w in cal.get("埋伏窗口", []):
        add("埋伏", w["主题"], "分批布局", "事件前完成布局",
            f"{w['关联事件']}前 {w['距事件(天)']} 天（窗口 {w['规律窗口']}）", "日历预警")
    for s in out.get("stealth", []):
        add("埋伏", s["板块"], "小仓位跟随并设止损", "3-10 天",
            f"5日涨{s['5日涨幅%']}% · 净流入{s['5日主力净流入(亿)']}亿 · 换手未放量", "异动侦测")
    for s in out.get("selling", []):
        add("逃离", s["板块"], "回避追高，持仓分批止盈", "1-2 周",
            f"5日涨{s['5日涨幅%']}% 已高位放量 · 主力净流出{abs(s['5日主力净流入(亿)'] or 0)}亿",
            "异动侦测")
    for s in out.get("crowding", []):
        add("逃离", s["板块"], "分批止盈，保留底仓", "1-2 周内执行",
            f"拥挤度 {s['触发项数']} 项触发（换手/量能/涨幅/资金流出）", "拥挤度监控")
    for s in out.get("cold", []):
        add("趋势", s["板块"], "关注政策催化与均值回归，低位埋伏", "1-3 个月",
            f"冷度得分 {s['冷度得分']}（成交占比/换手/涨幅均处尾部）", "冷门板块")
    for ch in out.get("chains", []):
        if ch["环节"] == "上游" and (ch["5日涨幅%"] or -99) > 3:
            chain = next((x for x in TRANSMIT_CHAINS if x["上游"] == ch["传导链"]), None)
            mid = "、".join(chain.get("中游板块", [])) if chain else ""
            add("趋势", f"{ch['板块']}（上游）", "关注中游补涨与业绩兑现时滞", "1-3 个月",
                f"上游 5 日涨 {ch['5日涨幅%']}%，向中游传导（{mid}）", "传导链")
    # 特殊概念：仅「触发」的概念成信号；「异动」（少数冲板）与「观察」行只作表格观察项。
    sp = out.get("special") or {}
    mkt = sp.get("market_avg")
    for s in [c for c in (sp.get("concepts") or []) if c.get("触发")][:12]:
        add("概念", s["概念"], "纯情绪题材：只做辨识度最高的龙头，追高极易接力站岗",
            "1-5 天（来得快、去得快）",
            f"命中 {s['命中']} 只（上涨 {s['上涨']}、涨停 {s['涨停']}），"
            f"均涨 {s['均涨幅%']}% vs 全市场 {mkt}%（超额 {s['超额%']}pp）· 最强 {s['最强']}",
            "特殊概念")
    # 去重：同一「类型+对象」（大小写/全半角归一）只留第一条，避免日历与埋伏窗口、
    # 异动与拥挤度对同一板块各出一条，造成信号表自相重复。
    seen, uniq = set(), []
    for s in sig:
        key = (s["类型"], s["对象"].replace("（", "(").replace("）", ")").strip())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    # 排序：先按类型（埋伏→逃离→趋势→概念），同类型保持合成顺序（已按各模块优先级）
    order = {"埋伏": 0, "逃离": 1, "趋势": 2, "概念": 3}
    uniq.sort(key=lambda s: order.get(s["类型"], 9))
    return uniq


# ==================================================================
# 五、主调度 scan()
# ==================================================================
def scan(cfg=None, sections=None, progress_cb: ProgressCb = None,
         logs: Optional[list] = None) -> dict:
    t0 = _dt.datetime.now()
    c = _merge_cfg(cfg)
    sections = [s for s in (sections or SECTIONS) if s in SECTIONS]
    notes: list = []
    if logs is not None:
        logs.append(f"前瞻预警启动：模块={','.join(sections)}")

    out = {"date": _dt.date.today().strftime("%Y-%m-%d"),
           "sections": sections, "notes": notes,
           "calendar": None, "stealth": [], "selling": [], "cold": [],
           "national": [], "foreign": [], "crowding": [],
           "chains": [], "macro": [], "special": None, "signals": [], "elapsed": 0.0}

    def prog(a, b, msg):
        if progress_cb:
            progress_cb(a, b, msg)

    total_steps = len(sections) + 1
    step = 0

    # --- 日历预警（纯本地逻辑，不联网） ---
    if "calendar" in sections:
        prog(step, total_steps, "日历预警与埋伏窗口…")
        out["calendar"] = get_calendar_alerts(c["calendar"])
        step += 1

    # --- 需要板块级行情/资金流的模块 ---
    need_flow = any(s in sections for s in ("stealth", "cold", "crowding", "chains"))
    need_all_hist = any(s in sections for s in ("cold", "crowding"))
    merged = None
    hist_all: dict = {}

    hist_broken = {"n": 0}

    def hist_get(name, days=260):
        if name in hist_all:
            return hist_all[name]
        try:
            hist_all[name] = _sector_hist(name, days)
        except Exception as e:  # noqa: BLE001 —— 单板块失败不影响整体
            if hist_broken["n"] < 2:  # 笔记上限，避免刷屏
                notes.append(f"{name} 板块历史获取失败：{str(e)[:80]}")
            hist_broken["n"] += 1
            hist_all[name] = None
        return hist_all[name]

    if need_flow:
        prog(step, total_steps, "加载行业板块行情与资金流…")
        try:
            merged = _merge_basic_flow(_industry_basic(), _sector_flow())
        except Exception as e:  # noqa: BLE001 —— 行情/资金流失败不拖垮整次预警
            notes.append(f"板块行情/资金流获取失败（联网）：{str(e)[:120]}")
        step += 1

    if need_all_hist:
        if merged is None:
            notes.append("板块行情不可用，板块历史未拉取（冷门/拥挤度将无结果）")
        else:
            names = merged["板块名称"].tolist()
            days = max(260, int(c["cold"]["window"]) + 10)
            for i, nm in enumerate(names):
                prog(step * 1.0 + i / max(len(names), 1), total_steps,
                     f"板块历史 {nm} ({i + 1}/{len(names)})")
                hist_get(nm, days)

    # --- 异动侦测 ---
    if "stealth" in sections and merged is not None:
        prog(step, total_steps, "异动板块侦测…")
        out["stealth"], out["selling"] = _stealth_scan(merged, c, hist_get, notes)
        step += 1

    # --- 冷门板块 ---
    if "cold" in sections:
        prog(step, total_steps, "冷门板块计算…")
        out["cold"] = _cold_scan(hist_all, c, notes)
        step += 1

    # --- 国家队资金（最新报告期前十大股东名单口径，四列表可选） ---
    if "national" in sections:
        out["national"] = _national_holders(c, prog, step, total_steps, notes)
        step += 1

    # --- 外资重仓 ---
    if "foreign" in sections:
        prog(step, total_steps, "外资重仓查询…")
        out["foreign"] = _foreign_top(c, notes)
        step += 1

    # --- 拥挤度 ---
    if "crowding" in sections:
        prog(step, total_steps, "板块拥挤度监控…")
        out["crowding"] = _crowding_scan(hist_all, merged or pd.DataFrame(), c)
        step += 1

    # --- 产业链传导（需要板块行情）与 宏观雷达（纯新闻驱动）**独立开关** ---
    if "chains" in sections:
        prog(step, total_steps, "产业链传导雷达…")
        if merged is None:
            notes.append("板块行情不可用，产业链传导实时进展已跳过")
        else:
            out["chains"] = _chain_radar(merged, c)
        step += 1

    if "macro" in sections:
        # 宏观雷达 v3：主题归类扫描 + 方向信号（纯新闻驱动，无静态知识卡）
        # ⚠ 与 chains 完全独立：不依赖板块行情 merged，不受其失败影响
        prog(step, total_steps, "宏观雷达（快讯主题扫描）…")
        narrative, sig_rows = _macro_radar()
        out["macro_narrative"] = narrative
        out["macro"] = sig_rows
        step += 1

    # --- 特殊概念（名字玄学 / 谐音梗 / 生肖字辈；独立于板块链路） ---
    if "special" in sections:
        prog(step, total_steps, "特殊概念扫描（名字玄学/生肖字辈）…")
        out["special"] = _special_scan(c, notes)
        step += 1

    # --- 信号合成（永远执行，基于已采集模块） ---
    prog(total_steps, total_steps, "信号合成…")
    out["signals"] = _synthesize(out)
    out["elapsed"] = round((_dt.datetime.now() - t0).total_seconds(), 1)
    if logs is not None:
        logs.append(f"前瞻预警完成：信号 {len(out['signals'])} 条，耗时 {out['elapsed']}s")
    return out


# ==================================================================
# 六、独立网页报告（2026 重制版：锚点导航 + 统计卡 + 徽章信号 + 参数附录 + 打印样式）
# ==================================================================
_CSS = """
  :root { --bg:#0b0f14; --bg2:#101722; --card:#121924; --card2:#0e141d; --line:#1e2a3a;
          --ink:#dbe4ee; --mut:#7d8ca0; --gold:#d4af37; --red:#e35d5d;
          --green:#22a06b; --blue:#4d9fff; --amber:#e0a34a; }
  * { margin:0; box-sizing:border-box; }
  html { scroll-behavior:smooth; }
  body { background:linear-gradient(160deg,var(--bg) 55%,var(--bg2)); color:var(--ink);
         font-family:"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
         padding:26px 20px 60px; }
  .wrap { max-width:1180px; margin:0 auto; }
  a { color:var(--blue); text-decoration:none; } a:hover { text-decoration:underline; }
  .hero { text-align:center; padding:24px 16px 14px; }
  .hero h1 { font-size:26px; letter-spacing:1px;
             background:linear-gradient(90deg,#f0b429,#e35d5d);
             -webkit-background-clip:text; background-clip:text; color:transparent; }
  .hero .sub { color:var(--mut); font-size:12.5px; margin-top:8px; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; justify-content:center; margin-top:14px; }
  .chip { font-size:12px; padding:4px 12px; border:1px solid var(--line);
          border-radius:999px; background:var(--card2); }
  .chip b { color:var(--gold); }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
           gap:12px; margin:18px 0 4px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:14px;
          padding:14px 16px; text-align:center; }
  .stat .n { font-size:30px; font-weight:700; line-height:1.15; }
  .stat .l { color:var(--mut); font-size:12px; margin-top:4px; }
  .stat.red .n{color:var(--red)} .stat.green .n{color:var(--green)}
  .stat.gold .n{color:var(--gold)} .stat.blue .n{color:var(--blue)}
  .stat.amber .n{color:var(--amber)}
  .rail { position:sticky; top:0; z-index:9; display:flex; flex-wrap:wrap; gap:6px;
          justify-content:center; padding:10px 8px; margin:14px 0 0;
          background:rgba(11,15,20,.88); backdrop-filter:blur(8px);
          border:1px solid var(--line); border-radius:12px; }
  .rail a { font-size:12px; color:var(--mut); padding:4px 11px;
            border-radius:999px; border:1px solid transparent; }
  .rail a:hover { color:var(--ink); border-color:var(--line); text-decoration:none; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(460px,1fr));
          gap:14px; margin-top:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px;
          padding:16px 18px; box-shadow:0 6px 24px rgba(0,0,0,.35); scroll-margin-top:76px; }
  .card.wide { grid-column:1 / -1; }
  .card h2 { font-size:15px; color:var(--gold); border-left:3px solid var(--gold);
             padding-left:9px; margin-bottom:12px; }
  .card h2 small { color:var(--mut); font-weight:400; font-size:11.5px; margin-left:8px; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th,td { padding:7px 9px; border-bottom:1px solid #1a2432; text-align:left; }
  th { color:var(--mut); font-weight:600; font-size:11.5px; white-space:nowrap; }
  tbody tr:nth-child(even) { background:#131b27; }
  tbody tr:hover { background:#172231; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  td.w { white-space:normal; line-height:1.55; }
  .up{color:var(--red)} .down{color:var(--green)}
  .muted{color:var(--mut); font-size:12px;}
  .empty { color:var(--mut); font-size:12.5px; text-align:center; padding:16px 0; }
  .sig { padding:10px 12px; margin:7px 0; background:var(--card2); border-radius:10px;
         border-left:3px solid var(--line); font-size:13px; line-height:1.6; }
  .sig.b { border-left-color:var(--red); } .sig.s { border-left-color:var(--amber); }
  .sig.t { border-left-color:var(--blue); } .sig.c { border-left-color:#a970ff; }
  .sig small { color:var(--mut); display:block; margin-top:3px; }
  .tag { display:inline-block; font-size:11px; padding:1.5px 9px; border-radius:999px;
         margin-right:8px; vertical-align:1px; }
  .tag.b{background:#3a2020;color:#ff9b8a} .tag.s{background:#3a3120;color:var(--amber)}
  .tag.t{background:#1c2f45;color:#7db5ff} .tag.c{background:#2a2140;color:#c0a2ff}
  .ttl { display:inline-block; font-size:11px; padding:1px 8px; border-radius:6px;
         margin-left:8px; background:#182130; color:var(--mut); }
  .notes { margin-top:16px; padding:12px 16px; border:1px dashed #4a3b1e;
           border-radius:12px; background:#161408; color:var(--amber);
           font-size:12.5px; line-height:1.7; }
  .params { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
            gap:6px 20px; font-size:12.5px; }
  .params div { display:flex; justify-content:space-between;
                border-bottom:1px dashed #1a2432; padding:4px 2px; }
  .params span { color:var(--mut); }
  .params b { font-variant-numeric:tabular-nums; }
  .top-btn { position:fixed; right:22px; bottom:24px; width:40px; height:40px;
             border-radius:50%; background:var(--card); border:1px solid var(--line);
             color:var(--gold); font-size:18px; cursor:pointer; }
  footer { text-align:center; color:var(--mut); font-size:11.5px; margin-top:26px; }
  @media print {
    body { background:#fff; color:#111; padding:0; }
    .rail,.top-btn { display:none; }
    .card,.stat { border-color:#ddd; background:#fff; box-shadow:none; break-inside:avoid; }
    .hero h1 { color:#b8860b; -webkit-text-fill-color:initial; background:none; }
    tbody tr:nth-child(even){background:#f6f6f6} tbody tr:hover{background:none}
    td,th{border-color:#eee} .sig{background:#fafafa} .notes{background:#fffdf2;color:#8a6d1a}
  }
"""


def _esc(v) -> str:
    return _html.escape(str(v if v is not None else "—"))


def _cls(g) -> str:
    v = _f(g)
    if v is None:
        return ""
    return "up" if v > 0 else ("down" if v < 0 else "")


def _table(headers, rows, pct_cols=(), wide_cols=()):
    """rows: list[list[值]]；pct_cols 索引列按正负红绿着色并右对齐；wide_cols 允许换行。"""
    h = "".join(f"<th>{_esc(x)}</th>" for x in headers)
    body = []
    for r in rows:
        tds = []
        for i, v in enumerate(r):
            if i in pct_cols:
                tds.append(f'<td class="{_cls(v)} num">{_esc(v)}</td>')
            elif i in wide_cols:
                tds.append('<td class="w">' + _esc(v) + "</td>")
            else:
                tds.append(f"<td>{_esc(v)}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    if not body:
        body.append('<tr><td colspan="%d" class="muted">— 暂无数据 —</td></tr>' % max(len(headers), 1))
    return f"<table><thead><tr>{h}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _sig_item(s: dict) -> str:
    t = s.get("类型")
    k = "b" if t == "埋伏" else ("s" if t == "逃离" else ("c" if t == "概念" else "t"))
    src = s.get("来源")
    src_tag = f"<span class='ttl'>来源：{_esc(src)}</span>" if src else ""
    return (f"<div class='sig {k}'><span class='tag {k}'>{_esc(t)}</span>"
            f"<b>{_esc(s.get('对象'))}</b> — {_esc(s.get('动作'))}"
            f"<span class='ttl'>时效：{_esc(s.get('时效'))}</span>{src_tag}"
            f"<small>{_esc(s.get('依据'))}</small></div>")


def build_report_html(result: dict, cfg: Optional[dict] = None) -> str:
    """把 scan() 结果渲染成独立网页报告（2026 重制版，涨红跌绿，零依赖可打印）。"""
    res = result or {}
    date = res.get("date") or _dt.date.today().strftime("%Y-%m-%d")
    now = _dt.datetime.now().strftime("%H:%M")
    cal = res.get("calendar") or {}
    themes = [t for t in (cal.get("本月主线") or []) if t]
    sigs = res.get("signals") or []
    n_b = sum(1 for s in sigs if s.get("类型") == "埋伏")
    n_s = sum(1 for s in sigs if s.get("类型") == "逃离")
    n_t = len(sigs) - n_b - n_s
    sections = res.get("sections") or []
    has = lambda k: (k in sections) if sections else True  # noqa: E731

    rail = [("信号", "signals", True), ("日历", "calendar", has("calendar")),
            ("埋伏窗口", "window", has("calendar")), ("异动侦测", "stealth", has("stealth")),
            ("冷门板块", "cold", has("cold")), ("国家队", "national", has("national")),
            ("外资重仓", "foreign", has("foreign")), ("拥挤度", "crowding", has("crowding")),
            ("产业链", "chains", has("chains")), ("宏观雷达", "macro", has("macro")),
            ("特殊概念", "special", has("special")),
            ("参数附录", "params", True)]
    rail_html = "".join(f"<a href='#{i}'>{t}</a>" for t, i, ok in rail if ok)

    chips = []
    if themes:
        chips.append(f"<span class='chip'>📅 本月主线：<b>{_esc(' / '.join(themes))}</b></span>")
    chips.append(f"<span class='chip'>🕒 生成于 {date} {now}</span>")
    if res.get("elapsed") is not None:
        chips.append(f"<span class='chip'>⏱ 耗时 <b>{_esc(res.get('elapsed'))}s</b></span>")
    chips.append(f"<span class='chip'>🧩 扫描模块 <b>{len(sections) or len(SECTIONS)}</b> 个</span>")

    parts = [
        "<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\">",
        f"<title>A股前瞻预警报告 · {date}</title>",
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<style>{_CSS}</style></head><body><div class='wrap'>",
        "<div class='hero'><h1>⏰ A股前瞻预警报告</h1>",
        "<div class='sub'>规律引擎 + 四重领先机制（日历预热 / 传导链 / 资金领先 / 拥挤度）"
        " · 仅供研究参考，不构成投资建议</div>",
        "<div class='chips'>" + "".join(chips) + "</div></div>",
        "<div class='stats'>",
        f"<div class='stat gold'><div class='n'>{len(sigs)}</div><div class='l'>核心信号</div></div>",
        f"<div class='stat red'><div class='n'>{n_b}</div><div class='l'>埋伏</div></div>",
        f"<div class='stat amber'><div class='n'>{n_s}</div><div class='l'>逃离</div></div>",
        f"<div class='stat blue'><div class='n'>{n_t}</div><div class='l'>趋势 / 概念</div></div>",
        "</div>",
        f"<nav class='rail'>{rail_html}</nav>",
        "<div class='grid'>",
    ]

    # 核心信号（通栏）
    items = "".join(_sig_item(s) for s in sigs) or "<div class='empty'>— 本轮无核心信号 —</div>"
    parts.append("<div class='card wide' id='signals'><h2>🎯 核心信号"
                 "<small>埋伏 / 逃离 / 趋势 · 自动合成自各模块</small></h2>" + items + "</div>")

    if has("calendar"):
        rows = [[a["事件"], a["日期"], a["距今(天)"], a["备注"]] for a in cal.get("日历预警", [])]
        parts.append("<div class='card' id='calendar'><h2>📅 日历预警<small>未来 N 天事件预热</small></h2>"
                     + _table(["事件", "日期", "距今(天)", "备注"], rows, pct_cols=(2,), wide_cols=(3,)) + "</div>")
        rows = [[w["主题"], w["关联事件"], w["事件日期"], w["距事件(天)"], w["规律窗口"]]
                for w in cal.get("埋伏窗口", [])]
        parts.append("<div class='card' id='window'><h2>🪟 规律埋伏窗口<small>核心反马后炮机制</small></h2>"
                     + _table(["主题", "关联事件", "事件日", "距事件(天)", "窗口"], rows, pct_cols=(3,)) + "</div>")

    if has("stealth"):
        rows = [[s["板块"], s["5日涨幅%"], s["5日主力净流入(亿)"],
                 "—" if s["换手分位%"] is None else s["换手分位%"], "埋伏嫌疑"]
                for s in res.get("stealth", [])]
        rows += [[s["板块"], s["5日涨幅%"], s["5日主力净流入(亿)"],
                  "—" if s["换手分位%"] is None else s["换手分位%"], "出货嫌疑"]
                 for s in res.get("selling", [])]
        parts.append("<div class='card' id='stealth'><h2>🕵️ 异动侦测<small>资金先动 · 价格未燥</small></h2>"
                     + _table(["板块", "5日涨幅%", "主力净流入(亿)", "换手分位%", "类型"], rows,
                              pct_cols=(1, 2)) + "</div>")

    if has("cold"):
        rows = [[s["板块"], s["冷度得分"], s["成交额占比%"], s["窗口换手%"], s["提示"]]
                for s in res.get("cold", [])]
        parts.append("<div class='card' id='cold'><h2>🧊 长期最冷门板块<small>均值回归 + 政策催化观察</small></h2>"
                     + _table(["板块", "冷度", "额占比%", "换手%", "提示"], rows, wide_cols=(4,)) + "</div>")

    if has("national"):
        nat = res.get("national") or {}
        sections_html = []
        titles = {"etf": "🏛️ ETF 持仓（国家队宽基）", "huijin": "💰 中央汇金",
                  "ssf": "🧧 社保基金", "zhengjin": "🛡️ 中国证金"}
        if isinstance(nat, dict) and nat.get("groups"):
            for k in ("etf", "huijin", "ssf", "zhengjin"):
                g = nat["groups"].get(k) or {}
                rows = g.get("rows") or []
                if not rows:
                    continue
                headers = list(rows[0].keys())
                trs = [[r.get(h) for h in headers] for r in rows]
                sections_html.append(
                    f"<div style='margin:14px 0 6px;font-weight:700;color:#d8c9a3'>{titles[k]} · {len(rows)} 条</div>"
                    + _table(headers, trs, wide_cols=(2,) if k != "etf" else (4,)))
        if sections_html:
            parts.append("<div class='card wide' id='national'><h2>🏛️ 国家队资金流向<small>"
                         + str(nat.get("date") or "") + " 报告期 · 前十大股东名单口径 · 持股≥1000万股</small></h2>"
                         + "".join(sections_html) + "</div>")

    if has("foreign"):
        rows = [[s["名称"], s["代码"], s["持股市值(亿)"], s["持股占比%"],
                 f"<a href='https://stockpage.10jqka.com.cn/{s['代码']}/' target='_blank'>同花顺 ↗</a>"]
                for s in res.get("foreign", [])]
        parts.append("<div class='card' id='foreign'><h2>🌍 外资（北向）重仓<small>持股市值 Top N</small></h2>"
                     + _table(["名称", "代码", "市值(亿)", "占比%", "链接"], rows) + "</div>")

    if has("crowding"):
        rows = [[s["板块"], s["触发项数"], s["换手分位%"], s["量能分位%"],
                 s["20日涨幅%"], s["5日主力净流入(亿)"], s["判定"]]
                for s in res.get("crowding", [])]
        parts.append("<div class='card wide' id='crowding'><h2>🔥 拥挤度监控<small>高点逃离核心</small></h2>"
                     + _table(["板块", "触发", "换手分位%", "量能分位%", "20日涨幅%", "5日净流入(亿)", "判定"],
                              rows, pct_cols=(4, 5), wide_cols=(6,)) + "</div>")

    if has("chains"):
        rows = [[s["传导链"], s["环节"], s["板块"], s["5日涨幅%"], s["5日主力净流入(亿)"], s["逻辑"]]
                for s in res.get("chains", [])]
        parts.append("<div class='card wide' id='chains'><h2>⛓️ 产业链传导雷达<small>上游涨价 → 中游滞后受益</small></h2>"
                     + _table(["链", "环节", "板块", "5日涨幅%", "5日净流入(亿)", "逻辑"],
                              rows, pct_cols=(3, 4), wide_cols=(5,)) + "</div>")

    if has("macro"):
        # 宏观雷达与产业链传导是**独立模块**（各自可单独选入/踢出），报告里也分开渲染
        mrows = [[m.get("类型", ""), m.get("触发", ""), m.get("细节", ""), m.get("方向", ""), m.get("应对", "")]
                 for m in res.get("macro", [])]
        narr = res.get("macro_narrative") or ""
        parts.append("<div class='card wide' id='macro'><h2>🧭 宏观雷达<small>主题归类扫描 + 方向信号（纯新闻驱动 · 无回波不显示 · 非利率预测）</small></h2>"
                     + (f"<div style='margin:0 0 8px;font-size:12px;color:#d8c9a3'>🧭 {_esc(narr)}</div>" if narr else "")
                     + _table(["类型", "主题/信号", "细节", "方向", "应对"], mrows, wide_cols=(2, 4)) + "</div>")

    if has("special"):
        sp = res.get("special") or {}
        zoo = sp.get("zodiac") or {}
        head = (f"数据源：{sp.get('src') or '—'} · 全市场均涨 {sp.get('market_avg')}%"
                f"、上涨占比 {sp.get('market_up%')}%、涨停率 {sp.get('market_lu%')}%"
                if sp.get("src") else "数据源不可用")
        if zoo:
            head += (f" · 当前生肖 {zoo.get('当前')}年，下一生肖 {zoo.get('下一')}年"
                     f"（春节 {zoo.get('春节日期')}，还有 {zoo.get('距春节(天)')} 天）")
        surge_hdr = f"大涨(≥{(sp.get('surge') or 5):g}%)"
        crows = []
        for c in (sp.get("concepts") or []):
            grade = "🔴 触发" if c.get("触发") else ("🟡 异动" if c.get("异动") else "⚪ 观察")
            crows.append([c["概念"], c["类型"], grade, c["命中"], c["上涨"], c["涨停"],
                          c["大涨"], c["均涨幅%"], c["超额%"], c["宽度%"],
                          c["涨停密度×"], c["最强"], c["判定"]])
        srows = [[s["概念"], s["代码"], s["简称"], s["涨跌幅%"], s["最新价"],
                  s["成交额(亿)"], s["换手率%"], s["量比"], s["涨停"]]
                 for s in (sp.get("stocks") or [])]
        lore = "".join(f"<div style='margin:2px 0'><b>{_esc(c['概念'])}</b>：{_esc(c['依据'])}</div>"
                       for c in (sp.get("concepts") or []) if c.get("依据") and c.get("触发"))
        parts.append("<div class='card wide' id='special'><h2>🎋 特殊概念"
                     "<small>名字玄学 / 谐音梗 / 生肖字辈 · 三档判定：🔴触发（跑赢全市场）/ "
                     "🟡异动（少数个股冲板，组内跑输）/ ⚪观察 · 纯情绪题材，非买入建议</small></h2>"
                     + f"<div style='margin:0 0 8px;font-size:12px;color:#d8c9a3'>{_esc(head)}</div>"
                     + _table(["概念", "类型", "档位", "命中", "上涨", "涨停", surge_hdr,
                               "均涨%", "超额%", "宽度%", "涨停密度×", "最强", "判定"],
                              crows, pct_cols=(7, 8, 9), wide_cols=(0, 12))
                     + ("<div style='margin:12px 0 6px;font-weight:700;color:#d8c9a3'>"
                        f"📈 命中个股明细（仅列真在动的票：大涨或量比≥1.5，共 {len(srows)} 条）</div>"
                        + _table(["概念", "代码", "简称", "涨跌幅%", "最新价", "成交额(亿)",
                                  "换手率%", "量比", "状态"], srows, pct_cols=(3,))
                        if srows else "")
                     + (f"<div class='muted' style='margin-top:12px;line-height:1.8'>{lore}</div>" if lore else "")
                     + "</div>")

    # 参数附录（本次运行生效的阈值）
    eff = _merge_cfg(cfg)
    prows = "".join(f"<div><span>{_esc(g + '.' + k)}</span><b>{_esc(v)}</b></div>"
                    for g, kv in eff.items() if isinstance(kv, dict)
                    for k, v in kv.items())
    parts.append("</div><div class='card wide' id='params'><h2>⚙ 本次生效阈值<small>"
                 "所有阈值均可在网页「⚙ 预警参数」窗口调整</small></h2>"
                 f"<div class='params'>{prows}</div></div>")

    if res.get("notes"):
        parts.append("<div class='notes'>📝 数据说明：" + _esc(" ｜ ".join(res["notes"])) + "</div>")
    parts.append("<footer>筛股 · P的初航 — 前瞻预警报告 · 涨红跌绿 · 生成时间 "
                 f"{date} {now}</footer>")
    parts.append("<button class='top-btn' onclick='window.scrollTo({top:0,behavior:\"smooth\"})'"
                 " title='回到顶部'>↑</button>")
    parts.append("</div></body></html>")
    return "".join(parts)
