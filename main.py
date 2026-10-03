# -*- coding: utf-8 -*-
"""每日时政 + 公考公告推送 —— 主程序

两条消息、两套逻辑：
  ① 考试日程提醒 —— 公告源里的"报名/考试时间"，做成一张时间表（公告维度，累积着用）
  ② 每日资讯清单 —— 时政源里前一天的热门时政
流程：抓取 → 去重 → 组装消息 → 企业微信推送 → 更新索引
任何环节出错都会推一条失败通知，不会静默死掉。
"""

import calendar
import datetime
import logging
import re
import time
import traceback

import config
import exam_dates
import topics
from fetcher import fetch_all, url_date
from log_utils import setup_logging, tail_logs
from push import safe_push
from state import load_state, save_state

MD_ESCAPE = str.maketrans({"[": "（", "]": "）"})

BJ_OFFSET = 8 * 3600   # 服务器跑在 UTC，消息里统一显示北京时间


def bj_now():
    return time.gmtime(time.time() + BJ_OFFSET)


def _epoch_of_bj_today(hhmm):
    """北京"今天 HH:MM"对应的 UTC epoch；解析不出来返回 None。"""
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
    except Exception:
        return None
    t = bj_now()                     # 北京的"现在"（拿它的年月日）
    # 用北京的年月日 + 目标时分凑一个时刻，再减掉 8 小时偏移
    return calendar.timegm((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, 0)) - BJ_OFFSET


def _wait_until_bj(hhmm, max_wait=6 * 3600):
    """等到北京时间的 HH:MM 再往下走；已经过了就直接返回。

    为什么这个等待放在脚本里、而不是再加一个 job：两条消息共用同一次抓取；
    而且 state 只由这一个进程提交，不会出现两个 job 抢着 push 的麻烦。
    """
    if not hhmm:
        return
    target = _epoch_of_bj_today(hhmm)
    if target is None:
        logging.warning("NEWS_AT_BJ 的值看不懂（%r），跳过等待", hhmm)
        return
    gap = target - time.time()
    if gap <= 0:
        logging.info("已过北京 %s，时政不再等待，直接发", hhmm)
        return
    if gap > max_wait:
        # 保险：万一算出了隔天（或者 cron 被改乱了），别在这儿干等几小时
        logging.warning("距北京 %s 还有 %.0f 分钟，超过 %.0f 分钟上限，不等了",
                        hhmm, gap / 60, max_wait / 60)
        return
    logging.info("等到北京 %s 再推时政（还需 %.0f 分 %.0f 秒）", hhmm, gap // 60, gap % 60)
    time.sleep(gap)
    logging.info("到点，继续推时政")


def _clean_title(title):
    title = title.translate(MD_ESCAPE)
    if len(title) > config.TITLE_MAX:
        title = title[: config.TITLE_MAX - 1] + "…"
    return title


def _keep(title):
    """按关键词规则判断这条要不要"""
    if len(re.sub(r"\s", "", title)) < config.MIN_TITLE_LEN:
        return False
    if any(w and w in title for w in config.EXCLUDE_KEYWORDS):
        return False
    if config.INCLUDE_KEYWORDS:
        if not any(w and w in title for w in config.INCLUDE_KEYWORDS):
            return False
    return True


def eligible_by_source(all_items, seen_set, kind=None):
    """过滤 + 去重，把候选按源分组。返回 {源: [条目...]}，保持页面出现顺序。

    kind 用来只取某一类源："news"=时政源（进每日清单），
    "announce"=公告源（走日程提醒那条线，不进清单）。
    """
    by_source, used_url, used_title = {}, set(), set()

    for it in all_items:
        if kind and it.get("kind") != kind:
            continue
        title, url = it["title"], it["url"]
        if url in seen_set or url in used_url:
            continue
        if not _keep(title):
            continue
        # 同一篇稿子常常在多个源里重复，按标题再挡一道
        key = re.sub(r"[\s\W]", "", title)[:24]
        if key in used_title:
            continue

        used_url.add(url)
        used_title.add(key)
        src = it.get("source", "")
        by_source.setdefault(src, []).append(it)

    return by_source


def _pub_date(it):
    """条目 URL 里的发布日期，读不出返回 None。"""
    d = url_date(it["url"])
    if not d:
        return None
    try:
        return datetime.date(d.tm_year, d.tm_mon, d.tm_mday)
    except ValueError:
        return None


def _news_target_day():
    """这次清单该看哪一天。

    正常是**前一天**（凌晨运行时，当天还几乎没有稿子）。
    config.NEWS_DAY 设了 "today"/"yesterday" 就按它来 —— workflow 的定时那次固定给
    "yesterday"；手动触发给 "auto"，才走下面这条按北京时间猜的兜底逻辑。
    """
    today = exam_dates.bj_today()
    if config.NEWS_DAY == "today":
        return today
    if config.NEWS_DAY == "yesterday":
        return today - datetime.timedelta(days=1)
    if config.NEWS_WINDOW_AUTO and bj_now().tm_hour >= config.NEWS_EVENING_HOUR:
        return today
    return today - datetime.timedelta(days=1)


def _news_day_label():
    """「今天」还是「前一天」——只用来写日志和空消息文案。"""
    return "今天" if _news_target_day() == exam_dates.bj_today() else "前一天"


def pick_news(all_items, seen_set):
    """每日资讯清单：只挑时政源里目标那一天的条目（正常就是前一天）。

    某个源那一天没更新（一条都挑不出来）时，退而取它最新的几条，
    免得整个源缺席。**公告不在这条清单里**——公告统一走日程提醒。
    """
    by_source = eligible_by_source(all_items, seen_set, kind="news")
    target = _news_target_day()

    queues = []
    for name, queue in by_source.items():
        fresh = [it for it in queue if _pub_date(it) == target]
        queues.append(fresh[: config.MAX_PER_SOURCE] if fresh
                      else queue[: config.NEWS_FALLBACK_MAX])

    # 按源轮转地拿，避免某个源条数多就把别的源挤掉
    final, idx = [], 0
    while len(final) < config.MAX_TOTAL:
        added = False
        for q in queues:
            if idx < len(q):
                final.append(q[idx])
                added = True
                if len(final) >= config.MAX_TOTAL:
                    break
        if not added:
            break
        idx += 1

    # 按源归拢，让消息里同一个源的内容连在一起
    order = {name: i for i, name in enumerate(by_source.keys())}
    final.sort(key=lambda x: order.get(x.get("source", ""), 999))
    return topics.tag_all(final)


# 一条消息里留给标题行的字节（标题可能带 "(1/2)" 后缀，后面还要跟一个空行）
_MSG_HEADER_RESERVE = 90


def _page_header(page, total, now):
    """第 page/total 条消息的标题行。"""
    date_str = time.strftime("%m-%d", now)
    if total <= 1:
        return f"**📰 {config.REPORT_TITLE} · {date_str}**"
    return f"**📰 {config.REPORT_TITLE} · {date_str}（{page}/{total}）**"


def build_messages(items, notice=""):
    """组装「每日时政」，**超长就拆成多条**。返回 [(正文, 本条包含的条数), ...]。

    以前的做法是超长就从尾部截断，被截掉的条目还**不能**记进索引
    （记了就等于永久丢内容）。现在改成拆条：每条都在企业微信的长度上限内，
    内容一条不丢；返回值里的"条数"是给调用方记账的 —— 只有真发出去的那几条才记账。

    企业微信 markdown 正文上限 4096 字节，这里按 config.MSG_BUDGET 分页，
    余量留给标题行、摘要行和 notice。
    """
    now = bj_now()

    # 先摊平成 (源名, 正文行) —— 分页跨了源时，新的一页要重新打一次源标题。
    entries = [(it.get("source", ""),
                f"- {_tag_prefix(it.get('topic', ''))}"
                f"[{_clean_title(it['title'])}]({it['url']})")
               for it in items]

    budget = config.MSG_BUDGET - _MSG_HEADER_RESERVE
    pages, cur, cur_source = [], {"lines": [], "used": 0, "count": 0}, None

    for src, line in entries:
        block = ([f"**{src}**"] if src != cur_source else []) + [line]
        size = len(("\n".join(block) + "\n").encode("utf-8"))

        if cur["lines"] and cur["used"] + size > budget:
            # 这一页装不下了：先收下它，另起一页（新页要重新写源标题）
            pages.append(cur)
            cur = {"lines": [], "used": 0, "count": 0}
            block = [f"**{src}**", line]
            size = len(("\n".join(block) + "\n").encode("utf-8"))

        if size > budget and not cur["lines"]:
            # 单条自己就超预算（超长标题 + 超长 URL 的极端情况）：仍然放进去，
            # 交给 push._truncate 按行兜底，总比直接丢掉强。
            logging.warning("单条内容超过单页预算，仍放入本条消息：%s", line[:40])
        cur["lines"].extend(block)
        cur["used"] += size
        cur["count"] += 1
        cur_source = src

    if cur["lines"] or not pages:
        pages.append(cur)

    total = len(pages)
    total_items = sum(p["count"] for p in pages)
    out = []
    for idx, page in enumerate(pages, 1):
        lines = [_page_header(idx, total, now), ""]
        lines.extend(page["lines"])
        if idx == total:
            lines.append("")
            if notice:
                lines.append(notice)
            lines.append(f"共 {total_items} 条 · {time.strftime('%H:%M', now)} 推送")
        out.append(("\n".join(lines), page["count"]))

    if total > 1:
        logging.info("内容较长，已拆成 %d 条消息发送（合计 %d 条）", total, total_items)
    return out


def _failure_notice(health, threshold=2):
    """把"连续失败"的源拼成一行提示；没有就返回空串。

    阈值取 2 是有意的：偶尔一次的 403 / 超时很常见（对方站点抖一下），不值得惊动；
    **连着两天以上**才说明这个源是真的坏了，得让人知道。
    """
    broken = []
    for name, rec in (health or {}).items():
        try:
            n = int(rec.get("count", 0))
        except (TypeError, ValueError):
            n = 0
        if n >= threshold:
            broken.append((n, name))
    if not broken:
        return ""
    broken.sort(key=lambda x: (-x[0], x[1]))
    shown = "、".join("%s 连续 %d 天" % (name, n) for n, name in broken[:4])
    more = "" if len(broken) <= 4 else " 等共 %d 个源" % len(broken)
    return "⚠️ 抓取异常：%s%s" % (shown, more)


def _tag_prefix(topic):
    """考点标签：只挂一个，挂在标题前面。"""
    if not (config.TOPIC_TAGS and topic):
        return ""
    return f"【{topic}】"


def _send(text, is_success=True, tag="消息"):
    """统一的推送出口：DRY_RUN 时只写日志，不真发。

    注意 DRY_RUN 下调用方也不能写索引——否则"预览"会把内容标记成已推送，
    等真正运行时就什么都推不出去了。
    """
    if config.DRY_RUN:
        logging.info("（DRY_RUN）%s如下，未发送：\n%s", tag, text)
        return True
    return safe_push(text, is_success=is_success)


def _schedule_step(all_items, state, send=True):
    """抓公告正文、抽关键时间点，然后推一条「考试日程提醒」。

    **一天只提醒一遍**，规矩是两条：
      ① 发过就不再发：state["last_schedule"] 记着上次发的是哪一天（北京时间），
         同一天再跑（手动触发、重跑）都会跳过；
      ② 平时只有定时那次负责发（send=True）；万一整天都没发出去，
         下一次运行（send=False）会补一条 —— 总比当天完全没有提醒好。
    这一块独立于"只推新的"逻辑：日历是累积的，今天没有新公告也会照常提醒。
    出错不影响主流程。
    """
    if not config.SCHEDULE_ENABLED:
        return
    try:
        store = exam_dates.load_store()
        n = exam_dates.update_calendar(all_items, store)
        # ⚠️ DRY_RUN 时不能写盘：exams.json 也是"状态"，写下去会让工作区变脏，
        #    而 workflow 里那步 Commit state 是 if: always()，
        #    干跑一次就会平白多出一条"更新已推送索引"的提交。
        #    （日历只在内存里更新，所以下面的提醒文案照样是完整的）
        if config.DRY_RUN:
            logging.info("（DRY_RUN）考试日历本应保存 %d 个时间点，已跳过写盘",
                         len(store.get("events", [])))
        else:
            exam_dates.save_store(store)
        if n:
            logging.info("本次新解析了 %d 篇公告的日程", n)

        today = exam_dates.bj_today().isoformat()
        if state.get("last_schedule") == today:
            logging.info("今天的日程提醒已经发过了，本次跳过（日历照常更新）")
            return
        if not send:
            logging.info("今天还没发过日程提醒，本次补发一条")

        msgs = exam_dates.build_reminders(store)
        if not msgs:
            logging.info("日历里还没有写明了时间的考试公告")
            return
        # 超长会拆成多条，逐条发。任何一条失败就停在那儿、本次不记账 ——
        # 下次运行会**整体重发**，不会出现"只发出去半截"的状态。
        ok = True
        for i, body in enumerate(msgs, 1):
            tag = "日程提醒" if len(msgs) == 1 else "日程提醒%d/%d" % (i, len(msgs))
            if not _send(body, is_success=True, tag=tag):
                ok = False
                logging.warning("日程提醒第 %d/%d 条发送失败，本次不记账，下次整体重发",
                                i, len(msgs))
                break
        if ok and not config.DRY_RUN:
            # 只有真发出去了才记账：发送失败的话，下一轮会补发
            state["last_schedule"] = today
            logging.info("日程提醒已发出（%d 条消息），今天不再重复提醒", len(msgs))
    except Exception as exc:
        logging.error("考试日程模块出错（已忽略）：%s", exc)
        logging.error("堆栈：\n%s", traceback.format_exc())


def main():
    setup_logging()
    logging.info("=" * 46)
    logging.info("每日时政推送开始")
    logging.info("=" * 46)

    # ------- 只发一条测试消息，验证企业微信通道（不抓取、不动索引）-------
    if config.TEST_PUSH:
        ok = safe_push(
            f"**🐾 {config.REPORT_TITLE} · 通道测试**\n"
            f"看到这条说明企业微信机器人配置成功。\n"
            f"当前配置了 {len(config.SOURCES)} 个抓取源，"
            f"每天北京时间 02:30 自动触发（随机延迟 0~15 分钟）。",
            is_success=True,
        )
        logging.info("测试推送结果：%s", "成功" if ok else "失败")
        return ok

    try:
        state = load_state()
        seen = state.get("seen", [])
        seen_set = set(seen)
        first_run = not seen
        logging.info("索引里已有 %d 条历史记录", len(seen))

        all_items, failures = fetch_all(config.SOURCES)
        logging.info("共抓到 %d 条候选", len(all_items))

        # ---- 源健康度 ----
        # 以前源失败只在日志里留一行，于是"山西两个源全挂、中公一直 403"这种
        # 持续好几天的故障，谁都没发现（日志是没人天天翻的）。
        # 现在把"连续失败"记进 state，连续 2 天以上就写进推送消息里。
        today_str = exam_dates.bj_today().isoformat()
        health = state.setdefault("source_health", {})
        for name in list(health):
            if name not in failures:
                del health[name]                  # 恢复了就清零
        for name, reason in failures.items():
            rec = health.setdefault(name, {})
            if rec.get("last") != today_str:      # 同一天跑两次只算一天
                rec["count"] = int(rec.get("count", 0)) + 1
            rec["reason"] = reason
            rec["last"] = today_str
        notice = _failure_notice(health)
        if notice:
            logging.warning("源健康度：%s", notice)

        if not all_items:
            raise RuntimeError(
                "所有源都没抓到内容，可能是网络被拦或站点改版，请检查源配置。"
            )

        new_items = pick_news(all_items, seen_set)
        logging.info("时政里没推过的 %d 条（看的是 %s）", len(new_items), _news_day_label())

        # 先推日程提醒（公告的时间表，一天只推一条），再推时政清单
        _schedule_step(all_items, state, send=config.PUSH_SCHEDULE)

        # 两条消息错开：日程先发，时政等到 config.NEWS_AT_BJ 那个点再发。
        # 抓取已经在上面做完了，时政用的还是同一批数据 —— 隔着半小时，
        # "前一天的要闻"不会因为这个有差别，没必要再抓一遍。
        # DRY_RUN 不等：预览没必要干等半小时。
        if not config.DRY_RUN:
            _wait_until_bj(config.NEWS_AT_BJ)

        # 首次运行只建索引，避免一口气把几百条糊你脸上
        if first_run and not config.FIRST_RUN_PUSH:
            # 注意：这里记的是"全部时政候选"，不是挑出来的那几条。
            # 如果只记 24 条，剩下的会在之后十来天里被当成"新内容"
            # 陆续推出来，等于给你补一星期旧闻。
            # （公告不进这个索引：它靠日历的 parsed 表去重，见 exam_dates.py）
            all_new = [it for queue in eligible_by_source(all_items, seen_set, kind="news").values()
                       for it in queue]
            if config.DRY_RUN:
                logging.info("（DRY_RUN）首次运行，本应建立索引 %d 条，已跳过", len(all_new))
            else:
                state["seen"] = seen + [it["url"] for it in all_new]
                save_state(state)
                ready = (f"**✅ {config.REPORT_TITLE} 已就绪**\n"
                         f"首次运行已建立索引（{len(all_new)} 条时政），从明天起只推新增内容。")
                if notice:
                    ready += f"\n\n{notice}"
                _send(ready, is_success=True)
            return True

        if not new_items:
            save_state(state)  # 内容没变的话它不会写盘，也就不会产生提交
            body = f"**📭 {config.REPORT_TITLE}**\n{_news_day_label()}没有新的时政内容。"
            if notice:
                body += f"\n\n{notice}"
            _send(body, is_success=True)
            return True

        pages = build_messages(new_items, notice)
        sent, ok = 0, True
        for i, (body, cnt) in enumerate(pages, 1):
            tag = "时政清单" if len(pages) == 1 else "时政清单%d/%d" % (i, len(pages))
            if not _send(body, is_success=True, tag=tag):
                ok = False
                logging.warning("时政清单第 %d/%d 条发送失败，停在已成功的前 %d 条",
                                i, len(pages), sent)
                break
            sent += cnt

        if config.DRY_RUN:
            logging.info("DRY_RUN：这 %d 条不记入索引，下次仍会推出", len(new_items))
        else:
            # 发出去多少记多少：没发出去的那部分下次还会推，不会丢内容。
            # （分页是按顺序发的，所以前 sent 条就是已经成功送达的那些。）
            if sent:
                state["seen"] = seen + [it["url"] for it in new_items[:sent]]
                logging.info("已记录 %d 条新链接进索引", sent)
            if not ok:
                logging.warning("有消息没发出去，没发的那部分下次会重推。")

        save_state(state)
        return ok

    except Exception as exc:
        tb = traceback.format_exc()
        logging.error("任务异常：%s", exc)
        logging.error("堆栈：\n%s", tb)
        safe_push(
            f"**❌ {config.REPORT_TITLE} 运行失败**\n"
            f"错误类型：{type(exc).__name__}\n"
            f"错误信息：{str(exc)[:300]}\n\n"
            f"日志尾部：\n{tail_logs(12)[-600:]}",
            is_success=False,
        )
        return False


if __name__ == "__main__":
    try:
        if not main():
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception:
        logging.exception("出现未被 main 捕获的异常")
        try:
            safe_push("任务发生未捕获异常，请查看 Actions 日志。", is_success=False)
        except Exception:
            pass
        raise SystemExit(1)
