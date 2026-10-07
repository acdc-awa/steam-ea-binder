"""PySide6 版本的 Steam / EA 账号绑定工具界面。"""

from __future__ import annotations
import multiprocessing
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from queue import Empty
from typing import Any, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from binder.app_logging import install_global_exception_hooks, log_exception, log_info
from binder.app_runtime import is_running_as_admin
from binder.flows.errors import ACCOUNT_INFO_ERROR_PREFIX
from main import (
    APP_TITLE,
    DEFAULT_PROGRESS,
    SKIP_REASON_INFO_ERROR,
    SKIP_REASON_TEMP,
    TERMINAL_PROGRESS_PREFIXES,
    Account,
    _configure_debug_from_args,
    batch_binding_worker_entry,
    binding_worker_entry,
    load_accounts,
    mask_secret,
    write_back_account_progress,
)


COLUMNS = (
    "序号",
    "Steam 账号",
    "Steam 密码",
    "EA 邮箱",
    "EA 密码",
    "EA 2FA 16位码",
    "是否已绑定",
    "当前绑号进度",
    "跳过状态",
)


class AccountEditDialog(QDialog):
    def __init__(
        self,
        account: Account,
        secrets_visible: bool,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._original_account = account
        self.setWindowTitle("修改账号")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(12)
        layout.addLayout(form)

        self.steam_account = QLineEdit(account.steam_account)
        self.steam_password = QLineEdit(account.steam_password)
        self.ea_email = QLineEdit(account.ea_email)
        self.ea_password = QLineEdit(account.ea_password)
        self.ea_2fa_secret = QLineEdit(account.ea_2fa_secret)
        self.bound_status = QComboBox()
        self.bound_status.addItems(("未绑定", "已绑定"))
        self.bound_status.setCurrentText(
            "已绑定" if account.is_bound else "未绑定"
        )

        self.secret_entries = (
            self.steam_password,
            self.ea_password,
            self.ea_2fa_secret,
        )
        self.show_secrets = QCheckBox("显示密码和16位码")
        self.show_secrets.setChecked(secrets_visible)
        self.show_secrets.toggled.connect(self._set_secret_visibility)
        self._set_secret_visibility(secrets_visible)

        form.addRow("Steam 账号", self.steam_account)
        form.addRow("Steam 密码", self.steam_password)
        form.addRow("EA 邮箱", self.ea_email)
        form.addRow("EA 密码", self.ea_password)
        form.addRow("EA 2FA 16位码", self.ea_2fa_secret)
        form.addRow("是否已绑定", self.bound_status)
        form.addRow("", self.show_secrets)

        # 改完账密标记还在，容易让人以为"修好了为什么还跳过"，
        # 所以在对话框里直接说明。
        if account.skip_status:
            skip_hint = QLabel(
                f"该账号已标记为“{account.skip_status}”，修改账密不会自动"
                "取消标记；核对无误后请在右键菜单里手动“取消跳过标记”。"
            )
            skip_hint.setWordWrap(True)
            skip_hint.setStyleSheet("color: #B25E00;")
            layout.addWidget(skip_hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_secret_visibility(self, visible: bool) -> None:
        echo_mode = (
            QLineEdit.EchoMode.Normal
            if visible
            else QLineEdit.EchoMode.Password
        )
        for entry in self.secret_entries:
            entry.setEchoMode(echo_mode)

    def account(self) -> Account:
        # 用 replace 保留 csv_row_index / csv_progress / skip_status：
        # 跳过标记只进不出，改账密不能顺手把它清掉。
        return replace(
            self._original_account,
            steam_account=self.steam_account.text().strip(),
            steam_password=self.steam_password.text(),
            ea_email=self.ea_email.text().strip(),
            ea_password=self.ea_password.text(),
            ea_2fa_secret=self.ea_2fa_secret.text().strip(),
            is_bound=self.bound_status.currentText() == "已绑定",
        )


class QtBinderWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.is_admin = is_running_as_admin()
        permission = "管理员权限" if self.is_admin else "非管理员权限"
        debug_suffix = ""
        try:
            from binder.automation import debug_trace
            if debug_trace.is_enabled():
                debug_suffix = " · 调试截图已开启"
        except Exception:
            debug_suffix = ""
        self.setWindowTitle(
            f"{APP_TITLE}（Qt / {permission}）{debug_suffix}"
        )
        self.resize(1320, 720)
        self.setMinimumSize(1040, 540)

        self.item_accounts: Dict[str, Account] = {}
        self.row_ids: List[str] = []
        self.progress_statuses: Dict[str, str] = {}
        self.account_processes: Dict[str, multiprocessing.Process] = {}
        self.aborted_item_ids: Set[str] = set()
        self.account_generation = 0
        self.secrets_masked = True

        self.process_context = multiprocessing.get_context("spawn")
        self.progress_queue = self.process_context.Queue()
        self.batch_process: Optional[multiprocessing.Process] = None
        self.batch_stop_event: Any = None
        self.batch_item_ids: List[str] = []
        self.round_number = 0
        self.loaded_file: Optional[Path] = None
        self.pending_csv_progress: Dict[str, Tuple[int, str, bool, str, str]] = {}

        self._build_ui()
        self._apply_style()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_progress_queue)
        self.poll_timer.start(150)

        log_info(
            "Qt GUI程序启动，管理员权限="
            f"{'是' if self.is_admin else '否'}"
        )
        if not self.is_admin:
            QTimer.singleShot(300, self._show_admin_warning)

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("appRoot")
        self.setCentralWidget(central)
        page = QVBoxLayout(central)
        page.setContentsMargins(24, 20, 24, 24)
        page.setSpacing(14)

        title = QLabel("账号绑定管理")
        title.setObjectName("pageTitle")
        subtitle = QLabel("管理 Steam 与 EA 账号，并按顺序执行自动绑定任务")
        subtitle.setObjectName("pageSubtitle")
        page.addWidget(title)
        page.addWidget(subtitle)

        toolbar_card = QFrame()
        toolbar_card.setObjectName("card")
        toolbar = QHBoxLayout(toolbar_card)
        toolbar.setContentsMargins(14, 12, 14, 12)
        toolbar.setSpacing(8)

        self.import_button = QPushButton("读取账号")
        self.import_button.setObjectName("accentButton")
        self.import_button.clicked.connect(self.choose_account_file)
        toolbar.addWidget(self.import_button)

        self.mask_button = QPushButton("显示密码/16位码")
        self.mask_button.clicked.connect(self.toggle_secret_mask)
        toolbar.addWidget(self.mask_button)

        self.switch_button = QPushButton("切换到 Tk")
        self.switch_button.clicked.connect(self.switch_to_tk)
        toolbar.addWidget(self.switch_button)

        toolbar.addSpacing(8)
        toolbar.addWidget(QLabel("每轮数量"))
        self.batch_count = QSpinBox()
        self.batch_count.setRange(1, 99999)
        self.batch_count.setValue(1)
        self.batch_count.setFixedWidth(80)
        toolbar.addWidget(self.batch_count)

        self.batch_start_button = QPushButton("批量启动")
        self.batch_start_button.setObjectName("accentButton")
        self.batch_start_button.clicked.connect(self.start_batch)
        toolbar.addWidget(self.batch_start_button)

        self.batch_stop_button = QPushButton("中止")
        self.batch_stop_button.setObjectName("dangerButton")
        self.batch_stop_button.setEnabled(False)
        self.batch_stop_button.clicked.connect(self.stop_batch)
        toolbar.addWidget(self.batch_stop_button)

        self.next_round_button = QPushButton("下一轮")
        self.next_round_button.setEnabled(False)
        self.next_round_button.clicked.connect(self.start_next_round)
        toolbar.addWidget(self.next_round_button)

        self.round_status = QLabel("就绪")
        toolbar.addWidget(self.round_status, stretch=1)
        page.addWidget(toolbar_card)

        table_card = QFrame()
        table_card.setObjectName("card")
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(1, 1, 1, 1)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(
            self._show_context_menu
        )

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        table_layout.addWidget(self.table)
        page.addWidget(table_card, stretch=1)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                font-family: "Segoe UI";
                font-size: 10pt;
                color: #202020;
            }
            #appRoot {
                background: #f3f3f3;
            }
            #pageTitle {
                font-size: 21pt;
                font-weight: 600;
            }
            #pageSubtitle {
                color: #666666;
                margin-bottom: 4px;
            }
            #card {
                background: white;
                border: 1px solid #e3e3e3;
                border-radius: 8px;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #c7c7c7;
                border-radius: 5px;
                padding: 7px 13px;
            }
            QPushButton:hover {
                background: #f5f5f5;
            }
            QPushButton:pressed {
                background: #e7e7e7;
            }
            QPushButton:disabled {
                color: #9a9a9a;
                background: #f1f1f1;
            }
            #accentButton {
                color: white;
                background: #0067c0;
                border-color: #0067c0;
            }
            #accentButton:hover {
                background: #1976c8;
            }
            #dangerButton {
                color: white;
                background: #c42b1c;
                border-color: #c42b1c;
            }
            QLineEdit, QSpinBox, QComboBox {
                background: white;
                border: 1px solid #b8b8b8;
                border-radius: 4px;
                padding: 6px 8px;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
                border: 2px solid #0067c0;
            }
            QTableWidget {
                background: white;
                alternate-background-color: #fafafa;
                border: 0;
                gridline-color: #eeeeee;
                selection-background-color: #dceeff;
                selection-color: #111111;
            }
            QHeaderView::section {
                background: #f7f7f7;
                border: 0;
                border-bottom: 1px solid #e4e4e4;
                padding: 9px 8px;
                font-weight: 600;
            }
            QMenu {
                background: white;
                border: 1px solid #d8d8d8;
                padding: 5px;
            }
            QMenu::item {
                padding: 7px 28px 7px 10px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background: #e5f1fb;
            }
            """
        )

    def _show_admin_warning(self) -> None:
        QMessageBox.warning(
            self,
            "当前不是管理员权限",
            "账号可以读取，但启动绑号流程前需要以管理员身份重新运行。",
        )

    def choose_account_file(self) -> None:
        if self._any_process_running():
            QMessageBox.warning(
                self,
                "流程正在运行",
                "请先等待当前流程结束或中止批量流程。",
            )
            return

        dialog = QFileDialog(self, "选择账号 CSV 文件")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setNameFilter("CSV 文件 (*.csv)")
        dialog.setWindowFlag(
            Qt.WindowType.WindowStaysOnTopHint,
            True,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected_files = dialog.selectedFiles()
        if not selected_files:
            return
        path = selected_files[0]

        try:
            accounts, warnings, _ = load_accounts(Path(path))
        except Exception as error:
            log_exception(f"Qt界面读取CSV失败，path={path}")
            QMessageBox.critical(self, "读取失败", str(error))
            return

        self._flush_csv_progress()
        self.account_generation += 1
        self.item_accounts.clear()
        self.row_ids.clear()
        self.progress_statuses.clear()
        self.aborted_item_ids.clear()
        self.loaded_file = Path(path)
        for index, account in enumerate(accounts, start=1):
            item_id = f"qt_account_{self.account_generation}_{index}"
            self.item_accounts[item_id] = account
            self.row_ids.append(item_id)
            self.progress_statuses[item_id] = (
                account.csv_progress or DEFAULT_PROGRESS
            )
        self._refresh_table()
        self.round_number = 0
        self.next_round_button.setEnabled(False)
        self.round_status.setText(
            f"已读取 {len(accounts)} 个账号，点「批量启动」开始第 1 轮"
        )

        log_info(
            f"Qt界面账号文件读取完成 file={Path(path).name} "
            f"count={len(accounts)}"
        )
        if warnings:
            QMessageBox.warning(
                self,
                "账号读取完成",
                "\n".join(warnings[:10]),
            )

    def _display_values(
        self,
        row_index: int,
        account: Account,
        progress: str,
    ) -> Tuple[str, ...]:
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
            str(row_index + 1),
            account.steam_account,
            steam_password,
            account.ea_email,
            ea_password,
            ea_2fa_secret,
            "已绑定" if account.is_bound else "未绑定",
            progress,
            account.skip_status,
        )

    def _refresh_table(self) -> None:
        self.table.setRowCount(len(self.row_ids))
        for row_index, item_id in enumerate(self.row_ids):
            self._update_row(row_index, item_id)

    def _update_row(self, row_index: int, item_id: str) -> None:
        account = self.item_accounts[item_id]
        progress = self.progress_statuses.get(item_id, DEFAULT_PROGRESS)
        values = self._display_values(row_index, account, progress)
        for column, value in enumerate(values):
            item = self.table.item(row_index, column)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(row_index, column, item)
            item.setText(value)
            if column in (0, 6, 7, 8):
                item.setTextAlignment(
                    int(Qt.AlignmentFlag.AlignCenter)
                )
            if account.skip_status:
                # 跳过的账号用橙色，和"已绑定"的灰区分开：
                # 一个是完成，一个是待核对。
                color = QColor("#B25E00")
            else:
                color = QColor("#687078" if account.is_bound else "#202020")
            item.setForeground(QBrush(color))
        self.table.item(row_index, 0).setData(
            Qt.ItemDataRole.UserRole,
            item_id,
        )

    def toggle_secret_mask(self) -> None:
        self.secrets_masked = not self.secrets_masked
        self.mask_button.setText(
            "显示密码/16位码"
            if self.secrets_masked
            else "隐藏密码/16位码"
        )
        self._refresh_table()

    def _item_id_for_row(self, row: int) -> Optional[str]:
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _selected_item_id(self) -> Optional[str]:
        return self._item_id_for_row(self.table.currentRow())

    def _show_context_menu(self, position: Any) -> None:
        row = self.table.rowAt(position.y())
        item_id = self._item_id_for_row(row)
        if item_id is None:
            return
        self.table.selectRow(row)

        account = self.item_accounts[item_id]
        process = self.account_processes.get(item_id)
        start_disabled = (
            account.is_bound
            or bool(account.skip_status)
            or self._batch_is_running()
            or (process is not None and process.is_alive())
        )

        menu = QMenu(self)
        start_action = QAction("启动", menu)
        start_action.setEnabled(not start_disabled)
        start_action.triggered.connect(self.start_selected_account)
        menu.addAction(start_action)
        mark_action = QAction("标记为已绑定", menu)
        mark_action.setEnabled(not start_disabled)
        mark_action.triggered.connect(self.mark_selected_bound)
        menu.addAction(mark_action)
        menu.addSeparator()
        # 跳过标记是纯元数据操作，不碰正在跑的流程，所以批量运行期间
        # 也允许标记 —— 发现账号资料不对时能立刻把它从后续轮次摘掉。
        info_error_action = QAction("标记为信息有误", menu)
        info_error_action.setEnabled(not account.skip_status)
        info_error_action.triggered.connect(self.mark_selected_info_error)
        menu.addAction(info_error_action)
        temp_skip_action = QAction("标记为暂不登录", menu)
        temp_skip_action.setEnabled(not account.skip_status)
        temp_skip_action.triggered.connect(self.mark_selected_temp_skip)
        menu.addAction(temp_skip_action)
        clear_skip_action = QAction("取消跳过标记", menu)
        clear_skip_action.setEnabled(bool(account.skip_status))
        clear_skip_action.triggered.connect(self.clear_selected_skip)
        menu.addAction(clear_skip_action)
        menu.addSeparator()
        edit_action = menu.addAction("修改")
        edit_action.triggered.connect(self.edit_selected_account)
        delete_action = menu.addAction("删除")
        delete_action.triggered.connect(self.delete_selected_account)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def start_selected_account(self) -> None:
        item_id = self._selected_item_id()
        if item_id is None:
            return
        account = self.item_accounts[item_id]
        if account.is_bound:
            QMessageBox.information(
                self,
                "账号已绑定",
                "该账号已绑定，不能再次启动绑号流程。",
            )
            return
        if account.skip_status:
            QMessageBox.information(
                self,
                "账号已跳过",
                f"该账号已标记为“{account.skip_status}”，不会启动绑号流程。\n"
                "请先核对账号资料，再用右键菜单“取消跳过标记”恢复。",
            )
            return
        if not self.is_admin:
            QMessageBox.critical(
                self,
                "需要管理员权限",
                "请以管理员身份重新运行程序。",
            )
            return
        if self._batch_is_running():
            QMessageBox.information(
                self,
                "批量流程正在运行",
                "请先等待或中止当前批量流程。",
            )
            return
        process = self.account_processes.get(item_id)
        if process is not None and process.is_alive():
            return

        self.aborted_item_ids.discard(item_id)
        self._set_progress(item_id, "Steam登录中")
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
            name=f"qt-steam-binder-{item_id}",
            daemon=True,
        )
        try:
            process.start()
        except Exception as error:
            log_exception(f"Qt启动绑号子进程失败，account_id={item_id}")
            self._set_progress(
                item_id,
                f"启动失败：{type(error).__name__}: {error}",
            )
            return
        self.account_processes[item_id] = process

    def mark_selected_bound(self) -> None:
        """手动把账号标记为已绑定，并立即写回 CSV。"""
        item_id = self._selected_item_id()
        if item_id is None or not self._can_change_account(item_id):
            return

        account = self.item_accounts[item_id]
        if account.is_bound:
            QMessageBox.information(
                self,
                "账号已绑定",
                "该账号已标记为已绑定。",
            )
            return

        result = QMessageBox.question(
            self,
            "标记已绑定",
            f"确定将 Steam 账号“{account.steam_account}”标记为已绑定吗？\n"
            "标记后会写入 CSV，批量启动和下一轮都会跳过它。",
        )
        if result != QMessageBox.StandardButton.Yes:
            return

        self._set_progress(item_id, "已绑定")
        log_info(f"Qt手动标记已绑定，account_id={item_id}")

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
        result = QMessageBox.question(
            self,
            "取消跳过标记",
            f"确定取消该账号的“{account.skip_status}”标记吗？\n"
            "取消后它会重新参与批量绑定。",
        )
        if result != QMessageBox.StandardButton.Yes:
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
        result = QMessageBox.question(self, title, message)
        if result != QMessageBox.StandardButton.Yes:
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
        self.item_accounts[item_id] = updated_account
        progress = self.progress_statuses.get(item_id, DEFAULT_PROGRESS)
        if item_id in self.row_ids:
            self._update_row(self.row_ids.index(item_id), item_id)
        self._queue_csv_progress(item_id, updated_account, progress)
        log_info(
            f"Qt跳过标记变化 account_id={item_id}: "
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
        log_info(f"Qt自动标记账号信息有误，account_id={item_id}")

    def edit_selected_account(self) -> None:
        item_id = self._selected_item_id()
        if item_id is None or not self._can_change_account(item_id):
            return
        dialog = AccountEditDialog(
            self.item_accounts[item_id],
            not self.secrets_masked,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated_account = dialog.account()
        self.item_accounts[item_id] = updated_account
        self._update_row(self.row_ids.index(item_id), item_id)
        self._queue_csv_progress(
            item_id,
            updated_account,
            self.progress_statuses.get(item_id, DEFAULT_PROGRESS),
        )
        log_info(f"Qt界面账号信息已修改，account_id={item_id}")

    def delete_selected_account(self) -> None:
        item_id = self._selected_item_id()
        if item_id is None or not self._can_change_account(item_id):
            return
        account = self.item_accounts[item_id]
        result = QMessageBox.question(
            self,
            "删除账号",
            f"确定删除 Steam 账号“{account.steam_account}”吗？",
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        self.aborted_item_ids.add(item_id)
        self.row_ids.remove(item_id)
        self.item_accounts.pop(item_id, None)
        self.progress_statuses.pop(item_id, None)
        self._refresh_table()
        log_info(f"Qt界面账号已删除，account_id={item_id}")

    def _can_change_account(self, item_id: str) -> bool:
        if self._batch_is_running():
            QMessageBox.warning(
                self,
                "批量流程正在运行",
                "批量流程运行时不能修改或删除账号。",
            )
            return False
        process = self.account_processes.get(item_id)
        if process is not None and process.is_alive():
            QMessageBox.warning(
                self,
                "账号正在运行",
                "当前账号正在执行绑号流程。",
            )
            return False
        return True

    def start_batch(self) -> None:
        if not self.is_admin:
            QMessageBox.critical(
                self,
                "需要管理员权限",
                "批量绑号需要以管理员身份运行程序。",
            )
            return
        if self._any_process_running():
            QMessageBox.information(
                self,
                "流程正在运行",
                "请先等待当前流程结束。",
            )
            return

        eligible = self._eligible_item_ids()
        count = self.batch_count.value()
        if count > len(eligible):
            QMessageBox.warning(
                self,
                "账号数量不足",
                f"当前仅有 {len(eligible)} 个未处理账号。",
            )
            return

        self.batch_item_ids = eligible[:count]
        payloads = []
        for item_id in self.batch_item_ids:
            account = self.item_accounts[item_id]
            self.aborted_item_ids.discard(item_id)
            self._set_progress(item_id, "批量等待中")
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
            name="qt-steam-binder-batch",
            daemon=True,
        )
        try:
            self.batch_process.start()
        except Exception as error:
            log_exception("Qt启动批量绑号子进程失败")
            for item_id in self.batch_item_ids:
                self._set_progress(
                    item_id,
                    f"启动失败：{type(error).__name__}: {error}",
                )
            self._reset_batch_controls()
            return

        self.round_number += 1
        self.batch_start_button.setEnabled(False)
        self.batch_stop_button.setEnabled(True)
        self.batch_count.setEnabled(False)
        self.next_round_button.setEnabled(False)
        self.round_status.setText(
            f"第 {self.round_number} 轮进行中"
            f"（{len(self.batch_item_ids)} 个账号）…"
        )
        log_info(
            f"Qt批量绑号已启动，轮次={self.round_number}，"
            f"数量={len(self.batch_item_ids)}"
        )

    def start_next_round(self) -> None:
        """人工触发下一轮：按“每轮数量”继续处理剩余账号。"""
        if self._batch_is_running():
            return
        self.start_batch()
        if not self._batch_is_running() and self._eligible_item_ids():
            self.next_round_button.setEnabled(True)

    def _report_round_paused(self, reason: str) -> None:
        """一轮结束或中止后更新状态栏，并在仍有账号时启用“下一轮”。"""
        remaining = self._eligible_item_ids()
        if remaining:
            self.round_status.setText(
                f"{reason} · 剩余 {len(remaining)} 个 · 点「下一轮」继续"
            )
            self.next_round_button.setEnabled(True)
        else:
            self.round_status.setText(f"{reason} · 没有待处理账号了")
            self.next_round_button.setEnabled(False)

    def stop_batch(self) -> None:
        if not self._batch_is_running():
            return
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
                self._set_progress(item_id, "已中止")
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
        self.batch_start_button.setEnabled(True)
        self.batch_stop_button.setEnabled(False)
        self.batch_count.setEnabled(True)
        self.next_round_button.setEnabled(False)

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

    def _set_progress(self, item_id: str, status: str) -> None:
        if item_id not in self.item_accounts:
            return
        previous = self.progress_statuses.get(item_id)
        self.progress_statuses[item_id] = status
        if previous != status:
            log_info(
                f"Qt绑定状态变化 account_id={item_id}: "
                f"{previous or '无'} -> {status}"
            )
        # 子进程报"账号信息有误"时同步打上跳过标记，写回 CSV 后
        # 后续轮次自动跳过，不再每轮空转重试。
        if status.startswith(ACCOUNT_INFO_ERROR_PREFIX):
            self._mark_account_info_error(item_id)
        if status == "已绑定":
            account = self.item_accounts[item_id]
            if not account.is_bound:
                self.item_accounts[item_id] = replace(
                    account,
                    is_bound=True,
                )
        self._update_row(self.row_ids.index(item_id), item_id)
        if previous != status:
            self._queue_csv_progress(
                item_id,
                self.item_accounts[item_id],
                status,
            )

    def _poll_progress_queue(self) -> None:
        try:
            while True:
                account_id, status = self.progress_queue.get_nowait()
                if account_id not in self.aborted_item_ids:
                    self._set_progress(account_id, status)
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
                    if not status.startswith(TERMINAL_PROGRESS_PREFIXES):
                        self._set_progress(
                            item_id,
                            "失败：批量子进程异常退出",
                        )
            self._reset_batch_controls()
            if exit_code in (0, None):
                self._report_round_paused(
                    f"第 {self.round_number} 轮完成"
                )
            else:
                self.round_status.setText(
                    "批量子进程异常退出，请查看 error.log"
                )

        self._flush_csv_progress()

    def _batch_is_running(self) -> bool:
        return (
            self.batch_process is not None
            and self.batch_process.is_alive()
        )

    def _eligible_item_ids(self) -> List[str]:
        """返回尚未绑定、未被跳过、可以进入下一轮的账号行 ID。"""
        return [
            item_id
            for item_id in self.row_ids
            if not self.item_accounts[item_id].is_bound
            and not self.item_accounts[item_id].skip_status
            and self.progress_statuses.get(
                item_id,
                DEFAULT_PROGRESS,
            ) != "已绑定"
        ]

    def _single_is_running(self) -> bool:
        return any(
            process.is_alive()
            for process in self.account_processes.values()
        )

    def _any_process_running(self) -> bool:
        return self._batch_is_running() or self._single_is_running()

    def switch_to_tk(self) -> None:
        if self._any_process_running():
            QMessageBox.warning(
                self,
                "流程正在运行",
                "请先等待当前流程结束或中止批量流程。",
            )
            return
        main_path = Path(__file__).resolve().parent / "main.py"
        subprocess.Popen(
            [sys.executable, str(main_path)],
            cwd=str(main_path.parent),
        )
        log_info("从Qt界面切换到Tk界面")
        self.close()

    def closeEvent(self, event: Any) -> None:
        self._flush_csv_progress()
        self.poll_timer.stop()
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
        event.accept()


def main() -> None:
    multiprocessing.freeze_support()
    _configure_debug_from_args(sys.argv[1:])
    install_global_exception_hooks()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = QtBinderWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
