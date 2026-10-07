"""窗口查找与前台激活。"""

from __future__ import annotations
import random
import time
from typing import Collection, Iterable, List

import win32api
import win32con
import win32gui

from binder.app_logging import log_exception, log_info


# 切换前台会静默失败（SetForegroundWindow 被前台锁拦下时不抛异常），
# 所以切完要校验 GetForegroundWindow，不成立就重试。
FOREGROUND_RETRY_COUNT = 3
FOREGROUND_VERIFY_TIMEOUT_SECONDS = 0.4
FOREGROUND_POLL_INTERVAL_SECONDS = 0.05


def find_windows(window_title: str) -> List[int]:
    """按标题精确匹配（忽略大小写）返回所有可见顶层窗口句柄。"""
    matched_windows: List[int] = []
    expected_title = window_title.casefold()

    def enum_window_callback(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True

        title = win32gui.GetWindowText(hwnd).strip()
        if title.casefold() == expected_title:
            matched_windows.append(hwnd)
        return True

    win32gui.EnumWindows(enum_window_callback, None)
    return matched_windows


def is_foreground_window(hwnd: int) -> bool:
    """目标窗口（或它自己弹出的子窗口）当前是否处于前台。"""
    try:
        foreground = win32gui.GetForegroundWindow()
        if not foreground:
            return False
        if foreground == hwnd:
            return True
        return win32gui.GetAncestor(foreground, win32con.GA_ROOTOWNER) == hwnd
    except win32gui.error:
        return False


def is_minimized_window(hwnd: int) -> bool:
    """窗口是否已最小化。"""
    try:
        return bool(win32gui.IsIconic(hwnd))
    except win32gui.error:
        return False


def minimize_window(hwnd: int) -> bool:
    """把窗口最小化，返回是否真的做了动作。

    已经最小化、句柄失效都返回 False（调用方据此避免重复动作和重复日志）。
    """
    try:
        if not win32gui.IsWindow(hwnd):
            return False
        if win32gui.IsIconic(hwnd):
            return False
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return True
    except win32gui.error:
        log_exception(f"最小化窗口失败 hwnd={hwnd}")
        return False


def minimize_windows(
    hwnds: Iterable[int],
    keep_hwnds: Collection[int] = (),
) -> int:
    """最小化一批窗口，``keep_hwnds`` 里的保持不动，返回实际最小化的数量。"""
    minimized = 0
    for hwnd in hwnds:
        if hwnd in keep_hwnds:
            continue
        if minimize_window(hwnd):
            minimized += 1
    return minimized


def activate_window(hwnd: int) -> bool:
    """把窗口切到前台，返回是否确认成功。

    只有确认前台之后才发点击：窗口没真正到前台时，第一次点击会被
    系统当成"激活点击"吃掉，页面不动却看不出任何异常。
    """
    if not win32gui.IsWindow(hwnd):
        log_info(f"切换窗口到前台失败：窗口句柄已失效 hwnd={hwnd}")
        return False

    for _ in range(FOREGROUND_RETRY_COUNT):
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

            # 模拟一次 Alt 按键，让 Windows 允许当前进程切换前台窗口。
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(
                win32con.VK_MENU,
                0,
                win32con.KEYEVENTF_KEYUP,
                0,
            )
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except win32gui.error:
            log_exception(f"切换窗口到前台出错 hwnd={hwnd}")

        deadline = time.monotonic() + FOREGROUND_VERIFY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if is_foreground_window(hwnd):
                return True
            time.sleep(FOREGROUND_POLL_INTERVAL_SECONDS)

    log_info(
        "切换窗口到前台失败 "
        f"hwnd={hwnd} 前台窗口={win32gui.GetForegroundWindow()}"
    )
    return False


def wait_before_action() -> None:
    time.sleep(random.uniform(0.1, 0.5))
