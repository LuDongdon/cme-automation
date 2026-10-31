"""微信发送回退方案 — 保存截图供手动发送"""

import os
import shutil
import logging
from datetime import datetime

logger = logging.getLogger("cme")


def save_for_manual_send(image_path, group_name, fallback_dir=None):
    """
    保存截图副本供手动发送。

    注意：二维码截图已由 qr_manager 自动保存到 Desktop/二维码截图/{日期}/，
    此函数仅复制到手动发送目录作为提醒。

    Args:
        image_path: 源截图路径（已在二维码目录中）
        group_name: 目标群名（用于文件命名）
        fallback_dir: 回退目录，默认 Desktop/待发送截图/

    Returns:
        str: 保存的目标路径
    """
    if fallback_dir is None:
        fallback_dir = os.path.join(
            os.path.expanduser("~"), "Desktop", "待发送截图"
        )

    os.makedirs(fallback_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"[{group_name}]{timestamp}.png"
    dest_path = os.path.join(fallback_dir, filename)

    try:
        shutil.copy2(image_path, dest_path)
        logger.info(f"手动发送副本已保存到: {dest_path}")
        return dest_path
    except Exception as e:
        logger.warning(f"保存手动发送副本失败: {e}")
        return image_path
