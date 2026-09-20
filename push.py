# -*- coding: utf-8 -*-
"""企业微信群机器人推送。只有这一个渠道，够用且最省事。

Webhook 地址从环境变量 WEWORK_WEBHOOK 读，不要写进代码里。
企业微信 markdown 消息正文上限 4096 字节，所以发之前会做长度校验。
"""

import json
import logging
import random
import time
import urllib.request

from config import WEWORK_MSG_TYPE, WEWORK_WEBHOOK

MAX_BYTES = 4000  # 留点余量


class PushError(Exception):
    pass


def _post(webhook, payload, timeout=10):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"errcode": -1, "errmsg": body[:200]}


def _truncate(text, limit=MAX_BYTES):
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore")


def push(content, is_success=True, webhook=None, msg_type=None):
    """推送一条消息，返回是否成功。推送失败不抛异常，交由调用方决定怎么处理。"""
    webhook = webhook or WEWORK_WEBHOOK
    msg_type = (msg_type or WEWORK_MSG_TYPE or "markdown").lower()

    if not webhook:
        logging.warning("没配 WEWORK_WEBHOOK，跳过推送。")
        return False

    flag = "✅" if is_success else "❌"
    if msg_type == "text":
        content = _truncate(f"{flag} {content}")
        payload = {"msgtype": "text", "text": {"content": content}}
    else:
        content = _truncate(content)
        payload = {"msgtype": "markdown", "markdown": {"content": content}}

    for attempt in range(3):
        try:
            res = _post(webhook, payload)
            errcode = res.get("errcode", -1)
            if errcode == 0:
                logging.info("企业微信推送成功。")
                return True
            # 45009: 接口调用超过限制
            logging.error("企业微信返回错误：errcode=%s errmsg=%s", errcode, res.get("errmsg"))
            if errcode in (45009, -1) and attempt < 2:
                time.sleep(random.randint(5, 15))
                continue
            return False
        except Exception as exc:
            logging.error("企业微信推送异常（第 %d 次）：%s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(random.randint(5, 15))
    return False


def safe_push(content, is_success=True):
    """推送包装：推送自己挂了也不能影响主流程。"""
    try:
        return push(content, is_success=is_success)
    except Exception as exc:
        logging.error("推送过程出错（已忽略）：%s", exc)
        return False
