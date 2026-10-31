"""CME 定时自动启动 — 唤醒屏幕、解锁、启动应用、准备微信

由 Windows 计划任务触发，执行流程：
  1. 多种方式唤醒屏幕/解除屏保
  2. OpenInputDesktop 检测锁屏 → 输入 PIN 解锁（最多3轮重试）
  3. 启动 Flask + 打开浏览器（带 auto=1）
  4. 准备微信（非阻塞：失败不影响 WebUI，日志输出到 logs/auto_start.log）
"""

import os
import sys
import time
import ctypes
import logging
import subprocess
import threading
from ctypes import wintypes

# 确保项目模块可导入（计划任务上下文可能无此路径）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 修复 Windows 控制台 GBK 编码问题
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 日志输出：文件 + 控制台（定时任务后台运行时也能留下日志）
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_script_dir)
_log_dir = os.path.join(_project_root, "logs")
os.makedirs(_log_dir, exist_ok=True)
_log_file = os.path.join(_log_dir, "auto_start.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("auto_start")

# ===== Windows API constants =====
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_TAB = 0x09
VK_SPACE = 0x20
VK_SHIFT = 0x10
VK_BACK = 0x08

VK_DIGITS = {
    '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34,
    '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38, '9': 0x39,
}

# ===== STRUCTURES FOR SendInput =====

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


# ===== HELPERS (使用 keybd_event / mouse_event 而非 SendInput，避免 UIPI 阻塞) =====

def _send_key(vk_code, key_up=False):
    """发送按键 — 使用 keybd_event，无 SendInput 的 UIPI 限制"""
    flags = 0x0002 if key_up else 0  # KEYEVENTF_KEYUP = 2
    ctypes.windll.user32.keybd_event(vk_code, 0, flags, 0)

def _press_key(vk_code):
    _send_key(vk_code, key_up=False)
    time.sleep(0.05)
    _send_key(vk_code, key_up=True)
    time.sleep(0.1)

def _press_key_robust(vk_code, hold_ms=80, after_ms=120):
    """
    发送按键（锁屏安全桌面用）。

    使用 SendInput（在 Winlogon 安全桌面上可用，不会阻塞）。
    调用者负责超时保护。
    """
    sc = ctypes.windll.user32.MapVirtualKeyW(vk_code, 0)

    event_down = INPUT()
    event_down.type = INPUT_KEYBOARD
    event_down.union.ki.wVk = vk_code
    event_down.union.ki.wScan = sc if sc else 0
    ctypes.windll.user32.SendInput(1, ctypes.byref(event_down), ctypes.sizeof(INPUT))
    time.sleep(hold_ms / 1000.0)

    event_up = INPUT()
    event_up.type = INPUT_KEYBOARD
    event_up.union.ki.wVk = vk_code
    event_up.union.ki.wScan = sc if sc else 0
    event_up.union.ki.dwFlags = KEYEVENTF_KEYUP
    ctypes.windll.user32.SendInput(1, ctypes.byref(event_up), ctypes.sizeof(INPUT))
    time.sleep(after_ms / 1000.0)


# ===== STEP 1: 唤醒屏幕 =====

def is_screensaver_running():
    """检测 Windows 屏保是否正在运行"""
    hwnd = ctypes.windll.user32.FindWindowW("WindowsScreenSaverClass", None)
    return hwnd != 0


def dismiss_screensaver():
    """
    唤醒屏幕/解除屏保 — 委托 src.utils.screen 的可靠实现。

    旧实现只发一次 1px mouse_event，不点亮显示器也不验证，实际无法退出屏保。
    新实现：点亮显示器 + 无害按键/鼠标移动循环 + 验证 WindowsScreenSaverClass 已消失。
    """
    logger.info("[1/4] 唤醒屏幕/解除屏保...")
    try:
        from src.utils.screen import keep_awake, wake_display, dismiss_screensaver as _dismiss
        # 阻止系统进入睡眠/屏保状态
        keep_awake()
        wake_display()
        ok = _dismiss(max_seconds=8)
        if ok:
            logger.info("  [OK] 屏保已退出(验证通过)")
        else:
            logger.warning("  屏保未能确认退出,继续后续步骤")
    except Exception as e:
        logger.warning(f"  解除屏保异常(继续执行): {e}")
        # 兜底：保持旧的 ES 标志 + mouse_event 调用
        ctypes.windll.kernel32.SetThreadExecutionState(
            0x80000000 | 0x00000001 | 0x00000002
        )  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        ctypes.windll.user32.mouse_event(0x0001, 1, 1, 0, 0)
        ctypes.windll.user32.mouse_event(0x0001, -1, -1, 0, 0)


# ===== STEP 2: 锁屏检测与解锁 =====

def is_workstation_locked():
    """
    检测是否锁屏 — 双重验证。

    主检测: OpenInputDesktop (不是 OpenDesktopW)
      - 解锁时: 打开用户桌面，成功 → 返回 False
      - 锁屏时: 当前输入桌面 = Winlogon 安全桌面，普通进程无权打开 → 返回 True

    辅助检测: LogonUI.exe 进程是否存在
    """
    # 主检测: OpenInputDesktop — 打开当前接收输入的桌面
    hDesktop = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if hDesktop != 0 and hDesktop is not None:
        ctypes.windll.user32.CloseDesktop(hDesktop)
        return False

    # 辅助检测: LogonUI.exe 进程
    try:
        result = subprocess.run(
            ['tasklist', '/fi', 'IMAGENAME eq LogonUI.exe', '/fo', 'csv', '/nh'],
            capture_output=True, text=True, timeout=5
        )
        if 'LogonUI.exe' in result.stdout:
            logger.info("  [LOCKED] 检测到锁屏状态 (OpenInputDesktop + LogonUI)")
            return True
    except Exception:
        pass

    # 两个检测都失败/返回锁屏 → 保守按锁屏处理
    is_locked = (hDesktop == 0 or hDesktop is None)
    if is_locked:
        logger.info("  [LOCKED] 检测到锁屏状态 (OpenInputDesktop)")
    return is_locked

def unlock_with_pin(pin):
    """
    解锁 Windows 锁屏 — VK+扫描码混合 + 充裕延迟，3轮重试。

    问题分析：
    - 锁屏 LogonUI 在中文系统上可能使用中文键盘布局
    - 硬编码 US 扫描码在中文键盘上映射错误
    - 按键间隔太短会导致锁屏来不及处理

    策略：
    - 使用 VK 虚拟键码为主（系统自动适配键盘布局）
    - 同时设 wScan 作辅助线索
    - 每个按键都有充裕的 press/hold/release 时间
    - 最长延迟 2 秒让锁屏 UI 稳定
    """
    pin_str = str(pin)
    logger.info(f"[2/4] 解锁锁屏 (PIN: {'*' * len(pin_str)})...")

    MAX_ROUNDS = 3

    for round_num in range(1, MAX_ROUNDS + 1):
        if not is_workstation_locked():
            logger.info("  已解锁，无需重复操作")
            return True

        logger.info(f"  解锁尝试 {round_num}/{MAX_ROUNDS}")

        # Step 1: 发送 Space 滑开锁屏壁纸 → 露出 PIN 输入界面
        # 使用充分的 hold 时间确保锁屏收到
        _press_key_robust(VK_SPACE, hold_ms=100, after_ms=2000)

        # Step 2: 逐个键入 PIN 数字，每个键留充裕时间
        for i, ch in enumerate(pin_str):
            vk = VK_DIGITS.get(ch)
            if vk is None:
                logger.warning(f"  PIN 包含非数字字符 '{ch}'，已跳过")
                continue
            _press_key_robust(vk, hold_ms=120, after_ms=200)

        # 额外等待确保所有输入被处理
        time.sleep(0.5)

        # Step 3: Enter 提交
        logger.info("  PIN 已输入，按 Enter 提交...")
        _press_key_robust(VK_RETURN, hold_ms=120, after_ms=500)

        # Step 4: 轮询验证 — 用 is_workstation_locked (OpenInputDesktop)
        for wait_sec in [2, 4, 6, 8, 10]:
            time.sleep(2)
            if not is_workstation_locked():
                logger.info(f"  [OK] 解锁成功 (第{round_num}轮, ~{wait_sec}s)")
                return True
            logger.info(f"  等待解锁... {wait_sec}s")

        logger.warning(f"  第{round_num}轮超时(10s)")

    logger.error(f"  [FAIL] 解锁失败: {MAX_ROUNDS}轮均未成功")
    return False


# ===== 超时包装 =====

def _run_with_timeout(func, args=(), kwargs=None, timeout=15, label=""):
    """在独立线程中运行函数，超时则放弃（用于 Win32 API 可能阻塞的场景）"""
    if kwargs is None:
        kwargs = {}
    result = [None]
    exception = [None]
    completed = [False]

    def _target():
        try:
            result[0] = func(*args, **kwargs)
            completed[0] = True
        except Exception as e:
            exception[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if not completed[0]:
        logger.warning(f"  [{label}] 超时（{timeout}s），跳过此步骤")
        return None

    if exception[0]:
        raise exception[0]

    return result[0]


# ===== STEP 3: 启动应用 (参考 start.bat 逻辑) =====

def start_cme_application(project_root):
    """启动 Flask 服务器 + 等待就绪 + 打开浏览器（对齐 start.bat）"""
    import urllib.request

    script_dir = os.path.dirname(os.path.abspath(__file__))
    app_py = os.path.join(script_dir, "app.py")
    flask_url = "http://localhost:5000"

    # 检查 Flask 是否已在运行
    try:
        urllib.request.urlopen(flask_url, timeout=1)
        logger.info("  Flask 已在运行，跳过启动")
    except Exception:
        logger.info("  启动 Flask 服务器...")
        python_exe = sys.executable
        logger.info(f"    Python: {python_exe}")
        logger.info(f"    入口: {app_py}")
        # start /b 等效：后台启动 Flask，不阻塞
        subprocess.Popen(
            [python_exe, app_py, "--auto"],  # --auto: 仅定时任务启动时启用空闲自动退出
            cwd=project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 等待 Flask 就绪（对齐 start.bat 的 wait_flask 循环）
        for i in range(30):
            try:
                urllib.request.urlopen(flask_url, timeout=1)
                logger.info(f"  Flask 就绪 (等待 {i} 秒)")
                break
            except Exception:
                time.sleep(1)
        else:
            logger.warning("  Flask 在 30s 内未就绪，仍将打开浏览器")

    # 打开浏览器（带 ?auto=1 触发自动确认弹窗）
    auto_url = f"{flask_url}/?auto=1"
    logger.info(f"  打开浏览器: {auto_url}")
    os.startfile(auto_url)
    logger.info("  [OK] 浏览器已打开")
    return True


def check_wechat_in_subprocess():
    """在独立子进程中检测微信（对齐 start.bat 的做法：python -c "..."）

    独立子进程的好处：
    - 微信相关的 ctypes/win32 操作即使崩溃也不会拖垮主进程
    - 退出码清晰，方便判断状态
    """
    import subprocess as sp
    script_dir = os.path.dirname(os.path.abspath(__file__))
    check_script = os.path.join(script_dir, "wechat", "check_ready.py")
    python_exe = sys.executable

    try:
        r = sp.run(
            [python_exe, check_script],
            cwd=os.path.dirname(script_dir),
            capture_output=True, text=True, timeout=90,
        )
        logger.info(f"  [微信子进程] stdout: {r.stdout.strip()[:200]}")
        if r.returncode == 0:
            logger.info("  [微信子进程] ✅ 退出码 0 → 微信已就绪")
            return True
        else:
            logger.warning(f"  [微信子进程] ❌ 退出码 {r.returncode} → 微信未就绪")
            if r.stderr.strip():
                logger.warning(f"  [微信子进程] stderr: {r.stderr.strip()[:200]}")
            return False
    except sp.TimeoutExpired:
        logger.error("  [微信子进程] ⏱ 超时（90s），判定为未就绪")
        return False
    except Exception as e:
        logger.error(f"  [微信子进程] 启动失败: {e}")
        return False


# ===== MAIN =====

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)

    logger.info("=" * 50)
    logger.info("CME 定时自动启动")
    logger.info(f"项目目录: {project_root}")
    logger.info(f"Python: {sys.executable}")
    logger.info("=" * 50)

    # 加载配置
    try:
        import yaml
        config_path = os.path.join(project_root, "config.yaml")
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        logger.info(f"配置已加载: {config_path}")
    except Exception as e:
        logger.warning(f"配置加载失败，使用默认值: {e}")
        config = {}

    # ---- 0. 检测是否已有运行中的工作流（避免重复启动）----
    logger.info("[0/4] 检测是否已有运行中的工作流...")
    import urllib.request
    flask_running = False
    try:
        urllib.request.urlopen("http://localhost:5000/api/status", timeout=3)
        flask_running = True
    except Exception:
        pass

    if flask_running:
        # Flask 在运行 → 检查是否有活跃的工作流状态
        state_path = os.path.join(project_root, "data", "workflow_state.json")
        has_active_workflow = False
        if os.path.exists(state_path):
            try:
                import json
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                phase = state.get("phase", "")
                if phase in ("login", "course_creation", "attendance"):
                    has_active_workflow = True
                    logger.info(f"  检测到活跃工作流 (phase={phase})，跳过本次计划任务")
            except Exception:
                pass

        if has_active_workflow:
            logger.info("=" * 50)
            logger.info("[SKIP] 已有工作流在运行中，计划任务退出")
            logger.info("=" * 50)
            return 0

        logger.info("  Flask 在运行但无活跃工作流，继续执行")
    else:
        logger.info("  无运行中的 Flask，继续执行")

    # ---- 1. 唤醒屏幕 (超时保护：计划任务上下文可能没有活动桌面) ----
    logger.info("[1/4] 唤醒屏幕...")
    try:
        _run_with_timeout(dismiss_screensaver, timeout=10, label="唤醒屏幕")
    except Exception as e:
        logger.warning(f"唤醒屏幕异常（继续执行）: {e}")

    # ---- 2. 锁屏检测与解锁 (超时保护) ----
    logger.info("[2/4] 检测锁屏状态...")
    try:
        locked = _run_with_timeout(is_workstation_locked, timeout=5, label="锁屏检测")
        if locked:
            pin = config.get("auto_start", {}).get("lock_password", "")
            unlocked = _run_with_timeout(unlock_with_pin, args=(pin,), timeout=45, label="解锁")
            if not unlocked:
                logger.error("❌ 解锁失败或超时！后续步骤仍会尝试")
        else:
            logger.info("  未锁屏，跳过解锁")
    except Exception as e:
        logger.warning(f"锁屏检测/解锁异常（继续执行）: {e}")

    # ---- 3. 启动 Flask + 打开浏览器 (对齐 start.bat [1/3] + [3/3]) ----
    logger.info("[3/4] 启动 Flask 服务器并打开 WebUI...")
    try:
        start_cme_application(project_root)
    except Exception as e:
        logger.error(f"启动 WebUI 失败: {e}", exc_info=True)

    # ---- 4. 检测微信 (对齐 start.bat [2/3]，独立子进程不拖垮主进程) ----
    logger.info("[4/4] 检测微信状态（独立子进程）...")
    wechat_ready = False
    try:
        wechat_ready = check_wechat_in_subprocess()
    except Exception as e:
        logger.warning(f"微信子进程异常: {e}")

    if not wechat_ready:
        # 对齐 start.bat: 失败后等10秒再重试
        logger.warning("微信首次检测未就绪（对齐 start.bat），10秒后重试...")
        time.sleep(10)
        try:
            wechat_ready = check_wechat_in_subprocess()
        except Exception as e:
            logger.error(f"微信子进程重试异常: {e}")

    if wechat_ready:
        logger.info("[OK] 微信已就绪")
    else:
        logger.warning("⚠ 微信未就绪（WebUI 已正常启动），考勤时微信发送会自动回退")

    logger.info("=" * 50)
    logger.info("[OK] 自动启动完成")
    logger.info("=" * 50)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        logger.error(f"❌ 未捕获异常，程序崩溃: {e}", exc_info=True)
        sys.exit(1)
