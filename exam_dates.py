# -*- coding: utf-8 -*-
"""考试日程：从公告里抽出「报名 / 缴费 / 准考证 / 笔试 / 面试」这些时间点，
存成日历（state/exams.json），到期前自动提醒。

为什么单独一个模块：列表页只有标题和链接，时间信息全在正文里。
所以这里会对"看起来像公告"的条目去抓一次正文，用正则把关键时间点抠出来。

正文里的写法很野，实测过的几种：
    报名时间为2026年9月10日9:00至9月12日17:00
    报名时间：9月15至10月26日            （连"日"字都没有）
    须于2026年10月14日9:00至10月17日8:30……自行下载打印准考证   （时间在前，关键词在后）
    面试定于2026年6月13日、6月14日进行
    面试通知书打印：2026年6月11日-6月14日
    公示时间：2026年7月22日-7月28日
所以策略是：先把正文按标点切成小片段，片段里"同时有事件关键词和日期"才算数。
"""

import datetime
import difflib
import html as html_mod
import json
import logging
import os
import re
import time

import config
from fetcher import http_get, url_date

# ---------- 正文清洗 ----------
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

# ---------- 日期 ----------
# 2026年9月12日 / 9月12日 / 9月12 / 9月15至……
# 年份同时认 19xx 和 20xx：公告里常混着「1986年9月18日」这种资格/年龄说明，
# 只认 20xx 的话会被当成本年，变成一条假日程。
_DATE_CN_RE = re.compile(r"(?:(19\d{2}|20\d{2})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*(?:[日号])?(?!\d)")
# 2026-09-12 / 2026/9/12 / 2026.9.12
_DATE_ISO_RE = re.compile(r"(20\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)")

# 事件类型：越"专有"的排越前，一个片段只认最靠前的那一个
KINDS = [
    ("准考证", ("准考证", "面试通知书")),
    ("缴费", ("缴费时间", "交费时间", "网上缴费", "缴费截止", "交费截止")),
    ("资格审查", ("资格初审", "资格审查", "资格复审")),
    ("笔试", ("笔试时间", "笔试日期", "笔试定于", "笔试定", "笔试于", "考试时间")),
    ("面试", ("面试时间", "面试定于", "面试日期", "面试于")),
    ("公示", ("公示时间", "公示期")),
    ("报名", ("报名时间", "报名截止", "报名日期", "网上报名", "报名")),
]

# 切片段：中文公告里一个逗号基本等于一个信息点
_SPLIT_RE = re.compile(r"[。；;！!\n\r，,、]+")

# 兜底解析用：关键词后面多少字以内出现日期算"挨着"
_WINDOW_SPAN = 35
# 关键词和日期之间要有这些"连接词"之一，否则多半不是同一件事
_CONNECT_RE = re.compile(r"时间|日期|于|为|起|至|—|–|-|~|：|:")

# 片段里出现这些说明只是个"另行通知"，没有具体时间，别浪费解析
_VAGUE_RE = re.compile(r"另行通知|另行公告|详见|关注|待定|以后续")

# 「报名」是所有关键词里最宽的一个，正文里的"报名费/报名表/报名人数/报名确认"
# 这类**说明性**文字也会命中它，实测会凭空长出一条报名区间：
#   2026年湖北省就业援藏…公告 里的「本次招聘考试免收报名费。4.打印准考证：
#   10月8日9:00至10月10日10:30」→ 被解析成 报名开始 10-08 / 报名截止 10-10
#   （那其实是准考证的时间），这条公告的报名窗口因此被显示成 9月22日–10月10日。
# 所以凡是"报名"后面紧跟这些字的，都不算报名事件本身。
_RECRUIT_FALSE_RE = re.compile(
    r"报名(?:费|费用|表|确认|确定|人数|条件|须知|网址|系统|入口|流程|照片|信息|"
    r"序号|登记表|记录表|推荐表|资格)")


def _recruit_ok(frag):
    """片段里除了"报名费/报名表/…"之外，还有没有真正的"报名"。"""
    return "报名" in _RECRUIT_FALSE_RE.sub("", frag)


# ============================== 工具 ==============================

def _plain_text(text):
    """HTML → 纯文本（去标签、实体转义、压缩空白）。"""
    text = _SCRIPT_RE.sub(" ", text or "")
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text)


def bj_today():
    """北京时间今天（服务器跑在 UTC，必须加 8 小时）。"""
    t = time.gmtime(time.time() + 8 * 3600)
    return datetime.date(t.tm_year, t.tm_mon, t.tm_mday)


def _fragments(text):
    for frag in _SPLIT_RE.split(text):
        frag = frag.strip()
        if 6 <= len(frag) <= 220:
            yield frag


def _find_dates(frag, default_year):
    """片段里的日期，按出现顺序返回 [date, ...]，去重。

    年份只写了半截的两种情况，在这里补齐：
      ① 「2025年10月15日至10月24日」——后半段省略了年份，要沿用前一个日期的年份。
         不补的话会拿"公告发布年"顶上，把去年的旧公告算成今年的（实测国考那条
         2025 年 10 月的公告被排到了 2026 年 10 月）。
      ② 沿用之后反而**早于**前一个日期，说明是跨年区间（「12月20日至1月5日」），
         再往后推一年。
    """
    items = []                      # (位置, 日期, 原文里到底写没写年份)
    for m in _DATE_ISO_RE.finditer(frag):
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        items.append((m.start(), d, True))
    for m in _DATE_CN_RE.finditer(frag):
        try:
            d = datetime.date(int(m.group(1)) if m.group(1) else default_year,
                              int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        items.append((m.start(), d, bool(m.group(1))))

    items.sort(key=lambda x: x[0])

    out, seen, prev = [], set(), None
    for _, d, explicit in items:
        if not explicit and prev is not None:
            # 省了年份 → 先按前一个日期的年份算；算出来又早于前一个日期，
            # 说明跨年了，再往后推（「12月20日至1月5日」→ 2027-01-05）。
            try:
                d = d.replace(year=prev.year)
            except ValueError:      # 2 月 29 日碰上没这个日期的年份，保持原样
                pass
            while d < prev:
                try:
                    d = d.replace(year=d.year + 1)
                except ValueError:
                    break
        if d in seen:
            continue
        seen.add(d)
        out.append(d)
        prev = d
    return out


def _kind_of(frag):
    for kind, words in KINDS:
        for w in words:
            if w not in frag:
                continue
            # 「考试时间」是个含糊说法：既可能是笔试，也可能是面试。
            # 实测「面试考试时间：2026年9月20日」原来被挂成了"笔试"。
            # 判断口径：写了「面试考试时间」的算面试；片段里提到面试、
            # 却完全没提笔试的也算面试；其它（例如
            # 「考试时间：…8:30—11:00 笔试成绩和进入面试人员名单…」）仍算笔试。
            if w == "考试时间" and ("面试考试时间" in frag
                                    or ("面试" in frag and "笔试" not in frag)):
                return "面试"
            return kind
    return None


def _assign(kind, dates, frag):
    """把日期落到具体事件上。返回 [(事件名, 日期), ...]。"""
    first = dates[0]
    # 区间一律用「前两个日期」，不能用最后一个：片段里常跟着别的子句，
    # 例如「报名时间：9月18日—9月23日 查询时间：9月24日」，
    # 取最后一个会把报名截止写成查询时间那天（差一天就可能误事）。
    second = dates[1] if len(dates) >= 2 else dates[0]

    if kind == "报名":
        if len(dates) >= 2:
            return [("报名开始", first), ("报名截止", second)]
        if "截止" in frag or "结束" in frag or "最后一天" in frag:
            return [("报名截止", first)]
        if "起" in frag or "开始" in frag or "开通" in frag:
            return [("报名开始", first)]
        # 只有一个日期、又没说清是开始还是截止——这种多半是正文里的
        # "报名确认""报名表打印"之类，含义不明，宁可不记，别误导。
        return []

    if kind == "缴费":
        return [("缴费截止", second)]

    if kind == "准考证":
        if len(dates) >= 2:
            return [("准考证开始", first), ("准考证截止", second)]
        return [("准考证打印", first)]

    if kind == "资格审查":
        return [("资格审查截止", second)]

    if kind == "公示":
        return [("公示截止", second)]

    if kind == "笔试":
        return [("笔试", first)]

    if kind == "面试":
        return [("面试", first)]

    return []


def _window_pass(text, default_year):
    """兜底：有些公告把关键词和日期用句号切开了，按片段切就漏。

    实测的原型：
        1.网上报名。于2026年8月17日9:00—8月25日17:00期间登录"龙江先锋网"…
    这里改成"围着关键词看后面 35 个字"，要求中间有 于/为/时间/： 这类连接词，
    再交给同一套 _assign() 落地，避免瞎认。
    """
    events = []
    for kind, words in KINDS:
        for w in words:
            for m in re.finditer(re.escape(w), text):
                # 裸"报名"命中「报名费/报名表/…」时跳过（那不是报名事件）
                if w == "报名" and _RECRUIT_FALSE_RE.match(text, m.start()):
                    continue
                # 「考试时间」含糊：附近提到面试（且没提笔试）就交给面试那条线，
                # 别在这儿算成笔试。片段法会给它挂「面试」。
                if w == "考试时间":
                    win = text[max(0, m.start() - 8): m.end() + _WINDOW_SPAN]
                    if "面试考试时间" in win or ("面试" in win and "笔试" not in win):
                        continue
                # 窗口右端不能切在数字中间：把「…至2026年9月27日」切成「…9月2」
                # 会凭空多出一个 9月2日（安徽林业职业技术学院那条实测多了
                # 一条"报名截止 2026-09-02"）。往后吃满连续数字再切。
                end = m.end() + _WINDOW_SPAN
                while end < len(text) and text[end].isdigit():
                    end += 1
                seg = text[m.end(): end]
                if not _CONNECT_RE.search(seg[:12]):
                    continue
                dates = _find_dates(seg, default_year)
                if not dates:
                    continue
                frag = text[max(0, m.start() - 8): end]
                if _VAGUE_RE.search(frag):
                    continue
                for name, d in _assign(kind, dates, frag):
                    events.append({"kind": name, "date": d.isoformat(), "raw": frag[:80]})
    return events


def parse_schedule(title, plain, default_year, url=""):
    """从标题 + 正文里抽出事件，返回 [{"kind":..,"date":..,"raw":..}, ...]。"""
    events = []
    for kind_hint in (title, plain):
        for frag in _fragments(kind_hint):
            if _VAGUE_RE.search(frag):
                continue
            kind = _kind_of(frag)
            if not kind:
                continue
            # 片段里的"报名"如果只是"报名费/报名表"这类说明，别当报名事件
            if kind == "报名" and not _recruit_ok(frag):
                continue
            dates = _find_dates(frag, default_year)
            if not dates:
                continue
            for name, d in _assign(kind, dates, frag):
                events.append({"kind": name, "date": d.isoformat(), "raw": frag[:80]})

    # 片段解析漏掉的（关键词和日期被句号切开），再粗糙地补一遍
    events.extend(_window_pass(plain, default_year))

    # 同一篇里同名事件只留最早提到的那个
    out, seen = [], set()
    for ev in events:
        key = (ev["kind"], ev["date"])
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


# ============================== 日历存取 ==============================

def load_store(path=None):
    path = path or config.SCHEDULE_FILE
    blank = {"parsed": {}, "events": []}
    if not os.path.exists(path):
        return blank
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            raise ValueError("格式不对")
        data.setdefault("parsed", {})
        data.setdefault("events", [])
        return data
    except Exception as exc:
        logging.warning("考试日历读取失败，按空处理：%s", exc)
        return blank


def save_store(store, path=None):
    path = path or config.SCHEDULE_FILE
    today = bj_today()
    # 收拾一下：去重、丢掉早就过去的事件、控制总量
    keep, seen = [], set()
    for ev in store.get("events", []):
        try:
            d = datetime.date.fromisoformat(ev["date"])
        except Exception:
            continue
        if (today - d).days > 10:           # 过期 10 天以上就不要了
            continue
        key = (ev.get("url", ""), ev.get("kind", ""), ev["date"], ev.get("title", ""))
        if key in seen:                      # 手动录入的日期每轮都会来一遍，这里挡掉
            continue
        seen.add(key)
        keep.append(ev)
    keep.sort(key=lambda e: (e["date"], e.get("kind", "")))
    store["events"] = keep[-config.SCHEDULE_MAX:]

    # parsed 只留最近的记录，别让文件无限膨胀
    parsed = store.get("parsed", {})
    if len(parsed) > 1200:
        items = sorted(parsed.items(), key=lambda kv: kv[1], reverse=True)[:1000]
        store["parsed"] = dict(items)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # newline="\n"：不加的话 Windows 本地跑会落成 CRLF，
    # 一提交就是"整个文件重写"的假 diff（Actions 上是 Linux，写出来本来就是 LF）。
    with open(path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(store, fp, ensure_ascii=False, indent=1)
    logging.info("考试日历已保存：%d 个时间点", len(store["events"]))


# ============================== 主流程 ==============================

def _worth_parsing(title):
    if any(w in title for w in config.DETAIL_SKIP):
        return False
    return any(w in title for w in config.DETAIL_HINTS)


def is_announcement(it):
    """这条算不算"招考公告"（要过"正文有没有写报名/考试时间"的检查）。

    以源的 kind 为准（config 里每个源都标了 announce / news）；
    没有 kind 的老数据退化成按标题关键词判断。
    """
    if isinstance(it, dict):
        if it.get("kind"):
            return it["kind"] == "announce"
        title = it.get("title", "")
    else:
        title = it or ""
    return _worth_parsing(title)


def _candidates(all_items, store):
    """挑出值得抓正文的公告，新的、近期的排前面。"""
    parsed = store.get("parsed", {})
    today = bj_today()
    picked = []
    for it in all_items:
        url, title = it["url"], it["title"]
        # 只解析**公告源**（kind="announce"）的东西。以前这里只看标题关键词，
        # 时政源里标题带"公告/招聘/通知"的文章也会被抓去解析，混进考试日历。
        if not is_announcement(it):
            continue
        if url in parsed or not _worth_parsing(title):
            continue
        pub = url_date(url)
        year = pub.tm_year if pub else today.year
        if pub:
            age = (today - datetime.date(pub.tm_year, pub.tm_mon, pub.tm_mday)).days
            if age > config.DETAIL_MAX_AGE_DAYS:
                continue
        else:
            age = 999
        picked.append((age, year, it))
    picked.sort(key=lambda x: (x[0], x[2].get("source", "")))
    return picked


def update_calendar(all_items, store):
    """抓公告正文、抽时间点、写进日历。返回新解析到日程的公告条数。"""
    picked = _candidates(all_items, store)[: config.DETAIL_FETCH]
    if not picked:
        logging.info("没有需要解析正文的公告")
        return 0

    today = bj_today()
    added = 0
    for age, year, it in picked:
        if parse_item(it, store, today):
            added += 1

    # 手动补充的重要日期（官方大考机器未必抓得到）
    for date_str, kind, title in (config.MANUAL_EVENTS or []):
        store["events"].append({"kind": kind, "date": date_str, "raw": "手动录入",
                                "url": "", "title": title, "source": "手动",
                                "added": today.isoformat()})
    return added


def fetch_plain(it):
    """抓一篇正文并转成纯文本；抓不到返回 None。"""
    try:
        raw, _size = http_get(it["url"], timeout=max(config.REQUEST_TIMEOUT, 20))
    except Exception as exc:
        logging.warning("解析正文失败（%s）：%s", it["title"][:20], exc)
        return None
    return _plain_text(raw)


def parse_item(it, store, today=None, plain=None):
    """抓一篇公告正文、抽时间点、写进日历，并返回这篇的事件列表。

    返回 None 和返回 [] 不是一回事：
      None → 正文根本没抓到，判断不了（调用方要按"未知"处理，宁可留着）
      []   → 正文拿到了，里面确实没有任何时间点
    已经解析过的链接直接走缓存，不重复抓；plain 可以复用外面已经抓好的正文。
    """
    url, title = it["url"], it["title"]
    today = today or bj_today()

    if url in store.get("parsed", {}):
        return [dict(ev) for ev in store.get("events", []) if ev.get("url") == url]

    if plain is None:
        plain = fetch_plain(it)
        if plain is None:
            return None

    pub = url_date(url)
    year = pub.tm_year if pub else today.year
    events = parse_schedule(title, plain, year, url)
    store["parsed"][url] = today.isoformat()

    for ev in events:
        ev.update({"url": url, "title": title,
                   "source": it.get("source", ""),
                   "added": today.isoformat()})
        store["events"].append(ev)
    if events:
        logging.info("解析到日程：%s → %s", title[:24],
                     "、".join("%s %s" % (e["kind"], e["date"]) for e in events))
    return events


# ============================== 提醒文案 ==============================

def _fmt(date_str):
    d = datetime.date.fromisoformat(date_str)
    return "%d月%d日" % (d.month, d.day)


def _when(days):
    if days == 0:
        return "今天"
    if days == 1:
        return "明天"
    if days == 2:
        return "后天"
    return "%d 天后" % days


def _short(text, n=30):
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# 只有写明了这些时间的公告才算"考试公告"（只写缴费/资格审查/公示的不算）
KEY_KINDS = ("报名开始", "报名截止", "准考证开始", "准考证截止", "准考证打印",
             "笔试", "面试")

# 「最近几天」那块里同一天撞了多个事件时，按这个顺序挑一个显示
_KEY_ORDER = ("笔试", "面试", "准考证截止", "准考证开始", "准考证打印",
              "报名截止", "报名开始")

# 同一篇公告里**同名事件有多条**时怎么取值（多阶段报名、多批缴费/审查很常见）：
# 截止类取**最晚**那个，其余（开始类）取最早的。早先一律"只留第一个"，
# 两段就会错配，提醒里出现「报名 9月15日–9月12日」这种倒挂
# （2027 内蒙古事业单位那条实测）。
_TAKE_LATEST = ("报名截止", "准考证截止", "缴费截止", "资格审查截止", "公示截止")

# 去重：两条标题**只有这些词的差别**时，才算同一篇公告。
# 华图 / 中公互相转载，惯用「招聘 ↔ 引进」「公开」「工作人员」「年 / 第 / 批」
# 这类字眼做区分；反过来，一旦差在「民乐 ↔ 肃南」「榆林市」这种专名上，
# 就宁可各留一行，也不要误删一条真公告。
_IGNORABLE_DIFF_RE = re.compile(
    r"^(?:年度|度|年|第|批|次|期|届|招聘|招录|招考|招收|公开|面向社会|面向|"
    r"引进|选聘|选调|遴选|选录|补充|再次|公告|简章|通告|通知|公示|安排|"
    r"事业|单位|工作|人员|岗位|职位|编制|计划|方案|共|计|若干|"
    r"人|名|个|位|的|与|和|及)+$"
)


def _dedup_norm(title):
    """标题归一化：去掉标点空白和年份，剩下的用来比对。"""
    return re.sub(r"[\s\W]", "", re.sub(r"20\d{2}|19\d{2}", "", title or ""))


def _dkey_compatible(a, b):
    """两组"考试时间"能不能视为同一场：完全相同，或一方是另一方的子集。

    子集也算，是因为两个站解析同一篇公告时常常一个多抠出一个时间点；
    子集意味着**没有互相矛盾的日期**，不会把两场不同的考试合并到一起。
    """
    sa, sb = set(a), set(b)
    return sa == sb or sa <= sb or sb <= sa


def _dedup_items(items):
    """把"同一篇公告被两个网站各转一遍"的合成一条，返回留下的那些。

    留下的那条挑"时间点多、标题完整"的——否则合并时会把
    「笔试 X 月 X 日」这类信息一起丢掉。
    """
    kept = []
    for it in items:
        dup_at = None
        for idx, old in enumerate(kept):
            if _dkey_compatible(it["dkey"], old["dkey"]) \
                    and _same_announcement(it["norm"], old["norm"]):
                dup_at = idx
                break
        if dup_at is None:
            kept.append(it)
            continue
        old = kept[dup_at]
        logging.info("去重：%s ⟵ 与「%s」重复", it["short"][:26], old["short"][:26])
        if (len(it["dkey"]), len(it["title"])) > (len(old["dkey"]), len(old["title"])):
            kept[dup_at] = it
    return kept


def _same_announcement(a, b):
    """两篇是不是同一张公告（被两个网站各转了一遍）。

    判据有两道：整体得够像（≥0.6），且**每一处差异都是发布用语**。
    第二道是关键——「民乐县」和「肃南县」两条公告的标题相似度高达 0.90、
    报名时间还完全一样，只看相似度必然误删一条。
    """
    if a == b:
        return True
    if abs(len(a) - len(b)) > 12:        # 长度差太多，多半不是同一条
        return False
    sm = difflib.SequenceMatcher(None, a, b)
    if sm.ratio() < 0.6:
        return False
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for piece in (a[i1:i2], b[j1:j2]):
            if piece and not _IGNORABLE_DIFF_RE.match(piece):
                return False
    return True


def _span(dates, name, k1, k2):
    """一对起止日期 → 「报名 9月18日–9月23日」；只有一头也照样显示。"""
    a, b = dates.get(k1), dates.get(k2)
    if a and b:
        if b < a:
            # 兜底：万一截止比开始还早（数据脏、或两段报名没配好），
            # 宁可不给区间，也别显示"9月15日–9月12日"这种倒挂。
            return "%s %s起" % (name, _fmt(a))
        return "%s %s–%s" % (name, _fmt(a), _fmt(b))
    if a:
        return "%s %s起" % (name, _fmt(a))
    if b:
        return "%s 至%s" % (name, _fmt(b))
    return ""


def _exam_brief(dates):
    """考试类时间（准考证/笔试/面试），给前两块当附注。"""
    parts = []
    span = _span(dates, "准考证", "准考证开始", "准考证截止")
    if span:
        parts.append(span)
    if dates.get("准考证打印"):
        parts.append("准考证打印 %s" % _fmt(dates["准考证打印"]))
    if dates.get("笔试"):
        parts.append("笔试 %s" % _fmt(dates["笔试"]))
    if dates.get("面试"):
        parts.append("面试 %s" % _fmt(dates["面试"]))
    return " ｜ ".join(parts)


def _timeline(dates):
    """一篇公告的完整时间线，一行短文案（最多 4 段，免得撑爆）。"""
    parts = []
    for name, k1, k2 in (("报名", "报名开始", "报名截止"),
                         ("准考证", "准考证开始", "准考证截止")):
        span = _span(dates, name, k1, k2)
        if span:
            parts.append(span)
    if dates.get("准考证打印"):
        parts.append("准考证打印 %s" % _fmt(dates["准考证打印"]))
    if dates.get("缴费截止"):
        parts.append("缴费止 %s" % _fmt(dates["缴费截止"]))
    if dates.get("资格审查截止"):
        parts.append("审查止 %s" % _fmt(dates["资格审查截止"]))
    if dates.get("笔试"):
        parts.append("笔试 %s" % _fmt(dates["笔试"]))
    if dates.get("面试"):
        parts.append("面试 %s" % _fmt(dates["面试"]))
    return " ｜ ".join(parts[:4])


def build_reminder(store):
    """生成提醒文案（公告维度）；一条都凑不出来时返回 None。

    三块：
      ① 🔥 报名进行中   —— 报到名的（按截止日排序，最急的在前）
      ② ⏳ 最近 N 天    —— 马上要动的事（报名开始/截止、准考证、笔试、面试）
      ③ 📋 其它已定时间的公告 —— 只要公告里写明了报名或考试时间，都列在这儿，
                                 不再因为"离得远"就不提
    """
    today = bj_today()

    by_url = {}
    for ev in store.get("events", []):
        if ev.get("url"):
            by_url.setdefault(ev["url"], []).append(ev)

    # ① 先把"同一篇公告被华图、中公各转一遍"的合并掉，只留信息最全的那条。
    #    只比"考试时间"（报名、准考证、笔试、面试），不比缴费/资格审查——
    #    那两项各站解析出的早晚不一，拿来当条件就永远去不掉重了。
    #    标题用"差异必须只是发布用语"来判（见 _same_announcement）。
    items = []
    for url, evs in by_url.items():
        dates = {}                       # kind -> 日期
        for ev in evs:
            kind, day = ev["kind"], ev["date"]
            # 同名多条时的取值口径见 _TAKE_LATEST 的注释
            if kind in _TAKE_LATEST:
                if kind not in dates or day > dates[kind]:
                    dates[kind] = day
            else:
                dates.setdefault(kind, day)
        if not (set(dates) & set(KEY_KINDS)):
            continue                     # 只写了缴费/资格审查/公示的，不算考试公告
        items.append({
            "url": url, "title": evs[0]["title"], "short": _short(evs[0]["title"]),
            "norm": _dedup_norm(evs[0]["title"]), "dates": dates,
            "dkey": tuple(sorted((k, v) for k, v in dates.items() if k in KEY_KINDS)),
        })

    kept = _dedup_items(items)

    # ② 再按时间分块
    ongoing, upcoming, other = [], [], []
    for it in kept:
        dates, url, title = it["dates"], it["url"], it["short"]
        start, end = dates.get("报名开始"), dates.get("报名截止")
        if start and end:
            d_start = datetime.date.fromisoformat(start)
            d_end = datetime.date.fromisoformat(end)
            if d_start <= today <= d_end and (d_end - today).days <= config.ONGOING_WINDOW_DAYS:
                ongoing.append(((d_end - today).days, d_end, url, title, _exam_brief(dates)))
                continue

        soon = None
        for kind in _KEY_ORDER:
            iso = dates.get(kind)
            if not iso:
                continue
            days = (datetime.date.fromisoformat(iso) - today).days
            if 0 <= days <= config.REMIND_DAYS and (soon is None or days < soon[0]):
                soon = (days, kind, iso)
        if soon:
            upcoming.append((soon[0], datetime.date.fromisoformat(soon[2]),
                             soon[1], url, title, _exam_brief(dates)))
            continue

        future = sorted(d for d in dates.values()
                        if datetime.date.fromisoformat(d) >= today)
        if future:
            other.append((future[0], url, title, _timeline(dates)))

    if not (ongoing or upcoming or other):
        return None

    ongoing.sort(key=lambda x: x[0])
    upcoming.sort(key=lambda x: (x[0], x[2]))
    other.sort(key=lambda x: x[0])

    lines = ["**⏰ 考试日程提醒 · %s**" % today.strftime("%m-%d"), ""]
    used = len("\n".join(lines).encode("utf-8"))

    def add(text):
        """按字节预算加行；超了就返回 False，调用方自己收尾。"""
        nonlocal used
        size = len(text.encode("utf-8")) + 1
        if used + size > config.MSG_BUDGET:
            return False
        lines.append(text)
        used += size
        return True

    if ongoing:
        add("**🔥 报名进行中（%d）**" % len(ongoing))
        n = 0
        for left, d_end, url, title, brief in ongoing[: config.REMIND_ONGOING_MAX]:
            tail = "今天截止！" if left == 0 else "还剩 %d 天（%s 截止）" % (left, _fmt(d_end.isoformat()))
            line = "- %s ｜ [%s](%s)" % (tail, title, url)
            if brief:
                line += " ｜ %s" % brief
            if not add(line):
                break
            n += 1
        if len(ongoing) > n:
            add("- …另有 %d 条报名进行中" % (len(ongoing) - n))
        add("")

    if upcoming:
        add("**⏳ 最近 %d 天（%d）**" % (config.REMIND_DAYS, len(upcoming)))
        n = 0
        for days, d, kind, url, title, brief in upcoming[: config.REMIND_UPCOMING_MAX]:
            label = "%s %s" % (_when(days), d.strftime("%m-%d"))
            line = "- %s ｜ %s ｜ [%s](%s)" % (label, kind, title, url)
            if brief:
                line += " ｜ %s" % brief
            if not add(line):
                break
            n += 1
        if len(upcoming) > n:
            add("- …另有 %d 条" % (len(upcoming) - n))
        add("")

    if other:
        add("**📋 其它已定时间的公告（%d）**" % len(other))
        n = 0
        for _nearest, url, title, timeline in other[: config.REMIND_OTHER_MAX]:
            line = "- [%s](%s)" % (title, url)
            if timeline:
                line += " ｜ %s" % timeline
            if not add(line):
                break
            n += 1
        if len(other) > n:
            add("- …另有 %d 条" % (len(other) - n))
        add("")

    # 这里的"共 N 条"是三个板块加起来的总数（含被折叠的），
    # 跟上面每个板块括号里的数字对得上。
    lines.append("共 %d 条 · %s 更新"
                 % (len(ongoing) + len(upcoming) + len(other),
                    time.strftime("%H:%M", time.gmtime(time.time() + 8 * 3600))))
    return "\n".join(lines)
