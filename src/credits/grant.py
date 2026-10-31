"""授分自动化 — 已举办活动的学分授予"""

import logging
import re
import time
from datetime import date

logger = logging.getLogger("cme")


def _strip_suffix(name):
    """去掉活动名称末尾的括号后缀，与 course_creator 保持一致。"""
    clean = re.sub(r'[（(][^）)]*[）)]\s*$', '', name).strip()
    return clean


# 授分查找顺序：优先『待授分』tab（就是要授分的课程集合，通常单页放得下），
# 兜底『全部』tab（含分页遍历，覆盖课程不在待授分 tab 的情况）。
_GRANT_TABS = [
    ("tab_1_3", "待授分"),
    ("tab_1_0", "全部"),
]


# ============================================================================
# 工具
# ============================================================================

def _get_dept_frame(page):
    """获取 deptGroupproj 页面的 iframe"""
    for f in page.frames:
        if 'deptGroupproj' in f.url:
            return f
    return None


def _find_in_any_frame(page, js_code, arg=None):
    """在所有 frame 中执行 JS，返回第一个 ok=true 的结果"""
    for f in page.frames:
        try:
            result = f.evaluate(js_code, arg) if arg is not None else f.evaluate(js_code)
            if result and result.get('ok'):
                return result, f
        except Exception:
            continue
    try:
        result = page.evaluate(js_code, arg) if arg is not None else page.evaluate(js_code)
        if result and result.get('ok'):
            return result, page
    except Exception:
        pass
    return None, None


def _wait_for_selector_any_frame(page, selector, timeout=8000):
    """条件等待任意 frame 中 selector 的元素出现（出现即返回，不等满）。

    替代固定 sleep：页面加载快时几乎不耗时，慢时最多等 timeout ms。
    只返回可见元素；找不到返回 None（供 JS 兜底确认前使用）。

    Returns:
        Locator | None: 找到的可见元素 locator，超时返回 None
    """
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        for scope in [page] + list(page.frames):
            try:
                loc = scope.locator(selector).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        time.sleep(0.3)
    return None


# ============================================================================
# 主流程
# ============================================================================

def grant_credits(page, course_name):
    """对指定课程执行授分完整流程。

    流程：
      1. 切换到『待授分』tab（tab_1_3）查找课程；未命中回退『全部』tab（tab_1_0，含分页遍历）
      2. 在表格中找到匹配 course_name 的行，点击 "授分" (lay-event="grantPoints")
      3. 点击 "考勤记录授分" (lay-event="attendanceRecordAwardPoints")
      4. 点击 "筛选授分人员" 按钮
      5. 全选人员
      6. 点击 "授分" → 弹窗填策略+日期 → 确认

    Returns:
        bool: 是否成功
    """
    logger.info(f"=== 授分流程: {course_name[:40]} ===")

    frame = _get_dept_frame(page)
    if frame is None:
        logger.error("找不到 deptGroupproj iframe")
        return False

    # ---- Step 1-2: 在「待授分」→「全部」tab 中查找课程行并点击 "授分" ----
    # 2026-08-31：『全部』tab 已积累 50+ 门课、分页显示，待授分课程可能被挤到第 2 页，
    # 单查当前页会"未找到"。改为优先查『待授分』tab（就是要授分的课程，单页放得下），
    # 兜底『全部』tab（含分页遍历，见 _click_grant_points_in_row）。
    found = False
    for tab_id, tab_label in _GRANT_TABS:
        if not _switch_to_tab(frame, tab_id, tab_label):
            continue
        if _click_grant_points_in_row(frame, course_name, tab_label):
            found = True
            break
    if not found:
        logger.error(f"未找到课程或授分按钮: {course_name[:40]}")
        return False

    # ---- Step 3: 点击 "考勤记录授分" ----
    if not _click_lay_event_in_any_frame(page, "attendanceRecordAwardPoints", "考勤记录授分"):
        logger.error("未找到'考勤记录授分'按钮")
        return False

    # ---- Step 4: 点击 "筛选授分人员" ----
    if not _click_button_by_text(page, "筛选授分人员"):
        logger.error("未找到'筛选授分人员'按钮")
        return False

    # ---- Step 5: 全选人员 ----
    if not _select_all_personnel(page):
        logger.error("全选人员失败")
        return False

    # ---- Step 6: 授权弹窗 — 点"授分"按钮 → 填策略+日期 → 确认 ----
    if not _open_and_submit_grant_dialog(page):
        logger.error("授分弹窗操作失败")
        return False

    logger.info(f"✅ 授分完成: {course_name[:40]}")
    return True


# ============================================================================
# Step 1: Tab 切换（待授分 / 全部）
# ============================================================================

def _switch_to_tab(frame, tab_id, label):
    """切换到指定 Tab，确保能看到对应状态的课程。

    layui 的 tab 切换不能靠简单的 element.click()，需要用 layui API
    或完整的鼠标事件序列才能触发其内部事件监听。

    Args:
        frame: deptGroupproj 页面的 iframe
        tab_id: layui tab id，如 'tab_1_0'(全部) / 'tab_1_3'(待授分)
        label: tab 显示名，用于日志
    """
    logger.info(f"点击'{label}' tab...")
    result = frame.evaluate("""
        (tabId) => {
            // 策略1: layui 原生 API（最可靠）
            try {
                if (window.layui && layui.element) {
                    layui.element.tabChange('tabarea_1', tabId);
                    return {ok: true, msg: 'layui.element.tabChange'};
                }
            } catch(e) {}

            // 策略2: 完整鼠标事件序列
            let tab = document.getElementById(tabId);
            if (!tab) {
                tab = document.querySelector('li[lay-id="' + tabId + '"]');
            }
            if (tab) {
                if (tab.classList.contains('layui-this')) {
                    return {ok: true, msg: 'already active'};
                }
                ['mousedown','mouseup','click'].forEach(t => {
                    tab.dispatchEvent(new MouseEvent(t,
                        {bubbles: true, cancelable: true, view: window}));
                });
                tab.click();
                return {ok: true, msg: 'clicked with events'};
            }
            return {ok: false, msg: 'tab not found: ' + tabId};
        }
    """, tab_id)

    time.sleep(1.0)  # 等待 layui AJAX 重新加载表格数据

    if result.get('ok'):
        logger.info(f"已切换[{label}] tab ({result.get('msg')})")
    else:
        logger.warning(f"切换[{label}] tab结果: {result}")

    # 验证切换是否生效
    verify = frame.evaluate("""
        (tabId) => {
            let tab = document.getElementById(tabId);
            if (tab && tab.classList.contains('layui-this')) return {ok: true};
            tab = document.querySelector('li[lay-id="' + tabId + '"]');
            if (tab && tab.classList.contains('layui-this')) return {ok: true};
            return {ok: false, active: document.querySelector('#tabarea_1 li.layui-this')?.id || 'unknown'};
        }
    """, tab_id)
    if verify.get('ok'):
        logger.info(f"  ✅ [{label}] tab切换验证通过")
    else:
        logger.warning(f"  ⚠️ [{label}] tab切换可能未生效，当前激活: {verify.get('active')}")

    return verify.get('ok', False)


# ============================================================================
# Step 2: 找到课程行，点击 "授分" 按钮
# ============================================================================

# 在表格中按课程名匹配并点击 "授分" 按钮的 JS（当前页 DOM 内执行）
_GRANT_SEARCH_JS = """
    (courseName) => {
        let tbodies = document.querySelectorAll(
            '#cmeGroupproj tbody, .layui-table-body tbody');
        for (let tbody of tbodies) {
            let rows = tbody.querySelectorAll('tr');
            for (let row of rows) {
                let text = (row.innerText || row.textContent || '');
                if (text.indexOf(courseName) >= 0 ||
                    courseName.indexOf(text.trim()) >= 0) {
                    let btn = row.querySelector('a[lay-event="grantPoints"]');
                    if (btn) {
                        ['mousedown','mouseup','click'].forEach(t => {
                            btn.dispatchEvent(new MouseEvent(t,
                                {bubbles: true, cancelable: true, view: window}));
                        });
                        btn.click();
                        return {ok: true, found_row: text.substring(0, 60)};
                    }
                }
            }
        }
        return {ok: false, error: 'course or grantPoints btn not found'};
    }
"""

# 当前页课程名签名（用于判断分页是否卡住——点击下一页后签名不变则停止）
_PAGE_SIGNATURE_JS = """
    () => {
        let names = [];
        let tbodies = document.querySelectorAll(
            '#cmeGroupproj tbody, .layui-table-body tbody');
        for (let tbody of tbodies) {
            let rows = tbody.querySelectorAll('tr');
            for (let row of rows) {
                let cell = row.querySelector('td[data-field="groupProjectName"] .layui-table-cell');
                let t = cell ? (cell.innerText || cell.textContent || '').trim() : '';
                if (t) names.push(t);
            }
        }
        return names.join('|');
    }
"""

# 点击 layui 分页的"下一页"控件
_CLICK_NEXT_PAGE_JS = """
    () => {
        let next = document.querySelector('a.layui-laypage-next');
        if (!next) next = document.querySelector('.layui-laypage-next');
        if (next && next.classList.contains('layui-disabled')) {
            return {ok: false, msg: 'next disabled'};
        }
        if (next) {
            ['mousedown','mouseup','click'].forEach(t => {
                next.dispatchEvent(new MouseEvent(t,
                    {bubbles: true, cancelable: true, view: window}));
            });
            next.click();
            return {ok: true};
        }
        // 兜底：按"下一页"文本找（a/span/li 中文本恰为"下一页"且未禁用）
        let all = document.querySelectorAll('a, span, li');
        for (let el of all) {
            if ((el.innerText || '').trim() === '下一页' &&
                !el.classList.contains('layui-disabled')) {
                el.click();
                return {ok: true};
            }
        }
        return {ok: false, msg: 'no next control'};
    }
"""


def _click_grant_points_in_row(frame, course_name, tab_label="全部"):
    """在当前 tab 的表格中匹配 course_name 的行，点击 '授分' 按钮。

    2026-08-31：科室已积累 50+ 门课，『全部』tab 分页显示，待授分课程可能被挤到
    第 2 页之后。当前页没找到时遍历后续分页再找；找不到分页控件或已是最后一页即停止。

    Args:
        frame: deptGroupproj iframe
        course_name: 完整课程名（内部去括号后缀匹配）
        tab_label: 当前所在 tab 名，用于日志

    Returns:
        bool: 是否点击成功
    """
    search_name = _strip_suffix(course_name)[:40]
    logger.info(
        f"查找课程行并点击'授分'[{tab_label}]: '{course_name[:40]}' "
        f"→ 去括号: '{search_name}'")

    max_pages = 20
    prev_signature = None
    for page_idx in range(1, max_pages + 1):
        result = frame.evaluate(_GRANT_SEARCH_JS, search_name)
        if result.get('ok'):
            logger.info(f"  已点击授分[{tab_label}] 第{page_idx}页: "
                        f"{result.get('found_row', '')}")
            # 条件等待'考勤记录授分'按钮出现（替代固定 sleep(2.0)，出现即返回）
            _wait_for_selector_any_frame(
                frame.page, "a[lay-event='attendanceRecordAwardPoints']", timeout=8000)
            return True

        # 当前页没找到 → 记录课程名签名，准备翻下一页
        signature = frame.evaluate(_PAGE_SIGNATURE_JS) or ''
        if page_idx > 1 and signature == prev_signature:
            logger.warning(f"  第{page_idx}页与上一页课程相同，判定分页卡住，停止遍历")
            break
        prev_signature = signature

        next_res = frame.evaluate(_CLICK_NEXT_PAGE_JS)
        if not next_res.get('ok'):
            if page_idx == 1:
                logger.warning(
                    f"  未找到: {result}（当前页无分页或已是最后一页）")
            else:
                logger.info(f"  已在第{page_idx}页翻到底，未找到课程")
            break
        time.sleep(0.8)  # 等表格重载到下一页

    return False


# ============================================================================
# Step 3-4: 点击 lay-event 按钮 / 文字按钮（跨 frame）
# ============================================================================

def _click_lay_event_in_any_frame(page, lay_event, label):
    """在所有 frame 中查找并点击 lay-event 按钮。

    优化（2026-08-24）：原实现 `btn.click()` 无 timeout（默认 30s），失败后
    continue 到下一个 frame 再等 30s，多个 frame 依次白等导致每门课授分时间
    线性暴涨（20s→370s）。改为：条件等待元素出现 → click 短超时(5s) →
    失败立即 JS 兜底，不跨 frame 累积等待。
    """
    logger.info(f"查找并点击: {label} (lay-event='{lay_event}')")

    btn = _wait_for_selector_any_frame(page, f"a[lay-event='{lay_event}']")
    if btn is not None:
        try:
            btn.scroll_into_view_if_needed()
            btn.click(timeout=5000)
            logger.info(f"  已点击: {label}")
            page.wait_for_timeout(800)
            return True
        except Exception:
            try:
                btn.evaluate("el => el.click()")
                logger.info(f"  已点击: {label} (JS force)")
                page.wait_for_timeout(800)
                return True
            except Exception as e:
                logger.warning(f"  点击失败: {label}: {e}")

    # JS fallback（轮询：元素可见但常规点击失败时）
    js = f"""() => {{
        let btns = document.querySelectorAll('a[lay-event="{lay_event}"]');
        for (let btn of btns) {{
            if (btn.offsetWidth > 0 || btn.offsetHeight > 0) {{
                ['mousedown','mouseup','click'].forEach(t => {{
                    btn.dispatchEvent(new MouseEvent(t,
                        {{bubbles: true, cancelable: true, view: window}}));
                }});
                btn.click();
                return {{ok: true}};
            }}
        }}
        return {{ok: false}};
    }}"""
    deadline = time.time() + 8
    while time.time() < deadline:
        result, _ = _find_in_any_frame(page, js)
        if result and result.get('ok'):
            logger.info(f"  已点击: {label} (JS)")
            page.wait_for_timeout(1000)
            return True
        time.sleep(0.4)
    logger.warning(f"  未找到: {label}")
    return False


def _click_button_by_text(page, text):
    """在所有 frame 中查找并点击文字按钮。

    优化（2026-08-24）：同 _click_lay_event_in_any_frame —— 消除默认 30s
    click 超时在多个 frame 上的累积等待。
    """
    logger.info(f"查找并点击: '{text}'")

    btn = _wait_for_selector_any_frame(page, f"button:has-text('{text}')")
    if btn is not None:
        try:
            btn.scroll_into_view_if_needed()
            btn.click(timeout=5000)
            logger.info(f"  已点击: {text}")
            page.wait_for_timeout(800)
            return True
        except Exception:
            try:
                btn.evaluate("el => el.click()")
                logger.info(f"  已点击: {text} (JS force)")
                page.wait_for_timeout(800)
                return True
            except Exception as e:
                logger.warning(f"  点击失败: {text}: {e}")

    # JS fallback（轮询）
    js = f"""() => {{
        let buttons = document.querySelectorAll('button');
        for (let btn of buttons) {{
            if ((btn.innerText || btn.textContent || '').indexOf('{text}') >= 0) {{
                btn.click();
                return {{ok: true}};
            }}
        }}
        return {{ok: false}};
    }}"""
    deadline = time.time() + 8
    while time.time() < deadline:
        result, _ = _find_in_any_frame(page, js)
        if result and result.get('ok'):
            logger.info(f"  已点击: {text} (JS)")
            page.wait_for_timeout(1000)
            return True
        time.sleep(0.4)
    logger.warning(f"  未找到: {text}")
    return False


# ============================================================================
# Step 5: 全选人员
# ============================================================================

def _select_all_personnel(page):
    """在 creditAnalysis 表格中点击全选 checkbox。

    layui 的 lay-skin="primary" checkbox 会隐藏原始 <input>，
    渲染一个 <div class="layui-form-checkbox"> 作为可见的点击代理。
    必须点击这个 div 而非原始 input。

    优化（2026-08-24）：固定 wait 改为条件等待；已全选则幂等跳过。
    """
    logger.info("全选人员...")

    # 策略1: 条件等待全选 checkbox 渲染，点击可见的 .layui-form-checkbox div
    proxy = _wait_for_selector_any_frame(
        page, ".layui-table-header .layui-form-checkbox", timeout=8000)
    if proxy is not None:
        # 幂等：已全选（layui-form-checked）则跳过，避免重复等待
        try:
            cls = proxy.get_attribute("class") or ""
            if "layui-form-checked" in cls:
                logger.info("  全选: 已处于全选状态，跳过")
                return True
        except Exception:
            pass
        try:
            proxy.scroll_into_view_if_needed()
            proxy.click(timeout=3000)
            logger.info("  全选: clicked .layui-form-checkbox in thead")
            page.wait_for_timeout(500)
            return True
        except Exception:
            pass

    # 策略2: 通过全选checkbox的容器cell定位 → 点击其中的 div
    for scope in [page] + list(page.frames):
        try:
            cell = scope.locator('.laytable-cell-checkbox').first
            if cell.count() > 0:
                proxy = cell.locator('.layui-form-checkbox').first
                if proxy.count() > 0:
                    proxy.click(timeout=3000)
                    logger.info("  全选: clicked .layui-form-checkbox in .laytable-cell-checkbox")
                    page.wait_for_timeout(800)
                    return True
        except Exception:
            pass

    # 策略3: JS — 找到隐藏的 input → 点击它后面的 .layui-form-checkbox 兄弟元素
    js = """() => {
        // layui 全选 checkbox 特征: lay-filter="layTableAllChoose"
        let input = document.querySelector('input[lay-filter="layTableAllChoose"]');
        if (!input) {
            // fallback: thead 中任意 checkbox
            let header = document.querySelector('.layui-table-header');
            if (header) input = header.querySelector('input[type="checkbox"]');
        }
        if (!input) return {ok: false, msg: 'checkbox input not found'};

        // layui 把原始 input 隐藏，渲染一个 div.layui-form-checkbox 作为代理
        // 这个 div 是 input 的兄弟节点
        let parent = input.parentElement;
        let proxy = parent ? parent.querySelector('.layui-form-checkbox') : null;
        if (!proxy) {
            // 尝试在整个cell中找
            let cell = input.closest('.laytable-cell-checkbox') || input.closest('td');
            if (cell) proxy = cell.querySelector('.layui-form-checkbox');
        }
        if (!proxy) return {ok: false, msg: 'proxy div not found'};

        proxy.scrollIntoView({block: 'center'});
        ['mousedown','mouseup','click'].forEach(t => {
            proxy.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
        });
        proxy.click();
        return {ok: true, msg: 'clicked proxy div via JS'};
    }"""
    result, _ = _find_in_any_frame(page, js)

    import time
    time.sleep(0.8)

    if result and result.get('ok'):
        logger.info(f"  全选(JS): {result.get('msg')}")
        return True

    # 策略4: 直接用键盘 Ctrl+A 全选（最后手段）
    try:
        page.keyboard.press("Control+a")
        time.sleep(0.5)
        logger.info("  全选: Ctrl+A")
        return True
    except Exception:
        pass

    logger.warning("  全选失败：所有策略均未成功")
    return False


# ============================================================================
# Step 6: 授权弹窗 — 点"授分"按钮 → 填策略+日期 → 确认
# ============================================================================

def _open_and_submit_grant_dialog(page):
    """点击'授分'按钮打开弹窗，设置策略和日期，确认"""
    logger.info("点击'授分'按钮打开授分弹窗...")

    # 1. 点击 "授分" 按钮 — 该按钮特征: onclick="openAwardDate()"
    clicked = False

    # 策略1: Playwright 精确匹配 onclick 属性
    for scope in [page] + list(page.frames):
        try:
            btn = scope.locator('button[onclick="openAwardDate()"]').first
            if btn.count() > 0:
                btn.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                btn.click(timeout=3000)
                logger.info("  已点击授分按钮 (onclick=openAwardDate)")
                clicked = True
                break
        except Exception:
            continue

    # 策略2: 文字匹配
    if not clicked:
        for scope in [page] + list(page.frames):
            try:
                btn = scope.locator('button:has-text("授分")').first
                if btn.count() > 0:
                    btn.scroll_into_view_if_needed()
                    page.wait_for_timeout(300)
                    btn.click(timeout=3000)
                    logger.info("  已点击授分按钮 (text match)")
                    clicked = True
                    break
            except Exception:
                continue

    # 策略3: JS 直接调用 openAwardDate()
    if not clicked:
        logger.info("  尝试 JS 调用 openAwardDate()...")
        js = """() => {
            if (typeof openAwardDate === 'function') {
                openAwardDate();
                return {ok: true};
            }
            // 找 onclick 含 openAwardDate 的 button 并点击
            let btns = document.querySelectorAll('button');
            for (let btn of btns) {
                let oc = btn.getAttribute('onclick') || '';
                if (oc.indexOf('openAwardDate') >= 0) {
                    btn.click();
                    return {ok: true};
                }
            }
            return {ok: false};
        }"""
        result, _ = _find_in_any_frame(page, js)
        if result and result.get('ok'):
            logger.info("  已点击授分按钮 (JS)")
            clicked = True
        else:
            logger.warning("  所有策略均未找到授分按钮")

    # 2. 条件等待授分弹窗出现（creditStrategy_id 下拉），再设置策略
    _wait_for_selector_any_frame(page, "#creditStrategy_id", timeout=8000)
    logger.info("设置授分策略...")
    js_strategy = """() => {
        let select = document.getElementById('creditStrategy_id');
        if (select) {
            select.value = '4';
            select.dispatchEvent(new Event('change', {bubbles: true}));
            // Also try layui form render
            try { if (window.layui && layui.form) layui.form.render('select'); }
            catch(e) {}
            return {ok: true, value: select.value};
        }
        // Try creditStrategy_ids (hidden form)
        select = document.getElementById('creditStrategy_ids');
        if (select) {
            select.value = '4';
            select.dispatchEvent(new Event('change', {bubbles: true}));
            return {ok: true, value: select.value};
        }
        return {ok: false};
    }"""
    result, _ = _find_in_any_frame(page, js_strategy)
    if result and result.get('ok'):
        logger.info(f"  授分策略已设置: {result.get('value')}")
    else:
        logger.warning("  未找到授分策略下拉框")

    # 3. 设置授分日期为今天
    today_str = date.today().strftime("%Y-%m-%d")
    logger.info(f"设置授分日期: {today_str}")
    js_date = f"""() => {{
        let inp = document.getElementById('creditStrategyDate_ids');
        if (inp) {{
            inp.value = '{today_str}';
            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
            return {{ok: true}};
        }}
        return {{ok: false}};
    }}"""
    result, _ = _find_in_any_frame(page, js_date)
    if result and result.get('ok'):
        logger.info(f"  授分日期已设置: {today_str}")
    else:
        logger.warning("  未找到授分日期输入框")

    page.wait_for_timeout(300)

    # 4. 点击 layui 弹窗确认按钮 (.layui-layer-btn0 "授分")
    logger.info("点击弹窗确认('授分')...")
    for scope in [page] + list(page.frames):
        try:
            btn = scope.locator(".layui-layer-btn0").first
            if btn.count() > 0:
                text = btn.inner_text()
                if "授分" in text:
                    btn.scroll_into_view_if_needed()
                    page.wait_for_timeout(300)
                    btn.click()
                    logger.info(f"  已点击弹窗确认: '{text}'")
                    page.wait_for_timeout(1500)
                    return True
        except Exception:
            continue

    # Fallback: click any .layui-layer-btn0
    for scope in [page] + list(page.frames):
        try:
            btn = scope.locator(".layui-layer-btn0").first
            if btn.count() > 0:
                btn.click()
                logger.info("  已点击弹窗确认 (fallback)")
                page.wait_for_timeout(1500)
                return True
        except Exception:
            continue

    logger.warning("  未找到弹窗确认按钮")
    return False
