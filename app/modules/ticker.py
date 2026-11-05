# -*- coding: utf-8 -*-
"""横向新闻轮播条模块 —— 近日财经快讯 + 关键词跳转 + 偶发个股关联 + 小概率大盘行情插入。

轮播条目结构（供前端 marquee 渲染）：
  news   : {type, kws:[{kw,url}] 1~2 个实体关键词徽章, kw/kw_url(兼容字段=第一个), text, url, time}
  stock  : {type, name, code, url}   —— 新闻标题中匹配到本地股票名时按概率插入（同花顺新标签）
  market : {type, label, value, chg, pct} —— 小概率插入 纽约黄金 / 恒生 / 恒生科技 /
            科创50 / 沪深300 / 中证消费 之一的现值与涨跌幅

关键词跳转分流：上市公司名 → 同花顺个股页；伦敦金/布伦特原油 → 问财行情；
其余关键词（动词/专有名词）→ B 站搜索 search.bilibili.com。

数据源级联：东财全球快讯(带链接) → 富途快讯(带链接) → 财联社/新浪(无链接时仅展示)。
个股名表：沪深交易所官方接口全量 A 股，落盘 data/name_map.json 缓存 7 天（后台线程重建，
首次不阻塞请求）。行情：腾讯 qt.gtimg.cn 批量快照（GBK 文本，实测稳定）。
结果缓存 5 分钟，避免频繁打外部接口。
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from urllib.parse import quote

from .alert import _UA, _ak, _ensure_ua, _net_call

DEFAULTS: dict = {
    "limit": 18,          # 轮播消息条数
    "stock_prob": 0.35,   # 命中股票名时插入个股卡片的概率
    "market_prob": 0.40,  # 每次刷新插入大盘行情卡片的概率
    "cache_ttl": 300,     # 结果缓存秒数
}

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_NAME_MAP_PATH = os.path.join(_ROOT, "data", "name_map.json")

_CACHE = {"ts": 0.0, "data": None}
_LOCK = threading.Lock()
_NAME_BUILDING = {"v": False}

# 关键词表（≤5 字；标题命中即作为可点击关键词；构建时按长度降序做最长匹配）
KEYWORDS = [
    "美联储", "国务院", "证监会", "财政部", "发改委", "国资委", "中央银行", "央行",
    "降息", "加息", "降准", "LPR", "MLF", "逆回购", "国债", "专项债", "地方债",
    "汇率", "人民币", "离岸人民币", "北向", "外资", "两融", "融资融券", "大宗交易",
    "回购", "增持", "减持", "解禁", "股权激励", "熔断",
    "中报", "年报", "季报", "业绩预告", "预增", "预亏", "涨停", "跌停", "连板", "龙虎榜",
    "半导体", "芯片", "光模块", "存储", "AI", "算力", "机器人", "固态电池", "智能驾驶",
    "新能源", "光伏", "储能", "锂电", "电网", "核电", "氢能",
    "军工", "商业航天", "卫星", "低空经济", "创新药", "医药", "中药", "白酒", "消费",
    "旅游", "免税", "房地产", "基建", "建材", "钢铁", "煤炭", "有色", "稀土",
    "黄金", "白银", "原油", "油气", "天然气", "航运", "港口", "物流",
    "数据要素", "数字经济", "跨境电商", "外贸",
    "ETF", "IPO", "再融资", "注册制", "分红", "股息", "纳入MSCI", "富时罗素",
    "美股", "港股", "纳指", "标普", "日经", "欧股",
    "关税", "贸易战", "出口", "进口", "制裁", "地缘", "台风", "地震", "飓风",
    "牛市", "熊市", "反转", "底部", "顶部", "放量", "缩量", "背离", "估值",
]
_KW_SORTED = sorted({k for k in KEYWORDS if 1 <= len(k) <= 5}, key=len, reverse=True)

# 大盘行情池（腾讯代码 + 解析类型）
_MARKET_POOL = [
    {"key": "gold",     "label": "纽约黄金", "tencent": "hf_GC",      "type": "hf"},
    {"key": "hsi",      "label": "恒生指数", "tencent": "s_hkHSI",    "type": "s"},
    {"key": "hstech",   "label": "恒生科技", "tencent": "s_hkHSTECH", "type": "s"},
    {"key": "kc50",     "label": "科创50",   "tencent": "s_sh000688", "type": "s"},
    {"key": "hs300",    "label": "沪深300",  "tencent": "s_sh000300", "type": "s"},
    {"key": "consumer", "label": "中证消费", "tencent": "s_sh000932", "type": "s"},
]


# ============ 行情快照（腾讯批量） ============
_SESSION = None


def _sess():
    """模块级共享 Session：复用 TLS 连接，省去每次握手开销。"""
    global _SESSION
    if _SESSION is None:
        import requests
        _SESSION = requests.Session()
    return _SESSION


def _market_quotes() -> dict:
    """一次批量拉全部行情池：{key: {label, value, chg, pct}}；失败返回 {}。"""
    codes = ",".join(m["tencent"] for m in _MARKET_POOL)

    def fetch():
        r = _sess().get(f"https://qt.gtimg.cn/q={codes}",
                        headers={"User-Agent": _UA}, timeout=8)
        r.encoding = "gbk"
        return r.text

    try:
        text = _net_call(fetch)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for m in _MARKET_POOL:
        try:
            mm = re.search(rf'v_{re.escape(m["tencent"])}="([^"]*)"', text)
            if not mm:
                continue
            raw = mm.group(1)
            if m["type"] == "s":
                f = raw.split("~")
                out[m["key"]] = {"label": m["label"], "value": f[3],
                                 "chg": f[4], "pct": f[5]}
            else:  # hf_ 期货格式（逗号分隔）：[0]现价 [1]涨跌幅% [7]昨结
                f = raw.split(",")
                price, pct, prev = float(f[0]), float(f[1]), float(f[7])
                out[m["key"]] = {"label": m["label"], "value": f[0], "chg": f"{price - prev:.2f}",
                                 "pct": f"{pct:.2f}"}
        except (ValueError, IndexError):
            continue
    return out


# ============ CCI 市场情绪（上证指数日K → CCI(20)，常驻轮播） ============
_CCI_CACHE = {"ts": 0.0, "data": None}
_CCI_TTL = 600        # 成功结果缓存 10 分钟（盘中当日 CCI 随指数变动）
_CCI_COOLDOWN = 60    # 拉取失败后 60 秒内不重试（避免每次请求都打外部接口）


def _calc_cci(rows: list[tuple], n: int = 20) -> dict | None:
    """rows: [(date, open, close, high, low)] 按日期升序 → CCI(20) 条目。

    CCI = (TP - MA(TP)) / (0.015 × MD)，TP=(H+L+C)/3。
    多空口径：>100 多头强势 / 0~100 偏多 / -100~0 偏空 / <-100 空头强势；
    |CCI|>100 触发预警标记（前端标红）。
    """
    if len(rows) < n:
        return None
    tp = [(h + l + c) / 3.0 for (_, _o, c, h, l) in rows]
    window = tp[-n:]
    ma = sum(window) / n
    md = sum(abs(x - ma) for x in window) / n
    if md <= 0:
        return None
    cci = (window[-1] - ma) / (0.015 * md)
    if cci > 100:
        trend = "多头强势"
    elif cci > 0:
        trend = "偏多"
    elif cci > -100:
        trend = "偏空"
    else:
        trend = "空头强势"
    return {"type": "cci", "value": round(cci, 1), "trend": trend,
            "warn": abs(cci) > 100, "date": str(rows[-1][0])}


def _cci_item() -> dict | None:
    """上证指数 CCI(20)：腾讯日K → akshare 新浪日线 两级降级；失败返回 None（轮播静默跳过）。"""
    now = time.time()
    c = _CCI_CACHE
    if c["data"] and now - c["ts"] < _CCI_TTL:
        return c["data"]
    if c["data"] is None and c["ts"] and now - c["ts"] < _CCI_COOLDOWN:
        return None  # 刚失败过，冷却中

    def _rows_from_tencent():
        def fetch():
            # 【2026-10-19】web.ifzq.gtimg.cn 被腾讯 WAF 拦截（501 页 → JSON
            # 解析失败）；改双入口轮询（ifzq 裸域 / proxy.finance 实测正常）
            qs = "appstock/app/fqkline/get?param=sh000001,day,,,80,qfq"
            last_err = None
            for base in ("https://ifzq.gtimg.cn",
                         "https://proxy.finance.qq.com/ifzqgtimg"):
                try:
                    return _sess().get(
                        f"{base}/{qs}",
                        headers={"User-Agent": _UA}, timeout=8).json()
                except Exception as e:  # noqa: BLE001 —— 换备用入口
                    last_err = e
            raise last_err if last_err else RuntimeError("腾讯指数日K失败")
        j = _net_call(fetch)
        d = (j.get("data") or {}).get("sh000001") or {}
        days = d.get("qfqday") or d.get("day") or []
        return [(str(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                for r in days]

    def _rows_from_ak():
        ak = _ak()
        df = ak.stock_zh_index_daily(symbol="sh000001")
        return [(str(r["date"]), float(r["open"]), float(r["close"]),
                 float(r["high"]), float(r["low"]))
                for _, r in df.tail(80).iterrows()]

    rows = None
    for loader in (_rows_from_tencent, _rows_from_ak):
        try:
            rows = loader()
            if len(rows) >= 20:
                break
            rows = None
        except Exception:  # noqa: BLE001 —— 数据源故障静默降级
            rows = None
    item = _calc_cci(rows) if rows else None
    c.update(ts=now, data=item)
    return item


# ============ 伦敦金/布油 + 期货异动（滚动条增强） ============
_GOLDOIL = [   # 腾讯外盘 hf_ 代码（与 _MARKET_POOL 同源，实测稳定）
    {"tencent": "hf_XAU", "label": "伦敦金",
     "url": "https://www.24k99.com/market/XAU"},
    {"tencent": "hf_OIL", "label": "布伦特原油",
     "url": "https://finance.sina.com.cn/futures/quotes/OIL.shtml"},
]
_GOLDOIL_CACHE = {"ts": 0.0, "data": {}}
_GOLDOIL_TURN = {"i": 0}   # 轮替游标：第一轮伦敦金 → 第二轮布油 → ……（跨缓存周期递进）


def _next_goldoil(gold: dict) -> dict | None:
    """按轮次交替取伦敦金/布油（每次重建轮播条推进一格）。"""
    if not gold:
        return None
    labels = [g["label"] for g in _GOLDOIL]
    item = gold.get(labels[_GOLDOIL_TURN["i"] % len(labels)])
    _GOLDOIL_TURN["i"] += 1
    return item

_FUT_WATCH = {   # 新浪国内期货主连代码 → 中文品种名（活跃品种 27 个）
    "nf_RB0": "螺纹钢", "nf_HC0": "热卷", "nf_I0": "铁矿石", "nf_J0": "焦炭",
    "nf_JM0": "焦煤", "nf_CU0": "沪铜", "nf_AL0": "沪铝", "nf_ZN0": "沪锌",
    "nf_NI0": "沪镍", "nf_SN0": "沪锡", "nf_AU0": "沪金", "nf_AG0": "沪银",
    "nf_LC0": "碳酸锂", "nf_SI0": "工业硅", "nf_SA0": "纯碱", "nf_FG0": "玻璃",
    "nf_SC0": "原油", "nf_MA0": "甲醇", "nf_TA0": "PTA", "nf_PP0": "聚丙烯",
    "nf_UR0": "尿素", "nf_SP0": "纸浆", "nf_RU0": "橡胶", "nf_P0": "棕榈油",
    "nf_M0": "豆粕", "nf_CF0": "棉花", "nf_SR0": "白糖",
}
_FUT_ALERT_CACHE = {"ts": 0.0, "data": []}
_FUT_ALERT_TH = 5.0   # 主连涨跌幅（对昨结）绝对值 ≥5% 视为异动


def _goldoil_quotes() -> dict:
    """伦敦金/布伦特原油快照（5 分钟缓存）。返回 {label: item}，item 含现价/涨跌幅/iwencai 链接。"""
    now = time.time()
    if _GOLDOIL_CACHE["data"] and now - _GOLDOIL_CACHE["ts"] < 300:
        return _GOLDOIL_CACHE["data"]
    codes = ",".join(g["tencent"] for g in _GOLDOIL)

    def fetch():
        r = _sess().get(f"https://qt.gtimg.cn/q={codes}",
                        headers={"User-Agent": _UA}, timeout=8)
        r.encoding = "gbk"
        return r.text

    out: dict = {}
    try:
        text = _net_call(fetch)
    except Exception:  # noqa: BLE001 —— 行情失败轮播静默跳过
        text = ""
    for g in _GOLDOIL:
        try:
            mm = re.search(rf'v_{re.escape(g["tencent"])}="([^"]*)"', text)
            if not mm:
                continue
            f = mm.group(1).split(",")
            out[g["label"]] = {"type": "fut", "label": g["label"], "value": f[0],
                               "pct": f"{float(f[1]):+.2f}", "url": g["url"]}
        except (ValueError, IndexError):
            continue
    _GOLDOIL_CACHE.update(ts=now, data=out)
    return out


def _fut_alert_items() -> list:
    """期货异动扫描：主连涨跌幅（最新价/昨结-1）绝对值 ≥5% → 异动卡（点击跳 iwencai 筛查）。"""
    now = time.time()
    if _FUT_ALERT_CACHE["ts"] and now - _FUT_ALERT_CACHE["ts"] < 300:
        return _FUT_ALERT_CACHE["data"]
    codes = ",".join(_FUT_WATCH)

    def fetch():
        r = _sess().get(f"https://hq.sinajs.cn/list={codes}",
                        headers={"User-Agent": _UA,
                                 "Referer": "https://finance.sina.com.cn"}, timeout=8)
        r.encoding = "gbk"
        return r.text

    alerts: list = []
    try:
        text = _net_call(fetch)
    except Exception:  # noqa: BLE001
        text = ""
    for m in re.finditer(r'hq_str_(nf_\w+)="([^"]*)"', text):
        code, raw = m.group(1), m.group(2).split(",")
        name = _FUT_WATCH.get(code, code)
        try:
            last = float(raw[8])     # 最新价
            prev = float(raw[10])    # 昨结算价
        except (ValueError, IndexError):
            continue
        if last <= 0 or prev <= 0:
            continue
        pct = (last / prev - 1) * 100
        if abs(pct) >= _FUT_ALERT_TH:
            alerts.append({"type": "fut_alert", "label": name, "pct": f"{pct:+.2f}",
                           "url": f"https://www.iwencai.com/screener/result?w={name}"})
    alerts.sort(key=lambda x: -abs(float(x["pct"])))
    _FUT_ALERT_CACHE.update(ts=now, data=alerts)
    return alerts


# ============ 股票名表（交易所官方，落盘缓存） ============
def _name_map(force: bool = False) -> dict:
    now = time.time()
    if not force:
        try:
            with open(_NAME_MAP_PATH, encoding="utf-8") as f:
                j = json.load(f)
            if now - j.get("ts", 0) < 7 * 86400 and j.get("map"):
                return j["map"]
        except Exception:  # noqa: BLE001
            pass
        if _NAME_BUILDING["v"]:  # 已有后台线程在重建
            return {}
        _NAME_BUILDING["v"] = True
    try:
        ak = _ak()
        m: dict = {}
        for sym in ("主板A股", "科创板"):
            try:
                df = ak.stock_info_sh_name_code(symbol=sym)
                for _, r in df.iterrows():
                    m[str(r["证券代码"]).zfill(6)] = str(r["证券简称"]).strip()
            except Exception:  # noqa: BLE001
                pass
        try:
            df = ak.stock_info_sz_name_code(symbol="A股列表")
            for _, r in df.iterrows():
                m[str(r["A股代码"]).zfill(6)] = str(r["A股简称"]).replace(" ", "").strip()
        except Exception:  # noqa: BLE001
            pass
        if len(m) > 3000:
            os.makedirs(os.path.dirname(_NAME_MAP_PATH), exist_ok=True)
            with open(_NAME_MAP_PATH, "w", encoding="utf-8") as f:
                json.dump({"ts": now, "map": m}, f, ensure_ascii=False)
            return m
        return {}
    finally:
        _NAME_BUILDING["v"] = False


# ============ 新闻（级联） ============
def _strip_head(text: str) -> str:
    """去掉快讯开头的【标题】包装。"""
    return re.sub(r"^【[^】]*】\s*", "", text or "").strip()


def _cell(v) -> str:
    """pandas 单元格 → 干净字符串。

    ★ NaN 是 truthy，`str(r["摘要"] or "")` 会得到字符串 "nan"，
      链接列同样会把 "nan" 当成 URL（实测东财偶发空摘要）。
    """
    if v is None:
        return ""
    try:
        if v != v:          # NaN
            return ""
    except Exception:       # noqa: BLE001
        pass
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _news_items() -> list[dict]:
    """近日快讯：东财(200条带链接) → 富途(50条带链接) → 财联社。返回含 title/text/url/time。"""
    ak = _ak()
    items: list[dict] = []
    try:
        df = ak.stock_info_global_em()
        items = [{"title": _cell(r["标题"]),
                  "text": _strip_head(_cell(r["摘要"])) or _cell(r["标题"]),
                  "url": _cell(r["链接"]),
                  "time": _cell(r["发布时间"])[:16]}
                 for _, r in df.iterrows()]
    except Exception:  # noqa: BLE001
        try:
            df = ak.stock_info_global_futu()
            items = [{"title": (_cell(r["标题"]) or _cell(r["内容"])[:24]),
                      "text": _strip_head(_cell(r["内容"]))[:120],
                      "url": _cell(r["链接"]),
                      "time": _cell(r["发布时间"])[:16]}
                     for _, r in df.iterrows()]
        except Exception:  # noqa: BLE001
            try:
                df = ak.stock_info_global_cls()
                items = [{"title": _cell(r["标题"]),
                          "text": _strip_head(_cell(r["内容"])) or _cell(r["标题"]),
                          "url": "",
                          # 财联社分「发布日期 + 发布时间」两列，只取日期会丢掉时分
                          "time": (_cell(r["发布日期"]) + " " +
                                   _cell(r.get("发布时间"))[:5]).strip()}
                         for _, r in df.iterrows()]
            except Exception:  # noqa: BLE001
                return []
    seen, out = set(), []
    for it in items:
        if not it["title"]:
            continue
        k = it["title"][:24]
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


# ============ 关键词跳转链接工具 ============
_BILI_BASE = "https://search.bilibili.com/all?vt=02441088&keyword="
_EM_SEARCH = "https://so.eastmoney.com/news/s?keyword="


def _bili_url(kw: str) -> str:
    """B 站搜索链接（中文关键词需 URL 编码）。"""
    return _BILI_BASE + quote(kw)


def _em_url(kw: str) -> str:
    """东方财富新闻搜索：金融类关键词查资讯比 B 站有用得多。"""
    return _EM_SEARCH + quote(kw)


# 标题自带【】中的泛化分类词（无实体信息量，不做关键词徽章）
_GENERIC_TAGS = {
    "公司", "数据", "个股", "公告", "研报", "热点", "聚焦", "快讯", "行情", "盘面",
    "异动", "资金", "行业", "宏观", "海外", "观点", "时讯", "播报", "解读", "复盘",
    "晨报", "晚报", "午报", "早报", "夜报", "标题", "图说", "风向", "纠错", "提示",
    "重要", "关注", "突发", "重磅", "快看", "直播", "连线", "问答", "答疑", "提醒",
    "风险", "预警", "机会", "掘金", "调研", "纪要", "科普", "专题", "策略", "财经",
    "股市", "证券", "期货", "基金", "全球", "国际", "国内", "中国", "动态", "资讯",
    "速报", "要闻", "必读", "盘中", "收盘", "开盘", "午评", "收评", "日评", "周评",
    # ★ 2026-11-05：实测高频噪声词（做徽章没有检索价值，且会把真实体挤掉）
    "现货", "高达", "主力合约", "美股", "美国", "市场", "消息", "报告", "亿元",
    "美元", "同比", "环比", "记者", "人士", "方面", "情况", "问题", "影响",
    "最新", "本周", "上周", "今年", "去年", "明年", "早盘", "尾盘", "全天",
    "股价", "续刷", "记录", "新高", "新低", "消息", "截止",
}

# 金油词豁免：这些关键词保留问财行情跳转（其余一律 B 站搜索）
_GOLD_OIL_WORDS = {"伦敦金", "布伦特", "原油"}

# 海外/非 A 股重要实体：jieba 会把「英伟达」切成「英/伟达」、「礼来」切成「礼/来」，
# 词性分词拿不到完整专名 → 用一张小词表在分词前直接命中（长词优先）。
# 只收「对 A 股板块有传导」的名字，避免把词表做成第二个新闻源。
_ENTITY_WORDS = sorted([
    "英伟达", "台积电", "特斯拉", "苹果公司", "苹果", "微软", "谷歌", "亚马逊",
    "英特尔", "博通", "超微", "阿斯麦", "礼来", "诺和诺德", "摩根大通", "高盛",
    "摩根士丹利", "伯克希尔", "巴菲特", "马斯克", "黄仁勋", "库克", "OpenAI",
    "高通", "美光", "闪迪", "西部数据", "德州仪器",
    "三星", "SK海力士", "索尼", "丰田", "空客", "波音", "壳牌", "沙特阿美",
], key=len, reverse=True)

# 报道套话（动词性，但作检索关键词无信息量，jieba 提取时过滤）
_STOP_VERBS = {
    "称", "表示", "通报", "获悉", "报道", "宣布", "要求", "显示", "预计", "认为",
    "指出", "提醒", "介绍", "分析", "透露", "消息", "发布", "召开", "举行", "回应",
    "有关", "方面", "今日", "明日", "昨日", "近日", "目前", "目前", "记者", "总台",
    # ★ 行情涨跌动词：做检索词没有意义，会把真实体挤掉（实测「现货钯金最新上涨5%」
    #   被 TF-IDF 兜底成「上涨」）
    "上涨", "下跌", "下挫", "大涨", "大跌", "飙升", "回落", "走高", "走低",
    "反弹", "回调", "收涨", "收跌", "刷新", "创下", "突破", "报收",
    "涨幅", "跌幅", "扩大", "收窄", "延续", "重挫", "跌超", "涨超",
    "续刷记录", "续刷", "创新高", "创新低", "股价",
}

# 虚字过滤：候选词首/尾字命中则丢弃（挡"已使""超65"等分词碎片）
_STOP_HEAD = set("已使未超约逾再又将在于与及或被把从向至对为等之的了是有无不没更最很较且而并若如该此本某各每另共皆均即就也都还太挺颇经则方即必均")
_STOP_TAIL = set("的了等之者性化率值元号称员长总们所续")

# jieba 词性分词：名词性词性集合（参与"连续名词段"判定）
_POSSEG_N_FLAGS = {"n", "nz", "ns", "nt", "nr", "nrt", "ng", "s", "j", "vn", "eng"}
# 词性 → 实体权重（nz 专有名词最高；nr 人名次之；地名/机构再次；普通名词最低）
_KW_POS_W = {"nz": 4.0, "nrt": 3.0, "nr": 3.0, "nt": 1.5, "ns": 1.5,
             "eng": 1.5, "n": 1.0, "s": 1.0, "j": 1.0, "vn": 1.0}


def _kw_push(cands: dict, word: str, weight: float, pos: int) -> None:
    """候选词入池（去重 + 基础过滤：含数字 / 无汉字 / 虚字首尾）。"""
    if word in cands or len(word) < 2:
        return
    if re.search(r"\d", word) or not re.search(r"[\u4e00-\u9fa5]", word):
        return
    if word[0] in _STOP_HEAD or word[-1] in _STOP_TAIL:
        return
    cands[word] = (weight, pos, len(word), word)


def _posseg_candidates(t: str) -> list[tuple]:
    """jieba 词性分词 → 实体候选 [(权重, 起点词元序, 长度, 词)]（降序）。

    三路生成：① 段内单词元（2~5 字）② 整段拼接 ≤5 字（+0.5 整段加成，
    修复"埃/博拉""赛马/季"被切碎的专名）③ 相邻 2 元拼接（两元均 ≥2 字，
    修复"海上/封锁"类动名词组；1 字碎片不参与，挡"伊海上"）。
    排序：词性权重 > 标题位置（前部优先）> 长度。
    """
    import jieba.posseg as pseg
    words = [w for w in pseg.cut(t) if w.word.strip()]
    cands: dict = {}
    i, n = 0, len(words)
    while i < n:
        if words[i].flag in _POSSEG_N_FLAGS:
            j = i
            while j < n and words[j].flag in _POSSEG_N_FLAGS:
                j += 1
            seg = words[i:j]  # 连续名词性词元段
            for k, w in enumerate(seg):  # ① 单词元
                if len(w.word) <= 5:
                    _kw_push(cands, w.word, _KW_POS_W.get(w.flag, 1.0), i + k)
            full = "".join(w.word for w in seg)  # ② 整段拼接（含 1 字碎片时仅限 2 元段，挡"伊海上封锁"）
            if 2 <= len(full) <= 5 and (len(seg) == 2 or all(len(w.word) >= 2 for w in seg)):
                _kw_push(cands, full,
                         max(_KW_POS_W.get(w.flag, 1.0) for w in seg) + 0.5, i)
            for k in range(len(seg) - 1):  # ③ 相邻 2 元拼接
                a, b = seg[k].word, seg[k + 1].word
                fa, fb = seg[k].flag, seg[k + 1].flag
                # ★ 不跨人名拼接：把「研发」+「苏杰」拼成「研发苏杰」这类词，
                #   做徽章既搜不到东西也读不懂（实测自 2026-11-05 东财标题）。
                if fa.startswith("nr") or fb.startswith("nr"):
                    continue
                if len(a) >= 2 and len(b) >= 2 and len(a + b) <= 5:
                    _kw_push(cands, a + b,
                             max(_KW_POS_W.get(fa, 1.0), _KW_POS_W.get(fb, 1.0)), i + k)
            i = j
        else:
            i += 1
    return sorted(cands.values(), key=lambda x: (-x[0], x[1], -x[2]))


def _kw_of(title: str) -> str:
    """标题 → 实体关键词（动词或专有名词，2~5 字）。三级策略：
    ① 关键词表最长匹配（金融专有词优先）
    ② jieba 词性分词实体候选（名词段合并，排序见 _posseg_candidates）
    ③ TF-IDF 标签降级 → 清洗后前 4 字兜底
    """
    t = re.sub(r"^((【[^【】]{1,16}】\s*)+)", "", title or "")
    t = re.sub(r"【(.*?)】", r"\1", t)
    for kw in _KW_SORTED:
        # ★ 关键词表里也有「美股/港股/中国」这类泛化词，命中了也跳过，
        #   否则会盖掉后面的真实体（实测「随着美股下挫，恐慌指数VIX飙升」被标成【美股】）
        if kw in t and kw not in _GENERIC_TAGS:
            return kw
    try:  # jieba 首次 import 约需 1s 建缓存，失败则静默走降级
        for _, _, _, w in _posseg_candidates(t):
            if w not in _STOP_VERBS and w not in _GENERIC_TAGS:
                return w
    except Exception:  # noqa: BLE001
        pass
    try:
        import jieba.analyse as _ja
        tags = _ja.extract_tags(t, topK=5)
        for tag in tags:
            # ★ 这里以前只挡了动词，没挡泛化词：'现货钯金' 的 jieba 词性只有一个
            #   「现货」(n)，被词性分支跳过后 TF-IDF 又把它捞了回来。
            if (2 <= len(tag) <= 6 and not re.search(r"\d", tag)
                    and tag not in _STOP_VERBS and tag not in _GENERIC_TAGS):
                return tag
    except Exception:  # noqa: BLE001
        pass
    t2 = re.sub(r"[\s【】\[\]（）()：:，,。.！!？?\"'“”‘’·—-]", "", t)
    return t2[:4] or "快讯"


def _stock_in(t: str, name_pairs: list[tuple[str, str]]) -> tuple | None:
    """正文股票简称匹配（长名优先），命中返回 (code, name)。"""
    for nm, code in name_pairs:
        if nm in t:
            return (code, nm)
    return None


def _kw_target(kw: str, name_pairs: list[tuple[str, str]]) -> dict:
    """单个关键词 → {kw, url}。

    跳转分流（2026-11-05 调整）：
      上市公司名      → 同花顺个股页
      伦敦金/布伦特/原油 → 问财行情（保留原设计）
      其余金融实体词   → 东方财富新闻搜索（原来一律跳 B 站，查「英伟达」得到的是视频）
      非金融词        → B 站搜索（埃博拉、台风这类仍按学习向处理）
    """
    for nm, code in name_pairs:
        if nm == kw or (len(nm) >= 2 and nm in kw):
            return {"kw": kw, "url": _stock_url(code)}
    if kw in _GOLD_OIL_WORDS:
        return {"kw": kw,
                "url": f"https://www.iwencai.com/screener/result?w={quote(kw)}"}
    if kw in _FIN_KW:
        return {"kw": kw, "url": _em_url(kw)}
    return {"kw": kw, "url": _bili_url(kw)}


# ============ 快讯跳转链接策略（上市公司名 > 指数词 > 机构观点 > 关键词表） ============
_INDEX_WORDS = sorted([   # 指数 / 大类资产词（长词优先，避免"恒生"截胡"恒生科技"）
    "科创50", "沪深300", "中证500", "中证1000", "中证A500", "北证50", "创业板指", "科创板",
    "纳斯达克", "道琼斯", "标普500", "标普", "日经225", "日经", "恒生科技", "恒生指数", "恒生",
    "上证指数", "上证50", "深证成指", "伦敦金", "布伦特", "原油", "黄金", "白银", "碳酸锂",
    "离岸人民币", "人民币汇率", "国债期货", "恐慌指数", "VIX",
    # 海外交易所 / 大宗机构简称（英文缩写，jieba 词性不稳，直接命中）
    "LME", "COMEX", "NYMEX", "OPEC", "欧佩克", "EIA",
], key=len, reverse=True)

# 金融类关键词全集（关键词表 + 指数/大类资产 + 海外实体）→ 跳东财资讯；
# 不在这个集合里的（埃博拉、台风…）继续跳 B 站。
_FIN_KW = set(KEYWORDS) | set(_INDEX_WORDS) | set(_ENTITY_WORDS)

_NAME_SORTED = {"key": 0, "pairs": []}


def _sorted_name_pairs(name_map: dict) -> list[tuple[str, str]]:
    """[(简称, code)] 按简称长度降序（结果缓存；长名优先避免短名误截）。"""
    key = id(name_map)
    if _NAME_SORTED["key"] != key or not _NAME_SORTED["pairs"]:
        _NAME_SORTED["pairs"] = sorted(
            ((nm, code) for code, nm in name_map.items() if 2 <= len(nm) <= 6),
            key=lambda x: -len(x[0]))
        _NAME_SORTED["key"] = key
    return _NAME_SORTED["pairs"]


def _stock_url(code: str) -> str:
    return f"https://stockpage.10jqka.com.cn/{code}/"


def _classify(title: str, name_pairs: list[tuple[str, str]]) -> tuple[list[dict], tuple | None]:
    """快讯标题 → (关键词列表 [{kw,url}] 1~2 个, 个股命中(code,name)|None)。

    关键词只取动词/专有名词（实体），优先级：
    ① 标题自带【实体】前缀（编辑标注的专有名词，可多个，如【欧佩克】【石油】），
       过滤泛化分类词（公司/数据/异动等无信息量词）
    ② 机构观点/评级提取 → 上市公司名 → 指数与金油词 → 词典/jieba 实体词
    跳转分流：股票名 → 同花顺个股页；伦敦金/布伦特原油 → 问财行情；其余 → B 站搜索。
    例：【埃博拉】刚果（金）通报累计确诊 6522 例
        → [{"kw":"埃博拉","url":"https://search.bilibili.com/...keyword=埃博拉"}]"""
    t_all = (title or "").strip()
    t_body = re.sub(r"^((【[^【】]{1,16}】\s*)+)", "", t_all).strip()

    # ① 标题自带【实体】前缀（可多个，最多取 2 个）
    kws: list[str] = []
    for tag in re.findall(r"【([^【】]{1,16})】", t_all):
        tag = tag.strip()
        if len(tag) < 2 or len(tag) > 8:
            continue
        if tag in _GENERIC_TAGS:
            continue
        if re.fullmatch(r"[0-9A-Za-z@#．.]+", tag):
            continue
        if tag not in kws:
            kws.append(tag)
        if len(kws) >= 2:
            break
    if kws:
        return [_kw_target(kw, name_pairs) for kw in kws], _stock_in(t_body, name_pairs)

    # ②a 机构观点：『XX认为/预计…观点串』→ 观点对象做关键词（剥离纯助词）
    m = re.search(r"([\u4e00-\u9fa5A-Za-z]{2,10}(?:证券|银行|基金|资管|高盛|摩根|瑞银|花旗|大摩|小摩|美银|德银|野村|高瓴))"
                  r"[^，。：:]{0,10}?(?:认为|预计|预期|预测|看好|看空|警告|警示|指出|呼吁)[，,：:]?\s*([^。；]{2,20})", t_body)
    if m:
        view = re.sub(r"将会|或将|可能|有望|继续|进一步|已经|正在|大幅", "", m.group(2))
        view = re.sub(r"[，,、].*$", "", view).strip()
        if len(view) >= 2:
            hit = next(((c, nm) for nm, c in name_pairs if nm in view), None)
            if hit:
                return [{"kw": hit[1], "url": _stock_url(hit[0])}], hit
            return [_kw_target(view[:8], name_pairs)], _stock_in(t_body, name_pairs)

    # ②b 机构评级：『提高/上调/下调/维持 (对) XX (的) 评级』→ 被评对象为关键词
    m = re.search(r"(?:提高|上调|下调|维持|重申|首次)(?:对|给予|给)?\s*([\u4e00-\u9fa5A-Za-z0-9]{2,10}?)(?:的)?评级", t_body)
    if m:
        obj = m.group(1)
        hit = next(((c, nm) for nm, c in name_pairs if nm in obj), None)
        if hit:
            return [{"kw": hit[1], "url": _stock_url(hit[0])}], hit
        return [_kw_target(obj, name_pairs)], _stock_in(t_body, name_pairs)

    # ②c 上市公司名（长名优先）
    hit = _stock_in(t_body, name_pairs)
    if hit:
        return [{"kw": hit[1], "url": _stock_url(hit[0])}], hit

    # ②c2 海外/非 A 股重要实体（jieba 会切碎这类专名，先按词表命中）
    for w in _ENTITY_WORDS:
        if w in t_body:
            return [_kw_target(w, name_pairs)], None

    # ②d 指数 / 大类资产词（金融词走东财资讯，金油词走问财行情）
    for w in _INDEX_WORDS:
        if w in t_body:
            return [_kw_target(w, name_pairs)], None

    # ②e 兜底：关键词表 / jieba 实体词
    kw = _kw_of(title)
    return [_kw_target(kw, name_pairs)], None


# ============ 组装 ============
def build_items(cfg: dict | None = None) -> dict:
    c = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if v is not None and k in c:
            c[k] = v
    with _LOCK:
        if _CACHE["data"] and time.time() - _CACHE["ts"] < float(c["cache_ttl"]):
            return _CACHE["data"]

    notes: list = []
    cci = _cci_item()          # CCI 市场情绪（独立于新闻源，新闻挂了也照常展示）
    gold = _goldoil_quotes()   # 伦敦金/布伦特原油（首位轮替，每 7 则插播一次）
    fut_alerts = _fut_alert_items()   # 期货异动（|涨跌幅|≥5%）
    news = _news_items()
    lead: list = []
    g0 = _next_goldoil(gold)   # 首位：伦敦金/布油按轮次交替
    if g0:
        lead.append(g0)
    lead.extend(fut_alerts)       # 期货异动紧随首位，醒目
    if cci:                       # CCI 市场情绪
        lead.append(cci)
    if not news:
        return {"items": lead, "notes": ["新闻源暂不可用"],
                "ts": time.strftime("%H:%M:%S")}

    name_map = _name_map()
    if not name_map:
        notes.append("股票名表首次构建中，本轮暂无个股关联（几分钟后自动生效）")
        threading.Thread(target=_name_map, kwargs={"force": True}, daemon=True).start()
    name_pairs = _sorted_name_pairs(name_map)

    quotes = _market_quotes()
    limit = int(c["limit"])
    items: list = list(lead)

    # 小概率：本轮流播开头插入一条大盘行情（黄金/恒生/恒生科技/科创50/沪深300/中证消费 择一）
    market_used = False
    if quotes and random.random() < float(c["market_prob"]):
        q = quotes[random.choice(list(quotes.keys()))]
        items.append({"type": "market", **q})
        market_used = True

    stock_prob = float(c["stock_prob"])
    news_cnt = 0
    for it in news:
        if len(items) >= limit + (1 if market_used else 0):
            break
        # ★ 2026-11-05：无原文链接的条目不再整条丢弃（财联社兜底源全部无链接，
        #   旧逻辑会让轮播一条新闻都不剩）。保留为纯文本，前端按 url 是否为空降级渲染。
        kws, stock_hit = _classify(it["title"], name_pairs)
        # 正文去掉头部全部【】标签（关键词已进徽章，正文更干净不重复）
        body = re.sub(r"^((【[^【】]{1,16}】\s*)+)", "", (it["text"] or it["title"])).strip()
        row = {"type": "news", "kws": kws,
               "kw": (kws[0]["kw"] if kws else ""),
               "kw_url": (kws[0]["url"] if kws else it["url"]),
               "text": body[:90],
               "url": it["url"], "time": it["time"]}
        items.append(row)
        news_cnt += 1
        if gold and news_cnt % 7 == 0:   # 每七则消息插播一次伦敦金/布油（轮次交替）
            gi = _next_goldoil(gold)
            if gi:
                items.append(gi)
        # 偶发：标题命中本地股票名 → 插入同花顺个股卡片（复用 classify 命中，概率控制密度）
        if stock_hit and random.random() < stock_prob:
            items.append({"type": "stock", "name": stock_hit[1], "code": stock_hit[0],
                          "url": _stock_url(stock_hit[0])})

    quote_hit = random.random() < 0.08   # 小概率彩蛋：一句话（约 8% 轮次出现）
    if quote_hit:
        items.append({"type": "quote", "text": "做好人，买好股，得好报"})

    data = {"items": items[: limit + 6 + (1 if quote_hit else 0)],
            "ts": time.strftime("%H:%M:%S"), "notes": notes}
    _CACHE.update(ts=time.time(), data=data)
    return data
