# -*- coding: utf-8 -*-
"""日志工具：统一日志格式，顺便挂一个内存缓冲，方便出错时把尾部日志一起推出去。"""

import logging
import sys
from collections import deque

_BUFFER = deque(maxlen=200)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            _BUFFER.append(self.format(record))
        except Exception:
            pass


def setup_logging(level=logging.INFO):
    root = logging.getLogger()
    if root.handlers:
        return root

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    buf = _BufferHandler()
    buf.setFormatter(fmt)
    root.addHandler(buf)

    root.setLevel(level)
    return root


def tail_logs(n=30):
    """取最近 n 行日志，供失败推送时附带。"""
    lines = list(_BUFFER)[-n:]
    return "\n".join(lines)
