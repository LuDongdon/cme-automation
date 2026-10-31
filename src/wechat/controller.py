"""微信桌面客户端控制 — pywinauto 自动化"""

import time
import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger("cme")

# Windows constants
SW_SHOW = 5
SW_HIDE = 0

VK_SHIFT = 0x10
VK_CTRL = 0x11
VK_V = 0x56
VK_F = 0x46
VK_ENTER = 0x0D
VK_ESCAPE = 0x1B

# ---- key helpers ----
_user32 = ctypes.windll.user32


def _dismiss_screensaver():
    """解除屏保 — 委托 src.utils.screen 的可靠实现(点亮显示器+无害按键+验证)"""
    try:
        from ..utils import screen
        screen.wake_display()
        screen.dismiss_screensaver(max_seconds=5)
        logger.info("  屏保唤醒/解除已完成(验证通过)")
    except Exception as e:
        logger.warning(f"  解除屏保失败: {e}")


def _tap(vk):
    """Press and release a single key."""
    _user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    _user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP = 2


def _ctrl_key(vk):
    """Send Ctrl+key combo."""
    _user32.keybd_event(VK_CTRL, 0, 0, 0)
    time.sleep(0.05)
    _user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    _user32.keybd_event(vk, 0, 2, 0)
    time.sleep(0.03)
    _user32.keybd_event(VK_CTRL, 0, 2, 0)


def _activate_wechat_window(silent=False):
    """Find and activate the WeChat window, restoring from tray/minimized if needed.

    Args:
        silent: If True, save the user's current foreground window and restore
                it after operations complete (via _restore_after_send).

    Returns:
        (hwnd, restore_ctx) tuple on success, or (None, None) on failure.
        restore_ctx is a dict with keys needed by _restore_after_send().
        If silent, restore_ctx includes 'original_fg' — the window to switch back to.
    """
    import win32gui, win32con, ctypes

    # 先确保屏幕唤醒/解锁。屏保或锁屏会让 SetForegroundWindow 失败、按键失效,
    # 微信窗口无法激活 → 「进入微信」Enter 无效。
    try:
        from ..utils import screen
        screen.ensure_screen_ready()
    except Exception as e:
        logger.warning(f"  唤醒屏幕失败(继续尝试): {e}")

    restore_ctx = {"was_hidden": False, "was_iconic": False, "original_fg": None}

    # If silent, remember the user's current foreground window
    if silent:
        try:
            restore_ctx["original_fg"] = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            pass

    # Step 1: Find the WeChat window
    hwnd = None
    for title in ["微信", "WeChat"]:
        hwnd = win32gui.FindWindow(None, title)
        if hwnd:
            break

    if not hwnd:
        # Fallback: enumerate all windows looking for "微信" or "WeChat" in title
        found = []

        def enum_cb(h, _):
            try:
                t = win32gui.GetWindowText(h)
                if t and ("微信" in t or "WeChat" in t):
                    found.append(h)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(enum_cb, None)
        if found:
            hwnd = found[0]
            logger.info(f"  EnumWindows 找到微信窗口: '{win32gui.GetWindowText(hwnd)}'")

    if not hwnd:
        logger.warning("  找不到微信窗口句柄（微信可能未运行）")
        return None, None

    # Step 2: Check window state and restore/show as needed
    try:
        is_visible = ctypes.windll.user32.IsWindowVisible(hwnd)
        restore_ctx["was_hidden"] = not bool(is_visible)
    except Exception:
        restore_ctx["was_hidden"] = False

    restore_ctx["was_iconic"] = bool(win32gui.IsIconic(hwnd))

    # Always use SW_SHOW — keybd_event needs the window fully activated.
    # Silent mode handles disruption in _restore_after_send, not during activation.
    if restore_ctx["was_hidden"]:
        logger.info(f"  微信窗口隐藏中（系统托盘），正在恢复...")
        win32gui.ShowWindow(hwnd, SW_SHOW)
        time.sleep(2.0)  # WeChat CEF needs time to fully render after tray restore
    if restore_ctx["was_iconic"]:
        logger.info(f"  微信窗口已最小化，正在还原...")
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.5)

    # Step 3: AllowSetForegroundWindow — grant our process permission to set foreground.
    # On Windows, SetForegroundWindow can fail if our process doesn't have foreground
    # rights (e.g. screen saver active, desktop locked, or background process).
    # ASFW_ANY = 2 tells the system to allow it unconditionally.
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(2)  # ASFW_ANY
    except Exception:
        pass

    # Step 4: AttachThreadInput + SetForegroundWindow for reliable cross-process focus.
    # Without AttachThreadInput, SetForegroundWindow may bring the window to front
    # but not attach keyboard focus — keybd_event keystrokes then land nowhere.
    try:
        import win32process
        our_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        _, wx_tid = win32process.GetWindowThreadProcessId(hwnd)
        if our_tid != wx_tid:
            ctypes.windll.user32.AttachThreadInput(our_tid, wx_tid, True)
        win32gui.SetForegroundWindow(hwnd)
        win32gui.SetFocus(hwnd)
        time.sleep(0.5)
        if our_tid != wx_tid:
            ctypes.windll.user32.AttachThreadInput(our_tid, wx_tid, False)
    except Exception:
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.5)

    # Step 5: Verify activation succeeded.
    fg = ctypes.windll.user32.GetForegroundWindow()

    # When fg=0 (NULL), the system may have screen saver / secure desktop active.
    # Simulate a harmless key tap to dismiss screen saver, then retry.
    if fg == 0:
        logger.info("  检测到前台窗口为空（可能屏保/锁屏），尝试唤醒...")
        _dismiss_screensaver()
        time.sleep(2.0)  # 给屏保足够时间退出

        # 屏保可能在另一个桌面运行，退出后窗口可能重建 → 重新搜索 hwnd
        logger.info("  重新搜索微信窗口...")
        new_hwnd = None
        for title in ["微信", "WeChat"]:
            new_hwnd = win32gui.FindWindow(None, title)
            if new_hwnd:
                break
        if not new_hwnd:
            found = []
            def _enum_cb(h, _):
                try:
                    t = win32gui.GetWindowText(h)
                    if t and ("微信" in t or "WeChat" in t):
                        found.append(h)
                except Exception:
                    pass
                return True
            win32gui.EnumWindows(_enum_cb, None)
            if found:
                new_hwnd = found[0]

        if new_hwnd:
            hwnd = new_hwnd
            logger.info(f"  找到微信窗口: hwnd={hwnd}")
            # 确保窗口可见
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                win32gui.ShowWindow(hwnd, SW_SHOW)
                time.sleep(0.5)
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.5)
            restore_ctx["was_hidden"] = False
            restore_ctx["was_iconic"] = False
        else:
            logger.warning("  屏保唤醒后找不到微信窗口")
            # 继续用原 hwnd 尝试

        try:
            ctypes.windll.user32.AllowSetForegroundWindow(2)
        except Exception:
            pass
        # Retry foreground activation with (possibly new) hwnd
        try:
            import win32process
            our_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            _, wx_tid = win32process.GetWindowThreadProcessId(hwnd)
            if our_tid != wx_tid:
                ctypes.windll.user32.AttachThreadInput(our_tid, wx_tid, True)
            win32gui.SetForegroundWindow(hwnd)
            win32gui.SetFocus(hwnd)
            time.sleep(0.5)
            if our_tid != wx_tid:
                ctypes.windll.user32.AttachThreadInput(our_tid, wx_tid, False)
        except Exception as e:
            logger.warning(f"  屏保唤醒后激活失败: {e}")
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
        fg = ctypes.windll.user32.GetForegroundWindow()

    if fg != hwnd:
        logger.warning(f"  SetForegroundWindow 验证失败: fg={fg}, expected={hwnd}")
        try:
            ctypes.windll.user32.BringWindowToTop(hwnd)
            time.sleep(0.3)
            # Retry AllowSetForegroundWindow before second attempt
            try:
                ctypes.windll.user32.AllowSetForegroundWindow(2)
            except Exception:
                pass
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.5)
            fg = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            pass

        if fg != hwnd and fg != 0:
            # Window exists and is visible but we can't force it to foreground.
            # Don't give up — the window may still accept keystrokes even if not
            # technically "foreground" (especially after BringWindowToTop).
            logger.warning(f"  二次尝试仍失败 (fg={fg})，继续尝试发送...")
        elif fg == 0:
            # fg still 0 after retry — screen may be locked. Window was shown
            # though (SW_SHOW/SW_RESTORE above), so keystrokes should still route.
            logger.warning(f"  前台窗口仍为空（可能锁屏中），继续尝试发送...")

    return hwnd, restore_ctx


def ensure_wechat_ready(wechat_path=None):
    """确保微信已启动并已登录（用于考勤流程前自动检测）。

    在发送签到/签退二维码之前调用，确保微信可用。

    Args:
        wechat_path: WeChatAppEx.exe 的完整路径。为 None 时从 config.yaml 读取。

    Returns:
        True 表示微信已就绪可发送消息，
        False 表示微信不可用，需人工介入或跳过微信发送。
    """
    import subprocess
    import os as _os

    # 如果未传入路径，从 config.yaml 读取
    if not wechat_path:
        try:
            from ..utils.config import load_config
            cfg = load_config()
            wechat_path = cfg.get("wechat", {}).get("exe_path", None)
        except Exception:
            wechat_path = None

    logger.info("=== 微信就绪检测 ===")
    logger.info(f"  配置的微信路径: {wechat_path or '(未配置，将自动搜索)'}")

    # ---- 0. 先确保屏幕唤醒/解锁(屏保或锁屏会让窗口激活和按键失效) ----
    try:
        from ..utils import screen
        screen.ensure_screen_ready()
    except Exception as e:
        logger.warning(f"  唤醒屏幕失败(继续启动微信): {e}")

    # ---- 1. 检查进程是否运行 ----
    try:
        r = subprocess.run(
            'tasklist /fi "imagename eq Weixin.exe" /fo csv /nh',
            shell=True, capture_output=True, text=True, timeout=5,
        )
        found_weixin = "Weixin.exe" in r.stdout
        if not found_weixin:
            r = subprocess.run(
                'tasklist /fi "imagename eq WeChatAppEx.exe" /fo csv /nh',
                shell=True, capture_output=True, text=True, timeout=5,
            )
        found_wechatapp = "WeChatAppEx.exe" in r.stdout
        wechat_running = found_weixin or found_wechatapp
    except Exception as e:
        logger.warning(f"  进程检测异常: {e}")
        wechat_running = False
        found_weixin = False
        found_wechatapp = False

    logger.info(f"  进程检测: Weixin.exe={'✓' if found_weixin else '✗'} WeChatAppEx.exe={'✓' if found_wechatapp else '✗'}")

    if not wechat_running:
        logger.info("  微信未运行，正在启动...")

        # 用配置的路径，不搜索
        if wechat_path and _os.path.exists(wechat_path):
            exe_path = wechat_path
            logger.info(f"  使用配置路径: {exe_path}")
        else:
            if wechat_path:
                logger.warning(f"  配置路径不存在: {wechat_path}，自动搜索...")
            exe_path = _find_wechat_exe()
            if exe_path:
                logger.info(f"  自动搜索到: {exe_path}")
            if not exe_path:
                logger.error("找不到微信！请在 config.yaml wechat.exe_path 中填写微信路径")
                return False

        logger.info(f"启动微信: {exe_path}")
        try:
            subprocess.Popen([exe_path], shell=False)
        except Exception as e:
            logger.error(f"启动微信失败: {e}")
            return False

        # 等待窗口出现
        logger.info("等待微信窗口出现...")
        for i in range(30):
            time.sleep(2)
            hwnd = _find_wechat_hwnd()
            if hwnd:
                logger.info(f"微信窗口已出现 (等待 {i*2}s)")
                break
        else:
            logger.error("微信窗口未在 60s 内出现")
            return False
    else:
        logger.info("微信进程已在运行")
        hwnd = _find_wechat_hwnd()
        if not hwnd:
            # 进程在但找不到窗口，可能在后台初始化
            logger.warning("进程存在但找不到窗口，等待...")
            for i in range(15):
                time.sleep(2)
                hwnd = _find_wechat_hwnd()
                if hwnd:
                    break
            if not hwnd:
                logger.error("找不到微信窗口")
                return False

    # ---- 2. 激活微信窗口 ----
    logger.info("激活微信窗口...")
    activated_hwnd, restore_ctx = _activate_wechat_window(silent=True)
    if activated_hwnd:
        hwnd = activated_hwnd  # 用激活返回的句柄（可能更新）
    elif not hwnd:
        # 激活失败且之前没有 hwnd，重新搜索
        logger.warning("激活失败，重新搜索微信窗口...")
        for i in range(10):
            time.sleep(1)
            hwnd = _find_wechat_hwnd()
            if hwnd:
                logger.info(f"重新找到微信窗口 (重试 {i+1}s)")
                break
    if not hwnd:
        logger.error("找不到微信窗口，无法继续")
        return False
    time.sleep(1)

    # ---- 3. 检查登录状态（带重试，刚启动时UI可能未渲染完毕） ----
    logger.info("检查微信登录状态...")
    login_state = None
    for retry in range(5):
        login_state = _check_wechat_login(hwnd)
        logger.info(f"  登录状态 [{retry+1}/5]: {login_state if isinstance(login_state, str) else login_state[0] if isinstance(login_state, tuple) else 'unknown'}")
        if login_state != "logged_in":
            break  # 找到了明确的登录按钮，跳出
        # 刚启动时可能UI未渲染，判定为 logged_in 不准，等一会重试
        if retry < 4:
            logger.info(f"  未检测到登录按钮，等待UI渲染后重试...")
            time.sleep(2)

    if login_state == "logged_in":
        logger.info("[OK] 微信已登录，可以发送消息")
        _minimize_after_precheck(hwnd, restore_ctx)
        return True

    elif login_state == "need_qr_scan":
        logger.warning("微信需要扫码登录，请在手机上确认...")
        # 等待用户扫码（最多 120 秒）
        for i in range(60):
            time.sleep(2)
            hwnd = _find_wechat_hwnd()
            if not hwnd:
                continue
            state = _check_wechat_login(hwnd)
            if isinstance(state, tuple) and state[0] in ("can_click", "can_click_keyboard"):
                login_state = state
                break
            elif state == "logged_in":
                logger.info("[OK] 用户已扫码登录")
                _minimize_after_precheck(hwnd, restore_ctx)
                return True
        else:
            logger.error("扫码超时（120s），微信未登录")
            _minimize_after_precheck(hwnd, restore_ctx)
            return False

    # ---- 处理 "进入微信" 场景 ----
    if isinstance(login_state, tuple):
        state_type, h = login_state

        if state_type == "can_click":
            # 原生 Win32 按钮 — 直接 BM_CLICK
            logger.info("点击「进入微信」(Win32按钮)...")
            _click_wechat_button(h)
            time.sleep(2)
        elif state_type == "can_click_keyboard":
            # CE/Qt 渲染 — 键盘 Enter
            logger.info("点击「进入微信」(键盘Enter)...")
            _press_enter_on_window(h)
            time.sleep(2)
        else:
            logger.warning(f"未知的 can_click 类型: {state_type}")

        # 点击后等待主界面出现
        for i in range(20):
            time.sleep(1.5)
            new_hwnd = _find_wechat_hwnd()
            if not new_hwnd:
                continue
            state = _check_wechat_login(new_hwnd)
            if state == "logged_in":
                logger.info(f"[OK] 微信已进入主界面 (等待 {(i+1)*1.5:.0f}s)")
                _minimize_after_precheck(new_hwnd, restore_ctx)
                return True
            elif isinstance(state, tuple) and state[0] in ("can_click", "can_click_keyboard"):
                # 按钮还在，重新点击
                logger.info("「进入微信」仍在，再次尝试...")
                if state[0] == "can_click":
                    _click_wechat_button(state[1])
                else:
                    _press_enter_on_window(state[1])
                time.sleep(1)
        logger.warning("点击「进入微信」后未确认到主界面，微信未就绪")
        _minimize_after_precheck(new_hwnd, restore_ctx)
        return False

    logger.warning(f"=== 微信就绪检测: 未识别的状态 ({login_state})，继续 ===")
    _minimize_after_precheck(hwnd, restore_ctx)
    return True


def _restore_foreground_window(target_hwnd):
    """Robustly bring a window back to the foreground.

    Windows restricts SetForegroundWindow to the process that last owned the
    foreground (or one granted permission via ASFW_ANY). Mirroring
    _activate_wechat_window's proven approach: AttachThreadInput + SetForeground
    + SetFocus works across processes in the same desktop session.

    Returns:
        bool: True if the target became the foreground window.
    """
    if not target_hwnd:
        return False
    import win32gui
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(2)  # ASFW_ANY
    except Exception:
        pass
    try:
        import win32process
        our_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        _, target_tid = win32process.GetWindowThreadProcessId(target_hwnd)
        if our_tid != target_tid:
            ctypes.windll.user32.AttachThreadInput(our_tid, target_tid, True)
        win32gui.SetForegroundWindow(target_hwnd)
        win32gui.SetFocus(target_hwnd)
        time.sleep(0.3)
        if our_tid != target_tid:
            ctypes.windll.user32.AttachThreadInput(our_tid, target_tid, False)
        return ctypes.windll.user32.GetForegroundWindow() == target_hwnd
    except Exception:
        try:
            win32gui.SetForegroundWindow(target_hwnd)
        except Exception:
            pass
        return False


def _cleanup_blank_wechat_shells(keep_hwnd=None):
    """清理微信客户端异常产生的空白顶层窗口壳。

    微信 4.1.x 在「隐藏→唤出→抢前台失败→置顶强发」这类自动化操作下，
    会在主进程里新建标题仍为默认 'Weixin'、无会话内容的空窗口壳
    （class Qt51514QWindowIcon）。它们不会被收尾逻辑隐藏/关闭，
    日积月累就成了任务栏里一排「没内容」的微信窗口。

    判定标准（全部满足才算空壳，避免误伤）：
      - 顶层窗口归属进程与 keep_hwnd 相同（微信主进程；找不到时按 Weixin.exe 名匹配）
      - class 含 'Qt51514QWindowIcon'
      - 标题恰为默认 'Weixin'（真实会话窗口标题是中文/群名，主窗口标题是"微信"）
      - hwnd != keep_hwnd（绝不动正在使用的主窗口）
    对这些窗口 PostMessage WM_CLOSE 优雅关闭。

    Returns:
        int: 关闭的空壳数量
    """
    import win32gui, win32con, win32process

    target_pid = None
    if keep_hwnd:
        try:
            _, target_pid = win32process.GetWindowThreadProcessId(keep_hwnd)
        except Exception:
            target_pid = None

    def _is_wechat_process(h):
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
            if pid == 0:
                return False
            if target_pid is not None:
                return pid == target_pid
            import win32api
            hproc = win32api.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
            try:
                exe = win32process.GetModuleFileNameEx(hproc, 0).replace("\\", "/").lower()
            finally:
                win32api.CloseHandle(hproc)
            return "weixin.exe" in exe
        except Exception:
            return False

    def _is_blank_shell(h):
        try:
            if h == keep_hwnd:
                return False
            if "Qt51514QWindowIcon" not in win32gui.GetClassName(h):
                return False
            return win32gui.GetWindowText(h) == "Weixin"
        except Exception:
            return False

    closed = 0

    def _enum_cb(h, _):
        nonlocal closed
        if _is_wechat_process(h) and _is_blank_shell(h):
            try:
                win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
                logger.info(f"  清理微信空白窗口壳: hwnd={h}")
                closed += 1
            except Exception:
                pass
        return True

    try:
        win32gui.EnumWindows(_enum_cb, None)
    except Exception as e:
        logger.warning(f"  清理微信空白窗口壳失败: {e}")
    if closed:
        logger.info(f"  已清理 {closed} 个微信空白窗口壳")
    return closed


def _minimize_after_precheck(hwnd, restore_ctx):
    """预检完成后：还回用户原前台窗口，并一律把微信缩到系统托盘。

    与发送链路的 _restore_after_send 不同，预检只是后台就绪探测，
    无论预检前微信处于什么状态，结束后都缩到托盘，等发送时再唤出。
    """
    # 1. 还回用户原前台窗口（若原前台就是微信自身则跳过——反正接下来要隐藏它，
    #    强行拉回只会让前台停在已隐藏的窗口上）
    original_fg = restore_ctx.get("original_fg") if restore_ctx else None
    if original_fg and original_fg != hwnd:
        restored = _restore_foreground_window(original_fg)
        if not restored:
            logger.info("  原前台窗口还原失败（可能已最小化/关闭），保持当前状态")
    # 2. 微信一律缩到系统托盘
    if hwnd:
        try:
            import win32gui as _wg
            _wg.ShowWindow(hwnd, 0)  # SW_HIDE → 系统托盘
            logger.info("  微信已缩到系统托盘（预检完成）")
        except Exception:
            pass
    # 3. 清理微信异常产生的空白窗口壳（微信 4.1.x 唤醒/置顶操作会残留，
    #    不清理会跨天积累成任务栏一排"没内容"窗口）
    _cleanup_blank_wechat_shells(keep_hwnd=hwnd)


# ---- 微信工具函数 ----

def _find_wechat_hwnd():
    """查找微信主窗口句柄"""
    import win32gui
    for title in ["微信", "WeChat"]:
        hwnd = win32gui.FindWindow(None, title)
        if hwnd:
            return hwnd
    found = []
    def _enum(h, _):
        try:
            t = win32gui.GetWindowText(h)
            if t and ("微信" in t or "WeChat" in t):
                found.append(h)
        except Exception:
            pass
        return True
    win32gui.EnumWindows(_enum, None)
    return found[0] if found else None


_WECHAT_SEARCH_PATHS = [
    r"C:\Program Files\Tencent\WeChat\WeChatAppEx.exe",
    r"C:\Program Files (x86)\Tencent\WeChat\WeChatAppEx.exe",
    r"D:\Program Files\Tencent\WeChat\WeChatAppEx.exe",
    r"D:\Program Files (x86)\Tencent\WeChat\WeChatAppEx.exe",
    # 新版微信 — Weixin.exe
    r"C:\Program Files\Tencent\Weixin\Weixin.exe",
    r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe",
    r"D:\Program Files\Tencent\Weixin\Weixin.exe",
    r"D:\Program Files (x86)\Tencent\Weixin\Weixin.exe",
    r"D:\Program files\Weixin\Weixin.exe",
]

def _find_wechat_exe():
    """查找 WeChatAppEx.exe 路径"""
    import os as _os
    for p in _WECHAT_SEARCH_PATHS:
        if _os.path.exists(p):
            return p
    # 搜索注册表
    try:
        import winreg
        for root in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
            for subkey in [r"SOFTWARE\Tencent\WeChat", r"SOFTWARE\WOW6432Node\Tencent\WeChat"]:
                try:
                    with winreg.OpenKey(root, subkey) as key:
                        install_dir, _ = winreg.QueryValueEx(key, "InstallPath")
                        exe = _os.path.join(install_dir, "WeChatAppEx.exe")
                        if _os.path.exists(exe):
                            return exe
                except Exception:
                    pass
    except Exception:
        pass
    # 搜索 AppData
    try:
        local_appdata = _os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            for root, dirs, files in _os.walk(_os.path.join(local_appdata, "Tencent"), topdown=True):
                if root.count(_os.sep) - local_appdata.count(_os.sep) > 4:
                    dirs.clear()
                    continue
                for f in files:
                    if f.lower() == "wechatappex.exe":
                        return _os.path.join(root, f)
    except Exception:
        pass
    return None


def _check_wechat_login(hwnd):
    """检查微信登录状态。

    返回:
      "logged_in"      — 已登录主界面
      "need_qr_scan"   — 在登录页需要扫码
      ("can_click", hwnd) — 已扫码，可以点击「进入微信」（原生按钮）
      ("can_click_keyboard", hwnd) — 疑似登录确认对话框（CEF/Qt渲染，用键盘Enter）
    """
    import win32gui

    if not hwnd:
        logger.warning("_check_wechat_login: hwnd is None")
        return "logged_in"

    # ---- 方法1: Win32 原生按钮（旧版微信）----
    result = [None]
    all_children = []

    def _enum_child(h, _):
        if result[0]:
            return False
        try:
            text = win32gui.GetWindowText(h)
            cls = win32gui.GetClassName(h)
            all_children.append((text[:40], cls, h))
            for t in ["进入微信", "Enter WeChat"]:
                if t in text and ("Button" in cls or "Static" in cls):
                    result[0] = ("can_click", h)
                    return False
            for t in ["登录", "Log In"]:
                if t == text.strip() and "Button" in cls:
                    result[0] = ("need_qr_scan", h)
                    return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _enum_child, None)
    except Exception:
        pass

    if result[0]:
        state, h = result[0]
        if state == "need_qr_scan":
            logger.info("检测到「登录」按钮 — 需要扫码")
            return "need_qr_scan"
        else:
            logger.info("检测到「进入微信」按钮（原生Win32）— 已扫码待确认")
            return ("can_click", h)

    # ---- 方法2: 窗口大小判断（新版微信 Qt/CEF 渲染，无原生按钮）----
    try:
        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        logger.info(f"  窗口尺寸: {w}x{h} (child windows: {len(all_children)})")
    except Exception:
        w, h = 0, 0

    # 小窗口(< 500px 宽) = 登录/确认对话框，需要键盘 Enter
    # 大窗口 = 主界面，已登录
    if 0 < w < 500:
        logger.info(f"  小窗口({w}x{h}) → 判定为登录确认对话框，将用键盘 Enter")
        if all_children:
            for text, cls, _ in all_children[:5]:
                logger.info(f"    child: cls={cls} text={text}")
        return ("can_click_keyboard", hwnd)

    # ---- 方法3: UIA（如果可用）----
    try:
        import uiautomation as auto
        wx = auto.WindowControl(NativeWindowHandle=hwnd)
        btn = auto.ButtonControl(Name='进入微信')
        if btn.Exists(maxSearchSeconds=1):
            logger.info("UIA 检测到「进入微信」按钮")
            return ("can_click_keyboard", hwnd)

        # 也搜英文
        btn = auto.ButtonControl(Name='Enter WeChat')
        if btn.Exists(maxSearchSeconds=1):
            logger.info("UIA 检测到「Enter WeChat」按钮")
            return ("can_click_keyboard", hwnd)

        # 搜索结果中包含 "进入微信" 的任意控件
        def _search_uia(ctrl, target, depth=0):
            if depth > 5:
                return None
            try:
                if ctrl.Name and target in ctrl.Name:
                    return ctrl
            except Exception:
                pass
            try:
                for child in ctrl.GetChildren():
                    r = _search_uia(child, target, depth + 1)
                    if r:
                        return r
            except Exception:
                pass
            return None

        found = _search_uia(wx, "进入微信")
        if found:
            logger.info(f"UIA 递归搜索找到: {found.ControlTypeName} '{found.Name}'")
            return ("can_click_keyboard", hwnd)
    except ImportError:
        pass
    except Exception as e:
        logger.info(f"  UIA 检测异常: {e}")

    logger.info(f"未检测到登录相关按钮（窗口 {w}x{h}），判定为已登录")
    return "logged_in"


def _click_wechat_button(hwnd):
    """点击微信窗口中的按钮"""
    import win32gui, win32con
    try:
        win32gui.PostMessage(hwnd, win32con.BM_CLICK, 0, 0)
        logger.info(f"BM_CLICK -> hwnd={hwnd}")
        return True
    except Exception:
        pass
    try:
        rect = win32gui.GetWindowRect(hwnd)
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        ctypes.windll.user32.SetCursorPos(cx, cy)
        time.sleep(0.1)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        logger.info(f"鼠标点击 -> ({cx}, {cy})")
        return True
    except Exception:
        return False


def _press_enter_on_window(hwnd):
    """在微信登录对话框上按 Enter（CEF/Qt 渲染，「进入微信」是默认按钮）。

    加强版：最多3次「前台激活 + 验证 GetForegroundWindow」，
    失败则还原/置顶/点击对话框中心再取焦点；每轮后验证是否已进入主界面
    （窗口变宽或句柄重建）。
    """
    import win32gui, win32con

    logger.info("键盘 Enter → 微信登录对话框...")

    def _focus(h):
        """尝试让窗口获得前台+键盘焦点，返回是否成功。"""
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(2)  # ASFW_ANY
        except Exception:
            pass
        try:
            import win32process
            our_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            _, wx_tid = win32process.GetWindowThreadProcessId(h)
            if our_tid != wx_tid:
                ctypes.windll.user32.AttachThreadInput(our_tid, wx_tid, True)
            win32gui.SetForegroundWindow(h)
            win32gui.SetFocus(h)
            time.sleep(0.5)
            if our_tid != wx_tid:
                ctypes.windll.user32.AttachThreadInput(our_tid, wx_tid, False)
            return ctypes.windll.user32.GetForegroundWindow() == h
        except Exception as e:
            logger.warning(f"  窗口聚焦异常: {e}")
            return False

    for attempt in range(1, 4):
        # hwnd 可能因进入主界面而重建，先确认有效性
        if not win32gui.IsWindow(hwnd):
            hwnd = _find_wechat_hwnd()
            if not hwnd:
                logger.warning("  微信窗口已失效且无法重新找到")
                return False

        if not _focus(hwnd):
            logger.warning(f"  [尝试{attempt}] 微信窗口未获得前台，置顶+点击对话框中心...")
            try:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    time.sleep(0.5)
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
                rect = win32gui.GetWindowRect(hwnd)
                cx = (rect[0] + rect[2]) // 2
                cy = (rect[1] + rect[3]) // 2
                ctypes.windll.user32.SetCursorPos(cx, cy)
                time.sleep(0.1)
                ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
                time.sleep(0.05)
                ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
                time.sleep(0.3)
                win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
                _focus(hwnd)
            except Exception as e:
                logger.warning(f"  置顶/点击失败: {e}")

        # 发送 Enter 键
        _user32.keybd_event(VK_ENTER, 0, 0, 0)
        time.sleep(0.05)
        _user32.keybd_event(VK_ENTER, 0, 2, 0)  # KEYEVENTF_KEYUP
        logger.info(f"  [尝试{attempt}] 已发送 Enter 键")
        time.sleep(1.0)

        # 验证：微信主窗口是否已变大（进入主界面）
        try:
            if win32gui.IsWindow(hwnd):
                rect = win32gui.GetWindowRect(hwnd)
                w = rect[2] - rect[0]
                if w >= 600:
                    logger.info(f"  ✅ 微信已进入主界面 (窗口宽={w})")
                    return True
        except Exception:
            pass
        # 进入主界面后窗口可能重建，改用新句柄继续
        new_h = _find_wechat_hwnd()
        if new_h and new_h != hwnd:
            hwnd = new_h
            logger.info(f"  窗口已重建，改用新句柄 {hwnd}")

    logger.warning("  按 Enter 后仍未确认进入主界面")
    return False


def _restore_after_send(hwnd, restore_ctx):
    """Restore user's desktop state after silent send.

    - If HWND_TOPMOST was set temporarily, remove it first.
    - If silent mode captured the user's original foreground window, switch back to it.
    - If WeChat was hidden (tray), re-hide it.
    - If WeChat was minimized, re-minimize it.
    """
    if restore_ctx is None:
        return

    import win32gui, win32con, ctypes

    # 0. 取消临时置顶（必须在恢复前台/隐藏之前做，否则置顶状态会导致后续操作无效）
    if restore_ctx.get("was_topmost"):
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST,
                                  0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
            logger.info("  已取消微信临时置顶")
        except Exception:
            pass

    # 1. If silent: switch focus back to user's original window
    original_fg = restore_ctx.get("original_fg")
    if original_fg:
        _restore_foreground_window(original_fg)

    # 1.5 If WeChat was minimized before, re-minimize it (after original foreground is
    #     restored, so the minimize doesn't steal focus back).
    if restore_ctx.get("was_iconic"):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            logger.info("  微信已重新最小化（静默模式）")
        except Exception:
            pass

    # 2. If WeChat was in tray, re-hide it
    if restore_ctx.get("was_hidden"):
        try:
            win32gui.ShowWindow(hwnd, SW_HIDE)
            logger.info("  微信窗口已重新隐藏（静默模式）")
        except Exception:
            pass

    # 3. 发送结束后清理微信异常产生的空白窗口壳（在主窗口收走之后再关，
    #    避免动到刚完成发送的窗口）。微信 4.1.x 在唤出/置顶强发后会残留
    #    标题为默认 'Weixin' 的空窗口，不清理会跨天积累。
    _cleanup_blank_wechat_shells(keep_hwnd=hwnd)


def send_image_to_group(image_path, group_name):
    """
    发送图片到微信群（不含文字）。
    """
    logger.info(f"发送图片到微信群: {group_name}")
    return _send(image_path, group_name, text_msg=None)


def send_image_with_text(image_path, group_name, text_msg):
    """
    发送文字 + 图片到微信群（先发文字说明，再发二维码图片）。

    Args:
        image_path: 图片路径
        group_name: 微信群名称
        text_msg: 要发送的文字说明
    """
    logger.info(f"发送文字+图片到微信群: {group_name}")
    return _send(image_path, group_name, text_msg=text_msg)


def _get_silent_setting():
    """Read silent mode from config.yaml. Safe to call at any time."""
    try:
        from ..utils.config import load_config
        cfg = load_config()
        return cfg.get("wechat", {}).get("silent", True)
    except Exception:
        return True  # default to silent


def _get_wechat_config(key, default):
    """Read a wechat config value from config.yaml."""
    try:
        from ..utils.config import load_config
        cfg = load_config()
        return cfg.get("wechat", {}).get(key, default)
    except Exception:
        return default


def _send(image_path, group_name, text_msg=None):
    """统一发送入口，多级回退"""
    silent = _get_silent_setting()
    # Tier 1: built-in win32 API (always available, no extra deps)
    try:
        result = _send_via_win32(image_path, group_name, text_msg, silent=silent)
        if result:
            return True
    except Exception as e:
        logger.warning(f"win32 发送失败: {e}")

    # Tier 2: pywinauto UIA (if installed)
    try:
        result = _send_via_pywinauto(image_path, group_name, text_msg, silent=silent)
        if result:
            return True
    except Exception as e:
        logger.warning(f"pywinauto 发送失败: {e}")

    # Tier 3: pynput keyboard (if installed)
    try:
        result = _send_via_keyboard(image_path, group_name, text_msg, silent=silent)
        if result:
            return True
    except Exception as e:
        logger.warning(f"键盘模拟发送失败: {e}")

    logger.info(f"微信自动发送未成功，截图已本地保存: {image_path}")
    return False


def _ensure_wechat_entered(hwnd=None):
    """发送前确保微信已进入主界面。

    若微信还停在「进入微信」确认界面（上次未进入/登录确认弹窗），
    再次按 Enter 或点按钮进入主界面；否则在错误的界面上点搜索框/发消息全部无效。

    Returns:
        bool: True=已进入主界面，False=无法进入
    """
    import win32gui
    if hwnd is None:
        hwnd = _find_wechat_hwnd()
    if not hwnd:
        return False

    for attempt in range(1, 4):
        state = _check_wechat_login(hwnd)
        if state == "logged_in":
            return True
        if isinstance(state, tuple):
            stype, h = state
            if stype == "can_click":
                logger.info(f"  [进入微信 {attempt}/3] 点击进入按钮...")
                _click_wechat_button(h)
            elif stype == "can_click_keyboard":
                logger.info(f"  [进入微信 {attempt}/3] 按 Enter...")
                _press_enter_on_window(h)
            time.sleep(2)
            # 进入后窗口可能重建，改用新句柄
            new_h = _find_wechat_hwnd()
            if new_h:
                hwnd = new_h
            continue
        # need_qr_scan 等异常状态
        logger.warning(f"  微信登录状态异常: {state}")
        return False

    logger.warning("  多次尝试后仍未进入微信主界面")
    return False


def _send_via_win32(image_path, group_name, text_msg=None, silent=False):
    """Tier 1: 纯 win32 API — 激活微信 → 点击搜索框 → 输入群名 → 发文字 → 发图片"""
    import win32clipboard
    import win32gui

    # 1. Copy image to clipboard
    _copy_image_to_clipboard(image_path)

    # 2. Find & activate WeChat window
    hwnd, restore_ctx = _activate_wechat_window(silent=silent)
    if not hwnd:
        logger.warning("  win32: 找不到或无法激活微信窗口")
        return False

    # 2.4 确保已进入微信主界面（处理「进入微信」确认界面）
    _ensure_wechat_entered(hwnd)

    # 2.5 前台验证：确认微信确实在最前面，否则临时置顶（发送完立即取消）
    import win32con
    fg = ctypes.windll.user32.GetForegroundWindow()
    if fg != hwnd:
        fg_title = ""
        try:
            fg_title = win32gui.GetWindowText(fg)
        except Exception:
            pass
        logger.warning(f"  微信不在前台 (fg={fg} '{fg_title}')，临时置顶以完成发送...")
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST,
                                  0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
            restore_ctx["was_topmost"] = True
            time.sleep(0.3)
            logger.info(f"  微信已临时置顶")
        except Exception as e:
            logger.warning(f"  临时置顶失败: {e}")
            # 不阻断，继续尝试发送

    try:
        # 3. 找到渲染窗口，计算搜索框坐标，鼠标点击搜索框
        render_hwnd = None

        def find_render(h, _):
            nonlocal render_hwnd
            if 'MMUIRenderSubWindowHW' in win32gui.GetClassName(h):
                render_hwnd = h
            return True

        win32gui.EnumChildWindows(hwnd, find_render, None)

        if render_hwnd:
            r = win32gui.GetWindowRect(render_hwnd)  # (left, top, right, bottom)
            # Search box position is relative to render area — stable across window moves.
            # Calibrate via config.yaml: wechat.search_offset_x / search_offset_y
            ox = _get_wechat_config("search_offset_x", 90)
            oy = _get_wechat_config("search_offset_y", 25)
            click_x = r[0] + ox
            click_y = r[1] + oy
        else:
            # Fallback: click near top-left of main window
            r = win32gui.GetWindowRect(hwnd)
            click_x = r[0] + _get_wechat_config("search_offset_x", 80)
            click_y = r[1] + _get_wechat_config("search_offset_y", 55)

        logger.info(f"  点击搜索框 ({click_x}, {click_y})")
        ctypes.windll.user32.SetCursorPos(click_x, click_y)
        time.sleep(0.08)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # down
        time.sleep(0.08)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # up
        time.sleep(0.3)

        # 4. 输入群名 → Enter
        _copy_text_to_clipboard(group_name)
        _ctrl_key(VK_V)
        time.sleep(0.8)
        _tap(VK_ENTER)
        time.sleep(1.0)

        # 5. 发送文字
        if text_msg:
            _copy_text_to_clipboard(text_msg)
            _ctrl_key(VK_V)
            time.sleep(0.3)
            _tap(VK_ENTER)
            time.sleep(0.5)

        # 6. 粘贴图片 → 发送
        _copy_image_to_clipboard(image_path)
        _ctrl_key(VK_V)
        time.sleep(0.5)
        _tap(VK_ENTER)

        logger.info(f"win32: 已发送到 {group_name}")
        return True
    finally:
        _restore_after_send(hwnd, restore_ctx)


def _send_via_pywinauto(image_path, group_name, text_msg=None, silent=False):
    """Tier 2: pywinauto UIA 控制（支持 xwechat/微信多版本）"""
    from pywinauto.application import Application
    from pywinauto.keyboard import send_keys

    # 1. 复制图片到剪贴板
    _copy_image_to_clipboard(image_path)

    # 2. 先确保微信窗口可见并激活（处理托盘隐藏状态）
    hwnd, restore_ctx = _activate_wechat_window(silent=silent)
    if not hwnd:
        logger.warning("  无法激活微信窗口，跳过 pywinauto")
        return False  # Let dispatcher try next tier

    # 2.4 确保已进入微信主界面（处理「进入微信」确认界面）
    _ensure_wechat_entered(hwnd)

    # 2.5 前台验证（同 win32 tier）
    import win32gui as _wg, win32con as _wc, ctypes as _ct
    fg = _ct.windll.user32.GetForegroundWindow()
    if fg != hwnd:
        try:
            _wg.SetWindowPos(hwnd, _wc.HWND_TOPMOST,
                             0, 0, 0, 0,
                             _wc.SWP_NOMOVE | _wc.SWP_NOSIZE | _wc.SWP_SHOWWINDOW)
            restore_ctx["was_topmost"] = True
            time.sleep(0.3)
        except Exception:
            pass

    try:
        # 3. 连接微信窗口（窗口现在已可见，连接更可靠）
        win = None
        connect_methods = [
            ("xwechat path", lambda: Application(backend="uia").connect(path="WeChatAppEx.exe", timeout=5)),
            ("xwechat title_re", lambda: Application(backend="uia").connect(title_re=".*微信.*", timeout=5)),
            ("wechat title_re", lambda: Application(backend="uia").connect(title_re=".*微信.*", timeout=3)),
        ]
        for method_name, method_fn in connect_methods:
            try:
                app = method_fn()
                win = app.top_window()
                if win:
                    logger.info(f"  微信连接成功 ({method_name})")
                    break
            except Exception:
                continue
        if win is None:
            logger.warning("  无法连接微信窗口（微信可能未运行）")
            return False
        try:
            win.set_focus()
        except Exception:
            logger.warning("  pywinauto set_focus 失败")
            return False
        time.sleep(0.5)

        # 4. Ctrl+F 搜索群名 → 进入聊天
        send_keys("^f")
        time.sleep(0.5)
        _copy_text_to_clipboard(group_name)
        send_keys("^v")
        time.sleep(0.8)
        send_keys("{ENTER}")
        time.sleep(1)

        # 5. 如果有文字说明，先发送文字
        if text_msg:
            _copy_text_to_clipboard(text_msg)
            send_keys("^v")
            time.sleep(0.3)
            send_keys("{ENTER}")
            time.sleep(0.5)
            logger.info(f"  文字已发送: {text_msg[:40]}")

        # 6. 重新复制图片 → 粘贴 → 发送
        _copy_image_to_clipboard(image_path)
        send_keys("^v")
        time.sleep(0.5)
        send_keys("{ENTER}")

        logger.info(f"pywinauto: 已发送到 {group_name}")
        return True
    finally:
        _restore_after_send(hwnd, restore_ctx)


def _send_via_keyboard(image_path, group_name, text_msg=None, silent=False):
    """Tier 3: win32gui + pynput 键盘模拟（支持 xwechat）"""
    import win32gui, win32con

    # 1. 复制到剪贴板
    _copy_image_to_clipboard(image_path)

    # 2. 找到并激活微信窗口（处理托盘隐藏状态）
    hwnd, restore_ctx = _activate_wechat_window(silent=silent)
    if not hwnd:
        logger.warning("  键盘模拟：找不到或无法激活微信窗口")
        return False  # Let caller handle fallback

    # 2.4 确保已进入微信主界面（处理「进入微信」确认界面）
    _ensure_wechat_entered(hwnd)

    # 2.5 前台验证（同 win32 tier）
    fg = ctypes.windll.user32.GetForegroundWindow()
    if fg != hwnd:
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST,
                                  0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
            restore_ctx["was_topmost"] = True
            time.sleep(0.3)
        except Exception:
            pass

    try:
        from pynput.keyboard import Key, Controller
        kb = Controller()

        # 3. Ctrl+F → 粘贴群名 → 回车进入聊天
        with kb.pressed(Key.ctrl):
            kb.press('f'); kb.release('f')
        time.sleep(0.5)
        _copy_text_to_clipboard(group_name)
        with kb.pressed(Key.ctrl):
            kb.press('v'); kb.release('v')
        time.sleep(0.8)
        kb.press(Key.enter); kb.release(Key.enter)
        time.sleep(1)

        # 4. 如果有文字说明，先发送文字
        if text_msg:
            _copy_text_to_clipboard(text_msg)
            with kb.pressed(Key.ctrl):
                kb.press('v'); kb.release('v')
            time.sleep(0.3)
            kb.press(Key.enter); kb.release(Key.enter)
            time.sleep(0.5)
            logger.info(f"  文字已发送: {text_msg[:40]}")

        # 5. 重新复制图片 → 粘贴 → 发送
        _copy_image_to_clipboard(image_path)
        with kb.pressed(Key.ctrl):
            kb.press('v'); kb.release('v')
        time.sleep(0.5)
        kb.press(Key.enter); kb.release(Key.enter)

        logger.info(f"键盘模拟: 已发送到 {group_name}")
        return True
    finally:
        _restore_after_send(hwnd, restore_ctx)


def _copy_text_to_clipboard(text):
    """复制文本到剪贴板"""
    import win32clipboard
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardText(text)
    win32clipboard.CloseClipboard()


def _copy_image_to_clipboard(image_path):
    """将图片复制到 Windows 剪贴板"""
    from PIL import Image
    from io import BytesIO
    import win32clipboard

    image = Image.open(image_path)
    output = BytesIO()
    image.convert("RGB").save(output, "BMP")
    data = output.getvalue()[14:]  # 去掉 BMP 文件头
    output.close()

    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    win32clipboard.CloseClipboard()
    logger.info("图片已复制到剪贴板")
