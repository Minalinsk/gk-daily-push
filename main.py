# -*- coding: utf-8 -*-
"""每日时政 + 公考公告推送 —— 主程序

流程：抓取 → 去重（只留没推过的）→ 组装消息 → 企业微信推送 → 更新索引
任何环节出错都会推一条失败通知，不会静默死掉。
"""

import logging
import re
import time
import traceback

import config
import exam_dates
from fetcher import fetch_all
from log_utils import setup_logging, tail_logs
from push import safe_push
from state import load_state, save_state

MD_ESCAPE = str.maketrans({"[": "（", "]": "）"})

BJ_OFFSET = 8 * 3600   # 服务器跑在 UTC，消息里统一显示北京时间


def bj_now():
    return time.gmtime(time.time() + BJ_OFFSET)



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


def eligible_by_source(all_items, seen_set):
    """过滤 + 去重，把候选按源分组。返回 {源: [条目...]}，保持页面出现顺序。

    只做筛选不做挑选，所以拿到的是一份"完整候选池"——首次运行建索引时要用它。
    """
    by_source, used_url, used_title = {}, set(), set()

    for it in all_items:
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


def pick_new(all_items, seen_set):
    """从候选池里挑出"这次要推的新条目"。

    关键点：用「按源轮转」的方式挑选，而不是从头顺着拿。
    否则时政源条数多，会把公考公告全挤掉——而公告恰恰是最不该漏的。
    """
    by_source = eligible_by_source(all_items, seen_set)

    # 轮转挑选：每个源先各拿 1 条，再各拿第 2 条，直到到上限
    final, idx = [], 0
    while len(final) < config.MAX_TOTAL:
        added = False
        for queue in by_source.values():
            if idx < len(queue) and idx < config.MAX_PER_SOURCE:
                final.append(queue[idx])
                added = True
                if len(final) >= config.MAX_TOTAL:
                    break
        if not added:
            break
        idx += 1

    # 按源归拢，让消息里同一个源的内容连在一起
    order = {name: i for i, name in enumerate(by_source.keys())}
    final.sort(key=lambda x: order.get(x.get("source", ""), 999))
    return final


def build_message(items):
    now = bj_now()
    date_str = time.strftime("%m-%d", now)
    lines = [f"**📰 {config.REPORT_TITLE} · {date_str}**", ""]

    current, shown, budget = None, 0, 3900
    for it in items:
        block = []
        if it.get("source", "") != current:
            current = it["source"]
            block.append(f"**{current}**")
        block.append(f"- [{_clean_title(it['title'])}]({it['url']})")

        chunk = "\n".join(block) + "\n"
        if len("\n".join(lines).encode("utf-8")) + len(chunk.encode("utf-8")) > budget:
            logging.warning("消息接近长度上限，后面的条目被截断（共 %d 条未展示）",
                            len(items) - shown)
            break
        lines.extend(block)
        shown += 1

    lines.append("")
    lines.append(f"共 {shown} 条 · {time.strftime('%H:%M', now)} 推送")
    return "\n".join(lines)


def _send(text, is_success=True, tag="消息"):
    """统一的推送出口：DRY_RUN 时只写日志，不真发。

    注意 DRY_RUN 下调用方也不能写索引——否则"预览"会把内容标记成已推送，
    等真正运行时就什么都推不出去了。
    """
    if config.DRY_RUN:
        logging.info("（DRY_RUN）%s如下，未发送：\n%s", tag, text)
        return True
    return safe_push(text, is_success=is_success)


def _schedule_step(all_items):
    """抓公告正文、抽关键时间点，然后推一条日程提醒。

    这一块独立于"只推新的"逻辑：日历是累积的，就算今天没有新公告，
    只要临近报名截止或笔试，也会提醒。出错不影响主流程。
    """
    if not config.SCHEDULE_ENABLED:
        return
    try:
        store = exam_dates.load_store()
        n = exam_dates.update_calendar(all_items, store)
        exam_dates.save_store(store)
        if n:
            logging.info("本次新解析了 %d 篇公告的日程", n)

        msg = exam_dates.build_reminder(store)
        if not msg:
            logging.info("近期没有需要提醒的考试日程")
            return
        _send(msg, is_success=True, tag="日程提醒")
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
            f"每天北京时间 07:00 自动推送（随机延迟 0~20 分钟）。",
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

        all_items, failed = fetch_all(config.SOURCES)
        logging.info("共抓到 %d 条候选", len(all_items))

        if not all_items:
            raise RuntimeError(
                "所有源都没抓到内容，可能是网络被拦或站点改版，请检查源配置。"
            )

        new_items = pick_new(all_items, seen_set)
        logging.info("其中没推过的 %d 条", len(new_items))

        # 先推日程提醒（最有时效性），再推今天的资讯清单
        _schedule_step(all_items)

        # 首次运行只建索引，避免一口气把几百条糊你脸上
        if first_run and not config.FIRST_RUN_PUSH:
            # 注意：这里记的是"全部候选"，不是 pick_new 挑出来的那几条。
            # 公告类源一页就有几百条候选，如果只记 24 条，剩下的会在之后
            # 十来天里被当成"新内容"陆续推出来，等于给你补一星期旧闻。
            all_new = [it for queue in eligible_by_source(all_items, seen_set).values()
                       for it in queue]
            if config.DRY_RUN:
                logging.info("（DRY_RUN）首次运行，本应建立索引 %d 条，已跳过", len(all_new))
            else:
                state["seen"] = seen + [it["url"] for it in all_new]
                save_state(state)
                _send(
                    f"**✅ {config.REPORT_TITLE} 已就绪**\n"
                    f"首次运行已建立索引（{len(all_new)} 条），从明天起只推新增内容。",
                    is_success=True,
                )
            return True

        if not new_items:
            save_state(state)  # 刷新 last_run，顺便让仓库保持活跃
            _send(f"**📭 {config.REPORT_TITLE}**\n今日没有新增内容。", is_success=True)
            return True

        msg = build_message(new_items)
        ok = _send(msg, is_success=True, tag="资讯清单")

        if ok and not config.DRY_RUN:
            state["seen"] = seen + [it["url"] for it in new_items]
            logging.info("已记录 %d 条新链接进索引", len(new_items))
        elif config.DRY_RUN:
            logging.info("DRY_RUN：这 %d 条不记入索引，下次仍会推出", len(new_items))
        else:
            # 推送失败就不记索引，下次还会重试，不会丢内容
            logging.warning("推送未成功，本次条目不记入索引，下次会重推。")

        save_state(state)
        if failed:
            logging.warning("（本次这些源没数据：%s）", "、".join(failed))
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
