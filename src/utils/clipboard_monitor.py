"""Windows 剪贴板监控 — 自动检测医教管理验证码短信

当登录流程需要验证码时，后台线程轮询剪贴板。
剪贴板内容变化后自动提取 6 位验证码，无需用户手动输入。

匹配策略（兼容任意短信措辞）：
1. 优先：验证码类关键词（校验码/验证码/动态码/安全码/确认码/激活码/code 等）
   后跟的 6 位数字，如 "校验码：549732"、"本次验证码是 123456"。
2. 兜底：任意"独立"的 6 位数字（前后都不是数字，排除从手机号等更长数字串里截取）。

短信格式示例: 【医教管理】尊敬的用户：您的校验码：549732，工作人员不会索取，请勿泄漏。
"""

import re
import time
import threading
import logging

logger = logging.getLogger("cme")

# 优先：验证码类关键词后跟 6 位数字（[^\d]{0,8}? 兼容冒号/空格/中文"是/为"等分隔与措辞，
# 上限 8 个非数字字符，避免跨过长文本乱配；(?!\d) 保证该 6 位独立、不是更长数字串的一部分）
_KEYWORD_PATTERN = re.compile(
    r"(?:校验码|验证码|动态码|安全码|确认码|激活码|短信验证码|验证码是|code)[^\d]{0,8}?(\d{6})(?!\d)",
    re.IGNORECASE,
)

# 兜底：任意"独立"的 6 位数字（前后都不是数字）
_CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# 全角数字 → 半角（部分短信使用全角数字）
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def get_clipboard_text():
    """通过 win32clipboard 读取剪贴板文本内容。
    项目中已有 pywin32（pywinauto 的依赖），无需额外安装。

    Returns:
        str | None: 剪贴板中的文本，读取失败返回 None
    """
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            return None
        finally:
            win32clipboard.CloseClipboard()
    except ImportError:
        # fallback: 如果 pywin32 不可用，打印警告
        logger.debug("win32clipboard 不可用，尝试使用 subprocess")
        return _get_clipboard_via_powershell()
    except Exception as e:
        logger.debug(f"剪贴板读取异常: {e}")
        return None


def _get_clipboard_via_powershell():
    """备选方案：通过 PowerShell 读取剪贴板"""
    import subprocess
    try:
        result = subprocess.run(
            ["powershell", "-Command", "Get-Clipboard"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return None


def extract_verify_code(text):
    """从剪贴板文本中提取 6 位验证码数字，兼容任意短信措辞。

    匹配策略：
    1. 优先取验证码类关键词（校验码/验证码/动态码/安全码/确认码/激活码/code 等）
       附近的 6 位数字，如 "校验码：549732"、"本次验证码是 123456"。
    2. 找不到关键词时，退回取任意"独立"的 6 位数字（前后都不是数字，
       避免从手机号等更长数字串中误截）。

    Args:
        text: 剪贴板文本内容

    Returns:
        str | None: 提取到的验证码，未匹配返回 None
    """
    if not text:
        return None
    # 全角数字归一化为半角
    text = text.translate(_FULLWIDTH_DIGITS)
    m = _KEYWORD_PATTERN.search(text)
    if m:
        return m.group(1)
    m = _CODE_PATTERN.search(text)
    if m:
        return m.group(1)
    return None


def start_clipboard_monitor(on_code_found):
    """启动剪贴板监控后台线程。

    后台线程每 0.8 秒轮询剪贴板，检测到内容变化且匹配到
    医教管理验证码格式时调用 on_code_found(code)。

    Args:
        on_code_found: callable(code) — 检测到验证码时的回调

    Returns:
        callable: 停止监控的回调函数，调用后线程会在下一次轮询时退出
    """
    stop_event = threading.Event()

    def _monitor():
        last_text = None
        while not stop_event.is_set():
            try:
                text = get_clipboard_text()
                if text and text != last_text:
                    last_text = text
                    code = extract_verify_code(text)
                    if code:
                        logger.info(f"📋 剪贴板检测到验证码: {code}")
                        on_code_found(code)
                        break
            except Exception as e:
                logger.debug(f"剪贴板轮询异常: {e}")
            time.sleep(0.8)
        logger.debug("剪贴板监控线程退出")

    t = threading.Thread(target=_monitor, daemon=True, name="clipboard-monitor")
    t.start()

    def stop():
        stop_event.set()

    return stop
