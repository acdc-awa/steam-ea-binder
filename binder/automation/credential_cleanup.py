"""Steam / EA 登录态清理与客户端进程结束。
绑定流程的每个账号开始前、结束后都会调用这里，避免上一个账号的
登录态被下一个账号沿用（串号）。
设计约定：
- 路径运行时解析（注册表 / 运行中的进程 / 环境变量），不硬编码。
- 删除一律走"改名备份"，便于回滚；备份失败不影响主流程但会记日志。
- 遇到无法确认登录身份的情况（例如 Steam Guard）由上层跳过该账号，
  本模块不尝试绕过任何二次验证。
"""

from __future__ import annotations
import os
import re
import shutil
import threading
import winreg
from pathlib import Path
from typing import List, Optional

from binder.app_logging import log_exception, log_info
from binder.automation.process_utils import (
    find_processes_by_names,
    kill_processes,
    query_service_state,
    stop_windows_service,
    wait_for_processes_gone,
)


# --- Steam ---
STEAM_WINDOW_TITLE = "Steam"
# 主进程：判断"Steam 是否还在运行"只认它。
STEAM_MAIN_PROCESS = "steam.exe"
# 需要结束的进程：主进程 + 它的渲染子进程。
# 不含 steamservice.exe —— 那是常驻 Windows 服务，不保存登录态，
# 强杀它既没必要也会让"必须退出"的判断永远失败。
STEAM_PROCESS_NAMES = (
    "steam.exe",
    "steamwebhelper.exe",
)
STEAM_KILL_TIMEOUT_SECONDS = 25.0
STEAM_LOGINUSERS_RELATIVE_PATH = Path("config") / "loginusers.vdf"
BACKUP_SUFFIX = ".binderbak"

# --- EA ---
EA_ANTI_CHEAT_PROCESS = "EAAntiCheat.GameServiceLauncher"
EA_BACKGROUND_PROCESS = "EABackgroundService"
EA_DESKTOP_PROCESS = "EADesktop"
EA_LOCAL_HOST_PROCESS = "EALocalHostSvc"
# 游戏本体《战地1》。Steam 阶段用 steam://rungameid 把它拉起来，
# 收尾时也必须关掉：留着它会占着反作弊启动器（反作弊被关后会被
# 游戏重新拉起），批量跑还会越堆越多。
EA_GAME_PROCESS = "bf1"
EA_SERVICE_NAMES = ("EABackgroundService", "EALocalHostSvc")
EA_PROCESS_NAMES = (
    EA_DESKTOP_PROCESS,
    EA_BACKGROUND_PROCESS,
    EA_ANTI_CHEAT_PROCESS,
    EA_LOCAL_HOST_PROCESS,
    EA_GAME_PROCESS,
)
EA_KILL_TIMEOUT_SECONDS = 25.0
EA_INSTALL_FALLBACKS = (
    Path(r"C:\Program Files\Electronic Arts\EA Desktop"),
    Path(r"C:\Program Files (x86)\Electronic Arts\EA Desktop"),
)
EA_USER_DATA_RELATIVE_PATH = Path("Electronic Arts") / "EA Desktop"
EA_CEF_DIR_NAME = "CEF"


_USER_BLOCK_PATTERN = re.compile(
    r'"(?P<steam_id>\d{17})"\s*\{(?P<body>[^{}]*)\}',
    re.DOTALL,
)


def _read_vdf_value(body: str, key: str) -> Optional[str]:
    match = re.search(
        r'"%s"\s*"([^"]*)"' % re.escape(key),
        body,
    )
    return match.group(1) if match else None


def _registry_candidates() -> List[Path]:
    """从注册表收集 steam.exe / Steam 安装目录候选。"""
    candidates: List[Path] = []
    registry_locations = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"Software\WOW6432Node\Valve\Steam",
        ),
    )
    access_modes = (
        winreg.KEY_READ,
        winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    )

    for root_key, sub_key in registry_locations:
        for access_mode in access_modes:
            try:
                with winreg.OpenKey(
                    root_key,
                    sub_key,
                    0,
                    access_mode,
                ) as steam_key:
                    try:
                        steam_exe, _ = winreg.QueryValueEx(
                            steam_key,
                            "SteamExe",
                        )
                        candidates.append(Path(steam_exe))
                    except OSError:
                        install_path, _ = winreg.QueryValueEx(
                            steam_key,
                            "InstallPath",
                        )
                        candidates.append(Path(install_path) / "steam.exe")
            except OSError:
                continue

    return candidates


def find_steam_executable() -> Optional[Path]:
    candidates = _registry_candidates()

    for environment_name in ("ProgramFiles(x86)", "ProgramFiles"):
        program_files = os.environ.get(environment_name)
        if program_files:
            candidates.append(Path(program_files) / "Steam/steam.exe")

    candidates.append(Path(r"C:\Program Files (x86)\Steam\steam.exe"))
    candidates.append(Path(r"C:\Program Files\Steam\steam.exe"))

    checked_paths = set()
    for candidate in candidates:
        normalized = Path(str(candidate).replace("/", "\\")).expanduser()
        path_key = str(normalized).casefold()
        if path_key in checked_paths:
            continue
        checked_paths.add(path_key)
        if normalized.is_file():
            return normalized

    return None


def find_steam_dir() -> Optional[Path]:
    """定位 Steam 安装目录（用于读取/改写 loginusers.vdf）。"""
    steam_executable = find_steam_executable()
    if steam_executable is not None:
        return steam_executable.parent

    for root_key, sub_key in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam"),
    ):
        try:
            with winreg.OpenKey(root_key, sub_key, 0, winreg.KEY_READ) as key:
                steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
        except OSError:
            continue
        candidate = Path(str(steam_path).replace("/", "\\"))
        if candidate.is_dir():
            return candidate

    return None


def login_users_path(steam_dir: Path) -> Path:
    return steam_dir / STEAM_LOGINUSERS_RELATIVE_PATH


def read_steam_login_users(steam_dir: Path) -> List[dict]:
    """解析 loginusers.vdf，返回每个账号的字段字典。"""
    path = login_users_path(steam_dir)
    if not path.is_file():
        return []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_exception(f"读取 loginusers.vdf 失败，path={path}")
        return []

    users: List[dict] = []
    for match in _USER_BLOCK_PATTERN.finditer(text):
        body = match.group("body")
        users.append(
            {
                "steam_id": match.group("steam_id"),
                "account_name": _read_vdf_value(body, "AccountName") or "",
                "persona_name": _read_vdf_value(body, "PersonaName") or "",
                "auto_login": _read_vdf_value(body, "AutoLogin") or "0",
                "remember_password": (
                    _read_vdf_value(body, "RememberPassword") or "0"
                ),
            }
        )
    return users


def read_steam_autologin_account(steam_dir: Optional[Path]) -> Optional[str]:
    """返回当前被标记为自动登录的 Steam 账号名。"""
    if steam_dir is not None:
        for user in read_steam_login_users(steam_dir):
            if user["auto_login"] == "1" and user["account_name"]:
                return user["account_name"]

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Valve\Steam",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AutoLoginUser")
    except OSError:
        return None
    return str(value) if value else None


def kill_steam_client() -> bool:
    """结束 Steam 主进程与渲染子进程，返回主进程是否确认退出。"""
    processes = find_processes_by_names(STEAM_PROCESS_NAMES)
    if processes:
        log_info(
            "正在结束 Steam 进程 "
            f"pids={[process.pid for process in processes]}"
        )
        kill_processes(processes)

    gone = wait_for_processes_gone(
        (STEAM_MAIN_PROCESS,),
        STEAM_KILL_TIMEOUT_SECONDS,
    )
    if not gone:
        log_info("Steam 主进程未能在超时内退出")
    return gone


def clear_steam_login_state(steam_dir: Optional[Path]) -> None:
    """把 loginusers.vdf 的自动登录/记住密码关掉，并清注册表 AutoLoginUser。

    注意：Steam 未勾选"在此设备上记住我"时本身就不会保存登录态，
    这里只是确保即使被保存过也不会被下一个账号沿用。
    """
    if steam_dir is not None:
        path = login_users_path(steam_dir)
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                cleared = re.sub(
                    r'("AutoLogin"\s*)"[^"]*"',
                    r'\1"0"',
                    text,
                )
                cleared = re.sub(
                    r'("RememberPassword"\s*)"[^"]*"',
                    r'\1"0"',
                    cleared,
                )
                if cleared != text:
                    backup_path = path.with_name(
                        path.name + BACKUP_SUFFIX
                    )
                    if not backup_path.exists():
                        shutil.copy2(path, backup_path)
                    path.write_text(cleared, encoding="utf-8")
                    log_info(f"已关闭 Steam 自动登录标记 path={path}")
            except OSError:
                log_exception(
                    f"改写 loginusers.vdf 失败，path={path}"
                )

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Valve\Steam",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            for value_name in ("AutoLoginUser", "RememberPassword"):
                try:
                    winreg.DeleteValue(key, value_name)
                    log_info(f"已删除注册表值 name={value_name}")
                except FileNotFoundError:
                    continue
    except OSError:
        log_exception("清理注册表 AutoLoginUser 失败")


def find_ea_install_dir() -> Optional[Path]:
    """定位 EA App 安装目录：优先取运行中的 EADesktop.exe 所在目录。"""
    processes = find_processes_by_names((EA_DESKTOP_PROCESS,))
    for process in processes:
        try:
            executable = process.exe()
        except Exception:
            continue
        if executable:
            candidate = Path(executable).parent
            if candidate.is_dir():
                return candidate

    for fallback in EA_INSTALL_FALLBACKS:
        if fallback.is_dir():
            return fallback
    return None


def find_ea_user_data_dir() -> Optional[Path]:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    candidate = Path(local_app_data) / EA_USER_DATA_RELATIVE_PATH
    return candidate if candidate.is_dir() else None


def kill_ea_app() -> bool:
    """结束 EA App 及其后台服务/反作弊进程。"""
    for service_name in EA_SERVICE_NAMES:
        if query_service_state(service_name) not in (None, 1):
            try:
                stop_windows_service(service_name)
            except (PermissionError, TimeoutError):
                log_exception(f"停止 EA 服务失败 name={service_name}")

    processes = find_processes_by_names(EA_PROCESS_NAMES)
    if processes:
        log_info(
            "正在结束 EA 进程 "
            f"pids={[process.pid for process in processes]}"
        )
        kill_processes(processes)

    gone = wait_for_processes_gone(
        EA_PROCESS_NAMES,
        EA_KILL_TIMEOUT_SECONDS,
    )
    if not gone:
        log_info("EA 进程未能在超时内全部退出")
    return gone


def clear_ea_login_state() -> bool:
    """把 EA App 的 CEF 登录态目录改名备份，强制下次重新登录。"""
    user_data_dir = find_ea_user_data_dir()
    if user_data_dir is None:
        log_info("未找到 EA App 用户数据目录，跳过登录态清理")
        return False

    cef_dir = user_data_dir / EA_CEF_DIR_NAME
    if not cef_dir.is_dir():
        log_info(f"EA CEF 目录不存在，跳过清理 path={cef_dir}")
        return False

    backup_dir = cef_dir.with_name(cef_dir.name + BACKUP_SUFFIX)
    try:
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        os.replace(cef_dir, backup_dir)
        log_info(f"已备份 EA 登录态 {cef_dir} -> {backup_dir}")
        return True
    except OSError:
        log_exception(f"备份 EA 登录态失败，path={cef_dir}")
        return False


def prepare_account_environment() -> None:
    """账号开始前：彻底结束 Steam/EA 并清空两边登录态。"""
    steam_dir = find_steam_dir()
    if not kill_steam_client():
        raise RuntimeError(
            "Steam 进程未能全部退出，无法保证切换到目标账号"
        )
    clear_steam_login_state(steam_dir)

    kill_ea_app()
    clear_ea_login_state()


def cleanup_after_account() -> None:
    """账号结束后：清掉 EA 侧残留与两边登录态（尽力而为）。"""
    try:
        kill_ea_app()
    except Exception:
        log_exception("账号结束后结束 EA 进程失败")

    try:
        clear_ea_login_state()
    except Exception:
        log_exception("账号结束后清理 EA 登录态失败")

    try:
        clear_steam_login_state(find_steam_dir())
    except Exception:
        log_exception("账号结束后清理 Steam 登录态失败")


def run_emergency_cleanup_async() -> threading.Thread:
    """在后台线程执行兜底清理（批量中止时调用，避免阻塞界面）。"""

    def worker() -> None:
        try:
            cleanup_after_account()
            log_info("兜底清理完成")
        except Exception:
            log_exception("兜底清理失败")

    thread = threading.Thread(
        target=worker,
        name="binder-emergency-cleanup",
        daemon=True,
    )
    thread.start()
    return thread
