"""EA 账号绑定阶段及页面策略。"""

from __future__ import annotations
import base64
import binascii
import ctypes
import hashlib
import hmac
import random
import struct
import time
from abc import ABC, abstractmethod
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import win32api
import win32con

from binder.automation import debug_trace
from binder.app_logging import log_exception, log_info
from binder.app_runtime import is_running_as_admin
from binder.automation.credential_cleanup import (
    EA_ANTI_CHEAT_PROCESS,
    EA_BACKGROUND_PROCESS,
    EA_GAME_PROCESS,
    EA_SERVICE_NAMES,
)
from binder.automation.error_text_detect import (
    ErrorTextTracker,
    region_below_input,
)
from binder.automation.mouse_motion import smooth_move_to
from binder.automation.process_utils import (
    find_processes,
    find_processes_started_after,
    wait_and_kill_process,
)
from binder.automation.win_utils import activate_window, find_windows, wait_before_action
from binder.flows.errors import AccountInfoError
from binder.paths import EA_TEMPLATE_DIR


EA_WINDOW_TITLE = "EA"
MATCH_THRESHOLD = 0.85
POLL_INTERVAL_SECONDS = 1.0
IDLE_TIMEOUT_SECONDS = 60.0
STRATEGY_RETRY_INTERVAL_SECONDS = 3.0
INPUT_FOCUS_TIMEOUT_SECONDS = 1.5
INPUT_FOCUS_RETRY_COUNT = 3
INPUT_FOCUS_SCORE_MARGIN = 0.01
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
EA_BACKGROUND_SERVICE = EA_SERVICE_NAMES[0]
EA_ANTI_CHEAT_WAIT_SECONDS = 60.0
EA_ANTI_CHEAT_PRE_KILL_DELAY_SECONDS = 2.0
EA_GAME_WAIT_SECONDS = 30.0
BOUND_SIGNAL_STABLE_SECONDS = 3.0
EA_BACKGROUND_WAIT_SECONDS = 30.0
EA_PROCESS_SHUTDOWN_INTERVAL_SECONDS = 5.0
TOTP_PERIOD_SECONDS = 30
TOTP_MIN_REMAINING_SECONDS = 5
REMEMBER_CHECK_MATCH_THRESHOLD = 0.75
# "Not your account?" 是小字号细体文字链接，比标题/输入框/按钮这类大
# 面积模板脆弱得多，所以单独放宽阈值。
CONFIRM_ACCOUNT_LINK_MATCH_THRESHOLD = 0.75
POST_INPUT_DETECTION_TIMEOUT_SECONDS = 2.0
POST_INPUT_DETECTION_INTERVAL_SECONDS = 0.2

# 进入 EA 阶段后先给登录页面留出的渲染时间，避免把"页面还没出来"
# 误判成"没有登录页 = 已绑定"。每次账号都会清空 EA 登录态，
# EA 冷启动渲染较慢，因此留得较宽。
BOUND_DETECTION_GRACE_SECONDS = 15.0
# 2FA 提交后，等待"关联页 / 游戏启动 / 重新登录"三种结果的时间。
POST_2FA_DECISION_TIMEOUT_SECONDS = 20.0
# 点击"关联账号"后，等待游戏/反作弊启动以确认绑定结果的时间。
LINK_VERIFY_TIMEOUT_SECONDS = 90.0
LINK_VERIFY_POLL_SECONDS = 1.0
LINK_VERIFY_LOG_INTERVAL_SECONDS = 5.0
# 设为 False 可放宽为"点击即成功"（不推荐）。
LINK_VERIFY_STRICT = True
# 关联页这一下是全流程唯一一次"孤立的第一次点击"（其它翻页按钮前面
# 0.3 秒内都先点过输入框），最容易被系统当成激活点击吃掉。所以点完
# 要先确认页面真的跳转，没跳转就重试，而不是点一次就上报成功、把
# 假失败留给后面的 90 秒超时。
LINK_CLICK_MAX_ATTEMPTS = 3
LINK_CLICK_CONFIRM_TIMEOUT_SECONDS = 2.5
LINK_CLICK_CONFIRM_INTERVAL_SECONDS = 0.5

# 同一个页面策略连续重复执行的次数上限。账密错误时页面不跳转，但标题、
# 输入框、按钮三个模板依旧全部匹配，于是策略会每 3 秒重输一次并刷新
# 活动时间 —— 60 秒空闲超时因此永远触发不了，表现为无限"卡住"。
# 这条上限不依赖红字检测，即使 EA 改版让检测失效也不会再空转。
LOGIN_STRATEGY_MAX_ATTEMPTS = 3

# 登录类页面上，输入框下方这个范围内的红字就是错误提示。
LOGIN_ERROR_REGION_VERTICAL_SPAN = 140
LOGIN_ERROR_REGION_HORIZONTAL_PAD = 40
LOGIN_ERROR_REQUIRED_HITS = 2
# 设为 False 可完全关掉红字判定（只保留下面的重复次数上限兜底）。
# 万一 EA 改版把这个区域改出了别的红色元素，先关这个再调区域参数。
LOGIN_ERROR_DETECTION_ENABLED = True
# 各登录页上的红字分别代表哪个字段有问题，用于给出可操作的失败原因。
LOGIN_ERROR_FIELDS = {
    "ea_email_input": "EA邮箱",
    "ea_password_input": "EA密码",
    "ea_code_input": "EA 2FA 16位码",
    "ea_verify_select": "EA登录",
}

LOGIN_PAGE_STRATEGY_NAMES = frozenset(
    {"ea_email_input", "ea_password_input"}
)


class EaPhase(Enum):
    """EA 阶段的显式状态。"""

    STARTUP = auto()
    PAGES = auto()
    POST_2FA = auto()


@dataclass(frozen=True)
class TemplateMatch:
    x: int
    y: int
    width: int
    height: int
    score: float
    label: str = ""


@dataclass(frozen=True)
class StrategyMatch:
    input_box: Optional[TemplateMatch] = None
    submit_button: Optional[TemplateMatch] = None
    optional_target: Optional[TemplateMatch] = None


@dataclass
class EaContext:
    account_id: str
    ea_email: str
    ea_password: str
    ea_2fa_secret: str
    progress_queue: Any
    hwnd: int
    screenshot: np.ndarray
    window_origin: Tuple[int, int]
    # 本次是否出现过 EA 邮箱/密码登录页。没出现过就说明 EA 里已经
    # 登录着某个账号，那不是本行要绑的对象。
    saw_login_page: bool = False


class KeyboardInput(ctypes.Structure):
    _fields_ = (
        ("virtual_key", wintypes.WORD),
        ("scan_code", wintypes.WORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("extra_info", wintypes.WPARAM),
    )


class MouseInput(ctypes.Structure):
    _fields_ = (
        ("x", wintypes.LONG),
        ("y", wintypes.LONG),
        ("mouse_data", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("extra_info", wintypes.WPARAM),
    )


class HardwareInput(ctypes.Structure):
    _fields_ = (
        ("message", wintypes.DWORD),
        ("parameter_low", wintypes.WORD),
        ("parameter_high", wintypes.WORD),
    )


class InputUnion(ctypes.Union):
    _fields_ = (
        ("mouse", MouseInput),
        ("keyboard", KeyboardInput),
        ("hardware", HardwareInput),
    )


class Input(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = (
        ("input_type", wintypes.DWORD),
        ("data", InputUnion),
    )


def report_progress(
    progress_queue: Any,
    account_id: str,
    status: str,
) -> None:
    progress_queue.put((account_id, status))


def click_match(
    match: TemplateMatch,
    window_origin: Tuple[int, int],
) -> None:
    margin_x = min(8, max(1, match.width // 10))
    margin_y = min(8, max(1, match.height // 10))
    left = window_origin[0] + match.x + margin_x
    top = window_origin[1] + match.y + margin_y
    usable_width = max(1, match.width - margin_x * 2)
    usable_height = max(1, match.height - margin_y * 2)
    click_x = left + random.randrange(usable_width)
    click_y = top + random.randrange(usable_height)

    log_info(
        f"点击 {match.label or '目标'} 屏幕坐标=({click_x}, {click_y}) "
        f"匹配分={match.score:.4f}"
    )

    smooth_move_to(click_x, click_y)
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


def _press_hotkey(modifier: int, key: int) -> None:
    win32api.keybd_event(modifier, 0, 0, 0)
    win32api.keybd_event(key, 0, 0, 0)
    win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(
        modifier,
        0,
        win32con.KEYEVENTF_KEYUP,
        0,
    )


def send_unicode_text(text: str) -> None:
    encoded_text = text.encode("utf-16-le")
    inputs = []
    for offset in range(0, len(encoded_text), 2):
        scan_code = int.from_bytes(
            encoded_text[offset : offset + 2],
            "little",
        )
        for key_up in (False, True):
            event = Input()
            event.input_type = INPUT_KEYBOARD
            event.keyboard = KeyboardInput(
                virtual_key=0,
                scan_code=scan_code,
                flags=(
                    KEYEVENTF_UNICODE
                    | (win32con.KEYEVENTF_KEYUP if key_up else 0)
                ),
                time=0,
                extra_info=0,
            )
            inputs.append(event)

    if not inputs:
        return

    input_array = (Input * len(inputs))(*inputs)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    send_input = user32.SendInput
    send_input.argtypes = (
        wintypes.UINT,
        ctypes.POINTER(Input),
        ctypes.c_int,
    )
    send_input.restype = wintypes.UINT
    ctypes.set_last_error(0)
    sent_count = send_input(
        len(input_array),
        input_array,
        ctypes.sizeof(Input),
    )
    if sent_count != len(input_array):
        error_code = ctypes.get_last_error()
        if error_code:
            raise ctypes.WinError(error_code)
        raise OSError(
            f"SendInput 仅发送了 {sent_count}/{len(input_array)} 个事件"
        )


def replace_focused_text(text: str) -> None:
    wait_before_action()
    _press_hotkey(win32con.VK_CONTROL, ord("A"))
    time.sleep(0.05)
    send_unicode_text(text)


def generate_totp(
    secret: str,
    timestamp: Optional[float] = None,
) -> str:
    normalized_secret = "".join(secret.split()).replace("-", "").upper()
    if not normalized_secret:
        raise ValueError("EA 2FA 16位码为空")

    padding = "=" * ((8 - len(normalized_secret) % 8) % 8)
    try:
        secret_bytes = base64.b32decode(
            normalized_secret + padding,
            casefold=True,
        )
    except (binascii.Error, ValueError, TypeError) as error:
        raise ValueError("EA 2FA 16位码不是有效的Base32密钥") from error

    current_time = time.time() if timestamp is None else timestamp
    counter = int(current_time // TOTP_PERIOD_SECONDS)
    digest = hmac.new(
        secret_bytes,
        struct.pack(">Q", counter),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 0x0F
    binary_code = (
        struct.unpack(">I", digest[offset : offset + 4])[0]
        & 0x7FFFFFFF
    )
    return f"{binary_code % 1_000_000:06d}"


def current_totp(secret: str) -> str:
    remaining = TOTP_PERIOD_SECONDS - (time.time() % TOTP_PERIOD_SECONDS)
    if remaining < TOTP_MIN_REMAINING_SECONDS:
        time.sleep(remaining + 0.2)
    return generate_totp(secret)


def load_templates() -> Dict[str, np.ndarray]:
    from binder.automation.window_capture import load_template

    template_names = (
        "ea_email_input_title",
        "ea_email_input_no_focus",
        "ea_email_input_focus",
        "ea_email_input_next_button",
        "ea_password_input_title",
        "ea_password_input_no_focus",
        "ea_password_input_focus",
        "ea_password_input_sign_button",
        "ea_verify_select_title",
        "ea_verify_select_no_focus",
        "ea_verify_select_focus",
        "ea_verify_select_send_code_button",
        "ea_code_input_title",
        "ea_code_input_no_focus",
        "ea_code_input_focus",
        "ea_code_input_remember_check_bot",
        "ea_code_input_next_button",
        "ea_link_steam_title",
        "ea_link_steam_link_accounts_button",
        "ea_confirm_account_title",
        "ea_confirm_account_not_you_account",
    )
    return {
        name: load_template(EA_TEMPLATE_DIR / f"{name}.png")
        for name in template_names
    }


def match_template(
    screenshot: np.ndarray,
    template: np.ndarray,
    threshold: float = MATCH_THRESHOLD,
    label: str = "",
) -> Optional[TemplateMatch]:
    from binder.automation.window_capture import find_template

    result = find_template(screenshot, template, threshold, label)
    if result is None:
        return None

    x, y, score = result
    height, width = template.shape[:2]
    return TemplateMatch(x, y, width, height, score, label)


def best_template_match(
    screenshot: np.ndarray,
    labeled_templates: Sequence[Tuple[str, np.ndarray]],
) -> Optional[TemplateMatch]:
    matches = [
        match
        for label, template in labeled_templates
        if (match := match_template(screenshot, template, label=label))
        is not None
    ]
    if not matches:
        return None
    return max(matches, key=lambda match: match.score)


def anti_cheat_started_after(since_timestamp: float) -> bool:
    """反作弊启动器在 since_timestamp 之后启动。

    只用反作弊启动器作为信号：游戏本体(bf1.exe)在 Steam 阶段就被拉起，
    与是否已绑定无关，用它判断会误判。
    """
    return bool(
        find_processes_started_after(
            EA_ANTI_CHEAT_PROCESS,
            since_timestamp,
        )
    )


class EaPageStrategy(ABC):
    name: str
    progress_status: str
    title_template_name: str
    input_template_names: Tuple[str, str]
    button_template_name: str
    completes_page_flow = False

    def __init__(self, templates: Dict[str, np.ndarray]) -> None:
        self.templates = templates

    def match(self, screenshot: np.ndarray) -> Optional[StrategyMatch]:
        title_match = match_template(
            screenshot,
            self.templates[self.title_template_name],
            label=self.title_template_name,
        )
        if title_match is None:
            return None

        input_match = best_template_match(
            screenshot,
            [
                (name, self.templates[name])
                for name in self.input_template_names
            ],
        )
        if input_match is None:
            return None

        button_match = match_template(
            screenshot,
            self.templates[self.button_template_name],
            label=self.button_template_name,
        )
        if button_match is None:
            return None

        return StrategyMatch(input_match, button_match)

    def input_has_focus(self, screenshot: np.ndarray) -> bool:
        focused_match = match_template(
            screenshot,
            self.templates[self.input_template_names[0]],
            label=self.input_template_names[0],
        )
        if focused_match is None:
            return False

        unfocused_match = match_template(
            screenshot,
            self.templates[self.input_template_names[1]],
            label=self.input_template_names[1],
        )
        return (
            unfocused_match is None
            or focused_match.score
            >= unfocused_match.score + INPUT_FOCUS_SCORE_MARGIN
        )

    def focus_input(
        self,
        context: EaContext,
        input_match: TemplateMatch,
    ) -> None:
        """点击输入框并确认焦点，防止 Ctrl+A 被整个页面接收。"""
        for _ in range(INPUT_FOCUS_RETRY_COUNT):
            activate_window(context.hwnd)
            click_match(input_match, context.window_origin)

            deadline = time.monotonic() + INPUT_FOCUS_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.2)
                screenshot, _ = capture_ea_window(context.hwnd)
                if self.input_has_focus(screenshot):
                    return

        raise RuntimeError(
            f"{self.progress_status}时输入框未获得焦点"
        )

    def execute(
        self,
        context: EaContext,
        strategy_match: StrategyMatch,
    ) -> None:
        report_progress(
            context.progress_queue,
            context.account_id,
            self.progress_status,
        )
        if (
            strategy_match.input_box is None
            or strategy_match.submit_button is None
        ):
            raise RuntimeError(f"{self.name}策略缺少点击目标")
        self.focus_input(context, strategy_match.input_box)
        replace_focused_text(self.input_value(context))
        time.sleep(0.2)
        click_match(strategy_match.submit_button, context.window_origin)

    @abstractmethod
    def input_value(self, context: EaContext) -> str:
        raise NotImplementedError


class EaEmailInputStrategy(EaPageStrategy):
    name = "ea_email_input"
    progress_status = "EA输入邮箱中"
    title_template_name = "ea_email_input_title"
    input_template_names = (
        "ea_email_input_focus",
        "ea_email_input_no_focus",
    )
    button_template_name = "ea_email_input_next_button"

    def input_value(self, context: EaContext) -> str:
        if not context.ea_email:
            raise ValueError("EA 邮箱为空")
        return context.ea_email


class EaPasswordInputStrategy(EaPageStrategy):
    name = "ea_password_input"
    progress_status = "EA输入密码中"
    title_template_name = "ea_password_input_title"
    input_template_names = (
        "ea_password_input_focus",
        "ea_password_input_no_focus",
    )
    button_template_name = "ea_password_input_sign_button"

    def input_value(self, context: EaContext) -> str:
        if not context.ea_password:
            raise ValueError("EA 密码为空")
        return context.ea_password


class EaVerifySelectStrategy(EaPageStrategy):
    name = "ea_verify_select"
    progress_status = "EA验证身份中"
    title_template_name = "ea_verify_select_title"
    input_template_names = (
        "ea_verify_select_focus",
        "ea_verify_select_no_focus",
    )
    button_template_name = "ea_verify_select_send_code_button"

    def execute(
        self,
        context: EaContext,
        strategy_match: StrategyMatch,
    ) -> None:
        if (
            strategy_match.input_box is None
            or strategy_match.submit_button is None
        ):
            raise RuntimeError("EA验证身份策略缺少点击目标")

        report_progress(
            context.progress_queue,
            context.account_id,
            self.progress_status,
        )
        activate_window(context.hwnd)
        click_match(strategy_match.input_box, context.window_origin)
        time.sleep(0.3)
        click_match(strategy_match.submit_button, context.window_origin)

    def input_value(self, context: EaContext) -> str:
        return ""


class EaCodeInputStrategy(EaPageStrategy):
    name = "ea_code_input"
    progress_status = "EA输入验证码中"
    title_template_name = "ea_code_input_title"
    input_template_names = (
        "ea_code_input_focus",
        "ea_code_input_no_focus",
    )
    button_template_name = "ea_code_input_next_button"

    def execute(
        self,
        context: EaContext,
        strategy_match: StrategyMatch,
    ) -> None:
        if (
            strategy_match.input_box is None
            or strategy_match.submit_button is None
        ):
            raise RuntimeError("EA验证码策略缺少点击目标")

        report_progress(
            context.progress_queue,
            context.account_id,
            self.progress_status,
        )
        self.focus_input(context, strategy_match.input_box)
        replace_focused_text(self.input_value(context))
        time.sleep(0.3)

        latest_origin = context.window_origin
        latest_button = strategy_match.submit_button
        checkbox_clicked = False
        deadline = (
            time.monotonic() + POST_INPUT_DETECTION_TIMEOUT_SECONDS
        )
        while time.monotonic() < deadline:
            screenshot, latest_origin = capture_ea_window(context.hwnd)
            remember_checkbox = match_template(
                screenshot,
                self.templates["ea_code_input_remember_check_bot"],
                REMEMBER_CHECK_MATCH_THRESHOLD,
                label="ea_code_input_remember_check_bot",
            )
            refreshed_button = match_template(
                screenshot,
                self.templates[self.button_template_name],
                label=self.button_template_name,
            )
            if refreshed_button is not None:
                latest_button = refreshed_button

            if remember_checkbox is not None:
                click_match(remember_checkbox, latest_origin)
                checkbox_clicked = True
                log_info(
                    "EA验证码页面已取消记住设备，"
                    f"account_id={context.account_id}"
                )
                time.sleep(0.3)
                break

            time.sleep(POST_INPUT_DETECTION_INTERVAL_SECONDS)

        if not checkbox_clicked:
            log_info(
                "EA验证码页面未识别到已勾选的记住设备，"
                f"account_id={context.account_id}"
            )

        screenshot, latest_origin = capture_ea_window(context.hwnd)
        refreshed_button = match_template(
            screenshot,
            self.templates[self.button_template_name],
            label=self.button_template_name,
        )
        if refreshed_button is not None:
            latest_button = refreshed_button
        click_match(latest_button, latest_origin)

    def input_value(self, context: EaContext) -> str:
        return current_totp(context.ea_2fa_secret)


class EaLinkSteamStrategy(EaPageStrategy):
    name = "ea_link_steam"
    progress_status = "EA绑定Steam中"
    completes_page_flow = True
    title_template_name = "ea_link_steam_title"
    input_template_names = ("", "")
    button_template_name = "ea_link_steam_link_accounts_button"

    def match(self, screenshot: np.ndarray) -> Optional[StrategyMatch]:
        title_match = match_template(
            screenshot,
            self.templates[self.title_template_name],
            label=self.title_template_name,
        )
        if title_match is None:
            return None

        button_match = match_template(
            screenshot,
            self.templates[self.button_template_name],
            label=self.button_template_name,
        )
        if button_match is None:
            return None
        return StrategyMatch(submit_button=button_match)

    def require_authenticated_login(self, context: EaContext) -> None:
        """关联前必须确认这次是我们自己登录进 EA 的。

        EA 里若残留着别的账号的登录态，就不会出现邮箱/密码页，流程
        会直接走到关联页；此时点"关联账号"会把本行的 Steam 账号绑到
        那个不属于本行的 EA 账号上，而且界面上看不出任何异常。
        """
        if context.saw_login_page:
            log_info(
                "已确认本次经EA登录页登录，关联对象可信 "
                f"account_id={context.account_id}"
            )
            return

        raise RuntimeError(
            "未出现EA登录页就直接到达关联页，EA可能仍登录着其它账号，"
            "无法确认关联对象，已中止以免绑错账号；"
            "请确认EA已退出登录后重试"
        )

    def execute(
        self,
        context: EaContext,
        strategy_match: StrategyMatch,
    ) -> None:
        if strategy_match.submit_button is None:
            raise RuntimeError("EA绑定Steam策略缺少按钮目标")

        self.require_authenticated_login(context)

        report_progress(
            context.progress_queue,
            context.account_id,
            self.progress_status,
        )

        button_match = strategy_match.submit_button
        window_origin = context.window_origin
        for attempt in range(1, LINK_CLICK_MAX_ATTEMPTS + 1):
            if not activate_window(context.hwnd):
                log_info(
                    "关联账号点击前EA窗口未确认到前台，"
                    f"account_id={context.account_id} 第{attempt}次"
                )
            click_match(button_match, window_origin)

            advanced, button_match, window_origin = self.confirm_page_gone(
                context,
                button_match,
                window_origin,
            )
            if advanced:
                log_info(
                    "关联账号按钮点击已生效，页面已跳转 "
                    f"account_id={context.account_id} 第{attempt}次点击"
                )
                return
            log_info(
                "关联账号页面未跳转，重新点击 "
                f"account_id={context.account_id} 第{attempt}次"
            )

        log_info(
            f"关联账号按钮点击{LINK_CLICK_MAX_ATTEMPTS}次后页面仍未跳转，"
            f"继续等待反作弊启动器确认 account_id={context.account_id}"
        )

    def confirm_page_gone(
        self,
        context: EaContext,
        fallback_button: TemplateMatch,
        fallback_origin: Tuple[int, int],
    ) -> Tuple[bool, TemplateMatch, Tuple[int, int]]:
        """确认点击是否生效，未生效时返回最新按钮位置供重试。"""
        deadline = time.monotonic() + LINK_CLICK_CONFIRM_TIMEOUT_SECONDS
        while True:
            time.sleep(LINK_CLICK_CONFIRM_INTERVAL_SECONDS)
            try:
                screenshot, origin = capture_ea_window(context.hwnd)
            except Exception:
                log_exception(
                    "关联账号点击后截图失败，"
                    f"account_id={context.account_id}, hwnd={context.hwnd}"
                )
                if time.monotonic() >= deadline:
                    return False, fallback_button, fallback_origin
                continue

            if (
                match_template(
                    screenshot,
                    self.templates[self.title_template_name],
                    label=self.title_template_name,
                )
                is None
            ):
                return True, fallback_button, fallback_origin

            button_match = match_template(
                screenshot,
                self.templates[self.button_template_name],
                label=self.button_template_name,
            )
            if button_match is not None:
                fallback_button = button_match
                fallback_origin = origin

            if time.monotonic() >= deadline:
                return False, fallback_button, fallback_origin

    def input_value(self, context: EaContext) -> str:
        return ""


class EaConfirmAccountStrategy(EaPageStrategy):
    """EA 仍记着上一个账号时的"确认关联账号"页：点"Not your account?"换号。

    这页是登录态没被彻底清掉时的第一屏，长这样：标题 "Confirm your EA
    Account"、上一行账号的 gamertag、蓝色链接 "Not your account?"，以及
    一个通栏蓝色 CONTINUE 按钮。点链接会把上一个账号登出、回到邮箱页，
    后面的登录流程照旧。

    **绝不能点 CONTINUE**：点下去就是"就用上一个账号关联 Steam"，正是要
    避免的串号。这个按钮还和密码页的 Sign in 按钮像素级相似 —— 实测截图
    里 CONTINUE 在 ea_password_input_sign_button 模板上得 0.877 分，高于
    全局阈值 0.85。所以按钮不能当判据，这里只认标题 + 链接两个模板，且
    标题命中而链接没命中时什么都不做：宁可超时，也不乱点。
    """

    name = "ea_confirm_account"
    progress_status = "EA切换账号中"
    title_template_name = "ea_confirm_account_title"
    input_template_names = ("", "")
    button_template_name = "ea_confirm_account_not_you_account"

    def match(self, screenshot: np.ndarray) -> Optional[StrategyMatch]:
        title_match = match_template(
            screenshot,
            self.templates[self.title_template_name],
            label=self.title_template_name,
        )
        if title_match is None:
            return None

        link_match = match_template(
            screenshot,
            self.templates[self.button_template_name],
            threshold=CONFIRM_ACCOUNT_LINK_MATCH_THRESHOLD,
            label=self.button_template_name,
        )
        if link_match is None:
            return None
        return StrategyMatch(submit_button=link_match)

    def execute(
        self,
        context: EaContext,
        strategy_match: StrategyMatch,
    ) -> None:
        if strategy_match.submit_button is None:
            raise RuntimeError("EA切换账号策略缺少点击目标")

        report_progress(
            context.progress_queue,
            context.account_id,
            self.progress_status,
        )
        log_info(
            "EA 仍停留在上一个账号的关联确认页，"
            f"点击 Not your account? 换号 account_id={context.account_id}"
        )
        # 这是孤立的第一下点击（前面没有点过输入框），最容易被系统当成
        # 激活点击吃掉；点击没生效时主循环会再匹配到本页并重试。
        activate_window(context.hwnd)
        click_match(strategy_match.submit_button, context.window_origin)

    def input_value(self, context: EaContext) -> str:
        return ""


def decide_post_2fa(
    screenshot: np.ndarray,
    templates: Dict[str, np.ndarray],
    submitted_at: float,
) -> Optional[str]:
    """2FA 提交后判断后续走向。

    返回 "link" / "bound" / "relogin"，都未命中时返回 None。
    """
    if (
        match_template(
            screenshot,
            templates["ea_link_steam_title"],
            label="ea_link_steam_title",
        )
        is not None
    ):
        return "link"

    if anti_cheat_started_after(submitted_at):
        return "bound"

    for title_name in ("ea_email_input_title", "ea_password_input_title"):
        if (
            match_template(
                screenshot,
                templates[title_name],
                label=title_name,
            )
            is not None
        ):
            return "relogin"

    return None


def capture_after_link_click(
    hwnd: Optional[int],
    account_id: str,
) -> None:
    """点击关联账号后立刻落一张截图，事后可判断页面到底跳没跳。"""
    if hwnd is None:
        return
    try:
        with debug_trace.force_frames():
            capture_ea_window(hwnd, note="link_clicked")
    except Exception:
        log_exception(f"关联账号点击后截图失败，account_id={account_id}")


def verify_link_result(
    link_clicked_at: float,
    account_id: str,
    hwnd: Optional[int] = None,
) -> None:
    """点击关联账号后，等待反作弊启动器启动以确认绑定生效。

    ``link_clicked_at`` 必须是 epoch 秒（``time.time()``）：它要和
    psutil 的 ``create_time()`` 比大小，传 ``time.monotonic()`` 会让
    "点击之后才启动"这个条件恒为真，校验就退化成"进程是否存在"。
    """
    if not LINK_VERIFY_STRICT:
        log_info("已关闭关联结果校验，直接按成功处理")
        return

    capture_after_link_click(hwnd, account_id)

    started_at = time.monotonic()
    deadline = started_at + LINK_VERIFY_TIMEOUT_SECONDS
    next_log_at = started_at + LINK_VERIFY_LOG_INTERVAL_SECONDS
    while time.monotonic() < deadline:
        if anti_cheat_started_after(link_clicked_at):
            log_info(
                "关联后检测到反作弊启动器启动，绑定结果已确认 "
                f"account_id={account_id} "
                f"用时={time.monotonic() - started_at:.1f}s"
            )
            return

        now = time.monotonic()
        if now >= next_log_at:
            log_info(
                f"等待反作弊启动器启动 account_id={account_id} "
                f"已等待 {now - started_at:.0f}s"
            )
            next_log_at += LINK_VERIFY_LOG_INTERVAL_SECONDS
        time.sleep(LINK_VERIFY_POLL_SECONDS)

    raise TimeoutError(
        "点击关联账号后 "
        f"{LINK_VERIFY_TIMEOUT_SECONDS:.0f} 秒内未检测到反作弊启动器启动，"
        "无法确认绑定结果；如确认已绑定可右键手动标记"
    )


def close_ea_game(account_id: str) -> None:
    """先关游戏本体再关反作弊。

    游戏还在跑的话，反作弊启动器被关掉后会被它重新拉起来，导致
    ``wait_and_kill_process`` 一直重杀直到超时报错。这里关不掉只记
    警告：此时绑定已经成功，收尾失败不该反过来把账号判成失败，
    后面的 ``cleanup_after_account`` 还会再收一次。
    """
    if not find_processes(EA_GAME_PROCESS):
        log_info("未检测到游戏进程，跳过关闭")
        return

    try:
        closed = wait_and_kill_process(
            EA_GAME_PROCESS,
            EA_GAME_WAIT_SECONDS,
        )
    except TimeoutError:
        log_exception(
            f"游戏进程未能关闭，交给账号收尾清理重试 account_id={account_id}"
        )
        return

    if closed:
        log_info("游戏进程已关闭")
    else:
        log_info("未检测到游戏进程，跳过关闭")


def finish_ea_binding(
    account_id: str,
    progress_queue: Any,
) -> None:
    if not is_running_as_admin():
        raise PermissionError(
            "当前程序不是管理员权限，无法关闭EA后台进程"
        )

    close_ea_game(account_id)

    anti_cheat_closed = wait_and_kill_process(
        EA_ANTI_CHEAT_PROCESS,
        EA_ANTI_CHEAT_WAIT_SECONDS,
        pre_kill_delay_seconds=EA_ANTI_CHEAT_PRE_KILL_DELAY_SECONDS,
    )
    if anti_cheat_closed:
        log_info(
            "EAAntiCheat.GameServiceLauncher已关闭，"
            f"等待{EA_PROCESS_SHUTDOWN_INTERVAL_SECONDS:.0f}秒"
        )
        time.sleep(EA_PROCESS_SHUTDOWN_INTERVAL_SECONDS)
    else:
        log_info(
            "未检测到 EAAntiCheat.GameServiceLauncher，跳过关闭"
        )

    background_closed = wait_and_kill_process(
        EA_BACKGROUND_PROCESS,
        EA_BACKGROUND_WAIT_SECONDS,
        service_name=EA_BACKGROUND_SERVICE,
    )
    if not background_closed:
        log_info("未检测到 EABackgroundService，跳过关闭")

    report_progress(progress_queue, account_id, "已绑定")


def capture_ea_window(
    hwnd: int,
    note: Optional[str] = None,
) -> Tuple[np.ndarray, Tuple[int, int]]:
    from binder.automation.window_capture import capture_window

    return capture_window(hwnd, note=note)


def detect_page_error(
    screenshot: np.ndarray,
    strategies: Sequence[EaPageStrategy],
    trackers: Dict[str, ErrorTextTracker],
) -> Optional[AccountInfoError]:
    """在登录类页面的输入框下方查找红字错误提示。

    只有"当前截图确实匹配到某个登录类策略"（说明正处在登录环节）并且
    该区域连续多帧出现红字，才判定账号资料有误。没匹配到的策略会把
    自己的计数清零，避免上一次的命中凑出假阳性。
    """
    if not LOGIN_ERROR_DETECTION_ENABLED:
        return None

    for strategy in strategies:
        field = LOGIN_ERROR_FIELDS.get(strategy.name)
        tracker = trackers.get(strategy.name)
        if field is None or tracker is None:
            continue

        strategy_match = strategy.match(screenshot)
        if strategy_match is None or strategy_match.input_box is None:
            tracker.reset()
            continue

        input_box = strategy_match.input_box
        region = region_below_input(
            input_box.x,
            input_box.y,
            input_box.width,
            input_box.height,
            LOGIN_ERROR_REGION_VERTICAL_SPAN,
            LOGIN_ERROR_REGION_HORIZONTAL_PAD,
        )
        evidence = tracker.observe(screenshot, region)
        if evidence is not None:
            log_info(
                f"检测到{field}页面的红字错误提示：{evidence.describe()}"
            )
            return AccountInfoError(field, evidence.describe())
    return None


def run_ea_stage(
    account_id: str,
    ea_email: str,
    ea_password: str,
    ea_2fa_secret: str,
    progress_queue: Any,
    started_after: Optional[float] = None,
) -> bool:
    try:
        log_info(f"EA阶段开始，account_id={account_id}")
        debug_trace.set_context(f"EA阶段/{account_id}")
        debug_trace.dump_all_windows("EA阶段开始")
        templates = load_templates()
        strategies: List[EaPageStrategy] = [
            EaEmailInputStrategy(templates),
            EaPasswordInputStrategy(templates),
            EaVerifySelectStrategy(templates),
            EaCodeInputStrategy(templates),
            EaLinkSteamStrategy(templates),
            # 换号页放最后：它出现时其它页面的模板都不会命中（实测过），
            # 所以位置只影响日志顺序；放最后也让"卡在登录页"的测试语义
            # 保持不变。
            EaConfirmAccountStrategy(templates),
        ]
        last_strategy_run: Dict[str, float] = {}
        last_strategy_activity_at = time.monotonic()
        ea_stage_started_at = time.monotonic()
        active_hwnd: Optional[int] = None
        process_since = (
            time.time() if started_after is None else started_after
        )

        phase = EaPhase.STARTUP
        saw_login_page = False
        bound_signal_since: Optional[float] = None
        post_2fa_submitted_at: Optional[float] = None
        error_trackers = {
            name: ErrorTextTracker(LOGIN_ERROR_REQUIRED_HITS)
            for name in LOGIN_ERROR_FIELDS
        }
        last_executed_strategy: Optional[str] = None
        strategy_attempts: Dict[str, int] = {}

        report_progress(progress_queue, account_id, "EA启动中")

        while (
            time.monotonic() - last_strategy_activity_at
            < IDLE_TIMEOUT_SECONDS
        ):
            # 已绑定账号：从未出现邮箱/密码登录页，游戏直接拉起反作弊进程。
            if not saw_login_page:
                debug_trace.set_context(
                    f"EA阶段/{account_id}/已绑定检测"
                )
                grace_elapsed = (
                    time.monotonic() - ea_stage_started_at
                    >= BOUND_DETECTION_GRACE_SECONDS
                )
                if grace_elapsed and anti_cheat_started_after(process_since):
                    signal_now = time.monotonic()
                    if bound_signal_since is None:
                        bound_signal_since = signal_now
                    elif (
                        signal_now - bound_signal_since
                        >= BOUND_SIGNAL_STABLE_SECONDS
                    ):
                        log_info(
                            "未出现EA登录页面但检测到反作弊启动器启动，"
                            f"判定账号已绑定，account_id={account_id}"
                        )
                        report_progress(
                            progress_queue,
                            account_id,
                            "检测到已绑定，收尾中",
                        )
                        finish_ea_binding(account_id, progress_queue)
                        return True
                else:
                    bound_signal_since = None

            ea_windows = find_windows(EA_WINDOW_TITLE)
            debug_trace.log_window_enum(EA_WINDOW_TITLE, ea_windows)
            if not ea_windows:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            hwnd = ea_windows[0]
            if hwnd != active_hwnd:
                activate_window(hwnd)
                active_hwnd = hwnd
                # Steam 阶段结束时已经收过它的界面，但启动游戏会让 Steam
                # 自己再弹一次（"正在准备启动"之类的窗口，标题同样是
                # Steam）。这里再收一遍，保证前台只有 EA。
                from binder.flows.steam_flow import minimize_steam_windows

                minimize_steam_windows()
                report_progress(
                    progress_queue,
                    account_id,
                    "EA启动完成",
                )
                time.sleep(0.3)

            try:
                screenshot, window_origin = capture_ea_window(hwnd)
            except Exception:
                log_exception(
                    f"EA窗口截图失败，account_id={account_id}, hwnd={hwnd}"
                )
                try:
                    activate_window(hwnd)
                except Exception:
                    log_exception(
                        "EA窗口截图失败后重新前置窗口也失败，"
                        f"account_id={account_id}, hwnd={hwnd}"
                    )
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            now = time.monotonic()

            # 红字错误提示优先于其它判断：它是"账号资料不对"的直接证据，
            # 比 2FA 后 20 秒的通用超时更早、更能说明原因。
            credential_error = detect_page_error(
                screenshot,
                strategies,
                error_trackers,
            )
            if credential_error is not None:
                raise credential_error

            # 2FA 提交后：在"关联页 / 游戏启动 / 重新登录"之间做决策。
            if (
                phase is EaPhase.POST_2FA
                and post_2fa_submitted_at is not None
            ):
                debug_trace.set_context(f"EA阶段/{account_id}/POST_2FA")
                outcome = decide_post_2fa(
                    screenshot,
                    templates,
                    post_2fa_submitted_at,
                )
                if outcome == "bound":
                    log_info(
                        "2FA 后未出现关联页但检测到反作弊启动器启动，"
                        f"判定无需关联，account_id={account_id}"
                    )
                    report_progress(
                        progress_queue,
                        account_id,
                        "检测到已绑定，收尾中",
                    )
                    finish_ea_binding(account_id, progress_queue)
                    return True
                if outcome == "relogin":
                    raise RuntimeError(
                        "2FA 提交后回到登录页，EA 账号或密码可能不正确"
                    )
                if outcome == "link":
                    phase = EaPhase.PAGES
                elif (
                    now - post_2fa_submitted_at
                    > POST_2FA_DECISION_TIMEOUT_SECONDS
                ):
                    raise TimeoutError(
                        "2FA 提交后 "
                        f"{POST_2FA_DECISION_TIMEOUT_SECONDS:.0f} 秒内"
                        "未识别到关联页，也未检测到反作弊启动器启动"
                    )
                else:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

            for strategy in strategies:
                debug_trace.set_context(
                    f"EA阶段/{account_id}/{strategy.name}"
                )
                strategy_match = strategy.match(screenshot)
                if strategy_match is None:
                    continue

                if strategy.name in LOGIN_PAGE_STRATEGY_NAMES:
                    saw_login_page = True

                last_run_at = last_strategy_run.get(strategy.name)
                if (
                    last_run_at is not None
                    and now - last_run_at
                    < STRATEGY_RETRY_INTERVAL_SECONDS
                ):
                    # 只跳过这一个策略：用 break 会把列表后半段（含
                    # ea_link_steam）整轮一起跳过，导致页面明明匹配上
                    # 却迟迟点不到。
                    continue

                # 同一页面连续重复执行说明页面没跳转。超过上限就停下来：
                # 这里没有红字证据，所以按普通失败上报（不自动标记"信息
                # 有误"），由人工核对后决定是否手动标记。
                if last_executed_strategy == strategy.name:
                    strategy_attempts[strategy.name] = (
                        strategy_attempts.get(strategy.name, 0) + 1
                    )
                else:
                    strategy_attempts[strategy.name] = 1
                if (
                    strategy_attempts[strategy.name]
                    > LOGIN_STRATEGY_MAX_ATTEMPTS
                ):
                    # 只有登录类页面才能说"账号或密码可能不正确"；换号页
                    # 反复不跳转跟账密无关，报那句话会把人带偏。
                    hint = (
                        "账号或密码可能不正确；如确认请右键标记为"
                        "“信息有误”"
                        if strategy.name in LOGIN_ERROR_FIELDS
                        else "页面未跳转，EA 界面可能已改版或被其它窗口遮挡"
                    )
                    raise RuntimeError(
                        f"{strategy.progress_status}已连续 "
                        f"{LOGIN_STRATEGY_MAX_ATTEMPTS} 次未进入下一步，"
                        f"{hint}"
                    )
                last_executed_strategy = strategy.name

                context = EaContext(
                    account_id=account_id,
                    ea_email=ea_email,
                    ea_password=ea_password,
                    ea_2fa_secret=ea_2fa_secret,
                    progress_queue=progress_queue,
                    hwnd=hwnd,
                    screenshot=screenshot,
                    window_origin=window_origin,
                    saw_login_page=saw_login_page,
                )
                # 点击发起前先取时间戳：反作弊启动器只要晚于这个时刻
                # 启动就算点击生效。必须用 epoch 秒，psutil 的
                # create_time() 是同一时钟；也不能等 execute 返回再取，
                # 否则重试期间启动的进程会被判成"点击之前"而漏掉。
                click_started_at = time.time()
                strategy.execute(context, strategy_match)
                last_strategy_run[strategy.name] = time.monotonic()
                last_strategy_activity_at = time.monotonic()

                if strategy.name == "ea_code_input":
                    phase = EaPhase.POST_2FA
                    post_2fa_submitted_at = time.monotonic()
                elif strategy.completes_page_flow:
                    report_progress(
                        progress_queue,
                        account_id,
                        "校验关联结果中",
                    )
                    verify_link_result(click_started_at, account_id, hwnd)
                    finish_ea_binding(account_id, progress_queue)
                    return True
                break

            time.sleep(POLL_INTERVAL_SECONDS)

        raise TimeoutError("EA阶段连续60秒未匹配到可执行策略")
    except AccountInfoError as error:
        # 账号资料问题：立刻停下并让界面标记为"信息有误"，
        # 后续轮次自动跳过，不再每轮空转重试。
        log_exception(f"EA阶段账号信息有误，account_id={account_id}")
        report_progress(progress_queue, account_id, error.status)
        return False
    except Exception as error:
        log_exception(f"EA阶段失败，account_id={account_id}")
        report_progress(
            progress_queue,
            account_id,
            f"失败：{type(error).__name__}: {error}",
        )
        return False
