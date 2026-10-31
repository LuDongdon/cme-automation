"""二维码考勤管理 — 签到/签退流程，QR截图本地保存 + 微信发送"""

import os
import re
import time
import logging
from datetime import datetime

logger = logging.getLogger("cme")


def _strip_suffix(name):
    """去掉活动名称末尾的括号后缀，与 course_creator 保持一致。
    '下巴与颊部再生（骨相重塑）' → '下巴与颊部再生'
    """
    clean = re.sub(r'[（(][^）)]*[）)]\s*$', '', name).strip()
    return clean


# ============================================================================
# 工具函数
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


def _empty_result():
    """返回一个空的考勤结果字典，用于错误时统一返回格式。"""
    return {
        "screenshots": [],
        "error": "deptGroupproj frame or table not found",
    }


def _countdown_sleep(total_seconds, label="", log_interval=None):
    """纯 Python 倒计时，不依赖任何网页元素。

    日志频率根据剩余时间动态调整：
      - 剩余 >  1小时  → 每20分钟 log 一次
      - 剩余 > 15分钟  → 每 5分钟 log 一次
      - 剩余 >  1分钟  → 每 1分钟 log 一次
      - 剩余 <= 1分钟  → 每 1秒   log 一次

    Args:
        total_seconds: 总倒计时秒数
        label: 日志前缀标签
        log_interval: 固定日志间隔秒数（指定后覆盖动态逻辑）
    """
    import time

    def _dynamic_interval(remaining):
        if log_interval is not None:
            return log_interval
        if remaining > 3600:        # > 1小时
            return 1200              # 每20分钟
        elif remaining > 900:       # > 15分钟
            return 300               # 每5分钟
        elif remaining > 60:        # > 1分钟
            return 60                # 每1分钟
        else:                        # <= 1分钟
            return 1                 # 每秒

    label_tag = f"[{label}] " if label else ""
    elapsed = 0
    while elapsed < total_seconds:
        remaining = total_seconds - elapsed
        interval = _dynamic_interval(remaining)
        logger.info(f"{label_tag}⏳ 剩余 {remaining}s / {total_seconds}s")
        sleep_chunk = min(interval, remaining)
        time.sleep(sleep_chunk)
        elapsed += sleep_chunk


# ============================================================================
# 主流程
# ============================================================================

def start_attendance_flow(page, course_name, qr_timeout=300, resend_count=3,
                           wechat_sender=None, wechat_groups=None, mode="签到",
                           sign_out_time=None):
    """
    执行完整的考勤流程（签到或签退）。

    Args:
        page: Playwright Page
        course_name: 课程名称
        qr_timeout: 二维码时效（秒），默认300
        resend_count: 发送二维码次数，默认3
        wechat_sender: 微信发送函数 callback(image_path, group_name, text_msg=None) -> bool
        wechat_groups: 微信目标群列表
        mode: "签到" 或 "签退"
        sign_out_time: 签退时间 "HH:MM"（仅签到最后一张码时需要）

    Returns:
        dict: {"screenshots": [path1, path2, ...]}
    """
    logger.info(f"=== {mode}流程开始: {course_name[:40]} ===")
    logger.info(f"二维码时效: {qr_timeout}秒, 共 {resend_count} 张")

    # Step 1: 在列表中找到课程并点击"扫码考勤"
    if not _find_and_click_scan_qr(page, course_name):
        logger.error(f"未找到课程或扫码考勤按钮: {course_name}")
        return _empty_result()

    # Step 2: 展开批次 → 点击"开始考勤"
    if not _click_start_attendance(page):
        logger.error("点击'开始考勤'失败")
        return _empty_result()

    # Step 3: 考勤配置弹窗 — 点击确定
    if not _confirm_attendance_dialog(page):
        logger.error("考勤配置确定失败")
        return _empty_result()

    # Step 4: QR时效预热（300→5s→3→5s→300→10s）
    logger.info("QR时效预热: 300秒→5s→3秒→5s→300秒→10s")
    _set_qr_timeout_to(page, 300)
    _countdown_sleep(5, "等待300s触发")
    _set_qr_timeout_to(page, 3)
    _countdown_sleep(5, "等待3s触发")
    _set_qr_timeout_to(page, qr_timeout)
    _countdown_sleep(10, "等待最终时效生效")
    page.bring_to_front()
    logger.info("浏览器窗口已置顶，开始截图")

    screenshots = []

    # 截图循环：截图 → 文字提示 → 发微信 → 等300s
    for i in range(resend_count):
        seq = i + 1
        logger.info(f"{mode}: 第 {seq}/{resend_count} 张二维码")

        # 截图（始终本地保存）
        path = _screenshot_qr(page, mode, seq)
        if path:
            screenshots.append(path)
            logger.info(f"  第{seq}张截图已保存: {path}")

        # 生成文字说明
        next_dt = datetime.now() + __import__('datetime').timedelta(seconds=qr_timeout)
        next_time_str = next_dt.strftime("%H:%M")

        at_all = "@所有人 " if seq == 1 else ""

        if mode == "签到" and seq == resend_count and sign_out_time:
            text_msg = f"{at_all}{mode}码 {seq}/{resend_count}\n签到完成\n签退码将在 {sign_out_time} 开始发送"
        elif seq == resend_count:
            text_msg = f"{at_all}{mode}码 {seq}/{resend_count}\n{mode}完成"
        else:
            text_msg = f"{at_all}{mode}码 {seq}/{resend_count}\n下一个{mode}码将在 {next_time_str} 发送"

        # 发送微信（文字+图片）
        if wechat_sender and wechat_groups and path:
            for group in wechat_groups:
                try:
                    wechat_sender(path, group, text_msg=text_msg)
                    logger.info(f"  已发送到: {group}")
                except Exception as e:
                    logger.warning(f"  发送失败 [{group}]: {e}")

        # 等待（每张截图后都等满300s）
        _countdown_sleep(qr_timeout, f"{mode}第{seq}张")

    logger.info(f"=== {mode}完成: 共 {len(screenshots)} 张截图 ===")
    _close_qr_popup(page)

    return {
        "screenshots": screenshots,
        "first_nonzero_time": None,
        "count_history": [],
        "max_count": 0,
        "final_count": 0,
    }


# ============================================================================
# 倒计时等待（已废弃人数监控）
# ============================================================================

def _monitor_attendance(page, total_seconds, count_history):
    """纯倒计时等待（已废弃实时人数监控，保持向后兼容签名）
    Returns: None"""
    logger.info(f"  ⏳ 等待 {total_seconds} 秒...")
    _countdown_sleep(total_seconds, "考勤")
    return None


# ============================================================================
# Step 0: 从表格读取课程时间
# ============================================================================

def read_course_times_from_table(page, course_name):
    """点击课程的"扫码考勤"，读取实际开始/结束时间，保持停留在子页面。

    流程：
      1. 在科室院内活动列表中定位匹配 course_name 的行
      2. 点击"扫码考勤"按钮 → 进入课程详情页（含 #child0 子表）
      3. 从 #child0 表格中读取 data-field="startDate" / "endDate"
      4. 停留在子页面（不返回列表），供后续考勤流程直接使用

    Args:
        page: Playwright Page
        course_name: 课程名称/活动名称（用于匹配行）

    Returns:
        dict or None: {"start_date": "2026-07-26 18:57:00",
                        "end_date": "2026-07-26 20:45:00"}
                      如果找不到课程或读取失败，返回 None
    """
    frame = _get_dept_frame(page)
    if frame is None:
        logger.error("找不到 deptGroupproj iframe，无法读取课程时间")
        return None

    # 先切换到"全部"tab，确保活动无论处于何种状态都能被找到
    _switch_to_all_tab(frame)

    # 去括号后缀，与表单填充时保持一致
    search_name = _strip_suffix(course_name)
    logger.info(f"搜索课程: '{course_name[:40]}' → 去括号: '{search_name[:40]}'")

    # ---- Step 1: 找到课程行，点击"扫码考勤" ----
    click_result = frame.evaluate("""
        (courseName) => {
            let tbodies = document.querySelectorAll('.layui-table-body tbody, '
                + '#cmeGroupproj tbody, table tbody');
            for (let tbody of tbodies) {
                let rows = tbody.querySelectorAll('tr');
                for (let row of rows) {
                    let text = (row.innerText || row.textContent || '');
                    if (text.indexOf(courseName) >= 0 || courseName.indexOf(text.trim()) >= 0) {
                        let btn = row.querySelector('a[lay-event="sweepCodeAttendance"]');
                        if (btn) {
                            btn.click();
                            return {ok: true, msg: 'clicked sweepCodeAttendance'};
                        }
                    }
                }
            }
            return {ok: false, error: 'course or sweepCodeAttendance btn not found'};
        }
    """, search_name[:40])

    if not click_result.get('ok'):
        logger.warning(f"未找到课程或扫码考勤按钮: {search_name[:40]}")
        return None

    logger.info(f"已点击扫码考勤: {course_name[:40]}")
    page.wait_for_timeout(3000)

    # ---- Step 2: 读取 #child0 表格的 startDate / endDate ----
    times = _read_child_table_times(page)

    if times and times.get('start_date'):
        logger.info(
            f"从扫码考勤页面读取课程时间: "
            f"{times['start_date']} ~ {times['end_date']}"
        )
        logger.info("停留在扫码考勤子页面，等待后续考勤流程")
        return times

    logger.warning(f"未能从扫码考勤页面读取到课程时间")
    return None


def _read_child_table_times(page):
    """在扫码考勤后的子页面中，读取 #child0 的 startDate / endDate。

    layui 表格结构：
      <table id="child0" lay-filter="child0"></table>   ← 空占位
      <div class="layui-table-view">                     ← 实际渲染的数据
        <div class="layui-table-body layui-table-main">
          <table><tbody><tr>
            <td data-field="startDate">2026-07-26 18:57:00</td>
            <td data-field="endDate">2026-07-26 20:45:00</td>
          </tr></tbody></table>
        </div>
      </div>

    Returns:
        dict or None: {"start_date": "2026-07-26 18:57:00",
                        "end_date": "2026-07-26 20:45:00"}
    """
    js = """() => {
        // 1) 先通过 #child0 定位，然后在紧随其后的 .layui-table-view 中读取
        let placeholder = document.getElementById('child0');
        let container = placeholder ? placeholder.parentElement : document;

        // 2) 在 #child0 的父容器内找 .layui-table-body.layui-table-main 中的行
        let bodies = container.querySelectorAll('.layui-table-body.layui-table-main');
        for (let body of bodies) {
            let rows = body.querySelectorAll('tbody tr');
            for (let row of rows) {
                let startCell = row.querySelector('td[data-field="startDate"]');
                let endCell   = row.querySelector('td[data-field="endDate"]');
                let startDate = '';
                let endDate   = '';
                if (startCell) {
                    startDate = (startCell.innerText || startCell.textContent || '').trim();
                }
                if (endCell) {
                    endDate = (endCell.innerText || endCell.textContent || '').trim();
                }
                if (startDate || endDate) {
                    return {ok: true, startDate: startDate, endDate: endDate};
                }
            }
        }

        // 3) fallback: 全局搜索 .layui-table-body tbody tr 中带 startDate/endDate 的 td
        let allBodies = document.querySelectorAll('.layui-table-body');
        for (let body of allBodies) {
            let rows = body.querySelectorAll('tbody tr');
            for (let row of rows) {
                let startCell = row.querySelector('td[data-field="startDate"]');
                let endCell   = row.querySelector('td[data-field="endDate"]');
                if (startCell || endCell) {
                    let sd = startCell ? (startCell.innerText || startCell.textContent || '').trim() : '';
                    let ed = endCell   ? (endCell.innerText   || endCell.textContent   || '').trim() : '';
                    if (sd || ed) {
                        return {ok: true, startDate: sd, endDate: ed};
                    }
                }
            }
        }

        return {ok: false, error: 'no startDate/endDate found anywhere'};
    }"""

    # 列出所有 frame URL 以便调试
    frame_urls = []
    for i, f in enumerate(page.frames):
        try:
            frame_urls.append(f"  frame[{i}]: {f.url[:80]}")
        except Exception:
            pass
    logger.info(f"搜索子表时间，当前 frames ({len(page.frames)}):\n" + "\n".join(frame_urls))

    # 在所有 frame 中搜索
    for i, f in enumerate(page.frames):
        try:
            result = f.evaluate(js)
            if result and result.get('ok') and (result.get('startDate') or result.get('endDate')):
                logger.info(f"  在 frame[{i}] 中找到子表时间: {result['startDate']} ~ {result['endDate']}")
                return {"start_date": result["startDate"], "end_date": result["endDate"]}
        except Exception as e:
            logger.debug(f"  frame[{i}] 搜索跳过: {e}")

    # 也搜主页面
    try:
        result = page.evaluate(js)
        if result and result.get('ok') and (result.get('startDate') or result.get('endDate')):
            logger.info(f"  在主页面中找到子表时间: {result['startDate']} ~ {result['endDate']}")
            return {"start_date": result["startDate"], "end_date": result["endDate"]}
    except Exception:
        pass

    logger.warning(f"读取子表时间失败: 在所有 frame 中都没找到 startDate/endDate")
    return None


def _back_to_activity_list(page):
    """关闭当前 tab 并重新打开科室院内活动列表"""
    logger.info("返回科室院内活动列表...")
    try:
        # 关闭当前激活的 tab
        page.evaluate("""
            () => {
                let activeTab = document.querySelector('li.layui-this .layui-tab-close');
                if (activeTab) { activeTab.click(); }
            }
        """)
        page.wait_for_timeout(500)
    except Exception as e:
        logger.warning(f"关闭tab失败: {e}")

    # 重新导航到科室院内活动
    from ..browser.navigator import goto_dept_activity
    goto_dept_activity(page)

    # 等待 layui 表格渲染完成
    page.wait_for_timeout(2000)
    frame = _get_dept_frame(page)
    if frame:
        try:
            frame.wait_for_selector('.layui-table-body', timeout=10000)
            logger.info("列表页表格已就绪")
        except Exception:
            logger.warning("等待列表页表格超时")
    logger.info("已返回科室院内活动列表")


def start_attendance_from_child_page(page, course_name, qr_timeout=300,
                                     resend_count=3, wechat_sender=None,
                                     wechat_groups=None, mode="签到",
                                     sign_out_time=None):
    """在已打开的扫码考勤子页面上直接继续考勤流程。跳过 _find_and_click_scan_qr。"""
    logger.info(f"=== {mode}流程（从子页面继续）: {course_name[:40]} ===")

    if not _click_start_attendance(page):
        logger.error("点击'开始考勤'失败")
        return _empty_result()
    if not _confirm_attendance_dialog(page):
        logger.error("考勤配置确定失败")
        return _empty_result()

    # QR 时效预热
    logger.info("QR时效预热: 300→5s→3→5s→300→10s")
    _set_qr_timeout_to(page, 300)
    _countdown_sleep(5, "等待300s触发")
    _set_qr_timeout_to(page, 3)
    _countdown_sleep(5, "等待3s触发")
    _set_qr_timeout_to(page, qr_timeout)
    _countdown_sleep(10, "等待最终时效生效")
    page.bring_to_front()
    logger.info("已置顶，开始截图")

    screenshots = []

    for i in range(resend_count):
        seq = i + 1
        logger.info(f"{mode}: 第 {seq}/{resend_count} 张二维码")

        path = _screenshot_qr(page, mode, seq)
        if path:
            screenshots.append(path)
            logger.info(f"  第{seq}张截图已保存: {path}")

        # 文字说明
        next_dt = datetime.now() + __import__('datetime').timedelta(seconds=qr_timeout)
        next_time_str = next_dt.strftime("%H:%M")

        at_all = "@所有人 " if seq == 1 else ""

        if mode == "签到" and seq == resend_count and sign_out_time:
            text_msg = f"{at_all}{mode}码 {seq}/{resend_count}\n签到完成\n签退码将在 {sign_out_time} 开始发送"
        elif seq == resend_count:
            text_msg = f"{at_all}{mode}码 {seq}/{resend_count}\n{mode}完成"
        else:
            text_msg = f"{at_all}{mode}码 {seq}/{resend_count}\n下一个{mode}码将在 {next_time_str} 发送"

        # 发微信
        if wechat_sender and wechat_groups and path:
            for group in wechat_groups:
                try:
                    wechat_sender(path, group, text_msg=text_msg)
                    logger.info(f"  已发送到: {group}")
                except Exception as e:
                    logger.warning(f"  发送失败 [{group}]: {e}")

        _countdown_sleep(qr_timeout, f"{mode}第{seq}张")

    logger.info(f"=== {mode}完成: 共 {len(screenshots)} 张截图 ===")
    _close_qr_popup(page)

    return {
        "screenshots": screenshots,
        "first_nonzero_time": None,
        "count_history": [],
        "max_count": 0,
        "final_count": 0,
    }


# ============================================================================
# Step 1: 找到活动行，点击"扫码考勤"
# ============================================================================

def _switch_to_all_tab(frame):
    """切换到'全部' tab（tab_1_0），确保能看到所有状态的活动。

    签到后活动会被系统移到'已举办'栏目，如果不切到'全部'tab，
    在'未举办'或其他tab中就找不到该活动。

    layui 的 tab 切换不能靠简单的 element.click()，需要用 layui API
    或完整的鼠标事件序列才能触发其内部事件监听。
    """
    import time

    result = frame.evaluate("""
        () => {
            // 策略1: layui 原生 API（最可靠）
            try {
                if (window.layui && layui.element) {
                    layui.element.tabChange('tabarea_1', 'tab_1_0');
                    return {ok: true, msg: 'layui.element.tabChange'};
                }
            } catch(e) {}

            // 策略2: 完整鼠标事件序列
            let tab = document.getElementById('tab_1_0');
            if (!tab) {
                tab = document.querySelector('li[lay-id="tab_1_0"]');
            }
            if (tab) {
                // 先检查是否已经是激活状态
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
            return {ok: false, msg: 'tab_1_0 not found'};
        }
    """)

    if result.get('ok'):
        logger.info(f"已切换到'全部'tab ({result.get('msg')})")
    else:
        logger.warning(f"切换到'全部'tab失败: {result.get('msg')}")

    # 验证切换是否生效：检查 layui-this 是否在 tab_1_0 上
    time.sleep(1.0)
    verify = frame.evaluate("""
        () => {
            let tab = document.getElementById('tab_1_0');
            if (tab && tab.classList.contains('layui-this')) {
                return {ok: true};
            }
            tab = document.querySelector('li[lay-id="tab_1_0"]');
            if (tab && tab.classList.contains('layui-this')) {
                return {ok: true};
            }
            return {ok: false, active: document.querySelector('#tabarea_1 li.layui-this')?.id || 'unknown'};
        }
    """)
    if verify.get('ok'):
        logger.info("  ✅ tab切换验证通过 (layui-this 在 tab_1_0)")
    else:
        logger.warning(f"  ⚠️ tab切换可能未生效，当前激活: {verify.get('active')}")

    return verify.get('ok', False)


def _find_and_click_scan_qr(page, course_name):
    """在主表格中找到指定活动，点击扫码考勤按钮。先在'全部'tab中查找。"""
    # 去括号后缀，与表单填充时保持一致
    search_name = _strip_suffix(course_name)
    logger.info(f"查找活动: '{course_name[:40]}' → 去括号: '{search_name[:40]}'")

    frame = _get_dept_frame(page)
    if frame is None:
        logger.error("找不到 deptGroupproj iframe")
        return False

    # 先切换到"全部"tab，确保活动无论处于何种状态都能被找到
    _switch_to_all_tab(frame)

    result = frame.evaluate("""
        (courseName) => {
            let tbodies = document.querySelectorAll('#cmeGroupproj tbody, .layui-table-body tbody');
            for (let tbody of tbodies) {
                let rows = tbody.querySelectorAll('tr');
                for (let row of rows) {
                    let text = (row.innerText || row.textContent || '');
                    if (text.indexOf(courseName) >= 0 || courseName.indexOf(text.trim()) >= 0) {
                        let btn = row.querySelector('a[lay-event="sweepCodeAttendance"]');
                        if (btn) {
                            btn.click();
                            return {ok: true, found_row: text.substring(0, 60)};
                        }
                    }
                }
            }
            // Fallback: 只有一个活动时直接点
            let allBtns = document.querySelectorAll('a[lay-event="sweepCodeAttendance"]');
            if (allBtns.length > 0) {
                let allRows = document.querySelectorAll('.layui-table-body tbody tr');
                if (allRows.length === 1) {
                    allBtns[0].click();
                    return {ok: true, found_row: 'only one row'};
                }
            }
            return {ok: false, error: 'activity not found', count: allBtns.length};
        }
    """, search_name[:40])

    if not result.get('ok'):
        logger.warning(f"未找到活动: {result.get('error')}, 扫码按钮数: {result.get('count', 0)}")
        return False

    logger.info(f"已点击扫码考勤: {result.get('found_row', '')}")
    page.wait_for_timeout(2000)
    return True


# ============================================================================
# Step 2: 展开批次 → 点击"开始考勤"
# ============================================================================

def _click_start_attendance(page):
    """在批次表中点击开始考勤按钮 (a[lay-event='appAttend'])"""
    logger.info("查找'开始考勤'按钮...")
    page.wait_for_timeout(1500)

    # 方法1: Playwright locator 在所有 frame 中查找
    for scope in [page] + list(page.frames):
        try:
            btn = scope.locator('a[lay-event="appAttend"]').first
            if btn.count() > 0:
                btn.scroll_into_view_if_needed()
                btn.click(timeout=3000)
                logger.info("已点击开始考勤 (Playwright locator)")
                page.wait_for_timeout(1500)
                return True
        except Exception:
            pass

    # 方法2: JS 完整事件序列
    js = """() => {
        // 先展开批次行（如果需要）
        let expandIcons = document.querySelectorAll('.opTable-i-table-open, [class*="opTable-open"]');
        for (let icon of expandIcons) {
            if (icon.getAttribute('status') !== 'on') icon.click();
        }
        // 查找开始考勤按钮
        let btns = document.querySelectorAll('a[lay-event="appAttend"]');
        for (let btn of btns) {
            if (btn.offsetWidth > 0 || btn.offsetHeight > 0) {
                ['mousedown', 'mouseup', 'click'].forEach(t => {
                    btn.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
                });
                btn.click();
                return {ok: true, msg: 'clicked appAttend via events'};
            }
        }
        if (btns.length > 0) {
            btns[0].click();
            return {ok: true, msg: 'clicked first appAttend (forced)'};
        }
        return {ok: false, msg: 'no appAttend button'};
    }"""

    result, found_frame = _find_in_any_frame(page, js)
    if result and result.get('ok'):
        logger.info(f"已点击开始考勤 (JS): {result.get('msg')}")
        page.wait_for_timeout(1500)
        return True

    logger.warning(f"未找到开始考勤按钮: {result}")
    return False


# ============================================================================
# Step 3: 考勤配置弹窗 → 点击确定
# ============================================================================

def _confirm_attendance_dialog(page):
    """在 layui 确认弹窗中点击'确定'"""
    logger.info("考勤配置: 点击确定...")
    page.wait_for_timeout(1500)

    # 方法1: Playwright locator 在主页面和所有 frame 中查找
    for scope in [page] + list(page.frames):
        try:
            btn = scope.locator('.layui-layer-btn0').first
            if btn.count() > 0:
                btn.scroll_into_view_if_needed()
                btn.click(timeout=3000)
                logger.info("已点击确定 (Playwright locator)")
                page.wait_for_timeout(2000)
                return True
        except Exception:
            pass

    # 方法2: JS 完整事件序列
    js = """() => {
        let btn = document.querySelector('.layui-layer-btn0');
        if (btn) {
            ['mousedown', 'mouseup', 'click'].forEach(t => {
                btn.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
            });
            btn.click();
            return {ok: true, msg: 'clicked via events'};
        }
        // 也搜包含"确定"文字的按钮
        let all = document.querySelectorAll('.layui-layer-btn a, .layui-layer-btn0, .layui-layer-btn1');
        for (let b of all) {
            if ((b.innerText || b.textContent || '').trim() === '确定') {
                ['mousedown', 'mouseup', 'click'].forEach(t => {
                    b.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
                });
                b.click();
                return {ok: true, msg: 'clicked 确定 by text+events'};
            }
        }
        return {ok: false, msg: 'no 确定 button'};
    }"""

    result, _ = _find_in_any_frame(page, js)
    if result and result.get('ok'):
        logger.info(f"已点击确定: {result.get('msg')}")
        page.wait_for_timeout(2000)
        return True

    # 方法3: 键盘 Enter（layui 弹窗的默认确认键）
    logger.info("尝试键盘 Enter 确认...")
    try:
        page.keyboard.press("Enter")
        page.wait_for_timeout(2000)
        return True
    except Exception:
        pass

    logger.warning("未点到确定按钮")
    return False


# ============================================================================
# Step 4: 设置二维码时效
# ============================================================================

def _set_qr_timeout(page, seconds):
    """设置二维码时效时间（先切3秒再切目标值，强制触发两次 change）"""
    _set_qr_timeout_to(page, 3)
    page.wait_for_timeout(500)
    _set_qr_timeout_to(page, seconds)


def _set_qr_timeout_to(page, seconds):
    """直接设置 selectForTime 为指定秒数（单次切换）"""
    target_str = str(seconds)
    js = """(target) => {
        let select = document.getElementById('selectForTime');
        if (!select) return {ok: false, msg: 'selectForTime not found'};
        let options = Array.from(select.querySelectorAll('option'));
        let found = options.some(o => o.value === target);
        if (!found) return {ok: false, msg: 'option not found: ' + target,
                            available: options.map(o => o.value)};
        select.value = target;
        select.dispatchEvent(new Event('change', {bubbles: true}));
        if (typeof selectChange === 'function') selectChange();
        return {ok: true, value: select.value};
    }"""
    result, _ = _find_in_any_frame(page, js, target_str)
    if result and result.get('ok'):
        logger.info(f"QR时效 → {result.get('value')}秒 ✅")
    else:
        logger.warning(f"QR时效设置失败: {result}")
    page.wait_for_timeout(500)


# ============================================================================
# Step 5: 截图二维码
# ============================================================================

def _screenshot_qr(page, mode, index):
    """截取二维码区域，保存到 {截图目录}/{日期}/{mode}{index}.png"""
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{mode}{index}.png"
    # 读取配置的截图保存目录；未配置则用默认桌面二维码截图目录
    base_dir = ""
    try:
        from ..utils.config import load_config
        base_dir = (load_config().get("attendance", {}).get("qr_save_dir") or "").strip()
    except Exception:
        pass
    if not base_dir:
        base_dir = os.path.join(os.path.expanduser("~"), "Desktop", "二维码截图")
    qr_save_dir = os.path.join(base_dir, today)
    os.makedirs(qr_save_dir, exist_ok=True)
    path = os.path.join(qr_save_dir, filename)

    try:
        # 等待 canvas 渲染
        page.wait_for_timeout(500)

        # ---- 策略1: JS 查找 #qrCodeForTeach，clip 精确截图 ----
        for scope in [page] + list(page.frames):
            try:
                info = scope.evaluate("""() => {
                    let el = document.getElementById('qrCodeForTeach');
                    if (!el || el.offsetWidth === 0) return null;
                    let rect = el.getBoundingClientRect();
                    // 往上找 .cent_box 或 .layui-layer
                    let card = el.closest('.cent_box') ||
                               el.closest('.bigbox') ||
                               el.closest('.layui-layer-content') ||
                               el.closest('.layui-layer');
                    let cardRect = card ? card.getBoundingClientRect() : rect;
                    return {
                        x: cardRect.x, y: cardRect.y,
                        width: cardRect.width, height: cardRect.height,
                        found_card: !!card,
                    };
                }""")
                if info:
                    scope_name = "page" if scope == page else f"frame"
                    logger.info(f"二维码截图 ({scope_name}): clip=({info['x']:.0f},{info['y']:.0f} {info['width']:.0f}x{info['height']:.0f})")
                    scope.screenshot(path=path, clip={
                        'x': info['x'], 'y': info['y'],
                        'width': info['width'], 'height': info['height'],
                    })
                    return path
            except Exception:
                continue

        # ---- 策略2: Playwright locator 截图 .cent_box ----
        for scope in [page] + list(page.frames):
            try:
                card = scope.locator('.cent_box').first
                if card.count() > 0:
                    card.screenshot(path=path)
                    logger.info(f"二维码截图 (cent_box locator): {path}")
                    return path
            except Exception:
                continue

        # ---- 策略3: 截图 #qrCodeForTeach 元素本身 ----
        for scope in [page] + list(page.frames):
            try:
                qr = scope.locator('#qrCodeForTeach').first
                if qr.count() > 0:
                    qr.screenshot(path=path)
                    logger.info(f"二维码截图 (qrCodeForTeach): {path}")
                    return path
            except Exception:
                continue

        # ---- 策略4: 全页面兜底 ----
        page.screenshot(path=path)
        logger.info(f"全页面截图: {path}")
        return path

    except Exception as e:
        logger.error(f"截图失败: {e}")
        return None


# ============================================================================
# Step 6: 关闭二维码弹窗
# ============================================================================

def _close_qr_popup(page):
    """关闭二维码弹窗"""
    logger.info("关闭二维码弹窗...")

    js = """() => {
        if (typeof closeParentAll === 'function') {
            closeParentAll();
            return {ok: true, msg: 'closeParentAll()'};
        }
        let closeBtn = document.querySelector('.layui-layer-close');
        if (closeBtn) { closeBtn.click(); return {ok: true, msg: 'clicked layer-close'}; }
        let backBtn = document.querySelector('button[onclick*="closeParentAll"]');
        if (backBtn) { backBtn.click(); return {ok: true, msg: 'clicked back button'}; }
        return {ok: false, msg: 'no close method found'};
    }"""

    result, _ = _find_in_any_frame(page, js)
    if result and result.get('ok'):
        logger.info(f"弹窗已关闭: {result.get('msg')}")
    else:
        logger.info("使用 Escape 关闭弹窗")
        page.keyboard.press("Escape")

    page.wait_for_timeout(1000)
