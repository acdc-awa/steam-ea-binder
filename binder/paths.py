"""项目的路径基准。

所有模块都必须从这里取路径，不要再各自写 ``Path(__file__).parent``：
代码分散到子包之后，那个写法指向的是模块自己所在的目录，不再是仓库根。
"""

from __future__ import annotations

from pathlib import Path


# binder/paths.py -> binder/ -> 仓库根
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 资源
ASSETS_DIR = PROJECT_ROOT / "assets"
EA_TEMPLATE_DIR = ASSETS_DIR / "img" / "ea"
STEAM_TEMPLATE_DIR = ASSETS_DIR / "img" / "steam"

# Steam EULA 模板（steam_flow 与 tools/eula_detector 共用）
STEAM_EULA_TITLE_TEMPLATE = STEAM_TEMPLATE_DIR / "steam_eula_title.png"
STEAM_EULA_ACCEPT_TEMPLATE = (
    STEAM_TEMPLATE_DIR / "steam_eula_accept_button.png"
)

# 运行时产物（都在仓库根，便于用户直接找到；已被 .gitignore 排除）
INFO_LOG_PATH = PROJECT_ROOT / "info.log"
ERROR_LOG_PATH = PROJECT_ROOT / "error.log"
DEBUG_CAPTURE_DIR = PROJECT_ROOT / "debug_capture"
