// 继续教育自动化工具 — 前端交互

const state = {
    running: false,
    verificationCallback: null,
};

let appConfig = null;  // /api/config 缓存（排课预览用默认考勤时间）

// DOM 元素
const els = {
    btnStart: document.getElementById('btn-start'),
    btnCourseOnly: document.getElementById('btn-course-only'),
    btnAttendOnly: document.getElementById('btn-attend-only'),
    logContent: document.getElementById('log-content'),
    logPanel: document.getElementById('log-panel'),
    statusIndicator: document.getElementById('status-indicator'),
    loginStatus: document.getElementById('login-status'),
    // Verification modal
    modalVerify: document.getElementById('modal-verify'),
    verifyCodeInput: document.getElementById('verify-code-input'),
    btnVerifySubmit: document.getElementById('btn-verify-submit'),
    btnVerifyCancel: document.getElementById('btn-verify-cancel'),
};

// ===== 日志 =====
let logCount = 0;

function addLog(message, level) {
    const div = document.createElement('div');
    div.className = 'log-line' + (level ? ' log-' + level : '');
    div.textContent = message;
    els.logContent.appendChild(div);
    els.logPanel.scrollTop = els.logPanel.scrollHeight;
    logCount++;
    // 限制日志行数
    if (logCount > 500) {
        els.logContent.firstChild.remove();
        logCount--;
    }
}

// ===== SSE 日志流 =====
function connectLogStream() {
    const es = new EventSource('/api/logs/stream');
    es.onmessage = function(event) {
        const msg = event.data;
        let level = '';
        if (msg.includes('[ERROR]') || msg.includes('失败')) level = 'error';
        else if (msg.includes('[WARNING]') || msg.includes('⚠')) level = 'warn';
        else if (msg.includes('✅') || msg.includes('成功') || msg.includes('完成')) level = 'success';
        // TASK_DONE 是纯内部结束标记，只触发解锁逻辑，不直接显示（下面会有友好提示）
        if (msg.trim() !== 'TASK_DONE') addLog(msg, level);

        // 检测验证码请求 → 弹出验证码输入框
        if (msg.includes('VERIFY_NEEDED')) {
            els.modalVerify.classList.add('show');
            els.verifyCodeInput.value = '';
            els.verifyCodeInput.focus();
            addLog('[系统] 📱 请输入手机验证码（复制短信自动识别并提交，也可手动输入）', 'warn');
        }

        // 检测剪贴板自动填入 → 后端已自动接收验证码，前端填入输入框并关闭弹窗
        if (msg.includes('VERIFY_AUTO_FILLED')) {
            const match = msg.match(/VERIFY_AUTO_FILLED:\s*(\d+)/);
            const code = match ? match[1] : '';
            if (code) {
                els.verifyCodeInput.value = code;
                addLog(`[系统] 📋 已从剪贴板自动识别验证码: ${code}`, 'success');
                els.modalVerify.classList.remove('show');
            }
        }

        // 课程状态同步完成 → 刷新课程/考勤/授分列表
        if (msg.includes('SYNC_DONE')) {
            setTimeout(loadCourseList, 1000);
        }

        // 任一后台任务真正结束（后端在 _spawn_workflow_thread 统一发出）→ 解锁按钮，
        // 回到"空闲"，可立即开始下一个操作（解决任务结束后按钮一直被禁用的痛点）
        if (msg.includes('TASK_DONE')) {
            setTimeout(() => {
                setButtonsState(false);
                addLog('[系统] ✅ 当前任务已完成，按钮已解锁，可开始下一个操作', 'success');
                loadCourseList();  // 刷新列表，让已创建/已考勤状态落地
            }, 800);
        }
    };
    es.onerror = function() {
        addLog('[系统] 日志连接断开，5秒后重连...', 'warn');
        setTimeout(connectLogStream, 5000);
    };
}

// ===== API =====
async function apiCall(endpoint, data) {
    const resp = await fetch('/api/' + endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    });
    return resp.json();
}

// ===== 课程列表加载 =====
let coursesData = null;
let allCourses = [];  // flat list of {id, course_name, note, status, category}

async function loadCourseList() {
    try {
        const resp = await fetch('/api/courses');
        coursesData = await resp.json();
        if (coursesData.error) {
            addLog('[系统] 课程列表加载失败: ' + coursesData.error, 'warn');
            return;
        }
        // Build flat course list
        allCourses = [];
        coursesData.categories.forEach(cat => {
            cat.courses.forEach(c => {
                allCourses.push({
                    id: c.id,
                    course_name: c.course_name,
                    note: c.note || '',
                    status: c.status || {},
                    category: cat.name,
                });
            });
        });
        renderCourseList();
        addLog(`[系统] 已加载 ${allCourses.length} 个课程`);
    } catch (e) {
        addLog('[系统] 课程列表加载失败', 'warn');
    }
}

function renderCourseList() {
    const listDiv = document.getElementById('course-checkboxes');
    listDiv.innerHTML = '';
    allCourses.forEach(c => {
        const isCreated = c.status.created;
        const div = document.createElement('div');
        div.className = 'course-checkbox-item';
        div.onclick = function(e) {
            if (e.target.tagName === 'INPUT') return; // let checkbox handle itself
            const cb = this.querySelector('input[type="checkbox"]');
            if (cb && !cb.disabled) { cb.checked = !cb.checked; onCourseCheck(); }
        };
        div.innerHTML = `
            <input type="checkbox" id="course-cb-${c.id}" value="${c.id}"
                   onchange="onCourseCheck()" ${isCreated ? 'disabled' : ''}>
            <span class="course-name">#${c.id} ${c.course_name}</span>
            ${isCreated ? `<span class="course-status created">已创建 ${c.status.created_date||''}</span>` : ''}
            ${c.note ? `<span class="course-status" style="color:var(--amber);">${c.note}</span>` : ''}
        `;
        listDiv.appendChild(div);
    });
    updateModeUI();
    refreshAttendanceList();
    refreshGrantList();
}

// ===== 考勤课程列表（单选，仅今日课程） =====
let selectedAttendanceCourse = '';

// 本地日期 "YYYY-MM-DD"（不用 toISOString，避免 UTC 偏差）
function todayStr() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function refreshAttendanceList() {
    const listDiv = document.getElementById('attendance-course-list');
    const today = todayStr();
    // 仅显示今日计划举办的课程（created_date = 计划举办日期）
    const created = allCourses.filter(c => c.status.created && c.status.created_date === today);
    listDiv.innerHTML = '';

    if (created.length === 0) {
        listDiv.innerHTML = '<div style="padding:8px;color:var(--text-2);">-- 今日暂无已创建的课程 --</div>';
        selectedAttendanceCourse = '';
        return;
    }

    created.forEach(c => {
        const isCompleted = c.status.sign_in && c.status.sign_out;

        const div = document.createElement('div');
        div.className = 'course-checkbox-item';
        if (isCompleted) {
            div.classList.add('attendance-completed');
        }
        if (c.course_name === selectedAttendanceCourse && !isCompleted) {
            div.classList.add('attendance-selected');
        }

        const radioIcon = document.createElement('span');
        radioIcon.className = 'attendance-radio';
        radioIcon.style.cssText = 'font-size:18px;margin-right:6px;';
        if (isCompleted) {
            radioIcon.textContent = '✅';
            radioIcon.style.opacity = '0.5';
        } else {
            radioIcon.textContent = c.course_name === selectedAttendanceCourse ? '🔘' : '⚪';
        }

        div.onclick = function() {
            if (isCompleted) return;  // 已完成不可选
            // 单选：取消其他选中
            listDiv.querySelectorAll('.course-checkbox-item:not(.attendance-completed)').forEach(el => {
                el.classList.remove('attendance-selected');
                const icon = el.querySelector('.attendance-radio');
                if (icon) icon.textContent = '⚪';
            });
            this.classList.add('attendance-selected');
            const thisIcon = this.querySelector('.attendance-radio');
            if (thisIcon) thisIcon.textContent = '🔘';
            selectedAttendanceCourse = c.course_name;
        };

        const nameSpan = document.createElement('span');
        nameSpan.className = 'course-name';
        nameSpan.textContent = `#${c.id} ${c.course_name}`;

        div.appendChild(radioIcon);
        div.appendChild(nameSpan);

        // 状态标签
        if (c.status.granted) {
            const statusSpan = document.createElement('span');
            statusSpan.className = 'course-status';
            statusSpan.style.color = 'var(--teal)';
            statusSpan.textContent = '✅ 已授分';
            div.appendChild(statusSpan);
        } else if (isCompleted) {
            const statusSpan = document.createElement('span');
            statusSpan.className = 'course-status';
            statusSpan.style.color = 'var(--amber)';
            statusSpan.textContent = '⏳ 待授分';
            div.appendChild(statusSpan);
        } else if (c.status.sign_in) {
            const statusSpan = document.createElement('span');
            statusSpan.className = 'course-status';
            statusSpan.style.color = 'var(--blue)';
            statusSpan.textContent = '已签到，待签退';
            div.appendChild(statusSpan);
        } else if (c.status.created_date) {
            const statusSpan = document.createElement('span');
            statusSpan.className = 'course-status created';
            statusSpan.textContent = c.status.created_date;
            div.appendChild(statusSpan);
        }
        listDiv.appendChild(div);
    });
}

// ===== 全选/取消 =====
function toggleAllCourses(select) {
    const listDiv = document.getElementById('course-checkboxes');
    const cbs = listDiv.querySelectorAll('input[type="checkbox"]:not([disabled])');
    cbs.forEach(cb => { cb.checked = select; });
    onCourseCheck();
}

// ===== 课程勾选 → 更新模式 =====
function onCourseCheck() {
    const listDiv = document.getElementById('course-checkboxes');
    const checked = listDiv.querySelectorAll('input[type="checkbox"]:checked');
    const count = checked.length;
    document.getElementById('course-count').textContent = `已选 ${count} 个`;
    updateModeUI();
}

// ===== 根据课程数切换单/多课程模式 =====
function updateModeUI() {
    const listDiv = document.getElementById('course-checkboxes');
    const checked = listDiv.querySelectorAll('input[type="checkbox"]:checked');
    const count = checked.length;

    const singleDateRow = document.getElementById('single-date-row');
    const dateModeRow = document.getElementById('date-mode-row');
    const scheduleRow = document.getElementById('schedule-preview-row');
    const btnStart = document.getElementById('btn-start');

    if (count >= 2) {
        // Multi-course mode
        singleDateRow.style.display = 'none';
        dateModeRow.style.display = 'block';
        scheduleRow.style.display = 'block';
        renderSchedulePreview();
        // 多课程不能使用"创建+考勤"模式
        btnStart.disabled = true;
        btnStart.title = '创建+考勤仅支持单个课程';
    } else {
        // Single course mode (0 or 1)
        singleDateRow.style.display = 'block';
        dateModeRow.style.display = 'none';
        scheduleRow.style.display = 'none';
        // 单个课程或无选择时恢复
        btnStart.disabled = (count === 0);
        btnStart.title = count === 0 ? '请先选择一个课程' : '先创建课程，再自动考勤';
    }
    document.getElementById('course-count').textContent = `已选 ${count} 个`;
}

function getSelectedCourseName(courseId) {
    const course = allCourses.find(c => c.id === courseId);
    return course ? course.course_name : '';
}

// ===== 排课预览 =====
// ===== 排课预览：与后端 _run_course_creation 的日期逻辑保持一致 =====

// 排课起点：今天未到默认考勤时间(attendance.sign_out_time, 默认20:45)则从今天开始，
// 已过则从明天开始（与后端 _run_course_creation 一致：不能创建当天已无法考勤的课程）
function getScheduleStartDay() {
    const today = new Date();
    const nowHm = String(today.getHours()).padStart(2, '0') + ':' + String(today.getMinutes()).padStart(2, '0');
    const attendEnd = (appConfig && appConfig.attendance && appConfig.attendance.sign_out_time) || '20:45';
    if (nowHm < attendEnd) return today;
    const tomorrow = new Date(today);
    tomorrow.setDate(today.getDate() + 1);
    return tomorrow;
}

// 已创建课程占用的日期映射 {日期: [课程名]}（用于冲突顺延预览，显示被谁占用）
function getOccupiedDates() {
    const map = {};
    allCourses.forEach(c => {
        if (c.status.created && c.status.created_date) {
            if (!map[c.status.created_date]) map[c.status.created_date] = [];
            map[c.status.created_date].push(c.course_name);
        }
    });
    return map;
}

function toDateStr(d) {
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function renderSchedulePreview() {
    const listDiv = document.getElementById('course-checkboxes');
    const checked = Array.from(listDiv.querySelectorAll('input[type="checkbox"]:checked'));
    if (checked.length < 2) return;

    const dateMode = document.getElementById('date-mode').value;
    // JS getDay(): 周日=0, 周一=1 ... 周六=6
    const patterns = {
        'mon_fri': [1, 2, 3, 4, 5],
        'workdays': [1, 2, 3, 4, 5],
        'mon_wed_fri': [1, 3, 5],
        'tue_thu_sat': [2, 4, 6],
        'every_day': [0, 1, 2, 3, 4, 5, 6],
        'weekends': [0, 6],
    };
    const pattern = patterns[dateMode] || [1, 2, 3, 4, 5];

    const occupied = getOccupiedDates();
    const WEEKDAY_NAMES = ['日', '一', '二', '三', '四', '五', '六'];

    // 从 start 起找第一个符合工作日模式且未被占用的日期（与后端 _next_free_day 一致）
    function nextFreeDay(start) {
        const d = new Date(start);
        while (true) {
            if (pattern.includes(d.getDay()) && !occupied[toDateStr(d)]) {
                return d;
            }
            d.setDate(d.getDate() + 1);
        }
    }

    // 课程名简化（去括号后缀 + 截断），用于展示"被谁占用"
    function shortName(n) {
        return n.replace(/(（.*?）|\(.*?\))$/, '').substring(0, 14);
    }

    const startDay = getScheduleStartDay();
    let cursor = new Date(startDay);
    let html = '<table>';
    checked.forEach((cb, i) => {
        const d = nextFreeDay(cursor);
        const shifted = toDateStr(d) !== toDateStr(cursor);

        // 收集被跳过的占用日期+课程，解释为什么顺延（如 08-31 被某课程占用）
        let skipInfo = '';
        if (shifted) {
            const skipped = [];
            const t = new Date(cursor);
            while (toDateStr(t) < toDateStr(d)) {
                const ds = toDateStr(t);
                if (occupied[ds]) {
                    skipped.push(`${ds} ${occupied[ds].map(shortName).join('、')}`);
                }
                t.setDate(t.getDate() + 1);
            }
            if (skipped.length) skipInfo = skipped.join('；');
        }
        occupied[toDateStr(d)] = ['本批次'];      // 批次内已排课程也占坑
        cursor = new Date(d);
        cursor.setDate(cursor.getDate() + 1);

        const dateStr = toDateStr(d);
        const courseName = cb.parentElement.querySelector('.course-name').textContent;
        html += `<tr${shifted ? ' class="sched-shifted"' : ''}>
            <td class="sched-weekday">周${WEEKDAY_NAMES[d.getDay()]}</td>
            <td class="sched-date">${dateStr}${shifted ? ' ⏭' : ''}</td>
            <td class="sched-course">${courseName}${skipInfo ? `<br><span class="sched-skip">跳过: ${skipInfo}</span>` : ''}</td>
        </tr>`;
    });
    html += '</table>';
    document.getElementById('schedule-preview').innerHTML = html;
    document.getElementById('schedule-preview-row').style.display = 'block';
}

// ===== 获取已选课程ID列表 =====
function getSelectedCourseIds() {
    const listDiv = document.getElementById('course-checkboxes');
    const checked = listDiv.querySelectorAll('input[type="checkbox"]:checked');
    return Array.from(checked).map(cb => parseInt(cb.value));
}

// ===== 表单数据收集 =====
function getFormData() {
    const selectedIds = getSelectedCourseIds();
    const isMulti = selectedIds.length >= 2;
    return {
        selected_course_ids: selectedIds,
        is_multi_course: isMulti,
        date_mode: document.getElementById('date-mode').value,
        single_date: document.getElementById('single-date').value,
        course_start_time: document.getElementById('course-start').value,
        course_end_time: document.getElementById('course-end').value,
        sign_in_time: document.getElementById('sign-in-time').value,
        sign_out_time: document.getElementById('sign-out-time').value,
        verification_recipient: document.getElementById('verify-recipient').value,
        planned_students: parseInt(document.getElementById('planned-students').value) || 6,
        qr_timeout: parseInt(document.getElementById('qr-timeout').value) || 300,
        qr_resend_count: parseInt(document.getElementById('qr-count').value) || 3,
        wechat_group: getWechatGroup(),
        attendance_course_name: selectedAttendanceCourse,
    };
}

function getWechatGroup() {
    const select = document.getElementById('wechat-group');
    if (select.value) return select.value;
    const custom = document.getElementById('wechat-group-custom');
    return custom ? custom.value.trim() : '';
}

function setButtonsState(disabled) {
    els.btnStart.disabled = disabled;
    els.btnCourseOnly.disabled = disabled;
    els.btnAttendOnly.disabled = disabled;
    state.running = disabled;
    els.statusIndicator.className = disabled ? 'status-on' : 'status-off';
    els.statusIndicator.textContent = disabled ? '● 运行中' : '● 空闲';
}

// ===== 发送验证码 =====
async function submitVerificationCode(code) {
    try {
        const resp = await fetch('/api/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: code }),
        });
        els.modalVerify.classList.remove('show');
        addLog('[系统] 验证码已提交: ' + code);
        return resp.json();
    } catch (e) {
        addLog('[系统] 验证码提交失败: ' + e.message, 'error');
        return { status: 'error' };
    }
}

// ===== 启动工作流 =====
async function startWorkflow(mode) {
    const data = getFormData();

    // 考勤相关模式需要选择考勤课程
    if (mode === 'attend_only') {
        if (!data.attendance_course_name) {
            addLog('[系统] 请先在"考勤课程"下拉中选择一个已创建的课程', 'warn');
            return;
        }
    }

    // full 模式：必须选恰好1个课程，自动作为考勤课程
    if (mode === 'full') {
        if (data.selected_course_ids.length !== 1) {
            addLog('[系统] 创建+考勤模式只能选择1个课程', 'warn');
            return;
        }
        // 用选中的课程名作为考勤课程
        const selectedName = getSelectedCourseName(data.selected_course_ids[0]);
        if (selectedName) {
            data.attendance_course_name = selectedName;
            addLog(`[系统] 考勤课程自动设为: ${selectedName.substring(0, 40)}`);
        }
    }

    // 考勤相关模式需要选择微信群
    if (mode === 'attend_only' || mode === 'full') {
        const wechatGroup = data.wechat_group;
        if (!wechatGroup) {
            addLog('[系统] 签到/签退需要发送二维码到微信群，请先在"选择群聊"中选择或输入群名', 'warn');
            return;
        }
    }

    if (mode !== 'attend_only' && data.selected_course_ids.length === 0) {
        addLog('[系统] 请至少选择一个课程', 'warn');
        return;
    }
    const courseCount = data.selected_course_ids.length;
    addLog(`[系统] 启动工作流: ${mode}, ${courseCount}个课程（每个课程=独立活动）`, 'success');
    setButtonsState(true);

    try {
        data.mode = mode;
        const resp = await fetch('/api/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        const result = await resp.json();
        if (result.status === 'busy') {
            addLog('[系统] ⚠ ' + (result.message || '已有任务正在运行，请稍候'), 'warn');
            return;
        }
        addLog('[系统] 工作流已启动: ' + result.mode);
    } catch (e) {
        addLog('[系统] 启动失败: ' + e.message, 'error');
        setButtonsState(false);
    }
}

// ===== 授分 =====
// 当前选中的待授分课程名称集合
let selectedGrantCourses = new Set();

function refreshGrantList() {
    const listDiv = document.getElementById('grant-course-list');
    // 筛选已考勤（sign_in + sign_out）的课程
    const attended = allCourses.filter(c =>
        c.status.sign_in && c.status.sign_out
    );
    listDiv.innerHTML = '';

    if (attended.length === 0) {
        listDiv.innerHTML = '<div style="padding:8px;color:var(--text-2);">-- 暂无已考勤课程，请先完成考勤 --</div>';
        document.getElementById('btn-grant').disabled = true;
        document.getElementById('btn-grant').textContent = '📋 授分（无待授分课程）';
        document.getElementById('grant-count').textContent = '已选 0 个';
        return;
    }

    selectedGrantCourses.clear();
    let hasPending = false;

    attended.forEach(c => {
        const isGranted = c.status.granted;
        if (!isGranted) hasPending = true;

        const div = document.createElement('div');
        div.className = 'course-checkbox-item';
        if (isGranted) {
            div.classList.add('grant-granted');
        } else {
            div.classList.add('grant-eligible');
        }

        // Checkbox（已授分不可选）
        if (!isGranted) {
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.id = 'grant-cb-' + c.id;
            cb.value = c.course_name;
            cb.addEventListener('change', onGrantCheck);
            div.appendChild(cb);

            div.addEventListener('click', function(e) {
                if (e.target.tagName === 'INPUT') return;
                cb.checked = !cb.checked;
                onGrantCheck();
            });
        }

        // 课程名称
        const nameSpan = document.createElement('span');
        nameSpan.className = 'course-name';
        nameSpan.textContent = `#${c.id} ${c.course_name}`;
        div.appendChild(nameSpan);

        // 状态标签
        const statusSpan = document.createElement('span');
        statusSpan.className = 'grant-status';
        if (isGranted) {
            statusSpan.classList.add('granted');
            statusSpan.textContent = '✅ 已授分';
        } else {
            statusSpan.classList.add('pending');
            statusSpan.textContent = '⏳ 待授分';
        }
        div.appendChild(statusSpan);

        listDiv.appendChild(div);
    });

    // 初始按钮状态
    if (!hasPending) {
        document.getElementById('btn-grant').disabled = true;
        document.getElementById('btn-grant').textContent = '📋 授分（全部已授分）';
    } else {
        document.getElementById('btn-grant').disabled = true;
        document.getElementById('btn-grant').textContent = '📋 授分（请先选择课程）';
    }
    document.getElementById('grant-count').textContent = '已选 0 个';
}

function onGrantCheck() {
    const listDiv = document.getElementById('grant-course-list');
    const checked = listDiv.querySelectorAll('input[type="checkbox"]:checked');
    const count = checked.length;

    selectedGrantCourses.clear();
    checked.forEach(cb => selectedGrantCourses.add(cb.value));

    document.getElementById('grant-count').textContent = `已选 ${count} 个`;

    const btnGrant = document.getElementById('btn-grant');
    if (count > 0) {
        btnGrant.disabled = false;
        btnGrant.textContent = `📋 授分（${count} 门课程）`;
        btnGrant.title = '';
    } else {
        btnGrant.disabled = true;
        btnGrant.textContent = '📋 授分（请先选择课程）';
        btnGrant.title = '请先选择待授分课程';
    }
}

function toggleGrantAll(select) {
    const listDiv = document.getElementById('grant-course-list');
    const cbs = listDiv.querySelectorAll('input[type="checkbox"]:not([disabled])');
    cbs.forEach(cb => { cb.checked = select; });
    onGrantCheck();
}

async function startGrantCredits() {
    if (selectedGrantCourses.size === 0) {
        addLog('[系统] 请先选择待授分课程', 'warn');
        return;
    }

    const courseNames = Array.from(selectedGrantCourses);
    addLog(`[系统] 批量授分: ${courseNames.length} 门课程`, 'success');
    courseNames.forEach((cn, i) => {
        addLog(`  ${i+1}. ${cn.substring(0, 50)}`);
    });

    setButtonsState(true);

    try {
        const resp = await fetch('/api/grant', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ course_names: courseNames }),
        });
        const result = await resp.json();
        if (result.status === 'busy') {
            addLog('[系统] ⚠ ' + (result.message || '已有任务正在运行，请稍候'), 'warn');
            return;
        }
        if (result.status === 'started') {
            addLog(`[系统] 授分流程已启动（共 ${result.count} 门课程）`, 'success');
        } else {
            addLog('[系统] 授分启动失败: ' + (result.message || '未知错误'), 'error');
            setButtonsState(false);
        }
    } catch (e) {
        addLog('[系统] 授分请求失败: ' + e.message, 'error');
        setButtonsState(false);
    }
}

// ===== 事件处理 =====
els.btnStart.addEventListener('click', () => startWorkflow('full'));
els.btnCourseOnly.addEventListener('click', () => startWorkflow('course_only'));
els.btnAttendOnly.addEventListener('click', () => startWorkflow('attend_only'));
document.getElementById('btn-grant').addEventListener('click', () => startGrantCredits());

// ===== 手动同步课程状态 =====
const btnSync = document.getElementById('btn-sync');
if (btnSync) {
    btnSync.addEventListener('click', async () => {
        addLog('[系统] 正在同步课程状态（读取网页举办/授分状态，更新 courses.yaml）...');
        try {
            const resp = await fetch('/api/sync', { method: 'POST' });
            const result = await resp.json();
            if (result.status === 'busy') {
                addLog('[系统] ⚠ ' + (result.message || '已有任务正在运行，请稍候'), 'warn');
            } else if (result.status === 'started') {
                addLog('[系统] 课程状态同步已启动，完成后列表将自动刷新', 'success');
            } else {
                addLog('[系统] 同步启动失败: ' + (result.message || ''), 'error');
            }
        } catch (e) {
            addLog('[系统] 同步请求失败: ' + e.message, 'error');
        }
    });
}

// ===== 验证码弹窗 =====
els.btnVerifySubmit.addEventListener('click', async () => {
    const code = els.verifyCodeInput.value.trim();
    if (!code) return;
    els.btnVerifySubmit.disabled = true;
    await submitVerificationCode(code);
    els.btnVerifySubmit.disabled = false;
});

els.btnVerifyCancel.addEventListener('click', () => {
    els.modalVerify.classList.remove('show');
    addLog('[系统] 用户取消了验证码输入', 'warn');
});

els.verifyCodeInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') els.btnVerifySubmit.click();
});

// ===== 接收人管理 =====
const RECIPIENT_STORAGE_KEY = 'cme_recipients';
const DEFAULT_RECIPIENTS = [];

function getRecipients() {
    try {
        const saved = localStorage.getItem(RECIPIENT_STORAGE_KEY);
        return saved ? JSON.parse(saved) : [...DEFAULT_RECIPIENTS];
    } catch {
        return [...DEFAULT_RECIPIENTS];
    }
}

function saveRecipients(list) {
    localStorage.setItem(RECIPIENT_STORAGE_KEY, JSON.stringify(list));
}

function refreshRecipientDropdown() {
    const recipients = getRecipients();
    const select = document.getElementById('verify-recipient');
    select.innerHTML = '';
    if (recipients.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '请先添加接收人';
        select.appendChild(opt);
        return;
    }
    recipients.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        select.appendChild(opt);
    });
}

// Add recipient button
document.getElementById('btn-add-recipient').addEventListener('click', () => {
    document.getElementById('modal-add-recipient').classList.add('show');
    document.getElementById('new-recipient-name').focus();
    document.getElementById('new-recipient-name').value = '';
});

// Save new recipient
document.getElementById('btn-save-recipient').addEventListener('click', () => {
    const name = document.getElementById('new-recipient-name').value.trim();
    if (!name) return;
    const recipients = getRecipients();
    if (!recipients.includes(name)) {
        recipients.push(name);
        saveRecipients(recipients);
        refreshRecipientDropdown();
        // Select the newly added
        document.getElementById('verify-recipient').value = name;
        addLog('[系统] 已添加接收人: ' + name);
    }
    document.getElementById('modal-add-recipient').classList.remove('show');
});

// Cancel
document.getElementById('btn-cancel-recipient').addEventListener('click', () => {
    document.getElementById('modal-add-recipient').classList.remove('show');
});

// Close modal on overlay click
document.getElementById('modal-add-recipient').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) {
        e.currentTarget.classList.remove('show');
    }
});

// Enter key in modal
document.getElementById('new-recipient-name').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('btn-save-recipient').click();
});

// ===== 微信聊天列表管理 =====
const CHAT_STORAGE_KEY = 'cme_wechat_chats';

const PRESET_CHATS = ['继续教育签到', '文件传输助手'];

function getSavedChats() {
    try {
        const saved = JSON.parse(localStorage.getItem(CHAT_STORAGE_KEY)) || [];
        // Merge presets at the top
        const merged = [...PRESET_CHATS];
        saved.forEach(c => { if (!merged.includes(c)) merged.push(c); });
        return merged;
    } catch { return [...PRESET_CHATS]; }
}

function saveChats(list) {
    localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(list));
}

function refreshChatDropdown() {
    const chats = getSavedChats();
    const select = document.getElementById('wechat-group');
    const currentVal = select.value;
    select.innerHTML = '<option value="">-- 选择群聊 --</option>';
    chats.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        select.appendChild(opt);
    });
    if (currentVal && chats.includes(currentVal)) {
        select.value = currentVal;
    }
}

function addChatToList(name) {
    if (!name) return;
    const chats = getSavedChats();
    if (!chats.includes(name)) {
        chats.unshift(name);
        if (chats.length > 20) chats.pop();
        saveChats(chats);
        refreshChatDropdown();
        document.getElementById('wechat-group').value = name;
        addLog('[系统] 已添加微信聊天: ' + name);
    }
}

// 刷新按钮 — 尝试从 API 获取聊天列表
document.getElementById('btn-refresh-chats').addEventListener('click', async () => {
    addLog('[系统] 正在获取微信聊天列表...');
    try {
        const resp = await fetch('/api/wechat/chats');
        const data = await resp.json();
        if (data.chats && data.chats.length > 0) {
            data.chats.forEach(c => addChatToList(c));
            addLog(`[系统] 已同步 ${data.chats.length} 个聊天窗口`);
        } else {
            addLog('[系统] 未检测到微信窗口，请确保微信已打开，或手动输入群名');
        }
    } catch (e) {
        addLog('[系统] 获取聊天列表失败，请手动输入群名', 'warn');
    }
});

// 保存自定义群名
document.getElementById('btn-save-chat').addEventListener('click', () => {
    const name = document.getElementById('wechat-group-custom').value.trim();
    if (name) {
        addChatToList(name);
        document.getElementById('wechat-group-custom').value = '';
    }
});

// 选择下拉后同步到全局
document.getElementById('wechat-group').addEventListener('change', function() {
    if (!this.value) {
        document.getElementById('wechat-group-custom').focus();
    }
});

// ===== 初始化 =====
// ===== 课程时间 → 签到签退时间同步 =====
function calcSignOutTime(courseEndTime) {
    // 签退时间 = 课程结束时间 - 15分钟
    if (!courseEndTime) return '';
    const [h, m] = courseEndTime.split(':').map(Number);
    let totalMin = h * 60 + m - 15;
    if (totalMin < 0) totalMin += 24 * 60;
    const newH = Math.floor(totalMin / 60);
    const newM = totalMin % 60;
    return String(newH).padStart(2, '0') + ':' + String(newM).padStart(2, '0');
}

document.getElementById('course-start').addEventListener('input', function() {
    document.getElementById('sign-in-time').value = this.value;
    // 课程结束时间 = 开始时间 + 90分钟
    if (this.value) {
        const [h, m] = this.value.split(':').map(Number);
        const totalMin = h * 60 + m + 90;
        const endH = Math.floor(totalMin / 60) % 24;
        const endM = totalMin % 60;
        const endTime = String(endH).padStart(2, '0') + ':' + String(endM).padStart(2, '0');
        document.getElementById('course-end').value = endTime;
        document.getElementById('sign-out-time').value = calcSignOutTime(endTime);
    }
});
document.getElementById('course-end').addEventListener('input', function() {
    document.getElementById('sign-out-time').value = calcSignOutTime(this.value);
});

async function checkResumeState() {
    try {
        const resp = await fetch('/api/state');
        const data = await resp.json();
        if (data.has_incomplete && data.state) {
            const st = data.state;
            const time = st.last_updated ? new Date(st.last_updated).toLocaleString('zh-CN') : '未知时间';
            const phaseMap = {
                'course_creation': '课程创建',
                'attendance': '考勤',
                'done': '已完成',
            };
            const phaseName = phaseMap[st.phase] || st.phase;
            document.getElementById('resume-time').textContent =
                `阶段: ${phaseName}, 进度: ${st.current_index || 0}/${st.total_courses || '?'}, 最后更新: ${time}`;
            document.getElementById('resume-banner').style.display = 'flex';
            addLog(`[系统] 检测到未完成的工作流（${phaseName}），可选择恢复或放弃`, 'warn');
        }
    } catch (e) {
        // 静默处理
    }
}

// Resume button
document.getElementById('btn-resume').addEventListener('click', async () => {
    document.getElementById('resume-banner').style.display = 'none';
    addLog('[系统] 正在恢复工作流...', 'success');
    setButtonsState(true);
    try {
        const resp = await fetch('/api/resume', { method: 'POST' });
        const result = await resp.json();
        if (result.status === 'busy') {
            addLog('[系统] ⚠ ' + (result.message || '已有任务正在运行，请稍候'), 'warn');
            return;
        }
        if (result.status === 'resumed') {
            addLog('[系统] 工作流已恢复: ' + result.phase);
        } else {
            addLog('[系统] 恢复失败: ' + (result.message || '未知错误'), 'error');
            setButtonsState(false);
        }
    } catch (e) {
        addLog('[系统] 恢复请求失败: ' + e.message, 'error');
        setButtonsState(false);
    }
});

// Discard button
document.getElementById('btn-discard').addEventListener('click', async () => {
    document.getElementById('resume-banner').style.display = 'none';
    try {
        await fetch('/api/state/clear', { method: 'POST' });
        addLog('[系统] 已放弃未完成的工作流');
    } catch (e) {
        addLog('[系统] 清除状态失败: ' + e.message, 'warn');
    }
});

// ===== 定时自动启动 =====
let autoStartLoaded = false;  // 防止初始加载时误触发自动保存

let autoStartSettings = {
    enabled: false,
    time: '19:20',
    day_mode: 'workdays',
    auto_confirm_delay: 3,
};

async function loadAutoStartSettings() {
    try {
        const resp = await fetch('/api/auto-start/settings');
        const data = await resp.json();
        autoStartSettings = data;
        document.getElementById('auto-start-enabled').checked = data.enabled;
        document.getElementById('auto-start-time').value = data.time;
        document.getElementById('auto-start-days').value = data.day_mode;
        document.getElementById('auto-confirm-delay').value = data.auto_confirm_delay || 3;
        document.getElementById('auto-start-pin').value = data.lock_password || '';
        onAutoStartToggle();
        // 获取计划任务状态
        fetchAutoStartStatus();
        autoStartLoaded = true;  // 加载完成，允许自动保存
    } catch (e) {
        addLog('[系统] 定时设置加载失败', 'warn');
    }
}

async function fetchAutoStartStatus() {
    try {
        const resp = await fetch('/api/auto-start/status');
        const data = await resp.json();
        if (data.exists && data.next_run) {
            document.getElementById('auto-start-next-run').textContent = data.next_run;
        } else if (autoStartSettings.enabled) {
            document.getElementById('auto-start-next-run').textContent =
                '每天 ' + autoStartSettings.time + '（待创建）';
        } else {
            document.getElementById('auto-start-next-run').textContent = '未启用';
        }
    } catch (e) {
        document.getElementById('auto-start-next-run').textContent = '--';
    }
}

function onAutoStartToggle() {
    const enabled = document.getElementById('auto-start-enabled').checked;
    const details = document.getElementById('auto-start-details');
    const badge = document.getElementById('auto-start-badge');
    details.style.display = enabled ? 'block' : 'none';
    badge.textContent = enabled ? '🟢 已启用' : '⚪ 未启用';
    if (!enabled) {
        document.getElementById('auto-start-next-run').textContent = '未启用';
    } else {
        fetchAutoStartStatus();
    }
    // 用户手动切换时即时保存（初始加载时不触发）
    if (autoStartLoaded) {
        saveAutoStartSettings();
    }
}

async function saveAutoStartSettings() {
    const enabled = document.getElementById('auto-start-enabled').checked;
    const time = document.getElementById('auto-start-time').value;
    const day_mode = document.getElementById('auto-start-days').value;
    const auto_confirm_delay = parseInt(document.getElementById('auto-confirm-delay').value) || 3;
    const lock_password = document.getElementById('auto-start-pin').value;

    const msgEl = document.getElementById('auto-start-save-msg');
    msgEl.textContent = '⏳ 保存中...';
    msgEl.style.color = 'var(--blue)';

    try {
        const resp = await fetch('/api/auto-start/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                enabled, time, day_mode, auto_confirm_delay, lock_password,
            }),
        });
        const result = await resp.json();
        if (result.status === 'ok') {
            autoStartSettings = { enabled, time, day_mode, auto_confirm_delay };
            msgEl.textContent = enabled ? '✅ 已保存，计划任务已创建' : '✅ 已保存，计划任务已删除';
            msgEl.style.color = 'var(--teal)';
            fetchAutoStartStatus();
            addLog(`[系统] 定时设置已保存: ${enabled ? '启用 ' + time + ' ' + day_mode : '已禁用'}`, 'success');
        } else {
            msgEl.textContent = '❌ 保存失败: ' + (result.message || '');
            msgEl.style.color = 'var(--red)';
        }
    } catch (e) {
        msgEl.textContent = '❌ 请求失败: ' + e.message;
        msgEl.style.color = 'var(--red)';
    }
    setTimeout(() => { msgEl.textContent = ''; }, 5000);
}

document.getElementById('btn-save-auto-start').addEventListener('click', saveAutoStartSettings);

// ===== 自动确认智能弹窗 =====
// 检测 URL 参数 ?auto=1（start.bat 自动启动时传入）
function isAutoStartMode() {
    const urlParams = new URLSearchParams(window.location.search);
    return urlParams.get('auto') === '1';
}

// 覆盖 showSmartPrompt 以支持自动确认
const _originalShowSmartPrompt = showSmartPrompt;
showSmartPrompt = function(mode, course, extra) {
    _originalShowSmartPrompt(mode, course, extra);

    // 只在自动启动模式下执行自动确认
    if (!isAutoStartMode()) return;

    const modal = document.getElementById('modal-smart');
    if (!modal.classList.contains('show')) return;

    const btnStart = document.getElementById('btn-smart-start');
    const delay = autoStartSettings.auto_confirm_delay || 3;

    addLog(`[系统] 🤖 自动启动模式 — ${delay}秒后自动确认`, 'success');

    // 在按钮上显示倒计时
    let countdown = delay;
    const originalText = btnStart.textContent;
    btnStart.textContent = `${originalText} (${countdown}s)`;
    btnStart.style.background = 'var(--teal)';

    const timer = setInterval(() => {
        countdown--;
        if (countdown <= 0) {
            clearInterval(timer);
            btnStart.click();
            addLog('[系统] 🤖 已自动确认启动', 'success');
        } else {
            btnStart.textContent = `${originalText} (${countdown}s)`;
        }
    }, 1000);

    // 如果用户手动关闭弹窗，清除定时器
    const skipBtn = document.getElementById('btn-smart-skip');
    const originalSkip = skipBtn.onclick;
    skipBtn.onclick = () => {
        clearInterval(timer);
        btnStart.textContent = originalText;
        btnStart.style.background = '';
        if (originalSkip) originalSkip();
    };
};

async function init() {
    addLog('[系统] 继续教育自动化工具已启动');

    try {
        const config = await fetch('/api/config').then(r => r.json());
        appConfig = config;
        document.getElementById('course-start').value = config.course.default_start_time;
        // 课程结束时间 = 开始时间 + 90分钟（自动计算，不从config读取）
        document.getElementById('course-start').dispatchEvent(new Event('input'));
        document.getElementById('sign-in-time').value = config.attendance.sign_in_time;
        document.getElementById('sign-out-time').value = config.attendance.sign_out_time;
        document.getElementById('verify-recipient').value = config.login.verification_recipient;
        document.getElementById('planned-students').value = config.course.planned_students || 6;
        document.getElementById('qr-timeout').value = config.attendance.qr_timeout_seconds;
        document.getElementById('qr-count').value = config.attendance.qr_resend_count;
        if (config.wechat.target_groups && config.wechat.target_groups.length > 0) {
            config.wechat.target_groups.forEach(g => addChatToList(g));
            document.getElementById('wechat-group').value = config.wechat.target_groups[0];
        }
        addLog('[系统] 配置已从 config.yaml 加载');
    } catch (e) {
        addLog('[系统] 配置加载失败，使用默认值', 'warn');
    }

    connectLogStream();
    refreshRecipientDropdown();
    refreshChatDropdown();
    await loadAutoStartSettings();
    await loadCourseList();
    document.getElementById('single-date').value = new Date().toISOString().split('T')[0];
    addLog('[系统] 准备就绪，请选择活动后勾选课程');

    // Check for incomplete workflow (crash recovery)
    await checkResumeState();
    // Smart workflow: auto-select course and prompt user
    checkSmartWorkflow();
}

// ===== 智能工作流：自动选择课程并提示 =====
function checkSmartWorkflow() {
    const today = todayStr();
    // 优先：找今天已创建但未签到的课程 → 提示仅考勤（只考勤今日课程）
    const unsignined = allCourses.filter(c =>
        c.status.created && !c.status.sign_in && c.status.created_date === today
    );
    if (unsignined.length > 0) {
        const course = unsignined[0];
        // 自动选择该课程为考勤课程
        selectAttendanceCourse(course.course_name);
        refreshAttendanceList();
        const remaining = unsignined.length > 1 ? `（共${unsignined.length}个待考勤课程）` : '';
        showSmartPrompt('attendance', course, remaining);
        return;
    }

    // 有其他日期已创建但未签到的课程 → 提示但不建议考勤（避免考勤非今日课程）
    const otherUnsigned = allCourses.filter(c => c.status.created && !c.status.sign_in);
    if (otherUnsigned.length > 0) {
        addLog(`[系统] 有 ${otherUnsigned.length} 门已创建课程但非今日举办，跳过考勤建议（仅考勤今日课程）`, 'warn');
    }

    // 其次：找未创建的课程 → 提示创建+考勤
    const uncreated = allCourses.filter(c => !c.status.created);
    if (uncreated.length > 0) {
        const course = uncreated[0];
        // 自动勾选该课程
        const cb = document.getElementById('course-cb-' + course.id);
        if (cb && !cb.disabled) {
            cb.checked = true;
            onCourseCheck();
        }
        showSmartPrompt('full', course, '');
        return;
    }

    addLog('[系统] 所有课程已完成，没有待处理的任务');
}

function selectAttendanceCourse(courseName) {
    selectedAttendanceCourse = courseName;
}

function showSmartPrompt(mode, course, extra) {
    const modal = document.getElementById('modal-smart');
    const title = document.getElementById('smart-title');
    const detail = document.getElementById('smart-detail');

    document.getElementById('smart-course-name').textContent =
        `#${course.id} ${course.course_name}`.substring(0, 50);

    const btnStart = document.getElementById('btn-smart-start');
    const btnSkip = document.getElementById('btn-smart-skip');

    const closeModal = () => { modal.classList.remove('show'); };

    if (mode === 'attendance') {
        title.textContent = '📱 发现待考勤课程';
        detail.textContent = extra ?
            `该课程已创建但未考勤（共 ${allCourses.filter(c => c.status.created && !c.status.sign_in).length} 个待考勤）。是否现在开始考勤？` :
            '该课程已创建但未考勤。是否现在开始考勤？';
        btnStart.textContent = '📱 开始考勤';
        btnStart.onclick = () => {
            closeModal();
            document.getElementById('wechat-group').value =
                document.getElementById('wechat-group').value || '继续教育签到';
            startWorkflow('attend_only');
        };
    } else {
        title.textContent = '🚀 发现未创建课程';
        detail.textContent = '该课程尚未创建。是否现在创建课程并自动考勤？';
        btnStart.textContent = '🚀 创建+考勤';
        btnStart.onclick = () => {
            closeModal();
            document.getElementById('wechat-group').value =
                document.getElementById('wechat-group').value || '继续教育签到';
            startWorkflow('full');
        };
    }

    btnSkip.onclick = () => {
        closeModal();
        addLog('[系统] 已跳过智能推荐，可手动选择');
    };

    // Close on overlay click
    modal.onclick = (e) => {
        if (e.target === modal) closeModal();
    };

    modal.classList.add('show');
}

// ===== 账号与教师设置弹窗 =====

// HTML 属性转义（教师姓名/证件号拼进 value="..." 前必须转义）
function escapeAttr(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// 渲染授课教师名单（顺序即轮换顺序）；少于 3 位时补空行方便填写
function renderTeacherRows(teachers) {
    const box = document.getElementById('settings-teachers');
    const list = (teachers || []).slice();
    while (list.length < 3) list.push({ name: '', id_number: '' });
    box.innerHTML = list.map((t, i) => `
        <div class="form-row">
            <label>第 ${i + 1} 位 · 姓名 ${i === 0 ? '<span class="required">*</span>' : ''}</label>
            <input type="text" class="settings-teacher-name" data-index="${i}"
                   placeholder="授课教师姓名" value="${escapeAttr(t.name)}">
        </div>
        <div class="form-row">
            <label>第 ${i + 1} 位 · 身份证号</label>
            <input type="text" class="settings-teacher-id" data-index="${i}" maxlength="18"
                   placeholder="18 位，用于授分，请准确填写" value="${escapeAttr(t.id_number)}">
        </div>`).join('');
}

// 收集教师名单，整行空白丢弃；返回 {teachers} 或 {error}
function collectTeachers() {
    const names = document.querySelectorAll('#settings-teachers .settings-teacher-name');
    const ids = document.querySelectorAll('#settings-teachers .settings-teacher-id');
    const teachers = [];
    for (let i = 0; i < names.length; i++) {
        const name = names[i].value.trim();
        const id = (ids[i] && ids[i].value.trim()) || '';
        if (!name && !id) continue;  // 整行空白 → 丢弃
        const pos = i + 1;
        if (!name) return { error: `第 ${pos} 位教师姓名不能为空` };
        if (!id) return { error: `第 ${pos} 位教师（${name}）身份证号不能为空（用于授分）` };
        if (id.length !== 18) return { error: `第 ${pos} 位教师（${name}）身份证号应为 18 位` };
        teachers.push({ name, id_number: id });
    }
    if (teachers.length === 0) return { error: '至少需要一位授课教师' };
    return { teachers };
}

document.getElementById('btn-settings').addEventListener('click', () => {
    const modal = document.getElementById('modal-settings');
    // 打开时加载当前配置到表单
    fetch('/api/config').then(r => r.json()).then(cfg => {
        document.getElementById('settings-username').value = (cfg.login && cfg.login.username) || '';
        document.getElementById('settings-password').value = (cfg.login && cfg.login.password) || '';
        document.getElementById('settings-recipient').value = (cfg.login && cfg.login.verification_recipient) || '';
        const course = cfg.course || {};
        renderTeacherRows(course.teachers || (course.teacher_name
            ? [{ name: course.teacher_name, id_number: course.teacher_id || '' }] : []));
        document.getElementById('settings-wechat-path').value = (cfg.wechat && cfg.wechat.exe_path) || '';
        document.getElementById('settings-idle-minutes').value = (cfg.auto_start && cfg.auto_start.idle_exit_minutes != null) ? cfg.auto_start.idle_exit_minutes : 10;
        document.getElementById('settings-qr-dir').value = (cfg.attendance && cfg.attendance.qr_save_dir) || '';
    }).catch(() => addLog('[系统] 设置加载失败', 'warn'));
    modal.classList.add('show');
    document.getElementById('settings-username').focus();
});

document.getElementById('btn-settings-save').addEventListener('click', async () => {
    const collected = collectTeachers();
    if (collected.error) { addLog('[系统] ' + collected.error, 'error'); return; }
    const data = {
        username: document.getElementById('settings-username').value.trim(),
        password: document.getElementById('settings-password').value,
        verification_recipient: document.getElementById('settings-recipient').value.trim(),
        teachers: collected.teachers,
        wechat_exe_path: document.getElementById('settings-wechat-path').value.trim(),
        idle_exit_minutes: parseFloat(document.getElementById('settings-idle-minutes').value),
        qr_save_dir: document.getElementById('settings-qr-dir').value.trim(),
    };
    if (!data.username) { addLog('[系统] 登录账号不能为空', 'error'); return; }
    try {
        const resp = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        const result = await resp.json();
        if (result.status === 'ok') {
            document.getElementById('modal-settings').classList.remove('show');
            addLog('[系统] 账号与教师设置已保存', 'success');
            // 同步验证码接收人下拉（配置里的接收人出现在课程表单的下拉里）
            const recSel = document.getElementById('verify-recipient');
            if (recSel && data.verification_recipient) {
                const names = Array.from(recSel.options).map(o => o.value);
                if (!names.includes(data.verification_recipient)) {
                    const opt = document.createElement('option');
                    opt.value = data.verification_recipient;
                    opt.textContent = data.verification_recipient;
                    recSel.appendChild(opt);
                }
                recSel.value = data.verification_recipient;
            }
        } else {
            addLog('[系统] 保存失败: ' + (result.message || ''), 'error');
        }
    } catch (e) {
        addLog('[系统] 保存失败: ' + e.message, 'error');
    }
});

document.getElementById('btn-settings-cancel').addEventListener('click', () => {
    document.getElementById('modal-settings').classList.remove('show');
});

// 点击遮罩关闭设置弹窗
document.getElementById('modal-settings').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) e.currentTarget.classList.remove('show');
});

// Esc 关闭设置弹窗
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        const modal = document.getElementById('modal-settings');
        if (modal.classList.contains('show')) modal.classList.remove('show');
    }
});

// ===== 键盘快捷键 =====
document.addEventListener('keydown', function(e) {
    if (e.key !== 'Enter') return;
    // 如果焦点在输入框内，不拦截（让用户正常输入）
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

    // 智能工作流弹窗 → Enter 触发主按钮
    const smartModal = document.getElementById('modal-smart');
    if (smartModal.classList.contains('show')) {
        e.preventDefault();
        document.getElementById('btn-smart-start').click();
        return;
    }

    // 验证码弹窗 → Enter 触发提交（已有 input keydown 处理，但加个保险）
    const verifyModal = document.getElementById('modal-verify');
    if (verifyModal.classList.contains('show')) {
        e.preventDefault();
        document.getElementById('btn-verify-submit').click();
        return;
    }
});

init();
