"""定时调度模块 — 签到/签退时间触发"""

import logging
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger("cme")


class AttendanceScheduler:
    """签到/签退定时调度器"""

    def __init__(self):
        self._sign_in_job = None
        self._sign_out_job = None
        self._running = False
        self._thread = None

    def schedule(self, sign_in_time_str, sign_out_time_str, callback):
        """
        设置签到和签退的定时任务。

        Args:
            sign_in_time_str: 签到时间 "HH:MM" 格式
            sign_out_time_str: 签退时间 "HH:MM" 格式
            callback: 回调函数 callback(mode) 其中 mode 为 "签到" 或 "签退"
        """
        now = datetime.now()
        today = now.date()

        sign_in_h, sign_in_m = map(int, sign_in_time_str.split(":"))
        sign_out_h, sign_out_m = map(int, sign_out_time_str.split(":"))

        sign_in_time = datetime(today.year, today.month, today.day,
                                 sign_in_h, sign_in_m)
        sign_out_time = datetime(today.year, today.month, today.day,
                                  sign_out_h, sign_out_m)

        logger.info(f"定时任务已设置:")
        logger.info(f"  签到: {sign_in_time.strftime('%Y-%m-%d %H:%M')}")
        logger.info(f"  签退: {sign_out_time.strftime('%Y-%m-%d %H:%M')}")

        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            args=(sign_in_time, sign_out_time, callback),
            daemon=True,
        )
        self._thread.start()

    def _run(self, sign_in_time, sign_out_time, callback):
        """后台线程：根据当前时间决定立即执行还是等待。

        规则：
          - 当前时间 >= 签退时间 → 立即签退
          - 当前时间 >= 签到时间 → 立即签到，然后等待签退
          - 当前时间 <  签到时间 → 等待到签到时间再签到，然后签退
        """
        now = datetime.now()

        # ================================================================
        # 情况1: 已过签退时间 → 立即签退
        # ================================================================
        if now >= sign_out_time:
            logger.info(
                f"当前时间 {now.strftime('%H:%M')} >= 签退时间 "
                f"{sign_out_time.strftime('%H:%M')}，立即开始签退"
            )
            self._sign_in_job = True   # 跳过签到（已过时）
            self._sign_out_job = True
            callback("签退")
            return

        # ================================================================
        # 情况2: 已在签到时间范围内 → 立即签到，然后等签退
        # ================================================================
        if now >= sign_in_time:
            late_minutes = int((now - sign_in_time).total_seconds() / 60)
            logger.info(
                f"当前时间 {now.strftime('%H:%M')} >= 签到时间 "
                f"{sign_in_time.strftime('%H:%M')}（晚 {late_minutes} 分钟），立即开始签到"
            )
            self._sign_in_job = True
            callback("签到")

            # 签到完成后，等待签退时间
            if sign_out_time > now:
                wait_secs = (sign_out_time - datetime.now()).total_seconds()
                if wait_secs > 0:
                    logger.info(
                        f"签到完成，等待签退时间 "
                        f"{sign_out_time.strftime('%H:%M')}（剩余 {wait_secs/60:.0f} 分钟）"
                    )
                    self._sleep_until(sign_out_time)
                if self._running:
                    logger.info("=== 触发签退 ===")
                    self._sign_out_job = True
                    callback("签退")
            return

        # ================================================================
        # 情况3: 还没到签到时间 → 等待
        # ================================================================
        wait_secs = (sign_in_time - now).total_seconds()
        logger.info(
            f"等待签到时间 {sign_in_time.strftime('%H:%M')}"
            f"（剩余 {wait_secs/60:.0f} 分钟）"
        )
        self._sleep_until(sign_in_time)

        if not self._running:
            return

        logger.info("=== 触发签到 ===")
        self._sign_in_job = True
        callback("签到")

        # 签到完成后，等待签退
        if sign_out_time > datetime.now():
            wait_secs = (sign_out_time - datetime.now()).total_seconds()
            if wait_secs > 0:
                logger.info(
                    f"签到完成，等待签退时间 "
                    f"{sign_out_time.strftime('%H:%M')}（剩余 {wait_secs/60:.0f} 分钟）"
                )
                self._sleep_until(sign_out_time)

        if self._running:
            logger.info("=== 触发签退 ===")
            self._sign_out_job = True
            callback("签退")

    def _sleep_until(self, target_time):
        """分段 sleep 直到目标时间，期间检查 _running 标志"""
        while self._running:
            remaining = (target_time - datetime.now()).total_seconds()
            if remaining <= 0:
                return
            # 每 10 秒醒来检查一次，避免 stop() 长时间无响应
            time.sleep(min(10, remaining))

    def stop(self):
        """停止调度"""
        self._running = False
        logger.info("调度器已停止")

    def get_status(self):
        """返回当前状态"""
        return {
            "running": self._running,
            "sign_in_done": self._sign_in_job is not None,
            "sign_out_done": self._sign_out_job is not None,
        }


def wait_until(target_time_str, buffer_minutes=0):
    """
    等待直到指定时间。

    Args:
        target_time_str: "HH:MM" 格式
        buffer_minutes: 提前多少分钟（负数=延后）
    """
    now = datetime.now()
    h, m = map(int, target_time_str.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)

    if buffer_minutes:
        target = target + timedelta(minutes=buffer_minutes)

    # 如果目标时间已过，设为明天
    if target < now:
        target = target + timedelta(days=1)

    wait_seconds = (target - now).total_seconds()
    if wait_seconds > 0:
        logger.info(f"等待至 {target.strftime('%H:%M:%S')} (剩余 {wait_seconds:.0f} 秒)...")
        time.sleep(wait_seconds)
