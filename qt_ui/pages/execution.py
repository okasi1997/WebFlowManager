from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import re
from threading import Event, Lock
from typing import Any, Callable

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFrame, QInputDialog, QLabel, QMenu, QPlainTextEdit,
    QHeaderView, QProgressBar, QPushButton, QSpinBox, QStackedWidget, QStyle,
    QStyledItemDelegate, QStyleOptionHeader, QStyleOptionViewItem, QTreeWidget,
    QTreeWidgetItem, QWidget,
)

from core.conditions import decode_guard
from core.daily_log import DailyLogWriter
from core.database import Database
from i18n import tr
from .auth import profile_path
from ..table_view import (
    HierarchicalReorderTreeWidget, capture_scroll_position, configure_row_move_tooltips,
    configure_table_view, restore_scroll_position, set_row_enabled_appearance,
)
from ..ui_loader import (
    load_ui_into, localize_dialog_buttons, require, show_information, show_warning,
)


# DB には処理しやすい英語値を保持し、画面上だけ日本語で表示する。
STATUS_LABELS = {
    'not_run': '未実行',
    'waiting': '待機中',
    'running': '実行中',
    'error_waiting': 'エラー確認中',
    'stopping': '停止中',
    'stopped': '中止',
    'success': '成功',
    'failed': '失敗',
    'skipped': 'スキップ',
}

STATUS_COLORS = {
    'not_run': '#657786',
    'waiting': '#657786',
    'running': '#0b78c5',
    'error_waiting': '#c23a32',
    'stopping': '#c47a13',
    'stopped': '#b66a18',
    'success': '#218449',
    'failed': '#c23a32',
    'skipped': '#8a949d',
}

STATUS_SORT_ORDER = {
    'running': 0,
    'error_waiting': 1,
    'stopping': 2,
    'waiting': 3,
    'not_run': 4,
    'stopped': 5,
    'failed': 6,
    'success': 7,
    'skipped': 8,
}

ACTION_KIND_ROLE = Qt.ItemDataRole.UserRole + 1
ACTION_ENABLED_ROLE = Qt.ItemDataRole.UserRole + 2


def natural_sort_key(value: Any) -> tuple[tuple[int, Any], ...]:
    """数値部分を数値として比較し、1、2、10 の順に並べる。"""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r'(\d+)', str(value)) if part
    )


class ExecutionActionDelegate(QStyledItemDelegate):
    """実在するボタンを行数分作らず、表示範囲へ実行操作を軽量描画する。"""

    COLORS = {
        'start': ('#27945a', '#217d4d', '#ffffff'),
        'stop': ('#cf443c', '#b93832', '#ffffff'),
        'finish': ('#cf443c', '#b93832', '#ffffff'),
        'stopping': ('#aeb9c2', '#a2aeb7', '#f5f7f8'),
        'disabled': ('#aeb9c2', '#a2aeb7', '#f5f7f8'),
    }

    def __init__(self, clicked: Callable[[int], None], parent: QWidget) -> None:
        super().__init__(parent)
        self.clicked = clicked

    @staticmethod
    def button_rect(cell_rect: QRect) -> QRect:
        width = min(68, max(0, cell_rect.width() - 12))
        height = min(32, max(0, cell_rect.height() - 6))
        return QRect(
            cell_rect.left() + (cell_rect.width() - width) // 2,
            cell_rect.top() + (cell_rect.height() - height) // 2,
            width, height,
        )

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index,
    ) -> None:
        # 選択行の背景は共通 Delegate と同じ Qt 標準描画へ任せる。
        base = QStyleOptionViewItem(option)
        base.text = ''
        base.state &= ~QStyle.StateFlag.State_MouseOver
        base.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, base, index)

        kind = str(index.data(ACTION_KIND_ROLE) or 'start')
        enabled = bool(index.data(ACTION_ENABLED_ROLE))
        background, border, foreground = self.COLORS[kind if enabled else 'disabled']
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(border), 1))
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(self.button_rect(option.rect), 5, 5)
        font = option.font
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(foreground))
        painter.drawText(
            self.button_rect(option.rect), Qt.AlignmentFlag.AlignCenter,
            tr(
                '停止中' if kind == 'stopping'
                else ('終了' if kind == 'finish' else ('停止' if kind == 'stop' else '実行'))
            ),
        )
        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        if not bool(index.data(ACTION_ENABLED_ROLE)):
            return False
        clicked = (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and self.button_rect(option.rect).contains(event.position().toPoint())
        )
        keyboard = (
            event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)
        )
        if not clicked and not keyboard:
            return False
        record_id = index.data(Qt.ItemDataRole.UserRole)
        if record_id is not None:
            self.clicked(int(record_id))
            return True
        return False

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        base = super().sizeHint(option, index)
        return QSize(base.width(), max(40, base.height()))


class ExecutionSortHeader(QHeaderView):
    """列名を変えず、右端へ小さなソート方向と優先順位を描画する。"""

    def __init__(
        self,
        criteria_getter: Callable[[], list[tuple[int, Qt.SortOrder]]],
        parent: QWidget,
    ) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._criteria_getter = criteria_getter

    def sort_indicator(self, column: int) -> tuple[int, Qt.SortOrder] | None:
        """指定列の1始まり優先順位と方向を返す。描画とテストで共用する。"""
        for index, (current, order) in enumerate(self._criteria_getter(), 1):
            if current == column:
                return index, order
        return None

    def paintSection(self, painter: QPainter, rect, logical_index: int) -> None:
        if not rect.isValid():
            return
        indicator = self.sort_indicator(logical_index)
        criteria_count = len(self._criteria_getter())
        option = QStyleOptionHeader()
        self.initStyleOptionForIndex(option, logical_index)
        option.rect = rect
        option.sortIndicator = QStyleOptionHeader.SortIndicator.None_
        if indicator is not None:
            reserved = 30 if criteria_count > 1 else 17
            option.text = painter.fontMetrics().elidedText(
                option.text, Qt.TextElideMode.ElideRight,
                max(0, rect.width() - reserved - 12),
            )
        self.style().drawControl(QStyle.ControlElement.CE_Header, option, painter, self)
        if indicator is None:
            return

        priority, order = indicator
        painter.save()
        center_y = rect.center().y()
        arrow_x = rect.right() - 10
        arrow = QPolygon([
            QPoint(arrow_x - 4, center_y + (2 if order == Qt.SortOrder.AscendingOrder else -2)),
            QPoint(arrow_x + 4, center_y + (2 if order == Qt.SortOrder.AscendingOrder else -2)),
            QPoint(arrow_x, center_y + (-3 if order == Qt.SortOrder.AscendingOrder else 3)),
        ])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#0b78c5'))
        painter.drawPolygon(arrow)
        if criteria_count > 1:
            # 優先順位は主張しすぎない小さな通常数字として矢印の左へ置く。
            priority_font = painter.font()
            priority_font.setPointSizeF(max(7.0, priority_font.pointSizeF() - 2.0))
            painter.setFont(priority_font)
            painter.setPen(QColor('#657786'))
            painter.drawText(
                rect.right() - 31, rect.top(), 14, rect.height(),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                str(priority),
            )
        painter.restore()


class ExecutionOrderDialog(QDialog):
    """グループごとの固定実行順をドラッグ操作でまとめて編集する。"""

    def __init__(
        self, parent: QWidget, db: Database, initial_group: str | None=None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.records = db.list_data_records()
        self.records_by_id = {int(record['id']): record for record in self.records}
        self.orders: dict[str, list[int]] = {}
        for record in self.records:
            if not record['enabled']:
                continue
            self.orders.setdefault(str(record['execution_group']), []).append(int(record['id']))
        self.changed_groups: set[str] = set()
        self.current_group = ''
        load_ui_into(self, 'execution_order.ui')

        self.group_combo = require(self, QComboBox, 'groupCombo')
        for group in sorted(self.orders, key=natural_sort_key):
            self.group_combo.addItem(group)
        if initial_group in self.orders:
            self.group_combo.setCurrentText(initial_group)

        placeholder = require(self, QTreeWidget, 'orderTree')
        tree_layout = placeholder.parentWidget().layout()
        self.tree = HierarchicalReorderTreeWidget(placeholder.parentWidget())
        self.tree.setObjectName('orderTree')
        tree_layout.replaceWidget(placeholder, self.tree)
        placeholder.setObjectName('orderTreeDesignerPlaceholder')
        placeholder.setParent(None)
        placeholder.deleteLater()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([tr('実行データ'), tr('概要')])
        configure_table_view(self.tree, reorder=True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.tree.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.tree.setContainerTest(lambda _item: False)
        self.tree.orderChanged.connect(self._order_changed)
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 190)

        # イベント一覧と同じ操作で、選択中の実行データを一行ずつ移動する。
        move_up = require(self, QPushButton, 'moveUpButton')
        move_down = require(self, QPushButton, 'moveDownButton')
        configure_row_move_tooltips(move_up, move_down, '実行データ')
        move_up.clicked.connect(lambda: self.tree.moveSelected(-1))
        move_down.clicked.connect(lambda: self.tree.moveSelected(1))

        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.group_combo.currentTextChanged.connect(self._show_group)
        self._show_group(self.group_combo.currentText())

    def _capture_order(self) -> None:
        if not self.current_group:
            return
        self.orders[self.current_group] = [
            int(self.tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole))
            for index in range(self.tree.topLevelItemCount())
        ]

    def _show_group(self, group: str) -> None:
        self._capture_order()
        self.current_group = group
        self.tree.clear()
        record_ids = self.orders.get(group, [])
        total = len(record_ids)
        for index, record_id in enumerate(record_ids, 1):
            record = self.records_by_id[record_id]
            item = QTreeWidgetItem([f'({index}/{total}) {record["name"]}', record['summary']])
            item.setData(0, Qt.ItemDataRole.UserRole, record_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
            self.tree.addTopLevelItem(item)

    def _order_changed(self) -> None:
        self._capture_order()
        self.changed_groups.add(self.current_group)
        total = self.tree.topLevelItemCount()
        for index in range(total):
            item = self.tree.topLevelItem(index)
            record = self.records_by_id[int(item.data(0, Qt.ItemDataRole.UserRole))]
            item.setText(0, f'({index + 1}/{total}) {record["name"]}')

    def _save(self) -> None:
        self._capture_order()
        for group in self.changed_groups:
            self.db.reorder_enabled_group_data_records(group, self.orders[group])
        self.accept()


class ExecutionPage(QWidget):
    COLUMNS = ('操作', 'グループ', '今回実行', '実行データ', '概要', '実行状況', '業務フロー', '現在のイベント')

    def __init__(self, project_dir: Path, db: Database) -> None:
        super().__init__()
        self.project_dir, self.db = project_dir, db
        self.db.recover_interrupted_data_record_statuses()
        self.pool = ThreadPoolExecutor(max_workers=20, thread_name_prefix='qt-pcl')
        self._execution_mode: str | None = None
        self._queued_record_ids: list[int] = []
        self._active_tasks: dict[int, Future] = {}
        self._stop_requests: dict[int, Event] = {}
        self._batch_finished: set[int] = set()
        self._execution_jobs: list[dict[str, Any]] = []
        self._log_lines: list[str] = []
        self._log_lock = Lock()
        self.file_log = DailyLogWriter(self.project_dir)
        self._runtime_lock = Lock()
        self._runtime_details: dict[int, tuple[str, str]] = {}
        self._table_signature: tuple[Any, ...] | None = None
        self._sort_criteria: list[tuple[int, Qt.SortOrder]] = []
        self._running_display_order: list[int] | None = None
        load_ui_into(self, 'execution.ui')

        separator = require(self, QFrame, 'summarySeparator1')
        separator.setFrameShape(QFrame.Shape.NoFrame)
        separator.setProperty('summarySeparator', True)

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
        self.records.setHeader(ExecutionSortHeader(lambda: self._sort_criteria, self.records))
        configure_table_view(self.records)
        self.action_delegate = ExecutionActionDelegate(self._row_action, self.records)
        self.records.setItemDelegateForColumn(0, self.action_delegate)
        self.records.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # 長い見出しによる既定の最小幅を解除し、設定した八列を画面内に収める。
        header = self.records.header()
        header.setMinimumSectionSize(54)
        header.setSectionsClickable(True)
        # 複数列用の独自表示を使うため、単一列用の標準矢印は使用しない。
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._sort_by_column)
        for column in range(1, 6):
            self.records.headerItem().setToolTip(column, tr('execution.sort_header_hint'))
        # Windows の表示倍率を考慮し、通常幅では全八列が表示領域に収まる比率にする。
        for column, width in enumerate((84, 68, 90, 190, 210, 155, 200, 215)):
            self.records.setColumnWidth(column, width)
        self.records.itemDoubleClicked.connect(self._record_double_clicked)
        self.records.itemSelectionChanged.connect(self._update_selection_controls)

        self.log = require(self, QPlainTextEdit, 'logEdit')
        self.stack = require(self, QStackedWidget, 'executionStack')
        self.progress = require(self, QProgressBar, 'progressBar')
        self.progress_count = require(self, QLabel, 'progressCountLabel')
        self.status_tab = require(self, QPushButton, 'statusTabButton')
        self.log_tab = require(self, QPushButton, 'logTabButton')
        self.status_tab.clicked.connect(lambda: self.select_tab(0))
        self.log_tab.clicked.connect(lambda: self.select_tab(1))

        self.selection_count = require(self, QLabel, 'selectionCountLabel')
        self.toggle_button = require(self, QPushButton, 'toggleExecutionButton')
        self.group_button = require(self, QPushButton, 'setGroupButton')
        self.order_button = require(self, QPushButton, 'setOrderButton')
        clear_button = require(self, QPushButton, 'clearResultsButton')
        self.execution_setting_buttons = (
            self.toggle_button, self.group_button, self.order_button, clear_button,
        )
        execution_menu = QMenu(self.toggle_button)
        execution_menu.addAction(tr('実行に設定'), lambda: self._set_selected_enabled(True))
        execution_menu.addAction(tr('スキップに設定'), lambda: self._set_selected_enabled(False))
        execution_menu.addAction(tr('選択を反転'), lambda: self._set_selected_enabled(None))
        self.toggle_button.setMenu(execution_menu)
        self.toggle_button.setToolTip(tr('選択したデータの今回実行をまとめて設定します'))
        self.group_button.setToolTip(tr('選択したデータの実行グループをまとめて設定します'))
        self.order_button.setToolTip(tr('グループ内の固定実行順を変更します'))
        clear_button.setToolTip(tr('すべての実行結果を未実行状態に戻します'))
        self.group_button.clicked.connect(self.set_group)
        self.order_button.clicked.connect(self.set_order)
        clear_button.clicked.connect(self.clear_results)
        require(self, QPushButton, 'clearLogButton').clicked.connect(self.log.clear)
        require(self, QPushButton, 'copyLogButton').clicked.connect(
            lambda: QApplication.clipboard().setText(self.log.toPlainText())
        )

        self.timer = QTimer(self)
        # ブラウザー実行中も画面操作を妨げない頻度で、状態だけを軽量に反映する。
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.select_tab(0)
        self.reload()
        self._update_selection_controls()

    def reload(self) -> None:
        records = self.db.list_data_records()
        records_by_id = {int(record['id']): record for record in records}
        # 受付順をグループごとに採番し、異なるグループの並列実行と混同させない。
        queued_group_counts: dict[str, int] = {}
        queued_positions: dict[int, int] = {}
        for record_id in self._queued_record_ids:
            queued_record = records_by_id.get(record_id)
            if queued_record is None:
                continue
            queued_group = str(queued_record['execution_group'])
            queued_group_counts[queued_group] = queued_group_counts.get(queued_group, 0) + 1
            queued_positions[record_id] = queued_group_counts[queued_group]
        # 表示ソート後も、実行データ欄の番号は DB 上のグループ内順序を示す。
        group_totals: dict[str, int] = {}
        group_indexes: dict[str, int] = {}
        group_positions: dict[int, tuple[int, int]] = {}
        for record in records:
            group = str(record['execution_group'])
            group_totals[group] = group_totals.get(group, 0) + 1
        for record in records:
            group = str(record['execution_group'])
            group_indexes[group] = group_indexes.get(group, 0) + 1
            group_positions[int(record['id'])] = (group_indexes[group], group_totals[group])
        records = self._records_in_display_order(records)
        with self._runtime_lock:
            runtime_details = dict(self._runtime_details)
        signature = tuple(
            (
                record['id'], record['execution_group'], record['enabled'], record['name'],
                record['summary'], record['execution_status'], runtime_details.get(record['id']),
                self._row_action_kind(record), self._row_action_enabled(record),
                queued_positions.get(int(record['id'])),
            )
            for record in records
        )

        # タイマー更新で選択位置やスクロール位置が揺れないよう、内容が変わった時だけ再構築する。
        if signature != self._table_signature:
            scroll = capture_scroll_position(self.records)
            selected_ids = {
                int(item.data(0, Qt.ItemDataRole.UserRole)) for item in self.records.selectedItems()
            }
            current = self.records.currentItem()
            current_id = int(current.data(0, Qt.ItemDataRole.UserRole)) if current else None
            self.records.setUpdatesEnabled(False)
            self.records.clear()
            items: list[QTreeWidgetItem] = []
            current_item: QTreeWidgetItem | None = None
            for record in records:
                group = str(record['execution_group'])
                group_index, group_total = group_positions[int(record['id'])]
                workflow, event = runtime_details.get(record['id'], ('-', '-'))
                status_key = str(record['execution_status'])
                queue_position = queued_positions.get(int(record['id']))
                status_text = (
                    f'{tr("実行予定")} {queue_position}'
                    if queue_position is not None else tr(STATUS_LABELS.get(status_key, status_key))
                )
                item = QTreeWidgetItem([
                    '',
                    group,
                    tr('実行') if record['enabled'] else tr('スキップ'),
                    f'({group_index}/{group_total}) {record["name"]}',
                    record['summary'],
                    status_text,
                    workflow,
                    event,
                ])
                item.setData(0, Qt.ItemDataRole.UserRole, record['id'])
                item.setData(0, ACTION_KIND_ROLE, self._row_action_kind(record))
                item.setData(0, ACTION_ENABLED_ROLE, self._row_action_enabled(record))
                item.setForeground(0, QColor('#0b78c5'))
                set_row_enabled_appearance(item, bool(record['enabled']))
                # 通常行にはツールチップを出さず、意味の補足が必要な状態セルだけに限定する。
                if queue_position is not None:
                    item.setToolTip(5, tr('実行予定の番号は、同じグループ内の実行待ち順です。'))
                if record['enabled']:
                    item.setForeground(5, QColor(STATUS_COLORS.get(status_key, '#314557')))
                items.append(item)
                if record['id'] == current_id:
                    current_item = item
            # 大量データでもモデル通知を一回にまとめ、初回表示のレイアウト負荷を抑える。
            self.records.addTopLevelItems(items)
            if current_item is not None:
                self.records.setCurrentItem(current_item)
            # QTreeWidgetItem の選択は、ビューへ追加した後でなければ反映されない。
            for item in items:
                if int(item.data(0, Qt.ItemDataRole.UserRole)) in selected_ids:
                    item.setSelected(True)
            self.records.setUpdatesEnabled(True)
            restore_scroll_position(self.records, scroll)
            self._table_signature = signature
            self._update_selection_controls()

        terminal = {'stopped', 'success', 'failed'}
        # 進捗は今回の実行対象だけを母数とし、スキップ行を完了数にも含めない。
        enabled_records = [record for record in records if record['enabled']]
        complete = sum(record['execution_status'] in terminal for record in enabled_records)
        enabled = len(enabled_records)
        self.target_summary.setText(f'{tr("実行対象 ")}{enabled} / {len(records)} {tr("件")}')
        self.progress.setMaximum(max(1, enabled))
        self.progress.setValue(complete)
        self.progress_count.setText(f'{complete} / {enabled}')

    def _row_action_kind(self, record: dict[str, Any]) -> str:
        record_id = int(record['id'])
        status = str(record['execution_status'])
        if status == 'stopping':
            return 'stopping'
        if status == 'error_waiting' and record_id in self._active_tasks:
            return 'finish'
        if record_id in self._active_tasks or record_id in self._queued_record_ids:
            return 'stop'
        return 'start'

    def _row_action_enabled(self, record: dict[str, Any]) -> bool:
        record_id = int(record['id'])
        kind = self._row_action_kind(record)
        if kind == 'stopping':
            return False
        if kind in {'stop', 'finish'}:
            return True
        if not record['enabled']:
            return False
        if self._execution_mode == 'global':
            return False
        return record_id not in self._batch_finished

    def _record_sort_key(self, record: dict[str, Any], column: int) -> Any:
        """画面表示文字ではなく、各列の業務値から安定したソートキーを作る。"""
        if column == 1:
            return natural_sort_key(record['execution_group'])
        if column == 2:
            return 0 if record['enabled'] else 1
        if column == 3:
            return natural_sort_key(record['name'])
        if column == 4:
            return natural_sort_key(record['summary'])
        if column == 5:
            return STATUS_SORT_ORDER.get(str(record['execution_status']), 99)
        return int(record['position'])

    def _sorted_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._sort_criteria:
            return records
        # 優先度の低い条件から安定ソートし、同値では DB 上の順序を維持する。
        ordered = sorted(records, key=lambda record: int(record['position']))
        for column, order in reversed(self._sort_criteria):
            ordered = sorted(
                ordered,
                key=lambda record, current=column: self._record_sort_key(record, current),
                reverse=order == Qt.SortOrder.DescendingOrder,
            )
        return ordered

    def _records_in_display_order(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """実行中は表示順を固定し、状態更新による行移動を防ぐ。"""
        if self._execution_mode is None or self._running_display_order is None:
            return self._sorted_records(records)
        positions = {record_id: index for index, record_id in enumerate(self._running_display_order)}
        return sorted(
            records,
            key=lambda record: (positions.get(int(record['id']), len(positions)), int(record['position'])),
        )

    def _sort_by_column(
        self, column: int,
        modifiers: Qt.KeyboardModifier | None=None,
    ) -> None:
        """通常クリックは単一条件、Shift クリックは複数条件として切り替える。"""
        if column < 1 or column > 5:
            return
        active_modifiers = QApplication.keyboardModifiers() if modifiers is None else modifiers
        append = bool(active_modifiers & Qt.KeyboardModifier.ShiftModifier)
        existing = next(
            (index for index, (current, _order) in enumerate(self._sort_criteria) if current == column),
            None,
        )
        if not append:
            if len(self._sort_criteria) == 1 and existing == 0:
                current_order = self._sort_criteria[0][1]
                self._sort_criteria = (
                    [(column, Qt.SortOrder.DescendingOrder)]
                    if current_order == Qt.SortOrder.AscendingOrder else []
                )
            else:
                self._sort_criteria = [(column, Qt.SortOrder.AscendingOrder)]
        elif existing is None:
            self._sort_criteria.append((column, Qt.SortOrder.AscendingOrder))
        else:
            current_order = self._sort_criteria[existing][1]
            if current_order == Qt.SortOrder.AscendingOrder:
                self._sort_criteria[existing] = (column, Qt.SortOrder.DescendingOrder)
            else:
                del self._sort_criteria[existing]
        self.records.header().viewport().update()
        ordered = self._sorted_records(self.db.list_data_records())
        if self._execution_mode is not None:
            # 実行中の自動更新では、利用者が選んだこの時点の順序を維持する。
            self._running_display_order = [int(record['id']) for record in ordered]
        self._table_signature = None
        self.reload()

    def selected_record(self) -> dict[str, Any] | None:
        item = self.records.currentItem()
        if item is None:
            return None
        record_id = item.data(0, Qt.ItemDataRole.UserRole)
        return next((record for record in self.db.list_data_records() if record['id'] == record_id), None)

    def _selected_record_ids(self) -> list[int]:
        """画面順を保ったまま、複数選択中のデータ ID を取得する。"""
        return [
            int(self.records.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole))
            for index in range(self.records.topLevelItemCount())
            if self.records.topLevelItem(index).isSelected()
        ]

    def _update_selection_controls(self) -> None:
        count = len(self.records.selectedItems())
        self.selection_count.setText(
            f'{tr("選択中：")}{count}{tr("件")}' if count else tr('選択なし')
        )
        editable = self._execution_mode is None
        self.toggle_button.setEnabled(editable and count > 0)
        self.group_button.setEnabled(editable and count > 0)
        self.order_button.setEnabled(editable and self.records.topLevelItemCount() > 0)

    def _record_double_clicked(self, item: QTreeWidgetItem | None, column: int) -> None:
        # 操作可能な先頭二列だけをダブルクリックに対応させる。
        if self._execution_mode is not None:
            return
        if item is not None:
            self.records.setCurrentItem(item)
            item.setSelected(True)
        if column == 1:
            self.set_group()
        elif column == 2:
            self.toggle_record()

    def toggle_record(self) -> None:
        if self._execution_mode is not None:
            return
        record = self.selected_record()
        if record:
            self._set_selected_enabled(not record['enabled'], [int(record['id'])])

    def _set_selected_enabled(
        self, enabled: bool | None, record_ids: list[int] | None=None,
    ) -> None:
        if self._execution_mode is not None:
            return
        selected_ids = self._selected_record_ids() if record_ids is None else record_ids
        if not selected_ids:
            return
        self.db.set_data_records_enabled(selected_ids, enabled)
        self._table_signature = None
        self.reload()

    def set_group(self) -> None:
        if self._execution_mode is not None:
            return
        selected_ids = self._selected_record_ids()
        if not selected_ids:
            return
        selected_id_set = set(selected_ids)
        selected = [
            record for record in self.db.list_data_records()
            if int(record['id']) in selected_id_set
        ]
        groups = {str(record['execution_group']) for record in selected}
        initial_group = next(iter(groups)) if len(groups) == 1 else ''
        group, ok = QInputDialog.getText(
            self, tr('グループ設定'),
            f'{tr("選択した")}{len(selected_ids)}{tr("件のグループを設定します")}',
            text=initial_group,
        )
        if ok and group.strip():
            self.db.set_data_records_group(selected_ids, group)
            self._table_signature = None
            self.reload()

    def set_order(self) -> None:
        if self._execution_mode is not None:
            return
        selected_ids = set(self._selected_record_ids())
        selected_groups = {
            str(record['execution_group']) for record in self.db.list_data_records()
            if int(record['id']) in selected_ids
        }
        initial_group = min(selected_groups, key=natural_sort_key) if selected_groups else None
        dialog = ExecutionOrderDialog(self, self.db, initial_group)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._table_signature = None
            self.reload()

    def clear_results(self) -> None:
        if self._execution_mode is not None:
            return
        self.db.clear_data_record_statuses()
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
        # 旧版と同じ日次ファイル形式を保ち、画面表示とファイル出力で翻訳結果を共用する。
        translated = tr(str(message))
        self.file_log.append(translated)
        with self._log_lock:
            self._log_lines.append(translated)

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
        if self._execution_mode is None:
            return
        task_finished = False
        for record_id, future in list(self._active_tasks.items()):
            if not future.done():
                continue
            task_finished = True
            try:
                future.result()
            except Exception as error:
                self.append_log(f'ERROR: {error}')
            self._active_tasks.pop(record_id, None)
            self._stop_requests.pop(record_id, None)
            self._batch_finished.add(record_id)
        # 待機列の再判定は、実行枠が解放された時だけで十分である。
        if task_finished:
            self._dispatch_waiting_records()
        if not self._active_tasks and not self._queued_record_ids:
            self._finish_execution_batch()
            return
        # 非表示ページの表更新は省略する。戻った時は MainWindow 側から reload される。
        # reload 自体も署名が変わった行だけを再構築し、クリック中の部品を保持する。
        if self.isVisible():
            self.reload()

    def start(self) -> None:
        if self._execution_mode is not None:
            return
        jobs = self._load_execution_jobs()
        if jobs is None:
            return
        records = self.db.list_data_records(enabled_only=True)
        if not records:
            show_information(self, '実行管理', '有効な実行データがありません。')
            return
        self.db.prepare_data_record_statuses()
        self._begin_execution_batch('global', jobs)
        self._queued_record_ids = [int(record['id']) for record in records]
        self._dispatch_waiting_records()

    def _load_execution_jobs(self) -> list[dict[str, Any]] | None:
        # 実行操作が要求されるまでブラウザー実行系モジュールを読み込まない。
        from core.executor import find_variables
        jobs = []
        for workflow in (dict(row) for row in self.db.list_workflows() if row['enabled']):
            events = [dict(row) for row in self.db.list_events(workflow['id'])]
            if events:
                jobs.append({
                    'id': workflow['id'], 'position': workflow['position'], 'name': workflow['name'],
                    'events': events, 'guard': decode_guard(workflow['guard_json']),
                    'guards': self.db.get_workflow_guards(int(workflow['id'])),
                })
        if not jobs:
            show_information(self, '実行管理', '実行可能な業務フローがありません。')
            return None
        variables = sorted({name for job in jobs for name in find_variables(job['events'])})
        if variables:
            show_warning(self, '実行管理', '外部変数が必要です: ' + ', '.join(variables))
            return None
        return jobs

    def _begin_execution_batch(self, mode: str, jobs: list[dict[str, Any]]) -> None:
        self._execution_mode = mode
        self._execution_jobs = jobs
        self._batch_finished.clear()
        with self._runtime_lock:
            self._runtime_details.clear()
        self.log.clear()
        self.select_tab(0)
        self.status.setText(tr('実行中'))
        self.run_button.setEnabled(False)
        self.session_spin.setEnabled(False)
        for button in self.execution_setting_buttons:
            button.setEnabled(False)
        self._running_display_order = [
            int(self.records.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole))
            for index in range(self.records.topLevelItemCount())
        ]
        self._table_signature = None

    def _row_action(self, record_id: int) -> None:
        if record_id in self._queued_record_ids:
            self._queued_record_ids.remove(record_id)
            self.db.set_data_record_status(record_id, 'stopped')
            self._batch_finished.add(record_id)
        elif record_id in self._active_tasks:
            self.db.set_data_record_status(record_id, 'stopping')
            self._stop_requests[record_id].set()
        else:
            record = next((row for row in self.db.list_data_records() if row['id'] == record_id), None)
            if record is None or not record['enabled'] or self._execution_mode == 'global':
                return
            if self._execution_mode is None:
                jobs = self._load_execution_jobs()
                if jobs is None:
                    return
                self._begin_execution_batch('manual', jobs)
            self.db.set_data_record_status(record_id, 'waiting')
            self._queued_record_ids.append(record_id)
            self._dispatch_waiting_records()
        self._table_signature = None
        self.reload()

    def _dispatch_waiting_records(self) -> None:
        if self._execution_mode is None:
            return
        records = {int(row['id']): row for row in self.db.list_data_records()}
        active_groups = {
            str(records[record_id]['execution_group'])
            for record_id in self._active_tasks if record_id in records
        }
        limit = max(1, self.db.get_pcl_session_limit())
        while len(self._active_tasks) < limit:
            queue_index = next((
                index for index, record_id in enumerate(self._queued_record_ids)
                if record_id in records
                and str(records[record_id]['execution_group']) not in active_groups
            ), None)
            if queue_index is None:
                break
            record_id = self._queued_record_ids.pop(queue_index)
            record = records[record_id]
            group = str(record['execution_group'])
            stop_request = Event()
            self._stop_requests[record_id] = stop_request
            self.db.set_data_record_status(record_id, 'running')
            self._active_tasks[record_id] = self.pool.submit(
                self._run_record, record, stop_request,
            )
            active_groups.add(group)

    def _run_record(self, record: dict[str, Any], stop_request: Event) -> None:
        from core.executor import ExecutionStopped, WorkflowExecutor
        enabled_records = self.db.list_data_records(enabled_only=True)
        record_positions = {row['id']: index for index, row in enumerate(enabled_records, 1)}
        groups = list(dict.fromkeys(str(row['execution_group']) for row in enabled_records))
        group = str(record['execution_group'])
        session = groups.index(group) + 1 if group in groups else 1
        state_path = profile_path(self.project_dir, self.db.get_auth_profile())
        steps = [{
            'phase': 'pcl', 'group': record['execution_group'], 'record': record,
            'session': session,
            'pcl_index': record_positions.get(record['id'], 1), 'pcl_total': len(enabled_records),
            'id': job['id'], 'position': job['position'], 'name': job['name'],
            'events': job['events'], 'guard': job['guard'], 'guards': job.get('guards'),
            'is_last_for_record': job is self._execution_jobs[-1],
        } for job in self._execution_jobs]
        active_run: list[int | None] = [None]

        def step_start(step):
            self._set_runtime_detail(record['id'], self._numbered_name(step), '-')
            context = WorkflowExecutor._step_log_context(step)
            self.append_log(
                f'{context} ▶ {record["name"]} / F{step["position"]} {step["name"]}'
            )
            active_run[0] = self.db.create_run(step['id'])
            return active_run[0]

        def event_start(step, event):
            record = step['record']
            self._set_runtime_detail(
                record['id'], self._numbered_name(step), self._numbered_name(event),
            )

        def step_success(step, run_id):
            self.db.finish_run(run_id, 'success', '')
            active_run[0] = None
            record = step['record']
            self.db.update_data_record(record['id'], record['name'], record['data'])
            if step['is_last_for_record']:
                self.db.set_data_record_status(record['id'], 'success')

        def step_failure(step, run_id, error):
            self.db.finish_run(run_id, 'failed', str(error))
            active_run[0] = None
            # 可視ブラウザーでは失敗画面を確認できるため、後処理完了までは中間状態にする。
            self.db.set_data_record_status(step['record']['id'], 'error_waiting')
            self.append_log(f'{WorkflowExecutor._step_log_prefix(step)}ERROR: {error}')

        try:
            WorkflowExecutor(
                self.project_dir, self.append_log, self.db.get_action_stable_ms(),
                close_source_tabs=True,
            ).run_batch(
                steps, {}, on_step_start=step_start, on_step_success=step_success,
                on_step_failure=step_failure, on_event_start=event_start,
                stop_requested=stop_request.is_set,
                browser_visible=self.db.get_browser_visible(),
                session_name=f'group_{group}', storage_state_path=state_path,
            )
        except ExecutionStopped:
            if active_run[0] is not None:
                self.db.finish_run(active_run[0], 'stopped', 'execution.stopped')
            self.db.set_data_record_status(record['id'], 'stopped')
            self.append_log(f'{record["name"]}: {tr("中止")}')
        except Exception:
            self.db.set_data_record_status(record['id'], 'failed')
            raise

    def _finish_execution_batch(self) -> None:
        self._execution_mode = None
        self._execution_jobs = []
        self._batch_finished.clear()
        self._running_display_order = None
        self.status.setText(tr('実行完了'))
        self.run_button.setEnabled(True)
        self.session_spin.setEnabled(True)
        for button in self.execution_setting_buttons:
            button.setEnabled(True)
        self._table_signature = None
        self.reload()

    def is_executing(self) -> bool:
        """終了確認などから実行バッチの占有状態を共通に判定する。"""
        return self._execution_mode is not None

    def shutdown(self) -> None:
        # 終了時もイベント途中で強制破棄せず、各ワーカーへ安全停止を通知する。
        self._queued_record_ids.clear()
        for stop_request in self._stop_requests.values():
            stop_request.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
