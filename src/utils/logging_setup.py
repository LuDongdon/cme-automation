"""日志模块 — 同时输出到文件和控制台"""

import os
import sys
import logging
from datetime import datetime


def setup_logging(level="INFO", log_dir=None):
    """初始化日志系统，返回 logger 和 log_stream（用于 SSE 推送）"""

    if log_dir is None:
        from .config import PROJECT_ROOT
        log_dir = os.path.join(PROJECT_ROOT, "logs")

    os.makedirs(log_dir, exist_ok=True)

    # 日志格式
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)-5s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 根 logger
    logger = logging.getLogger("cme")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    # 文件 handler — 按日期命名
    today = datetime.now().strftime("%Y-%m-%d")
    file_handler = logging.FileHandler(
        os.path.join(log_dir, f"{today}.log"),
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # 用于 Web UI SSE 推送的内存流
    log_stream = _LogStream()
    stream_handler = logging.StreamHandler(log_stream)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger, log_stream


class _LogStream:
    """可迭代的日志流，供 SSE 使用"""

    def __init__(self, max_lines=500):
        self._lines = []
        self._max_lines = max_lines
        self._listeners = []

    def write(self, message):
        msg = message.strip()
        if msg:
            self._lines.append(msg)
            if len(self._lines) > self._max_lines:
                self._lines = self._lines[-self._max_lines:]
            for listener in self._listeners:
                try:
                    listener(msg)
                except Exception:
                    pass

    def flush(self):
        pass

    def get_recent(self, count=100):
        return self._lines[-count:]

    def add_listener(self, callback):
        self._listeners.append(callback)

    def remove_listener(self, callback):
        if callback in self._listeners:
            self._listeners.remove(callback)


def get_logger():
    """获取项目 logger 实例"""
    return logging.getLogger("cme")
