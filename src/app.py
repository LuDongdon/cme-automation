"""Flask Web 服务入口 — 启动后浏览器访问 http://localhost:5000"""

import os
import sys
import time
import threading

# 确保 src 目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, jsonify, request, Response
from src.utils.config import load_config
from src.utils.logging_setup import setup_logging
from src.utils.state import WorkflowState

import logging
logger = logging.getLogger("cme")

# 全局状态
_verification_event = threading.Event()
_verification_code = None
_session = None
_page = None
_resume_state = None  # 恢复时的工作流状态

# 自动模式（由 auto_start.py 定时任务启动时传入 --auto）与空闲自动退出
_AUTO_MODE = "--auto" in sys.argv
_active_threads = set()          # 正在运行的工作流线程（用于"是否有活动任务"判断）
_active_lock = threading.Lock()
_last_activity = time.time()     # 最近一次 Web 界面操作（任意非 SSE 请求）
_tasks_empty_since = None        # 活动任务数变为 0 的时刻（None=期间有任务）
_NO_ACTIVITY_PATHS = {"/api/logs/stream"}  # 这些请求不算用户操作（SSE 持续连接）


def _spawn_workflow_thread(target, *args, **kwargs):
    """启动后台工作流线程，并跟踪其存活状态（供空闲自动退出判断）。

    线程结束后自动从 _active_threads 移除；与原 threading.Thread(daemon=True)
    行为一致，仅增加跟踪能力。
    """
    def _wrapped():
        try:
            target(*args, **kwargs)
        finally:
            with _active_lock:
                _active_threads.discard(threading.current_thread())
            # 任一后台任务（start/grant/sync/resume）真正结束都推送该标记，
            # SSE 转发给前端以解锁操作按钮（无论成功/失败/提前 return 都会发）。
            logger.info("TASK_DONE")

    t = threading.Thread(target=_wrapped, daemon=True)
    with _active_lock:
        _active_threads.add(t)
    t.start()
    return t


def _has_active_task():
    """是否已有工作流线程在运行（串行约束：同一时间只跑一个任务，抢同一个浏览器页面）"""
    with _active_lock:
        return len(_active_threads) > 0


def _busy_response():
    return jsonify({"status": "busy", "message": "已有任务正在运行，请等待其结束后再操作"}), 409


def create_app():
    """创建 Flask 应用"""
    # Use absolute paths for template and static folders
    _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(
        __name__,
        template_folder=os.path.join(_base, "web", "templates"),
        static_folder=os.path.join(_base, "web", "static"),
    )

    config = load_config()
    log_level = config.get("logging", {}).get("level", "INFO")
    _, log_stream = setup_logging(level=log_level)

    # ===== 路由 =====
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/attendance")
    def attendance():
        return render_template("attendance.html")

    @app.route("/api/status")
    def api_status():
        global _session
        return jsonify({
            "status": "ok",
            "browser_connected": _session is not None,
            "logged_in": False,
        })

    @app.route("/api/courses")
    def api_courses():
        """返回课程列表"""
        import yaml
        courses_path = os.path.join(os.path.dirname(__file__), "..", "courses.yaml")
        try:
            with open(courses_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return jsonify(data)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/config")
    def api_config():
        course = config.get("course", {})
        attendance_cfg = config.get("attendance", {})
        wechat_cfg = config.get("wechat", {})
        login_cfg = config.get("login", {})
        return jsonify({
            "course": {
                "default_start_time": course.get("default_start_time", "19:30"),
                "default_end_time": course.get("default_end_time", "20:45"),
                "period_start_time": course.get("period_start_time", "09:00"),
                "period_end_time": course.get("period_end_time", "23:00"),
                "duration_hours": course.get("duration_hours", 1),
                "teachers": course.get("teachers") or [course.get("teacher", {})],
                # 保留单教师字段，兼容旧前端
                "teacher_name": course.get("teacher", {}).get("name", ""),
                "teacher_id": course.get("teacher", {}).get("id_number", ""),
                "planned_students": course.get("planned_students", 6),
            },
            "attendance": {
                "sign_in_time": attendance_cfg.get("sign_in_time", "19:30"),
                "sign_out_time": attendance_cfg.get("sign_out_time", "20:45"),
                "qr_timeout_seconds": attendance_cfg.get("qr_timeout_seconds", 300),
                "qr_resend_count": attendance_cfg.get("qr_resend_count", 3),
                "qr_save_dir": attendance_cfg.get("qr_save_dir", ""),
            },
            "wechat": {
                "enabled": wechat_cfg.get("enabled", True),
                "target_groups": wechat_cfg.get("target_groups", []),
                "exe_path": wechat_cfg.get("exe_path", ""),
            },
            "login": {
                "username": login_cfg.get("username", ""),
                "password": login_cfg.get("password", ""),
                "verification_recipient": login_cfg.get("verification_recipient", ""),
            },
            "auto_start": {
                "enabled": config.get("auto_start", {}).get("enabled", False),
                "time": config.get("auto_start", {}).get("time", "19:20"),
                "day_mode": config.get("auto_start", {}).get("day_mode", "every_day"),
                "auto_confirm_delay": config.get("auto_start", {}).get("auto_confirm_delay", 3),
                "idle_exit_minutes": config.get("auto_start", {}).get("idle_exit_minutes", 10),
            },
        })

    @app.route("/api/settings", methods=["POST"])
    def api_settings():
        """保存登录账号与教师信息（写回 config.yaml）"""
        data = request.get_json() or {}
        result = _save_account_settings(data, config)
        return jsonify(result), (400 if result.get("status") == "error" else 200)

    @app.route("/api/start", methods=["POST"])
    def api_start():
        """启动工作流"""
        global _session, _page
        if _has_active_task():
            return _busy_response()
        data = request.get_json() or {}
        mode = data.get("mode", "full")  # full | course_only | attend_only

        logger.info(f"启动工作流: mode={mode}")
        logger.info(f"参数: {data}")

        # 在后台线程启动浏览器和登录
        _spawn_workflow_thread(_run_workflow, data, config, app)

        return jsonify({"status": "started", "mode": mode})

    @app.route("/api/state")
    def api_state():
        """获取未完成工作流状态（崩溃恢复检测）"""
        has_incomplete = WorkflowState.has_incomplete()
        state = WorkflowState.load() if has_incomplete else None
        return jsonify({
            "has_incomplete": has_incomplete,
            "state": state,
        })

    @app.route("/api/state/clear", methods=["POST"])
    def api_state_clear():
        """放弃未完成的工作流"""
        WorkflowState.clear()
        return jsonify({"status": "cleared"})

    @app.route("/api/resume", methods=["POST"])
    def api_resume():
        """恢复未完成的工作流"""
        global _resume_state, _session, _page
        if _has_active_task():
            return _busy_response()
        state = WorkflowState.load()
        if not state:
            return jsonify({"status": "error", "message": "没有可恢复的状态"}), 400

        logger.info(f"恢复工作流: phase={state.get('phase')} mode={state.get('mode')}")

        # 使用保存的 course_config 恢复参数
        saved_config = state.get("course_config", {})
        data = {
            "mode": state.get("mode", "full"),
            "selected_course_ids": saved_config.get("selected_course_ids", []),
            "course_start_time": saved_config.get("course_start_time", "19:30"),
            "course_end_time": saved_config.get("course_end_time", "20:45"),
            "sign_in_time": saved_config.get("sign_in_time", "19:30"),
            "sign_out_time": saved_config.get("sign_out_time", "20:45"),
            "qr_timeout": saved_config.get("qr_timeout", 300),
            "qr_resend_count": saved_config.get("qr_resend_count", 3),
            "wechat_group": saved_config.get("wechat_group", ""),
            "attendance_course_name": saved_config.get("attendance_course_name", ""),
            "single_date": saved_config.get("single_date", ""),
            "date_mode": saved_config.get("date_mode", "mon_fri"),
            "_resume": True,  # 内部标记
        }

        _resume_state = state
        _spawn_workflow_thread(_run_workflow, data, config, app)
        return jsonify({"status": "resumed", "phase": state.get("phase")})

    @app.route("/api/grant", methods=["POST"])
    def api_grant():
        """执行授分流程 — 支持单个 course_name 或批量 course_names"""
        if _has_active_task():
            return _busy_response()
        data = request.get_json() or {}
        # 支持批量 course_names 和单个 course_name（向后兼容）
        course_names = data.get("course_names", [])
        course_name = data.get("course_name", "").strip()
        if course_name and course_name not in course_names:
            course_names.append(course_name)

        if not course_names:
            return jsonify({"status": "error", "message": "请指定课程名称"}), 400

        logger.info(f"启动批量授分: {len(course_names)} 门课程")
        for cn in course_names:
            logger.info(f"  - {cn[:50]}")

        _spawn_workflow_thread(_run_batch_grant, course_names, config, app)
        return jsonify({"status": "started", "count": len(course_names)})

    @app.route("/api/sync", methods=["POST"])
    def api_sync():
        """手动触发课程状态同步 — 读取网页举办/授分状态，比对并更新 courses.yaml"""
        if _has_active_task():
            return _busy_response()
        logger.info("手动触发课程状态同步")
        _spawn_workflow_thread(_run_sync, config, app)
        return jsonify({"status": "started"})

    @app.route("/api/verify", methods=["POST"])
    def api_verify():
        """接收用户通过弹窗输入的验证码"""
        global _verification_code, _verification_event
        data = request.get_json() or {}
        code = data.get("code", "").strip()
        if code:
            _verification_code = code
            _verification_event.set()
            logger.info(f"验证码已接收: {code}")
            return jsonify({"status": "ok"})
        return jsonify({"status": "error", "message": "验证码为空"}), 400

    @app.route("/api/wechat/chats")
    def api_wechat_chats():
        """获取微信聊天窗口列表"""
        chats = []
        try:
            import pywinauto
            from pywinauto.application import Application
            app = Application(backend="uia").connect(path="WeChatAppEx.exe")
            dlg = app.window(class_name="WeChatMainWndForPC")
            # 遍历会话列表
            sessions = dlg.descendants(control_type="ListItem")
            for s in sessions[:20]:
                name = s.window_text()
                if name and len(name) > 1:
                    chats.append(name)
        except ImportError:
            logger.info("pywinauto 未安装，返回空列表")
        except Exception as e:
            logger.info(f"获取微信聊天列表失败: {e}")
        return jsonify({"chats": chats})

    @app.route("/api/auto-start/settings", methods=["GET", "POST"])
    def api_auto_start_settings():
        """读取/保存定时自动启动设置"""
        if request.method == "GET":
            ac = config.get("auto_start", {})
            return jsonify({
                "enabled": ac.get("enabled", False),
                "time": ac.get("time", "19:20"),
                "day_mode": ac.get("day_mode", "every_day"),
                "lock_password": ac.get("lock_password", ""),
                "auto_confirm_delay": ac.get("auto_confirm_delay", 3),
            })

        # POST — 保存设置并管理计划任务
        data = request.get_json() or {}
        result = _save_auto_start_config(data, config)
        return jsonify(result)

    @app.route("/api/auto-start/status")
    def api_auto_start_status():
        """检查 CME_AutoStart 计划任务状态"""
        import subprocess
        try:
            r = subprocess.run(
                'schtasks /query /tn "CME_AutoStart" /fo CSV /nh',
                shell=True, capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                # 解析 CSV 输出获取下次运行时间
                lines = r.stdout.strip().split("\n")
                parts = lines[-1].split('","')
                next_run = ""
                for p in parts:
                    p = p.strip('"')
                    if ":" in p and ("/" in p or "-" in p):
                        next_run = p
                        break
                return jsonify({"exists": True, "next_run": next_run, "raw": r.stdout[:500]})
            else:
                return jsonify({"exists": False, "next_run": ""})
        except Exception as e:
            return jsonify({"exists": False, "error": str(e)})

    @app.route("/api/logs/stream")
    def api_logs_stream():
        """SSE 日志推送"""
        def generate():
            queue = []

            def on_log(msg):
                queue.append(msg)

            log_stream.add_listener(on_log)
            try:
                while True:
                    if queue:
                        msg = queue.pop(0)
                        yield f"data: {msg}\n\n"
                    else:
                        import time
                        time.sleep(0.1)
            except GeneratorExit:
                pass
            finally:
                log_stream.remove_listener(on_log)

        return Response(generate(), mimetype="text/event-stream")

    @app.before_request
    def _track_user_activity():
        """记录最近一次 Web 界面操作（供空闲自动退出判断；SSE 流除外）"""
        global _last_activity
        if request.path not in _NO_ACTIVITY_PATHS:
            _last_activity = time.time()

    # 自动模式（定时任务启动）下的空闲自动退出监控
    if _AUTO_MODE:
        threading.Thread(
            target=_idle_exit_monitor,
            args=(config,),
            daemon=True,
        ).start()

    logger.info("Flask 应用已创建")
    return app, config


def _run_workflow(data, config, app):
    """后台运行的工作流"""
    global _session, _page, _verification_event, _verification_code, _resume_state
    from src.browser.session import BrowserSession
    from src.browser.auth import login

    is_resume = data.get("_resume", False)

    # ---- 屏幕唤醒/保活: 解除屏保 + 工作流期间每30s重发 ES 标志 ----
    from src.utils import screen
    keep_alive_stop = threading.Event()
    keep_alive_thread = None

    def _keep_alive_loop():
        while not keep_alive_stop.is_set():
            try:
                screen.keep_awake()
            except Exception:
                pass
            keep_alive_stop.wait(30)

    try:
        # 确保屏幕唤醒/解锁。屏保或锁屏会抢占前台,导致后续浏览器/微信
        # 的窗口激活和按键全部失效——必须先解除。
        screen.ensure_screen_ready()
        keep_alive_thread = threading.Thread(target=_keep_alive_loop, daemon=True)
        keep_alive_thread.start()
        logger.info("屏幕保活已启动(每30s重发,工作流期间阻止睡眠/屏保)")

        # 启动浏览器：已运行的直接复用，避免同一 profile 被二次启动占用
        # （上一工作流结束后浏览器是故意不关的，与 _run_sync/_run_batch_grant 行为一致）
        if _page is None or _page.is_closed():
            if _page is not None:
                logger.info("浏览器页面已关闭（前次流程结束），重新启动...")
            else:
                logger.info("浏览器未启动，正在启动...")
            _session = BrowserSession(config)
            _page = _session.start()
            # 导航到网站
            _session.navigate()
        else:
            logger.info("浏览器已运行，跳过启动")

        # 定义验证码回调（供 login 使用）
        def verify_callback(prompt_msg, timeout=120):
            global _verification_event, _verification_code
            logger.info(f"VERIFY_NEEDED: {prompt_msg}")
            _verification_event.clear()
            _verification_code = None

            # 启动剪贴板监控（与手动输入并行，互为补充）
            from src.utils.clipboard_monitor import start_clipboard_monitor

            def on_clipboard_code(code):
                global _verification_code
                _verification_code = code
                logger.info(f"VERIFY_AUTO_FILLED: {code}")
                _verification_event.set()

            stop_monitor = start_clipboard_monitor(on_clipboard_code)

            if _verification_event.wait(timeout=timeout):
                stop_monitor()
                logger.info(f"收到验证码: {_verification_code}")
                return _verification_code
            else:
                stop_monitor()
                if timeout < 120:
                    logger.info(f"验证码等待超时（{timeout}秒）")
                else:
                    logger.error("验证码输入超时（120秒）")
                return None

        # 检测是否已登录
        from src.browser.auth import check_already_logged_in
        if is_resume and _resume_state and _resume_state.get("phase") != "login":
            logger.info("恢复模式：跳过登录（使用已保存的会话）")
            if not check_already_logged_in(_page):
                logger.warning("恢复模式：登录状态已失效，重新登录")
                success = login(_page, verify_callback)
                if not success:
                    logger.error("登录失败，工作流终止")
                    return
        elif check_already_logged_in(_page):
            logger.info("检测到已登录状态，跳过登录流程")
        else:
            logger.info("需要登录，开始登录流程")
            success = login(_page, verify_callback)
            if not success:
                logger.error("登录失败，工作流终止")
                return

        # 导航到科室院内活动（返回内容 iframe）
        from src.browser.navigator import goto_dept_activity, get_content_frame
        content_frame = goto_dept_activity(_page)
        if content_frame is None:
            content_frame = get_content_frame(_page)
        logger.info("✅ 已进入科室院内活动页面")

        # 确保微信就绪（提前到考勤前，但不在 WebUI 启动时）
        # 仅 full/attend_only 模式需要微信，course_only 跳过
        mode = data.get("mode", "full")
        wechat_enabled = config.get("wechat", {}).get("enabled", True)
        if mode in ("full", "attend_only") and wechat_enabled:
            logger.info("检测微信状态（考勤前预检）...")
            try:
                wechat_exe_path = config.get("wechat", {}).get("exe_path", None)
                from src.wechat.controller import ensure_wechat_ready
                wx_ok = ensure_wechat_ready(wechat_path=wechat_exe_path)
                if wx_ok:
                    logger.info("✅ 微信已就绪")
                else:
                    logger.warning("⚠ 微信未就绪，考勤时微信发送将自动回退到本地保存")
            except Exception as wx_err:
                logger.warning(f"微信预检异常: {wx_err}，考勤时微信发送将自动回退")

        # 保存初始状态 — 崩溃恢复起点
        course_config = {
            "selected_course_ids": data.get("selected_course_ids", []),
            "course_start_time": data.get("course_start_time", "19:30"),
            "course_end_time": data.get("course_end_time", "20:45"),
            "sign_in_time": data.get("sign_in_time", "19:30"),
            "sign_out_time": data.get("sign_out_time", "20:45"),
            "qr_timeout": data.get("qr_timeout", 300),
            "qr_resend_count": data.get("qr_resend_count", 3),
            "wechat_group": data.get("wechat_group", ""),
            "attendance_course_name": data.get("attendance_course_name", ""),
            "single_date": data.get("single_date", ""),
            "date_mode": data.get("date_mode", "mon_fri"),
        }
        WorkflowState.save({
            "phase": "course_creation" if mode != "attend_only" else "attendance",
            "mode": mode,
            "current_index": 0,
            "total_courses": len(data.get("selected_course_ids", [])),
            "completed_course_ids": _resume_state.get("completed_course_ids", []) if _resume_state else [],
            "course_config": course_config,
            "attendance_phase": None,
            "qr_screenshot_index": 0,
        })
        _resume_state = None  # 消耗掉，只用一次

        # 根据模式执行后续操作
        if mode in ("full", "course_only"):
            _run_course_creation(data, config)
        if mode in ("full", "attend_only"):
            _run_attendance(data, config)

        # 当日任务结束：从网站读取举办/授分状态，比对并更新 courses.yaml
        try:
            from src.attendance.sync_courses import sync_courses_from_website
            sync_courses_from_website(_page)
        except Exception as sync_err:
            logger.error(f"课程状态同步失败: {sync_err}", exc_info=True)

    except Exception as e:
        logger.error(f"工作流异常: {e}", exc_info=True)
        # 状态文件保留在磁盘上，供恢复使用
        logger.info("状态文件已保留，可重启后恢复")
    finally:
        keep_alive_stop.set()
        logger.info("屏幕保活已停止")
        # 不关闭浏览器，让用户可以看到状态
        pass


def _teachers_list(course_cfg):
    """返回非空的授课教师名单 [{'name','id_number'}, ...]，顺序即轮换顺序。

    正常配置在 src/utils/config.py 的 _normalize_teachers() 里已归一化；
    这里再兜一层，保证建课时 teachers[0] 一定存在。
    """
    teachers = course_cfg.get("teachers")
    if isinstance(teachers, list) and teachers:
        return teachers
    single = course_cfg.get("teacher")
    if isinstance(single, dict) and single.get("name"):
        return [single]
    raise ValueError("未配置授课教师，请在 config.yaml 的 course.teachers 中设置")


def _run_course_creation(data, config):
    """执行课程创建流程"""
    global _page
    from src.browser.navigator import click_add_button
    import yaml

    logger.info("=== 课程创建流程 ===")

    # Build course config from user input + defaults
    course_cfg = config.get("course", {})
    _teachers = _teachers_list(course_cfg)
    base_config = {
        "year": course_cfg.get("year", 2026),
        "course_start_time": data.get("course_start_time", "19:30"),
        "course_end_time": data.get("course_end_time", "20:45"),
        "period_start_time": course_cfg.get("period_start_time", "09:00"),
        "period_end_time": course_cfg.get("period_end_time", "23:00"),
        "activity_content": course_cfg.get("activity_content", "专业课"),
        "medical_system": course_cfg.get("medical_system", "西医"),
        "planned_students": data.get("planned_students", 6),
        "discipline": course_cfg.get("discipline", "临床外科学"),
        "sub_discipline": course_cfg.get("sub_discipline", "整形外科学"),
        "activity_form": course_cfg.get("activity_form", "研讨会"),
        "assessment_method": course_cfg.get("assessment_method", "其他"),
        "credits": course_cfg.get("credits", 0.2),
        "duration_hours": course_cfg.get("duration_hours", 1),
        "is_lecturer_grant_credit": course_cfg.get("is_lecturer_grant_credit", True),
        "credit_category": course_cfg.get("credit_category", {}),
        # 教师名单：建课时按顺序轮换（每批次第 1 门课固定用第 1 位）
        "teachers": _teachers,
        "teacher": _teachers[0],
    }

    # Each course = one independent activity
    selected_course_ids = data.get("selected_course_ids", [])

    # Load courses from courses.yaml
    courses_yaml = os.path.join(os.path.dirname(__file__), "..", "courses.yaml")
    try:
        with open(courses_yaml, "r", encoding="utf-8") as f:
            courses_data = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"读取 courses.yaml 失败: {e}")
        return

    from datetime import date, datetime, timedelta
    today = date.today()
    today_str = today.isoformat()
    # 排课起点：今天未到默认考勤时间(attendance.sign_out_time, 默认20:45)则从今天开始；
    # 已过 → 从明天开始。（2026-08-24 用户要求：过了今天的默认考勤时间，
    # 创建课程默认从明天开始，不能创建当天已无法考勤的课程）
    default_attend_end = str(config.get("attendance", {}).get("sign_out_time", "20:45"))
    now_hhmm = datetime.now().strftime("%H:%M")
    if now_hhmm < default_attend_end:
        start_day = today
    else:
        start_day = today + timedelta(days=1)
        logger.info(f"当前 {now_hhmm} 已过默认考勤时间 {default_attend_end}，今天不再排课，从明天起")
    logger.info(
        f"排课起点日: {start_day}（当前 {now_hhmm}，默认考勤时间 {default_attend_end}，"
        f"{'今天' if start_day == today else '明天'}起排）")

    single_date = data.get("single_date", "")
    date_mode = data.get("date_mode", "every_day")
    is_multi = data.get("is_multi_course", False) or len(selected_course_ids) >= 2

    # Build flat course list with dates
    day_patterns = {
        "mon_fri": [0, 1, 2, 3, 4],
        "workdays": [0, 1, 2, 3, 4],
        "mon_wed_fri": [0, 2, 4],
        "tue_thu_sat": [1, 3, 5],
        "every_day": [0, 1, 2, 3, 4, 5, 6],
        "weekends": [5, 6],
    }
    offsets = day_patterns.get(date_mode, [0, 1, 2, 3, 4])
    id_set = set(selected_course_ids)

    # Build plan: flat list, each entry = one activity
    plan = []
    for cat in courses_data.get("categories", []):
        for c in cat.get("courses", []):
            if c["id"] not in id_set:
                continue
            plan.append({
                "course_name": c["course_name"],
                "course_id": c["id"],
                "_yaml_ref": c,
            })

    # ---- 冲突检测：构建已占用日期集合（所有已创建课程的 created_date）----
    occupied_dates = set()
    for cat in courses_data.get("categories", []):
        for c in cat.get("courses", []):
            st = c.get("status", {})
            if st.get("created") and st.get("created_date"):
                occupied_dates.add(str(st["created_date"]))
    logger.info(f"已创建课程占用日期 {len(occupied_dates)} 天: {sorted(occupied_dates)}")

    def _next_free_day(start, occupied, weekday_pattern):
        """从 start 起找第一个符合工作日模式且未被占用的日期"""
        d = start
        while True:
            wd = d.weekday()
            if wd in weekday_pattern and d.isoformat() not in occupied:
                return d
            d += timedelta(days=1)

    # Assign dates（含冲突检测：同一天已有课程则提示并顺延到下一空闲日）
    final_plan = []
    cursor = start_day
    for idx, item in enumerate(plan):
        if is_multi:
            course_date = _next_free_day(cursor, occupied_dates, offsets)
            if course_date > cursor:
                logger.warning(
                    f"  ⏭ 冲突顺延: {item['course_name'][:30]} "
                    f"{cursor} 该时段已有课程 → {course_date}")
            occupied_dates.add(course_date.isoformat())
            cursor = course_date + timedelta(days=1)  # 下一门从下一天起找
        else:
            if single_date:
                # 不能创建已过去日期的课程
                if single_date < today_str:
                    logger.error(
                        f"⛔ {item['course_name'][:30]}：{single_date} 是已过去的日期，"
                        f"不能创建，本次不创建")
                    continue
                # 不能创建当天已过默认考勤时间的课程（考勤无法进行）
                if single_date == today_str and now_hhmm >= default_attend_end:
                    logger.error(
                        f"⛔ {item['course_name'][:30]}：今天 {today_str} 的默认考勤时间 "
                        f"{default_attend_end} 已过（当前 {now_hhmm}），"
                        f"不能创建当天已无法考勤的课程，本次不创建")
                    continue
                if single_date in occupied_dates:
                    logger.error(
                        f"⛔ {item['course_name'][:30]}：{single_date} "
                        f"该时段已有课程创建，请更换日期，本次不创建")
                    continue  # 跳过，不进入 final_plan
                course_date = date.fromisoformat(single_date)
            else:
                course_date = _next_free_day(start_day, occupied_dates, offsets)
                if course_date > start_day:
                    logger.warning(
                        f"  ⏭ 冲突顺延: {item['course_name'][:30]} "
                        f"{start_day} 该时段已有课程 → {course_date}")
                occupied_dates.add(course_date.isoformat())
        wd = course_date.weekday()
        if wd >= 5:
            logger.warning(f"选择的日期是周末，课程通常在工作日")
        weekday_name = ["一", "二", "三", "四", "五", "六", "日"][wd]
        item["date"] = course_date.isoformat()
        item["weekday"] = weekday_name
        final_plan.append(item)

    if len(final_plan) < len(plan):
        logger.warning(f"共 {len(plan) - len(final_plan)} 门课程因日期冲突被跳过")
    plan = final_plan

    total = len(plan)
    logger.info(f"计划: {total} 个课程（每个课程 = 独立活动）")

    # Resume: skip courses already marked as created in courses.yaml
    skipped_count = 0
    for item in plan[:]:
        ref = item.get("_yaml_ref", {})
        if ref.get("status", {}).get("created", False):
            logger.info(f"  ⏭ 跳过(已创建): #{item['course_id']} {item['course_name'][:40]}")
            plan.remove(item)
            skipped_count += 1
    if skipped_count > 0:
        logger.info(f"共跳过 {skipped_count} 个已创建课程，剩余 {len(plan)} 个")
        if len(plan) == 0:
            logger.info("所有课程已创建完成")
            WorkflowState.save({
                "phase": "done" if data.get("mode") != "full" else "attendance",
                "mode": data.get("mode", "full"),
                "current_index": total,
                "total_courses": total,
                "completed_course_ids": data.get("selected_course_ids", []),
                "course_config": _get_course_config_snapshot(data),
                "attendance_phase": None,
                "qr_screenshot_index": 0,
            })
            return

    for item in plan:
        logger.info(f"  周{item['weekday']} {item['date']} | {item['course_name'][:40]}")

    # Execute: each course = full activity cycle
    from src.browser.navigator import click_add_button, get_content_frame
    from src.browser.course_creator import (
        _fill_activity_form, _add_period,
        _add_course, _add_teacher, _save_course_form, _close_tab_and_reopen,
    )
    from src.attendance.sync_courses import _read_course_names, _strip_suffix

    # 单门课程创建序列（主流程与失败重试共用）
    def _create_one_course(cfg):
        frame = get_content_frame(_page)
        if frame is None:
            raise RuntimeError("找不到内容 iframe")
        click_add_button(_page, frame)
        _fill_activity_form(frame, cfg)
        _add_period(frame, cfg)
        _add_course(frame, cfg)
        _add_teacher(frame, cfg)
        _save_course_form(frame)
        _close_tab_and_reopen(_page)

    for idx, item in enumerate(plan):
        cname = item["course_name"]
        cdate = item["date"]
        frame = get_content_frame(_page)
        if frame is None:
            logger.error(f"找不到内容 iframe，跳过: {cname[:30]}")
            continue
        logger.info(f"\n[{idx+1+skipped_count}/{total}] 周{item['weekday']} {cdate} | {cname[:40]}")

        # Save checkpoint BEFORE starting this course
        WorkflowState.save({
            "phase": "course_creation",
            "mode": data.get("mode", "full"),
            "current_index": idx + skipped_count,
            "total_courses": total,
            "completed_course_ids": [c["course_id"] for c in plan[:idx] if c.get("_yaml_ref", {}).get("status", {}).get("created", False)],
            "course_config": _get_course_config_snapshot(data),
            "attendance_phase": None,
            "qr_screenshot_index": 0,
        })

        try:
            cfg = dict(base_config)
            cfg["activity_name"] = cname  # Activity name = course name
            cfg["course_name"] = cname
            cfg["start_date"] = cdate
            cfg["end_date"] = cdate
            # Single course per activity → period time = course time
            cfg["period_start_time"] = cfg["course_start_time"]
            cfg["period_end_time"] = cfg["course_end_time"]
            # 授课教师按名单顺序轮换：每批次第 1 门课固定用第 1 位，
            # 失败的课第 2 次重试复用同一个 cfg，不会换人
            teacher = _teachers[idx % len(_teachers)]
            cfg["teacher"] = teacher
            logger.info(
                f"  授课教师: {teacher.get('name', '')}"
                f"（第 {idx % len(_teachers) + 1}/{len(_teachers)} 位）")

            _create_one_course(cfg)

            _update_course_status(cname, "created", True, cdate)
            logger.info(f"  ✅ {cname[:30]}")

            # Update completed list after success
            completed = [c["course_id"] for c in plan[:idx+1]]
            WorkflowState.save({
                "phase": "course_creation",
                "mode": data.get("mode", "full"),
                "current_index": idx + 1 + skipped_count,
                "total_courses": total,
                "completed_course_ids": completed,
                "course_config": _get_course_config_snapshot(data),
                "attendance_phase": None,
                "qr_screenshot_index": 0,
            })

        except Exception as e:
            logger.error(f"  ❌ {cname[:30]}: {e}")
            # 单门重试一次：先回干净列表页，确认网页上无同名课再重建（防重复创建）
            try:
                _close_tab_and_reopen(_page)
                list_frame = get_content_frame(_page)
                names = _read_course_names(list_frame) if list_frame else []
                if any(_strip_suffix(n) == _strip_suffix(cname) for n in names):
                    logger.info(f"  ⚠ 网页已存在同名课，按已创建处理")
                    _update_course_status(cname, "created", True, cdate)
                else:
                    logger.info(f"  🔄 第2次重试创建: {cname[:30]}")
                    _create_one_course(cfg)
                    _update_course_status(cname, "created", True, cdate)
                    logger.info(f"  ✅ 重试成功 {cname[:30]}")
            except Exception as e2:
                logger.error(f"  ❌ {cname[:30]} 重试仍失败: {e2}")
                try:
                    _page.keyboard.press("Escape")
                    time.sleep(0.5)
                except Exception:
                    pass

    logger.info(f"\n全部完成: {total} 个课程")

    # Transition to next phase
    next_phase = "attendance" if data.get("mode") == "full" else "done"
    WorkflowState.save({
        "phase": next_phase,
        "mode": data.get("mode", "full"),
        "current_index": total,
        "total_courses": total,
        "completed_course_ids": data.get("selected_course_ids", []),
        "course_config": _get_course_config_snapshot(data),
        "attendance_phase": None,
        "qr_screenshot_index": 0,
    })


def _get_course_config_snapshot(data):
    """Extract a serializable snapshot of course config from request data."""
    return {
        "selected_course_ids": data.get("selected_course_ids", []),
        "course_start_time": data.get("course_start_time", "19:30"),
        "course_end_time": data.get("course_end_time", "20:45"),
        "sign_in_time": data.get("sign_in_time", "19:30"),
        "sign_out_time": data.get("sign_out_time", "20:45"),
        "qr_timeout": data.get("qr_timeout", 300),
        "qr_resend_count": data.get("qr_resend_count", 3),
        "wechat_group": data.get("wechat_group", ""),
        "attendance_course_name": data.get("attendance_course_name", ""),
        "single_date": data.get("single_date", ""),
        "date_mode": data.get("date_mode", "mon_fri"),
    }


def _update_course_status(course_name, field, value, date_str=None):
    """Update a course's status in courses.yaml (atomic write to prevent corruption)"""
    import yaml
    courses_yaml = os.path.join(os.path.dirname(__file__), "..", "courses.yaml")
    tmp_path = courses_yaml + ".tmp"
    try:
        with open(courses_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        for cat in data.get("categories", []):
            for c in cat.get("courses", []):
                if c["course_name"] == course_name:
                    if "status" not in c:
                        c["status"] = {}
                    c["status"][field] = True
                    if date_str:
                        c["status"][f"{field}_date"] = date_str
                    break
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, courses_yaml)  # atomic on Windows
        logger.info(f"状态更新: {course_name[:30]} -> {field}=True")
    except Exception as e:
        logger.warning(f"状态更新失败: {e}")


def _get_course_created_date(course_name):
    """按课程名（去括号匹配）从 courses.yaml 读取计划举办日期 status.created_date。

    课程名可能带括号后缀，网站表格中的名称是去括号后的，需按 _strip_suffix 匹配。

    Returns:
        str | None: created_date（"YYYY-MM-DD"），读取失败或课程不存在返回 None
    """
    import re
    import yaml

    def _strip(name):
        return re.sub(r'[（(][^）)]*[）)]\s*$', '', name).strip()

    courses_yaml = os.path.join(os.path.dirname(__file__), "..", "courses.yaml")
    try:
        with open(courses_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        target = _strip(course_name)
        for cat in data.get("categories", []):
            for c in cat.get("courses", []):
                if _strip(c.get("course_name", "")) == target:
                    return c.get("status", {}).get("created_date")
    except Exception as e:
        logger.warning(f"读取课程日期失败: {e}")
    return None


def _run_attendance(data, config):
    """执行考勤流程（所有 Playwright 操作在同一个线程中）"""
    global _page
    from datetime import datetime
    from src.attendance.qr_manager import (
        start_attendance_flow,
        start_attendance_from_child_page,
        read_course_times_from_table,
        _back_to_activity_list,
    )

    logger.info("=== 考勤流程 ===")

    course_name = data.get("attendance_course_name", "")

    # ---- 防御：仅考勤今日课程（attend_only 模式）----
    # full(创建+考勤) 模式除外：课程刚创建，其日期由用户本次选择决定
    if data.get("mode") == "attend_only" and course_name:
        scheduled = _get_course_created_date(course_name)
        today_str = datetime.now().strftime("%Y-%m-%d")
        if scheduled and str(scheduled) != today_str:
            logger.error(
                f"⛔ 仅考勤模式：课程『{course_name[:40]}』的计划举办日期是 {scheduled}，"
                f"非今天({today_str})，拒绝考勤（仅考勤对应日期的课程）"
            )
            return

    sign_in_time = data.get("sign_in_time", "19:30")
    sign_out_time = data.get("sign_out_time", "20:45")
    qr_timeout = data.get("qr_timeout", 300)
    qr_count = data.get("qr_resend_count", 3)
    wechat_groups = [data.get("wechat_group", "").strip()] if data.get("wechat_group", "").strip() else []
    wechat_enabled = config.get("wechat", {}).get("enabled", True)

    # 轻量存活检查（微信已在工作流开始时启动，这里只验证还在不在）
    if wechat_enabled:
        logger.info("验证微信存活状态...")
        wx_alive = False
        try:
            import subprocess
            r = subprocess.run(
                'tasklist /fi "imagename eq Weixin.exe" /fo csv /nh',
                shell=True, capture_output=True, text=True, timeout=5,
            )
            if "Weixin.exe" not in r.stdout:
                r = subprocess.run(
                    'tasklist /fi "imagename eq WeChatAppEx.exe" /fo csv /nh',
                    shell=True, capture_output=True, text=True, timeout=5,
                )
            if "Weixin.exe" in r.stdout or "WeChatAppEx.exe" in r.stdout:
                from src.wechat.controller import _find_wechat_hwnd
                wx_hwnd = _find_wechat_hwnd()
                wx_alive = wx_hwnd is not None
        except Exception:
            pass
        if wx_alive:
            logger.info("✅ 微信仍在运行")
        else:
            logger.warning("⚠ 微信已不在运行，二维码将仅本地保存")
        # 微信检测可能激活了微信窗口，确保浏览器回到前台
        try:
            _page.bring_to_front()
        except Exception:
            pass

    # 阻止屏保/睡眠 — 考勤可能持续 15-30 分钟，屏保会阻断微信发送
    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    # 线程退出时自动重置，无需手动恢复
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 | 0x80000002)
    logger.info("已阻止系统屏保/睡眠")

    def wechat_sender(path, group, text_msg=None):
        if not wechat_enabled or not group:
            return False
        from src.wechat.sender import send_qr_screenshot
        return send_qr_screenshot(path, group, auto_mode=True, text_msg=text_msg)

    # ----- 从网页读取实际课程时间（点击扫码考勤，停留在子页面）-----
    now = datetime.now()
    sign_in_dt = None
    sign_out_dt = None
    on_child_page = False  # read_course_times_from_table 是否成功跳到子页面

    if course_name:
        table_times = read_course_times_from_table(_page, course_name)
        if table_times and table_times.get("start_date"):
            on_child_page = True  # 已成功跳转到扫码考勤子页面
            try:
                from datetime import timedelta
                # 兼容两种格式: "2026-07-26 18:25:00" 和 "2026-07-26 20:00"
                start_str = table_times["start_date"]
                end_str = table_times["end_date"]
                fmt = "%Y-%m-%d %H:%M:%S" if len(start_str) == 19 else "%Y-%m-%d %H:%M"
                start_dt = datetime.strptime(start_str, fmt)
                end_dt_web = datetime.strptime(end_str, fmt)
                # 签退提前15分钟开始
                sign_out_dt = end_dt_web - timedelta(minutes=15)
                sign_in_dt = start_dt
                sign_in_time = start_dt.strftime("%H:%M")
                sign_out_time = sign_out_dt.strftime("%H:%M")
                logger.info(
                    f"✅ 使用网页实际课程时间: "
                    f"签到={sign_in_time}, 签退={sign_out_time}"
                    f"（网页结束={end_dt_web.strftime('%H:%M')}，提前15分钟）"
                )
            except Exception as parse_err:
                logger.warning(f"无法解析表格时间，回退到表单时间: {parse_err}")
                sign_in_dt, sign_out_dt = None, None

        # fallback: 用表单时间
        if sign_in_dt is None or sign_out_dt is None:
            logger.info("使用表单时间")
            h, m = map(int, sign_in_time.split(":"))
            sign_in_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            h, m = map(int, sign_out_time.split(":"))
            sign_out_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    else:
        logger.warning("未指定考勤课程名称")
        return

    # ----- 考勤执行函数 -----
    def do_from_list(mode):
        logger.info(f"执行: {mode}")
        result = start_attendance_flow(
            _page, course_name,
            qr_timeout=qr_timeout,
            resend_count=qr_count,
            wechat_sender=wechat_sender,
            wechat_groups=wechat_groups,
            mode=mode,
            sign_out_time=sign_out_time,
        )
        _log_attendance_result(mode, result)
        return result

    def do_from_child(mode):
        logger.info(f"执行: {mode}")
        result = start_attendance_from_child_page(
            _page, course_name,
            qr_timeout=qr_timeout,
            resend_count=qr_count,
            wechat_sender=wechat_sender,
            wechat_groups=wechat_groups,
            mode=mode,
            sign_out_time=sign_out_time,
        )
        _log_attendance_result(mode, result)
        return result

    # ================================================================
    # 时间判断和流程编排（全部同步，不跨线程操作 Playwright）
    # ================================================================
    # on_child_page: True=已在扫码考勤子页面, False=在列表页需从头开始
    if not on_child_page:
        logger.info("未跳转到子页面，将从列表页开始考勤流程")

    # 辅助：成功后标记状态
    from datetime import date

    def _mark_status(mode_result, status_field):
        """如果考勤有截图产出则标记状态"""
        if mode_result and len(mode_result.get('screenshots', [])) > 0:
            _update_course_status(course_name, status_field, True, date.today().isoformat())

    # 情况1: 已过签退时间 → 立即签退
    if now >= sign_out_dt:
        logger.info(f"当前 {now.strftime('%H:%M')} >= 签退 {sign_out_time}，立即签退")
        WorkflowState.save(_build_attendance_state(data, "sign_out", 0))
        result = do_from_child("签退") if on_child_page else do_from_list("签退")
        _mark_status(result, "sign_out")
        WorkflowState.clear()
        return

    # 情况2: 在签到窗口内 → 立即签到，然后等签退
    if now >= sign_in_dt:
        late = int((now - sign_in_dt).total_seconds() / 60)
        logger.info(f"当前 {now.strftime('%H:%M')} >= 签到 {sign_in_time}（晚{late}分钟），立即签到")
        WorkflowState.save(_build_attendance_state(data, "sign_in", 0))
        result = do_from_child("签到") if on_child_page else do_from_list("签到")
        _mark_status(result, "sign_in")
        _back_to_activity_list(_page)

        # 签到完成，等待签退时间
        WorkflowState.save(_build_attendance_state(data, "waiting_sign_out", 0))
        _sleep_until(sign_out_dt)
        WorkflowState.save(_build_attendance_state(data, "sign_out", 0))
        result = do_from_list("签退")
        _mark_status(result, "sign_out")
        WorkflowState.clear()
        return

    # 情况3: 还没到签到时间 → 返回列表，等到签到时间再做
    _back_to_activity_list(_page)
    _sleep_until(sign_in_dt)
    WorkflowState.save(_build_attendance_state(data, "sign_in", 0))
    result = do_from_list("签到")
    _mark_status(result, "sign_in")

    # 签到完成，等待签退时间
    WorkflowState.save(_build_attendance_state(data, "waiting_sign_out", 0))
    _sleep_until(sign_out_dt)
    WorkflowState.save(_build_attendance_state(data, "sign_out", 0))
    result = do_from_list("签退")
    _mark_status(result, "sign_out")
    WorkflowState.clear()


def _build_attendance_state(data, attendance_phase, qr_index):
    """Build a state dict for attendance checkpoints."""
    return {
        "phase": "attendance",
        "mode": data.get("mode", "full"),
        "current_index": 0,
        "total_courses": 1,
        "completed_course_ids": [],
        "course_config": _get_course_config_snapshot(data),
        "attendance_phase": attendance_phase,
        "qr_screenshot_index": qr_index,
    }


def _sleep_until(target_dt):
    """同步 sleep 到目标时间。

    日志频率根据剩余时间动态调整：
      - 剩余 >  1小时  → 每20分钟 log 一次
      - 剩余 > 15分钟  → 每 5分钟 log 一次
      - 剩余 >  1分钟  → 每 1分钟 log 一次
      - 剩余 <= 1分钟  → 每 1秒   log 一次
    """
    import time
    from datetime import datetime

    def _dynamic_interval(remaining):
        if remaining > 3600:
            return 1200
        elif remaining > 900:
            return 300
        elif remaining > 60:
            return 60
        else:
            return 1

    while True:
        remaining = (target_dt - datetime.now()).total_seconds()
        if remaining <= 0:
            return

        interval = _dynamic_interval(remaining)

        if remaining > 3600:
            logger.info(f"等待至 {target_dt.strftime('%H:%M')}（剩余 {remaining/3600:.1f} 小时）...")
        elif remaining > 60:
            logger.info(f"等待至 {target_dt.strftime('%H:%M')}（剩余 {remaining/60:.0f} 分钟）...")
        else:
            logger.info(f"等待至 {target_dt.strftime('%H:%M')}（剩余 {remaining:.0f} 秒）...")

        time.sleep(min(interval, remaining))


def _refresh_current_tab(page):
    """点击网站顶部'刷新'按钮（layadmin-event=refresh）重载当前 tab 内容。

    2026-08-31: 授分完成后当前 tab 残留弹层/状态，重新点侧边栏只切回旧 tab、iframe
    不刷新（曾卡 networkidle 30s）；JS 关 tab 又会 Frame detached / 浏览器被关闭。
    用户确认：用网站自带的顶部刷新按钮重载当前 tab，干净且无副作用。
    """
    logger.info("点击顶部'刷新'按钮，重载当前 tab...")
    try:
        page.locator('a[layadmin-event="refresh"]').first.click(timeout=5000)
    except Exception:
        try:
            page.evaluate("""
                () => {
                    let btn = document.querySelector('a[layadmin-event="refresh"]');
                    if (btn) { btn.click(); return {ok: true}; }
                    return {ok: false};
                }
            """)
        except Exception as e:
            logger.warning(f"  刷新按钮点击失败: {e}")
    page.wait_for_timeout(1500)
    logger.info("  ✅ 已刷新当前 tab")


def _run_batch_grant(course_names, config, app):
    """后台批量执行授分流程 — 自动启动浏览器+登录"""
    global _session, _page, _verification_event, _verification_code

    logger.info(f"=== 批量授分开始: {len(course_names)} 门课程 ===")

    try:
        # Step 1: 确保浏览器已启动
        if _page is None or _page.is_closed():
            if _page is not None:
                logger.info("浏览器页面已关闭（前次流程结束），重新启动...")
            else:
                logger.info("浏览器未启动，正在启动...")
            from src.browser.session import BrowserSession
            _session = BrowserSession(config)
            _page = _session.start()
            _session.navigate()
            logger.info("浏览器已启动并导航到登录页")
        else:
            logger.info("浏览器已运行，跳过启动")

        # Step 2: 检查登录状态，未登录则走完整登录流程
        from src.browser.auth import check_already_logged_in, login
        if not check_already_logged_in(_page):
            logger.info("未登录，开始登录流程...")

            def verify_callback(prompt_msg, timeout=120):
                global _verification_event, _verification_code
                logger.info(f"VERIFY_NEEDED: {prompt_msg}")
                _verification_event.clear()
                _verification_code = None

                # 启动剪贴板监控（与手动输入并行，互为补充）
                from src.utils.clipboard_monitor import start_clipboard_monitor

                def on_clipboard_code(code):
                    global _verification_code
                    _verification_code = code
                    logger.info(f"VERIFY_AUTO_FILLED: {code}")
                    _verification_event.set()

                stop_monitor = start_clipboard_monitor(on_clipboard_code)

                if _verification_event.wait(timeout=timeout):
                    stop_monitor()
                    logger.info(f"收到验证码: {_verification_code}")
                    return _verification_code
                else:
                    stop_monitor()
                    if timeout < 120:
                        logger.info(f"验证码等待超时（{timeout}秒）")
                    else:
                        logger.error("验证码输入超时（120秒）")
                    return None

            success = login(_page, verify_callback)
            if not success:
                logger.error("登录失败，授分流程终止")
                return
            logger.info("✅ 登录成功")
        else:
            logger.info("已登录，跳过登录流程")

        # Step 3: 导航到科室院内活动
        from src.browser.navigator import goto_dept_activity
        goto_dept_activity(_page)
        logger.info("✅ 已进入科室院内活动页面")

        # Step 4: 逐个授分
        from src.credits.grant import grant_credits
        from datetime import date

        success_list = []
        fail_list = []

        for idx, course_name in enumerate(course_names):
            seq = idx + 1
            total = len(course_names)
            short_name = course_name[:50]
            logger.info(f"\n{'='*50}")
            logger.info(f"[{seq}/{total}] 授分: {short_name}")
            logger.info(f"{'='*50}")

            # 2026-08-31: 授分完成后当前 tab 残留弹层/状态，直接点下一个课程的授分按钮
            # 会退化。每门课之前点网站顶部'刷新'按钮（layadmin-event=refresh）重载当前
            # tab，再继续授分（用户确认的机制）。
            if idx > 0:
                try:
                    _refresh_current_tab(_page)
                except Exception as e:
                    logger.warning(f"刷新当前 tab 失败，尝试继续: {e}")

            try:
                ok = grant_credits(_page, course_name)
                if ok:
                    _update_course_status(course_name, "granted", True,
                                          date.today().isoformat())
                    success_list.append(course_name)
                    logger.info(f"✅ [{seq}/{total}] 授分成功: {short_name}")
                else:
                    fail_list.append(course_name)
                    logger.error(f"❌ [{seq}/{total}] 授分失败: {short_name}")
            except Exception as e:
                fail_list.append(course_name)
                logger.error(f"❌ [{seq}/{total}] 授分异常: {short_name} — {e}")

        # Step 5: 汇总
        logger.info(f"\n=== 批量授分完成 ===")
        logger.info(f"成功: {len(success_list)} / {len(course_names)}")
        if success_list:
            for cn in success_list:
                logger.info(f"  ✅ {cn[:50]}")
        if fail_list:
            for cn in fail_list:
                logger.info(f"  ❌ {cn[:50]}")

    except Exception as e:
        logger.error(f"批量授分流程异常: {e}", exc_info=True)


def _create_verification_callback():
    """构建验证码回调：等待用户/剪贴板输入验证码（与 _run_workflow 同逻辑）。

    Returns:
        callable: verify_callback(prompt_msg, timeout) -> str|None
    """
    def verify_callback(prompt_msg, timeout=120):
        global _verification_event, _verification_code
        logger.info(f"VERIFY_NEEDED: {prompt_msg}")
        _verification_event.clear()
        _verification_code = None

        # 启动剪贴板监控（与手动输入并行，互为补充）
        from src.utils.clipboard_monitor import start_clipboard_monitor

        def on_clipboard_code(code):
            global _verification_code
            _verification_code = code
            logger.info(f"VERIFY_AUTO_FILLED: {code}")
            _verification_event.set()

        stop_monitor = start_clipboard_monitor(on_clipboard_code)

        if _verification_event.wait(timeout=timeout):
            stop_monitor()
            logger.info(f"收到验证码: {_verification_code}")
            return _verification_code
        else:
            stop_monitor()
            if timeout < 120:
                logger.info(f"验证码等待超时（{timeout}秒）")
            else:
                logger.error("验证码输入超时（120秒）")
            return None

    return verify_callback


def _run_sync(config, app):
    """后台执行课程状态同步 — 复用浏览器/登录/导航引导"""
    global _session, _page

    logger.info("=== 手动课程状态同步开始 ===")

    try:
        # Step 1: 确保浏览器已启动
        if _page is None or _page.is_closed():
            if _page is not None:
                logger.info("浏览器页面已关闭（前次流程结束），重新启动...")
            else:
                logger.info("浏览器未启动，正在启动...")
            from src.browser.session import BrowserSession
            _session = BrowserSession(config)
            _page = _session.start()
            _session.navigate()
            logger.info("浏览器已启动并导航到登录页")
        else:
            logger.info("浏览器已运行，跳过启动")

        # Step 2: 检查登录状态，未登录则走完整登录流程
        from src.browser.auth import check_already_logged_in, login
        if not check_already_logged_in(_page):
            logger.info("未登录，开始登录流程...")
            verify_callback = _create_verification_callback()
            success = login(_page, verify_callback)
            if not success:
                logger.error("登录失败，同步终止")
                return
            logger.info("✅ 登录成功")
        else:
            logger.info("已登录，跳过登录流程")

        # Step 3: 导航到科室院内活动
        from src.browser.navigator import goto_dept_activity
        goto_dept_activity(_page)
        logger.info("✅ 已进入科室院内活动页面")

        # Step 4: 同步
        from src.attendance.sync_courses import sync_courses_from_website
        sync_courses_from_website(_page)
        logger.info("=== 手动课程状态同步完成 ===")

    except Exception as e:
        logger.error(f"课程状态同步异常: {e}", exc_info=True)


def _log_attendance_result(mode, result):
    """记录考勤结果"""
    if result:
        screenshots = result.get('screenshots', [])
        logger.info(f"=== {mode}考勤结果 ===")
        logger.info(f"  截图数: {len(screenshots)}")
        for s in screenshots:
            logger.info(f"     {s}")


def _save_auto_start_config(data, existing_config):
    """保存 auto_start 配置到 config.yaml 并管理 Windows 计划任务"""
    import yaml
    import subprocess
    import os as _os

    enabled = data.get("enabled", False)
    time_val = data.get("time", "19:20")
    day_mode = data.get("day_mode", "every_day")
    lock_password = data.get("lock_password", "")
    auto_confirm_delay = data.get("auto_confirm_delay", 3)

    # 更新内存中的 config
    if "auto_start" not in existing_config:
        existing_config["auto_start"] = {}
    existing_config["auto_start"]["enabled"] = enabled
    existing_config["auto_start"]["time"] = time_val
    existing_config["auto_start"]["day_mode"] = day_mode
    if lock_password:
        existing_config["auto_start"]["lock_password"] = lock_password
    existing_config["auto_start"]["auto_confirm_delay"] = auto_confirm_delay

    # 写回 config.yaml
    config_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "config.yaml")
    tmp_path = config_path + ".tmp"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if "auto_start" not in raw:
            raw["auto_start"] = {}
        raw["auto_start"] = existing_config["auto_start"]
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        _os.replace(tmp_path, config_path)
        logger.info(f"auto_start 配置已保存: enabled={enabled}, time={time_val}, day_mode={day_mode}")
    except Exception as e:
        logger.error(f"保存 auto_start 配置失败: {e}")
        return {"status": "error", "message": str(e)}

    # 管理 Windows 计划任务
    task_result = _manage_scheduled_task(enabled, time_val, day_mode)
    return {"status": "ok", "scheduled_task": task_result}


def _save_account_settings(data, existing_config):
    """保存登录账号与教师信息到 config.yaml（原子写回，与 _save_auto_start_config 同模式）。

    立即更新内存中的 config，对运行中的流程生效；下次登录/建课即用新值。
    """
    import yaml
    import os as _os

    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    recipient = (data.get("verification_recipient") or "").strip()
    wechat_path = (data.get("wechat_exe_path") or "").strip()
    qr_save_dir = (data.get("qr_save_dir") or "").strip()
    try:
        idle_minutes = float(data.get("idle_exit_minutes"))
        if idle_minutes < 0:
            idle_minutes = 0.0
    except (TypeError, ValueError):
        idle_minutes = None  # 非数字/缺失则保留原值

    # 教师名单：优先取 teachers 列表，兼容旧前端的单个 teacher_name/teacher_id
    raw_teachers = data.get("teachers")
    if not isinstance(raw_teachers, list):
        raw_teachers = [{
            "name": data.get("teacher_name") or "",
            "id_number": data.get("teacher_id") or "",
        }]
    teachers = []
    for i, t in enumerate(raw_teachers, 1):
        if not isinstance(t, dict):
            continue
        t_name = str(t.get("name") or "").strip()
        t_id = str(t.get("id_number") or "").strip()
        if not t_name and not t_id:
            continue  # 整行空白 → 丢弃（界面里留空行是常事）
        if not t_name:
            return {"status": "error", "message": f"第 {i} 位教师姓名不能为空"}
        if not t_id:
            return {"status": "error", "message": f"第 {i} 位教师（{t_name}）身份证号不能为空（用于授分）"}
        if len(t_id) != 18:
            return {"status": "error", "message": f"第 {i} 位教师（{t_name}）身份证号应为 18 位"}
        teachers.append({"name": t_name, "id_number": t_id})

    if not username:
        return {"status": "error", "message": "登录账号不能为空"}
    if not teachers:
        return {"status": "error", "message": "至少需要一位授课教师"}
    teacher_name = teachers[0]["name"]

    # 更新内存中的 config
    if "login" not in existing_config:
        existing_config["login"] = {}
    existing_config["login"]["username"] = username
    existing_config["login"]["password"] = password
    existing_config["login"]["verification_recipient"] = recipient
    if "course" not in existing_config:
        existing_config["course"] = {}
    existing_config["course"]["teachers"] = teachers
    existing_config["course"]["teacher"] = teachers[0]
    if "wechat" not in existing_config:
        existing_config["wechat"] = {}
    existing_config["wechat"]["exe_path"] = wechat_path
    if "attendance" not in existing_config:
        existing_config["attendance"] = {}
    existing_config["attendance"]["qr_save_dir"] = qr_save_dir
    if "auto_start" not in existing_config:
        existing_config["auto_start"] = {}
    if idle_minutes is not None:
        existing_config["auto_start"]["idle_exit_minutes"] = idle_minutes

    # 原子写回 config.yaml
    config_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "config.yaml")
    tmp_path = config_path + ".tmp"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        raw["login"] = existing_config["login"]
        raw["course"] = existing_config["course"]
        raw["wechat"] = existing_config["wechat"]
        raw["attendance"] = existing_config["attendance"]
        raw["auto_start"] = existing_config["auto_start"]
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        _os.replace(tmp_path, config_path)
        logger.info(f"设置已保存: 账号={username}, 教师={teacher_name}, 微信路径={wechat_path or '(未填)'}, 空闲退出={idle_minutes if idle_minutes is not None else '(保留)'}分钟, 截图目录={qr_save_dir or '(默认)'}")
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"保存账号/教师设置失败: {e}")
        return {"status": "error", "message": str(e)}


def _manage_scheduled_task(enabled, time_str, day_mode):
    """创建或删除 Windows 计划任务 CME_AutoStart

    每次调用都会先清除旧的同名任务，确保只有一个最新配置的任务。
    """
    import subprocess
    import os as _os

    task_name = "CME_AutoStart"
    script_dir = _os.path.dirname(_os.path.abspath(__file__))
    project_root = _os.path.dirname(script_dir)
    auto_start_py = _os.path.join(script_dir, "auto_start.py")

    # 使用当前 Python 解释器（确保运行环境和当前一致）
    python_exe = sys.executable
    logger.info(f"计划任务将使用 Python: {python_exe}")

    # ---- Step 1: 始终先删除旧任务，确保干净的起始状态 ----
    try:
        r = subprocess.run(
            f'schtasks /delete /tn "{task_name}" /f',
            shell=True, capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            logger.info("已清除旧的 CME_AutoStart 计划任务")
        elif "系统找不到指定的文件" not in (r.stderr or "") and "ERROR" not in (r.stderr or ""):
            pass  # 任务不存在，无需处理
    except Exception as e:
        logger.warning(f"清除旧任务时出错（可忽略）: {e}")

    # ---- Step 2: 如果 disabled，删完就结束 ----
    if not enabled:
        logger.info("自动启动已禁用，任务已删除")
        return {"action": "deleted"}

    # ---- Step 3: 日期模式映射 ----
    day_map = {
        "every_day": "MON,TUE,WED,THU,FRI,SAT,SUN",
        "workdays": "MON,TUE,WED,THU,FRI",
        "mon_fri": "MON,TUE,WED,THU,FRI",
        "weekends": "SAT,SUN",
    }
    days = day_map.get(day_mode, "MON,TUE,WED,THU,FRI,SAT,SUN")

    # ---- Step 4: 写入 launcher.bat（动态生成，确保 Python 路径正确）----
    launcher_bat = _os.path.join(project_root, "auto_start_launcher.bat")
    bat_content = (
        f'@echo off\r\n'
        f'cd /d "{project_root}"\r\n'
        f'"{python_exe}" "{auto_start_py}"\r\n'
    )
    try:
        with open(launcher_bat, "w", encoding="ascii") as f:
            f.write(bat_content)
        logger.info(f"已写入 launcher: {launcher_bat}")
    except Exception as e:
        logger.error(f"写入 launcher.bat 失败: {e}")
        return {"action": "launcher_error", "output": str(e)}

    # ---- Step 5: 创建任务 (指向 launcher.bat，避免 schtasks 的 cmd /c 引号转义问题) ----
    # /sc WEEKLY + /d 实现按星期循环
    # /it 仅当用户登录时运行 → 有桌面会话才能操作浏览器和微信
    cmd = (
        f'schtasks /create /tn "{task_name}" /tr "{launcher_bat}" '
        f'/sc WEEKLY /d {days} /st {time_str} /it /f'
    )
    try:
        logger.info(f"创建计划任务: /tn {task_name} /st {time_str} /d {days}")
        logger.info(f"  启动器: {launcher_bat}")
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            logger.info(f"✅ CME_AutoStart 计划任务已创建: 每天 {time_str} ({day_mode})")
            return {"action": "created", "time": time_str, "days": days, "output": r.stdout[:200]}
        else:
            logger.error(f"创建计划任务失败: {r.stderr or r.stdout}")
            return {"action": "create_failed", "output": (r.stderr or r.stdout)[:200]}
    except Exception as e:
        logger.error(f"创建计划任务异常: {e}")
        return {"action": "create_error", "output": str(e)}


def _idle_exit_monitor(config):
    """空闲自动退出监控（仅自动模式启动）。

    自动任务结束后，若持续 N 分钟无 Web 界面操作且无活动任务，则自动退出程序。
    - 有活动任务（工作流线程运行中）→ 重置计时
    - 无活动任务 → 从任务结束时刻起计时
    - 任一 Web 操作（非 SSE 请求）→ 更新 _last_activity，延长"无操作"窗口
    退出条件：无活动任务 且 任务已结束满 N 分钟 且 N 分钟无 Web 操作。
    """
    global _tasks_empty_since
    ac = config.get("auto_start", {})
    idle_minutes = float(ac.get("idle_exit_minutes", 10) or 0)
    interval = float(ac.get("idle_check_interval", 30) or 30)
    if idle_minutes <= 0:
        logger.info("[自动退出] idle_exit_minutes <= 0，已禁用")
        return
    target = idle_minutes * 60
    logger.info(
        f"[自动退出] 已启用：自动任务结束后无 Web 操作且无活动任务，"
        f"{idle_minutes:.0f} 分钟自动退出（检查间隔 {interval:.0f}s）"
    )

    _warned = False
    while True:
        time.sleep(interval)
        with _active_lock:
            has_active = len(_active_threads) > 0
        if has_active:
            # 有任务在跑，不进入/清零空闲计时
            _tasks_empty_since = None
            _warned = False
            continue

        now = time.time()
        if _tasks_empty_since is None:
            _tasks_empty_since = now  # 任务刚结束，开始计时
            continue

        remaining = target - min(now - _tasks_empty_since, now - _last_activity)
        if remaining <= 0:
            logger.info(
                f"[自动退出] 自动任务已结束 {idle_minutes:.0f} 分钟，"
                f"期间无 Web 操作且无活动任务，程序自动退出"
            )
            os._exit(0)
        if remaining <= 60 and not _warned:
            logger.warning(
                f"[自动退出] 无活动任务且无 Web 操作，将在约 {remaining:.0f} 秒后自动退出"
            )
            _warned = True


if __name__ == "__main__":
    app, _ = create_app()
    print("\n" + "=" * 60)
    print("  继续教育自动化工具")
    print("  打开浏览器访问: http://localhost:5000")
    print("=" * 60 + "\n")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
