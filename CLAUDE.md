# CME Automation — CLAUDE.md

## 项目概述

继续教育网站自动化工具，自动完成课程创建和扫码考勤。

- 目标网站：`https://your-cme-platform.example.com/index.html`（在 `config.yaml` 的 `browser.base_url` 中配置）
- 位置：项目根目录（本文件所在目录）

## 关键文件路径

| 文件 | 路径 |
|------|------|
| 需求文档 | `docs/requirements.md` |
| 技术规范 | `docs/tech-spec.md` |
| 设计规范 | `docs/design-standards.md` |
| 执行步骤 | `docs/execution-plan.md` |
| 开发日志 | `devlog/` (按日期命名) |
| 配置文件 | `config.yaml` |
| Web入口 | `src/app.py` |

## 工作说明

### 开发原则
1. **一次只做一个阶段**，参考 `docs/execution-plan.md` 的 Phase 划分
2. **每完成一个模块立即测试**，不积累问题
3. **每天结束时更新 `devlog/YYYY-MM-DD.md`**，记录完成事项和待办事项（`devlog/` 仅本地保留，已加入 .gitignore，不提交）
4. **选择器集中在 `src/browser/selectors.py`**，不散落在各模块中
5. **所有代码遵循 `docs/tech-spec.md` 和 `docs/design-standards.md`** 的规范

### 典型工作流程
1. 查看 `docs/execution-plan.md` 确认当前阶段
2. 查看 `devlog/` 最新日志了解进度和遗留问题
3. 编码实现 → 测试验证 → 更新 devlog

### 注意事项
- 网站使用自签名证书，Playwright 需要 `ignore_https_errors=True`
- 使用 `channel="msedge"` 启动 Edge 浏览器以利用其保存的密码
- 登录验证码通过监控剪贴板自动识别填入：验证码类关键词（校验码/验证码/动态码等）优先，找不到则取任意"独立"的 6 位数字兜底（逻辑在 `src/utils/clipboard_monitor.py`）；也可在弹窗手动输入
- 微信桌面客户端为 xwechat (WeChatAppEx.exe)，自动化时优先尝试 CDP 连接
- 不要在代码中硬编码任何凭据
- 定时任务启动时（`auto_start.py` 给 `app.py` 传 `--auto`）启用空闲自动退出：自动任务结束后，若 N 分钟（默认 10，配置 `config.yaml` → `auto_start.idle_exit_minutes`，0=禁用）无 Web 界面操作且无活动任务，自动结束 Flask 进程（浏览器/微信保留）。空闲判定为 HTTP 请求（非 SSE），页面挂着不点击也算空闲。手动启动（start.bat，无 `--auto`）永不自动退出

### 启动方式
```bash
# 激活环境
conda activate base

# 进入项目
cd <项目根目录>

# 启动 Web 服务
python src/app.py

# 或双击
start.bat
```
