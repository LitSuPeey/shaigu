# -*- coding: utf-8 -*-
"""政策面（Policy Surface）—— 把「国债期货代理」换成「真实的政策文本事件」。

为什么单独一个模块
------------------
指数指标页签原先把「政策流动性」这一维交给 10 年期国债期货（价格涨 = 收益率下行
= 宽货币）。那只是**利率预期**，不是政策本身：一次政治局会议定调、一份国常会部署、
一场发布会答问，都可以在国债期货上完全看不出来（甚至反向）。

本模块补的是「文本这一层」，且严格限定在自己能做到的范围内：
· 只做**新闻标题/摘要级**的关键词命中，不解析政策原文、不做语义理解；
· 只统计「说了什么」，不预测「什么时候落到价格」；
· 部委/主题归类是按关键词命中做的规则映射，不是实体识别模型。

★ 为什么单独开接口而不是并进 /api/idxhub/panel
  快讯源要拉 200 条再逐条正则，实测 2~6s；并进主面板会把 3s 的页签拖到 10s+，
  而政策事件的更新频率是「小时级」而非「分钟级」，完全没必要跟着主面板刷新。
  所以独立成 /api/idxpolicy/panel，前端在页签渲染完成后再异步取，失败不影响主视图。

数据源：ticker._news_items()（东财 → 富途 → 财联社 三级降级，与 alert 的宏观雷达同源）。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

_LOCK = threading.RLock()
_CACHE: dict = {"ts": 0.0, "data": None}
_TTL = 900              # 15 分钟：政策事件是小时级信息，不需要分钟级刷新

# ============ 部委 / 主题规则 ============
# 顺序即优先级：越具体的部委放前面，避免「央行降准」被归到泛泛的「国务院」。
# ★ 关键词必须**收紧到机构名/专有政策名**。实测教训（2026-11-06）：
#   裸词「央行」会把「欧洲央行」算成中国人民银行，「算力」会把
#   「某公司签署算力合同」算成工信部政策 —— 泛词一天能造出十几条假政策。
AGENCIES: list = [
    ("中国人民银行", "货币", ["中国人民银行", "人民银行", "货币政策", "降准", "降息",
                              "中期借贷便利", "MLF", "LPR", "逆回购", "再贷款",
                              "公开市场操作", "买断式回购", "潘功胜", "存款准备金率"]),
    ("财政部", "财政", ["财政部", "专项债", "赤字率", "超长期特别国债", "减税降费",
                        "财政贴息", "蓝佛安", "财政政策", "转移支付", "特别国债"]),
    ("证监会", "资本市场", ["证监会", "注册制", "减持新规", "退市制度", "并购重组",
                            "平准基金", "印花税", "吴清", "资本市场改革", "北交所"]),
    ("中央政治局", "定调", ["政治局", "中央经济工作会议", "两会", "政府工作报告",
                            "中央金融工作会议", "三中全会", "四中全会", "十五五"]),
    ("国务院", "定调", ["国务院", "国常会", "常务会议", "李强"]),
    ("发改委", "产业", ["发改委", "国家发展改革委", "重大项目", "以工代赈",
                        "两新", "两重", "郑栅洁"]),
    ("金融监管总局", "金融监管", ["金融监管总局", "银保监", "险资", "保险资金",
                                  "李云泽", "房地产融资协调机制"]),
    ("住建部", "地产", ["住建部", "房地产政策", "楼市", "限购", "房贷利率", "保交楼",
                        "城中村改造", "保障房", "收储"]),
    ("工信部", "产业", ["工信部", "工业和信息化部", "专精特新", "制造业单项冠军",
                        "新质生产力", "低空经济"]),
    ("外汇局", "汇率", ["外汇局", "人民币汇率", "中间价", "跨境资金", "外汇储备"]),
]

# 负向词：命中即整条丢弃。海外主体与公司公告是最大的两个噪声源。
NEG_PAT = re.compile(
    r"欧洲央行|美联储|日本央行|英国央行|韩国央行|印度|特朗普|白宫|美国国会|"
    r"欧盟|德国|法国|俄罗斯|乌克兰|以色列|伊朗|土耳其|巴西|新加坡|"
    r"香港|澳门|台湾|"
    r"有限公司|股份|中标|签署|签订|合同|营收|净利润|业绩预告|涨停|跌停|"
    r"龙虎榜|股价异动|回购|定增|可转债|上会|发行价|询价|"
    r"基金净值|ETF份额|持仓变动|大宗交易|ST[\u4e00-\u9fa5]{2}")

# 政策动作：没有动作词的「部委提及」多是会议报道/人事/外事，不算政策事件。
ACTION_PAT = re.compile(
    r"召开|举行|印发|发布|出台|部署|审议|通过|决定|通知|意见|方案|规划|"
    r"条例|办法|细则|试点|约谈|表态|强调|指出|明确|划出重点|征求意见|"
    r"座谈会|发布会|答记者问")

# 强动作词：即使没抓准部委，命中这些也几乎必然是政策事件。
STRONG_ACTIONS = ("降准", "降息", "下调存款准备金率", "中期借贷便利", "MLF", "LPR",
                  "逆回购", "再贷款", "专项债", "特别国债", "赤字率", "平准基金",
                  "印花税", "政治局会议", "国常会", "中央经济工作会议",
                  "政府工作报告", "金融委", "减持新规", "退市制度")

# 方向词：只收强信号，宁可漏判也不要误判（「下调油价」不是宽松）
EASE_PAT = re.compile(
    r"降准|降息|下调[^，。]{0,8}(利率|存款准备金率|税率|首付)|"
    r"(宽松|放水|加大|加码)[^，。]{0,8}(力度|逆周期|调节)|"
    r"扩大[^，。]{0,6}(赤字|专项债|额度|内需)|减税|降费|"
    r"(支持|提振|刺激|稳住|促进)[^，。]{0,8}(楼市|股市|消费|房地产|经济)|"
    r"释放流动性|增量政策|提前批|降成本|稳市|稳增长|稳就业|逆周期")
TIGHT_PAT = re.compile(
    r"上调[^，。]{0,8}(利率|存款准备金率|税率)|收紧|严控|去杠杆|加准|"
    r"(打击|整治)[^，。]{0,6}(违规|乱象|炒作)|"
    r"(暂停|限制)[^，。]{0,6}(新增|发行)|降温|遏制")

# 主题（与部委正交：一个部委可以出多个主题的事件）
THEMES: list = [
    ("货币政策", ["降准", "降息", "中期借贷便利", "MLF", "LPR", "逆回购", "再贷款",
                  "流动性", "存款准备金", "公开市场操作"]),
    ("财政政策", ["专项债", "赤字率", "特别国债", "减税降费", "财政贴息", "转移支付"]),
    ("资本市场", ["注册制", "减持新规", "退市制度", "并购重组", "平准基金", "印花税",
                  "资本市场改革", "中长期资金入市"]),
    ("房地产", ["房地产政策", "楼市", "限购", "房贷利率", "保交楼", "城中村改造",
                "收储", "保障房"]),
    ("产业政策", ["新质生产力", "专精特新", "低空经济", "制造业单项冠军",
                  "集成电路产业", "人工智能+"]),
    ("汇率与外储", ["人民币汇率", "中间价", "外汇储备", "跨境资金"]),
    ("监管与规范", ["金融监管", "专项整治", "立案调查", "处罚决定", "规范"]),
]

# 政策窗口（**历史惯例**，不是官方日程；只在临近时给出提示，不做精确日期承诺）
WINDOWS: list = [
    {"name": "两会（政府工作报告定赤字率/目标）", "month": 3, "day": 5, "span": 8,
     "note": "3 月上旬开幕，具体日期每年由人大常委会决定"},
    {"name": "政治局会议（分析经济形势）", "month": 4, "day": 28, "span": 6,
     "note": "惯例在 4 月底，通常配套一季度经济数据定调"},
    {"name": "政治局会议（年中定调）", "month": 7, "day": 28, "span": 6,
     "note": "惯例在 7 月底，部署下半年经济工作"},
    {"name": "政治局会议（四季度）", "month": 10, "day": 28, "span": 6,
     "note": "惯例在 10 月底，有时缺席"},
    {"name": "中央经济工作会议", "month": 12, "day": 12, "span": 6,
     "note": "惯例在 12 月中旬，定次年政策基调"},
]


def _dir_of(text: str) -> str:
    """方向判定：宽松 / 紧缩 / 中性。只认强信号词。"""
    e = bool(EASE_PAT.search(text))
    t = bool(TIGHT_PAT.search(text))
    if e and not t:
        return "宽松"
    if t and not e:
        return "紧缩"
    if e and t:
        return "分歧"
    return "中性"


def _agency_of(text: str) -> Optional[str]:
    for name, theme, kws in AGENCIES:
        if any(k in text for k in kws):
            return name
    return None


def _themes_of(text: str) -> list:
    return [name for name, kws in THEMES if any(k in text for k in kws)]


def _recent_days() -> int:
    """快讯源一般只有最近 1-3 天，取 7 天做上限，避免把陈年旧闻当新事件。"""
    return 7


def _parse_time(s: str) -> Optional[float]:
    """把各种时间格式统一成时间戳，用于排序与时间窗过滤。

    ★ 无年份的格式（"12-31 23:00"）不能一律套当前年：跨年之后，
      去年 12-31 的快讯会被解析成今年 12-31（一个未来时间），
      于是 7 天窗口过滤直接失效、旧新闻被当成新事件。
      判据：解析结果落在「明天之后」就退一年。
    """
    s = (s or "").strip()
    if not s:
        return None
    s = s.replace("/", "-")
    now = time.time()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%m-%d %H:%M", "%H:%M"):
        try:
            d = time.strptime(s, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            # ★ 纯时分没有日期，strptime 会把月/日补成 1 月 1 日 —— 必须补今天，
            #   否则一条「15:30」的快讯会被算成 1 月 1 日，直接掉出 7 天窗口。
            lt = time.localtime()
            return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                d.tm_hour, d.tm_min, 0, 0, 0, -1))
        if d.tm_year > 1900:
            y = d.tm_year
        else:
            lt = time.localtime()
            y = lt.tm_year
            ts = time.mktime((y, d.tm_mon, d.tm_mday, d.tm_hour, d.tm_min, 0, 0, 0, -1))
            if ts > now + 86400:          # 落在未来 → 是去年的
                y -= 1
        return time.mktime((y, d.tm_mon, d.tm_mday, d.tm_hour, d.tm_min, 0, 0, 0, -1))
    return None


def _windows_now() -> list:
    """距离下一个「惯例政策窗口」还有几天。明确标注是惯例而非官方日程。"""
    now = time.localtime()
    out = []
    for w in WINDOWS:
        for yy in (now.tm_year, now.tm_year + 1):
            try:
                t = time.mktime((yy, w["month"], w["day"], 0, 0, 0, 0, 0, -1))
            except (OverflowError, ValueError):
                continue
            days = int((t - time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                         0, 0, 0, 0, 0, -1))) / 86400)
            if 0 <= days <= 45:
                out.append({"name": w["name"], "date": time.strftime("%Y-%m-%d", time.localtime(t)),
                            "days": days, "in_window": days <= w["span"], "note": w["note"]})
                break
    out.sort(key=lambda x: x["days"])
    return out[:3]


def panel(force: bool = False) -> dict:
    """政策面面板。失败时也返回 ok=True 但带 unavailable 说明，便于前端区分
    「没有政策新闻」和「新闻源挂了」——这两种情况给的结论完全不同。"""
    now = time.time()
    with _LOCK:
        if not force and _CACHE["data"] and now - _CACHE["ts"] < _TTL:
            return _CACHE["data"]

    t0 = time.time()
    news, err = [], ""
    try:
        from .ticker import _news_items
        news = _news_items() or []
    except Exception as e:  # noqa: BLE001
        err = "新闻源不可用：%s" % str(e)[:120]

    cutoff = now - _recent_days() * 86400
    events, agency_cnt, theme_stat = [], {}, {}
    dropped = {"neg": 0, "weak": 0}
    for it in news:
        title = str(it.get("title") or "")
        text = str(it.get("text") or "")
        doc = (title + " " + text).strip()
        if not doc:
            continue
        # ① 先剔噪声：海外主体 / 公司公告 / 个股行情
        if NEG_PAT.search(doc):
            dropped["neg"] += 1
            continue
        agency = _agency_of(title) or _agency_of(doc)
        themes = _themes_of(title) or _themes_of(doc)
        action = bool(ACTION_PAT.search(title)) or any(k in doc for k in STRONG_ACTIONS)
        # ② 再判「够不够格算政策事件」：
        #    有主体 → 必须同时有动作词或主题词（否则只是媒体提了一嘴部委）
        #    无主体 → 必须命中强动作词（否则就是泛泛的行业新闻）
        if agency:
            if not (action or themes):
                dropped["weak"] += 1
                continue
        else:
            if not any(k in doc for k in STRONG_ACTIONS):
                dropped["weak"] += 1
                continue
        ts = _parse_time(str(it.get("time") or ""))
        if ts is not None and ts < cutoff:
            continue
        d = _dir_of(doc)
        # 命中位置：标题命中比正文命中更可信（快讯正文常混入大量无关词）
        in_title = bool(_agency_of(title) or _themes_of(title) or
                        EASE_PAT.search(title) or TIGHT_PAT.search(title))
        ev = {"time": str(it.get("time") or ""), "title": title[:90],
              "agency": agency or "", "themes": themes, "dir": d,
              "url": it.get("url") or "", "in_title": in_title,
              "ts": ts or 0.0}
        events.append(ev)
        if agency:
            agency_cnt[agency] = agency_cnt.get(agency, 0) + 1
        for th in themes:
            s = theme_stat.setdefault(th, {"name": th, "n": 0, "ease": 0, "tight": 0})
            s["n"] += 1
            if d == "宽松":
                s["ease"] += 1
            elif d == "紧缩":
                s["tight"] += 1

    # 标题命中的排前面，同组按时间倒序
    events.sort(key=lambda x: (0 if x["in_title"] else 1, -x["ts"]))
    events = events[:24]

    ease = sum(1 for e in events if e["dir"] == "宽松")
    tight = sum(1 for e in events if e["dir"] == "紧缩")
    # ★ 方向标签不能只看差值：ease=1 / tight=0 时差值是 1，
    #   早先的 `ease >= tight + 3` 会判成「分歧」，可实际上根本没有反向声音。
    if ease == 0 and tight == 0:
        tone, tone_label = "none", "未见方向信号"
    elif ease and tight:
        if abs(ease - tight) <= max(2, 0.5 * max(ease, tight)):
            tone, tone_label = "mixed", "方向分歧"
        elif ease > tight:
            tone, tone_label = "ease", "偏宽松（有反向声音）"
        else:
            tone, tone_label = "tight", "偏紧缩（有反向声音）"
    elif ease:
        tone, tone_label = "ease", ("偏宽松" if ease >= 2 else "轻微偏宽松")
    else:
        tone, tone_label = "tight", ("偏紧缩" if tight >= 2 else "轻微偏紧缩")

    res = {
        "ok": True, "err": err, "unavailable": bool(err),
        "asof": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_news": len(news), "n_hit": len(events),
        "events": events,
        "agencies": sorted([{"name": k, "n": v} for k, v in agency_cnt.items()],
                           key=lambda x: -x["n"])[:8],
        "themes": sorted(theme_stat.values(), key=lambda x: -x["n"])[:6],
        "tone": {"code": tone, "label": tone_label, "ease": ease, "tight": tight,
                 "neutral": len(events) - ease - tight},
        "windows": _windows_now(),
        "elapsed": round(time.time() - t0, 2),
        "filtered": {"noise": dropped["neg"], "weak": dropped["weak"]},
        "caveats": [
            "本区块是**新闻标题/摘要级的关键词检索**，不是政策原文分析，"
            "也没有做语义理解或实体识别",
            "只覆盖快讯源能抓到的内容（东财/富途/财联社），不等于政策全貌；"
            "部委官网原文、内部文件、发布会答问细节都不在覆盖范围内",
            "入表前过了两道筛：先剔除海外主体与公司公告，再要求「主体+动作」同时成立；"
            "被剔条数见 filtered，筛得狠是为了不把公司签合同当成产业政策",
            "方向判定基于强信号词（降准/降息/收紧 等），宁缺毋滥；"
            "「中性」不等于无政策，只等于没有命中方向词",
            "政策落地到价格有时滞且常被其它因素覆盖，本区块**不预测时点**，"
            "更不构成交易建议",
            "政策窗口是**历史惯例推算**，不是官方日程；具体日期以官方发布为准",
        ],
    }
    with _LOCK:
        _CACHE["ts"], _CACHE["data"] = now, res
    return res


def meta_info() -> dict:
    return {
        "agencies": [{"name": n, "theme": t, "keywords": len(k)} for n, t, k in AGENCIES],
        "themes": [{"name": n, "keywords": len(k)} for n, k in THEMES],
        "windows": WINDOWS,
        "recent_days": _recent_days(),
        "ttl": _TTL,
        "caveats": [
            "关键词级检索，非 NLP 语义分析；不解析政策原文",
            "数据源为公开快讯（东财/富途/财联社），存在覆盖偏差与重复",
        ],
    }
