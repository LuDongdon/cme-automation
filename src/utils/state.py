"""工作流状态持久化 — 崩溃恢复支持

在每个关键步骤前写入 data/workflow_state.json，
崩溃后重启可从断点恢复。
"""

import json
import os
import logging
from datetime import datetime

logger = logging.getLogger("cme")

STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "workflow_state.json"
)


class WorkflowState:
    """工作流状态保存/加载/清除。

    状态字段:
        phase: "course_creation" | "attendance" | "done"
        mode: "full" | "course_only" | "attend_only"
        current_index: int       # course_creation 中当前处理的课程索引
        total_courses: int       # 计划中的课程总数
        completed_course_ids: [int]  # 已成功创建的课程 ID（yaml id）
        course_config: dict      # 表单数据快照
        attendance_phase: str | null  # "sign_in" | "waiting_sign_out" | "sign_out"
        qr_screenshot_index: int      # 最后保存的 QR 截图序号
        last_updated: str        # ISO datetime
    """

    @staticmethod
    def save(state_dict):
        """原子写入状态文件。"""
        state_dict["last_updated"] = datetime.now().isoformat()
        tmp_path = STATE_FILE + ".tmp"
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state_dict, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, STATE_FILE)  # atomic on Windows
            logger.debug(f"工作流状态已保存: phase={state_dict.get('phase')}")
        except Exception as e:
            logger.warning(f"保存工作流状态失败: {e}")

    @staticmethod
    def load():
        """加载状态文件，不存在或损坏返回 None。"""
        if not os.path.exists(STATE_FILE):
            return None
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"读取工作流状态失败: {e}")
            return None

    @staticmethod
    def clear():
        """删除状态文件（工作流正常完成时调用）。"""
        try:
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
                logger.debug("工作流状态已清除")
        except OSError as e:
            logger.warning(f"清除工作流状态失败: {e}")

    @staticmethod
    def has_incomplete():
        """检查是否存在未完成的工作流。"""
        state = WorkflowState.load()
        if state is None:
            return False
        phase = state.get("phase", "")
        return phase not in ("", "done")
