"""微信发送编排模块"""

import logging
from .controller import send_image_to_group, send_image_with_text
from .fallback import save_for_manual_send

logger = logging.getLogger("cme")


def send_qr_screenshot(image_path, group_name, auto_mode=True, text_msg=None):
    """
    将二维码截图发送到微信群（可选附带文字说明）。

    Args:
        image_path: 截图文件路径
        group_name: 微信群名称
        auto_mode: True=自动发送, False=仅保存
        text_msg: 可选的文字说明（在图片之前发送）

    Returns:
        bool: 是否成功
    """
    if not auto_mode:
        return save_for_manual_send(image_path, group_name)

    if text_msg:
        success = send_image_with_text(image_path, group_name, text_msg)
    else:
        success = send_image_to_group(image_path, group_name)

    if not success:
        logger.warning(f"自动发送失败，保存到手动发送目录")
        return save_for_manual_send(image_path, group_name)

    return True


def send_to_multiple_groups(image_path, groups, auto_mode=True):
    """发送到多个微信群"""
    results = []
    for group in groups:
        result = send_qr_screenshot(image_path, group, auto_mode)
        results.append({"group": group, "success": result})
    return results
