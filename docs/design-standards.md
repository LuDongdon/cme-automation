# 继续教育自动化工具 — 设计规范

## 架构原则

1. **模块化**：浏览器控制、考勤管理、微信发送各自独立
2. **低耦合**：模块间通过明确接口通信，不直接访问对方内部状态
3. **可观测**：每个操作都有日志，关键步骤有截图
4. **可恢复**：崩溃后能从最近的检查点继续

## 模块接口规范

### Browser 模块
```
session.py:
  - launch(config) -> BrowserContext
  - new_page(context, url) -> Page
  - close(context)

auth.py:
  - login(page, config) -> bool
  - wait_for_verification(page) -> str  # 返回用户输入的验证码

course_creator.py:
  - create_course(page, course_config) -> dict  # 返回创建结果

navigator.py:
  - goto_dept_activity(page) -> None
  - click_add_button(page) -> None
```

### Attendance 模块
```
qr_manager.py:
  - start_attendance(page, course_name) -> list  # 返回截图路径列表
  - close_qr_popup(page) -> None
  - screenshot_qr(page) -> str  # 返回截图路径

scheduler.py:
  - schedule_sign_in(time, callback) -> Job
  - schedule_sign_out(time, callback) -> Job
  - run() -> None  # 阻塞等待
```

### WeChat 模块
```
sender.py:
  - send_image(image_path, group_name) -> bool

controller.py:
  - find_window() -> WindowHandle
  - search_group(name) -> bool
  - paste_and_send() -> bool

fallback.py:
  - save_for_manual(image_path, group_name) -> None
```

## 前端设计规范

### 技术选型
- 纯 HTML/CSS/JS，不引入前端框架（保持轻量）
- 使用 SSE (Server-Sent Events) 推送实时日志
- Fetch API 与后端通信

### UI 组件
- 配置表单：输入框、下拉框、单选按钮、开关
- 日志面板：滚动日志区域，自动滚动到最新
- 进度指示：步骤进度条
- 按钮状态：正常/加载中/禁用

### 配色方案
- 主色调：#1890ff（蓝色系，医疗/专业感）
- 成功：#52c41a
- 警告：#faad14
- 错误：#f5222d
- 背景：#f0f2f5
- 卡片：#ffffff

## 数据流

```
Web UI (用户操作)
    ↓ POST /api/start
Flask App (编排层)
    ↓ 调用各模块
Browser Module ← → Attendance Module → WeChat Module
    ↓                    ↓                   ↓
 网站操作           二维码截图           微信群发送
    ↓                    ↓                   ↓
 状态更新 ← ← ← ← SSE 推送日志 → → → UI 显示
```
