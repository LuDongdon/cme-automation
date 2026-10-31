"""屏幕唤醒 / 解除屏保 / 锁屏解锁 — 可靠的 Win32 原语

解决两个生产问题:
  1. 程序无法解除屏保 —— 旧的 1px mouse_event 不点亮显示器也不验证,无法真正退出屏保
  2. 微信"没进入" —— 屏保/锁屏占用前台导致按键和聚焦失效

所有函数都带日志和验证,绝不"假成功"。
可被 app.py / auto_start.py / wechat.controller.py 复用。
"""

import ctypes
import logging
import os
import time
from ctypes import wintypes

logger = logging.getLogger("cme")

# ===== Windows 常量 =====
HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
SC_MONITORPOWER_ON = -1

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_SHIFT = 0x10
VK_SPACE = 0x20
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


# ===== SendInput 结构 =====

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


# ===== 基础操作 =====

def keep_awake():
    """阻止系统睡眠/显示器关闭/屏保。

    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED。
    可重复调用;保活线程应每 ~30s 重发一次,因为 ES 标志跟随调用线程。
    """
    try:
        _kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
    except Exception as e:
        logger.warning(f"keep_awake 失败: {e}")


def wake_display():
    """点亮显示器 — 广播 WM_SYSCOMMAND SC_MONITORPOWER=-1。

    只使用非阻塞的 PostMessage。注意: 绝不用 SendMessage 对 HWND_BROADCAST 广播,
    它是同步的、会等待所有顶层窗口响应,任一窗口不响应就会永久阻塞整个程序。
    显示器若因电源管理关闭,此调用可将其点亮。
    """
    try:
        # argtypes 明确,避免 64 位下 LPARAM 被截断
        _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM]
        sent = _user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND,
                                    SC_MONITORPOWER, SC_MONITORPOWER_ON)
        if sent:
            logger.info("  wake_display: 已发送点亮显示器信号")
            return True
        logger.warning("  wake_display: PostMessage 返回 0(可能未发送)")
    except Exception as e:
        logger.warning(f"  wake_display PostMessage 失败: {e}")
    return False


def is_screensaver_running():
    """检测 Windows 屏保是否正在运行。

    单一检测不可靠: 经典屏保用 `WindowsScreenSaverClass` 窗口类,但第三方屏保
    (如本机的 FlipIt.scr)用的是自己的窗口类。因此用三种方式组合,任一命中即判定运行:
      1. SPI_GETSCREENSAVERRUNNING (0x0072) — 官方 API,屏保运行时系统置 TRUE
      2. 进程检测 — 注册表配置的 SCRNSAVE.EXE 进程是否在运行
      3. FindWindowW("WindowsScreenSaverClass") — 经典屏保窗口类
    """
    # 1) 官方 SPI
    try:
        running = wintypes.BOOL()
        if _user32.SystemParametersInfoW(0x0072, 0, ctypes.byref(running), 0) and running.value:
            return True
    except Exception:
        pass
    # 2) 屏保进程(读取注册表配置的 .scr 文件名)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Control Panel\Desktop') as k:
            scr = winreg.QueryValueEx(k, 'SCRNSAVE.EXE')[0]
        base = os.path.basename(scr).lower()
        if base:
            import subprocess
            r = subprocess.run(['tasklist', '/fo', 'csv', '/nh'],
                               capture_output=True, text=True, timeout=5)
            if base in r.stdout.lower():
                return True
    except Exception:
        pass
    # 3) 经典屏保窗口类
    try:
        if _user32.FindWindowW("WindowsScreenSaverClass", None) != 0:
            return True
    except Exception:
        pass
    return False


# ===== 输入模拟 =====

def _send_key_press(vk_code):
    """发送一个无害按键(SendInput 优先,keybd_event 兜底)。用于退出屏保。"""
    sc = _user32.MapVirtualKeyW(vk_code, 0)
    ev_down = INPUT()
    ev_down.type = INPUT_KEYBOARD
    ev_down.union.ki.wVk = vk_code
    ev_down.union.ki.wScan = sc if sc else 0
    sent = _user32.SendInput(1, ctypes.byref(ev_down), ctypes.sizeof(INPUT))
    time.sleep(0.05)
    ev_up = INPUT()
    ev_up.type = INPUT_KEYBOARD
    ev_up.union.ki.wVk = vk_code
    ev_up.union.ki.wScan = sc if sc else 0
    ev_up.union.ki.dwFlags = KEYEVENTF_KEYUP
    sent2 = _user32.SendInput(1, ctypes.byref(ev_up), ctypes.sizeof(INPUT))
    if not sent or not sent2:
        # SendInput 被过滤时的兜底
        _user32.keybd_event(vk_code, 0, 0, 0)
        time.sleep(0.03)
        _user32.keybd_event(vk_code, 0, 2, 0)
    time.sleep(0.1)


def _send_mouse_move(dx, dy=0):
    """相对移动鼠标 dx,dy 像素(SendInput,失败回退 mouse_event)。"""
    ev = INPUT()
    ev.type = INPUT_MOUSE
    ev.union.mi.dx = dx
    ev.union.mi.dy = dy
    ev.union.mi.dwFlags = MOUSEEVENTF_MOVE
    sent = _user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INPUT))
    if not sent:
        _user32.mouse_event(MOUSEEVENTF_MOVE, dx, dy, 0, 0)
    time.sleep(0.1)


def _press_key_robust(vk_code, hold_ms=80, after_ms=120):
    """SendInput 按键(锁屏安全桌面可用),与 auto_start 一致。"""
    sc = _user32.MapVirtualKeyW(vk_code, 0)
    ev_down = INPUT()
    ev_down.type = INPUT_KEYBOARD
    ev_down.union.ki.wVk = vk_code
    ev_down.union.ki.wScan = sc if sc else 0
    _user32.SendInput(1, ctypes.byref(ev_down), ctypes.sizeof(INPUT))
    time.sleep(hold_ms / 1000.0)
    ev_up = INPUT()
    ev_up.type = INPUT_KEYBOARD
    ev_up.union.ki.wVk = vk_code
    ev_up.union.ki.wScan = sc if sc else 0
    ev_up.union.ki.dwFlags = KEYEVENTF_KEYUP
    _user32.SendInput(1, ctypes.byref(ev_up), ctypes.sizeof(INPUT))
    time.sleep(after_ms / 1000.0)


def _click_center():
    """在屏幕中心模拟一次鼠标点击(滑开锁屏壁纸)。输入到当前输入桌面。"""
    try:
        sw = _user32.GetSystemMetrics(0)  # SM_CXSCREEN
        sh = _user32.GetSystemMetrics(1)  # SM_CYSCREEN
        cx, cy = sw // 2, sh // 2
        _user32.SetCursorPos(cx, cy)
        time.sleep(0.1)
        _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.2)
    except Exception as e:
        logger.warning(f"  点击屏幕中心失败: {e}")


# ===== 解除屏保 =====

def dismiss_screensaver(max_seconds=10):
    """主动退出已在运行的屏保,并点亮显示器。

    分层策略(不同屏保机制不同,组合才能覆盖):
      1. 输入模拟(无害按键 + 鼠标移动 + 点亮显示器)—— 对标准屏保有效
      2. 若超时仍运行 → 强制结束屏保进程。第三方屏保(如本机 FlipIt.scr)
         会忽略注入的合成输入,只能直接结束进程。
    """
    logger.info("解除屏保: 点亮显示器 + 无害输入,直至屏保退出...")
    wake_display()

    if not is_screensaver_running():
        logger.info("  屏保未在运行")
        return True

    # 第一层: 输入模拟
    deadline = time.time() + max_seconds
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        _send_key_press(VK_SHIFT)
        _send_key_press(VK_ESCAPE)  # 部分屏保对 ESC 敏感
        _send_mouse_move(8, 8)
        time.sleep(0.4)
        _send_mouse_move(-8, -8)
        wake_display()
        time.sleep(0.8)
        if not is_screensaver_running():
            logger.info(f"  ✅ 屏保已退出 (第{attempt}轮,输入模拟)")
            return True
        logger.info(f"  屏保仍在运行,重试 {attempt}...")

    # 第二层: 强制结束屏保进程
    logger.warning(f"  输入模拟超时({max_seconds}s),改用强制结束屏保进程...")
    if _kill_screensaver_process():
        logger.info("  ✅ 屏保进程已强制结束")
        return True

    logger.error("  强制结束屏保进程也失败")
    return False


def _kill_screensaver_process():
    """强制结束配置的屏保进程(taskkill /f)。返回是否成功。

    对忽略注入合成输入的第三方屏保(如 FlipIt.scr)是唯一可靠的解除手段。
    屏保是纯显示进程,强制结束无数据丢失;随后 keep_awake() 的 ES 标志
    会阻止其在工作流期间重新触发。
    """
    # 读取配置的屏保文件名
    base = None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Control Panel\Desktop') as k:
            scr = winreg.QueryValueEx(k, 'SCRNSAVE.EXE')[0]
        base = os.path.basename(scr) if scr else None
    except Exception:
        base = None

    import subprocess
    if base:
        logger.info(f"  结束屏保进程: {base}")
        try:
            r = subprocess.run(['taskkill', '/im', base, '/f'],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 or 'SUCCESS' in r.stdout:
                return True
            # 128 = 找不到进程(可能已自行退出)
            logger.warning(f"  taskkill 返回码 {r.returncode}: {r.stderr.strip()[:80]}")
            return not is_screensaver_running()
        except Exception as e:
            logger.error(f"  taskkill 异常: {e}")
            return False

    # 兜底: 结束所有 .scr 进程
    logger.warning("  无法读取屏保配置,结束所有 .scr 进程")
    try:
        r = subprocess.run(['taskkill', '/im', '*.scr', '/f'],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 or 'SUCCESS' in r.stdout:
            return True
        return not is_screensaver_running()
    except Exception as e:
        logger.error(f"  结束 .scr 进程失败: {e}")
        return False


# ===== 锁屏检测与解锁 =====

def is_workstation_locked():
    """检测是否锁屏 — OpenInputDesktop 主检测 + LogonUI 辅助。

    解锁时: 打开用户桌面成功 → False;锁屏时: 输入桌面为 Winlogon 安全桌面,无法打开 → True。
    """
    hDesktop = _user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if hDesktop != 0 and hDesktop is not None:
        _user32.CloseDesktop(hDesktop)
        return False

    try:
        import subprocess
        r = subprocess.run(
            ['tasklist', '/fi', 'IMAGENAME eq LogonUI.exe', '/fo', 'csv', '/nh'],
            capture_output=True, text=True, timeout=5,
        )
        if 'LogonUI.exe' in r.stdout:
            logger.info("  [LOCKED] 检测到锁屏状态 (OpenInputDesktop + LogonUI)")
            return True
    except Exception:
        pass

    is_locked = (hDesktop == 0 or hDesktop is None)
    if is_locked:
        logger.info("  [LOCKED] 检测到锁屏状态 (OpenInputDesktop)")
    return is_locked


def unlock_workstation(pin):
    """解锁 Windows 锁屏 — 滑开锁屏 → 输 PIN → Enter → 轮询验证(最多3轮)。

    - Space 滑开锁屏壁纸,再加一次屏幕中心点击兜底(部分 Win11 需点击)
    - VK 虚拟键码为主,系统自动适配键盘布局
    - 每键留足 press/hold/release 时间
    """
    pin_str = str(pin)
    logger.info(f"解锁锁屏 (PIN: {'*' * len(pin_str)})...")

    VK_DIGITS = {str(i): 0x30 + i for i in range(10)}

    for round_num in range(1, 4):
        if not is_workstation_locked():
            logger.info("  已解锁,无需重复操作")
            return True
        logger.info(f"  解锁尝试 {round_num}/3")

        # Step 1: 滑开锁屏壁纸 → 露出 PIN 输入界面
        _press_key_robust(VK_SPACE, hold_ms=100, after_ms=1500)
        _click_center()
        time.sleep(0.5)

        # Step 2: 逐个键入 PIN 数字
        for ch in pin_str:
            vk = VK_DIGITS.get(ch)
            if vk is None:
                logger.warning(f"  PIN 含非数字字符 '{ch}',已跳过")
                continue
            _press_key_robust(vk, hold_ms=120, after_ms=180)

        time.sleep(0.5)

        # Step 3: Enter 提交
        logger.info("  PIN 已输入,按 Enter 提交...")
        _press_key_robust(VK_RETURN, hold_ms=120, after_ms=500)

        # Step 4: 轮询验证
        for wait_sec in [2, 4, 6, 8, 10]:
            time.sleep(2)
            if not is_workstation_locked():
                logger.info(f"  [OK] 解锁成功 (第{round_num}轮, ~{wait_sec}s)")
                return True
            logger.info(f"  等待解锁... {wait_sec}s")
        logger.warning(f"  第{round_num}轮超时(10s)")

    logger.error(f"  [FAIL] 解锁失败: 3轮均未成功")
    return False


# ===== 编排入口 =====

def _get_lock_password():
    """从 config.yaml 读取 auto_start.lock_password。"""
    try:
        from ..utils.config import load_config
        cfg = load_config()
        return cfg.get("auto_start", {}).get("lock_password", "")
    except Exception:
        return ""


def ensure_screen_ready(pin=None):
    """编排: 保活 → 点亮显示器 → 若锁屏则解锁,否则解除屏保。

    Args:
        pin: 锁屏 PIN。为 None 时从 config.yaml 读取。

    Returns:
        bool: 屏幕是否已就绪(屏保退出/已解锁)。
    """
    logger.info("=== 确保屏幕就绪 ===")
    keep_awake()
    wake_display()

    if is_workstation_locked():
        if pin is None:
            pin = _get_lock_password()
        if not pin:
            logger.warning("  检测到锁屏但未配置锁屏密码,无法自动解锁")
            return False
        if not unlock_workstation(pin):
            logger.error("  解锁失败,屏幕可能仍锁定")
            return False
        logger.info("  ✅ 已解锁")
        return True

    dismissed = dismiss_screensaver(max_seconds=8)
    keep_awake()
    if dismissed:
        logger.info("  ✅ 屏幕就绪(屏保已解除)")
    else:
        logger.warning("  屏保未能确认退出,继续尝试后续流程")
    return dismissed


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("cme")
    ensure_screen_ready()
