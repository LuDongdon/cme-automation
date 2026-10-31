"""登录认证模块 — 包含人工验证码输入"""

import logging
from .selectors import SELECTORS

logger = logging.getLogger("cme")

SEL = SELECTORS["login"]


def check_already_logged_in(page):
    """检测是否已登录 — 即时检测，不等待"""
    return page.get_by_text("人员管理").first.is_visible()


def login(page, verification_callback):
    """
    执行登录流程。

    Args:
        page: Playwright Page 对象
        verification_callback: 当需要验证码时调用，签名 callback(prompt_msg) -> str
                               返回用户输入的验证码字符串

    Returns:
        bool: 登录是否成功
    """
    logger.info("=== 开始登录流程 ===")

    # Step 1: 等待页面稳定
    page.wait_for_timeout(1000)

    # Step 2: 检查并填入密码
    username_input = page.locator(SEL["username_input"]).first
    password_input = page.locator(SEL["password_input"]).first

    username_val = username_input.input_value()
    password_val = password_input.input_value()

    if username_val and password_val:
        logger.info("检测到已填充账号密码")
    elif username_val:
        # 尝试点击密码框触发Edge自动填充
        logger.info("密码未填充，尝试点击密码框触发Edge填充...")
        password_input.click()
        page.wait_for_timeout(1500)
        password_val = password_input.input_value()
        if password_val:
            logger.info("密码已填充")
        else:
            logger.warning("密码仍未填充，从配置文件读取...")
            _fill_from_config(username_input, password_input)
    else:
        logger.info("账号密码均未填充，从配置文件读取...")
        _fill_from_config(username_input, password_input)

    # Step 4: 点击登录按钮（找可见的那个）
    login_btn = _find_visible_login_button(page)
    if not login_btn:
        logger.error("找不到登录按钮")
        return False

    logger.info("点击登录按钮...")
    login_btn.click()

    # Step 5: 等待安全验证弹窗
    logger.info("等待安全验证弹窗...")
    try:
        page.wait_for_selector(SEL["verification_recipient_select"], timeout=10000)
        logger.info("安全验证弹窗已出现")
        # 不需要置顶 — 用户在 Web UI 输入验证码，网站可以留在后台
    except Exception:
        logger.warning("未检测到安全验证弹窗（可能已免验证或已登录）")
        page.wait_for_timeout(2000)
        if _check_logged_in(page):
            logger.info("已直接登录成功（无需验证）")
            return True
        return False

    # Step 6: 选择验证码接收人
    _select_verification_recipient(page)

    # Step 7-10: 获取验证码 → 填入 → 确认
    # 策略：失败后按钮进入冷却，冷却期间继续监控剪贴板，
    # 若剪贴板出现新验证码则立即提交；冷却结束仍无新码则重新点击"获取验证码"
    max_retries = 5
    for attempt in range(max_retries):
        try:
            code = None

            if attempt == 0:
                # 第1次：点击"获取验证码" → 等待剪贴板/手动输入
                _trigger_get_verification_code(page)
                code = verification_callback("请输入手机验证码", timeout=120)
            else:
                # 第2-5次：先检查按钮冷却，冷却期间监控剪贴板
                cooldown = _get_cooldown_seconds(page)
                if cooldown > 0:
                    logger.info(
                        f"按钮冷却 {cooldown} 秒，持续监控剪贴板（有变化的新验证码立即填入）..."
                    )
                    code = verification_callback(
                        f"验证码错误，冷却{cooldown}秒内监控剪贴板新验证码 (第{attempt+1}次)",
                        timeout=cooldown + 5,
                    )
                    if code:
                        logger.info(f"冷却期间剪贴板检测到新验证码: {code}")
                    else:
                        logger.info("冷却结束，剪贴板无新验证码")

                # 冷却期间没拿到新验证码 → 冷却结束后点击"获取验证码"
                if not code:
                    logger.info(f"重新获取验证码... (第{attempt+1}次)")
                    _trigger_get_verification_code(page)
                    code = verification_callback(
                        f"请重新输入验证码 (第{attempt+1}次重试)",
                        timeout=120,
                    )

            if not code:
                logger.error("未获取到验证码")
                if attempt < max_retries - 1:
                    continue
                else:
                    break

            # 填入验证码 — 用多种方式确保填入成功
            _fill_verification_code(page, code)

            # 点击确认
            page.wait_for_timeout(500)
            try:
                page.locator(".layui-layer-btn0").first.click()
                logger.info("已点击确认 (layui-layer-btn0)")
            except Exception:
                page.keyboard.press("Enter")
                logger.info("已点击确认 (Enter)")

            # 检查登录结果
            page.wait_for_timeout(3000)
            if _check_logged_in(page):
                logger.info("登录成功")
                return True

            if attempt < max_retries - 1:
                logger.warning(
                    f"验证码错误 ({attempt+1}/{max_retries})，进入冷却监控..."
                )
            else:
                logger.error(f"登录失败：已尝试 {max_retries} 次验证码")

        except Exception as e:
            logger.error(f"登录尝试 {attempt+1}/{max_retries} 异常: {e}")
            if attempt < max_retries - 1:
                logger.info("将在2秒后重试...")
                page.wait_for_timeout(2000)
            else:
                logger.error(f"登录失败：已重试 {max_retries} 次，最后一次异常: {e}")

    return False


def _ensure_account_login_tab(page):
    """确保在账号登录Tab"""
    try:
        tab = page.locator(SEL["账号登录_tab"]).first
        tab.click()
        page.wait_for_timeout(500)
        logger.info("已切换到账号登录Tab")
    except Exception:
        pass  # 可能已经在正确的Tab


def _find_visible_login_button(page):
    """找到当前可见的登录按钮"""
    buttons = page.locator(SEL["login_button"]).all()
    for btn in buttons:
        if btn.is_visible():
            return btn
    return buttons[0] if buttons else None


def _fill_from_config(username_input, password_input):
    """从配置文件读取账号密码并自动填入"""
    from ..utils.config import load_config
    try:
        config = load_config()
        username = config["login"].get("username", "")
        password = config["login"].get("password", "")
        if username:
            username_input.fill(username)
            logger.info(f"已从配置填入账号: {username}")
        if password:
            password_input.fill(password)
            logger.info("已从配置填入密码")
    except Exception as e:
        logger.warning(f"从配置读取账号密码失败: {e}")


def _select_verification_recipient(page, recipient=None):
    """在安全验证弹窗中选择验证码接收人（layui dd下拉）"""
    from ..utils.config import load_config

    if recipient is None:
        try:
            config = load_config()
            recipient = config["login"].get("verification_recipient", "")
        except Exception:
            recipient = ""

    if not recipient:
        logger.warning("未配置验证码接收人（config.yaml → login.verification_recipient），跳过自动选择")
        return

    logger.info(f"选择验证接收人: {recipient}")

    try:
        # 点击"请选择"输入框打开layui下拉
        select_input = page.locator(SEL["verification_recipient_select"]).first
        select_input.click()
        page.wait_for_timeout(800)

        # 用键盘选择：ArrowDown 导航到目标，Enter 确认
        # 先看看有哪些选项
        dds = page.locator(SEL["verification_recipient_options"]).all()
        target_idx = -1
        for i, dd in enumerate(dds):
            if dd.is_visible() and recipient in (dd.inner_text() or ""):
                target_idx = i
                break

        if target_idx >= 0:
            # 按键导航到目标
            for _ in range(target_idx):
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(100)
            # Enter 确认选择
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
            logger.info(f"已选择接收人: {recipient} (键盘, idx={target_idx})")
        else:
            logger.warning(f"在下拉列表中未找到: {recipient}")

        # 验证
        val = select_input.input_value()
        logger.info(f"接收人输入框当前值: '{val}'")
        if not val or recipient not in val:
            # 最后手段：用 JS 直接设置 layui 的值
            logger.warning("键盘选择也失败，尝试 JS 直接设置...")
            page.evaluate(f"""
                // 找到 layui 的 select 并设置值
                var form = layui.form;
                var selects = document.querySelectorAll('select');
                for (var s of selects) {{
                    for (var o of s.options) {{
                        if (o.text.indexOf('{recipient}') >= 0) {{
                            s.value = o.value;
                            form.render('select');
                            break;
                        }}
                    }}
                }}
            """)
            page.wait_for_timeout(500)
            val = select_input.input_value()
            logger.info(f"JS设置后: '{val}'")
    except Exception as e:
        logger.warning(f"选择接收人失败: {e}")


def _trigger_get_verification_code(page):
    """点击获取验证码按钮。如果按钮禁用（冷却中），用 JS 强制点击。"""
    import re

    page.wait_for_timeout(500)
    btns = page.locator("button[name='code_btn']").all()
    for btn in btns:
        if not btn.is_visible():
            continue

        try:
            if btn.is_disabled():
                # 按钮冷却中，用 JS 移除禁用并强制点击
                logger.info("按钮禁用中，JS 强制点击...")
                page.evaluate("""
                    const btn = document.querySelector('button[name="code_btn"]');
                    if (btn) {
                        btn.removeAttribute('disabled');
                        btn.classList.remove('layui-disabled');
                        btn.click();
                    }
                """)
            else:
                btn.click(timeout=5000)
        except Exception:
            btn.click(force=True, timeout=5000)

        logger.info("已点击'获取验证码'")
        return

    # fallback
    page.locator("button[name='code_btn']").first.click(force=True, timeout=5000)
    logger.info("已点击'获取验证码'(force)")


def _get_cooldown_seconds(page):
    """读取'获取验证码'按钮上的冷却倒计时秒数。
    按钮文字示例: "109秒后重获"

    Returns:
        int: 剩余冷却秒数，无冷却返回 0
    """
    import re

    try:
        btns = page.locator("button[name='code_btn']").all()
        for btn in btns:
            if not btn.is_visible():
                continue
            if btn.is_disabled():
                text = btn.inner_text() or ""
                m = re.search(r'(\d+)\s*秒', text)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return 0


def _fill_verification_code(page, code):
    """将验证码填入弹窗输入框"""
    code_filled = False

    code_selectors = [
        "input[name='code'][placeholder*='验证码']",
        "input.yzm",
        "input[name='code']",
        "input#code",
    ]

    for sel in code_selectors:
        try:
            inp = page.locator(sel).first
            if inp.count() > 0:
                inp.click(force=True, timeout=2000)
                page.wait_for_timeout(200)
                inp.fill('', force=True, timeout=2000)
                inp.fill(code, force=True, timeout=2000)
                val = inp.input_value()
                if val == code:
                    code_filled = True
                    logger.info(f"验证码已填入 ({sel}): {code}")
                    break
                else:
                    logger.warning(f"填入后值不匹配: expected={code}, got={val}")
        except Exception:
            continue

    if not code_filled:
        try:
            page.evaluate(f"""
                const inputs = document.querySelectorAll('input[name="code"]');
                for (const inp of inputs) {{
                    inp.value = '{code}';
                    inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            """)
            logger.info(f"验证码已填入 (JS evaluate): {code}")
        except Exception as e:
            logger.error(f"所有填入方式均失败: {e}")


def _check_logged_in(page):
    """检查是否已登录（通过页面元素判断）"""
    try:
        # 检测是否有导航菜单（登录成功后会显示）
        page.wait_for_selector("text=人员管理", timeout=3000)
        return True
    except Exception:
        # 检查是否有登录相关的错误提示
        if page.locator("text=用户名或密码错误").count() > 0:
            logger.error("用户名或密码错误")
        if page.locator("text=验证码错误").count() > 0:
            logger.error("验证码错误")
        return False
