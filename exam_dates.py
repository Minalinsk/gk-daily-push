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
_DATE_CN_RE = re.compile(r"(?:(20\d{2})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*(?:[日号])?(?!\d)")
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

# "算作考试时间"的事件：一篇公告里至少要有其中一个，才值得提醒。
# 报名/缴费/资格审查这类只是"手续时间"，公告连考试时间都没写出来的话，
# 提醒过去也是空的（老大 2026-09-20 明确要求：读不到考试时间的公告不用发）。
EXAM_TIME_KINDS = ("笔试", "面试", "准考证开始", "准考证截止", "准考证打印")

# 切片段：中文公告里一个逗号基本等于一个信息点
_SPLIT_RE = re.compile(r"[。；;！!\n\r，,、]+")

# 片段里出现这些说明只是个"另行通知"，没有具体时间，别浪费解析
_VAGUE_RE = re.compile(r"另行通知|另行公告|详见|关注|待定|以后续")


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
    """片段里的日期，按出现顺序返回 [(date, 原文), ...]，去重。"""
    found = []
    for m in _DATE_ISO_RE.finditer(frag):
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        found.append((m.start(), d))
    for m in _DATE_CN_RE.finditer(frag):
        year = int(m.group(1)) if m.group(1) else default_year
        try:
            d = datetime.date(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        found.append((m.start(), d))

    found.sort(key=lambda x: x[0])
    out, seen = [], set()
    for _, d in found:
        if d in seen:
            continue
        seen.add(d)
        out.append(d)
    return out


def _kind_of(frag):
    for kind, words in KINDS:
        if any(w in frag for w in words):
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
            dates = _find_dates(frag, default_year)
            if not dates:
                continue
            for name, d in _assign(kind, dates, frag):
                events.append({"kind": name, "date": d.isoformat(), "raw": frag[:80]})

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
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(store, fp, ensure_ascii=False, indent=1)
    logging.info("考试日历已保存：%d 个时间点", len(store["events"]))


# ============================== 主流程 ==============================

def _worth_parsing(title):
    if any(w in title for w in config.DETAIL_SKIP):
        return False
    return any(w in title for w in config.DETAIL_HINTS)


def _candidates(all_items, store):
    """挑出值得抓正文的公告，新的、近期的排前面。"""
    parsed = store.get("parsed", {})
    today = bj_today()
    picked = []
    for it in all_items:
        url, title = it["url"], it["title"]
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
    """抓公告正文、抽时间点、写进日历。返回新解析的公告条数。"""
    picked = _candidates(all_items, store)[: config.DETAIL_FETCH]
    if not picked:
        logging.info("没有需要解析正文的公告")
        return 0

    today = bj_today()
    added = 0
    for age, year, it in picked:
        url, title = it["url"], it["title"]
        try:
            raw, size = http_get(url, timeout=max(config.REQUEST_TIMEOUT, 20))
        except Exception as exc:
            logging.warning("解析正文失败（%s）：%s", title[:20], exc)
            continue

        events = parse_schedule(title, _plain_text(raw), year, url)
        store["parsed"][url] = today.isoformat()

        if not events:
            continue
        for ev in events:
            ev.update({"url": url, "title": title,
                       "source": it.get("source", ""),
                       "added": today.isoformat()})
            store["events"].append(ev)
        added += 1
        logging.info("解析到日程：%s → %s", title[:24],
                     "、".join("%s %s" % (e["kind"], e["date"]) for e in events))

    # 手动补充的重要日期（官方大考机器未必抓得到）
    for date_str, kind, title in (config.MANUAL_EVENTS or []):
        store["events"].append({"kind": kind, "date": date_str, "raw": "手动录入",
                                "url": "", "title": title, "source": "手动",
                                "added": today.isoformat()})
    return added


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


def build_reminder(store):
    """生成提醒文案；没什么可提醒的返回 None。"""
    today = bj_today()
    events = [ev for ev in store.get("events", []) if ev.get("url")]

    by_url = {}
    for ev in events:
        by_url.setdefault(ev["url"], []).append(ev)

    # 读不到考试时间的公告（正文只有报名/缴费，考试时间一个字都没写）
    # 整条不发——点进去也是空的。开关见 config.REQUIRE_EXAM_TIME。
    if config.REQUIRE_EXAM_TIME:
        ok = {u for u, evs in by_url.items()
              if any(e.get("kind") in EXAM_TIME_KINDS for e in evs)}
        skipped = len(by_url) - len(ok)
        if skipped:
            logging.info("有 %d 篇公告读不到考试时间，本次不提醒", skipped)
        by_url = {u: evs for u, evs in by_url.items() if u in ok}
    events = [ev for evs in by_url.values() for ev in evs]

    ongoing, upcoming = [], []
    for url, evs in by_url.items():
        start = next((e for e in evs if e["kind"] == "报名开始"), None)
        end = next((e for e in evs if e["kind"] == "报名截止"), None)
        if not (start and end):
            continue
        d_start = datetime.date.fromisoformat(start["date"])
        d_end = datetime.date.fromisoformat(end["date"])
        if d_start <= today <= d_end and (d_end - today).days <= config.ONGOING_WINDOW_DAYS:
            ongoing.append((d_end, url, evs[0]["title"], (d_end - today).days))
    ongoing.sort()

    for ev in events:
        d = datetime.date.fromisoformat(ev["date"])
        days = (d - today).days
        if 0 <= days <= config.REMIND_DAYS:
            upcoming.append((days, d, ev))
    upcoming.sort(key=lambda x: (x[0], x[2]["kind"]))

    if not ongoing and not upcoming:
        return None

    lines = ["**⏰ 考试日程提醒 · %s**" % today.strftime("%m-%d"), ""]

    if ongoing:
        lines.append("**🔥 报名进行中**")
        for d_end, url, title, left in ongoing[: config.REMIND_ONGOING_MAX]:
            tail = "今天截止！" if left == 0 else "还剩 %d 天" % left
            lines.append("- %s（%s 截止）｜ [%s](%s)"
                         % (tail, _fmt(d_end.isoformat()), _short(title), url))
        if len(ongoing) > config.REMIND_ONGOING_MAX:
            lines.append("- …另有 %d 条报名进行中" % (len(ongoing) - config.REMIND_ONGOING_MAX))
        lines.append("")

    if upcoming:
        lines.append("**⏳ 最近 %d 天**" % config.REMIND_DAYS)
        for days, d, ev in upcoming[: config.REMIND_UPCOMING_MAX]:
            label = "%s（%s）" % (_when(days), d.strftime("%m-%d"))
            if ev.get("url"):
                lines.append("- %s ｜ %s ｜ [%s](%s)"
                             % (label, ev["kind"], _short(ev["title"]), ev["url"]))
            else:
                lines.append("- %s ｜ %s ｜ %s" % (label, ev["kind"], _short(ev["title"])))
        if len(upcoming) > config.REMIND_UPCOMING_MAX:
            lines.append("- …另有 %d 条" % (len(upcoming) - config.REMIND_UPCOMING_MAX))
        lines.append("")

    lines.append("共 %d 条 · %s 提醒"
                 % (len(ongoing) + len(upcoming),
                    time.strftime("%H:%M", time.gmtime(time.time() + 8 * 3600))))
    return "\n".join(lines)
