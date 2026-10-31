"""课程创建模块 — 弹窗在 iframe 内，scope 必须正确"""

import json
import logging
import time
import os

logger = logging.getLogger("cme")


# ============================================================================
# 调试
# ============================================================================

def _dump(scope, label):
    _base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(_base, f"debug_{label}_{int(time.time())}.html")
    try:
        html = scope.evaluate("el => el.outerHTML") if hasattr(scope, 'evaluate') else scope.content()
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"  [DEBUG] → {os.path.basename(path)}")
    except Exception:
        pass


def _debug_frames(page):
    """调试：列出所有 frame 及关键元素"""
    logger.info("====== FRAME DEBUG ======")
    for i, f in enumerate(page.frames):
        try:
            url = f.url[:80]
            layers = f.locator(".layui-layer").count()
            dialogs = f.locator(".layui-layer-dialog").count()
            contents = f.locator(".layui-layer-content").count()
            add_cycle = f.locator("a[lay-event='addCycle']").count()
            next_step = f.locator("button[lay-filter='nextStep']").count()
            gpn = f.locator("input[name='groupProjName']").count()
            logger.info(f"  FRAME[{i}] url={url}")
            logger.info(f"    .layui-layer={layers} .layui-layer-dialog={dialogs} .layui-layer-content={contents}")
            logger.info(f"    addCycle={add_cycle} nextStep={next_step} groupProjName={gpn}")
        except Exception as e:
            logger.info(f"  FRAME[{i}] error: {e}")
    logger.info("========================")


def _log_field(label, ftype, target, hint, ok):
    icon = "✅" if ok else "❌"
    logger.info(f"  {icon} [{label}] type={ftype} target='{target}' | {hint}")


# ============================================================================
# Frame
# ============================================================================

def _get_frame(page):
    if not hasattr(page, 'frames'):
        return page
    for f in page.frames:
        if 'deptGroupproj' in f.url:
            return f
    raise RuntimeError("找不到 deptGroupproj iframe")


# ============================================================================
# 弹窗 scope — 先在 iframe 找，再在主页面找
# ============================================================================

def _get_dialog(frame, page):
    """
    找到当前打开的 layui 表单弹窗。
    弹窗可能在主 iframe 内，也可能在 CycleModal 等子 iframe 中。
    返回 scope Locator，或 None。
    """
    containers = [
        ".layui-layer-dialog",
        ".layui-layer:not(.layui-layer-shade)",
        ".layui-layer-content",
        ".layui-layer",
    ]

    # 搜所有 frame + 主页面（CycleModal 在独立的 iframe 里）
    search_scopes = [frame] + list(page.frames) + [page]

    for scope in search_scopes:
        for sel in containers:
            candidates = scope.locator(sel)
            cnt = candidates.count()
            for i in range(cnt):
                c = candidates.nth(i)
                # 必须有表单内容才算弹窗
                if c.locator("input[name='groupProjName']").count() > 0:
                    scope_name = "page" if scope == page else "frame"
                    logger.info(f"  dialog scope: {sel}[{i}] in {scope_name}")
                    return c
                if c.locator("input[id^='treeSelect-input']").count() > 0:
                    logger.info(f"  dialog scope: {sel}[{i}] in frame (treeSelect)")
                    return c
                # CycleModal / 日期弹窗（name=startDate/startDay 或 name=endDate/endDay）
                if c.locator("input[name='startDate'], input[name='startDay'], input[name='endDate'], input[name='endDay']").count() > 0:
                    logger.info(f"  dialog scope: {sel}[{i}] in frame (date inputs)")
                    return c
                if c.locator("label").count() > 2:
                    logger.info(f"  dialog scope: {sel}[{i}] in frame (labels)")
                    return c

    # 最后手段：返回含 label 最多的那个
    for scope in search_scopes:
        for sel in containers:
            candidates = scope.locator(sel)
            cnt = candidates.count()
            if cnt > 0:
                best = None
                best_labels = 0
                for i in range(cnt):
                    c = candidates.nth(i)
                    n = c.locator("label").count()
                    if n > best_labels:
                        best_labels = n
                        best = c
                if best and best_labels >= 2:
                    logger.info(f"  dialog scope: {sel} fallback ({best_labels} labels)")
                    return best

    return None


# ============================================================================
# Label → row
# ============================================================================

def _find_form_row(scope, label_text):
    """遍历 scope 内 label，优先精确匹配，再模糊匹配"""
    labels = scope.locator("label")
    best = None
    for i in range(labels.count()):
        try:
            label = labels.nth(i)
            text = label.inner_text().strip().replace("*", "").replace("：", "").replace(":", "").strip()
            # 精确匹配优先级最高
            if text == label_text:
                row = label.locator("xpath=ancestor::div[contains(@class,'layui-inline')]").first
                if row.count() > 0:
                    return row
                row = label.locator("xpath=ancestor::div[contains(@class,'layui-form-item')]").first
                if row.count() > 0:
                    return row
            # 模糊匹配作为备选
            if best is None and label_text in text:
                row = label.locator("xpath=ancestor::div[contains(@class,'layui-inline')]").first
                if row.count() > 0:
                    best = row
                else:
                    row = label.locator("xpath=ancestor::div[contains(@class,'layui-form-item')]").first
                    if row.count() > 0:
                        best = row
        except Exception:
            continue
    return best


# ============================================================================
# [TEXT]
# ============================================================================

def _fill_text_by_label(scope, page, label_text, value):
    """TEXT input — 全 JS 实现，限定弹窗内搜索"""
    frame = _get_frame(page)
    val_str = str(value)

    result = frame.evaluate("""
        ({label_text, value}) => {
            // 限定在弹窗表单内搜索（#groupProjForm），避免匹配到页面其他 label
            let container = document.getElementById('groupProjForm') ||
                            document.querySelector('.layui-layer-wrap') ||
                            document;
            let labels = container.querySelectorAll('label');
            let row = null;
            for (let label of labels) {
                let text = (label.innerText || '').replace(/[*：:]/g, '').trim();
                if (text === label_text) {
                    row = label.closest('.layui-inline') || label.closest('.layui-form-item');
                    break;
                }
            }
            if (!row) {
                for (let label of labels) {
                    let text = (label.innerText || '').replace(/[*：:]/g, '').trim();
                    if (text.includes(label_text) || label_text.includes(text)) {
                        row = label.closest('.layui-inline') || label.closest('.layui-form-item');
                        break;
                    }
                }
            }
            if (!row) return {ok: false, error: 'row not found'};
            let inp = row.querySelector('input:not([readonly]):not([type="hidden"])') ||
                      row.querySelector('input');
            if (!inp) return {ok: false, error: 'input not found'};
            let name = inp.name || inp.id || '?';
            inp.focus();
            inp.value = '';
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            inp.value = value;
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            inp.dispatchEvent(new Event('change', {bubbles: true}));
            return {ok: true, name: name, actual: inp.value};
        }
    """, {"label_text": label_text, "value": val_str})

    if not result.get('ok'):
        _log_field(label_text, "TEXT", value, result.get('error', 'unknown'), False)
        return False

    ok = result.get('actual') == val_str
    _log_field(label_text, "TEXT", value, f"name={result.get('name','?')} val='{result.get('actual','')}'", ok)
    return ok


# ============================================================================
# [SELECT] layui 下拉 — dd 渲染在 frame body 下，不在 row 内
# ============================================================================

def _select_by_label(scope, page, label_text, value):
    """layui select — 全 JS 实现，限定弹窗内搜索。

    年度/活动内容/学分等选项是弹窗打开后 AJAX 懒加载的，可能晚于弹窗渲染。
    所以不能"立刻打开下拉去点"——先等目标 <dd> 在 DOM 里出现（row 内→全
    frame 兜底，模糊匹配），出现后若下拉未展开再点 trigger 打开，点 dd，最后
    回读 display input 验证值已生效。任何一步没成都会重试，最多 ~12s，避免
    "选项没加载导致无法选择"。
    """
    frame = _get_frame(page)
    target = str(value)

    # 清除残留 shade，避免遮罩挡住 trigger 点击导致下拉打不开
    _dismiss_residual_shades(frame, page, caller="_select_by_label")

    deadline = time.time() + 12.0
    last_error = ""

    while time.time() < deadline:
        # 1) 定位 row + trigger（同原有逻辑）
        row_result = frame.evaluate("""
            ({label_text}) => {
                let container = document.getElementById('groupProjForm') ||
                                document.querySelector('.layui-layer-wrap') ||
                                document;
                let labels = container.querySelectorAll('label');
                let row = null;
                for (let label of labels) {
                    let text = (label.innerText || '').replace(/[*：:]/g, '').trim();
                    if (text === label_text) {
                        row = label.closest('.layui-inline') || label.closest('.layui-form-item');
                        break;
                    }
                }
                if (!row) {
                    for (let label of labels) {
                        let text = (label.innerText || '').replace(/[*：:]/g, '').trim();
                        if (text.includes(label_text) || label_text.includes(text)) {
                            row = label.closest('.layui-inline') || label.closest('.layui-form-item');
                            break;
                        }
                    }
                }
                if (!row) return {ok: false, error: 'row not found'};
                let trigger = row.querySelector('.layui-form-select input') ||
                              row.querySelector('input[readonly]') ||
                              row.querySelector('input');
                if (!trigger) return {ok: false, error: 'trigger not found'};
                return {ok: true};
            }
        """, {"label_text": label_text})
        if not row_result.get('ok'):
            last_error = row_result.get('error', 'row not found')
            page.wait_for_timeout(400)
            continue

        # 2) 找匹配 target 的 dd：先 row 内，再全 frame 兜底（dd 可能渲染在 body 下）
        #    模糊匹配兼容 '2026'/'2026年' 等格式差异；防空/短文本误配
        found = frame.evaluate("""
            ({label_text, target}) => {
                let container = document.getElementById('groupProjForm') ||
                                document.querySelector('.layui-layer-wrap') ||
                                document;
                let labels = container.querySelectorAll('label');
                let row = null;
                for (let label of labels) {
                    let text = (label.innerText || '').replace(/[*：:]/g, '').trim();
                    if (text === label_text) { row = label.closest('.layui-inline') || label.closest('.layui-form-item'); break; }
                }
                if (!row) return {ok: false, error: 'row not found in step2'};
                let match = (dd) => {
                    let t = (dd.innerText || '').trim();
                    if (!t) return false;
                    if (t === target) return true;
                    if (t.includes(target)) return true;                      // 兼容 '2026年'
                    if (target.includes(t) && t.length >= 2) return true;     // 防 '0' 之类误配 '0.2'
                    return false;
                };
                let isOpen = (dd) => {
                    let dl = dd.closest('dl');
                    return dl && getComputedStyle(dl).display !== 'none';
                };
                // a) 优先 row 内 .layui-form-select
                let scope = row.querySelector('.layui-form-select') || row.querySelector('.layui-unselect') || row;
                let dds = scope.querySelectorAll('dd');
                for (let dd of dds) {
                    if (match(dd)) return {ok: true, source: 'row', open: isOpen(dd)};
                }
                // b) 全 frame 兜底
                let allDds = document.querySelectorAll('.layui-form-select dl dd');
                for (let dd of allDds) {
                    if (match(dd)) return {ok: true, source: 'body', open: isOpen(dd)};
                }
                return {ok: false, row_dds: dds.length, body_dds: allDds.length};
            }
        """, {"label_text": label_text, "target": target})

        if not found.get('ok'):
            last_error = f"选项未加载 (row_dds={found.get('row_dds')}, body_dds={found.get('body_dds')})"
            page.wait_for_timeout(500)   # 等选项 AJAX 懒加载
            continue

        # 3) 下拉未展开则点 trigger 打开（完整鼠标事件序列，layui 需要）
        if not found.get('open'):
            opened = frame.evaluate("""
                ({label_text}) => {
                    let container = document.getElementById('groupProjForm') ||
                                    document.querySelector('.layui-layer-wrap') ||
                                    document;
                    let labels = container.querySelectorAll('label');
                    let row = null;
                    for (let label of labels) {
                        let text = (label.innerText || '').replace(/[*：:]/g, '').trim();
                        if (text === label_text) { row = label.closest('.layui-inline') || label.closest('.layui-form-item'); break; }
                    }
                    if (!row) return {ok: false};
                    let trigger = row.querySelector('.layui-form-select input') ||
                                  row.querySelector('input[readonly]') ||
                                  row.querySelector('input');
                    if (!trigger) return {ok: false};
                    ['mousedown','mouseup','click'].forEach(t => {
                        trigger.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
                    });
                    return {ok: true};
                }
            """, {"label_text": label_text})
            if not opened.get('ok'):
                last_error = "打开下拉失败"
                page.wait_for_timeout(400)
                continue
            page.wait_for_timeout(300)

        # 4) 点匹配的 dd（优先 row 内，兜底全 frame）
        clicked = frame.evaluate("""
            ({label_text, target}) => {
                let container = document.getElementById('groupProjForm') ||
                                document.querySelector('.layui-layer-wrap') ||
                                document;
                let labels = container.querySelectorAll('label');
                let row = null;
                for (let label of labels) {
                    let text = (label.innerText || '').replace(/[*：:]/g, '').trim();
                    if (text === label_text) { row = label.closest('.layui-inline') || label.closest('.layui-form-item'); break; }
                }
                if (!row) return {ok: false, error: 'row not found in step4'};
                let match = (dd) => {
                    let t = (dd.innerText || '').trim();
                    if (!t) return false;
                    if (t === target || t.includes(target)) return true;
                    if (target.includes(t) && t.length >= 2) return true;
                    return false;
                };
                let fire = (dd) => {
                    ['mousedown','mouseup','click'].forEach(t => {
                        dd.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
                    });
                    return dd.innerText.trim();
                };
                let scope = row.querySelector('.layui-form-select') || row.querySelector('.layui-unselect') || row;
                let dds = scope.querySelectorAll('dd');
                for (let dd of dds) {
                    if (match(dd)) return {ok: true, text: fire(dd)};
                }
                let allDds = document.querySelectorAll('.layui-form-select dl dd');
                for (let dd of allDds) {
                    if (match(dd)) return {ok: true, text: fire(dd)};
                }
                return {ok: false, error: 'dd not found when clicking'};
            }
        """, {"label_text": label_text, "target": target})

        if not (clicked and clicked.get('ok')):
            last_error = "点击 dd 时选项消失"
            page.wait_for_timeout(400)
            continue

        # 5) 回读验证 display input 值已更新；未生效则重开一轮重选
        cur = _get_field_value(scope, page, label_text)
        if cur and (cur == target or target in cur):
            page.wait_for_timeout(150)
            _log_field(label_text, "SELECT", value, f"matched='{clicked.get('text','')}'", True)
            return True
        last_error = f"选择后值未生效 (cur='{cur}')"
        page.wait_for_timeout(400)

    _log_field(label_text, "SELECT", value, f"{last_error}（重试12秒后仍失败）", False)
    page.keyboard.press("Escape")
    return False


# ============================================================================
# [SEARCH] 可搜索 select — 选项渲染在 frame body 下
# ============================================================================

def _dismiss_residual_shades(frame, page, caller=""):
    """
    检查并清除残留的 layui-layer-shade 遮罩。
    这些遮罩可能来自之前的下拉/弹窗未正确关闭。
    返回被清除的遮罩数量。
    """
    frame2 = _get_frame(page)
    diag = frame2.evaluate("""
        () => {
            let shades = document.querySelectorAll('.layui-layer-shade');
            let layers = document.querySelectorAll('.layui-layer:not(.layui-layer-shade)');
            let report = {
                shade_count: shades.length,
                layer_count: layers.length,
                shades: [],
                layers: [],
                element_at_center: null,
            };
            shades.forEach((s, i) => {
                report.shades.push({
                    index: i,
                    id: s.id || '',
                    display: getComputedStyle(s).display,
                    zIndex: s.style.zIndex || '',
                });
            });
            layers.forEach((l, i) => {
                let title = l.querySelector('.layui-layer-title');
                report.layers.push({
                    index: i,
                    id: l.id || '',
                    title: title ? title.innerText.trim() : '',
                    display: getComputedStyle(l).display,
                    zIndex: l.style.zIndex || '',
                });
            });
            // elementFromPoint at center of viewport
            let cx = window.innerWidth / 2;
            let cy = window.innerHeight / 2;
            let el = document.elementFromPoint(cx, cy);
            report.element_at_center = el ? (el.tagName + (el.className ? '.' + el.className.split(' ')[0] : '') + (el.id ? '#' + el.id : '')) : 'none';
            return JSON.stringify(report);
        }
    """)

    if caller:
        logger.info(f"  [{caller}] layui层诊断: {diag}")

    # 尝试清除残留 shade
    cleared = frame2.evaluate("""
        () => {
            let count = 0;
            // 只清除 shade（遮罩），保留正常的 layui 弹窗
            document.querySelectorAll('.layui-layer-shade').forEach(s => {
                // 检查是否关联了可见的 layui-layer（如果有可见弹窗，保留 shade）
                let zIdx = s.style.zIndex || '';
                let hasVisibleLayer = false;
                document.querySelectorAll('.layui-layer:not(.layui-layer-shade)').forEach(l => {
                    if (getComputedStyle(l).display !== 'none' &&
                        (l.style.zIndex || '') === zIdx) {
                        hasVisibleLayer = true;
                    }
                });
                if (!hasVisibleLayer) {
                    s.remove();
                    count++;
                }
            });
            return count;
        }
    """)
    if cleared > 0:
        logger.info(f"  [{caller}] 已清除 {cleared} 个残留 shade")
    return cleared


def _select_searchable_by_label(scope, page, label_text, keyword):
    """可搜索 select — 全 JS 实现，限定弹窗内搜索"""
    frame = _get_frame(page)

    result = frame.evaluate("""
        ({label_text, keyword}) => {
            let container = document.getElementById('groupProjForm') ||
                            document.querySelector('.layui-layer-wrap') ||
                            document;
            let labels = container.querySelectorAll('label');
            let row = null;
            for (let label of labels) {
                let text = (label.innerText || '').replace(/[*：:]/g, '').trim();
                if (text === label_text) {
                    row = label.closest('.layui-inline') || label.closest('.layui-form-item');
                    break;
                }
            }
            if (!row) return {ok: false, error: 'row not found'};
            let trigger = row.querySelector('input[placeholder="全部"]') || row.querySelector('input');
            if (!trigger) return {ok: false, error: 'trigger not found'};
            // 点击聚焦 → 清空 → 输入关键词
            ['mousedown','mouseup','click'].forEach(t => {
                trigger.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
            });
            trigger.value = '';
            trigger.dispatchEvent(new Event('input', {bubbles: true}));
            trigger.value = keyword;
            trigger.dispatchEvent(new Event('input', {bubbles: true}));
            trigger.dispatchEvent(new Event('change', {bubbles: true}));
            return {ok: true};
        }
    """, {"label_text": label_text, "keyword": keyword})

    if not result.get('ok'):
        _log_field(label_text, "SEARCH", keyword, result.get('error', 'unknown'), False)
        return False

    page.wait_for_timeout(500)

    # Step 2: 找 dd 选项并点击 — 网页刷新/慢加载时选项可能延迟出现，轮询重试（最多 ~6s）
    click_result = None
    deadline = time.time() + 6
    while time.time() < deadline:
        click_result = frame.evaluate("""
            ({label_text, kw}) => {
                let container = document.getElementById('groupProjForm') ||
                                document.querySelector('.layui-layer-wrap') ||
                                document;
                let labels = container.querySelectorAll('label');
                let row = null;
                for (let label of labels) {
                    let text = (label.innerText || '').replace(/[*：:]/g, '').trim();
                    if (text === label_text) { row = label.closest('.layui-inline') || label.closest('.layui-form-item'); break; }
                }
                if (!row) return {ok: false, error: 'row not found in step2'};
                // 限定在此 row 内的 .layui-form-select 找 dd
                let scope = row.querySelector('.layui-form-select') || row;
                let dds = scope.querySelectorAll('dd');
                for (let dd of dds) {
                    let txt = (dd.innerText || '').trim();
                    if (txt.includes(kw) || txt === kw) {
                        ['mousedown','mouseup','click'].forEach(t => {
                            dd.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
                        });
                        return {ok: true, text: txt};
                    }
                }
                return {ok: false, dds_found: dds.length};
            }
        """, {"label_text": label_text, "kw": keyword})
        if click_result and click_result.get('ok'):
            break
        page.wait_for_timeout(500)

    if not (click_result and click_result.get('ok')):
        _log_field(label_text, "SEARCH", keyword, f"no match（重试{6}秒后仍失败）", False)
        page.keyboard.press("Escape")
        return False

    _log_field(label_text, "SEARCH", keyword, f"matched='{click_result.get('text','')}'", True)
    _dismiss_residual_shades(frame, page, f"SEARCH:{label_text}")
    return True


# ============================================================================
# [TREE] treeSelect — 树节点渲染在 frame body 下
# ============================================================================

def _tree_select_by_label(scope, page, label_text, target, level1_label=None):
    """
    学分分类 — zTree 二级级联选择。

    流程：
      1. 点击 trigger 打开下拉
      2. JS click 展开 L1 节点
      3. Playwright mouse.click 坐标点击 L2 节点（trusted event → 触发 beforeClick）
      4. 验证 hidden (scoreLevel) 是否被正确设置，否则返回 False
    """
    frame = _get_frame(page)

    # 找 trigger — 优先在学分分类这一行里找，避免命中页面其他 treeSelect
    trigger = None
    row = _find_form_row(scope, label_text)
    if row:
        t = row.locator("input[id^='treeSelect-input']").first
        if t.count() > 0:
            trigger = t
            logger.info(f"  [TREE] trigger 在 '{label_text}' 行内找到")
    if trigger is None:
        for ctx_name, ctx in [("scope", scope), ("frame", frame), ("page", page)]:
            t = ctx.locator("input[id^='treeSelect-input']").first
            if t.count() > 0:
                trigger = t
                logger.info(f"  [TREE] trigger 在 {ctx_name} 中找到")
                break
    if trigger is None:
        _log_field(label_text, "TREE", target, "trigger not found", False)
        return False

    # ---- Step 1: 检查并清除残留遮罩，然后点击 trigger ----
    _dismiss_residual_shades(frame, page, "TREE:before_click")

    # 二次确认：elementFromPoint — 用 trigger 的实际 DOM id 精确定位
    trigger_id = trigger.get_attribute("id")
    hit_check = frame.evaluate("""
        (inputId) => {
            let inp = document.getElementById(inputId);
            if (!inp) return 'no_input';
            let rect = inp.getBoundingClientRect();
            let cx = rect.left + rect.width / 2;
            let cy = rect.top + rect.height / 2;
            let hit = document.elementFromPoint(cx, cy);
            if (!hit) return 'no_hit';
            // 往上找，看是否命中 input 或 treeSelect 容器
            let cur = hit;
            while (cur && cur !== document.body) {
                if (cur === inp) return 'input';
                if (cur.classList && cur.classList.contains('layui-treeSelect')) return 'treeSelect_container';
                if (cur.id && cur.id.startsWith('treeSelect-input')) return 'input_variant';
                cur = cur.parentElement;
            }
            return 'blocked_by:' + (hit.tagName || '?') + (hit.className ? '.' + hit.className.split(' ')[0] : '') + (hit.id ? '#' + hit.id : '');
        }
    """, trigger_id)
    logger.info(f"  [TREE] elementFromPoint 结果: {hit_check}")

    if 'blocked_by' in str(hit_check):
        logger.warning(f"  [TREE] ⚠ trigger 被遮挡: {hit_check}，强制清除所有 shade")
        frame.evaluate("""
            () => {
                document.querySelectorAll('.layui-layer-shade').forEach(s => s.remove());
            }
        """)
        page.wait_for_timeout(300)

    trigger.scroll_into_view_if_needed()
    trigger.click()
    page.wait_for_timeout(600)

    # ---- Step 2: JS 点击 L1 switch 展开子节点 ----
    _l1 = level1_label or target  # 如果没指定 L1，用 target 作为 fallback
    expand_result = frame.evaluate("""
        ({l1, inputId}) => {
            // 找可见的 treeSelect body（body 被 zTree 追加到 document.body，不在 .layui-treeSelect 内）
            let bodies = document.querySelectorAll('.layui-treeSelect-body');
            let body = null;
            for (let b of bodies) {
                if (b.offsetParent !== null) { body = b; break; }  // 可见的
            }
            if (!body) body = document;
            let scope = body || document;

            // 纯 JS 遍历匹配（避开 CSS 属性选择器中文编码问题）
            let allA = scope.querySelectorAll('a[treenode_a]');
            let l1a = null;
            for (let a of allA) {
                let t = (a.getAttribute('title') || '').trim();
                if (t === l1) { l1a = a; break; }
            }
            if (!l1a) {
                for (let a of allA) {
                    let t = (a.getAttribute('title') || '').trim();
                    if (t.includes(l1) || l1.includes(t)) { l1a = a; break; }
                }
            }
            if (!l1a) {
                let titles = Array.from(allA).map(a => ({
                    t: (a.getAttribute('title') || '').trim(),
                    len: (a.getAttribute('title') || '').trim().length
                }));
                return 'no_l1a; l1=' + JSON.stringify(l1) + ' l1_len=' + l1.length + '; available: ' + JSON.stringify(titles.slice(0, 10));
            }

            let li = l1a.closest('li');
            if (!li) return 'no_li';
            let sw = li.querySelector('span[treenode_switch]');
            if (!sw) return 'no_switch';

            // 判断是否已展开：switch className 含 _open 则已展开，含 _close 则收起
            let isOpen = sw.className.includes('_open');
            let isClosed = sw.className.includes('_close');
            if (isOpen) {
                return 'already_open:' + (l1a.getAttribute('title') || '?');
            }
            sw.click();
            return 'expanded:' + (l1a.getAttribute('title') || '?');
        }
    """, {"l1": _l1, "inputId": trigger_id})
    logger.info(f"  [TREE] expand L1('{_l1}'): {expand_result}")
    if not (expand_result.startswith('expanded') or expand_result.startswith('already_open')):
        _log_field(label_text, "TREE", target, f"expand failed: {expand_result}", False)
        return False

    page.wait_for_timeout(500)

    # ---- Step 3: JS native click L2 节点（限定到可见 treeSelect body） ----
    frame.evaluate("""
        ({target, inputId}) => {
            // 找可见的 treeSelect body（zTree 追加到 document.body）
            let bodies = document.querySelectorAll('.layui-treeSelect-body');
            let body = null;
            for (let b of bodies) {
                if (b.offsetParent !== null) { body = b; break; }
            }
            if (!body) body = document;
            let nodes = body.querySelectorAll('.node_name');
            let match = Array.from(nodes).find(x => x.innerText.trim() === target);
            if (!match) {
                // diagnostic: list available L2 nodes
                let available = Array.from(nodes).map(x => x.innerText.trim()).filter(Boolean);
                console.log('[TREE:L2] target=' + JSON.stringify(target) + ' available=' + JSON.stringify(available.slice(0, 20)));
                return;
            }
            let a = match.closest('a');
            if (!a) return;
            // 完整鼠标事件序列 + native click（与 Console 行为一致）
            a.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
            a.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
            a.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
            a.click();
        }
    """, {"target": target, "inputId": trigger_id})
    page.wait_for_timeout(400)

    # ---- Step 4: 验证 — display 和 hidden 是否被正确设置 ----
    verify = json.loads(frame.evaluate("""
        (inputId) => {
            let r = {};
            let input = document.getElementById(inputId);
            r.display_value = input ? (input.value || '(empty)') : 'no_input';
            r.display_ok = !!(input && input.value && input.value.trim() !== '');

            let sl = document.querySelector("[name='scoreLevel']");
            r.scoreLevel_value = sl ? (sl.value || '(empty)') : 'not_found';
            r.scoreLevel_ok = !!(sl && sl.value && sl.value.trim() !== '');

            let gp = document.querySelector("[name='groupProjId']");
            r.groupProjId_value = gp ? (gp.value || '(empty)') : 'not_found';

            // 检查 layui form 内部状态
            try {
                if (window.layui && layui.form) {
                    let formVal = layui.form.val();
                    r.layui_scoreLevel = (formVal && formVal.scoreLevel) ? formVal.scoreLevel : '(empty)';
                }
            } catch(e) { r.layui_err = e.message; }

            return JSON.stringify(r);
        }
    """, trigger_id))
    logger.info(f"  [TREE] verify: display='{verify.get('display_value')}' "
                f"scoreLevel='{verify.get('scoreLevel_value')}' "
                f"layui_scoreLevel='{verify.get('layui_scoreLevel', '?')}'")

    # 核心判断：display 必须有值
    if not verify.get('display_ok'):
        logger.error(f"  [TREE] ❌ display input 未更新! 点击无效")
        _log_field(label_text, "TREE", target, "display empty after click", False)
        return False

    # 如果 scoreLevel 为空，尝试触发 treeSelect onChange 更新 hidden
    if not verify.get('scoreLevel_ok'):
        logger.warning(f"  [TREE] ⚠ scoreLevel 仍为空，尝试触发 treeSelect onChange...")
        # 方法：对 display input 触发 change → treeSelect 可能更新 hidden
        frame.evaluate("""
            (inputId) => {
                let input = document.getElementById(inputId);
                if (input && input.value) {
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    if (window.jQuery) window.jQuery(input).trigger('change');
                }
                // 也尝试 layui form render
                try {
                    if (window.layui && layui.form) {
                        layui.form.render();
                    }
                } catch(e) {}
            }
        """, trigger_id)
        page.wait_for_timeout(500)

        # 重新检查
        verify2 = json.loads(frame.evaluate("""
            () => {
                let sl = document.querySelector("[name='scoreLevel']");
                let gp = document.querySelector("[name='groupProjId']");
                return JSON.stringify({
                    scoreLevel: sl ? (sl.value || '(empty)') : 'not_found',
                    groupProjId: gp ? (gp.value || '(empty)') : 'not_found',
                });
            }
        """))
        logger.info(f"  [TREE] 二次检查: scoreLevel='{verify2.get('scoreLevel')}' "
                    f"groupProjId='{verify2.get('groupProjId')}'")

        if not verify2.get('scoreLevel') or verify2.get('scoreLevel') == '(empty)':
            logger.error(f"  [TREE] ❌ scoreLevel 仍然为空! treeSelect 回调未触发")
            _log_field(label_text, "TREE", target, "scoreLevel still empty", False)
            return False

    _log_field(label_text, "TREE", target,
               f"display='{verify.get('display_value')}' scoreLevel='{verify.get('scoreLevel_value')}'", True)
    return True


# ============================================================================
# 按钮 — page 级别
# ============================================================================

def _wait_for_element(page, selector, timeout=12000, label=""):
    """轮询等待 selector 元素出现在任一 frame（页面切换/自动刷新的场景）。

    网站"下一步完善举办周期"后会出现新版中间页，约 3 秒后自动刷新到旧版
    "添加举办周期"页。固定 sleep 会撞上中间页导致 addCycle 检测/点击失败，
    改条件等待：元素出现即返回，最多等 timeout ms。

    Returns:
        bool: 是否在超时前出现
    """
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        for scope in [page] + list(page.frames):
            try:
                if scope.locator(selector).count() > 0:
                    if label:
                        logger.info(f"  [wait] '{label}' 已出现")
                    return True
            except Exception:
                continue
        time.sleep(0.3)
    return False


def _click_btn_lay_filter(page, lay_filter, label):
    """点击 lay-filter 按钮 — 不使用 force，正常 scroll + click"""
    for scope_name, scope in [("page", page)] + [(f"frame[{i}]", f) for i, f in enumerate(page.frames)]:
        btn = scope.locator(f"button[lay-filter='{lay_filter}']").first
        if btn.count() > 0:
            try:
                btn.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                btn.click()
                logger.info(f"  已点击: {label} ({scope_name})")
                return True
            except Exception as e:
                logger.warning(f"  click失败 [{label}] ({scope_name}): {e}")
                continue
    logger.warning(f"  未找到 button[lay-filter='{lay_filter}']")
    return False


def _click_btn_lay_event(page, lay_event, label):
    """点击 lay-event 按钮 — 不使用 force，正常 scroll + click"""
    for scope_name, scope in [("page", page)] + [(f"frame[{i}]", f) for i, f in enumerate(page.frames)]:
        btn = scope.locator(f"a[lay-event='{lay_event}']").first
        if btn.count() > 0:
            try:
                btn.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                btn.click()
                logger.info(f"  已点击: {label} ({scope_name})")
                return True
            except Exception as e:
                logger.warning(f"  click失败 [{label}] ({scope_name}): {e}")
                continue
    logger.warning(f"  未找到 a[lay-event='{lay_event}']")
    return False


def _click_dialog_confirm(page):
    """点击弹窗确认/保存按钮 — 不使用 force"""
    for sel in [
        ".layui-layer-btn0",
        "button[lay-filter='courseFormSubmit']",
        "button[lay-filter='cycleFormSubmit']",
        "button[lay-submit]",
    ]:
        for scope in [page] + list(page.frames):
            try:
                btn = scope.locator(sel).first
                if btn.count() > 0:
                    btn.scroll_into_view_if_needed()
                    page.wait_for_timeout(200)
                    btn.click(timeout=3000)
                    logger.info(f"  弹窗确认: {sel}")
                    return
            except Exception:
                continue
    # 回退
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass


# ============================================================================
# 校验补填 — 点击按钮前检查字段是否已填写/选择，因刷新丢失则补上
# ============================================================================

def _get_field_value(scope, page, label_text):
    """读取指定 label 行的输入框当前值（用于点击前校验补填）。

    Returns:
        str | None: 当前值（去空格）；字段/输入框找不到返回 None
    """
    frame = _get_frame(page)
    try:
        result = frame.evaluate("""
            ({label_text}) => {
                let container = document.getElementById('groupProjForm') ||
                                document.querySelector('.layui-layer-wrap') || document;
                let labels = container.querySelectorAll('label');
                for (let label of labels) {
                    let text = (label.innerText || '').replace(/[*：:]/g, '').trim();
                    if (text === label_text) {
                        let row = label.closest('.layui-inline') || label.closest('.layui-form-item');
                        if (!row) return {ok: false};
                        let inp = row.querySelector('.layui-form-select input') ||
                                  row.querySelector('input[id^="treeSelect-input"]') ||
                                  row.querySelector('input:not([type="hidden"])') ||
                                  row.querySelector('input');
                        if (!inp) return {ok: false};
                        return {ok: true, value: (inp.value || '').trim()};
                    }
                }
                return {ok: false};
            }
        """, {"label_text": label_text})
        if result.get('ok'):
            return result.get('value')
    except Exception:
        pass
    return None


def _verify_activity_form(dialog, page, config):
    """点击"下一步"前校验活动表单各字段，因网页刷新丢失/未填的补上。

    网站改版后存在新版中间页自动刷新，可能重置已填字段。在点按钮前
    逐字段读取当前值，空或不匹配期望值的重新填写，避免创建失败。

    Returns:
        int: 补填的字段数
    """
    import re
    name = config.get("activity_name", "")
    clean_name = re.sub(r'[（(][^）)]*[）)]\s*$', '', name).strip()
    year = str(config.get("year", 2026))
    activity_content = config.get("activity_content", "专业课")
    duration = str(config.get("duration_hours", 1))
    medical_system = config.get("medical_system", "西医")
    assessment_method = config.get("assessment_method", "其他")
    credits = str(config.get("credits", 0.2))
    planned = str(config.get("planned_students", 6))
    discipline = config.get("discipline", "临床外科学")
    sub_discipline = config.get("sub_discipline", "整形外科学")
    cat_l1 = config.get("credit_category", {}).get(
        "level1", "有组织的继续医学教育实践活动")
    cat_l2 = config.get("credit_category", {}).get(
        "level2", "单位组织的继续医学教育实践活动")
    activity_form = config.get("activity_form", "研讨会")

    checks = [
        ("活动名称", clean_name, lambda: _fill_text_by_label(dialog, page, "活动名称", clean_name)),
        ("举办年度", year, lambda: _select_by_label(dialog, page, "举办年度", year)),
        ("活动内容", activity_content, lambda: _select_by_label(dialog, page, "活动内容", activity_content)),
        ("学时", duration, lambda: _fill_text_by_label(dialog, page, "学时", duration)),
        ("所属医学体系", medical_system, lambda: _select_by_label(dialog, page, "所属医学体系", medical_system)),
        ("考核方式", assessment_method, lambda: _select_by_label(dialog, page, "考核方式", assessment_method)),
        ("学分", credits, lambda: _select_by_label(dialog, page, "学分", credits)),
        ("拟授分人数", planned, lambda: _fill_text_by_label(dialog, page, "拟授分人数", planned)),
        ("二级学科", discipline, lambda: _select_searchable_by_label(dialog, page, "二级学科", discipline)),
        ("三级学科", sub_discipline, lambda: _select_searchable_by_label(dialog, page, "三级学科", sub_discipline)),
        ("学分分类", cat_l2, lambda: _tree_select_by_label(dialog, page, "学分分类", cat_l2, level1_label=cat_l1)),
        ("活动形式", activity_form, lambda: _select_by_label(dialog, page, "活动形式", activity_form)),
    ]

    refilled = 0
    for label_text, expected, fill in checks:
        val = _get_field_value(dialog, page, label_text)
        if val is None or val == "":
            logger.info(f"  [校验补填] {label_text} 为空，重新填写")
            fill()
            refilled += 1
        elif val != expected:
            logger.info(f"  [校验补填] {label_text}: 当前'{val}' ≠ 期望'{expected}'，重新填写")
            fill()
            refilled += 1

    # 举办开始日期 [DATE] 单独校验
    try:
        start_date_val = config.get("start_date", "")
        if start_date_val:
            sds = dialog.locator("input[name='startDateSearch']").first
            if sds.count() == 0:
                sds = _get_frame(page).locator("input[name='startDateSearch']").first
            if sds.count() > 0 and not sds.input_value():
                _fill_date_input(sds, "举办开始日期", start_date_val)
                logger.info("  [校验补填] 举办开始日期 为空，补填")
                refilled += 1
    except Exception:
        pass

    if refilled:
        logger.info(f"  [校验] 活动表单补填 {refilled} 个字段")
    else:
        logger.info("  [校验] 活动表单字段全部就绪")
    return refilled


# ============================================================================
# 公开 API
# ============================================================================

def _fill_activity_form(page_or_frame, config):
    frame = _get_frame(page_or_frame)
    page = frame.page
    name = config.get("activity_name", "")
    logger.info(f"填写活动表单: {name[:40]}")

    # 找弹窗
    dialog = _get_dialog(frame, page)
    if not dialog:
        logger.error("找不到新增活动弹窗！")
        _dump(page, "no_dialog")
        return
    logger.info(f"  表单 scope: found")

    import re
    # 1. 活动名称 [TEXT] — 去括号
    clean_name = re.sub(r'[（(][^）)]*[）)]\s*$', '', name).strip()
    if clean_name != name:
        logger.info(f"  活动名称去括号: '{name}' → '{clean_name}'")
    _fill_text_by_label(dialog, page, "活动名称", clean_name)

    # 2. 举办年度 [SELECT]
    _select_by_label(dialog, page, "举办年度", str(config.get("year", 2026)))

    # 3. 活动内容 [SELECT]
    _select_by_label(dialog, page, "活动内容", config.get("activity_content", "专业课"))

    # 4-8. 基础字段
    _fill_text_by_label(dialog, page, "学时", str(config.get("duration_hours", 1)))
    _select_by_label(dialog, page, "所属医学体系", config.get("medical_system", "西医"))
    _select_by_label(dialog, page, "考核方式", config.get("assessment_method", "其他"))
    _select_by_label(dialog, page, "学分", str(config.get("credits", 0.2)))
    _fill_text_by_label(dialog, page, "拟授分人数", str(config.get("planned_students", 6)))

    # 9-10. 学科 [SEARCH]
    _select_searchable_by_label(dialog, page, "二级学科", config.get("discipline", "临床外科学"))
    page.wait_for_timeout(1000)  # 等三级学科 AJAX 加载
    _select_searchable_by_label(dialog, page, "三级学科", config.get("sub_discipline", "整形外科学"))

    # 11. 举办开始日期 [DATE] — 在 treeSelect 之前填，避免干扰
    start_date_val = config.get("start_date", "")
    if start_date_val:
        sds = dialog.locator("input[name='startDateSearch']").first
        if sds.count() == 0:
            sds = frame.locator("input[name='startDateSearch']").first
        if sds.count() > 0:
            cur = sds.input_value()
            if not cur:
                _fill_date_input(sds, "举办开始日期", start_date_val)
                logger.info(f"  已填 举办开始日期: {start_date_val}")

    # 12. 学分分类 [TREE] — 会级联重置活动形式，所以放在活动形式之前
    credit_cat = config.get("credit_category", {})
    cat_l1 = credit_cat.get("level1", "有组织的继续医学教育实践活动")
    cat_l2 = credit_cat.get("level2", "单位组织的继续医学教育实践活动")
    _tree_select_by_label(dialog, page, "学分分类", cat_l2, level1_label=cat_l1)

    # 13. 活动形式 [SELECT] — 学分分类级联重置后重新填写
    _select_by_label(dialog, page, "活动形式", config.get("activity_form", "研讨会"))

    # 13.5 校验补填：点"下一步"前逐字段确认，网页刷新丢失的重新填写
    _verify_activity_form(dialog, page, config)

    # 13.6 年度必填：仍为空则明确报错，不再带空字段静默点"下一步"
    #     （由批量循环捕获后走单门重试）
    year_val = _get_field_value(dialog, page, "举办年度")
    if not year_val:
        raise RuntimeError("举办年度 选择失败（选项未加载/无法选择）")

    # 14. 点击下一步
    logger.info("  点击下一步完善举办周期...")
    next_btn = frame.locator("button[lay-filter='nextStep']").first
    if next_btn.count() > 0:
        next_btn.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        next_btn.click()
        logger.info("  已点击: 下一步完善举办周期")
    else:
        logger.warning("  未找到 nextStep 按钮")

    # 等待进入举办周期页面。网站新版中间页约 3 秒后自动刷新到旧版，
    # 固定 sleep(3s) 会撞上中间页导致 addCycle 检测失败 → 改条件等待（最多 15s）
    if _wait_for_element(page, "a[lay-event='addCycle']", timeout=15000, label="addCycle"):
        logger.info("  ✅ 已进入举办周期页面 (addCycle可见)")
    else:
        logger.warning("  ⚠ nextStep后15秒内未检测到addCycle，可能表单校验失败")
        try:
            _dump(_get_frame(page), "nextStep_failed")
        except Exception:
            pass


def _verify_cycle_modal(cycle_frame, config):
    """校验 CycleModal 日期时间字段，因刷新丢失则补填"""
    start_date = config.get("start_date", "")
    end_date = config.get("end_date", start_date)
    start_time = config.get("period_start_time", "09:00")
    end_time = config.get("period_end_time", "23:00")

    refilled = 0
    try:
        if cycle_frame.locator("input[name='startDay']").count() > 0:
            # 分离式 startDay/startTime/endDay/endTime
            for name, val, ftype, label in [
                ('startDay', start_date, 'date', '开始日期'),
                ('startTime', f"{start_time}:00", 'time', '开始时间'),
                ('endDay', end_date, 'date', '结束日期'),
                ('endTime', f"{end_time}:00", 'time', '结束时间'),
            ]:
                inp = cycle_frame.locator(f"input[name='{name}']").first
                if inp.count() > 0 and not inp.input_value():
                    if ftype == 'date':
                        _fill_laydate_date(inp, cycle_frame, val, label)
                    else:
                        _fill_laydate_time(inp, cycle_frame, val, label)
                    logger.info(f"  [校验补填] CycleModal {label}({name}) 为空，补填")
                    refilled += 1
        elif cycle_frame.locator("input[name='startDate']").count() > 0:
            # 合并式 startDate/endDate
            for name, val, label in [
                ('startDate', f"{start_date} {start_time}:00", '开始日期'),
                ('endDate', f"{end_date} {end_time}:00", '结束日期'),
            ]:
                inp = cycle_frame.locator(f"input[name='{name}']").first
                if inp.count() > 0 and not inp.input_value():
                    _fill_laydate_datetime(inp, cycle_frame, val, label)
                    logger.info(f"  [校验补填] CycleModal {label}({name}) 为空，补填")
                    refilled += 1
    except Exception as e:
        logger.warning(f"  [校验] CycleModal 校验异常: {e}")

    if refilled:
        logger.info(f"  [校验] 举办周期补填 {refilled} 个字段")
    else:
        logger.info("  [校验] 举办周期字段已就绪")
    return refilled


def _add_period(page_or_frame, config):
    frame = _get_frame(page_or_frame)
    page = frame.page
    logger.info("添加举办周期")

    # 下一步后页面可能刚切换（新版中间页约3秒后刷新到旧版），条件等待 addCycle 出现再点击
    if not _wait_for_element(page, "a[lay-event='addCycle']", timeout=15000, label="addCycle"):
        raise Exception("找不到 [lay-event='addCycle']")

    if not _click_btn_lay_event(page, "addCycle", "添加举办周期"):
        raise Exception("找不到 [lay-event='addCycle']")
    page.wait_for_timeout(2000)

    start_date = config.get("start_date", "")
    end_date = config.get("end_date", start_date)
    # 周期时间：全天范围（如 09:00-23:00），不同于课程时间（19:30-20:45）
    start_time = config.get("period_start_time", "09:00")
    end_time = config.get("period_end_time", "23:00")
    activity_name = config.get("activity_name", "")

    # ====================================================================
    # 找到 CycleModal iframe
    # ====================================================================
    cycle_frame = None
    for f in page.frames:
        if 'CycleModal' in f.url:
            cycle_frame = f
            break

    if cycle_frame is None:
        logger.warning("未找到 CycleModal iframe，回退到旧逻辑")
        dialog = _get_dialog(frame, page) or page
        if start_date:
            _set_laydate(dialog, page, "举办开始日期", f"{start_date} {start_time}:00")
        if end_date:
            _set_laydate(dialog, page, "举办结束日期", f"{end_date} {end_time}:00")
        _click_dialog_confirm(page)
        page.wait_for_timeout(1500)
        logger.info("举办周期已保存")
        return

    logger.info(f"  [CycleModal] frame: {cycle_frame.url[:80]}")
    # 等待 CycleModal 内 layui form 渲染完成 + AJAX 数据加载
    try:
        cycle_frame.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    # 确认 form 已渲染
    try:
        cycle_frame.wait_for_selector("input[name='startDate'], input[name='startDay'], button[lay-submit]", timeout=5000)
        logger.info("  [CycleModal] form 已就绪")
    except Exception:
        logger.warning("  [CycleModal] form 可能未完全加载")

    # ====================================================================
    # 检查活动名称是否带入 CycleModal
    # ====================================================================
    # 可能的活动名称字段名
    activity_name_fields = ['groupProjName', 'projName', 'name', 'activityName', 'actName']
    name_inp_info = cycle_frame.evaluate("""
        (names) => {
            for (let n of names) {
                let el = document.querySelector(`input[name="${n}"]`);
                if (el) return {name: n, value: el.value || '', visible: !!(el.offsetWidth > 0)};
            }
            return null;
        }
    """, activity_name_fields)

    if name_inp_info:
        logger.info(f"  [CycleModal] 活动名称字段: name={name_inp_info['name']} value='{name_inp_info['value']}' visible={name_inp_info['visible']}")
    else:
        logger.info(f"  [CycleModal] 未找到活动名称字段 (搜了: {activity_name_fields})")

    # 如果 CycleModal 中没有活动名称，从父 frame (deptGroupproj) 获取 groupProjName 写入
    if not name_inp_info or not name_inp_info['value']:
        # 尝试从父 frame 获取活动名称
        parent_name = frame.evaluate("""
            () => {
                let el = document.querySelector("input[name='groupProjName']");
                return el ? (el.value || '') : '';
            }
        """)
        logger.info(f"  [CycleModal] 父frame groupProjName = '{parent_name}'")
        if not parent_name and activity_name:
            parent_name = activity_name

        if parent_name:
            # 尝试写入 CycleModal 中可能的活动名称字段
            write_result = cycle_frame.evaluate("""
                (nameVal) => {
                    let candidates = ['groupProjName', 'projName', 'name', 'activityName', 'actName'];
                    let filled = [];
                    for (let n of candidates) {
                        let el = document.querySelector(`input[name="${n}"]`);
                        if (el && !el.value) {
                            el.value = nameVal;
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            filled.push(n);
                        }
                    }
                    return filled;
                }
            """, parent_name)
            logger.info(f"  [CycleModal] 已写入活动名称到: {write_result}")

    # ====================================================================
    # 三、填充日期时间字段
    # ====================================================================
    has_separate = cycle_frame.locator("input[name='startDay']").count() > 0
    has_combined = cycle_frame.locator("input[name='startDate']").count() > 0

    if has_separate:
        logger.info("  [CycleModal] 分离式日期+时间字段 (startDay/startTime/...)")
        fields = []
        if start_date:
            fields.append(('startDay', start_date, 'date', '开始日期'))
            fields.append(('startTime', f"{start_time}:00", 'time', '开始时间'))
        if end_date:
            fields.append(('endDay', end_date, 'date', '结束日期'))
            fields.append(('endTime', f"{end_time}:00", 'time', '结束时间'))

        for name, val, ftype, label in fields:
            inp = cycle_frame.locator(f"input[name='{name}']").first
            if inp.count() == 0:
                logger.warning(f"  未找到 input[name='{name}']")
                continue
            if ftype == 'date':
                _fill_laydate_date(inp, cycle_frame, val, label)
            else:
                _fill_laydate_time(inp, cycle_frame, val, label)

    elif has_combined:
        logger.info("  [CycleModal] 合并式datetime字段 (startDate/endDate)")
        start_val = f"{start_date} {start_time}:00" if start_date else ""
        end_val = f"{end_date} {end_time}:00" if end_date else ""
        start_inp = cycle_frame.locator("input[name='startDate']").first
        end_inp = cycle_frame.locator("input[name='endDate']").first
        if start_inp.count() > 0 and start_val:
            _fill_laydate_datetime(start_inp, cycle_frame, start_val, "开始日期")
        if end_inp.count() > 0 and end_val:
            _fill_laydate_datetime(end_inp, cycle_frame, end_val, "结束日期")
    else:
        logger.warning("  [CycleModal] 未识别日期字段模式")

    # 校验补填：网页刷新可能丢失日期时间，保存前确认
    _verify_cycle_modal(cycle_frame, config)

    _click_cycle_modal_save(page, cycle_frame)
    page.wait_for_timeout(2000)
    logger.info("举办周期已保存")


def _click_cycle_modal_save(page, cycle_frame=None):
    """点击 CycleModal 中的保存按钮"""
    if cycle_frame is None:
        for f in page.frames:
            if 'CycleModal' in f.url:
                cycle_frame = f
                break
    if cycle_frame is None:
        logger.warning("  [CycleModal] 未找到 frame，回退 Enter")
        page.keyboard.press("Enter")
        return

    # 优先 lay-filter 精确匹配 → lay-submit → 含"保存"文字
    for sel in ["button[lay-filter='cycleFormSubmit']", "button[lay-filter='courseFormSubmit']",
                "button[lay-submit]:has-text('保存')", "button:has-text('保存')"]:
        btn = cycle_frame.locator(sel).first
        if btn.count() > 0:
            try:
                btn.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                btn.click()
                logger.info(f"  [CycleModal] ✅ 保存: {sel}")
                return
            except Exception:
                continue

    # JS 兜底
    try:
        cycle_frame.evaluate("""
            () => {
                let btn = document.querySelector("button[lay-filter='cycleFormSubmit']") ||
                          document.querySelector("button[lay-filter='courseFormSubmit']") ||
                          document.querySelector("button[lay-submit]");
                if (btn && btn.offsetWidth > 0) { btn.click(); return; }
                document.querySelectorAll('button').forEach(b => {
                    if (b.offsetWidth > 0 && b.innerText.indexOf('保存') >= 0) b.click();
                });
            }
        """)
        logger.info("  [CycleModal] ✅ 保存 (JS)")
    except Exception:
        logger.error("  [CycleModal] ❌ 保存失败")
        page.keyboard.press("Enter")


def _fill_laydate_date(inp, frame, date_str, label):
    """
    填充 laydate DATE 字段（纯日期选择器，日历面板）。
    点击 input → 点日历格 → 面板自动关闭。
    """
    from datetime import date

    try:
        d = date.fromisoformat(date_str)
        lay_ymd = f"{d.year}-{d.month}-{d.day}"

        # 点击 input 打开日期面板
        inp.click(force=True)
        page = frame.page
        page.wait_for_timeout(800)

        # 确定面板在哪个 frame
        pf = frame
        for candidate in [frame] + list(page.frames):
            try:
                if candidate.evaluate("() => !!document.querySelector('.layui-laydate')"):
                    pf = candidate
                    break
            except Exception:
                pass

        # 点击日期格（跳过 disabled 的；即使已 selected 也要点以确认）
        result = pf.evaluate(
            """(ymd) => {
                let panel = document.querySelector('.layui-laydate');
                if (!panel) return 'no_panel';
                let cell = panel.querySelector('td[lay-ymd="' + ymd + '"]');
                if (!cell) return 'cell_not_found:' + ymd;
                if (cell.classList.contains('laydate-disabled'))
                    return 'disabled:' + ymd;
                // 即使已经 layui-this 也需要点击来触发 done 回调设置 input 值
                cell.click();
                return 'clicked';
            }""",
            lay_ymd
        )
        page.wait_for_timeout(500)
        logger.info(f"  laydate日期 [{label}]: {date_str} → {result}")

    except Exception as e:
        logger.warning(f"  laydate日期失败 ({label}): {e}, JS兜底")
        try:
            inp.evaluate(
                """(el, val) => {
                    el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                date_str
            )
        except Exception:
            pass


def _fill_laydate_time(inp, frame, time_str, label):
    """
    填充 laydate TIME 字段（纯时间选择器，时/分/秒列表）。
    点击 input → 点小时 → 点分钟 → 面板关闭。
    """
    try:
        parts = time_str.split(":")
        hour = str(int(parts[0])).zfill(2)
        minute = str(int(parts[1])).zfill(2) if len(parts) > 1 else "00"

        # 点击 input 打开时间面板
        inp.click(force=True)
        page = frame.page
        page.wait_for_timeout(800)

        # 确定面板 frame
        pf = frame
        for candidate in [frame] + list(page.frames):
            try:
                if candidate.evaluate("() => !!document.querySelector('.layui-laydate')"):
                    pf = candidate
                    break
            except Exception:
                pass

        # 点小时和分钟 — DOM: ul.laydate-time-list > li(时) > ol > li(00..23)
        # ol 是 scrollable 的，需要 scrollIntoView
        pf.evaluate(
            """({hour, minute}) => {
                let panel = document.querySelector('.layui-laydate');
                if (!panel) return;
                let timeUl = panel.querySelector('.laydate-time-list');
                if (!timeUl) return;
                let hourOl = timeUl.querySelector('li:first-child ol');
                if (hourOl) {
                    hourOl.querySelectorAll('li').forEach(function(li) {
                        if (li.innerText.trim() === hour) {
                            li.scrollIntoView({block: 'center'});
                            li.click();
                        }
                    });
                }
                let minOl = timeUl.querySelector('li:nth-child(2) ol');
                if (minOl) {
                    minOl.querySelectorAll('li').forEach(function(li) {
                        if (li.innerText.trim() === minute) {
                            li.scrollIntoView({block: 'center'});
                            li.click();
                        }
                    });
                }
                let btn = panel.querySelector('.laydate-btns-confirm');
                if (btn) btn.click();
            }""",
            {'hour': hour, 'minute': minute}
        )
        page.wait_for_timeout(500)
        logger.info(f"  laydate时间 [{label}]: {time_str}")

    except Exception as e:
        logger.warning(f"  laydate时间失败 ({label}): {e}, JS兜底")
        try:
            inp.evaluate(
                """(el, val) => {
                    el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                time_str
            )
        except Exception:
            pass


def _fill_laydate_datetime(inp, frame, value, label):
    """
    填充 laydate DATETIME 字段（合并的日期+时间，日历+时间列表在同一面板）。
    用于合并式字段如 startDate/endDate。
    value 格式: "2026-07-26 19:30:00"
    """
    from datetime import date

    try:
        parts = value.split(" ")
        date_str = parts[0]
        time_str = parts[1] if len(parts) > 1 else "00:00:00"
        time_parts = time_str.split(":")
        hour = str(int(time_parts[0])).zfill(2)
        minute = str(int(time_parts[1])).zfill(2) if len(time_parts) > 1 else "00"

        d = date.fromisoformat(date_str)
        lay_ymd = f"{d.year}-{d.month}-{d.day}"

        inp.click(force=True)
        page = frame.page
        page.wait_for_timeout(800)

        pf = frame
        for candidate in [frame] + list(page.frames):
            try:
                if candidate.evaluate("() => !!document.querySelector('.layui-laydate')"):
                    pf = candidate
                    break
            except Exception:
                pass

        # 点击日期（即使已选中也需点击确认）
        pf.evaluate(
            """(ymd) => {
                let panel = document.querySelector('.layui-laydate');
                if (!panel) return;
                let cell = panel.querySelector('td[lay-ymd="' + ymd + '"]');
                if (cell && !cell.classList.contains('laydate-disabled')) {
                    cell.click();
                }
            }""",
            lay_ymd
        )
        page.wait_for_timeout(400)

        # 点击时间 — DOM: ul.laydate-time-list > li(时) > ol > li, li(分) > ol > li
        # 小时和分钟列表是 scrollable <ol>，需要 scrollIntoView 再 click
        pf.evaluate(
            """({hour, minute}) => {
                let panel = document.querySelector('.layui-laydate');
                if (!panel) return;
                let timeUl = panel.querySelector('.laydate-time-list');
                if (!timeUl) return;
                let hourOl = timeUl.querySelector('li:first-child ol');
                if (hourOl) {
                    hourOl.querySelectorAll('li').forEach(function(li) {
                        if (li.innerText.trim() === hour) {
                            li.scrollIntoView({block: 'center'});
                            li.click();
                        }
                    });
                }
                let minOl = timeUl.querySelector('li:nth-child(2) ol');
                if (minOl) {
                    minOl.querySelectorAll('li').forEach(function(li) {
                        if (li.innerText.trim() === minute) {
                            li.scrollIntoView({block: 'center'});
                            li.click();
                        }
                    });
                }
                let btn = panel.querySelector('.laydate-btns-confirm');
                if (btn) btn.click();
            }""",
            {'hour': hour, 'minute': minute}
        )
        page.wait_for_timeout(500)

        actual = inp.input_value()
        logger.info(f"  已填(laydate) [{label}]: {value} → actual='{actual}'")

    except Exception as e:
        logger.warning(f"  laydate失败 ({label}): {e}, JS兜底")
        try:
            inp.evaluate(
                """(el, val) => {
                    el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                value
            )
        except Exception:
            pass


def _set_laydate(scope, page, label_text, value):
    """填充 laydate 日期字段。先按 label 找，失败则按 name/data-field 回退。"""
    # 尝试多个 label 变体
    label_variants = [label_text]
    if '举办' in label_text:
        label_variants.append(label_text.replace('举办', ''))

    row = None
    for variant in label_variants:
        row = _find_form_row(scope, variant)
        if row:
            break

    if not row:
        # 回退: 按 name 或 data-field 属性找 input
        name_map = {
            '开始日期': 'startDate',
            '结束日期': 'endDate',
            '举办开始日期': 'startDate',
            '举办结束日期': 'endDate',
            '课程开始时间': 'startTime',
            '课程结束时间': 'endTime',
        }
        inp = None
        for key, name_val in name_map.items():
            if key in label_text:
                inp = scope.locator(f"input[name='{name_val}']").first
                if inp.count() > 0:
                    break
                # 也搜 data-field
                inp = scope.locator(f"input[data-field='{name_val}']").first
                if inp.count() > 0:
                    break

        if inp and inp.count() > 0:
            _fill_date_input(inp, label_text, value)
            return

        logger.warning(f"  未找到日期字段: {label_text}")
        return

    inp = row.locator("input").first
    if inp.count() == 0:
        return
    _fill_date_input(inp, label_text, value)


def _fill_date_input(inp, label_text, value):
    """填充日期 input，先 JS evaluate 再 fill 回退"""
    try:
        inp.evaluate(
            """(el, val) => {
                el.value = val;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            }""",
            value
        )
        logger.info(f"  已填 [{label_text}]: {value}")
    except Exception as e:
        logger.warning(f"  evaluate 失败 ({label_text}): {e}, 尝试 fill")
        try:
            inp.click(force=True)
            inp.fill("")
            inp.fill(str(value), force=True)
            logger.info(f"  已填(fill) [{label_text}]: {value}")
        except Exception as e2:
            logger.warning(f"  fill 也失败 ({label_text}): {e2}")


def _verify_course_form(course_frame, config):
    """校验课程表单字段（讲授题目/时长/日期时间），因刷新丢失则补填"""
    lecture_title = config.get("course_name", config.get("activity_name", ""))
    start_date = config.get("start_date", "")
    start_time = config.get("course_start_time", "19:30")
    end_time = config.get("course_end_time", "20:45")
    period_val = str(config.get("duration_hours", 1))

    result = course_frame.evaluate("""
        () => {
            let subj = document.querySelector('input[name="teachSubject"]');
            let per = document.querySelector('input[name="period"]');
            let sd = document.querySelector('input[name="startDay"], input[name="startDate"]');
            let ed = document.querySelector('input[name="endDay"], input[name="endDate"]');
            let st = document.querySelector('input[name="startTime"]');
            let et = document.querySelector('input[name="endTime"]');
            return {
                subject: subj ? (subj.value || '') : '',
                period: per ? (per.value || '') : '',
                startDay: sd ? (sd.value || '') : '',
                endDay: ed ? (ed.value || '') : '',
                startTime: st ? (st.value || '') : '',
                endTime: et ? (et.value || '') : '',
            };
        }
    """)
    refilled = 0

    if not result.get('subject'):
        course_frame.evaluate("""
            (title) => {
                let subj = document.querySelector('input[name="teachSubject"]');
                if (subj) {
                    subj.value = title;
                    subj.dispatchEvent(new Event('input', {bubbles: true}));
                    subj.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }
        """, lecture_title)
        logger.info("  [校验补填] 讲授题目 为空，补填")
        refilled += 1
    if not result.get('period'):
        course_frame.evaluate("""
            (period) => {
                let per = document.querySelector('input[name="period"]');
                if (per) {
                    per.value = period;
                    per.dispatchEvent(new Event('input', {bubbles: true}));
                    per.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }
        """, period_val)
        logger.info("  [校验补填] 时长 为空，补填")
        refilled += 1
    # 日期时间缺失补填
    if start_date:
        if not result.get('startDay'):
            if course_frame.locator("input[name='startDay']").count() > 0:
                _fill_laydate_date(course_frame.locator("input[name='startDay']").first, course_frame, start_date, "课程开始日期")
                refilled += 1
        if not result.get('endDay'):
            if course_frame.locator("input[name='endDay']").count() > 0:
                _fill_laydate_date(course_frame.locator("input[name='endDay']").first, course_frame, start_date, "课程结束日期")
                refilled += 1
    if not result.get('startTime'):
        if course_frame.locator("input[name='startTime']").count() > 0:
            _fill_laydate_time(course_frame.locator("input[name='startTime']").first, course_frame, f"{start_time}:00", "课程开始时间")
            refilled += 1
    if not result.get('endTime'):
        if course_frame.locator("input[name='endTime']").count() > 0:
            _fill_laydate_time(course_frame.locator("input[name='endTime']").first, course_frame, f"{end_time}:00", "课程结束时间")
            refilled += 1

    if refilled:
        logger.info(f"  [校验] 课程表单补填 {refilled} 个字段")
    else:
        logger.info("  [校验] 课程表单字段已就绪")
    return refilled


def _verify_teacher_form(frame, page, teacher_id, teacher_name):
    """校验教师表单（证件号/姓名），因刷新丢失则补填"""
    state = None
    for f in [frame] + list(page.frames):
        try:
            state = f.evaluate("""
                () => {
                    let inp = document.querySelector('input[data-field="teachNo"]');
                    let cell = document.querySelector('td[data-field="teacherName"] .layui-table-cell');
                    let edit = document.querySelector('.layui-table-edit');
                    return {
                        teachNo: inp ? (inp.value || '') : '',
                        teacherName: cell ? (cell.innerText || '').trim()
                                       : (edit ? (edit.value || '') : ''),
                    };
                }
            """)
            if state:
                break
        except Exception:
            continue
    if not state:
        return 0

    refilled = 0
    if not state.get('teachNo'):
        for f in [frame] + list(page.frames):
            try:
                done = f.evaluate("""(tid) => {
                    let inp = document.querySelector('input[data-field="teachNo"]');
                    if (!inp) return false;
                    inp.value = tid;
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    let cell = document.querySelector('td[data-field="teacherName"] .layui-table-cell');
                    if (cell) cell.click();
                    return true;
                }""", teacher_id)
                if done:
                    logger.info("  [校验补填] 教师证件号 为空，补填")
                    refilled += 1
                    break
            except Exception:
                continue
    if not state.get('teacherName'):
        for f in [frame] + list(page.frames):
            try:
                done = f.evaluate("""(name) => {
                    let edit = document.querySelector('.layui-table-edit');
                    if (edit && edit.offsetParent !== null) {
                        edit.value = name;
                        edit.dispatchEvent(new Event('input', {bubbles: true}));
                        edit.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                    let cell = document.querySelector('td[data-field="teacherName"] .layui-table-cell');
                    if (cell && cell.offsetParent !== null) {
                        cell.click();
                        return 'clicked';
                    }
                    return false;
                }""", teacher_name)
                if done:
                    if done == 'clicked':
                        f.evaluate("""(name) => {
                            let edit = document.querySelector('.layui-table-edit');
                            if (edit) {
                                edit.value = name;
                                edit.dispatchEvent(new Event('input', {bubbles: true}));
                                edit.dispatchEvent(new Event('change', {bubbles: true}));
                            }
                        }""", teacher_name)
                    logger.info("  [校验补填] 教师姓名 为空，补填")
                    refilled += 1
                    break
            except Exception:
                continue

    if refilled:
        logger.info(f"  [校验] 教师表单补填 {refilled} 个字段")
    else:
        logger.info("  [校验] 教师表单已就绪")
    return refilled


def _add_course(page_or_frame, config):
    """添加课程 — 点击按钮 → 在所有 frame 中找课程表单 → JS填写 → 保存"""
    frame = _get_frame(page_or_frame)
    page = frame.page
    logger.info("添加课程")

    lecture_title = config.get("course_name", config.get("activity_name", ""))
    start_date = config.get("start_date", "")
    start_time = config.get("course_start_time", "19:30")
    end_time = config.get("course_end_time", "20:45")
    period_val = str(config.get("duration_hours", 1))

    # 1. 点击添加课程按钮
    if not _click_btn_lay_event(page, "addCourse", "添加课程"):
        raise Exception("找不到 [lay-event='addCourse']")
    page.wait_for_timeout(1500)

    # 2. 在所有 frame 中搜索课程表单
    course_frame = None
    for f in [frame] + list(page.frames):
        try:
            found = f.evaluate("""
                () => {
                    let subj = document.querySelector('input[name=\"teachSubject\"]');
                    return !!(subj && subj.offsetParent !== null);
                }
            """)
            if found:
                course_frame = f
                logger.info(f"  课程表单在 frame: {f.url[:60] if f.url else 'page'}")
                break
        except Exception:
            continue

    if not course_frame:
        logger.error("  找不到课程表单（在所有frame中）")
        _dump(frame, "course_form_missing")
        return

    # 3. JS 填写文本字段
    course_frame.evaluate("""
        ({title, period}) => {
            let subj = document.querySelector('input[name=\"teachSubject\"]');
            let per = document.querySelector('input[name=\"period\"]');
            if (subj) {
                subj.value = title;
                subj.dispatchEvent(new Event('input', {bubbles: true}));
                subj.dispatchEvent(new Event('change', {bubbles: true}));
            }
            if (per) {
                per.value = period;
                per.dispatchEvent(new Event('input', {bubbles: true}));
                per.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }
    """, {"title": lecture_title, "period": period_val})
    _log_field("讲授题目", "TEXT", lecture_title, "teachSubject", True)
    _log_field("时长", "TEXT", period_val, "period", True)

    # 4. 日期时间
    if course_frame.locator("input[name='startDay']").count() > 0:
        if start_date:
            _fill_laydate_date(course_frame.locator("input[name='startDay']").first, course_frame, start_date, "课程开始日期")
            _fill_laydate_date(course_frame.locator("input[name='endDay']").first, course_frame, start_date, "课程结束日期")
        _fill_laydate_time(course_frame.locator("input[name='startTime']").first, course_frame, f"{start_time}:00", "课程开始时间")
        _fill_laydate_time(course_frame.locator("input[name='endTime']").first, course_frame, f"{end_time}:00", "课程结束时间")

    # 4.5 校验补填：网页刷新可能丢失已填字段，保存前确认
    _verify_course_form(course_frame, config)

    logger.info("课程已添加")


def _add_teacher(page_or_frame, config):
    """填写教师内联表格 — 点击后在所有 frame 中搜索新行并填写"""
    frame = _get_frame(page_or_frame)
    page = frame.page

    teacher = config.get("teacher", {})
    teacher_name = teacher.get("name", "")
    teacher_id = str(teacher.get("id_number", ""))
    logger.info(f"添加教师: {teacher_name} / {teacher_id}")

    # 1. 点击添加教师按钮
    if not _click_btn_lay_event(page, "addLecturer", "添加教师"):
        logger.error("找不到 [lay-event='addLecturer']")
        return
    page.wait_for_timeout(800)

    # 2. 在所有 frame 中搜索新出现的 teachNo input 并填写
    fill1_ok = False
    for f in [frame] + list(page.frames):
        try:
            result = f.evaluate("""
                (tid) => {
                    let inp = document.querySelector('input[data-field=\"teachNo\"]');
                    if (!inp || inp.offsetParent === null) return false;
                    inp.value = tid;
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    // 同时点击 teacherName cell 激活编辑
                    let cell = document.querySelector('td[data-field=\"teacherName\"] .layui-table-cell');
                    if (cell) cell.click();
                    return true;
                }
            """, teacher_id)
            if result:
                fill1_ok = True
                logger.info(f"  证件号已填在 frame: {f.url[:40] if f.url else 'page'}")
                break
        except Exception:
            continue

    if not fill1_ok:
        logger.error("  教师证件号填写失败：在所有frame中找不到 teachNo input")
        return
    _log_field("教师证件号", "TEXT", teacher_id, "teachNo", True)

    page.wait_for_timeout(500)

    # 3. 在所有 frame 中搜索激活后的 edit input 或 cell 并填写姓名
    fill2_ok = False
    for f in [frame] + list(page.frames):
        try:
            result = f.evaluate("""
                (name) => {
                    let edit = document.querySelector('.layui-table-edit');
                    if (edit && edit.offsetParent !== null) {
                        edit.value = name;
                        edit.dispatchEvent(new Event('input', {bubbles: true}));
                        edit.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                    let cell = document.querySelector('td[data-field=\"teacherName\"] .layui-table-cell');
                    if (cell && cell.offsetParent !== null) {
                        cell.innerText = name;
                        return true;
                    }
                    return false;
                }
            """, teacher_name)
            if result:
                fill2_ok = True
                break
        except Exception:
            continue

    if not fill2_ok:
        logger.error("  教师姓名填写失败：在所有frame中找不到 edit/cell")
        return

    _log_field("教师姓名", "TABLE_CELL", teacher_name, "activated", True)
    page.wait_for_timeout(300)

    # 4. 校验补填：证件号/姓名缺失则补填
    _verify_teacher_form(frame, page, teacher_id, teacher_name)

    logger.info("教师已添加")


def _save_course_form(page_or_frame):
    """保存课程+教师表单 (courseFormSubmit) — 搜索所有 frame"""
    frame = _get_frame(page_or_frame)
    page = frame.page
    logger.info("保存课程表单")

    for f in [frame] + list(page.frames):
        try:
            btn = f.locator("button[lay-filter='courseFormSubmit']").first
            if btn.count() > 0:
                btn.evaluate("el => el.click()")
                logger.info("  已点击保存 (courseFormSubmit)")
                page.wait_for_timeout(2000)
                return True
        except Exception:
            continue

    logger.warning("  未找到 courseFormSubmit 按钮")
    return False


def _back_to_list(page_or_frame):
    logger.info("返回列表 — 保存并返回列表")
    page = page_or_frame if hasattr(page_or_frame, 'goto') else page_or_frame.page

    # JS 点击 saveBack（在主页面执行，frame detached 后也能工作）
    try:
        page.evaluate("""
            () => {
                let btn = document.querySelector("button[lay-filter='saveBack']") ||
                          document.querySelector("button:has-text('保存并返回列表')") ||
                          document.querySelector("button:has-text('返回列表')");
                if (btn && btn.offsetWidth > 0) {
                    btn.click();
                    return 'clicked';
                }
                return 'not_found';
            }
        """)
        logger.info("  已点击保存返回 (JS)")
    except Exception as e:
        logger.warning(f"  saveBack JS click失败: {e}，尝试键盘 Enter")
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

    try:
        page.wait_for_timeout(2000)
    except Exception:
        pass


def _close_tab_and_reopen(page):
    """关闭当前科室院内活动tab → 重新从菜单打开（清空页面状态）"""
    logger.info("关闭当前tab并重新打开科室院内活动...")

    # 1. 点击 tab 上的 X 关闭按钮
    try:
        page.evaluate("""
            () => {
                // 找当前激活的 tab 的关闭按钮
                let activeTab = document.querySelector('li.layui-this .layui-tab-close');
                if (activeTab) { activeTab.click(); return 'clicked'; }
                // fallback: 找 deptGroupproj 对应的 tab
                let tabs = document.querySelectorAll('.layui-tab-title li');
                for (let tab of tabs) {
                    if (tab.getAttribute('lay-id') && tab.getAttribute('lay-id').indexOf('deptGroupproj') >= 0) {
                        let closeBtn = tab.querySelector('.layui-tab-close');
                        if (closeBtn) { closeBtn.click(); return 'clicked_by_id'; }
                    }
                }
                return 'not_found';
            }
        """)
        logger.info("  已关闭旧tab")
    except Exception as e:
        logger.warning(f"  关闭tab失败: {e}")

    page.wait_for_timeout(500)

    # 2. 重新导航到科室院内活动
    from src.browser.navigator import goto_dept_activity
    goto_dept_activity(page)
    logger.info("  ✅ 已重新打开科室院内活动")


def _select_radio(scope, value):
    try:
        scope.locator(f"input[type='radio'][value='{value}']").first.click(force=True)
        return
    except Exception:
        pass
    try:
        scope.locator(f"input[type='radio'][title='{value}']").first.click(force=True)
        return
    except Exception:
        pass
    try:
        scope.locator(f".layui-form-radio:has-text('{value}')").first.click(force=True)
    except Exception:
        pass


def create_full_course(page, config):
    frame = _get_frame(page)
    logger.info(f"=== 创建课程: {config.get('activity_name', '')} ===")
    _click_add(frame)
    _fill_activity_form(frame, config)
    _add_period(frame, config)
    _add_course(frame, config)
    _add_teacher(frame, config)
    _back_to_list(frame)
    return {"success": True}


def _click_add(frame):
    logger.info("点击新增")
    frame.locator("#groupAdd").click()
    frame.wait_for_timeout(2000)
    try:
        frame.wait_for_selector("input[name='groupProjName']", timeout=10000)
        logger.info("表单已加载")
    except Exception:
        logger.warning("表单加载超时")
        _dump(frame, "form_load")
