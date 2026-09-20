# -*- coding: utf-8 -*-
"""模拟推送：真抓一遍，把两条消息的内容打印出来——不推送、不动 state/。

用法（在项目目录下跑）：
    python simulate.py                  # 按当前索引，看"下一次会推什么"
    python simulate.py --empty-index    # 假装索引是空的，看首次运行的完整清单
    python simulate.py --no-schedule    # 只看「每日时政」那条
    python simulate.py --day today      # 强制看"当天"（默认按当前时间自动判断）
    python simulate.py -o out.txt       # 顺手写进文件

state 用临时目录里的副本，所以跑完仓库是干净的（不会把预览当成"已推送"）。
"""

import argparse
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import config  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="模拟推送（只打印，不发送）")
    ap.add_argument("--empty-index", action="store_true",
                    help="把索引当成空的（相当于首次运行）")
    ap.add_argument("--no-schedule", action="store_true", help="不显示日程提醒")
    ap.add_argument("--day", choices=["auto", "today", "yesterday"], default="auto",
                    help="时政清单看哪一天（默认按当前时间自动判断）")
    ap.add_argument("-o", "--out", default="", help="把结果写进这个文件")
    args = ap.parse_args()

    # ---- 用临时 state，避免污染仓库（不然预览会被当成"已推送"）----
    tmp = tempfile.mkdtemp(prefix="gk-preview-")
    state_dir = os.path.join(HERE, "state")
    for name in ("seen.json", "exams.json"):
        src = os.path.join(state_dir, name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(tmp, name))
    config.STATE_FILE = os.path.join(tmp, "seen.json")
    config.SCHEDULE_FILE = os.path.join(tmp, "exams.json")

    import exam_dates
    import main
    from fetcher import fetch_all
    from state import load_state

    if args.day != "auto":
        config.NEWS_DAY = args.day

    if args.empty_index:
        seen = set()
        print("【索引】按空索引预览（相当于首次运行）")
    else:
        seen = set(load_state().get("seen", []))

    all_items, failed = fetch_all(config.SOURCES)
    print("【抓取】%d 条候选，%d 个源（失败：%s）"
          % (len(all_items), len(config.SOURCES), "、".join(failed) or "无"))

    out = []

    # ---- ① 考试日程提醒 ----
    if not args.no_schedule:
        store = exam_dates.load_store()
        n = exam_dates.update_calendar(all_items, store)
        reminder = exam_dates.build_reminder(store)
        print("【日程】新解析 %d 篇公告，日历里可提醒 %d 条" % (n, len(store.get("events", []))))
        out.append("────────── 消息①  ⏰ 考试日程提醒（只在早上推）──────────")
        out.append(reminder or "（日历里暂时没有写明了时间的考试公告）")
        out.append("")

    # ---- ② 每日时政 ----
    news = main.pick_news(all_items, seen)
    print("【时政】目标日 %s（%s），本次 %d 条"
          % (main._news_target_day(), main._news_day_label(), len(news)))
    out.append("────────── 消息②  📰 %s（早晚各一条）──────────" % config.REPORT_TITLE)
    out.append(main.build_message(news))

    text = "\n".join(out)
    print()
    print(text)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fp:
            fp.write(text + "\n")
        print("\n【已写入】%s" % args.out)

    shutil.rmtree(tmp, ignore_errors=True)
    return True


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
