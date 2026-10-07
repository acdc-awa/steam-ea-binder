"""单个账号的绑号主流程。"""

from __future__ import annotations
import time
from typing import Any

from binder.app_logging import log_exception, log_info


def run_binding_flow(
    account_id: str,
    steam_account: str,
    steam_password: str,
    ea_email: str,
    ea_password: str,
    ea_2fa_secret: str,
    progress_queue: Any,
) -> bool:
    log_info(f"绑号主流程开始，account_id={account_id}")
    flow_started_at = time.time()

    from binder.automation.credential_cleanup import cleanup_after_account
    from binder.flows.ea_flow import run_ea_stage
    from binder.flows.steam_flow import close_steam_after_binding, run_steam_stage

    try:
        steam_completed = run_steam_stage(
            account_id,
            steam_account,
            steam_password,
            progress_queue,
        )
        if not steam_completed:
            log_info(f"Steam阶段未完成，终止流程，account_id={account_id}")
            return False

        ea_completed = run_ea_stage(
            account_id,
            ea_email,
            ea_password,
            ea_2fa_secret,
            progress_queue,
            started_after=flow_started_at,
        )
        if not ea_completed:
            log_info(f"EA阶段未完成，终止流程，account_id={account_id}")
            return False

        try:
            close_steam_after_binding()
            log_info(f"绑号流程完成并已关闭Steam，account_id={account_id}")
        except Exception:
            log_exception(
                f"EA绑定完成后关闭Steam失败，account_id={account_id}"
            )
        return True
    finally:
        # 不论成功失败都要清掉登录态，避免下一个账号沿用本次会话。
        try:
            cleanup_after_account()
        except Exception:
            log_exception(f"账号收尾清理失败，account_id={account_id}")
