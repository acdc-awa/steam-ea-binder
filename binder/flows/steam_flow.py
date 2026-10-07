"""Steam 登录和 Battlefield 1 启动阶段。"""

from __future__ import annotations
import ctypes
import os
import random
import subprocess
import time
from pathlib import Path
from typing import Any, Collection, Optional, Tuple

import win32api
import win32con

from binder.automation import debug_trace
from binder.app_logging import log_exception, log_info
from binder.automation.credential_cleanup import (
    STEAM_MAIN_PROCESS,
    find_steam_dir,
    find_steam_executable,
    prepare_account_environment,
    read_steam_autologin_account,
)
from binder.automation.mouse_motion import smooth_move_to
from binder.automation.process_utils import find_processes_by_names
from binder.automation.win_utils import (
    activate_window,
    find_windows,
    is_minimized_window,
    minimize_windows,
)
from binder.flows.errors import AccountInfoError
from binder.paths import (
    STEAM_EULA_ACCEPT_TEMPLATE as ACCEPT_BUTTON_TEMPLATE_PATH,
    STEAM_EULA_TITLE_TEMPLATE as TITLE_TEMPLATE_PATH,
)


STEAM_WINDOW_TITLE = "Steam"
GAME_URI = "steam://rungameid/1238840"
MATCH_THRESHOLD = 0.85

# Steam 窗口出现的最长等待时间，避免登录被卡住时永久挂起。
STEAM_WINDOW_TIMEOUT_SECONDS = 90.0
# 登录后确认"当前登录的是目标账号"的最长等待时间。
STEAM_LOGIN_VERIFY_TIMEOUT_SECONDS = 30.0

# verify_steam_login 的三种结论。
# confirmed：确认自动登录账号就是目标账号；
# stale：仍停留在上一个账号，说明切换失败（密码错时就是这个结果）；
# unconfirmed：读不到自动登录标记，无法确认，但也不能据此断定有错
#   —— Steam 未勾选"记住我"时本来就不留标记。
STEAM_LOGIN_CONFIRMED = "confirmed"
STEAM_LOGIN_STALE = "stale"
STEAM_LOGIN_UNCONFIRMED = "unconfirmed"

# EULA 识别与点击。
EULA_DISCOVERY_TIMEOUT_SECONDS = 20.0
EULA_STABLE_SECONDS = 3.0
EULA_STABILITY_TIMEOUT_SECONDS = 15.0
EULA_POSITION_TOLERANCE = 3
EULA_POLL_INTERVAL_SECONDS = 1.0
EULA_CLICK_RETRY_COUNT = 2
EULA_CONFIRM_TIMEOUT_SECONDS = 6.0

STEAM_SHUTDOWN_TIMEOUT_SECONDS = 30.0
STEAM_FORCE_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def report_progress(
    progress_queue: Any,
    account_id: str,
    status: str,
) -> None:
    progress_queue.put((account_id, status))


def wait_for_steam_window(
    timeout_seconds: float = STEAM_WINDOW_TIMEOUT_SECONDS,
) -> int:
    """等待 Steam 窗口出现；超时抛错，不再无限等待。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        steam_windows = find_windows(STEAM_WINDOW_TITLE)
        if steam_windows:
            return steam_windows[0]
        time.sleep(1.0)

    raise TimeoutError(
        f"等待 Steam 窗口超时（{timeout_seconds:.0f}秒），"
        "登录可能被 Steam Guard 或网络问题拦住了"
    )


def is_steam_process_running() -> bool:
    """只认 Steam 主进程，渲染子进程短暂残留不影响判断。"""
    return bool(find_processes_by_names((STEAM_MAIN_PROCESS,)))


def minimize_steam_windows(keep_hwnds: Collection[int] = ()) -> int:
    """最小化 Steam 的界面窗口，``keep_hwnds`` 里的保持不动。

    Steam 主界面又大又爱抢前台，压在 EA 窗口上就是之前"关联账号点了没
    反应"的成因之一：鼠标点下去落在 Steam 上，页面当然不动。收起来之后
    它不会再盖住谁，PrintWindow 抓图也不受影响（抓的是窗口自身内容）。

    Steam 的窗口标题全是 ``Steam``（主界面、EULA 对话框、启动提示都一
    样），分不出哪个是"其他页面"，所以只按句柄排除——需要点击的那个窗
    口由调用方传进 keep_hwnds。
    """
    steam_windows = find_windows(STEAM_WINDOW_TITLE)
    minimized = minimize_windows(steam_windows, keep_hwnds)
    if minimized:
        log_info(
            f"已最小化 {minimized} 个 Steam 窗口，"
            f"保留 {len(keep_hwnds)} 个在前台"
        )
    return minimized


def close_steam_after_binding() -> None:
    """绑定完成后关闭 Steam（先正常退出，再强制结束）。"""
    steam_executable = find_steam_executable()
    if steam_executable is None:
        raise FileNotFoundError("未找到 steam.exe")
    if not is_steam_process_running():
        log_info("绑定完成后Steam已不在运行")
        return

    log_info("绑定完成，正在关闭Steam")
    subprocess.Popen(
        [str(steam_executable), "-shutdown"],
        cwd=str(steam_executable.parent),
    )
    deadline = time.monotonic() + STEAM_SHUTDOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not is_steam_process_running():
            return
        time.sleep(0.5)

    result = subprocess.run(
        ["taskkill", "/F", "/T", "/IM", "steam.exe"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    force_deadline = (
        time.monotonic() + STEAM_FORCE_SHUTDOWN_TIMEOUT_SECONDS
    )
    while time.monotonic() < force_deadline:
        if not is_steam_process_running():
            return
        time.sleep(0.5)

    raise RuntimeError(
        "Steam关闭失败，taskkill退出码="
        f"{result.returncode}"
    )


def click_screen_position(x: int, y: int) -> None:
    smooth_move_to(x, y)
    win32api.mouse_event(
        win32con.MOUSEEVENTF_LEFTDOWN,
        0,
        0,
        0,
        0,
    )
    time.sleep(random.uniform(0.05, 0.12))
    win32api.mouse_event(
        win32con.MOUSEEVENTF_LEFTUP,
        0,
        0,
        0,
        0,
    )


def _locate_eula_button(
    title_template: Any,
    button_template: Any,
) -> Optional[Tuple[int, int, int]]:
    """在所有标题为 Steam 的窗口中查找 EULA 接受按钮。

    返回 (hwnd, 按钮屏幕左坐标, 按钮屏幕上坐标)。
    """
    from binder.automation.window_capture import capture_window, find_template

    steam_windows = find_windows(STEAM_WINDOW_TITLE)
    debug_trace.log_window_enum(STEAM_WINDOW_TITLE, steam_windows)

    for hwnd in steam_windows:
        # 最小化的窗口既不可能显示 EULA 按钮，截出来的坐标也是错的
        # （窗口位置是 -32000 那种恢复位置），直接跳过，顺便避免每秒
        # 往 error.log 里记一条"窗口已最小化，无法截取"。
        if is_minimized_window(hwnd):
            continue

        try:
            screenshot, (window_left, window_top) = capture_window(
                hwnd,
                note=f"EULA_{hwnd}",
            )
        except Exception:
            log_exception(f"Steam EULA窗口截图失败，hwnd={hwnd}")
            continue

        if (
            find_template(
                screenshot,
                title_template,
                MATCH_THRESHOLD,
                "steam_eula_title",
            )
            is None
        ):
            continue

        button_match = find_template(
            screenshot,
            button_template,
            MATCH_THRESHOLD,
            "steam_eula_accept_button",
        )
        if button_match is None:
            continue

        button_x, button_y, _ = button_match
        return hwnd, window_left + button_x, window_top + button_y

    return None


def _positions_close(
    first: Tuple[int, int, int],
    second: Tuple[int, int, int],
) -> bool:
    return (
        first[0] == second[0]
        and abs(first[1] - second[1]) <= EULA_POSITION_TOLERANCE
        and abs(first[2] - second[2]) <= EULA_POSITION_TOLERANCE
    )


def _click_and_confirm_eula(
    hwnd: int,
    button_left: int,
    button_top: int,
    button_width: int,
    button_height: int,
    title_template: Any,
    button_template: Any,
) -> bool:
    """点击接受按钮并确认对话框消失；未消失则重试。"""
    from binder.automation.window_capture import capture_window, find_template

    for attempt in range(1, EULA_CLICK_RETRY_COUNT + 1):
        try:
            activate_window(hwnd)
        except Exception:
            log_exception(f"前置 Steam EULA 窗口失败，hwnd={hwnd}")

        click_x = button_left + random.randrange(max(1, button_width))
        click_y = button_top + random.randrange(max(1, button_height))
        click_screen_position(click_x, click_y)

        deadline = time.monotonic() + EULA_CONFIRM_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                screenshot, _ = capture_window(
                    hwnd,
                    note=f"EULA确认_{hwnd}",
                )
            except Exception:
                # 窗口已不可截取，视为对话框已关闭。
                return True

            if (
                find_template(
                    screenshot,
                    button_template,
                    MATCH_THRESHOLD,
                    "steam_eula_accept_button",
                )
                is None
            ):
                return True

        log_info(f"第{attempt}次点击 EULA 接受按钮后对话框仍存在")

    return False


def accept_eula_if_present() -> bool:
    """在限定时间内识别并接受 Steam EULA；未出现则返回 False。"""
    from binder.automation.window_capture import load_template

    title_template = load_template(TITLE_TEMPLATE_PATH)
    button_template = load_template(ACCEPT_BUTTON_TEMPLATE_PATH)
    button_height, button_width = button_template.shape[:2]

    discovery_deadline = (
        time.monotonic() + EULA_DISCOVERY_TIMEOUT_SECONDS
    )
    stability_deadline: Optional[float] = None
    stable_since: Optional[float] = None
    stable_signature: Optional[Tuple[int, int, int]] = None

    while True:
        now = time.monotonic()
        if stability_deadline is None and now >= discovery_deadline:
            log_info(
                f"{EULA_DISCOVERY_TIMEOUT_SECONDS:.0f}秒内未发现 "
                "Steam EULA 窗口，跳过"
            )
            return False
        if stability_deadline is not None and now >= stability_deadline:
            log_info("EULA 按钮位置始终不稳定，放弃自动接受")
            return False

        located = _locate_eula_button(title_template, button_template)
        if located is None:
            stable_since = None
            stable_signature = None
        else:
            hwnd, button_left, button_top = located
            if stability_deadline is None:
                stability_deadline = (
                    now + EULA_STABILITY_TIMEOUT_SECONDS
                )

            if (
                stable_signature is None
                or not _positions_close(stable_signature, located)
            ):
                stable_since = now
                stable_signature = located

            if (
                stable_since is not None
                and now - stable_since >= EULA_STABLE_SECONDS
            ):
                accepted = _click_and_confirm_eula(
                    hwnd,
                    button_left,
                    button_top,
                    button_width,
                    button_height,
                    title_template,
                    button_template,
                )
                if accepted:
                    log_info("已点击并确认 Steam 最终用户许可协议接受按钮")
                    return True

                stable_since = None
                stable_signature = None
                stability_deadline = (
                    time.monotonic() + EULA_STABILITY_TIMEOUT_SECONDS
                )

        time.sleep(EULA_POLL_INTERVAL_SECONDS)


def launch_steam(
    steam_executable: Path,
    steam_account: str,
    steam_password: str,
) -> None:
    subprocess.Popen(
        [
            str(steam_executable),
            "-noreactlogin",
            "-login",
            steam_account,
            steam_password,
        ],
        cwd=str(steam_executable.parent),
    )


def launch_battlefield_one() -> None:
    log_info(f"正在启动Battlefield 1，URI={GAME_URI}")
    os.startfile(GAME_URI)


def verify_steam_login(
    steam_account: str,
    previous_autologin: Optional[str],
    steam_dir: Optional[Path],
    timeout_seconds: float = STEAM_LOGIN_VERIFY_TIMEOUT_SECONDS,
) -> Tuple[str, Optional[str]]:
    """确认当前自动登录的账号就是目标账号。

    返回 (结论, 最后读到的自动登录账号)，结论取值见 STEAM_LOGIN_* 常量。

    读不到标记时只算"无法确认"（Steam 未勾选记住登录时本来就不留标记），
    不能据此断定账密有错 —— 那种情况会误伤好账号，而"信息有误"标记只能
    人工清除。只有确认仍停留在**上一个账号**才算切换失败。
    """
    target = steam_account.strip().casefold()
    previous = (previous_autologin or "").strip().casefold()
    deadline = time.monotonic() + timeout_seconds
    last_seen: Optional[str] = None

    while time.monotonic() < deadline:
        current = read_steam_autologin_account(steam_dir)
        last_seen = current
        if current and current.strip().casefold() == target:
            log_info(f"Steam 登录身份已确认 account={steam_account}")
            return STEAM_LOGIN_CONFIRMED, last_seen
        time.sleep(1.0)

    if (
        last_seen
        and previous
        and last_seen.strip().casefold() == previous
        and previous != target
    ):
        return STEAM_LOGIN_STALE, last_seen

    log_info(
        "未能确认 Steam 登录身份（可能未勾选记住登录），继续执行 "
        f"account={steam_account} last_autologin={last_seen}"
    )
    return STEAM_LOGIN_UNCONFIRMED, last_seen


def log_error_text_hint(hwnd: int) -> bool:
    """在 Steam 窗口里找红字错误提示，只记日志、不自动标记。

    Steam 主窗口里有不少红色元素（促销横幅、库里的红色角标），整窗扫描
    做不到 EA 那种"限定在输入框下方"的精度。而误判的代价是把好账号打上
    "信息有误"，只能人工清除 —— 太高了。所以这里只把线索写进日志，由
    人工核对后决定是否手动标记。
    """
    from binder.automation.error_text_detect import find_error_text
    from binder.automation.window_capture import capture_window

    try:
        screenshot, _ = capture_window(
            hwnd,
            note=f"steam_login_hint_{hwnd}",
        )
    except Exception:
        log_exception(f"Steam登录页截图失败，hwnd={hwnd}")
        return False

    evidence = find_error_text(screenshot)
    if evidence is None:
        return False

    log_info(
        "Steam窗口检测到红字，账密可能不正确；"
        "如已确认请右键标记为“信息有误” "
        f"{evidence.describe()}"
    )
    return True


def run_steam_stage(
    account_id: str,
    steam_account: str,
    steam_password: str,
    progress_queue: Any,
) -> bool:
    try:
        log_info(f"Steam阶段开始，account_id={account_id}")
        debug_trace.set_context(f"Steam阶段/{account_id}")
        debug_trace.dump_all_windows("Steam阶段开始")
        enable_dpi_awareness()

        if not steam_account or not steam_password:
            raise ValueError("Steam 账号或密码为空")

        steam_executable = find_steam_executable()
        if steam_executable is None:
            raise FileNotFoundError("未找到 steam.exe")

        steam_dir = find_steam_dir()
        previous_autologin = read_steam_autologin_account(steam_dir)

        # 前置清理：结束 Steam/EA 并清空两边登录态，
        # 避免下一个账号沿用上一个账号的会话（串号）。
        report_progress(progress_queue, account_id, "清理登录态中")
        prepare_account_environment()

        report_progress(progress_queue, account_id, "Steam登录中")
        launch_steam(steam_executable, steam_account, steam_password)
        steam_hwnd = wait_for_steam_window()

        report_progress(progress_queue, account_id, "校验Steam登录中")
        verdict, last_seen = verify_steam_login(
            steam_account,
            previous_autologin,
            steam_dir,
        )
        if verdict != STEAM_LOGIN_CONFIRMED:
            log_error_text_hint(steam_hwnd)
        if verdict == STEAM_LOGIN_STALE:
            raise AccountInfoError(
                "Steam账密",
                f"Steam 仍停留在上一个账号“{last_seen}”，"
                f"未切换到目标账号“{steam_account}”",
            )

        report_progress(progress_queue, account_id, "Steam登录成功")
        time.sleep(3.0)

        launch_battlefield_one()
        report_progress(progress_queue, account_id, "启动游戏中")
        eula_accepted = accept_eula_if_present()
        if eula_accepted:
            time.sleep(2.0)
            launch_battlefield_one()

        # Steam 这边的事都做完了：把它的界面收起来，后面是 EA 的舞台。
        # 放在 EULA 处理之后而不是之前 —— EULA 对话框是 Steam 主窗口弹
        # 出来的，先收主窗口有可能把它一起带下去，点了也白点。
        minimize_steam_windows()

        report_progress(progress_queue, account_id, "Steam阶段完成")
        return True
    except AccountInfoError as error:
        log_exception(f"Steam阶段账号信息有误，account_id={account_id}")
        report_progress(progress_queue, account_id, error.status)
        return False
    except Exception as error:
        log_exception(f"Steam阶段失败，account_id={account_id}")
        report_progress(
            progress_queue,
            account_id,
            f"失败：{type(error).__name__}: {error}",
        )
        return False
