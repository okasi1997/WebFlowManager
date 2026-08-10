from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QInputDialog, QLabel, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QStackedWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.conditions import decode_guard
from core.database import Database
from core.executor import WorkflowExecutor, find_variables
from .auth import profile_path
from ..table_view import capture_scroll_position, configure_table_view, restore_scroll_position
from ..ui_loader import load_ui_into, require, show_information, show_warning


# DB には処理しやすい英語値を保持し、画面上だけ日本語で表示する。
STATUS_LABELS = {
    'not_run': '未実行',
    'waiting': '待機中',
    'running': '実行中',
    'success': '成功',
    'failed': '失敗',
    'skipped': 'スキップ',
}

STATUS_COLORS = {
    'not_run': '#657786',
    'waiting': '#657786',
    'running': '#0b78c5',
    'success': '#218449',
    'failed': '#c23a32',
    'skipped': '#8a949d',
}


class ExecutionPage(QWidget):
    COLUMNS = ('実行グループ', '今回実行', '実行データ', '概要', '業務フロー', '現在のイベント', '実行状況')

    def __init__(self, project_dir: Path, db: Database) -> None:
        super().__init__()
        self.project_dir, self.db = project_dir, db
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='qt-workflow')
        self.future: Future | None = None
        self._log_lines: list[str] = []
        self._log_lock = Lock()
        self._runtime_lock = Lock()
        self._runtime_details: dict[int, tuple[str, str]] = {}
        self._table_signature: tuple[Any, ...] | None = None
        load_ui_into(self, 'execution.ui')

        root = require(self, QVBoxLayout, 'rootLayout')
        summary_card = require(self, QFrame, 'summaryCard')
        progress_card = require(self, QFrame, 'progressCard')
        summary_layout = require(self, QHBoxLayout, 'summaryLayout')
        separator = require(self, QFrame, 'summarySeparator1')
        separator.setFrameShape(QFrame.Shape.NoFrame)
        separator.setProperty('summarySeparator', True)
        progress_layout = require(self, QHBoxLayout, 'progressLayout')
        toolbar = require(self, QHBoxLayout, 'toolbar')

        self.run_button = require(self, QPushButton, 'runButton')
        self.run_button.clicked.connect(self.start)
        self.status = require(self, QLabel, 'statusLabel')
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.target_summary = require(self, QLabel, 'targetSummaryLabel')
        self.session_spin = require(self, QSpinBox, 'sessionSpin')
        self.session_spin.setValue(self.db.get_pcl_session_limit())
        self.session_spin.valueChanged.connect(self.db.set_pcl_session_limit)

        self.records = require(self, QTreeWidget, 'executionTree')
        self.records.setColumnCount(len(self.COLUMNS))
        self.records.setHeaderLabels(self.COLUMNS)
        configure_table_view(self.records)
        # 長い見出しによる既定の最小幅を解除し、設定した七列を画面内に収める。
        self.records.header().setMinimumSectionSize(54)
        # Windows の表示倍率を考慮し、通常幅では全七列が表示領域に収まる比率にする。
        for column, width in enumerate((90, 90, 190, 210, 200, 215, 155)):
            self.records.setColumnWidth(column, width)
        self.records.itemDoubleClicked.connect(self._record_double_clicked)

        self.log = require(self, QPlainTextEdit, 'logEdit')
        self.stack = require(self, QStackedWidget, 'executionStack')
        self.progress = require(self, QProgressBar, 'progressBar')
        self.progress_count = require(self, QLabel, 'progressCountLabel')
        self.status_tab = require(self, QPushButton, 'statusTabButton')
        self.log_tab = require(self, QPushButton, 'logTabButton')
        self.status_tab.clicked.connect(lambda: self.select_tab(0))
        self.log_tab.clicked.connect(lambda: self.select_tab(1))

        toggle_button = require(self, QPushButton, 'toggleExecutionButton')
        group_button = require(self, QPushButton, 'setGroupButton')
        clear_button = require(self, QPushButton, 'clearResultsButton')
        toggle_button.setToolTip('選択したデータの実行／スキップを切り替えます')
        group_button.setToolTip('選択したデータの実行グループを編集します')
        clear_button.setToolTip('すべての実行結果を未実行状態に戻します')
        toggle_button.clicked.connect(self.toggle_record)
        group_button.clicked.connect(self.set_group)
        clear_button.clicked.connect(self.clear_results)
        require(self, QPushButton, 'clearLogButton').clicked.connect(self.log.clear)
        require(self, QPushButton, 'copyLogButton').clicked.connect(
            lambda: QApplication.clipboard().setText(self.log.toPlainText())
        )

        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.select_tab(0)
        self.reload()

    def reload(self) -> None:
        records = self.db.list_data_records()
        with self._runtime_lock:
            runtime_details = dict(self._runtime_details)
        signature = tuple(
            (
                record['id'], record['execution_group'], record['enabled'], record['name'],
                record['summary'], record['execution_status'], runtime_details.get(record['id']),
            )
            for record in records
        )

        # タイマー更新で選択位置やスクロール位置が揺れないよう、内容が変わった時だけ再構築する。
        if signature != self._table_signature:
            scroll = capture_scroll_position(self.records)
            current = self.records.currentItem()
            selected_id = current.data(0, Qt.ItemDataRole.UserRole) if current else None
            group_totals: dict[str, int] = {}
            group_indexes: dict[str, int] = {}
            for record in records:
                group = str(record['execution_group'])
                group_totals[group] = group_totals.get(group, 0) + 1

            self.records.setUpdatesEnabled(False)
            self.records.clear()
            for record in records:
                group = str(record['execution_group'])
                group_indexes[group] = group_indexes.get(group, 0) + 1
                workflow, event = runtime_details.get(record['id'], ('-', '-'))
                status_key = str(record['execution_status'])
                item = QTreeWidgetItem([
                    group,
                    '実行' if record['enabled'] else 'スキップ',
                    f'({group_indexes[group]}/{group_totals[group]}) {record["name"]}',
                    record['summary'],
                    workflow,
                    event,
                    STATUS_LABELS.get(status_key, status_key),
                ])
                item.setData(0, Qt.ItemDataRole.UserRole, record['id'])
                item.setForeground(0, QColor('#0b78c5'))
                # 表全体ではなく実在する行だけに説明を付け、空白部分では表示しない。
                row_tooltip = '実行グループは編集、今回実行は実行／スキップをダブルクリックで変更できます'
                for column in range(len(self.COLUMNS)):
                    item.setToolTip(column, row_tooltip)
                item.setForeground(6, QColor(STATUS_COLORS.get(status_key, '#314557')))
                self.records.addTopLevelItem(item)
                if record['id'] == selected_id:
                    self.records.setCurrentItem(item)
            self.records.setUpdatesEnabled(True)
            restore_scroll_position(self.records, scroll)
            self._table_signature = signature

        terminal = {'success', 'failed', 'skipped'}
        complete = sum(record['execution_status'] in terminal for record in records)
        enabled = sum(1 for record in records if record['enabled'])
        self.target_summary.setText(f'実行対象 {enabled} / {len(records)} 件')
        self.progress.setMaximum(max(1, len(records)))
        self.progress.setValue(complete)
        self.progress_count.setText(f'{complete} / {len(records)}')

    def selected_record(self) -> dict[str, Any] | None:
        item = self.records.currentItem()
        if item is None:
            return None
        record_id = item.data(0, Qt.ItemDataRole.UserRole)
        return next((record for record in self.db.list_data_records() if record['id'] == record_id), None)

    def _record_double_clicked(self, _item: QTreeWidgetItem, column: int) -> None:
        # 操作可能な先頭二列だけをダブルクリックに対応させる。
        if self.future is not None:
            return
        if column == 0:
            self.set_group()
        elif column == 1:
            self.toggle_record()

    def toggle_record(self) -> None:
        record = self.selected_record()
        if record:
            self.db.set_data_record_enabled(record['id'], not record['enabled'])
            self._table_signature = None
            self.reload()

    def set_group(self) -> None:
        record = self.selected_record()
        if record is None:
            return
        group, ok = QInputDialog.getText(
            self, '実行グループ設定', '実行グループ', text=str(record['execution_group']),
        )
        if ok and group.strip():
            self.db.set_data_record_group(record['id'], group.strip())
            self._table_signature = None
            self.reload()

    def clear_results(self) -> None:
        for record in self.db.list_data_records():
            self.db.set_data_record_status(record['id'], 'not_run' if record['enabled'] else 'skipped')
        with self._runtime_lock:
            self._runtime_details.clear()
        self._table_signature = None
        self.reload()

    def select_tab(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for button, active in ((self.status_tab, index == 0), (self.log_tab, index == 1)):
            button.setProperty('activeTab', active)
            button.style().unpolish(button)
            button.style().polish(button)

    def append_log(self, message: str) -> None:
        with self._log_lock:
            self._log_lines.append(str(message))

    def _set_runtime_detail(self, record_id: int, workflow: str, event: str) -> None:
        with self._runtime_lock:
            self._runtime_details[record_id] = (workflow, event)

    @staticmethod
    def _numbered_name(value: dict[str, Any]) -> str:
        position = value.get('position', '')
        name = value.get('name', '')
        return f'{position}. {name}'.strip('. ')

    def poll(self) -> None:
        with self._log_lock:
            lines, self._log_lines = self._log_lines, []
        if lines:
            self.log.appendPlainText('\n'.join(lines))
        if self.future is None:
            return
        self.reload()
        if not self.future.done():
            return
        try:
            self.future.result()
            self.status.setText('実行完了')
        except Exception as error:
            self.status.setText('実行失敗')
            self.log.appendPlainText(str(error))
        self.future = None
        self.run_button.setEnabled(True)
        self.reload()

    def start(self) -> None:
        if self.future is not None:
            return
        workflows = [dict(row) for row in self.db.list_workflows() if row['enabled']]
        jobs = []
        for workflow in workflows:
            events = [dict(row) for row in self.db.list_events(workflow['id'])]
            if events:
                jobs.append({
                    'id': workflow['id'], 'position': workflow['position'], 'name': workflow['name'],
                    'events': events, 'guard': decode_guard(workflow['guard_json']),
                })
        records = self.db.list_data_records(enabled_only=True)
        if not jobs:
            show_information(self, '実行管理', '実行可能な業務フローがありません。')
            return
        if not records:
            show_information(self, '実行管理', '有効な実行データがありません。')
            return
        variables = sorted({name for job in jobs for name in find_variables(job['events'])})
        if variables:
            show_warning(self, '実行管理', '外部変数が必要です: ' + ', '.join(variables))
            return
        self.db.prepare_data_record_statuses()
        with self._runtime_lock:
            self._runtime_details.clear()
        self._table_signature = None
        self.log.clear()
        self.select_tab(0)
        self.status.setText('実行中')
        self.run_button.setEnabled(False)
        self.future = self.pool.submit(self._run, jobs, records)

    def _run(self, jobs: list[dict[str, Any]], records: list[dict[str, Any]]) -> None:
        groups: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            groups.setdefault(str(record['execution_group']), []).append(record)
        state_path = profile_path(self.project_dir, self.db.get_auth_profile())

        def steps(group_records):
            return [
                {
                    'phase': 'pcl', 'group': record['execution_group'], 'record': record,
                    'id': job['id'], 'position': job['position'], 'name': job['name'],
                    'events': job['events'], 'guard': job['guard'],
                }
                for record in group_records for job in jobs
            ]

        def step_start(step):
            record = step['record']
            self.db.set_data_record_status(record['id'], 'running')
            self._set_runtime_detail(record['id'], self._numbered_name(step), '-')
            self.append_log(f'{record["name"]} / {step["name"]}')
            return self.db.create_run(step['id'])

        def event_start(step, event):
            record = step['record']
            self._set_runtime_detail(
                record['id'], self._numbered_name(step), self._numbered_name(event),
            )

        def step_success(step, run_id):
            self.db.finish_run(run_id, 'success', '')
            record = step['record']
            self.db.update_data_record(record['id'], record['name'], record['data'])
            if step['id'] == jobs[-1]['id']:
                self.db.set_data_record_status(record['id'], 'success')

        def step_failure(step, run_id, error):
            self.db.finish_run(run_id, 'failed', str(error))
            self.db.set_data_record_status(step['record']['id'], 'failed')
            self.append_log(f'ERROR: {error}')

        failures = []
        workers = max(1, min(self.db.get_pcl_session_limit(), len(groups)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='qt-flow-group') as group_pool:
            futures = {
                group_pool.submit(
                    WorkflowExecutor(self.project_dir, self.append_log).run_batch,
                    steps(group_records), {}, step_start, step_success, step_failure, event_start,
                    self.db.get_browser_visible(), f'group_{group}', state_path,
                ): group
                for group, group_records in groups.items()
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    failures.append(f'{futures[future]}: {error}')
        if failures:
            raise RuntimeError('\n'.join(failures))

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
