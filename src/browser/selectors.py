"""页面元素选择器 — 集中管理所有选择器

选择器优先级：文本选择器 > role选择器 > CSS类名 > XPath
所有带 TODO 标记的选择器需要在首次访问网站时确认/修正
"""

SELECTORS = {
    # ===== 登录页（已通过DOM探查确认）=====
    "login": {
        # 登录页是Tab切换结构：账号登录 | 验证码登录
        "账号登录_tab": "text=账号",
        "username_input": "input[name='user_name'][placeholder*='用户名']",
        "password_input": "input[name='password'][placeholder*='密码']",
        "login_button": "input[name='login_btn']",     # type=button, 页面有多个，需选可见的
        # 安全手机验证弹窗
        "verification_popup": "[role='dialog'], .el-dialog, .layui-layer",
        "verification_recipient_select": "input[placeholder='请选择']",
        "verification_recipient_options": "dd",  # layui下拉选项
        "verification_code_input": "input[name='code'][placeholder*='验证码']",
        "verification_get_code_btn": "button[name='code_btn']",  # 精确选择器
        "verification_confirm_btn": "button:has-text('确认'), input[value='确认'], text=确认",
    },

    # ===== 左侧导航 =====
    "sidebar": {
        "院内活动管理": "text=院内活动管理",
        "科室院内活动": "text=科室院内活动",
    },

    # ===== 科室院内活动列表 =====
    "activity_list": {
        "新增按钮": "button:has-text('新增')",
        "查询按钮": "button:has-text('查询')",
        "活动名称搜索": "input[placeholder*='院内活动名称']",
        "操作列_扫码考勤": "text=扫码考勤",
        "操作列_维护举办周期": "text=维护举办周期",
        "表格行": "table tbody tr, .el-table__body tr",
    },

    # ===== 添加活动表单 =====
    # label 无 id，通过 .layui-form-label 文字定位 → 同行控件
    # layui select: .layui-form-select → 点击 input → dl>dd 选项
    # treeSelect: .layui-treeSelect-body.ztree → .node_name span
    "activity_form": {
        "label_pattern":     ".layui-form-label",            # label 定位前缀
        "activity_name":     "input[name='groupProjName']",  # 活动名称 input
        "add_btn":           "#groupAdd",                    # 新增按钮
        "next_step":         "button[lay-filter='nextStep']",
        "save_back":         "button[lay-filter='saveBack']",
    },

    # ===== 举办周期 =====
    "period": {
        "add_cycle_btn":    "a[lay-event='addCycle']",
        "confirm_btn":      ".layui-layer-btn0",
    },

    # ===== 课程 =====
    "course": {
        "add_course_btn":   "a[lay-event='addCourse']",
    },

    # ===== 教师 =====
    "teacher": {
        "add_teacher_btn":  "a[lay-event='addLecturer']",
        "teacher_no":       "input[data-field='teachNo']",     # 优先
        "teacher_name":     "input[data-field='teachName']",   # 优先
    },

    # ===== 考勤 =====
    "attendance": {
        "扫码考勤按钮": "text=扫码考勤",
        "开始考勤按钮": "button:has-text('开始考勤')",
        "考勤配置确定": "button:has-text('确定')",
        "考勤配置取消": "button:has-text('取消')",
        "二维码时效下拉": "select#selectForTime",                    # 已确认
        "二维码图片": "img[src*='qr'], canvas, [class*='qrcode']",
        "关闭弹窗按钮": "button[class*='close'], [class*='el-icon-close']",
        "实时考勤人数": "",                                       # 已废弃：不再需要实时人数监控
    },

    # ===== 授分 =====
    "grant_credits": {
        "全部_tab": "#tab_1_0",
        "已举办_tab": "#tab_1_2",
        "授分按钮": "a[lay-event='grantPoints']",
        "考勤记录授分": "a[lay-event='attendanceRecordAwardPoints']",
        "筛选授分人员": "button:has-text('筛选授分人员')",
        "全选复选框": "input[lay-filter='layTableAllChoose']",
        "授分提交按钮": "button:has-text('授分')",
        "授分策略": "#creditStrategy_id",
        "授分日期": "#creditStrategyDate_ids",
    },

    # ===== 通用 =====
    "common": {
        "返回列表": "button:has-text('返回列表'), text=返回列表",
        "返回按钮": "button:has-text('返回')",
        "确认弹窗": "[role='dialog'], .el-dialog",
        "加载中": "[class*='loading'], .el-loading-mask",
        "提示消息": "[class*='message'], .el-message",
    },
}
