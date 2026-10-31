"""课程状态同步 — 从网站读取举办/授分状态，更新 courses.yaml

在每日任务结束或手动触发时，读取网站「未举办/逾期未举办/待授分」Tab 的课程列表，
与 courses.yaml 比对后更新：
  - 未举办/逾期未举办 → held=false（未举办）
  - 待授分            → held=true, granted=false（已举办、待授分）；
                        且 created_date<=今天 → 补 sign_in/sign_out=true（已考勤）
  - 其余且已创建且 created_date<=今天 → 推断 held=true, granted=true,
                        sign_in=true, sign_out=true（已举办并授分）
  - 网页上存在的课程   → created=true（校准创建状态）
"""

import os
import re
import time
import logging
from datetime import date

import yaml

logger = logging.getLogger("cme")

# ===== 常量 =====
_TAB_ALL = "tab_1_0"            # 全部
_TAB_NOT_HELD = "tab_1_1"       # 未举办
_TAB_HELD = "tab_1_2"           # 已举办（预留，未读取）
_TAB_PENDING_GRANT = "tab_1_3"  # 待授分
_TAB_OVERDUE = "tab_1_4"        # 逾期未举办

_COURSES_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "courses.yaml",
)


# ============================================================================
# 工具函数
# ============================================================================

def _strip_suffix(name):
    """去掉活动名称末尾的括号后缀，与 course_creator 保持一致。"""
    clean = re.sub(r'[（(][^）)]*[）)]\s*$', '', name).strip()
    return clean


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


# ============================================================================
# Tab 切换与课程名读取
# ============================================================================

def _switch_to_tab(frame, tab_id, label):
    """切换到指定 Tab（layui API 优先，鼠标事件序列兜底，验证 layui-this）"""
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
            if (!tab) tab = document.querySelector('li[lay-id="' + tabId + '"]');
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
        logger.info(f"已切换到[{label}] Tab ({result.get('msg')})")
    else:
        logger.warning(f"切换[{label}] Tab失败: {result.get('msg')}")

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
        logger.info(f"  ✅ [{label}] Tab 切换验证通过")
    else:
        logger.warning(f"  ⚠️ [{label}] Tab 切换可能未生效，当前激活: {verify.get('active')}")

    return verify.get('ok', False)


def _read_course_names(frame):
    """读取当前 Tab 表格中的课程名称列表（去重）"""
    result = frame.evaluate("""
        () => {
            let names = [];
            let tbodies = document.querySelectorAll(
                '#cmeGroupproj tbody, .layui-table-body tbody');
            for (let tbody of tbodies) {
                let rows = tbody.querySelectorAll('tr');
                for (let row of rows) {
                    let cell = row.querySelector('td[data-field="groupProjectName"] .layui-table-cell');
                    if (cell) {
                        let t = (cell.innerText || cell.textContent || '').trim();
                        if (t) names.push(t);
                    }
                }
            }
            return {ok: true, names: Array.from(new Set(names))};
        }
    """)
    if result and result.get('ok'):
        return result.get('names', [])
    return []


# ============================================================================
# yaml 读写
# ============================================================================

def _load_courses():
    """加载 courses.yaml"""
    if not os.path.exists(_COURSES_YAML):
        raise FileNotFoundError(f"courses.yaml 不存在: {_COURSES_YAML}")
    with open(_COURSES_YAML, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _save_courses(data):
    """原子写回 courses.yaml"""
    tmp_path = _COURSES_YAML + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, _COURSES_YAML)


# ============================================================================
# 主流程
# ============================================================================

def _apply_status_update(data, not_held_names, pending_grant_names, today_str):
    """比对网页状态并更新 yaml 数据（纯函数，便于单测）。

    Args:
        data: courses.yaml 已加载的 dict（会被就地修改）
        not_held_names: 未举办/逾期未举办的课程名列表
        pending_grant_names: 待授分的课程名列表
        today_str: "YYYY-MM-DD"

    Returns:
        (updated, inferred): 更新的课程数、其中按日期推断的课程数
    """
    not_held_norm = {_strip_suffix(n) for n in not_held_names}
    pending_norm = {_strip_suffix(n) for n in pending_grant_names}

    updated = 0
    inferred = 0
    for cat in data.get("categories", []):
        for c in cat.get("courses", []):
            cname = c.get("course_name", "")
            norm = _strip_suffix(cname)
            status = c.setdefault("status", {})
            changed = False

            scheduled = status.get("created_date")
            date_ok = bool(scheduled) and str(scheduled) <= today_str

            if norm in not_held_norm:
                # 未举办/逾期：held=false；网页存在 → created=true
                if status.get("held") is not False:
                    status["held"] = False
                    changed = True
                if status.get("created") is not True:
                    status["created"] = True
                    changed = True
            elif norm in pending_norm:
                # 待授分：已举办但未授分
                if status.get("held") is not True:
                    status["held"] = True
                    changed = True
                if status.get("granted") is not False:
                    status["granted"] = False
                    changed = True
                if status.get("created") is not True:
                    status["created"] = True
                    changed = True
                # 网页上存在即视为已创建；计划日期 <= 今天 → 已考勤（签到+签退）
                if date_ok:
                    if status.get("sign_in") is not True:
                        status["sign_in"] = True
                        changed = True
                    if status.get("sign_out") is not True:
                        status["sign_out"] = True
                        changed = True
            else:
                # 都不在：已创建 且 计划日期 <= 今天 → 推断已举办+已授分+已考勤
                if status.get("created", False) and date_ok:
                    if status.get("held") is not True:
                        status["held"] = True
                        changed = True
                    if status.get("granted") is not True:
                        status["granted"] = True
                        changed = True
                    if status.get("sign_in") is not True:
                        status["sign_in"] = True
                        changed = True
                    if status.get("sign_out") is not True:
                        status["sign_out"] = True
                        changed = True
                    inferred += 1

            if changed:
                updated += 1

    return updated, inferred


# ============================================================================
# 过期未考勤课程清理 — 已创建未考勤且已过考勤时间 → 网站删除 + yaml 重置
# ============================================================================

def _find_overdue_candidates(data, today, sign_out_time="20:45"):
    """找出 yaml 中"已创建 + 计划日期已过考勤时间"的过期课程候选。

    过期定义（考勤只能在当天进行，错过即无法补）：
      - 计划举办日期(created_date) < 今天；或
      - == 今天 且 当前时间 >= 当天签退时间（考勤窗口已关闭）

    注意：这里只判断"创建 + 日期过期"，是否"未考勤/待举办"由网页『待举办』
    tab 名单确认（2026-08-24 用户要求：不能仅凭 yaml 判断）。

    Returns:
        [(category, course), ...]: 过期候选课程列表
    """
    from datetime import datetime
    now_hhmm = datetime.now().strftime("%H:%M")
    overdue = []
    for cat in data.get("categories", []):
        for c in cat.get("courses", []):
            st = c.get("status", {})
            if not st.get("created"):
                continue
            cd = st.get("created_date")
            if not cd:
                continue
            try:
                cdate = date.fromisoformat(str(cd))
            except (ValueError, TypeError):
                continue
            if cdate < today:
                overdue.append((cat, c))
            elif cdate == today and now_hhmm >= sign_out_time:
                overdue.append((cat, c))
    return overdue


def _select_overdue_to_delete(overdue_candidates, web_pending):
    """从过期候选中筛选出网页『待举办』名单内的课程（交集）。

    判断标准（2026-08-24 用户要求）：以网页『待举办』tab 为准，
    不能仅凭 yaml。yaml 提供"已创建+过期"，网页确认"确实待举办/未考勤"。

    Returns:
        [(category, course), ...]: 真正要删除的课程
    """
    pending_norm = {_strip_suffix(n) for n in web_pending}
    result = []
    for cat, c in overdue_candidates:
        norm = _strip_suffix(c.get("course_name", ""))
        if norm in pending_norm:
            result.append((cat, c))
    return result


def _reset_course_uncreated(course):
    """删除课程后，将 yaml 状态重置为未创建（可重新排期创建）"""
    st = course.setdefault("status", {})
    for key in ["created", "created_date", "sign_in", "sign_out",
                "sign_in_date", "sign_out_date", "granted", "granted_date", "held"]:
        st.pop(key, None)
    st["created"] = False
    st["created_date"] = None


def _delete_course_from_website(page, course_name):
    """在科室院内活动列表中删除指定课程（点击删除按钮 → 确认弹窗 → 确定）。

    基于网页 DOM：每行操作列有 <a lay-event="del">删除</a>；点击后 layui
    弹出确认框，确定按钮为 .layui-layer-btn0。

    Returns:
        bool: 是否删除成功
    """
    logger.info(f"删除过期未考勤课程: {course_name[:40]}")
    frame = _get_dept_frame(page)
    if frame is None:
        logger.error("找不到 deptGroupproj iframe，无法删除")
        return False

    search_name = _strip_suffix(course_name)

    # 1. 找到课程行并点击删除按钮（lay-event="del"）
    result = frame.evaluate("""
        (courseName) => {
            let tbodies = document.querySelectorAll(
                '#cmeGroupproj tbody, .layui-table-body tbody');
            for (let tbody of tbodies) {
                let rows = tbody.querySelectorAll('tr');
                for (let row of rows) {
                    let text = (row.innerText || row.textContent || '');
                    if (text.indexOf(courseName) >= 0) {
                        let btn = row.querySelector('a[lay-event="del"]');
                        if (btn) {
                            ['mousedown','mouseup','click'].forEach(t => {
                                btn.dispatchEvent(new MouseEvent(t,
                                    {bubbles: true, cancelable: true, view: window}));
                            });
                            btn.click();
                            return {ok: true, found: text.substring(0, 50)};
                        }
                    }
                }
            }
            return {ok: false, msg: 'course or del btn not found'};
        }
    """, search_name[:40])
    if not result.get('ok'):
        logger.warning(f"  网页未找到课程或删除按钮: {search_name[:30]}")
        return False
    logger.info(f"  已点击删除按钮: {result.get('found', '')[:30]}")
    time.sleep(1.5)

    # 2. 点击确认弹窗的"确定/删除"按钮
    confirm_clicked = False
    deadline = time.time() + 8
    while time.time() < deadline and not confirm_clicked:
        # 策略1: .layui-layer-btn0（layui 确认框第一个按钮，通常为"确定/删除"）
        for scope in [page] + list(page.frames):
            try:
                btn = scope.locator(".layui-layer-btn0").first
                if btn.count() > 0:
                    txt = btn.inner_text().strip()
                    if any(k in txt for k in ["确定", "删除", "是", "确认"]):
                        btn.click()
                        confirm_clicked = True
                        logger.info(f"  已点击确认删除: '{txt}'")
                        break
            except Exception:
                continue
        if confirm_clicked:
            break
        # 策略2: 任意含"确定/删除"文字的可见按钮
        for scope in [page] + list(page.frames):
            try:
                btn = scope.locator("button:has-text('确定'), button:has-text('删除')").first
                if btn.count() > 0:
                    btn.click()
                    confirm_clicked = True
                    logger.info("  已点击确认删除 (text match)")
                    break
            except Exception:
                continue
        if not confirm_clicked:
            time.sleep(0.5)

    if not confirm_clicked:
        logger.warning("  未找到确认按钮，删除可能未执行")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    # 3. 等待删除完成（弹窗关闭/行消失）
    time.sleep(2.0)
    logger.info(f"  ✅ 已删除: {course_name[:40]}")
    return True


def sync_courses_from_website(page):
    """主入口：从网站读取举办/授分状态，比对并更新 courses.yaml。

    额外：删除"已创建 + 未考勤 + 已过考勤时间"的过期课程
    （网站删除 + yaml 重置为未创建，便于重新排期）。

    Args:
        page: Playwright Page（需已登录并导航到科室院内活动页面）
    """
    logger.info("=== 课程状态同步：读取网页举办/授分状态 ===")
    today = date.today()
    today_str = today.isoformat()

    frame = _get_dept_frame(page)
    if frame is None:
        logger.warning("找不到 deptGroupproj iframe，跳过同步")
        return

    # 1. 先切到"全部"，确保页面表格已渲染
    _switch_to_tab(frame, _TAB_ALL, "全部")
    time.sleep(1.5)

    # 1.5 删除过期未考勤课程
    # 判断标准（2026-08-24 用户要求）：以网页『待举办』tab 为准 + yaml 已创建 + 日期已过考勤时间
    data = _load_courses()
    sign_out_time = "20:45"
    try:
        from ..utils.config import load_config
        sign_out_time = str(load_config().get("attendance", {}).get("sign_out_time", "20:45"))
    except Exception:
        pass

    # ① 读网页『待举办』tab 的课程名单（以网页实际状态为准）
    _switch_to_tab(frame, _TAB_NOT_HELD, "未举办")
    web_pending = set(_read_course_names(frame))
    _switch_to_tab(frame, _TAB_ALL, "全部")
    logger.info(f"  网页『待举办』课程 {len(web_pending)} 门: {sorted(web_pending)}")

    # ② yaml 中已创建且日期已过考勤时间（候选）
    overdue_candidates = _find_overdue_candidates(data, today, sign_out_time)

    # ③ 取交集：网页待举办 ∩ yaml过期 → 真正要删的
    to_delete = _select_overdue_to_delete(overdue_candidates, web_pending)
    skipped = len(overdue_candidates) - len(to_delete)
    if skipped:
        logger.info(
            f"  {skipped} 门 yaml 过期但网页非『待举办』（已考勤/已举办），跳过不删")

    changed = False
    if to_delete:
        logger.warning(f"发现 {len(to_delete)} 门『待举办+已过期+未考勤』课程，自动删除...")
        for cat, c in to_delete:
            logger.warning(
                f"  - #{c.get('id')} {c.get('course_name', '')[:40]} "
                f"(计划 {c.get('status', {}).get('created_date')})")
        deleted = 0
        for cat, c in to_delete:
            cname = c.get("course_name", "")
            if _delete_course_from_website(page, cname):
                _reset_course_uncreated(c)
                deleted += 1
        if deleted > 0:
            _save_courses(data)
            changed = True
            logger.info(f"  ✅ 已删除 {deleted}/{len(to_delete)} 门过期课程，yaml 重置为未创建")
    else:
        logger.info("  无『待举办+已过期』课程需删除")

    # 2. 读"未举办" + "逾期未举办"
    not_held = set()
    _switch_to_tab(frame, _TAB_NOT_HELD, "未举办")
    not_held.update(_read_course_names(frame))
    _switch_to_tab(frame, _TAB_OVERDUE, "逾期未举办")
    not_held.update(_read_course_names(frame))
    logger.info(f"  未举办/逾期未举办 共 {len(not_held)} 门")

    # 3. 读"待授分"
    _switch_to_tab(frame, _TAB_PENDING_GRANT, "待授分")
    pending_grant = set(_read_course_names(frame))
    logger.info(f"  待授分 共 {len(pending_grant)} 门")

    # 4. 比对更新 yaml（复用前面加载的 data）
    updated, inferred = _apply_status_update(data, not_held, pending_grant, today_str)
    if updated > 0:
        changed = True

    if changed:
        _save_courses(data)
        logger.info(f"  ✅ 已更新课程状态（推断 {inferred} 门）")
    else:
        logger.info("  无需更新（yaml 与网页一致）")

    logger.info(f"SYNC_DONE: 未举办={len(not_held)} 待授分={len(pending_grant)} 更新={updated}")
