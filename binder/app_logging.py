"""应用错误日志配置。"""

from __future__ import annotations
import logging
import sys
import threading
from types import TracebackType
from typing import Optional, Type

from binder.paths import ERROR_LOG_PATH, INFO_LOG_PATH


ERROR_LOGGER_NAME = "steam_ea_binder.error"
INFO_LOGGER_NAME = "steam_ea_binder.info"


def get_error_logger() -> logging.Logger:
    logger = logging.getLogger(ERROR_LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.ERROR)
    logger.propagate = False
    handler = logging.FileHandler(
        ERROR_LOG_PATH,
        mode="a",
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] "
            "pid=%(process)d process=%(processName)s "
            "%(module)s: %(message)s"
        )
    )
    logger.addHandler(handler)
    return logger


def get_info_logger() -> logging.Logger:
    logger = logging.getLogger(INFO_LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(
        INFO_LOG_PATH,
        mode="a",
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] "
            "pid=%(process)d process=%(processName)s "
            "%(module)s: %(message)s"
        )
    )
    logger.addHandler(handler)
    return logger


def log_info(message: str) -> None:
    get_info_logger().info(message)


def log_exception(message: str) -> None:
    """在 except 块中调用，记录当前异常的完整堆栈。"""
    get_info_logger().error(message)
    get_error_logger().exception(message)


def install_global_exception_hooks() -> None:
    logger = get_error_logger()

    def handle_exception(
        exception_type: Type[BaseException],
        exception: BaseException,
        traceback: Optional[TracebackType],
    ) -> None:
        if issubclass(exception_type, KeyboardInterrupt):
            sys.__excepthook__(exception_type, exception, traceback)
            return
        logger.error(
            "未捕获的主线程异常",
            exc_info=(exception_type, exception, traceback),
        )

    sys.excepthook = handle_exception

    if hasattr(threading, "excepthook"):
        def handle_thread_exception(
            args: threading.ExceptHookArgs,
        ) -> None:
            logger.error(
                "未捕获的线程异常: %s",
                args.thread.name if args.thread else "unknown",
                exc_info=(
                    args.exc_type,
                    args.exc_value,
                    args.exc_traceback,
                ),
            )

        threading.excepthook = handle_thread_exception
