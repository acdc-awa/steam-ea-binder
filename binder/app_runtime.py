"""Windows 运行环境检查。"""

from __future__ import annotations
import ctypes


def is_running_as_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False
