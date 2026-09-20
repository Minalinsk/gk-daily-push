# -*- coding: utf-8 -*-
"""去重索引：把推送过的链接存下来，实现"只推新内容"。

索引文件会由 GitHub Actions 自动提交回仓库，所以每一轮都有记录。
"""

import json
import logging
import os
import time

import config


def load_state(path=None):
    path = path or config.STATE_FILE
    if not os.path.exists(path):
        return {"last_run": "", "seen": []}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            raise ValueError("状态文件格式不对")
        data.setdefault("last_run", "")
        data.setdefault("seen", [])
        return data
    except Exception as exc:
        logging.warning("状态文件读取失败，按空索引处理：%s", exc)
        return {"last_run": "", "seen": []}


def save_state(state, path=None):
    path = path or config.STATE_FILE
    # 控制索引长度，先进先出，避免文件无限膨胀
    seen = state.get("seen", [])
    if len(seen) > config.STATE_MAX:
        seen = seen[-config.STATE_MAX:]
    state["seen"] = seen
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=1)
    logging.info("索引已保存，累计 %d 条", len(seen))
