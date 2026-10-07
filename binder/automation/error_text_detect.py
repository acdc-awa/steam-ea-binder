"""登录页错误提示（红字）检测。

EA 在账密或 2FA 码不正确时，会在输入框下方显示一行红字。这里不看
文案只看颜色：EA 的错误文案会随版本和语言变化（"Your credentials are
incorrect" / "We can't find an account with that email" …），用模板
匹配覆盖不住；而"错误提示是红字"这条长期稳定。

限定区域是这套判定的关键：EA 的 logo 本身就是红色，但它不在"输入框
下方"这个带状区域里，所以只在那个区域里找红字，就能把 logo 排除掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np


# 红色在 OpenCV HSV 里跨 0 度环绕，所以要取两端。
# H 的有效范围是 0..179，红是 H≈0 和 H≈180。
RED_HUE_LOW = 10
RED_HUE_HIGH = 170
RED_MIN_SATURATION = 90
RED_MIN_VALUE = 90

# 把同一行文字的字符横向连成一个连通域，再按尺寸过滤。
RED_TEXT_CLOSE_KERNEL_WIDTH = 15

# 连通域要满足的最小尺寸，用来排除零散噪点和图标的红色边缘。
# 宽度下限保证"是一行字"而不是一个红点；宽高比下限防止把方形红色
# 图标（比如警告三角）误当成文字。
MIN_TEXT_WIDTH = 18
MIN_TEXT_HEIGHT = 8
MIN_TEXT_ASPECT_RATIO = 1.2
MIN_TEXT_PIXELS = 40

# 整块区域里红色像素少于这个数就不必做连通域分析了。
MIN_ERROR_PIXELS = 80

# 连续命中多少帧才认定"确实有错误提示"。单帧红点（动画、悬停高亮）
# 不足以判定，因为判定结果会给账号打上需要人工清除的标记。
DEFAULT_REQUIRED_HITS = 2


@dataclass(frozen=True)
class ErrorText:
    """一次红字命中的证据，用于写日志和排查误判。"""

    red_pixels: int
    # 检测区域（相对截图左上角）与被命中连通域的位置尺寸。
    region: Tuple[int, int, int, int]
    bounding_box: Tuple[int, int, int, int]

    def describe(self) -> str:
        left, top, right, bottom = self.bounding_box
        return (
            f"红色像素={self.red_pixels} "
            f"命中框=({left},{top})-({right},{bottom})"
        )


def region_below_input(
    input_x: int,
    input_y: int,
    input_width: int,
    input_height: int,
    vertical_span: int = 140,
    horizontal_pad: int = 40,
) -> Tuple[int, int, int, int]:
    """返回输入框下方用于查找错误红字的矩形。

    EA 的错误提示紧贴在输入框下面，所以从这个区域找；左右各留一点
    余量，避免文字比输入框宽时被截断。返回的坐标不裁剪到图片范围，
    由 find_error_text 统一裁剪。
    """
    left = input_x - horizontal_pad
    top = input_y + input_height
    return (
        left,
        top,
        input_x + input_width + horizontal_pad,
        top + vertical_span,
    )


def _clamp_region(
    region: Tuple[int, int, int, int],
    width: int,
    height: int,
) -> Optional[Tuple[int, int, int, int]]:
    left = max(0, region[0])
    top = max(0, region[1])
    right = min(width, region[2])
    bottom = min(height, region[3])
    if right - left <= 0 or bottom - top <= 0:
        return None
    return left, top, right, bottom


def find_error_text(
    screenshot: np.ndarray,
    region: Optional[Tuple[int, int, int, int]] = None,
) -> Optional[ErrorText]:
    """在指定区域（默认整张图）里查找成行的红色文字。

    整张图作为区域只在 Steam 那种"整窗就是登录表单"的场景下用；
    EA 必须传 region_below_input 的结果，否则 EA 的红色 logo 会命中。
    """
    if screenshot is None or screenshot.size == 0:
        return None

    image_height, image_width = screenshot.shape[:2]
    if region is None:
        region = (0, 0, image_width, image_height)
    clamped = _clamp_region(region, image_width, image_height)
    if clamped is None:
        return None

    left, top, right, bottom = clamped
    crop = screenshot[top:bottom, left:right]
    hue, saturation, value = cv2.split(
        cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    )
    red_mask = (
        ((hue < RED_HUE_LOW) | (hue > RED_HUE_HIGH))
        & (saturation >= RED_MIN_SATURATION)
        & (value >= RED_MIN_VALUE)
    ).astype(np.uint8) * 255

    red_pixels = int(cv2.countNonZero(red_mask))
    if red_pixels < MIN_ERROR_PIXELS:
        return None

    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (RED_TEXT_CLOSE_KERNEL_WIDTH, 3),
    )
    closed = cv2.morphologyEx(red_mask, cv2.MORPH_CLOSE, kernel)
    component_count, _, stats, _ = cv2.connectedComponentsWithStats(
        closed,
        connectivity=8,
    )

    for index in range(1, component_count):
        text_x, text_y, text_width, text_height, area = stats[index]
        if text_width < MIN_TEXT_WIDTH or text_height < MIN_TEXT_HEIGHT:
            continue
        if text_width < text_height * MIN_TEXT_ASPECT_RATIO:
            continue
        if area < MIN_TEXT_PIXELS:
            continue
        return ErrorText(
            red_pixels=red_pixels,
            region=clamped,
            bounding_box=(
                left + int(text_x),
                top + int(text_y),
                left + int(text_x + text_width),
                top + int(text_y + text_height),
            ),
        )

    return None


class ErrorTextTracker:
    """要求连续多帧命中，压掉单帧红点造成的误报。

    调用方按自己的轮询节奏逐帧调 ``observe``；一旦某一帧没命中，计数
    立即归零 —— 需要"持续存在的红字"才算错误提示。
    """

    def __init__(self, required_hits: int = DEFAULT_REQUIRED_HITS) -> None:
        self.required_hits = max(1, required_hits)
        self._hits = 0
        self._last: Optional[ErrorText] = None

    def observe(
        self,
        screenshot: np.ndarray,
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> Optional[ErrorText]:
        """命中次数达到阈值时返回证据，否则返回 None。"""
        found = find_error_text(screenshot, region)
        if found is None:
            self.reset()
            return None

        self._hits += 1
        self._last = found
        if self._hits >= self.required_hits:
            return found
        return None

    def reset(self) -> None:
        self._hits = 0
        self._last = None