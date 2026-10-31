# 继续教育自动化工具 — 技术规范

## 技术栈

| 组件 | 选型 | 版本 |
|------|------|------|
| 语言 | Python | 3.8+ |
| 浏览器自动化 | Playwright | 1.48.0 |
| Web 框架 | Flask | 2.x |
| 定时调度 | APScheduler | 3.x |
| 微信自动化 | pywinauto | 0.6.x |
| 配置管理 | PyYAML | 6.x |
| 图像处理 | Pillow | 10.x |

## 浏览器配置

- 使用 Edge 浏览器 (`channel="msedge"`)
- `ignore_https_errors=True` 处理自签名证书
- 使用 `launch_persistent_context()` 保持会话
- 用户数据目录：`data/browser_profile/`
- 默认视口：1920x1080
- `slow_mo`: 300ms（可配置）

## 编码规范

### 选择器策略
- 优先级：文本选择器 > role选择器 > CSS类名 > XPath
- 所有选择器集中定义在 `src/browser/selectors.py`
- 使用 Playwright 的 `page.get_by_text()`, `page.get_by_role()` 优先

### 错误处理
- 每个浏览器操作都需要 try/except
- 失败时自动截图保存到 `logs/errors/`
- 可重试操作最多重试3次，间隔10秒
- 不可恢复的错误通过 Web UI 通知用户

### 日志规范
- 使用 Python logging 模块
- 日志级别：DEBUG(开发) / INFO(生产)
- 日志格式：`[时间] [级别] [模块] 消息`
- 同时输出到文件和 Web UI (SSE)

### 状态管理
- 运行时状态保存在 `data/state.json`
- 包含：当前步骤、已完成步骤、浏览器PID、微信连接状态
- 支持崩溃恢复：启动时读取 state.json 确定从何处继续

## 安全注意事项

- 不在代码中硬编码密码：真实凭据只写入本地 `config.yaml`（已在 .gitignore 中忽略），仓库仅提供 `config.example.yaml` 模板
- 验证码由用户手动输入，不自动处理
- HTTPS 自签名证书仅忽略，不信任
- 微信自动化仅在用户登录的桌面客户端操作
