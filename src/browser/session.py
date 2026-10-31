"""浏览器会话管理 — 启动、配置、关闭"""

import os
import logging
from playwright.sync_api import sync_playwright
from ..utils.config import PROJECT_ROOT

logger = logging.getLogger("cme")


class BrowserSession:
    """管理 Playwright 浏览器实例和上下文"""

    def __init__(self, config):
        self.config = config
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def start(self):
        """启动浏览器并返回主页面"""
        browser_cfg = self.config["browser"]

        logger.info("正在启动 Playwright...")
        self._playwright = sync_playwright().start()

        channel = browser_cfg.get("channel", "msedge")
        headless = browser_cfg.get("headless", False)
        slow_mo = browser_cfg.get("slow_mo", 300)
        viewport = {
            "width": browser_cfg.get("viewport_width", 1920),
            "height": browser_cfg.get("viewport_height", 1080),
        }

        # Try persistent context first (saves login state)
        persistent_dir = os.path.join(PROJECT_ROOT, "data", "browser_profile")
        tried_persistent = False

        for attempt in range(2):
            try:
                if attempt == 0:
                    os.makedirs(persistent_dir, exist_ok=True)
                    logger.info(f"启动浏览器: channel={channel} (persistent)")
                    self._context = self._playwright.chromium.launch_persistent_context(
                        user_data_dir=persistent_dir,
                        channel=channel,
                        headless=headless,
                        slow_mo=slow_mo,
                        ignore_https_errors=True,
                        viewport=viewport,
                    )
                    tried_persistent = True
                else:
                    # Fallback: simple launch + new context
                    logger.info(f"启动浏览器: channel={channel} (simple)")
                    self._browser = self._playwright.chromium.launch(
                        channel=channel,
                        headless=headless,
                        slow_mo=slow_mo,
                    )
                    self._context = self._browser.new_context(
                        ignore_https_errors=True,
                        viewport=viewport,
                    )

                break  # Success

            except Exception as e:
                if attempt == 0:
                    logger.warning(f"Persistent context failed: {e}")
                    # Keep profile intact — may be locked by a zombie process, not corrupted.
                    # Fall back to simple launch instead of destroying login state.
                    logger.info("Falling back to simple launch (profile preserved)")
                else:
                    raise

        # Get or create page
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = self._context.new_page()

        logger.info("浏览器启动完成")
        return self._page

    def navigate(self, url=None):
        """导航到目标URL"""
        if url is None:
            url = self.config["browser"]["base_url"]
        logger.info(f"导航到: {url}")
        self._page.goto(url, wait_until="networkidle")
        return self._page

    @property
    def page(self):
        return self._page

    @property
    def context(self):
        return self._context

    def screenshot(self, name="debug"):
        """保存当前页面截图（调试用）"""
        path = os.path.join(PROJECT_ROOT, "screenshots", f"{name}.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._page.screenshot(path=path)
        logger.info(f"截图保存: {path}")
        return path

    def close(self):
        """关闭浏览器"""
        logger.info("正在关闭浏览器...")
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.warning(f"关闭浏览器时出错: {e}")
        logger.info("浏览器已关闭")
