"""Windows 进程与服务的通用操作。
从 ea_flow 抽出，供 EA 收尾与凭证清理共用。
"""

from __future__ import annotations
import re
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Sequence

import psutil

from binder.app_logging import log_exception, log_info
from binder.app_runtime import is_running_as_admin


PROCESS_POLL_INTERVAL_SECONDS = 1.0
PROCESS_ABSENCE_STABLE_SECONDS = 3.0
PROCESS_CLOSE_TIMEOUT_SECONDS = 20.0
SERVICE_STOP_TIMEOUT_SECONDS = 15.0


def normalize_process_name(process_name: str) -> str:
    normalized = process_name.strip().casefold()
    if normalized.endswith(".exe"):
        normalized = normalized[:-4]
    return normalized


def find_processes(process_name: str) -> List[psutil.Process]:
    expected_name = normalize_process_name(process_name)
    matches: List[psutil.Process] = []
    for process in psutil.process_iter(["name", "exe"]):
        try:
            process_name_value = normalize_process_name(
                process.info.get("name") or ""
            )
            executable_name = normalize_process_name(
                Path(process.info.get("exe") or "").name
            )
            if expected_name in {process_name_value, executable_name}:
                matches.append(process)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            log_exception(f"读取进程信息被拒绝，pid={process.pid}")
    return matches


def find_processes_started_after(
    process_name: str,
    since_timestamp: float,
) -> List[psutil.Process]:
    """只保留在 since_timestamp 之后启动的进程，排除上一轮残留。"""
    matched = []
    for process in find_processes(process_name):
        try:
            if process.create_time() >= since_timestamp:
                matched.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matched


def find_processes_by_names(
    process_names: Sequence[str],
) -> List[psutil.Process]:
    """按多个进程名收集进程，按 pid 去重。"""
    collected: dict[int, psutil.Process] = {}
    for process_name in process_names:
        for process in find_processes(process_name):
            collected[process.pid] = process
    return list(collected.values())


def query_service_state(service_name: str) -> Optional[int]:
    result = subprocess.run(
        ["sc.exe", "query", service_name],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        log_info(
            f"查询Windows服务失败 name={service_name} "
            f"exit_code={result.returncode}"
        )
        return None

    state_match = re.search(
        r"STATE\s*:\s*(\d+)",
        result.stdout,
        re.IGNORECASE,
    )
    return int(state_match.group(1)) if state_match else None


def stop_windows_service(service_name: str) -> None:
    state = query_service_state(service_name)
    if state in (None, 1):
        return

    log_info(f"正在停止Windows服务 name={service_name} state={state}")
    result = subprocess.run(
        ["sc.exe", "stop", service_name],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise PermissionError(
            f"停止Windows服务失败 name={service_name} "
            f"exit_code={result.returncode}"
        )

    deadline = time.monotonic() + SERVICE_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if query_service_state(service_name) == 1:
            log_info(f"Windows服务已停止 name={service_name}")
            return
        time.sleep(PROCESS_POLL_INTERVAL_SECONDS)

    raise TimeoutError(f"等待Windows服务停止超时 name={service_name}")


def kill_processes(processes: Sequence[psutil.Process]) -> None:
    for process in processes:
        try:
            log_info(f"正在关闭进程 pid={process.pid}")
            process.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as error:
            if not is_running_as_admin():
                raise PermissionError(
                    f"没有权限关闭进程 PID={process.pid}，"
                    "请以管理员身份运行程序"
                ) from error

            result = subprocess.run(
                [
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(process.pid),
                ],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(
                    subprocess,
                    "CREATE_NO_WINDOW",
                    0,
                ),
            )
            if result.returncode != 0:
                raise PermissionError(
                    f"管理员模式下仍无法关闭进程 PID={process.pid}，"
                    f"taskkill退出码={result.returncode}"
                ) from error

    _, alive = psutil.wait_procs(list(processes), timeout=5.0)
    if alive:
        process_ids = ", ".join(str(process.pid) for process in alive)
        raise TimeoutError(f"进程未能关闭，PID={process_ids}")


def wait_and_kill_process(
    process_name: str,
    timeout_seconds: float,
    service_name: Optional[str] = None,
    pre_kill_delay_seconds: float = 0.0,
) -> bool:
    """等待目标出现并关闭，之后持续监测是否被重新拉起。

    返回 True 表示确认目标已保持关闭；返回 False 表示在 timeout 内
    从未发现该目标（调用方需自行决定是否视为失败）。
    """
    log_info(
        f"等待进程启动并关闭 name={process_name} "
        f"timeout={timeout_seconds:.0f}s"
        + (
            f" service={service_name}"
            if service_name is not None
            else ""
        )
    )
    discovery_deadline = time.monotonic() + timeout_seconds
    discovered = False

    while time.monotonic() < discovery_deadline:
        processes = find_processes(process_name)
        service_state = (
            query_service_state(service_name)
            if service_name is not None
            else None
        )
        service_active = (
            service_name is not None
            and service_state not in (None, 1)
        )
        if processes or service_active:
            discovered = True
            process_ids = [process.pid for process in processes]
            log_info(
                f"发现待关闭目标 name={process_name} "
                f"pids={process_ids} service_state={service_state}"
            )
            if pre_kill_delay_seconds > 0 and processes:
                log_info(
                    f"等待{pre_kill_delay_seconds:.0f}秒后关闭 "
                    f"name={process_name}"
                )
                time.sleep(pre_kill_delay_seconds)
                processes = find_processes(process_name)
            if service_active and service_name is not None:
                stop_windows_service(service_name)
            if processes:
                kill_processes(processes)
            break
        time.sleep(PROCESS_POLL_INTERVAL_SECONDS)

    if not discovered:
        log_info(f"等待进程超时 name={process_name}")
        return False

    stable_since: Optional[float] = None
    close_deadline = time.monotonic() + PROCESS_CLOSE_TIMEOUT_SECONDS
    while time.monotonic() < close_deadline:
        processes = find_processes(process_name)
        service_state = (
            query_service_state(service_name)
            if service_name is not None
            else None
        )
        service_active = (
            service_name is not None
            and service_state not in (None, 1)
        )
        service_confirmed_stopped = (
            service_name is None or service_state == 1
        )

        if service_active and service_name is not None:
            log_info(
                f"Windows服务被重新启动 name={service_name} "
                f"state={service_state}"
            )
            stable_since = None
            stop_windows_service(service_name)

        if processes:
            process_ids = [process.pid for process in processes]
            log_info(
                f"进程被重新启动，继续关闭 name={process_name} "
                f"pids={process_ids}"
            )
            stable_since = None
            kill_processes(processes)

        if (
            not processes
            and not service_active
            and service_confirmed_stopped
        ):
            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= PROCESS_ABSENCE_STABLE_SECONDS:
                log_info(
                    f"确认目标已关闭 name={process_name} "
                    f"stable={PROCESS_ABSENCE_STABLE_SECONDS:.0f}s"
                )
                return True
        else:
            stable_since = None

        time.sleep(PROCESS_POLL_INTERVAL_SECONDS)

    remaining_processes = find_processes(process_name)
    remaining_pids = [process.pid for process in remaining_processes]
    final_service_state = (
        query_service_state(service_name)
        if service_name is not None
        else None
    )
    raise TimeoutError(
        f"目标未能保持关闭 name={process_name} "
        f"pids={remaining_pids} service_state={final_service_state}"
    )


def wait_for_processes_gone(
    process_names: Sequence[str],
    timeout_seconds: float,
) -> bool:
    """等待一组进程名全部消失，返回是否在超时内达成。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not find_processes_by_names(process_names):
            return True
        time.sleep(PROCESS_POLL_INTERVAL_SECONDS)
    return not find_processes_by_names(process_names)
