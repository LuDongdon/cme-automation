"""微信就绪检测 — 独立子进程入口

由 auto_start.py 以独立 subprocess 调用，避免微信相关操作崩溃拖垮主进程。
对齐 start.bat 的做法:
  python -c "from src.wechat.controller import ensure_wechat_ready; ..."

退出码: 0=已就绪, 1=未就绪
"""

import sys
import os

# 确保项目模块可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

if __name__ == "__main__":
    try:
        from src.wechat.controller import ensure_wechat_ready
        ready = ensure_wechat_ready()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        ready = False

    if ready:
        print("WeChat is ready.")
        sys.exit(0)
    else:
        print("WeChat is NOT ready.", file=sys.stderr)
        sys.exit(1)
