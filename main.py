"""Steam / EA 账号绑定工具主程序。
当前版本实现 CSV 账号读取，以及 Steam 登录和 Battlefield 1 启动阶段。
"""

from __future__ import annotations
import csv
import importlib.util
import multiprocessing
import os
import subprocess
import sys
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from binder.app_logging import (
    get_error_logger,
    install_global_exception_hooks,
    log_exception,
    log_info,
)
from binder.app_runtime import is_running_as_admin
from binder.flows.errors import ACCOUNT_INFO_ERROR_PREFIX


APP_TITLE = "Steam / EA 账号绑定工具"
CSV_ENCODINGS = ("utf-8-sig", "gb18030")
DEFAULT_PROGRESS = "未启动"
TERMINAL_PROGRESS_PREFIXES = (
    "已绑定",
    "失败",
    "已中止",
    ACCOUNT_INFO_ERROR_PREFIX,
)
BOUND_COLUMN_INDEX = 5
PROGRESS_COLUMN_INDEX = 6
# 第 8 列：该账号是否被跳过（人工标记，或程序按红字证据自动标记）。
SKIP_COLUMN_INDEX = 7
BOUND_HEADER_LABEL = "是否已绑定"
PROGRESS_HEADER_LABEL = "绑定进度"
SKIP_HEADER_LABEL = "跳过状态"

# 跳过原因。程序只会写 SKIP_REASON_INFO_ERROR，且永不自动清除；
# SKIP_REASON_TEMP 只由人工设置。
SKIP_REASON_INFO_ERROR = "信息有误"
SKIP_REASON_TEMP = "暂不登录"
SKIP_REASON_ALIASES = {
    "信息有误": SKIP_REASON_INFO_ERROR,
    "账号信息有误": SKIP_REASON_INFO_ERROR,
    "暂不登录": SKIP_REASON_TEMP,
    "暂时无法登录": SKIP_REASON_TEMP,
    "暂时跳过": SKIP_REASON_TEMP,
}


@dataclass(frozen=True)
class Account:
    steam_account: str
    steam_password: str
    ea_email: str
    ea_password: str
    ea_2fa_secret: str
    is_bound: bool = False
    csv_row_index: int = -1
    csv_progress: str = ""
    # 非空表示该账号被跳过，值即原因（见 SKIP_REASON_*）。
    # 只进不出：程序可以设置它，但只有人工操作才能清空。
    skip_status: str = ""


def parse_bound(value: str) -> bool:
    """只有 True（不区分大小写）会被解析为已绑定。"""
    return value.strip().casefold() == "true"


def parse_skip_status(value: str) -> Optional[str]:
    """把 CSV 里的跳过状态归一化；空值返回 ""，无法识别返回 None。"""
    normalized = value.strip()
    if not normalized:
        return ""
    return SKIP_REASON_ALIASES.get(normalized)


def is_header_row(row: Sequence[str]) -> bool:
    """识别常见的中英文 CSV 表头。"""
    if not row:
        return False

    normalized = [cell.strip().casefold().replace(" ", "") for cell in row]
    header_names = {
        "steam账号",
        "steam帐号",
        "steamaccount",
        "steamusername",
        "steam密码",
        "steampassword",
        "ea邮箱",
        "ea账号",
        "ea帐号",
        "eaemail",
        "eaaccount",
        "ea密码",
        "eapassword",
        "ea2fa16位码",
        "ea2fa",
        "2fa",
        "是否已绑定",
        "isbound",
        "bound",
    }
    return sum(cell in header_names for cell in normalized[:6]) >= 2


def read_csv_rows(path: Path) -> Tuple[List[List[str]], str]:
    """读取 CSV，返回数据行和实际使用的编码。"""
    last_error: UnicodeDecodeError | None = None

    for encoding in CSV_ENCODINGS:
        try:
            with path.open("r", encoding=encoding, newline="") as csv_file:
                rows = list(csv.reader(csv_file))
            return rows, encoding
        except UnicodeDecodeError as error:
            last_error = error

    raise ValueError("CSV 编码无法识别，请将文件保存为 UTF-8 或 GBK") from last_error


def load_accounts(path: Path) -> Tuple[List[Account], List[str], str]:
    """从 CSV 加载账号，并返回账号、警告信息和文件编码。"""
    rows, encoding = read_csv_rows(path)
    accounts: List[Account] = []
    warnings: List[str] = []

    for row_number, row in enumerate(rows, start=1):
        if not row or all(not cell.strip() for cell in row):
            continue
        if row_number == 1 and is_header_row(row):
            continue
        if len(row) < 5:
            warnings.append(f"第 {row_number} 行少于 5 列，已跳过")
            continue

        values = [cell.strip() for cell in row]
        steam_account, steam_password, ea_email, ea_password, ea_2fa_secret = (
            values[:5]
        )
        is_bound = parse_bound(values[5]) if len(values) >= 6 else False
        csv_progress = values[6] if len(values) >= 7 else ""
        if csv_progress == "已绑定":
            is_bound = True
        elif is_bound:
            csv_progress = "已绑定"

        skip_status = ""
        if len(values) > SKIP_COLUMN_INDEX and values[SKIP_COLUMN_INDEX]:
            raw_skip = values[SKIP_COLUMN_INDEX]
            parsed_skip = parse_skip_status(raw_skip)
            if parsed_skip is None:
                # 只认已知关键字：CSV 里多出来的自用列不能被当成跳过标记，
                # 否则会把本来要跑的账号悄悄跳过。
                warnings.append(
                    f"第 {row_number} 行第 {SKIP_COLUMN_INDEX + 1} 列"
                    f"“{raw_skip}”不是已知的跳过状态，已忽略"
                )
            else:
                skip_status = parsed_skip

        if not any(
            (steam_account, steam_password, ea_email, ea_password, ea_2fa_secret)
        ):
            continue

        accounts.append(
            Account(
                steam_account=steam_account,
                steam_password=steam_password,
                ea_email=ea_email,
                ea_password=ea_password,
                ea_2fa_secret=ea_2fa_secret,
                is_bound=is_bound,
                csv_row_index=row_number - 1,
                csv_progress=csv_progress,
                skip_status=skip_status,
            )
        )

    return accounts, warnings, encoding


def _ensure_progress_header(rows: List[List[str]]) -> None:
    """表头存在但缺少第 6/7/8 列列名时补齐列名。"""
    if not rows or not is_header_row(rows[0]):
        return
    header = rows[0]
    while len(header) <= SKIP_COLUMN_INDEX:
        header.append("")
    if not header[BOUND_COLUMN_INDEX].strip():
        header[BOUND_COLUMN_INDEX] = BOUND_HEADER_LABEL
    if not header[PROGRESS_COLUMN_INDEX].strip():
        header[PROGRESS_COLUMN_INDEX] = PROGRESS_HEADER_LABEL
    if not header[SKIP_COLUMN_INDEX].strip():
        header[SKIP_COLUMN_INDEX] = SKIP_HEADER_LABEL


def write_back_account_progress(
    path: Path,
    updates: Sequence[Tuple[int, str, bool, str, str]],
) -> bool:
    """把一批账号的“是否已绑定/绑定进度/跳过状态”写回 CSV。

    updates 元素为 (CSV行下标, Steam账号, 是否已绑定, 进度文本, 跳过状态)。
    写回前按 Steam 账号校验行未错位；失败只记日志，不向界面抛异常。
    """
    if not updates:
        return False

    try:
        rows, encoding = read_csv_rows(path)
        applied = 0
        for (
            row_index,
            steam_account,
            is_bound,
            progress,
            skip_status,
        ) in updates:
            if row_index < 0 or row_index >= len(rows):
                continue
            row = rows[row_index]
            if not row or row[0].strip() != steam_account:
                log_info(
                    f"CSV进度写回跳过：第 {row_index + 1} 行"
                    f"与账号“{steam_account}”不匹配"
                )
                continue
            while len(row) <= SKIP_COLUMN_INDEX:
                row.append("")
            row[BOUND_COLUMN_INDEX] = "True" if is_bound else "False"
            row[PROGRESS_COLUMN_INDEX] = progress
            row[SKIP_COLUMN_INDEX] = skip_status
            applied += 1

        if applied == 0:
            return False

        _ensure_progress_header(rows)
        with path.open("w", encoding=encoding, newline="") as csv_file:
            csv.writer(csv_file).writerows(rows)
        return True
    except (OSError, csv.Error, ValueError) as error:
        log_exception(
            f"CSV进度写回失败，path={path}，error={error}"
        )
        return False


def mask_secret(value: str, visible_start: int = 0, visible_end: int = 0) -> str:
    if not value:
        return ""
    if len(value) <= visible_start + visible_end:
        return "*" * len(value)

    hidden_length = len(value) - visible_start - visible_end
    start = value[:visible_start] if visible_start else ""
    end = value[-visible_end:] if visible_end else ""
    return f"{start}{'*' * hidden_length}{end}"


def binding_worker_entry(
    account_id: str,
    steam_account: str,
    steam_password: str,
    ea_email: str,
    ea_password: str,
    ea_2fa_secret: str,
    progress_queue: Any,
) -> None:
    """子进程入口；延迟导入避免 GUI 启动时强制加载 OpenCV。"""
    install_global_exception_hooks()
    try:
        from binder.flows.binding_flow import run_binding_flow

        run_binding_flow(
            account_id,
            steam_account,
            steam_password,
            ea_email,
            ea_password,
            ea_2fa_secret,
            progress_queue,
        )
    except Exception as error:
        log_exception(f"绑号子进程异常，account_id={account_id}")
        progress_queue.put(
            (
                account_id,
                f"失败：{type(error).__name__}: {error}",
            )
        )


def batch_binding_worker_entry(
    account_payloads: Sequence[Tuple[str, str, str, str, str, str]],
    progress_queue: Any,
    stop_event: Any,
) -> None:
    """在单个子进程中按顺序处理多个账号。"""
    install_global_exception_hooks()
    from binder.flows.binding_flow import run_binding_flow

    log_info(f"批量绑号子进程开始，数量={len(account_payloads)}")
    for index, (
        account_id,
        steam_account,
        steam_password,
        ea_email,
        ea_password,
        ea_2fa_secret,
    ) in enumerate(account_payloads):
        if stop_event.is_set():
            for remaining_payload in account_payloads[index:]:
                progress_queue.put(
                    (remaining_payload[0], "已中止")
                )
            break

        try:
            log_info(
                f"批量账号开始 account_id={account_id} "
                f"index={index + 1}/{len(account_payloads)}"
            )
            succeeded = run_binding_flow(
                account_id,
                steam_account,
                steam_password,
                ea_email,
                ea_password,
                ea_2fa_secret,
                progress_queue,
            )
            log_info(
                f"批量账号结束 account_id={account_id} "
                f"result={'成功' if succeeded else '失败'}"
            )
        except Exception as error:
            log_exception(
                f"批量绑号账号异常，account_id={account_id}"
            )
            progress_queue.put(
                (
                    account_id,
                    f"失败：{type(error).__name__}: {error}",
                )
            )

    log_info("批量绑号子进程结束")


class BinderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.is_admin = is_running_as_admin()
        permission_label = (
            "管理员权限" if self.is_admin else "非管理员权限"
        )
        debug_suffix = ""
        try:
            from binder.automation import debug_trace
            if debug_trace.is_enabled():
                debug_suffix = " · 调试截图已开启"
        except Exception:
            debug_suffix = ""
        self.title(f"{APP_TITLE}（{permission_label}）{debug_suffix}")
        self.geometry("1280x680")
        self.minsize(1000, 500)
        self.configure(background="#F3F3F3")

        self.accounts: List[Account] = []
        self.loaded_file: Path | None = None
        self.batch_count_text = tk.StringVar(value="1")
        self.secrets_masked = True
        self.account_generation = 0
        self.item_accounts: Dict[str, Account] = {}
        self.account_processes: Dict[str, multiprocessing.Process] = {}
        self.progress_statuses: Dict[str, str] = {}
        self.process_context = multiprocessing.get_context("spawn")
        self.progress_queue = self.process_context.Queue()
        self.batch_process: Optional[multiprocessing.Process] = None
        self.batch_stop_event: Any = None
        self.batch_item_ids: List[str] = []
        self.aborted_item_ids: Set[str] = set()
        self.round_number = 0
        self.pending_csv_progress: Dict[str, Tuple[int, str, bool, str, str]] = {}

        self._configure_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll_progress_queue)
        log_info(
            "GUI程序启动，管理员权限="
            f"{'是' if self.is_admin else '否'}"
        )
        if not self.is_admin:
            self.after(300, self._show_admin_warning)

    def report_callback_exception(
        self,
        exception_type: type,
        exception: BaseException,
        traceback: Any,
    ) -> None:
        get_error_logger().error(
            "Tkinter回调异常",
            exc_info=(exception_type, exception, traceback),
        )
        messagebox.showerror(
            "程序错误",
            f"{exception_type.__name__}: {exception}\n"
            "完整堆栈已写入 error.log。",
            parent=self,
        )

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.option_add("*Font", "{Segoe UI} 10")
        self.option_add("*Menu.Font", "{Segoe UI} 10")
        self.option_add("*TCombobox*Listbox.font", "{Segoe UI} 10")

        style.configure(".", font=("Segoe UI", 10))
        style.configure("App.TFrame", background="#F3F3F3")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure(
            "Title.TLabel",
            background="#F3F3F3",
            foreground="#1F1F1F",
            font=("Segoe UI Semibold", 20),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#F3F3F3",
            foreground="#666666",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Toolbar.TLabel",
            background="#FFFFFF",
            foreground="#424242",
        )
        style.configure(
            "TButton",
            background="#FFFFFF",
            foreground="#202020",
            borderwidth=1,
            focusthickness=0,
            focuscolor="",
            padding=(12, 7),
            relief=tk.FLAT,
        )
        style.map(
            "TButton",
            background=[
                ("disabled", "#F1F1F1"),
                ("pressed", "#E0E0E0"),
                ("active", "#EAEAEA"),
            ],
            foreground=[("disabled", "#A0A0A0")],
        )
        style.configure(
            "Accent.TButton",
            background="#0067C0",
            foreground="#FFFFFF",
            borderwidth=0,
            padding=(14, 8),
        )
        style.map(
            "Accent.TButton",
            background=[
                ("disabled", "#A6C8E5"),
                ("pressed", "#004E92"),
                ("active", "#1976C8"),
            ],
            foreground=[("disabled", "#F4F4F4")],
        )
        style.configure(
            "Danger.TButton",
            background="#C42B1C",
            foreground="#FFFFFF",
            borderwidth=0,
            padding=(14, 8),
        )
        style.map(
            "Danger.TButton",
            background=[
                ("disabled", "#E4B4AF"),
                ("pressed", "#8F1E14"),
                ("active", "#D43B2A"),
            ],
        )
        style.configure(
            "TEntry",
            fieldbackground="#FFFFFF",
            foreground="#202020",
            bordercolor="#B8B8B8",
            lightcolor="#B8B8B8",
            darkcolor="#B8B8B8",
            padding=(8, 6),
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", "#0067C0")],
            lightcolor=[("focus", "#0067C0")],
            darkcolor=[("focus", "#0067C0")],
        )
        style.configure(
            "Treeview",
            background="#FFFFFF",
            fieldbackground="#FFFFFF",
            foreground="#202020",
            borderwidth=0,
            rowheight=34,
            font=("Segoe UI", 10),
        )
        style.map(
            "Treeview",
            background=[("selected", "#DCEEFF")],
            foreground=[("selected", "#111111")],
        )
        style.configure(
            "Treeview.Heading",
            background="#F7F7F7",
            foreground="#303030",
            borderwidth=0,
            relief=tk.FLAT,
            padding=(8, 9),
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "Treeview.Heading",
            background=[("active", "#EEEEEE")],
        )

    def _build_ui(self) -> None:
        app_frame = ttk.Frame(
            self,
            style="App.TFrame",
            padding=(20, 16, 20, 20),
        )
        app_frame.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(app_frame, style="App.TFrame")
        header.pack(fill=tk.X, pady=(0, 14))
        ttk.Label(
            header,
            text="账号绑定管理",
            style="Title.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            header,
            text="管理 Steam 与 EA 账号，并按顺序执行自动绑定任务",
            style="Subtitle.TLabel",
        ).pack(anchor=tk.W, pady=(3, 0))

        toolbar = ttk.Frame(
            app_frame,
            style="Card.TFrame",
            padding=(14, 12),
        )
        toolbar.pack(fill=tk.X, pady=(0, 12))

        read_button = ttk.Button(
            toolbar,
            text="读取账号",
            command=self.choose_account_file,
            style="Accent.TButton",
        )
        read_button.pack(side=tk.LEFT)

        self.mask_toggle_button = ttk.Button(
            toolbar,
            text="显示密码/16位码",
            command=self.toggle_secret_mask,
        )
        self.mask_toggle_button.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Button(
            toolbar,
            text="切换到 Qt",
            command=self.switch_to_qt,
        ).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            toolbar,
            text="每轮数量：",
            style="Toolbar.TLabel",
            padding=(12, 0, 3, 0),
        ).pack(side=tk.LEFT)
        self.batch_count_entry = ttk.Entry(
            toolbar,
            textvariable=self.batch_count_text,
            width=6,
            justify=tk.CENTER,
        )
        self.batch_count_entry.pack(side=tk.LEFT)

        self.batch_start_button = ttk.Button(
            toolbar,
            text="批量启动",
            command=self.start_batch,
            style="Accent.TButton",
        )
        self.batch_start_button.pack(side=tk.LEFT, padx=(8, 0))

        self.batch_stop_button = ttk.Button(
            toolbar,
            text="中止",
            command=self.stop_batch,
            state=tk.DISABLED,
            style="Danger.TButton",
        )
        self.batch_stop_button.pack(side=tk.LEFT, padx=(6, 0))

        self.next_round_button = ttk.Button(
            toolbar,
            text="下一轮",
            command=self.start_next_round,
            state=tk.DISABLED,
        )
        self.next_round_button.pack(side=tk.LEFT, padx=(8, 0))

        self.round_status_label = ttk.Label(
            toolbar,
            text="就绪",
            style="Toolbar.TLabel",
        )
        self.round_status_label.pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
            padx=(14, 0),
        )

        table_frame = ttk.Frame(
            app_frame,
            style="Card.TFrame",
            padding=(1, 1, 1, 1),
        )
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = (
            "index",
            "steam_account",
            "steam_password",
            "ea_email",
            "ea_password",
            "ea_2fa_secret",
            "is_bound",
            "progress",
            "skip",
        )
        self.account_table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )

        headings = {
            "index": "序号",
            "steam_account": "Steam 账号",
            "steam_password": "Steam 密码",
            "ea_email": "EA 邮箱",
            "ea_password": "EA 密码",
            "ea_2fa_secret": "EA 2FA 16位码",
            "is_bound": "是否已绑定",
            "progress": "当前绑号进度",
            "skip": "跳过状态",
        }
        widths = {
            "index": 55,
            "steam_account": 150,
            "steam_password": 110,
            "ea_email": 220,
            "ea_password": 110,
            "ea_2fa_secret": 145,
            "is_bound": 100,
            "progress": 190,
            "skip": 100,
        }

        for column in columns:
            self.account_table.heading(column, text=headings[column])
            anchor = (
                tk.CENTER
                if column in {"index", "is_bound", "progress", "skip"}
                else tk.W
            )
            stretch = column in {"steam_account", "ea_email", "progress"}
            self.account_table.column(
                column,
                width=widths[column],
                minwidth=widths[column],
                anchor=anchor,
                stretch=stretch,
            )

        vertical_scrollbar = ttk.Scrollbar(
            table_frame,
            orient=tk.VERTICAL,
            command=self.account_table.yview,
        )
        horizontal_scrollbar = ttk.Scrollbar(
            table_frame,
            orient=tk.HORIZONTAL,
            command=self.account_table.xview,
        )
        self.account_table.configure(
            yscrollcommand=vertical_scrollbar.set,
            xscrollcommand=horizontal_scrollbar.set,
        )
        self.account_table.tag_configure(
            "even",
            background="#FAFAFA",
        )
        self.account_table.tag_configure(
            "odd",
            background="#FFFFFF",
        )
        self.account_table.tag_configure(
            "bound",
            foreground="#687078",
        )
        # 跳过的账号用橙色，和"已绑定"的灰区分开：一个是完成，一个是待核对。
        self.account_table.tag_configure(
            "skipped",
            foreground="#B25E00",
        )

        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.account_table.grid(row=0, column=0, sticky="nsew")
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew")

        self.account_menu = tk.Menu(self, tearoff=False)
        self.account_menu.configure(
            background="#FFFFFF",
            foreground="#202020",
            activebackground="#E5F1FB",
            activeforeground="#202020",
            borderwidth=1,
            relief=tk.SOLID,
        )
        self.account_menu_entries: Dict[str, int] = {}
        self._add_account_menu_command(
            "start",
            "启动",
            self.start_selected_account,
        )
        self._add_account_menu_command(
            "mark_bound",
            "标记为已绑定",
            self.mark_selected_bound,
        )
        self.account_menu.add_separator()
        self._add_account_menu_command(
            "mark_info_error",
            "标记为信息有误",
            self.mark_selected_info_error,
        )
        self._add_account_menu_command(
            "mark_temp_skip",
            "标记为暂不登录",
            self.mark_selected_temp_skip,
        )
        self._add_account_menu_command(
            "clear_skip",
            "取消跳过标记",
            self.clear_selected_skip,
        )
        self.account_menu.add_separator()
        self._add_account_menu_command(
            "edit",
            "修改",
            self.edit_selected_account,
        )
        self._add_account_menu_command(
            "delete",
            "删除",
            self.delete_selected_account,
        )
        self.account_table.bind("<Button-3>", self._show_account_menu)

    def _add_account_menu_command(
        self,
        name: str,
        label: str,
        command: Any,
    ) -> None:
        """登记菜单项下标。

        置灰时按名字取下标，不再用硬编码的 entryconfigure(0/1) —— 那样
        每加一个菜单项就会错位。
        """
        self.account_menu.add_command(label=label, command=command)
        self.account_menu_entries[name] = self.account_menu.index("end")

    def choose_account_file(self) -> None:
        if self._batch_is_running() or self._single_process_is_running():
            messagebox.showwarning(
                "流程正在运行",
                "请先等待当前绑号流程结束或中止批量流程。",
                parent=self,
            )
            return

        dialog_owner = tk.Toplevel(self)
        dialog_owner.withdraw()
        dialog_owner.transient(self)
        dialog_owner.attributes("-topmost", True)
        dialog_owner.update_idletasks()

        try:
            selected_file = filedialog.askopenfilename(
                parent=dialog_owner,
                title="选择账号 CSV 文件",
                filetypes=(("CSV 文件", "*.csv"),),
                defaultextension=".csv",
            )
        finally:
            dialog_owner.destroy()

        if not selected_file:
            return

        path = Path(selected_file)
        if path.suffix.casefold() != ".csv":
            messagebox.showerror(
                "文件格式错误",
                "只能读取 .csv 文件。",
                parent=self,
            )
            return

        try:
            accounts, warnings, encoding = load_accounts(path)
        except (OSError, csv.Error, ValueError) as error:
            log_exception(f"读取CSV账号文件失败，path={path}")
            messagebox.showerror(
                "读取失败",
                f"无法读取账号文件：\n{error}",
                parent=self,
            )
            return

        self._flush_csv_progress()
        self.accounts = accounts
        self.loaded_file = path
        self.account_generation += 1
        self._refresh_account_table()
        self.round_number = 0
        self.next_round_button.configure(state=tk.DISABLED)
        self.round_status_label.configure(
            text=f"已读取 {len(accounts)} 个账号，点「批量启动」开始第 1 轮"
        )

        log_info(
            f"账号文件读取完成 file={path.name} "
            f"count={len(accounts)} "
            f"bound={sum(account.is_bound for account in accounts)}"
        )

        if warnings:
            self._show_warnings(warnings)
        elif not accounts:
            messagebox.showwarning(
                "没有账号",
                "CSV 中没有可加载的账号数据。",
                parent=self,
            )

    def switch_to_qt(self) -> None:
        if self._batch_is_running() or self._single_process_is_running():
            messagebox.showwarning(
                "流程正在运行",
                "请先等待当前流程结束或中止批量流程。",
                parent=self,
            )
            return
        if importlib.util.find_spec("PySide6") is None:
            messagebox.showerror(
                "缺少 Qt 依赖",
                "尚未安装 PySide6，请先运行：\n"
                "python -m pip install -r requirements.txt",
                parent=self,
            )
            return

        qt_main_path = Path(__file__).resolve().parent / "qt_main.py"
        subprocess.Popen(
            [sys.executable, str(qt_main_path)],
            cwd=str(qt_main_path.parent),
        )
        log_info("从Tk界面切换到Qt界面")
        self._on_close()

    def _refresh_account_table(self) -> None:
        self.account_table.delete(*self.account_table.get_children())
        self.item_accounts.clear()
        self.progress_statuses.clear()
        self.aborted_item_ids.clear()

        for index, account in enumerate(self.accounts, start=1):
            item_id = f"account_{self.account_generation}_{index}"
            self.item_accounts[item_id] = account
            status = account.csv_progress or DEFAULT_PROGRESS
            self.progress_statuses[item_id] = status
            self.account_table.insert(
                "",
                tk.END,
                iid=item_id,
                tags=self._row_tags(index, account),
                values=self._account_row_values(
                    index,
                    account,
                    status,
                ),
            )

    @staticmethod
    def _row_tags(index: int, account: Account) -> Tuple[str, ...]:
        tags = ["even" if index % 2 == 0 else "odd"]
        if account.is_bound:
            tags.append("bound")
        if account.skip_status:
            tags.append("skipped")
        return tuple(tags)

    def _account_row_values(
        self,
        index: int,
        account: Account,
        progress: str,
    ) -> Tuple[Any, ...]:
        if self.secrets_masked:
            steam_password = mask_secret(account.steam_password)
            ea_password = mask_secret(account.ea_password)
            ea_2fa_secret = mask_secret(
                account.ea_2fa_secret,
                visible_end=4,
            )
        else:
            steam_password = account.steam_password
            ea_password = account.ea_password
            ea_2fa_secret = account.ea_2fa_secret

        return (
            index,
            account.steam_account,
            steam_password,
            account.ea_email,
            ea_password,
            ea_2fa_secret,
            "已绑定" if account.is_bound else "未绑定",
            progress,
            account.skip_status,
        )

    def toggle_secret_mask(self) -> None:
        self.secrets_masked = not self.secrets_masked
        self.mask_toggle_button.configure(
            text=(
                "显示密码/16位码"
                if self.secrets_masked
                else "隐藏密码/16位码"
            )
        )
        for index, item_id in enumerate(
            self.account_table.get_children(),
            start=1,
        ):
            account = self.item_accounts[item_id]
            progress = self.progress_statuses.get(
                item_id,
                DEFAULT_PROGRESS,
            )
            self.account_table.item(
                item_id,
                tags=self._row_tags(index, account),
                values=self._account_row_values(
                    index,
                    account,
                    progress,
                ),
            )

    def _show_account_menu(self, event: tk.Event) -> None:
        item_id = self.account_table.identify_row(event.y)
        if not item_id:
            return

        self.account_table.selection_set(item_id)
        self.account_table.focus(item_id)
        account = self.item_accounts[item_id]
        process = self.account_processes.get(item_id)
        action_disabled = (
            account.is_bound
            or bool(account.skip_status)
            or self._batch_is_running()
            or (process is not None and process.is_alive())
        )
        item_state = tk.DISABLED if action_disabled else tk.NORMAL
        for name in ("start", "mark_bound"):
            self.account_menu.entryconfigure(
                self.account_menu_entries[name],
                state=item_state,
            )

        # 跳过标记本身是纯元数据操作，不碰正在跑的流程，所以批量运行期间
        # 也允许标记 —— 发现某个账号资料不对时能立刻把它从后续轮次里摘掉。
        for name in ("mark_info_error", "mark_temp_skip"):
            self.account_menu.entryconfigure(
                self.account_menu_entries[name],
                state=tk.DISABLED if account.skip_status else tk.NORMAL,
            )
        self.account_menu.entryconfigure(
            self.account_menu_entries["clear_skip"],
            state=tk.NORMAL if account.skip_status else tk.DISABLED,
        )
        try:
            self.account_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.account_menu.grab_release()

    def _selected_item_id(self) -> Optional[str]:
        item_id = self.account_table.focus()
        if item_id and item_id in self.item_accounts:
            return item_id
        return None

    def _account_can_be_changed(self, item_id: str) -> bool:
        if self._batch_is_running():
            messagebox.showwarning(
                "批量流程正在运行",
                "批量流程运行时不能修改或删除账号。",
                parent=self,
            )
            return False

        process = self.account_processes.get(item_id)
        if process is not None and process.is_alive():
            messagebox.showwarning(
                "账号正在运行",
                "当前账号正在执行绑号流程，不能修改或删除。",
                parent=self,
            )
            return False
        return True

    def _replace_account(
        self,
        item_id: str,
        updated_account: Account,
    ) -> None:
        old_account = self.item_accounts[item_id]
        for index, account in enumerate(self.accounts):
            if account is old_account:
                self.accounts[index] = updated_account
                break
        self.item_accounts[item_id] = updated_account

    def edit_selected_account(self) -> None:
        item_id = self._selected_item_id()
        if item_id is None or not self._account_can_be_changed(item_id):
            return

        account = self.item_accounts[item_id]
        dialog = tk.Toplevel(self)
        dialog.title("修改账号")
        dialog.configure(background="#FFFFFF")
        dialog.transient(self)
        dialog.attributes("-topmost", True)
        dialog.resizable(False, False)
        dialog.grab_set()

        content = ttk.Frame(
            dialog,
            style="Card.TFrame",
            padding=18,
        )
        content.pack(fill=tk.BOTH, expand=True)

        field_values = {
            "steam_account": tk.StringVar(value=account.steam_account),
            "steam_password": tk.StringVar(value=account.steam_password),
            "ea_email": tk.StringVar(value=account.ea_email),
            "ea_password": tk.StringVar(value=account.ea_password),
            "ea_2fa_secret": tk.StringVar(value=account.ea_2fa_secret),
            "is_bound": tk.StringVar(
                value="已绑定" if account.is_bound else "未绑定"
            ),
        }
        field_definitions = (
            ("Steam 账号", "steam_account", False),
            ("Steam 密码", "steam_password", True),
            ("EA 邮箱", "ea_email", False),
            ("EA 密码", "ea_password", True),
            ("EA 2FA 16位码", "ea_2fa_secret", True),
        )

        secret_entries = []
        for row, (label, field_name, is_secret) in enumerate(
            field_definitions
        ):
            ttk.Label(content, text=label).grid(
                row=row,
                column=0,
                sticky=tk.W,
                padx=(0, 12),
                pady=5,
            )
            entry = ttk.Entry(
                content,
                textvariable=field_values[field_name],
                width=42,
                show=("*" if is_secret and self.secrets_masked else ""),
            )
            entry.grid(row=row, column=1, sticky=tk.EW, pady=5)
            if is_secret:
                secret_entries.append(entry)

        bound_row = len(field_definitions)
        ttk.Label(content, text="是否已绑定").grid(
            row=bound_row,
            column=0,
            sticky=tk.W,
            padx=(0, 12),
            pady=5,
        )
        ttk.Combobox(
            content,
            textvariable=field_values["is_bound"],
            values=("未绑定", "已绑定"),
            state="readonly",
            width=39,
        ).grid(row=bound_row, column=1, sticky=tk.EW, pady=5)

        show_secrets = tk.BooleanVar(value=not self.secrets_masked)

        def toggle_dialog_secrets() -> None:
            show_value = "" if show_secrets.get() else "*"
            for entry in secret_entries:
                entry.configure(show=show_value)

        ttk.Checkbutton(
            content,
            text="显示密码和16位码",
            variable=show_secrets,
            command=toggle_dialog_secrets,
        ).grid(
            row=bound_row + 1,
            column=1,
            sticky=tk.W,
            pady=(4, 8),
        )

        # 改完账密标记还在，容易让人以为"修好了为什么还跳过"，
        # 所以在对话框里直接说明。
        if account.skip_status:
            ttk.Label(
                content,
                text=(
                    f"该账号已标记为“{account.skip_status}”，"
                    "修改账密不会自动取消标记；核对无误后请在右键菜单里"
                    "手动“取消跳过标记”。"
                ),
                foreground="#B25E00",
                wraplength=380,
                justify=tk.LEFT,
            ).grid(
                row=bound_row + 2,
                column=0,
                columnspan=2,
                sticky=tk.W,
                pady=(0, 4),
            )

        button_row = bound_row + 3
        button_frame = ttk.Frame(content)
        button_frame.grid(
            row=button_row,
            column=0,
            columnspan=2,
            sticky=tk.E,
            pady=(8, 0),
        )

        def save_account() -> None:
            # 用 replace 保留 csv_row_index / csv_progress / skip_status：
            # 跳过标记只进不出，改账密不能顺手把它清掉。
            updated_account = replace(
                account,
                steam_account=field_values["steam_account"].get().strip(),
                steam_password=field_values["steam_password"].get(),
                ea_email=field_values["ea_email"].get().strip(),
                ea_password=field_values["ea_password"].get(),
                ea_2fa_secret=field_values["ea_2fa_secret"].get().strip(),
                is_bound=field_values["is_bound"].get() == "已绑定",
            )
            self._replace_account(item_id, updated_account)
            item_index = (
                list(self.account_table.get_children()).index(item_id) + 1
            )
            progress = self.progress_statuses.get(
                item_id,
                DEFAULT_PROGRESS,
            )
            self._queue_csv_progress(item_id, updated_account, progress)
            self.account_table.item(
                item_id,
                tags=self._row_tags(item_index, updated_account),
                values=self._account_row_values(
                    item_index,
                    updated_account,
                    progress,
                ),
            )
            log_info(f"账号信息已修改，account_id={item_id}")
            dialog.destroy()

        ttk.Button(
            button_frame,
            text="保存",
            command=save_account,
            style="Accent.TButton",
        ).pack(side=tk.LEFT)
        ttk.Button(
            button_frame,
            text="取消",
            command=dialog.destroy,
        ).pack(side=tk.LEFT, padx=(8, 0))

        content.columnconfigure(1, weight=1)
        dialog.update_idletasks()
        dialog.geometry(
            f"+{self.winfo_rootx() + 120}+{self.winfo_rooty() + 80}"
        )

    def delete_selected_account(self) -> None:
        item_id = self._selected_item_id()
        if item_id is None or not self._account_can_be_changed(item_id):
            return

        account = self.item_accounts[item_id]
        confirmed = messagebox.askyesno(
            "删除账号",
            f"确定删除 Steam 账号“{account.steam_account}”吗？",
            parent=self,
        )
        if not confirmed:
            return

        self.accounts = [
            current_account
            for current_account in self.accounts
            if current_account is not account
        ]
        self.account_table.delete(item_id)
        self.item_accounts.pop(item_id, None)
        self.progress_statuses.pop(item_id, None)
        self.aborted_item_ids.add(item_id)
        process = self.account_processes.pop(item_id, None)
        if process is not None:
            process.join(timeout=0)

        for index, remaining_item_id in enumerate(
            self.account_table.get_children(),
            start=1,
        ):
            remaining_account = self.item_accounts[remaining_item_id]
            values = list(
                self.account_table.item(remaining_item_id, "values")
            )
            if values:
                values[0] = index
                self.account_table.item(
                    remaining_item_id,
                    tags=self._row_tags(
                        index,
                        remaining_account,
                    ),
                    values=values,
                )
        log_info(f"账号已删除，account_id={item_id}")

    def start_selected_account(self) -> None:
        item_id = self.account_table.focus()
        account = self.item_accounts.get(item_id)
        if account is None:
            return
        if account.is_bound:
            messagebox.showinfo(
                "账号已绑定",
                "该账号已绑定，不能再次启动绑号流程。",
                parent=self,
            )
            return

        if account.skip_status:
            messagebox.showinfo(
                "账号已跳过",
                f"该账号已标记为“{account.skip_status}”，不会启动绑号流程。\n"
                "请先核对账号资料，再用右键菜单“取消跳过标记”恢复。",
                parent=self,
            )
            return

        if self._batch_is_running():
            messagebox.showinfo(
                "批量流程正在运行",
                "请先等待或中止当前批量流程。",
                parent=self,
            )
            return

        if not self.is_admin:
            log_info(
                f"阻止非管理员启动绑号流程，account_id={item_id}"
            )
            messagebox.showerror(
                "需要管理员权限",
                "关闭 EABackgroundService 等进程需要管理员权限。\n"
                "请关闭程序后，右键选择“以管理员身份运行”。",
                parent=self,
            )
            return

        existing_process = self.account_processes.get(item_id)
        if existing_process is not None and existing_process.is_alive():
            messagebox.showinfo(
                "流程正在运行",
                "该账号的绑号流程已经在运行中。",
                parent=self,
            )
            return

        self.aborted_item_ids.discard(item_id)
        self._set_account_progress(item_id, "Steam登录中")
        process = self.process_context.Process(
            target=binding_worker_entry,
            args=(
                item_id,
                account.steam_account,
                account.steam_password,
                account.ea_email,
                account.ea_password,
                account.ea_2fa_secret,
                self.progress_queue,
            ),
            name=f"steam-binder-{item_id}",
            daemon=True,
        )

        try:
            process.start()
        except Exception as error:
            log_exception(f"启动绑号子进程失败，account_id={item_id}")
            self._set_account_progress(
                item_id,
                f"启动失败：{type(error).__name__}: {error}",
            )
            return

        self.account_processes[item_id] = process
        log_info(f"绑号子进程已启动，account_id={item_id}")

    def mark_selected_bound(self) -> None:
        """手动把账号标记为已绑定，并立即写回 CSV。"""
        item_id = self._selected_item_id()
        if item_id is None or not self._account_can_be_changed(item_id):
            return

        account = self.item_accounts[item_id]
        if account.is_bound:
            messagebox.showinfo(
                "账号已绑定",
                "该账号已标记为已绑定。",
                parent=self,
            )
            return

        confirmed = messagebox.askyesno(
            "标记已绑定",
            f"确定将 Steam 账号“{account.steam_account}”标记为已绑定吗？\n"
            "标记后会写入 CSV，批量启动和下一轮都会跳过它。",
            parent=self,
        )
        if not confirmed:
            return

        self._set_account_progress(item_id, "已绑定")
        log_info(f"手动标记已绑定，account_id={item_id}")

    def mark_selected_info_error(self) -> None:
        """人工标记账号资料有误。"""
        self._confirm_skip_status(
            SKIP_REASON_INFO_ERROR,
            "标记为信息有误",
            "确定把该账号标记为“信息有误”吗？\n"
            "标记会写入 CSV，批量启动和下一轮都会跳过它；\n"
            "修改账密不会自动取消标记，需要在右键菜单里手动取消。",
        )

    def mark_selected_temp_skip(self) -> None:
        """人工标记账号暂时无法登录。"""
        self._confirm_skip_status(
            SKIP_REASON_TEMP,
            "标记为暂不登录",
            "确定把该账号标记为“暂不登录”吗？\n"
            "标记会写入 CSV，批量启动和下一轮都会跳过它；\n"
            "需要时用右键菜单“取消跳过标记”恢复。",
        )

    def clear_selected_skip(self) -> None:
        """人工清除跳过标记 —— 这是唯一能把标记改回空的入口。"""
        item_id = self._selected_item_id()
        if item_id is None:
            return
        account = self.item_accounts[item_id]
        if not account.skip_status:
            return
        if not messagebox.askyesno(
            "取消跳过标记",
            f"确定取消该账号的“{account.skip_status}”标记吗？\n"
            "取消后它会重新参与批量绑定。",
            parent=self,
        ):
            return
        self._set_skip_status(item_id, "")

    def _confirm_skip_status(
        self,
        skip_status: str,
        title: str,
        message: str,
    ) -> None:
        item_id = self._selected_item_id()
        if item_id is None:
            return
        if not messagebox.askyesno(title, message, parent=self):
            return
        self._set_skip_status(item_id, skip_status)

    def _set_skip_status(self, item_id: str, skip_status: str) -> None:
        """设置或清除跳过标记，并立即排队写回 CSV。

        清除只能由人工操作触发（clear_selected_skip）：程序侧只调用
        _mark_account_info_error，永远不会把标记改回空 —— 否则一次网络
        抖动的误判就会把"待核对"悄悄变成"没问题"。
        """
        account = self.item_accounts.get(item_id)
        if account is None or account.skip_status == skip_status:
            return

        updated_account = replace(account, skip_status=skip_status)
        self._replace_account(item_id, updated_account)
        progress = self.progress_statuses.get(item_id, DEFAULT_PROGRESS)
        if self.account_table.exists(item_id):
            item_index = (
                list(self.account_table.get_children()).index(item_id) + 1
            )
            self.account_table.item(
                item_id,
                tags=self._row_tags(item_index, updated_account),
                values=self._account_row_values(
                    item_index,
                    updated_account,
                    progress,
                ),
            )
        self._queue_csv_progress(item_id, updated_account, progress)
        log_info(
            f"跳过标记变化 account_id={item_id}: "
            f"{account.skip_status or '无'} -> {skip_status or '无'}"
        )

    def _mark_account_info_error(self, item_id: str) -> None:
        """程序按红字证据把账号标记为“信息有误”。

        只加不清：这个标记是给人看的待核对清单，清标记必须由人工确认，
        所以这里绝不反向操作。
        """
        account = self.item_accounts.get(item_id)
        if account is None or account.skip_status:
            return
        self._set_skip_status(item_id, SKIP_REASON_INFO_ERROR)
        log_info(f"自动标记账号信息有误，account_id={item_id}")

    def _single_process_is_running(self) -> bool:
        return any(
            process.is_alive()
            for process in self.account_processes.values()
        )

    def _batch_is_running(self) -> bool:
        return (
            self.batch_process is not None
            and self.batch_process.is_alive()
        )

    def _eligible_item_ids(self) -> List[str]:
        """返回尚未绑定、未被跳过、可以进入下一轮的账号行 ID。"""
        return [
            item_id
            for item_id in self.account_table.get_children()
            if not self.item_accounts[item_id].is_bound
            and not self.item_accounts[item_id].skip_status
            and self.progress_statuses.get(
                item_id,
                DEFAULT_PROGRESS,
            ) != "已绑定"
        ]

    def start_next_round(self) -> None:
        """人工触发下一轮：按“每轮数量”继续处理剩余账号。"""
        if self._batch_is_running():
            return
        self.start_batch()
        if not self._batch_is_running() and self._eligible_item_ids():
            self.next_round_button.configure(state=tk.NORMAL)

    def _report_round_paused(self, reason: str) -> None:
        """一轮结束或中止后更新状态栏，并在仍有账号时启用“下一轮”。"""
        remaining = self._eligible_item_ids()
        if remaining:
            self.round_status_label.configure(
                text=f"{reason} · 剩余 {len(remaining)} 个 · 点「下一轮」继续"
            )
            self.next_round_button.configure(state=tk.NORMAL)
        else:
            self.round_status_label.configure(
                text=f"{reason} · 没有待处理账号了"
            )
            self.next_round_button.configure(state=tk.DISABLED)

    def start_batch(self) -> None:
        if not self.is_admin:
            messagebox.showerror(
                "需要管理员权限",
                "批量绑号需要以管理员身份运行程序。",
                parent=self,
            )
            return
        if self._batch_is_running():
            messagebox.showinfo(
                "批量流程正在运行",
                "当前已有批量绑号流程。",
                parent=self,
            )
            return
        if self._single_process_is_running():
            messagebox.showinfo(
                "单账号流程正在运行",
                "请等待当前单账号流程结束后再批量启动。",
                parent=self,
            )
            return

        try:
            requested_count = int(self.batch_count_text.get().strip())
        except ValueError:
            requested_count = 0
        if requested_count <= 0:
            messagebox.showerror(
                "数量无效",
                "批量数量必须是大于 0 的整数。",
                parent=self,
            )
            return

        eligible_item_ids = self._eligible_item_ids()
        if requested_count > len(eligible_item_ids):
            messagebox.showerror(
                "账号数量不足",
                f"当前仅有 {len(eligible_item_ids)} 个未处理账号。",
                parent=self,
            )
            return

        self.batch_item_ids = eligible_item_ids[:requested_count]
        payloads = []
        for item_id in self.batch_item_ids:
            account = self.item_accounts[item_id]
            self.aborted_item_ids.discard(item_id)
            self._set_account_progress(item_id, "批量等待中")
            payloads.append(
                (
                    item_id,
                    account.steam_account,
                    account.steam_password,
                    account.ea_email,
                    account.ea_password,
                    account.ea_2fa_secret,
                )
            )

        self.batch_stop_event = self.process_context.Event()
        self.batch_process = self.process_context.Process(
            target=batch_binding_worker_entry,
            args=(
                payloads,
                self.progress_queue,
                self.batch_stop_event,
            ),
            name="steam-binder-batch",
            daemon=True,
        )
        try:
            self.batch_process.start()
        except Exception as error:
            log_exception("启动批量绑号子进程失败")
            for item_id in self.batch_item_ids:
                self._set_account_progress(
                    item_id,
                    f"启动失败：{type(error).__name__}: {error}",
                )
            self.batch_process = None
            self.batch_stop_event = None
            return

        self.round_number += 1
        self.batch_start_button.configure(state=tk.DISABLED)
        self.batch_stop_button.configure(state=tk.NORMAL)
        self.batch_count_entry.configure(state=tk.DISABLED)
        self.next_round_button.configure(state=tk.DISABLED)
        self.round_status_label.configure(
            text=(
                f"第 {self.round_number} 轮进行中"
                f"（{len(self.batch_item_ids)} 个账号）…"
            )
        )
        log_info(
            f"批量绑号已启动，轮次={self.round_number}，"
            f"数量={len(self.batch_item_ids)}"
        )

    def stop_batch(self) -> None:
        if not self._batch_is_running():
            return

        log_info("用户请求中止批量绑号")
        if self.batch_stop_event is not None:
            self.batch_stop_event.set()
        if self.batch_process is not None:
            self.batch_process.terminate()
            self.batch_process.join(timeout=2.0)

        for item_id in self.batch_item_ids:
            status = self.progress_statuses.get(
                item_id,
                DEFAULT_PROGRESS,
            )
            if not status.startswith(TERMINAL_PROGRESS_PREFIXES):
                self.aborted_item_ids.add(item_id)
                self._set_account_progress(item_id, "已中止")

        self._reset_batch_controls()
        self._report_round_paused(f"第 {self.round_number} 轮已中止")
        self._run_emergency_cleanup()

    def _run_emergency_cleanup(self) -> None:
        """中止后兜底清理残留的 Steam/EA 进程与登录态（后台线程执行）。"""
        from binder.automation.credential_cleanup import run_emergency_cleanup_async

        run_emergency_cleanup_async()

    def _reset_batch_controls(self) -> None:
        self.batch_process = None
        self.batch_stop_event = None
        self.batch_item_ids = []
        self.batch_start_button.configure(state=tk.NORMAL)
        self.batch_stop_button.configure(state=tk.DISABLED)
        self.batch_count_entry.configure(state=tk.NORMAL)
        self.next_round_button.configure(state=tk.DISABLED)

    def _show_admin_warning(self) -> None:
        messagebox.showwarning(
            "当前不是管理员权限",
            "程序已检测到当前不是管理员权限。\n"
            "账号可以读取，但启动绑号流程前需要以管理员身份重新运行。",
            parent=self,
        )

    def _queue_csv_progress(
        self,
        item_id: str,
        account: Account,
        status: str,
    ) -> None:
        """把账号最新进度加入待写回队列，由轮询定时统一写入 CSV。"""
        if self.loaded_file is None or account.csv_row_index < 0:
            return
        self.pending_csv_progress[item_id] = (
            account.csv_row_index,
            account.steam_account,
            account.is_bound,
            status,
            account.skip_status,
        )

    def _flush_csv_progress(self) -> None:
        if not self.pending_csv_progress:
            return
        updates = list(self.pending_csv_progress.values())
        self.pending_csv_progress.clear()
        if self.loaded_file is None:
            return
        write_back_account_progress(self.loaded_file, updates)

    def _set_account_progress(self, item_id: str, status: str) -> None:
        previous_status = self.progress_statuses.get(item_id)
        self.progress_statuses[item_id] = status
        if previous_status != status:
            log_info(
                f"绑定状态变化 account_id={item_id}: "
                f"{previous_status or '无'} -> {status}"
            )

        # 子进程报"账号信息有误"时同步打上跳过标记，写回 CSV 后
        # 后续轮次自动跳过，不再每轮空转重试。
        if status.startswith(ACCOUNT_INFO_ERROR_PREFIX):
            self._mark_account_info_error(item_id)

        if not self.account_table.exists(item_id):
            return

        if status == "已绑定":
            account = self.item_accounts.get(item_id)
            if account is not None and not account.is_bound:
                self._replace_account(
                    item_id,
                    replace(account, is_bound=True),
                )

        account = self.item_accounts.get(item_id)
        if account is None:
            return
        item_index = (
            list(self.account_table.get_children()).index(item_id) + 1
        )
        self.account_table.item(
            item_id,
            tags=self._row_tags(item_index, account),
            values=self._account_row_values(item_index, account, status),
        )

        if previous_status != status:
            self._queue_csv_progress(item_id, account, status)

    def _poll_progress_queue(self) -> None:
        try:
            while True:
                account_id, status = self.progress_queue.get_nowait()
                if account_id in self.aborted_item_ids:
                    continue
                self._set_account_progress(account_id, status)
        except Empty:
            pass

        for account_id, process in list(self.account_processes.items()):
            if process.is_alive():
                continue
            process.join(timeout=0)
            del self.account_processes[account_id]

        if (
            self.batch_process is not None
            and not self.batch_process.is_alive()
        ):
            self.batch_process.join(timeout=0)
            exit_code = self.batch_process.exitcode
            if exit_code not in (0, None):
                for item_id in self.batch_item_ids:
                    status = self.progress_statuses.get(
                        item_id,
                        DEFAULT_PROGRESS,
                    )
                    if not status.startswith(
                        TERMINAL_PROGRESS_PREFIXES
                    ):
                        self._set_account_progress(
                            item_id,
                            "失败：批量子进程异常退出",
                        )
            self._reset_batch_controls()
            if exit_code in (0, None):
                self._report_round_paused(
                    f"第 {self.round_number} 轮完成"
                )
            else:
                self.round_status_label.configure(
                    text="批量子进程异常退出，请查看 error.log"
                )

        self._flush_csv_progress()
        self.after(150, self._poll_progress_queue)

    def _on_close(self) -> None:
        self._flush_csv_progress()
        if self.batch_process is not None:
            if self.batch_process.is_alive():
                if self.batch_stop_event is not None:
                    self.batch_stop_event.set()
                self.batch_process.terminate()
            self.batch_process.join(timeout=1.0)

        for process in self.account_processes.values():
            if process.is_alive():
                process.terminate()
            process.join(timeout=1.0)

        self.progress_queue.close()
        self.progress_queue.join_thread()
        self.destroy()

    def _show_warnings(self, warnings: Iterable[str]) -> None:
        warning_list = list(warnings)
        preview = "\n".join(warning_list[:10])
        if len(warning_list) > 10:
            preview += f"\n……另有 {len(warning_list) - 10} 条警告"

        messagebox.showwarning(
            "账号读取完成",
            f"文件已读取，但存在以下问题：\n\n{preview}",
            parent=self,
        )


def _configure_debug_from_args(argv: Sequence[str]) -> None:
    """处理调试相关命令行参数（在导入重量级模块前设置环境变量）。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Steam / EA 账号绑定工具",
    )
    parser.add_argument(
        "--debug-capture",
        action="store_true",
        help="开启模板识别调试：按工作顺序记录窗口枚举、截图与匹配分数",
    )
    parser.add_argument(
        "--debug-all-windows",
        action="store_true",
        help="配合 --debug-capture，额外截图所有可见顶层窗口",
    )
    parser.add_argument(
        "--debug-dir",
        default=None,
        help="调试输出目录，默认 <程序目录>/debug_capture/<时间戳>_<pid>",
    )
    parser.add_argument(
        "--snapshot-windows",
        action="store_true",
        help="只做一次窗口枚举与截图（并截图所有可见窗口）后退出",
    )
    args, _ = parser.parse_known_args(argv)

    if args.debug_capture or args.snapshot_windows:
        os.environ["STEAM_EA_DEBUG_CAPTURE"] = "1"
    if args.debug_all_windows or args.snapshot_windows:
        os.environ["STEAM_EA_DEBUG_ALL_WINDOWS"] = "1"
    if args.debug_dir:
        os.environ["STEAM_EA_DEBUG_DIR"] = args.debug_dir

    if not args.snapshot_windows:
        return

    from binder.automation import debug_trace
    from binder.automation.window_capture import capture_window
    from binder.automation.win_utils import find_windows

    debug_trace.reset()
    debug_trace.dump_all_windows("命令行快照")
    for window_title in ("Steam", "EA"):
        hwnds = find_windows(window_title)
        debug_trace.log_window_enum(window_title, hwnds)
        with debug_trace.force_frames():
            for hwnd in hwnds:
                try:
                    capture_window(hwnd, note=f"{window_title}_{hwnd}")
                except Exception as error:
                    debug_trace.log(
                        f"截图失败 hwnd={hwnd}: "
                        f"{type(error).__name__}: {error}"
                    )
    print(f"调试输出目录: {debug_trace.session_dir()}")
    raise SystemExit(0)


def main() -> None:
    multiprocessing.freeze_support()
    _configure_debug_from_args(sys.argv[1:])
    install_global_exception_hooks()
    app = BinderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
