# CME Automation — 继续教育平台自动化工具

面向**院内继续教育管理员**的桌面自动化工具：在继续医学教育（CME）平台上批量创建活动课程、按时发起扫码考勤、并把签到/签退二维码自动发送到微信群。

基于 Python + Playwright + Flask + Windows UI 自动化实现，全部操作在本机完成，不依赖平台提供的 API。

> ⚠️ **使用前请阅读[安全与合规声明](#安全与合规声明)**。本工具仅适用于你本人拥有或已获授权的账号与课程管理场景。

---

## 功能特性

| 模块 | 能力 |
|---|---|
| 登录认证 | Playwright 驱动 Edge 打开平台，自动填充账号密码；短信验证码支持**剪贴板监听自动识别**或弹窗手动输入 |
| 批量建课 | 自动完成「新增活动 → 举办周期 → 添加课程 → 添加教师」全流程；教师名单按顺序轮换；支持每天/工作日/周末/自定义日期模式 |
| 扫码考勤 | 在课程列表定位目标课程，点击「扫码考勤 → 开始考勤」，按设定时效生成二维码；签到/签退分别执行；二维码过期自动重发 |
| 微信下发 | 截图保存后发送到指定微信群/联系人，三级回退：CDP 控制 → pywinauto UIA → 保存到本地并提示手动发送 |
| 学分授予 | 批量在「待授分」列表中定位课程，执行考勤记录授分 |
| 状态同步 | 从平台回读课程的举办/授分状态，原子写回 `courses.yaml` |
| Web 界面 | Flask + 原生前端，参数配置、任务触发、实时日志（SSE）、二维码预览、考勤状态监控 |
| 定时任务 | 通过 Windows 任务计划程序定时启动；自动检测并解除锁屏；任务结束后空闲 N 分钟自动退出 |
| 稳定性 | 屏幕保活、自签名证书处理、持久化浏览器会话、崩溃后从 `data/workflow_state.json` 恢复 |

---

## 技术栈

- **Python 3.8+**
- **Playwright** — 浏览器自动化（Edge `channel="msedge"`，持久化 context）
- **Flask** — 本地 Web 控制台
- **pywinauto / pywin32 / uiautomation / pynput** — 微信桌面客户端自动化
- **PyYAML** — 配置与课程清单
- **Pillow** — 二维码截图处理
- **Windows 任务计划程序（schtasks）** — 定时启动

---

## 架构

```
┌─────────────────────────────┐
│  Web UI (web/)              │
│  Flask 路由 (src/app.py)    │
└──────────────┬──────────────┘
               │ 触发工作流
   ┌───────────┼────────────┬──────────────┐
   ▼           ▼            ▼              ▼
┌────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│browser/│ │attendance│ │ credits/ │ │ wechat/  │
│登录/建课│ │二维码考勤│ │ 学分授予 │ │ 截图发送 │
└───┬────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
    │           │            │            │
    └───────────┴────────────┴────────────┘
                     │
            ┌────────▼────────┐
            │  utils/ + data/ │
            │ 配置/日志/状态   │
            └─────────────────┘
```

| 目录 | 职责 |
|---|---|
| `src/app.py` | Flask 服务入口、所有 API 路由、工作流调度 |
| `src/browser/` | `session.py` 浏览器上下文、`auth.py` 登录、`navigator.py` 导航、`course_creator.py` 建课、`selectors.py` 选择器集中管理 |
| `src/attendance/` | `qr_manager.py` 二维码考勤、`scheduler.py` 时间调度、`sync_courses.py` 状态同步 |
| `src/credits/` | `grant.py` 学分授予流程 |
| `src/wechat/` | `controller.py` 微信控制、`sender.py` 发送、`fallback.py` 降级保存、`check_ready.py` 就绪检测 |
| `src/utils/` | `config.py` 配置加载校验、`logging_setup.py` 日志、`screen.py` 屏幕/锁屏、`state.py` 工作流状态、`clipboard_monitor.py` 验证码识别 |
| `src/auto_start.py` | 定时任务入口：锁屏检测解锁、启动 Web 服务、空闲退出 |
| `web/` | 前端模板与静态资源 |

---

## 快速开始

### 环境要求

- Windows 10 / 11
- Python 3.8 及以上
- Microsoft Edge
- 微信桌面客户端（可选，仅用于自动发送二维码）
- 可访问目标继续教育平台的网络环境

### 安装

```bash
git clone https://github.com/LuDongdon/-cme-automation.git
cd cme-automation

pip install -r requirements.txt
python -m playwright install chromium

copy config.example.yaml config.yaml
copy courses.example.yaml courses.yaml
```

> 也可以直接双击 `setup.bat` 完成依赖安装与目录创建。

### 配置

编辑 `config.yaml`，至少填写以下内容：

```yaml
browser:
  base_url: https://你的平台地址/index.html
login:
  username: '你的账号'
  password: '你的密码'
  verification_recipient: '验证码接收人姓名'
course:
  teachers:
  - name: 张三
    id_number: '18位证件号'
```

其余字段（学分、学科、活动形式、时间、微信群、微信路径等）按实际情况调整，完整说明见下一节。

### 启动

```bash
start.bat
```

或手动：

```bash
python src/app.py
```

启动后浏览器自动打开 `http://localhost:5000`。

首次使用流程：

1. 在「设置」中确认账号、教师、微信路径、锁屏 PIN
2. 在 `courses.yaml` 中准备课程清单（或使用界面导入）
3. 点击「建课」批量创建活动
4. 到达签到/签退时间后，点击「考勤」生成并下发二维码
5. 活动结束后点击「授分」

---

## 配置说明

| 配置节 | 关键字段 | 说明 |
|---|---|---|
| `browser` | `channel` / `headless` / `slow_mo` / `base_url` | 浏览器通道、是否无头、操作延迟、平台地址 |
| `login` | `username` / `password` / `verification_recipient` / `verification_timeout` | 平台账号、短信验证码接收人、等待超时 |
| `course` | `year` / `credits` / `teachers` / 各类枚举字段 | 建课表单默认值；`teachers` 为轮换名单，`id_number` 必填 |
| `attendance` | `sign_in_time` / `sign_out_time` / `qr_timeout_seconds` / `qr_resend_count` | 签到签退时间、二维码有效期与重发次数 |
| `wechat` | `target_groups` / `exe_path` / `search_offset_x/y` / `fallback_dir` | 发送目标、微信安装路径、搜索框偏移校准、降级保存目录 |
| `auto_start` | `enabled` / `time` / `day_mode` / `lock_password` / `idle_exit_minutes` | 定时任务开关与时间、锁屏 PIN、空闲退出时长（0=禁用） |
| `logging` | `level` / `log_dir` / `screenshot_dir` | 日志级别与目录 |

---

## 目录结构

```
cme-automation/
├── src/
│   ├── app.py                  # Flask 入口与工作流编排
│   ├── auto_start.py           # 定时任务入口
│   ├── browser/                # 浏览器自动化
│   ├── attendance/             # 二维码考勤
│   ├── credits/                # 学分授予
│   ├── wechat/                 # 微信发送
│   └── utils/                  # 配置、日志、屏幕、状态
├── web/
│   ├── templates/              # index.html / attendance.html
│   └── static/                 # app.js / style.css
├── docs/                       # 需求、技术规范、设计规范、执行计划
├── config.example.yaml         # 配置模板
├── courses.example.yaml        # 课程清单模板
├── requirements.txt
├── setup.bat
├── start.bat
├── auto_start_launcher.bat     # 计划任务调用的启动脚本
├── README.md
└── LICENSE
```

本地运行时会额外生成（已被 `.gitignore` 忽略，不会提交）：

```
config.yaml         # 真实配置（含账号密码）
courses.yaml        # 真实课程清单与状态
data/               # 浏览器 profile、会话、工作流状态
logs/               # 运行日志
screenshots/        # 调试截图
```

---

## 安全与合规声明

1. **授权使用**：本工具仅用于**你本人拥有或已获得明确授权**的平台账号、科室与课程管理。请勿用于他人账号。
2. **凭据只在本地**：账号、密码、证件号只写入本地 `config.yaml`，该文件已被 `.gitignore` 排除；仓库中仅提供占位符模板。请勿将真实配置提交到任何公开仓库。
3. **不得用于伪造记录**：禁止利用本工具伪造考勤、冒名授分、虚报学时或规避平台规则。继续医学教育学分具有职业资质效力，使用者须自行承担由此产生的全部合规与法律责任。
4. **不绕过身份验证**：短信验证码由平台发送，工具仅做剪贴板识别或人工输入辅助，不破解、不拦截、不伪造验证码。
5. **平台条款优先**：使用前请确认目标平台的服务条款允许自动化操作；如不允许，请停止使用。
6. **无担保**：本项目按 MIT 协议“按原样”提供，作者不对使用结果或任何损失承担责任。

---

## 已知限制

- 仅支持 **Windows**（依赖 pywin32 / pywinauto / UIA / 任务计划程序）。
- 强依赖目标平台的页面结构，所有选择器集中在 `src/browser/selectors.py`；平台改版后需同步维护。
- 微信自动化依赖桌面客户端版本，升级后可能需要重新校准搜索框偏移。
- 验证码识别依赖剪贴板/短信客户端，若平台要求图形验证码需人工介入。
- 不同分辨率下 `search_offset_x/y` 需要重新校准。

---

## License

[MIT](LICENSE) © 2026 LuDongdon
