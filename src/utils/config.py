"""配置加载与校验模块"""

import os
import yaml

# 项目根目录（cme_automation/）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_config(config_path=None):
    """加载 config.yaml 并返回配置字典"""
    if config_path is None:
        config_path = os.path.join(PROJECT_ROOT, "config.yaml")

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 校验必要字段
    _validate(config)
    # 教师名单归一化（老配置只有单个 teacher → 升级为 1 人名单）
    _normalize_teachers(config)
    return config


def _normalize_teachers(config):
    """保证 course.teachers 始终是非空 list[{name,id_number}]，teacher 恒等于 teachers[0]。

    建课时按 teachers 顺序轮换填授课教师（见 app.py 的批次循环）。
    """
    course = config.setdefault("course", {})
    teachers = course.get("teachers")
    if not isinstance(teachers, list) or not teachers:
        single = course.get("teacher") or {}
        teachers = [single] if single else []
    # 只保留有姓名的项，并统一字段类型
    clean = []
    for t in teachers:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        clean.append({"name": name, "id_number": str(t.get("id_number") or "").strip()})
    course["teachers"] = clean
    if clean:
        course["teacher"] = clean[0]


def _validate(config):
    """校验配置完整性"""
    required_sections = ["browser", "login", "course", "attendance", "wechat"]
    for section in required_sections:
        if section not in config:
            raise ValueError(f"配置文件缺少必要节: [{section}]")

    # browser 校验
    if "base_url" not in config["browser"]:
        raise ValueError("browser.base_url 未配置")

    # login 校验
    if "verification_recipient" not in config["login"]:
        raise ValueError("login.verification_recipient 未配置")

    # course 校验：教师名单（teachers 优先，兼容老的单个 teacher）
    course = config["course"]
    teachers = course.get("teachers")
    if teachers is not None:
        if not isinstance(teachers, list) or not teachers:
            raise ValueError("course.teachers 必须是非空列表")
        for i, t in enumerate(teachers, 1):
            if not isinstance(t, dict) or not t.get("id_number"):
                raise ValueError(f"course.teachers 第 {i} 位缺少 id_number")
    else:
        if "teacher" not in course:
            raise ValueError("course.teacher 未配置")
        if "id_number" not in course["teacher"]:
            raise ValueError("course.teacher.id_number 未配置")


def get_config_path():
    """返回默认配置文件路径"""
    return os.path.join(PROJECT_ROOT, "config.yaml")


def get_data_dir():
    """返回 data 目录路径"""
    path = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(path, exist_ok=True)
    return path


def get_screenshot_dir():
    """返回截图目录路径"""
    path = os.path.join(PROJECT_ROOT, "screenshots")
    os.makedirs(path, exist_ok=True)
    return path
