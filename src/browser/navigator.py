"""左侧栏导航 + iframe 管理"""

import logging
import time

logger = logging.getLogger("cme")


def goto_dept_activity(page):
    """导航到科室院内活动页面，返回内容 iframe"""
    logger.info("导航: 院内活动管理 → 科室院内活动")

    # JS 点击菜单（绕过 Playwright 可见性检查）
    page.evaluate("""
        () => {
            // 1. 展开"院内活动管理"菜单（如果折叠了）
            let navItems = document.querySelectorAll('.layui-nav-item');
            for (let item of navItems) {
                let cite = item.querySelector('cite');
                if (cite && cite.innerText.indexOf('院内活动管理') >= 0) {
                    if (!item.classList.contains('layui-nav-itemed')) {
                        item.querySelector('a').click();
                    }
                    break;
                }
            }
            // 2. 点击"科室院内活动"
            let links = document.querySelectorAll('a[lay-href*=\"deptGroupproj\"]');
            for (let link of links) {
                if (link.innerText.indexOf('科室院内活动') >= 0 || link.getAttribute('title') === '科室院内活动') {
                    link.click();
                    return 'clicked';
                }
            }
            return 'not_found';
        }
    """)
    page.wait_for_timeout(2000)

    # Step 3: 等待并返回内容 iframe
    frame = _get_content_frame(page)
    if frame:
        logger.info(f"已进入科室院内活动 (frame: {frame.url[:80]})")
    else:
        logger.warning("未找到内容 iframe")
    return frame


def get_content_frame(page):
    """获取当前页面内容所在的 iframe"""
    return _get_content_frame(page)


def _get_content_frame(page):
    """查找 deptGroupproj 页面的 iframe"""
    for frame in page.frames:
        if 'deptGroupproj' in frame.url:
            frame.wait_for_load_state("networkidle")
            return frame
    return None


def click_add_button(page, frame=None):
    """点击 新增 按钮（在 iframe 内）"""
    if frame is None:
        frame = _get_content_frame(page)
        if frame is None:
            logger.error("找不到内容 iframe")
            return False

    logger.info("点击'新增'按钮...")
    # <a class="layui-btn" id="groupAdd">新增</a>
    add_btn = frame.locator("#groupAdd")
    if add_btn.count() > 0:
        add_btn.click()
        page.wait_for_timeout(1500)
        logger.info("已打开新增活动表单")
        return True

    logger.error("找不到新增按钮 #groupAdd")
    return False
