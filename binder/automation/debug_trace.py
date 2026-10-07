"""调试追踪：按工作顺序记录窗口枚举、截图与模板匹配结果。
启用方式（任选其一）：
- 环境变量 ``STEAM_EA_DEBUG_CAPTURE=1``
- ``python main.py --debug-capture``
- ``python debug_trace.py --all-windows``（独立快照工具，跑完即退出）
可选环境变量：
- ``STEAM_EA_DEBUG_DIR``
    输出目录，默认 ``<程序目录>/debug_capture/<时间戳>_<pid>``
- ``STEAM_EA_DEBUG_FRAME_INTERVAL``
    截图落盘的最小间隔秒数，默认 1.0；设为 0 表示每帧都存
- ``STEAM_EA_DEBUG_ALL_WINDOWS``
    设为 1 时，额外枚举并截图所有可见顶层窗口（便于确认"屏幕上到底有什么"）
未启用时所有函数都是空操作，只有一次 ``is_enabled()`` 判断的开销。
日志统一写成 ``[DEBUG][序号] 上下文 | 内容``，序号严格按调用顺序递增，
因此日志顺序就是程序的工作顺序；截图文件名也带同一序号，可直接按名排序对照。
"""

from __future__ import annotations
import contextlib
import os
import re
import time
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence

import cv2
import numpy as np
import win32gui
import win32process

from binder.app_logging import log_info
from binder.paths import DEBUG_CAPTURE_DIR


DEBUG_FLAG_ENV = "STEAM_EA_DEBUG_CAPTURE"
DEBUG_DIR_ENV = "STEAM_EA_DEBUG_DIR"
FRAME_INTERVAL_ENV = "STEAM_EA_DEBUG_FRAME_INTERVAL"
ALL_WINDOWS_ENV = "STEAM_EA_DEBUG_ALL_WINDOWS"
DEFAULT_FRAME_INTERVAL_SECONDS = 1.0
FRAME_NAME_UNSAFE = re.compile(r"[^0-9A-Za-z_\-]+")
SKIPPED_WINDOW_CLASSES = {"IME", "MSCTFIME UI"}

_enabled: Optional[bool] = None
_session_dir: Optional[Path] = None
_index = 0
_context = ""
_last_frame_at = 0.0
_force_frame = False


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _frame_interval() -> float:
    raw = os.environ.get(FRAME_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_FRAME_INTERVAL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_FRAME_INTERVAL_SECONDS


def is_enabled() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = _env_flag(DEBUG_FLAG_ENV)
    return _enabled


def reset() -> None:
    """清空缓存（改过环境变量或测试时调用）。"""
    global _enabled, _session_dir, _index, _context, _last_frame_at
    _enabled = None
    _session_dir = None
    _index = 0
    _context = ""
    _last_frame_at = 0.0


def session_dir() -> Path:
    global _session_dir
    if _session_dir is None:
        configured = os.environ.get(DEBUG_DIR_ENV, "").strip()
        if configured:
            base = Path(configured)
        else:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            base = (
                DEBUG_CAPTURE_DIR
                / "debug_capture"
                / f"{stamp}_{os.getpid()}"
            )
        base.mkdir(parents=True, exist_ok=True)
        _session_dir = base
    return _session_dir


def set_context(text: str) -> None:
    """设置后续日志的上下文（例如 ``EA阶段/acc1/ea_email_input``）。"""
    global _context
    if is_enabled():
        _context = text


def _next_index() -> int:
    global _index
    _index += 1
    return _index


def _emit(index: int, message: str) -> None:
    log_info(f"[DEBUG][{index:04d}] {_context} | {message}")


def log(message: str) -> None:
    if not is_enabled():
        return
    _emit(_next_index(), message)


@contextlib.contextmanager
def force_frames() -> Iterator[None]:
    """在 with 块内忽略截图限流，强制落盘。"""
    global _force_frame
    previous = _force_frame
    _force_frame = True
    try:
        yield
    finally:
        _force_frame = previous


def _safe_name(text: str, limit: int = 48) -> str:
    cleaned = FRAME_NAME_UNSAFE.sub("_", text).strip("_")
    return (cleaned[:limit] or "frame")


def save_frame(
    image: Optional[np.ndarray],
    note: str = "",
    force: bool = False,
) -> Optional[Path]:
    """把一帧截图落盘（默认按间隔限流），并在日志中记录文件路径。"""
    if not is_enabled() or image is None:
        return None

    global _last_frame_at
    now = time.monotonic()
    interval = _frame_interval()
    throttled = (
        not force
        and not _force_frame
        and interval > 0
        and (now - _last_frame_at) < interval
    )
    if throttled:
        log(
            f"截图(限流未落盘) {note} "
            f"shape={image.shape[1]}x{image.shape[0]}"
        )
        return None

    _last_frame_at = now
    index = _next_index()
    path = session_dir() / f"{index:04d}_{_safe_name(note)}.png"
    try:
        # 用 imencode + tofile，兼容中文路径。
        encoded_ok, buffer = cv2.imencode(".png", image)
        if not encoded_ok:
            _emit(index, f"截图编码失败 {note}")
            return None
        buffer.tofile(str(path))
    except Exception as error:
        _emit(index, f"截图落盘失败 {note}: {type(error).__name__}: {error}")
        return None

    _emit(
        index,
        f"截图 {note} shape={image.shape[1]}x{image.shape[0]} "
        f"mean={float(image.mean()):.1f} 文件={path.name}",
    )
    return path


def log_match(
    label: str,
    threshold: float,
    best_score: Optional[float],
    matched: bool,
) -> None:
    """记录一次模板匹配的结果（含未命中时的最高分，便于调阈值）。"""
    if not is_enabled():
        return
    if best_score is None:
        log(f"模板匹配 {label or '?'} 跳过（模板大于截图）")
        return
    log(
        f"模板匹配 {label or '?'} 阈值={threshold:.2f} "
        f"最高分={best_score:.4f} 结果={'命中' if matched else '未命中'}"
    )


def log_window_enum(window_title: str, hwnds: Sequence[int]) -> None:
    """记录一次按标题查找窗口的结果。"""
    if not is_enabled():
        return
    if not hwnds:
        log(f"窗口枚举 title={window_title!r} 命中 0 个")
        return

    parts: List[str] = []
    for hwnd in hwnds:
        parts.append(_describe_window(hwnd))
    log(
        f"窗口枚举 title={window_title!r} 命中 {len(hwnds)} 个: "
        + " ; ".join(parts)
    )


def _describe_window(hwnd: int) -> str:
    title = win32gui.GetWindowText(hwnd)
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        left = top = right = bottom = 0
    return (
        f"hwnd={hwnd} title={title!r} "
        f"rect=({left},{top},{right},{bottom}) "
        f"iconic={bool(win32gui.IsIconic(hwnd))}"
    )


def dump_all_windows(label: str = "") -> None:
    """枚举所有顶层窗口并逐个记录；开启 ALL_WINDOWS 时再截图可见窗口。"""
    if not is_enabled():
        return

    rows: List[tuple] = []

    def callback(hwnd: int, _: Any) -> bool:
        title = win32gui.GetWindowText(hwnd).strip()
        class_name = win32gui.GetClassName(hwnd)
        visible = bool(win32gui.IsWindowVisible(hwnd))
        iconic = bool(win32gui.IsIconic(hwnd))
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            left = top = right = bottom = 0
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = 0
        rows.append(
            (
                hwnd,
                title,
                class_name,
                visible,
                iconic,
                (left, top, right, bottom),
                pid,
            )
        )
        return True

    win32gui.EnumWindows(callback, None)
    suffix = f" {label}" if label else ""
    # 无标题且不可见的窗口（多为 IME 辅助窗口）只计数不逐条记录，避免刷屏。
    meaningful = [
        row
        for row in rows
        if (row[1] or row[3]) and row[2] not in SKIPPED_WINDOW_CLASSES
    ]
    log(
        f"全窗口枚举{suffix} 共 {len(rows)} 个顶层窗口，"
        f"其中有标题或可见的 {len(meaningful)} 个："
    )

    for (
        hwnd,
        title,
        class_name,
        visible,
        iconic,
        rect,
        pid,
    ) in meaningful:
        log(
            f"  窗口 hwnd={hwnd} title={title!r} class={class_name!r} "
            f"visible={visible} iconic={iconic} rect={rect} pid={pid}"
        )

    if not _env_flag(ALL_WINDOWS_ENV):
        return

    from binder.automation.window_capture import capture_window

    with force_frames():
        for (
            hwnd,
            title,
            class_name,
            visible,
            iconic,
            rect,
            pid,
        ) in meaningful:
            if not visible or iconic or not title:
                continue
            if class_name in SKIPPED_WINDOW_CLASSES:
                continue
            try:
                capture_window(hwnd, note=f"win_{hwnd}_{_safe_name(title, 24)}")
            except Exception as error:
                log(
                    f"  窗口截图失败 hwnd={hwnd} title={title!r}: "
                    f"{type(error).__name__}: {error}"
                )


def main() -> None:
    """独立调试工具：枚举（并截图）窗口后退出。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="枚举窗口并截图，用于调试模板识别",
    )
    parser.add_argument(
        "--window-title",
        action="append",
        default=[],
        help="额外只关注该标题的窗口（可重复指定）",
    )
    parser.add_argument(
        "--all-windows",
        action="store_true",
        help="截图所有可见顶层窗口的内容",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="输出目录，默认 <程序目录>/debug_capture/<时间戳>_<pid>",
    )
    args = parser.parse_args()

    os.environ[DEBUG_FLAG_ENV] = "1"
    if args.out:
        os.environ[DEBUG_DIR_ENV] = args.out
    if args.all_windows:
        os.environ[ALL_WINDOWS_ENV] = "1"
    reset()

    from binder.automation.window_capture import capture_window
    from binder.automation.win_utils import find_windows

    dump_all_windows("命令行快照")

    for window_title in args.window_title:
        hwnds = find_windows(window_title)
        log_window_enum(window_title, hwnds)
        with force_frames():
            for hwnd in hwnds:
                try:
                    capture_window(hwnd, note=f"{window_title}_{hwnd}")
                except Exception as error:
                    log(
                        f"截图失败 hwnd={hwnd}: "
                        f"{type(error).__name__}: {error}"
                    )

    print(f"调试输出目录: {session_dir()}")


if __name__ == "__main__":
    # 以模块方式再导入一次再调用 main()：直接跑本文件会得到 __main__ 与
    # debug_trace 两份全局状态，导致序号/限流/force_frames 全部错位。
    from binder.automation import debug_trace as _debug_trace_module

    _debug_trace_module.main()
