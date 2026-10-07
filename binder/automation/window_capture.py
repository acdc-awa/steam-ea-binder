"""窗口截图与模板匹配。
PrintWindow 抓取（含 PW_RENDERFULLCONTENT，可抓 CEF/Chromium 窗口）、
失败回退屏幕截取，以及模板加载与匹配。绑号流程和 tools/eula_detector
都基于这里。
"""

from __future__ import annotations
import ctypes
import os
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
import win32gui
import win32ui
from PIL import ImageGrab

from binder.automation import debug_trace
# 这两个模板路径是给 tools/eula_detector 用的再导出。
from binder.paths import (
    STEAM_EULA_ACCEPT_TEMPLATE as ACCEPT_BUTTON_TEMPLATE_PATH,
    STEAM_EULA_TITLE_TEMPLATE as TITLE_TEMPLATE_PATH,
)


# PrintWindow 的 PW_RENDERFULLCONTENT，用于抓取 CEF(Chromium) 窗口内容。
PRINTWINDOW_FULL_CONTENT = 0x00000002
# 低于该标准差视为全黑/空白帧，回退到屏幕截取。
BLANK_FRAME_STDDEV_THRESHOLD = 1.0
# 设为 grab 可强制使用屏幕区域截取（会被遮挡影响）。
CAPTURE_MODE_ENV = "STEAM_EA_CAPTURE_MODE"



def enable_dpi_awareness() -> None:
    """避免 Windows 显示缩放导致窗口坐标与截图坐标不一致。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def find_window(window_title: str) -> Optional[int]:
    """查找标题精确匹配且可见的顶层窗口。"""
    matched_handles = []

    def enum_window_callback(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True

        title = win32gui.GetWindowText(hwnd).strip()
        if (
            not matched_handles
            and title.casefold() == window_title.casefold()
        ):
            matched_handles.append(hwnd)
        return True

    win32gui.EnumWindows(enum_window_callback, None)
    return matched_handles[0] if matched_handles else None


def _capture_with_printwindow(
    hwnd: int,
    width: int,
    height: int,
) -> Optional[np.ndarray]:
    """直接抓窗口自身内容，不受遮挡和其他窗口影响。

    失败或抓到全黑帧时返回 None，由调用方回退到屏幕截取。
    """
    window_dc = win32gui.GetWindowDC(hwnd)
    if not window_dc:
        return None

    mfc_dc = None
    save_dc = None
    bitmap = None
    try:
        mfc_dc = win32ui.CreateDCFromHandle(window_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        # PW_RENDERFULLCONTENT：让 CEF(Chromium) 窗口也能被完整渲染。
        result = ctypes.windll.user32.PrintWindow(
            hwnd,
            save_dc.GetSafeHdc(),
            PRINTWINDOW_FULL_CONTENT,
        )
        if result != 1:
            return None

        buffer = bitmap.GetBitmapBits(True)
        image = np.frombuffer(buffer, dtype=np.uint8)
        image = image.reshape((height, width, 4))
        image_bgr = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        if float(image_bgr.std()) < BLANK_FRAME_STDDEV_THRESHOLD:
            return None
        return image_bgr
    except Exception:
        return None
    finally:
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
        if save_dc is not None:
            save_dc.DeleteDC()
        if mfc_dc is not None:
            mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)


def capture_window(
    hwnd: int,
    prefer_printwindow: Optional[bool] = None,
    note: Optional[str] = None,
) -> Tuple[np.ndarray, Tuple[int, int]]:
    """截取窗口内容，返回 BGR 图像和窗口左上角屏幕坐标。

    默认优先用 PrintWindow 抓窗口自身内容（不受遮挡影响），
    失败或抓到全黑帧时回退到屏幕区域截取（会被遮挡影响）。
    可用环境变量 STEAM_EA_CAPTURE_MODE=grab 强制走屏幕截取。

    ``note`` 只用于调试模式下的截图文件名与日志说明。
    """
    if win32gui.IsIconic(hwnd):
        raise RuntimeError("窗口已最小化，无法截取可见内容")

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    if right <= left or bottom <= top:
        raise RuntimeError("窗口尺寸无效，窗口可能已最小化")

    width = right - left
    height = bottom - top
    debug_note = note or f"hwnd_{hwnd}"

    if prefer_printwindow is None:
        mode = os.environ.get(CAPTURE_MODE_ENV, "printwindow")
        prefer_printwindow = mode.strip().casefold() != "grab"

    if prefer_printwindow:
        image = _capture_with_printwindow(hwnd, width, height)
        if image is not None:
            debug_trace.save_frame(
                image,
                note=f"{debug_note}_printwindow",
            )
            return image, (left, top)
        debug_trace.log(f"PrintWindow 抓取失败/全黑，回退屏幕截取 {debug_note}")

    screenshot = ImageGrab.grab(
        bbox=(left, top, right, bottom),
        all_screens=True,
    )
    image_rgb = np.asarray(screenshot)
    image_bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
    debug_trace.save_frame(image_bgr, note=f"{debug_note}_grab")
    return image_bgr, (left, top)


def load_template(path: Path) -> np.ndarray:
    """读取模板图片，兼容 Windows 下包含非 ASCII 字符的路径。"""
    if not path.is_file():
        raise FileNotFoundError(f"模板图片不存在: {path}")

    encoded_data = np.fromfile(path, dtype=np.uint8)
    template = cv2.imdecode(encoded_data, cv2.IMREAD_COLOR)
    if template is None:
        raise RuntimeError(f"无法读取模板图片: {path}")
    return template


def find_template(
    screenshot: np.ndarray,
    template: np.ndarray,
    threshold: float,
    label: str = "",
) -> Optional[Tuple[int, int, float]]:
    """返回模板左上角坐标及置信度；未达到阈值时返回 None。

    ``label`` 只用于调试日志，便于对照是哪个模板得了多少分。
    """
    screenshot_height, screenshot_width = screenshot.shape[:2]
    template_height, template_width = template.shape[:2]
    if (
        template_width > screenshot_width
        or template_height > screenshot_height
    ):
        debug_trace.log_match(label, threshold, None, False)
        return None

    result = cv2.matchTemplate(
        screenshot,
        template,
        cv2.TM_CCOEFF_NORMED,
    )
    _, max_score, _, max_location = cv2.minMaxLoc(result)
    matched = max_score >= threshold
    debug_trace.log_match(label, threshold, float(max_score), matched)
    if not matched:
        return None

    x, y = max_location
    return x, y, float(max_score)


