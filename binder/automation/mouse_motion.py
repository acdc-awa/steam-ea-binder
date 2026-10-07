"""Windows 鼠标平滑移动工具。"""

from __future__ import annotations
import math
import random
import time
from typing import Optional

import win32api


MOVE_DURATION_MIN_SECONDS = 0.1
MOVE_DURATION_MAX_SECONDS = 0.5
MOVE_FRAME_RATE = 60
MAX_CURVE_OFFSET_PIXELS = 48.0
MAX_JITTER_PIXELS = 3.0


def smooth_move_to(
    target_x: int,
    target_y: int,
    duration: Optional[float] = None,
) -> None:
    """沿带轻微随机弯曲和抖动的轨迹移动到目标位置。"""
    start_x, start_y = win32api.GetCursorPos()
    move_duration = (
        random.uniform(
            MOVE_DURATION_MIN_SECONDS,
            MOVE_DURATION_MAX_SECONDS,
        )
        if duration is None
        else max(0.0, duration)
    )
    steps = max(2, int(move_duration * MOVE_FRAME_RATE))
    sleep_interval = move_duration / steps
    delta_x = target_x - start_x
    delta_y = target_y - start_y
    distance = math.hypot(delta_x, delta_y)

    if distance > 0:
        normal_x = -delta_y / distance
        normal_y = delta_x / distance
    else:
        normal_x = 0.0
        normal_y = 0.0

    curve_limit = min(
        MAX_CURVE_OFFSET_PIXELS,
        max(2.0, distance * 0.16),
    )
    curve_offset = random.uniform(-curve_limit, curve_limit)
    control_1_x = start_x + delta_x * 0.30 + normal_x * curve_offset
    control_1_y = start_y + delta_y * 0.30 + normal_y * curve_offset
    control_2_x = target_x - delta_x * 0.28 + normal_x * curve_offset * 0.55
    control_2_y = target_y - delta_y * 0.28 + normal_y * curve_offset * 0.55
    jitter_amplitude = min(
        MAX_JITTER_PIXELS,
        max(0.35, distance * 0.012),
    )
    jitter_phase = random.uniform(0.0, math.tau)
    jitter_frequency = random.uniform(1.5, 3.0)

    for step in range(1, steps + 1):
        if sleep_interval > 0:
            time.sleep(sleep_interval)
        progress = step / steps
        eased = progress * progress * (3.0 - 2.0 * progress)
        inverse = 1.0 - eased
        curve_x = (
            inverse ** 3 * start_x
            + 3.0 * inverse ** 2 * eased * control_1_x
            + 3.0 * inverse * eased ** 2 * control_2_x
            + eased ** 3 * target_x
        )
        curve_y = (
            inverse ** 3 * start_y
            + 3.0 * inverse ** 2 * eased * control_1_y
            + 3.0 * inverse * eased ** 2 * control_2_y
            + eased ** 3 * target_y
        )

        # 抖动在起点和终点自然衰减，避免最终落点偏移。
        jitter_envelope = math.sin(math.pi * eased)
        jitter = (
            math.sin(
                math.tau * jitter_frequency * eased + jitter_phase
            )
            * jitter_amplitude
            * jitter_envelope
        )
        current_x = round(curve_x + normal_x * jitter)
        current_y = round(curve_y + normal_y * jitter)
        win32api.SetCursorPos((current_x, current_y))

    win32api.SetCursorPos((target_x, target_y))
