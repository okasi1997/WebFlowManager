from __future__ import annotations

from pathlib import Path
import copy
import json
import ctypes
import queue
import re
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPlainTextEdit, QPushButton, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QWidget, QWidgetAction,
)

from core.conditions import OPERATORS, decode_guard, summarize_guard
from core.daily_log import DailyLogWriter
from core.database import Database
from core.data_templates import normalize_template_schema, schema_templates
from core.settings import SELECT_FIRST_VALUE, SUPPORTED_ACTIONS, SUPPORTED_SELECTOR_TYPES
from browser.element_picker import DebugBrowserSession
from browser.locators import multi_path_steps
from i18n import tr
from .auth import profile_path
from ..ui_loader import (
    confirm_action, confirm_deletion, load_ui_into, localize_dialog_buttons, require, set_button_icon,
    optically_align_form_labels,
    set_tree_toggle_icon, show_file_exported, show_file_imported,
    show_error, show_information, show_warning,
)
from ..table_view import (
    HierarchicalReorderTreeWidget, bind_delete_key, bind_structured_copy_paste, bulk_view_update, capture_scroll_position,
    capture_tree_display_state, configure_row_move_tooltips, configure_table_view,
    restore_scroll_position, restore_tree_display_state, selected_outer_items,
    set_column_layout, set_row_enabled_appearance, set_tree_expanded, update_preserving_scroll,
    unique_copy_name,
)

ACTION_LABELS = {
    'goto': 'ページへ移動', 'click': 'クリック', 'fill': '入力', 'select': '選択',
    'wait': '待機', 'press': 'キー入力', 'get_text': '文字取得', 'screenshot': 'スクリーンショット',
    'pause': '一時停止', 'upload_file': 'ファイル送信', 'group_start': 'グループ',
}

PARAMETER_MENU_MAX_WIDTH = 560
PARAMETER_MENU_MAX_HEIGHT = 420
FAILURE_ACTION_LABELS = {
    'stop': '実行を停止', 'continue': '次のイベントへ進む',
    'refresh': 'ページを再読み込み', 'goto': '指定 URL へ移動',
}
WAIT_CONDITION_LABELS = {'visible': '表示', 'hidden': '非表示', 'operable': '操作可能'}
CLICK_SUCCESS_LABELS = {
    'none': '確認しない', 'visible': '要素が表示', 'hidden': '要素が非表示',
    'operable': '要素が操作可能', 'url_contains': 'URL に文字列を含む',
}
# locator を実行時に使用する操作だけで、画面からの要素選択を許可する。
ELEMENT_SELECTOR_ACTIONS = frozenset({
    'click', 'fill', 'select', 'wait', 'press', 'get_text', 'screenshot', 'upload_file',
})
SUCCESS_CONFIRM_ACTIONS = frozenset({'click', 'goto', 'select', 'press'})
GROUP_ACTION_WIDTH = 120
SELECTOR_TYPE_LABELS = {'path': '多段パス'}
INLINE_DATA_REFERENCE_PATTERN = re.compile(r'\$\{data:([^{}]+)\}')


def _populate_selector_types(combo: QComboBox, current: Any) -> None:
    """保存キーを userData に保ち、多段パスだけ利用者向け名称で表示する。"""
    for selector_type in SUPPORTED_SELECTOR_TYPES:
        combo.addItem(tr(SELECTOR_TYPE_LABELS.get(selector_type, selector_type)), selector_type)
    index = combo.findData(str(current))
    combo.setCurrentIndex(max(0, index))


def _selector_type(combo: QComboBox) -> str:
    """旧来の表示値にも対応しながら selector type の保存キーを返す。"""
    return str(combo.currentData() or combo.currentText())


def _migrate_inline_path_parameters(data: dict[str, Any]) -> dict[str, Any]:
    """旧 ${data:path} を短い $n 参照へ重複なく移行する。"""
    parameters = data.setdefault('parameters', {})
    if not isinstance(parameters, dict):
        parameters = {}
        data['parameters'] = parameters
    path_numbers = {
        str(parameter.get('value', '')): str(number)
        for number, parameter in parameters.items()
        if isinstance(parameter, dict) and parameter.get('source') == 'data'
    }
    used = {int(number) for number in parameters if str(number).isdigit()}

    def replace(match: re.Match[str]) -> str:
        path = match.group(1).strip()
        if path in path_numbers:
            return f'${path_numbers[path]}'
        number = next((candidate for candidate in range(1, 100) if candidate not in used), None)
        if number is None:
            return match.group(0)
        used.add(number)
        path_numbers[path] = str(number)
        parameters[str(number)] = {
            'source': 'data', 'value': path,
            'empty_action': 'error', 'max_length': 120,
        }
        return f'${number}'

    for step in data.get('steps', []):
        if isinstance(step, dict):
            step['value'] = INLINE_DATA_REFERENCE_PATTERN.sub(replace, str(step.get('value', '')))
    return data


def _inline_host(*items: tuple[QWidget, int] | QWidget, spacing: int = 8) -> QWidget:
    """複数の入力部品を同じ行に配置する共通コンテナーを生成する。"""
    host = QWidget()
    host.setProperty('formHost', True)
    layout = QHBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for item in items:
        widget, stretch = item if isinstance(item, tuple) else (item, 0)
        layout.addWidget(widget, stretch)
    return host


def _iframe_path_text(value: Any) -> str:
    """iframe 未指定を空文字へ統一し、空配列の表示を防ぐ。"""
    if value in (None, '', '[]') or value == []:
        return ''
    return str(value).strip()


def _localized_guard_summary(guard: dict[str, Any] | None) -> str:
    """実行条件の演算子を現在言語へ変換して要約する。"""
    labels = {key: tr(label) for key, label in GUARD_OPERATOR_LABELS.items()}
    return summarize_guard(guard, labels)


def _aligned_form_host(
        field: QWidget, trailing: QWidget | None = None,
        trailing_width: int = GROUP_ACTION_WIDTH,
) -> QWidget:
    """主入力列と右側操作列の幅を固定し、フォームの境界を揃える。"""
    if trailing is None:
        # 操作がない行も幅だけを確保し、空き領域には背景色を付けない。
        trailing = QWidget()
        trailing.setProperty('formHost', True)
    trailing.setFixedWidth(trailing_width)
    return _inline_host((field, 1), trailing)


class MultiPathParameterDialog(QDialog):
    """多段パス内の $n パラメーターを一元管理する。"""

    SOURCE_LABELS = {'data': 'データ構造', 'variable': '実行変数', 'fixed': '固定値'}
    EMPTY_LABELS = {'error': 'エラー', 'empty': '空文字'}

    def __init__(self, parent: QWidget, parameters: dict[str, Any], schema: dict[str, Any]) -> None:
        super().__init__(parent)
        load_ui_into(self, 'multi_path_parameters.ui')
        self.parameters = copy.deepcopy(parameters) if isinstance(parameters, dict) else {}
        self.schema = schema
        self.table = require(self, QTableWidget, 'parameterTable')
        self.number = require(self, QLineEdit, 'numberEdit')
        self.source = require(self, QComboBox, 'sourceCombo')
        self.value = require(self, QLineEdit, 'valueEdit')
        self.empty = require(self, QComboBox, 'emptyCombo')
        self.maximum = require(self, QSpinBox, 'lengthSpin')
        self.data_button = require(self, QPushButton, 'dataButton')
        for key, label in self.SOURCE_LABELS.items():
            self.source.addItem(tr(label), key)
        for key, label in self.EMPTY_LABELS.items():
            self.empty.addItem(tr(label), key)
        parameter_header = self.table.horizontalHeader()
        parameter_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        parameter_header.setMinimumSectionSize(90)
        # 設定値は残り幅をすべて使い、短い先頭データでも表の右側を空けない。
        parameter_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        # Designer 読込直後は viewport が未確定のため、表示レイアウト確定後に再調整する。
        QTimer.singleShot(0, self._fit_parameter_columns_to_contents)
        self.table.itemSelectionChanged.connect(self._load_selected)
        self.source.currentIndexChanged.connect(self._source_changed)
        self.value.textEdited.connect(self._save_current)
        self.empty.currentIndexChanged.connect(self._save_current)
        self.maximum.valueChanged.connect(self._save_current)
        self.data_button.clicked.connect(self._choose_data)
        require(self, QPushButton, 'addButton').clicked.connect(self._add)
        require(self, QPushButton, 'deleteButton').clicked.connect(self._delete)
        set_button_icon(self.data_button, 'data-reference', 18)
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        self._refresh()

    def _fit_parameter_columns_to_contents(self) -> None:
        """各列を内容に合わせて広げ、上限を超えた分は横スクロールで表示する。"""
        header = self.table.horizontalHeader()
        # 最後の「設定値」列は Stretch に任せ、識別用の2列だけ内容幅に合わせる。
        for column in range(max(0, self.table.columnCount() - 1)):
            self.table.resizeColumnToContents(column)
            width = max(
                header.minimumSectionSize(),
                min(220, self.table.columnWidth(column)),
            )
            self.table.setColumnWidth(column, width)

    @staticmethod
    def _sort_key(number: str) -> int:
        return int(number) if number.isdigit() else 1000

    def _refresh(self, selected: str | None = None) -> None:
        selected = selected or self.number.text().lstrip('$')
        self.table.setRowCount(0)
        for number in sorted(self.parameters, key=self._sort_key):
            parameter = self.parameters[number]
            if not isinstance(parameter, dict):
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            source = str(parameter.get('source', 'fixed'))
            values = (f'${number}', tr(self.SOURCE_LABELS.get(source, source)), str(parameter.get('value', '')))
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, number)
                self.table.setItem(row, column, item)
            if number == selected:
                self.table.selectRow(row)
        if self.table.currentRow() < 0 and self.table.rowCount():
            self.table.selectRow(0)
        self._fit_parameter_columns_to_contents()
        self._load_selected()

    def _selected_number(self) -> str | None:
        item = self.table.item(self.table.currentRow(), 0) if self.table.currentRow() >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _load_selected(self) -> None:
        number = self._selected_number()
        enabled = number is not None
        require(self, QFrame, 'detailCard').setEnabled(enabled)
        if not enabled:
            self.number.clear()
            return
        parameter = self.parameters[number]
        self.number.setText(f'${number}')
        for combo, key in ((self.source, parameter.get('source', 'fixed')), (self.empty, parameter.get('empty_action', 'error'))):
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(str(key))))
            combo.blockSignals(False)
        self.value.blockSignals(True)
        self.value.setText(str(parameter.get('value', '')))
        self.value.blockSignals(False)
        self.maximum.blockSignals(True)
        self.maximum.setValue(int(parameter.get('max_length', 120)))
        self.maximum.blockSignals(False)
        self._source_changed()

    def _save_current(self, *_args) -> None:
        number = self._selected_number()
        if number is None:
            return
        self.parameters[number] = {
            'source': str(self.source.currentData()), 'value': self.value.text(),
            'empty_action': str(self.empty.currentData()), 'max_length': self.maximum.value(),
        }
        # 値変更時は対象行だけ更新し、全表再構築を避ける。
        row = self.table.currentRow()
        if row >= 0:
            self.table.item(row, 1).setText(tr(self.SOURCE_LABELS[str(self.source.currentData())]))
            self.table.item(row, 2).setText(self.value.text())

    def _source_changed(self, *_args) -> None:
        is_data = self.source.currentData() == 'data'
        self.data_button.setVisible(is_data)
        is_variable = self.source.currentData() == 'variable'
        self.value.setPlaceholderText(
            tr('変数名 または yyyyMMdd_HHmm') if is_variable else ''
        )
        self.value.setToolTip(
            tr('同名変数を優先し、存在しない場合は対応する日時書式を使用します')
            if is_variable else ''
        )
        self._save_current()

    def _choose_data(self) -> None:
        dialog = DataPathPickerDialog(self, self.schema, self.value.text())
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_path:
            self.value.setText(dialog.result_path)
            self._save_current()

    def _add(self) -> None:
        number = next((str(value) for value in range(1, 100) if str(value) not in self.parameters), None)
        if number is None:
            show_warning(self, 'パラメーター設定', 'パラメーターは99個まで設定できます。')
            return
        self.parameters[number] = {'source': 'data', 'value': '', 'empty_action': 'error', 'max_length': 120}
        self._refresh(number)

    def _delete(self) -> None:
        number = self._selected_number()
        if number is not None:
            del self.parameters[number]
            self._refresh()

    def _accept_if_valid(self) -> None:
        self._save_current()
        for number, parameter in self.parameters.items():
            if not str(parameter.get('value', '')).strip() and parameter.get('source') != 'fixed':
                show_warning(self, 'パラメーター設定', f'${number} の設定値を入力してください。')
                return
        self.accept()

    @property
    def selected_number(self) -> str | None:
        """保存時に選択中のパラメーター番号を返す。"""
        return self._selected_number()


class EventEditorDialog(QDialog):
    """保存対象のイベント項目を編集する独立ウィンドウ。"""

    def __init__(
        self, parent: QWidget, event: dict[str, Any] | None=None, *,
        group: bool=False, insert_at: int | None=None,
    ) -> None:
        # ブラウザーとの比較中にこの画面だけを前面へ出せるよう、Qt の親子関係は持たせない。
        # データベースやデバッグ機能への参照は、通常の Python 参照として別に保持する。
        super().__init__(None, Qt.WindowType.Window)
        self._service_host = parent
        self._closing = False
        self.setWindowTitle('イベントグループ編集' if group else ('イベント編集' if event else 'イベント追加'))
        event = event or {}
        self.event_id = int(event.get('id', 0) or 0)
        try:
            scroll_data = json.loads(str(event.get('scroll_json', ''))) if event.get('scroll_json') else {}
            self.scroll_data = scroll_data if isinstance(scroll_data, dict) else {}
        except json.JSONDecodeError:
            self.scroll_data = {}
        # 新規イベントでも「直前まで実行」できるよう、追加予定位置を保持する。
        self.insert_at = insert_at
        self._load_designer_form(event, group)

    def _parent_group_data_paths(self) -> list[str]:
        """編集中イベントを囲むデータグループのパスを外側から返す。"""
        workflow_id = self._service_host.current_workflow_id
        if workflow_id is None:
            return []
        rows = [dict(row) for row in self._service_host.db.list_events(workflow_id)]
        stack: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            is_target = (
                (self.event_id and int(row.get('id', 0)) == self.event_id)
                or (not self.event_id and self.insert_at is not None and index >= self.insert_at)
            )
            if is_target:
                return [
                    str(group.get('data_path', '')).strip()
                    for group in stack
                    if str(group.get('data_path', '')).strip()
                ]
            action = str(row.get('action', ''))
            if action in {'group_start', 'loop_start', 'retry_start'}:
                stack.append(row)
            elif action in {'group_end', 'loop_end', 'retry_end'} and stack:
                stack.pop()
        return []

    def _load_designer_form(self, event: dict[str, Any], group: bool) -> None:
        load_ui_into(self, 'event_editor.ui')
        self.setWindowTitle('イベントグループ編集' if group else ('イベント編集' if event else 'イベント追加'))
        bindings = {
            'name': (QLineEdit, 'nameEdit'), 'action': (QComboBox, 'actionCombo'),
            'selector_type': (QComboBox, 'selectorTypeCombo'), 'selector': (QLineEdit, 'selectorEdit'),
            'fallback_selector_type': (QComboBox, 'fallbackTypeCombo'),
            'fallback_selector': (QLineEdit, 'fallbackSelectorEdit'),
            'iframe_path': (QLineEdit, 'iframeEdit'), 'value': (QLineEdit, 'valueEdit'),
            'data_path': (QComboBox, 'dataPathCombo'), 'timeout': (QSpinBox, 'timeoutSpin'),
            'enabled': (QCheckBox, 'enabledCheck'), 'continue_on_error': (QCheckBox, 'continueCheck'),
            'failure_action': (QComboBox, 'failureActionCombo'),
            'failure_target': (QLineEdit, 'failureTargetEdit'),
            'retry_count': (QSpinBox, 'retryCountSpin'),
            'retry_interval': (QSpinBox, 'retryIntervalSpin'),
        }
        for attribute, (kind, name) in bindings.items():
            setattr(self, attribute, require(self, kind, name))
        actions = ['group_start'] if group else [a for a in SUPPORTED_ACTIONS if not a.endswith('_end') and a not in {'group_start', 'loop_start', 'retry_start'}]
        if not group:
            self.action.addItem('', '')
        for action in actions:
            self.action.addItem(ACTION_LABELS.get(action, action), action)
        initial_action = str(event.get('action', 'group_start' if group else ''))
        self.action.setCurrentIndex(max(0, self.action.findData(initial_action)))
        for combo, value in (
            (self.selector_type, event.get('selector_type', 'none')),
            (self.fallback_selector_type, event.get('fallback_selector_type', 'none')),
        ):
            _populate_selector_types(combo, value)
        stored_failure = str(event.get('failure_action', 'none'))
        failure_key = (
            'continue' if stored_failure == 'none' and event.get('continue_on_error', 0)
            else 'stop' if stored_failure == 'none' else stored_failure
        )
        for key, label in FAILURE_ACTION_LABELS.items():
            self.failure_action.addItem(label, key)
        self.failure_action.setCurrentIndex(max(0, self.failure_action.findData(failure_key)))
        for widget, value in (
            (self.name, event.get('name', '')), (self.selector, event.get('selector', '')),
            (self.fallback_selector, event.get('fallback_selector', '')),
            (self.iframe_path, event.get('iframe_path', '')), (self.value, event.get('value', '')),
            (self.failure_target, event.get('failure_target', '')),
        ):
            widget.setText(str(value))
        # 「先頭を選択」は保存用の内部値であり、入力欄には表示しない。
        self._select_first = (
            initial_action == 'select' and str(event.get('value', '')) == SELECT_FIRST_VALUE
        )
        if self._select_first:
            self.value.clear()
        self.timeout.setValue(int(event.get('timeout_ms', self._service_host.db.get_default_timeout_ms())))
        self.retry_count.setValue(int(event.get('retry_count', 0)))
        self.retry_interval.setValue(int(event.get('retry_interval_ms', 0)))
        self.enabled.setChecked(bool(event.get('enabled', 1)))
        # 画面構造は event_editor.ui で完成させ、Python では値と動作だけを設定する。
        self._finish_designer_event_form(event)
        return

    def _finish_designer_event_form(self, event: dict[str, Any]) -> None:
        """Designer で構築済みの画面へ、保存値と動作のみを割り当てる。"""
        self.continue_on_error.setChecked(bool(event.get('continue_on_error', 0)))
        self.guard_data = decode_guard(event.get('guard_json', event.get('guard', '')))
        self.left_tabs = require(self, QTabWidget, 'eventEditorTabs')
        self._tab_layouts_prepared = False
        optically_align_form_labels(
            require(self, QFormLayout, 'eventFormBasic'),
            require(self, QFormLayout, 'locatorForm'),
            require(self, QFormLayout, 'executionForm'),
        )
        # QTabBar の expanding は Designer から保存できないため、実行時に補完する。
        self.left_tabs.tabBar().setExpanding(False)
        self.selector_host = require(self, QWidget, 'selectorHost')
        self.multi_path_title_host = require(self, QWidget, 'multiPathTitleHost')
        self.multi_path_title = require(self, QLabel, 'multiPathTitle')
        self.multi_path_host = require(self, QWidget, 'multiPathHost')
        self.multi_path_form = require(self, QFormLayout, 'multiPathForm')
        self.fallback_type_host = require(self, QWidget, 'fallbackTypeHost')
        self.multi_path_inputs: list[QLineEdit] = []
        self._multi_path_data: dict[str, Any] = {}
        try:
            decoded_parameters = json.loads(str(event.get('selector_parameters_json', '') or '{}'))
            self._selector_parameters = decoded_parameters if isinstance(decoded_parameters, dict) else {}
        except (TypeError, ValueError):
            self._selector_parameters = {}
        self.fallback_selector_host = require(self, QWidget, 'fallbackSelectorHost')
        self.value_host = require(self, QWidget, 'valueHost')
        self.path_host = require(self, QWidget, 'pathHost')
        self.guard_host = require(self, QWidget, 'guardHost')
        self.failure_target_host = require(self, QWidget, 'failureTargetHost')

        # 選択肢は保存用キーを userData に保持するため、実行時に設定する。
        self.wait_condition = require(self, QComboBox, 'waitConditionCombo')
        for key, label in WAIT_CONDITION_LABELS.items():
            self.wait_condition.addItem(label, key)
        stored_wait = str(event.get('value', ''))
        self.wait_condition.setCurrentIndex(max(0, self.wait_condition.findData(
            stored_wait if stored_wait in WAIT_CONDITION_LABELS else 'visible'
        )))
        current_data_path = str(event.get('data_path', '')).strip()
        self.data_path.addItem('')
        self.data_path.addItems(_schema_condition_paths(self._data_schema()))
        if current_data_path and self.data_path.findText(current_data_path) < 0:
            self.data_path.addItem(current_data_path)
        self.data_path.setCurrentText(current_data_path)

        click_success: dict[str, Any] = {}
        raw_success = str(event.get('success_json', '') or '')
        # 旧 click データだけは value に成功確認が入っているため読み込み互換を保つ。
        if not raw_success and str(event.get('action', '')) == 'click':
            raw_success = str(event.get('value', '') or '')
        if raw_success:
            try:
                decoded = json.loads(raw_success)
                click_success = decoded if isinstance(decoded, dict) else {}
            except (TypeError, ValueError):
                pass
        if click_success and not event.get('success_json') and str(event.get('action', '')) == 'click':
            self.value.clear()
        self.click_success_condition = require(self, QComboBox, 'clickSuccessCombo')
        for key, label in CLICK_SUCCESS_LABELS.items():
            self.click_success_condition.addItem(label, key)
        self.click_success_condition.setCurrentIndex(max(0, self.click_success_condition.findData(
            str(click_success.get('condition', 'none'))
        )))
        self.click_success_selector_type = require(self, QComboBox, 'clickSuccessTypeCombo')
        _populate_selector_types(
            self.click_success_selector_type, click_success.get('selector_type', 'css')
        )
        self.click_success_target = require(self, QLineEdit, 'clickSuccessTargetEdit')
        self.click_success_target.setText(str(click_success.get('target', '')))
        self.click_success_iframe_path = require(self, QLineEdit, 'clickSuccessIframeEdit')
        self.click_success_iframe_path.setText(
            _iframe_path_text(click_success.get('iframe_path', ''))
        )

        self.guard_summary = require(self, QLineEdit, 'guardSummaryEdit')
        self.selector_reference_button = require(self, QPushButton, 'selectorReferenceButton')
        self.fallback_reference_button = require(self, QPushButton, 'fallbackReferenceButton')
        self.value_data_reference_button = require(self, QPushButton, 'valueReferenceButton')
        self.value_action_button = require(self, QPushButton, 'valueActionButton')
        self.selector_reference_button.clicked.connect(
            lambda: self._configure_event_parameter(self.selector)
        )
        self.fallback_reference_button.clicked.connect(
            lambda: self._configure_event_parameter(self.fallback_selector)
        )
        self.value_data_reference_button.clicked.connect(
            lambda: self._configure_event_parameter(self.value)
        )
        self.value_action_button.clicked.connect(self._value_action)
        # 利用者が選択値を直接入力した場合は、先頭選択状態を解除する。
        self.value.textEdited.connect(lambda _text: setattr(self, '_select_first', False))
        require(self, QPushButton, 'pathButton').clicked.connect(self.choose_data_path)
        require(self, QPushButton, 'guardButton').clicked.connect(self.edit_guard)
        require(self, QPushButton, 'failureReferenceButton').clicked.connect(
            lambda: self._configure_event_parameter(self.failure_target)
        )

        self.target_url = require(self, QLineEdit, 'targetUrlEdit')
        self.target_url.setText(self._service_host.db.get_start_url() or 'https://github.com/?locale=ja')
        self.debug_result = require(self, QPlainTextEdit, 'resultEdit')
        self.pick_button = require(self, QPushButton, 'pickButton')
        self.pick_button.clicked.connect(self.pick_element)
        require(self, QPushButton, 'closeDebugButton').clicked.connect(self.close_debug_browser)
        self.try_event_button = require(self, QPushButton, 'tryEventButton')
        self.try_event_button.clicked.connect(self.try_event)
        self.execute_until_button = require(self, QPushButton, 'executeUntilButton')
        self.execute_until_button.clicked.connect(self.execute_until_event)
        for name, icon_name in {
            'pickButton': 'page-pick', 'closeDebugButton': 'page-close',
            'tryEventButton': 'page-try', 'executeUntilButton': 'page-until',
        }.items():
            set_button_icon(require(self, QPushButton, name), icon_name, 16)

        self.action.currentIndexChanged.connect(self._update_action_fields)
        self.selector_type.currentIndexChanged.connect(self._update_action_fields)
        self.fallback_selector_type.currentIndexChanged.connect(self._update_action_fields)
        self.failure_action.currentIndexChanged.connect(self._update_action_fields)
        self.click_success_condition.currentIndexChanged.connect(self._update_action_fields)
        self.left_tabs.currentChanged.connect(self._update_picker_destination)
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        self._update_guard_summary()
        self._update_action_fields()
        self._update_picker_destination()
        self.iframe_path.setText(_iframe_path_text(self.iframe_path.text()))
        self._load_multi_path_fields()

    def _load_multi_path_fields(self) -> None:
        """保存済みの各段を、データ参照可能な入力行として再構築する。"""
        try:
            data = json.loads(self.selector.text())
            self._multi_path_data = _migrate_inline_path_parameters(data) if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            self._multi_path_data = {}
        path_parameters = self._multi_path_data.get('parameters', {})
        if isinstance(self._multi_path_data.get('steps'), list) and isinstance(path_parameters, dict):
            # 旧多段パスの定義もイベント共通パラメーターへ取り込む。
            self._selector_parameters = path_parameters | self._selector_parameters
            self._multi_path_data['parameters'] = self._selector_parameters
        if self._multi_path_data:
            self.selector.setText(json.dumps(
                self._multi_path_data, ensure_ascii=False, separators=(',', ':')
            ))
        while self.multi_path_form.count():
            item = self.multi_path_form.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.multi_path_inputs = []
        steps = multi_path_steps(self.selector.text())
        self.multi_path_title.setText(f'{tr("多段パス")}（{len(steps)}{tr("段")}）')
        for index, step in enumerate(steps, start=1):
            kind = {
                'target': '対象', 'source_row': '基準行', 'scope': '範囲',
            }.get(str(step.get('kind')), '範囲')
            method = str(step.get('match_method', 'text_contains'))
            attribute = str(step.get('match_attribute', ''))
            if method == 'attribute_equals' and attribute:
                match_label = f'{attribute} {tr("と等しい")}'
            elif method == 'tag':
                match_label = tr('タグ')
            else:
                match_label = tr('文字を含む')
            label = QLabel(f'{index}. {tr(kind)} · {match_label}')
            # 外側フォームと同じラベル幅にし、すべての入力欄の開始位置を揃える。
            label.setFixedWidth(108)
            value = QLineEdit(str(step.get('value', step.get('display', ''))))
            value.setToolTip(str(step.get('display', '')))
            button = QPushButton()
            button.setFixedWidth(42)
            button.setProperty('formAction', True)
            button.setProperty('dataReferenceButton', True)
            button.setToolTip(tr('パラメーターを挿入'))
            set_button_icon(button, 'data-reference', 18)
            button.clicked.connect(lambda _checked=False, field=value: self._configure_event_parameter(field))
            value.textChanged.connect(self._sync_multi_path_values)
            row = _inline_host((value, 1), button, spacing=8)
            self.multi_path_form.addRow(label, row)
            self.multi_path_inputs.append(value)

    def _sync_multi_path_values(self) -> None:
        """入力値を内部 JSON へ戻し、イベント保存と試行で同じ値を使用する。"""
        steps = self._multi_path_data.get('steps', [])
        if not isinstance(steps, list) or len(steps) != len(self.multi_path_inputs):
            return
        for step, field in zip(steps, self.multi_path_inputs):
            if isinstance(step, dict):
                step['value'] = field.text()
        self.selector.setText(json.dumps(
            self._multi_path_data, ensure_ascii=False, separators=(',', ':')
        ))

    def _configure_event_parameter(self, target: QLineEdit) -> None:
        """
        どのデータ連携ボタンからでもイベント共通設定を開き、
        保存時に選択中の $n を呼び出し元の入力欄へ挿入する。
        """
        if self._selector_parameters:
            menu, settings_action = self._build_parameter_summary_menu()
            selected = menu.exec(target.mapToGlobal(target.rect().bottomRight()))
            selected_number = getattr(menu, '_selected_parameter_number', None)
            if selected_number:
                target.insert(f'${selected_number}')
                target.setFocus()
                return
            if selected is None:
                return
            if selected is not settings_action:
                target.insert(f'${selected.data()}')
                target.setFocus()
                return
        dialog = MultiPathParameterDialog(
            self, self._selector_parameters, self._data_schema(),
        )
        dialog.setWindowTitle(tr('イベントパラメーター設定'))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._selector_parameters = dialog.parameters
        if isinstance(self._multi_path_data.get('steps'), list):
            self._multi_path_data['parameters'] = self._selector_parameters
            self._sync_multi_path_values()
        if dialog.selected_number:
            target.insert(f'${dialog.selected_number}')
            target.setFocus()

    def _build_parameter_summary_menu(self) -> tuple[QMenu, Any]:
        """番号と取得元を残し、長い値だけを省略したパラメーター概要を作る。"""
        menu = QMenu(self)
        menu.setMaximumWidth(PARAMETER_MENU_MAX_WIDTH)
        parameter_list = QListWidget(menu)
        parameter_list.setObjectName('parameterSummaryList')
        parameter_list.setFixedWidth(PARAMETER_MENU_MAX_WIDTH - 24)
        parameter_list.setMaximumHeight(PARAMETER_MENU_MAX_HEIGHT - 58)
        parameter_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        parameter_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        parameter_list.setUniformItemSizes(True)
        metrics = QFontMetrics(parameter_list.font())
        # リストの左右余白と縦スクロールバー領域を除いた幅を値表示に使用する。
        reserved_width = 42
        for number in sorted(
                self._selector_parameters, key=MultiPathParameterDialog._sort_key,
        ):
            parameter = self._selector_parameters[number]
            if not isinstance(parameter, dict):
                continue
            source = MultiPathParameterDialog.SOURCE_LABELS.get(
                str(parameter.get('source', 'fixed')), str(parameter.get('source', '')),
            )
            prefix = f'${number}  {tr(source)}  |  '
            value = str(parameter.get('value', ''))
            available = max(
                40, parameter_list.width()
                - metrics.horizontalAdvance(prefix) - reserved_width,
            )
            shown_value = metrics.elidedText(
                value, Qt.TextElideMode.ElideMiddle, available,
            )
            item = QListWidgetItem(f'{prefix}{shown_value}')
            item.setData(Qt.ItemDataRole.UserRole, str(number))
            item.setToolTip(f'{prefix}{value}')
            parameter_list.addItem(item)
        row_height = max(34, parameter_list.sizeHintForRow(0))
        parameter_list.setFixedHeight(min(
            PARAMETER_MENU_MAX_HEIGHT - 58,
            max(row_height, row_height * parameter_list.count() + 4),
        ))

        def select_parameter(item: QListWidgetItem) -> None:
            menu._selected_parameter_number = item.data(Qt.ItemDataRole.UserRole)
            menu.close()

        parameter_list.itemClicked.connect(select_parameter)
        parameter_list.itemActivated.connect(select_parameter)
        list_action = QWidgetAction(menu)
        list_action.setDefaultWidget(parameter_list)
        menu.addAction(list_action)
        menu.addSeparator()
        settings_action = menu.addAction(tr('イベントパラメーター設定'))
        # テストと再利用時に、スクロール対象へ安全にアクセスできるよう保持する。
        menu._parameter_list = parameter_list
        menu._selected_parameter_number = None
        return menu, settings_action

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._tab_layouts_prepared:
            return
        self._tab_layouts_prepared = True
        current = self.left_tabs.currentIndex()
        signals_blocked = self.left_tabs.blockSignals(True)
        updates_enabled = self.updatesEnabled()
        self.setUpdatesEnabled(False)
        try:
            # 初回切り替え時の再配置を防ぐため、最終ウィンドウ寸法で全タブを先に配置する。
            for index in range(self.left_tabs.count()):
                self.left_tabs.setCurrentIndex(index)
                page = self.left_tabs.widget(index)
                if page.layout() is not None:
                    page.layout().activate()
            self.left_tabs.setCurrentIndex(current)
        finally:
            self.left_tabs.blockSignals(signals_blocked)
            self.setUpdatesEnabled(updates_enabled)
            if updates_enabled:
                self.update()

    def _accept_if_valid(self) -> None:
        if not self.name.text().strip():
            show_warning(self, tr('error.create_title'), 'イベント名を入力してください')
            return
        try:
            decode_guard(self.guard_data)
        except (ValueError, json.JSONDecodeError) as error:
            show_warning(self, '実行条件', str(error))
            return
        action = self.action.currentData()
        if action in ELEMENT_SELECTOR_ACTIONS:
            if _selector_type(self.selector_type) == 'path':
                self._sync_multi_path_values()
                parameters = self._multi_path_data.get('parameters', {})
                values = [
                    str(step.get('value', ''))
                    for step in self._multi_path_data.get('steps', []) if isinstance(step, dict)
                ] + [self.value.text(), self.failure_target.text()]
            else:
                parameters = self._selector_parameters
                values = [
                    self.selector.text(), self.fallback_selector.text(),
                    self.value.text(), self.failure_target.text(),
                ]
            references = {
                match.group(1) for value in values
                for match in re.finditer(r'(?<!\$)\$(\d{1,2})(?!\d)', value)
            }
            undefined = sorted(
                (number for number in references if number not in parameters),
                key=MultiPathParameterDialog._sort_key,
            )
            if undefined:
                names = ', '.join(f'${number}' for number in undefined)
                show_warning(self, 'パラメーター設定', f'未定義のパラメーターがあります: {names}')
                return
        if (
            action in SUCCESS_CONFIRM_ACTIONS
            and self.click_success_condition.currentData() != 'none'
            and not self.click_success_target.text().strip()
        ):
            show_warning(self, '入力エラー', '成功条件の内容を入力してください。')
            return
        if action == 'upload_file' and not (self.value.text().strip() or self.data_path.currentText().strip()):
            show_warning(self, '入力エラー', '送信するファイルまたはデータリンクを指定してください。')
            return
        if action == 'get_text' and not (self.value.text().strip() or self.data_path.currentText().strip()):
            show_warning(self, '入力エラー', '取得結果の保存先を指定してください。')
            return
        if self.failure_action.currentData() == 'goto' and not self.failure_target.text().strip().startswith(('http://', 'https://')):
            show_warning(self, '入力エラー', '移動先 URL は http:// または https:// から入力してください。')
            return
        self.accept()

    def _update_guard_summary(self) -> None:
        self.guard_summary.setText(_localized_guard_summary(self.guard_data) or tr('常に実行'))

    def _data_schema(self) -> dict[str, Any]:
        """親画面が保持するデータ構造を安全に取得する。"""
        return (
            self._service_host.db.get_data_schema()
            if hasattr(self._service_host, 'db') else {'type': 'object', 'children': []}
        )

    def _select_data_path(self, current: str = '') -> str | None:
        """構造選択ダイアログの起動と選択結果の取得を共通化する。"""
        dialog = DataPathPickerDialog(self, self._data_schema(), current)
        return dialog.result_path if dialog.exec() == QDialog.DialogCode.Accepted else None

    def edit_guard(self) -> None:
        dialog = GuardConditionEditorDialog(self, self.guard_data, self._data_schema())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.guard_data = dialog.result_data()
            self._update_guard_summary()

    def choose_data_path(self) -> None:
        """データ項目は文字列結合せず、従来通り単一項目として選択する。"""
        path = self._select_data_path(self.data_path.currentText())
        if path:
            self.data_path.setCurrentText(path)

    def _update_action_fields(self) -> None:
        """カーソル位置または選択範囲へデータ参照式を挿入する。"""
        action = self.action.currentData()
        selector_enabled = action in ELEMENT_SELECTOR_ACTIONS
        path_enabled = selector_enabled and _selector_type(self.selector_type) == 'path'
        require(self, QFrame, 'locatorCard').setVisible(selector_enabled)
        # 項目数が少ない操作でも各入力欄は常に上詰めで表示する。
        require(self, QFrame, 'eventCard').setMinimumHeight(0)
        locator_form = require(self, QFormLayout, 'locatorForm')
        execution_form = require(self, QFormLayout, 'executionForm')

        def show_row(form: QFormLayout, field: QWidget, visible: bool) -> None:
            field.setVisible(visible)
            label = form.labelForField(field)
            if label is not None:
                label.setVisible(visible)

        show_row(locator_form, self.selector_type, selector_enabled)
        show_row(locator_form, self.selector_host, selector_enabled and not path_enabled)
        self.multi_path_title_host.setVisible(path_enabled)
        show_row(locator_form, self.multi_path_host, path_enabled)
        show_row(locator_form, self.fallback_type_host, selector_enabled and not path_enabled)
        show_row(locator_form, self.fallback_selector_host, selector_enabled and not path_enabled)
        show_row(locator_form, self.iframe_path, selector_enabled)
        self.selector.setEnabled(
            selector_enabled and _selector_type(self.selector_type) not in {'none', 'path'}
        )
        self.selector_reference_button.setEnabled(self.selector.isEnabled())
        self.fallback_selector.setEnabled(
            selector_enabled and _selector_type(self.fallback_selector_type) != 'none'
        )
        self.fallback_reference_button.setEnabled(self.fallback_selector.isEnabled())
        value_enabled = action in {'goto', 'fill', 'select', 'wait', 'press', 'get_text', 'screenshot', 'pause', 'upload_file'}
        self.value_host.setVisible(value_enabled)
        require(self, QLabel, 'valueLabel').setVisible(value_enabled)
        self.wait_condition.setVisible(action == 'wait')
        self.value.setVisible(value_enabled and action != 'wait')
        # 「選択」の先頭項目指定は互換用データとして保持するが、意味の異なる
        # データ参照アイコンでは判別しにくいため画面には表示しない。
        self.value_action_button.setVisible(action == 'upload_file')
        self.value_data_reference_button.setVisible(action in {'goto', 'fill', 'press', 'screenshot'})
        # 右側ボタンを持たない操作も、ほかの入力欄と同じ右端位置に揃える。
        # 右端ボタンを表示しない操作でも42pxの占有幅を残し、入力欄の右端を
        # ほかのコンボボックスや入力欄と揃える。
        require(self, QWidget, 'valueTrailing').setVisible(
            action in {'select', 'wait', 'get_text', 'pause'}
        )
        # 小型アイコンの機能は、操作ごとのツールチップで明確に区別する。
        self.value_action_button.setToolTip(tr('ファイルを選択'))
        if action == 'select':
            self.value_action_button.setProperty('selected', self._select_first)
        data_path_enabled = action in {'fill', 'select', 'get_text', 'upload_file'}
        self.path_host.setVisible(data_path_enabled)
        require(self, QLabel, 'dataPathLabel').setVisible(data_path_enabled)
        failure_target_visible = self.failure_action.currentData() == 'goto'
        show_row(execution_form, self.failure_target_host, failure_target_visible)
        click_success_visible = action in SUCCESS_CONFIRM_ACTIONS
        click_success_condition = str(self.click_success_condition.currentData())
        require(self, QLabel, 'successConfirmationTitle').setVisible(click_success_visible)
        show_row(execution_form, self.click_success_condition, click_success_visible)
        show_row(
            execution_form, self.click_success_selector_type,
            click_success_visible and click_success_condition in {'visible', 'hidden', 'operable'},
        )
        show_row(
            execution_form, self.click_success_target,
            click_success_visible and click_success_condition != 'none',
        )
        show_row(
            execution_form, self.click_success_iframe_path,
            click_success_visible and click_success_condition in {'visible', 'hidden', 'operable'},
        )
        # 表示行を切り替えた直後でも、未表示の編集画面で座標と高さを確定させる。
        locator_form.activate()
        execution_form.activate()
        self._update_picker_destination()

    def _update_picker_destination(self, _index: int=-1) -> None:
        if not hasattr(self, 'pick_button'):
            return
        success_mode = (
            self.action.currentData() in SUCCESS_CONFIRM_ACTIONS
            and self.left_tabs.currentIndex() == 1
            and self.click_success_condition.currentData() in {'visible', 'hidden', 'operable'}
        )
        self.pick_button.setProperty('pickDestination', 'click_success' if success_mode else 'event')
        self.pick_button.setText(tr('成功確認対象を選択' if success_mode else '操作対象を選択'))
        # goto 自体に操作対象は不要だが、要素を使う成功確認では選択を許可する。
        action = str(self.action.currentData())
        # 初期状態は要素選択から操作を推定するため、操作が空でも選択可能にする。
        self.pick_button.setEnabled(not action or success_mode or action in ELEMENT_SELECTOR_ACTIONS)
        # 未入力イベントは実行できないが、既存イベントの直前実行は新規追加時も利用できる。
        self.try_event_button.setEnabled(bool(action))
        self.execute_until_button.setEnabled(True)
        self.pick_button.style().unpolish(self.pick_button)
        self.pick_button.style().polish(self.pick_button)

    def _value_action(self) -> None:
        action = self.action.currentData()
        if action == 'upload_file':
            path, _ = QFileDialog.getOpenFileName(self, tr('送信するファイルを選択'))
            if path:
                self.value.setText(path)
        elif action == 'select':
            self._select_first = not self._select_first
            self.value.clear()
            self._update_action_fields()

    def _run_debug(
        self, working_text: str, operation, success_text,
        log_queue: queue.Queue[str] | None=None,
    ) -> None:
        buttons = [
            require(self, QPushButton, name) for name in
            ('pickButton', 'closeDebugButton', 'tryEventButton', 'executeUntilButton')
        ]
        for button in buttons:
            button.setEnabled(False)
        self.debug_result.appendPlainText(tr(working_text))
        future: Future = self._service_host.debug_pool.submit(operation)
        timer = QTimer(self)
        timer.setInterval(100)

        def poll() -> None:
            # 閉じた編集画面へ非同期結果を反映したり、再表示したりしない。
            if self._closing:
                timer.stop()
                return
            if log_queue is not None:
                lines: list[str] = []
                while True:
                    try:
                        lines.append(log_queue.get_nowait())
                    except queue.Empty:
                        break
                if lines:
                    self.debug_result.appendPlainText('\n'.join(lines))
            if not future.done():
                return
            timer.stop()
            for button in buttons:
                button.setEnabled(True)
            # 操作内容に依存するボタンは、現在の入力状態を使って再判定する。
            self._update_picker_destination()
            try:
                result = future.result()
                message = success_text(result) if callable(success_text) else str(success_text)
                self.debug_result.appendPlainText(message)
            except Exception as error:
                message = tr(str(error))
                self.debug_result.appendPlainText(tr(message))
            finally:
                self._restore_editor_focus()

        timer.timeout.connect(poll)
        timer.start()

    def _restore_editor_focus(self) -> None:
        """ブラウザー選択の成否にかかわらず、編集画面を前面へ戻す。"""
        if self._closing or not self.isVisible():
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()
        handle = self.windowHandle()
        if handle is not None:
            handle.requestActivate()
        if sys.platform != 'win32':
            self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
            return
        # Windows は別プロセスの Chrome が前面にあると通常の activateWindow() を
        # 拒否するため、前面スレッドへ一時的に接続してダイアログを復帰させる。
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        window_id = int(self.winId())
        foreground = user32.GetForegroundWindow()
        current_thread = kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = bool(
            foreground_thread and foreground_thread != current_thread
            and user32.AttachThreadInput(current_thread, foreground_thread, True)
        )
        try:
            user32.ShowWindow(window_id, 9)  # Windows の SW_RESTORE を指定する。
            user32.BringWindowToTop(window_id)
            user32.SetForegroundWindow(window_id)
            user32.SetFocus(window_id)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)

    def closeEvent(self, event) -> None:
        """画面を閉じる際に、この画面が開始した選択待機を残さない。"""
        self._closing = True
        self._service_host.debug_browser.cancel_selection()
        super().closeEvent(event)

    def pick_element(self) -> None:
        action = str(self.action.currentData())
        # ブラウザー側へフォーカスが移る前に、選択結果の反映先を確定する。
        pick_success_target = self.pick_button.property('pickDestination') == 'click_success'
        def picked(result: dict[str, Any]) -> str:
            diagnostics = str(result.get('path_diagnostics', '')).strip()
            diagnostic_suffix = f'\n{diagnostics}' if diagnostics else ''
            if pick_success_target:
                index = self.click_success_selector_type.findData(result['selector_type'])
                self.click_success_selector_type.setCurrentIndex(max(0, index))
                self.click_success_target.setText(result['selector'])
                self.click_success_iframe_path.setText(
                    _iframe_path_text(result.get('iframe_path', ''))
                )
                return f'成功確認要素を選択しました: {result.get("display", result["selector"])}{diagnostic_suffix}'
            index = self.selector_type.findData(result['selector_type'])
            self.selector_type.setCurrentIndex(max(0, index))
            self.selector.setText(result['selector'])
            self._load_multi_path_fields()
            self.fallback_selector_type.setCurrentText(result.get('fallback_selector_type', 'none'))
            self.fallback_selector.setText(result.get('fallback_selector', ''))
            self.iframe_path.setText(_iframe_path_text(result.get('iframe_path', '')))
            if action == 'screenshot':
                self.scroll_data = dict(result.get('scroll', {}))
            suggested = result.get('suggested_action', '')
            if not self.action.currentData() and suggested and self.action.findData(suggested) >= 0:
                self.action.setCurrentIndex(self.action.findData(suggested))
            return f'要素を選択しました: {result.get("display", result["selector"])}{diagnostic_suffix}'
        def select_target() -> dict[str, Any]:
            if action == 'screenshot':
                result = self._service_host.debug_browser.pick(
                    self.target_url.text().strip(), action,
                    selection_hint='スクリーンショット範囲の要素を選択してください',
                )
                try:
                    scroll = self._service_host.debug_browser.pick(
                        self.target_url.text().strip(), action,
                        require_scroll=True,
                    )
                except RuntimeError as error:
                    if str(error) != 'event.element_selection_cancelled':
                        raise
                    scroll = None
                # 新規保存では余分な階層を作らず、スクロール要素情報を直接保持する。
                result['scroll'] = dict(scroll) if scroll else {}
                if scroll:
                    result['display'] = (
                        f'{result.get("display", result["selector"])}'
                        f'{tr(" / スクロール: ")}{scroll["display"]}'
                    )
                return result
            result = self._service_host.debug_browser.pick(
                self.target_url.text().strip(), action,
            )
            return result

        self._run_debug(
            '画面で要素を選択してください',
            select_target,
            picked,
        )

    def close_debug_browser(self) -> None:
        self._run_debug(
            '画面を閉じています', self._service_host.debug_browser.close_browser, '画面を閉じました',
        )

    def try_event(self) -> None:
        event = self.result_data()
        preview_success = (
            event['action'] in SUCCESS_CONFIRM_ACTIONS
            and self.left_tabs.currentIndex() == 1
            and self.click_success_condition.currentData() in {'visible', 'hidden', 'operable'}
        )
        if preview_success:
            success_event = {
                'selector_type': _selector_type(self.click_success_selector_type),
                'selector': self.click_success_target.text().strip(),
                'fallback_selector_type': 'none', 'fallback_selector': '',
                'iframe_path': self.click_success_iframe_path.text().strip(),
                'timeout_ms': self.timeout.value(),
            }
            self._run_debug(
                '成功確認要素を検索しています',
                lambda: self._service_host.debug_browser.highlight_element(
                    success_event, self.target_url.text().strip(),
                ),
                '成功確認要素をブラウザー上で強調表示しました',
            )
            return
        from core.executor import find_variables
        variables: dict[str, str] = {}
        for name in find_variables([event]):
            value, ok = QInputDialog.getText(self, tr('変数入力'), name)
            if not ok:
                return
            variables[name] = value
        # 参照中の data パラメーターがある場合だけ PCL を読み込む。
        references = set(re.findall(
            r'(?<!\$)\$(\d{1,2})(?!\d)',
            ' '.join((
                str(event.get('selector', '')), str(event.get('fallback_selector', '')),
                str(event.get('value', '')), str(event.get('failure_target', '')),
            )),
        ))
        try:
            parameters = json.loads(str(event.get('selector_parameters_json', '') or '{}'))
        except (TypeError, ValueError):
            parameters = {}
        parent_group_data_paths = self._parent_group_data_paths()
        needs_pcl = bool(parent_group_data_paths) or bool(event.get('data_path')) or '${data:' in ' '.join(
            str(event.get(field, ''))
            for field in ('selector', 'fallback_selector', 'value', 'failure_target')
        ) or any(
            isinstance(parameters.get(number), dict)
            and parameters[number].get('source') == 'data'
            for number in references
        )
        records = self._service_host.db.list_data_records(enabled_only=True) if needs_pcl else []
        if needs_pcl and not records:
            show_warning(
                self, tr('イベント試行'),
                tr('データパラメーターに使用できる有効な PCL がありません。'),
            )
            return
        root_data = records[0]['data'] if records else None
        from core.executor import WorkflowExecutor
        try:
            loop_context = WorkflowExecutor.first_item_loop_context(
                root_data, parent_group_data_paths,
            )
        except (KeyError, TypeError, ValueError) as exc:
            show_warning(self, tr('イベント試行'), str(exc))
            return
        execution_logs: queue.Queue[str] = queue.Queue()

        def append_execution_log(message: str) -> None:
            """単体試行の解決済みパスを「操作結果」と日次ログの両方へ渡す。"""
            translated = tr(str(message))
            self._service_host.file_log.append(translated)
            execution_logs.put(translated)

        self._run_debug(
            'イベントを実行しています',
            lambda: self._service_host.debug_browser.execute_event(
                event, self.target_url.text().strip(),
                variables=variables, root_data=root_data,
                loop_context=loop_context,
                logger=append_execution_log,
            ),
            'イベントを実行しました',
            log_queue=execution_logs,
        )

    def execute_until_event(self) -> None:
        # イベントの試行時だけ実行系モジュールを読み込む。
        from core.executor import find_variables
        jobs = self._service_host.debug_jobs(
            self.event_id or None,
            current_event_limit=self.insert_at if not self.event_id else None,
        )
        variables: dict[str, str] = {}
        for name in sorted({name for job in jobs for name in find_variables(job['events'])}):
            value, ok = QInputDialog.getText(self, tr('変数入力'), name)
            if not ok:
                return
            variables[name] = value
        execution_logs: queue.Queue[str] = queue.Queue()

        def append_execution_log(message: str) -> None:
            """画面表示用キューと日次ログへ同じ実行内容を渡す。"""
            translated = tr(str(message))
            self._service_host.file_log.append(translated)
            execution_logs.put(translated)

        self._run_debug(
            '対象イベントの直前まで実行しています',
            lambda: self._service_host.debug_browser.execute_until(
                jobs, self.event_id or None, variables, self.target_url.text().strip(),
                logger=append_execution_log,
            ),
            '対象イベントの直前まで実行しました',
            log_queue=execution_logs,
        )

    def result_data(self) -> dict[str, Any]:
        failure_choice = str(self.failure_action.currentData())
        action = str(self.action.currentData())
        selector_type = _selector_type(self.selector_type)
        if action == 'wait':
            value = str(self.wait_condition.currentData())
        elif action == 'select' and self._select_first:
            value = SELECT_FIRST_VALUE
        else:
            value = self.value.text()
        success_condition = str(self.click_success_condition.currentData())
        success_json = '' if action not in SUCCESS_CONFIRM_ACTIONS or success_condition == 'none' else json.dumps(
            {
                'condition': success_condition,
                'selector_type': _selector_type(self.click_success_selector_type),
                'target': self.click_success_target.text().strip(),
            } | ({'iframe_path': self.click_success_iframe_path.text().strip()}
                 if self.click_success_iframe_path.text().strip() else {}),
            ensure_ascii=False,
        )
        return {
            'name': self.name.text().strip(),
            'action': action,
            'selector_type': selector_type,
            'selector': self.selector.text().strip(),
            'fallback_selector_type': (
                'none' if selector_type == 'path'
                else _selector_type(self.fallback_selector_type)
            ),
            'fallback_selector': (
                '' if selector_type == 'path' else self.fallback_selector.text().strip()
            ),
            'selector_parameters_json': (
                '' if not self._selector_parameters
                else json.dumps(self._selector_parameters, ensure_ascii=False, separators=(',', ':'))
            ),
            'iframe_path': _iframe_path_text(self.iframe_path.text()),
            'value': value,
            'success_json': success_json,
            'scroll_json': json.dumps(self.scroll_data, ensure_ascii=False) if action == 'screenshot' and self.scroll_data else '',
            'timeout_ms': self.timeout.value(),
            'enabled': int(self.enabled.isChecked()),
            # 「再読み込み」「指定 URL へ移動」は失敗後の復旧操作であり、
            # 失敗自体を成功扱いにはしない。明示的な「次へ進む」のみ継続する。
            'continue_on_error': int(failure_choice == 'continue'),
            'failure_action': failure_choice if failure_choice in {'refresh', 'goto'} else 'none',
            'failure_target': self.failure_target.text().strip() if failure_choice == 'goto' else '',
            'data_path': self.data_path.currentText().strip(),
            'guard': self.guard_data,
            'retry_count': self.retry_count.value(),
            'retry_interval_ms': self.retry_interval.value(),
        }


class EventGroupEditorDialog(QDialog):
    """グループ境界の組に保存されるループ・再試行機能を編集する。"""

    def __init__(self, parent: QWidget, event: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        event = event or {}
        load_ui_into(self, 'event_group.ui')
        self.setWindowTitle('イベントグループ編集' if event else 'イベントグループ追加')
        self.guard_data = decode_guard(event.get('guard_json', event.get('guard', '')))
        self.name = require(self, QLineEdit, 'nameEdit')
        self.enabled = require(self, QCheckBox, 'enabledCheck')
        self.loop_enabled = require(self, QCheckBox, 'loopCheck')
        self.retry_enabled = require(self, QCheckBox, 'retryCheck')
        self.data_path = require(self, QComboBox, 'dataPathCombo')
        self.path_button = require(self, QPushButton, 'choosePathButton')
        self.timeout = require(self, QSpinBox, 'timeoutSpin')
        self.retry_count = require(self, QSpinBox, 'retryCountSpin')
        self.retry_interval = require(self, QSpinBox, 'retryIntervalSpin')
        self.guard_summary = require(self, QLineEdit, 'guardSummaryEdit')
        self.name.setText(str(event.get('name', '')))
        self.enabled.setChecked(bool(event.get('enabled', 1)))
        self.loop_enabled.setChecked(bool(event.get('loop_enabled', str(event.get('data_path', '')).strip())))
        self.retry_enabled.setChecked(bool(event.get('retry_enabled', str(event.get('value', '')).strip())))
        list_paths = _schema_paths_of_type(self.parent().db.get_data_schema(), {'list'})
        self.data_path.addItems(list_paths)
        current_path = str(event.get('data_path', ''))
        if current_path and self.data_path.findText(current_path) < 0:
            self.data_path.addItem(current_path)
        self.data_path.setCurrentText(current_path)
        self.timeout.setValue(int(event.get('timeout_ms', 600_000)))
        stored_retry = str(event.get('value', '')).strip()
        self.retry_count.setValue(int(stored_retry or event.get('retry_count', 3)))
        self.retry_interval.setValue(int(event.get('retry_interval_ms', 0)))
        self.path_button.clicked.connect(self.choose_data_path)
        require(self, QPushButton, 'guardButton').clicked.connect(self.edit_guard)
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.loop_enabled.toggled.connect(self._update_fields)
        self.retry_enabled.toggled.connect(self._update_fields)
        self._update_guard_summary()
        self._update_fields()
        return
    def _update_fields(self) -> None:
        self.data_path.setEnabled(self.loop_enabled.isChecked())
        self.path_button.setEnabled(self.loop_enabled.isChecked())
        self.retry_count.setEnabled(self.retry_enabled.isChecked())
        self.retry_interval.setEnabled(self.retry_enabled.isChecked())

    def _update_guard_summary(self) -> None:
        self.guard_summary.setText(_localized_guard_summary(self.guard_data) or tr('常に実行'))

    def choose_data_path(self) -> None:
        dialog = DataPathPickerDialog(
            self, self.parent().db.get_data_schema(), self.data_path.currentText(), {'list'},
        )
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_path:
            self.data_path.setCurrentText(dialog.result_path)

    def edit_guard(self) -> None:
        dialog = GuardConditionEditorDialog(self, self.guard_data, self.parent().db.get_data_schema())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.guard_data = dialog.result_data()
            self._update_guard_summary()

    def _save(self) -> None:
        if not self.name.text().strip():
            show_warning(self, '入力エラー', 'グループ名を入力してください。')
            return
        if self.loop_enabled.isChecked() and not self.data_path.currentText().strip():
            show_warning(self, '入力エラー', '繰り返しに使用するデータリンクを選択してください。')
            return
        self.accept()

    def result_data(self) -> dict[str, Any]:
        retry_count = self.retry_count.value() if self.retry_enabled.isChecked() else 0
        return {
            'name': self.name.text().strip(), 'action': 'group_start',
            'selector_type': 'none', 'selector': '', 'fallback_selector_type': 'none',
            'fallback_selector': '', 'iframe_path': '',
            'value': str(retry_count) if self.retry_enabled.isChecked() else '',
            'timeout_ms': self.timeout.value(), 'enabled': int(self.enabled.isChecked()),
            'continue_on_error': 0, 'failure_action': 'none', 'failure_target': '',
            'data_path': self.data_path.currentText().strip() if self.loop_enabled.isChecked() else '',
            'loop_enabled': int(self.loop_enabled.isChecked()),
            'retry_enabled': int(self.retry_enabled.isChecked()),
            'guard': self.guard_data, 'retry_count': retry_count,
            'retry_interval_ms': self.retry_interval.value() if self.retry_enabled.isChecked() else 0,
        }


class GuardDialog(QDialog):
    def __init__(self, parent: QWidget, guard: object) -> None:
        super().__init__(parent)
        load_ui_into(self, 'guard_dialog.ui')
        self.editor = require(self, QPlainTextEdit, 'guardEdit')
        self.editor.setPlainText(json.dumps(decode_guard(guard), ensure_ascii=False, indent=2))
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        return
    def _accept(self) -> None:
        try:
            self.value = decode_guard(json.loads(self.editor.toPlainText()))
        except (ValueError, json.JSONDecodeError) as error:
            show_warning(self, '実行条件', str(error))
            return
        self.accept()


GUARD_OPERATOR_LABELS = {
    'eq': '等しい', 'ne': '等しくない', 'contains': '含む',
    'not_contains': '含まない', 'gt': 'より大きい', 'ge': '以上',
    'lt': 'より小さい', 'le': '以下', 'empty': '空',
    'not_empty': '空ではない', 'true': '真', 'false': '偽',
}
UNARY_GUARD_OPERATORS = {'empty', 'not_empty', 'true', 'false'}


def _schema_condition_paths(schema: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    def walk(node: dict[str, Any], prefix: str = '') -> None:
        for child in node.get('children', []):
            path = f'{prefix}.{child["name"]}' if prefix else child['name']
            if child.get('type') in {'object', 'list'}:
                walk(child, path)
            else:
                paths.append(path)
    walk(schema)
    for template in schema_templates(normalize_template_schema(schema)):
        prefix = f'@template.{template["name"]}'
        walk(template, prefix)
    return paths


def _schema_paths_of_type(schema: dict[str, Any], allowed_types: set[str]) -> list[str]:
    paths: list[str] = []
    def walk(node: dict[str, Any], prefix: str = '') -> None:
        for child in node.get('children', []):
            path = f'{prefix}.{child["name"]}' if prefix else child['name']
            if child.get('type') in allowed_types:
                paths.append(path)
            if child.get('type') in {'object', 'list'}:
                walk(child, path)
    walk(schema)
    for template in schema_templates(normalize_template_schema(schema)):
        prefix = f'@template.{template["name"]}'
        if 'object' in allowed_types:
            paths.append(prefix)
        walk(template, prefix)
    return paths


class DataPathPickerDialog(QDialog):
    # ダイアログは開くたびに作り直されるため、同じ構造ごとの表示状態を共有する。
    _display_states: dict[str, tuple[tuple[int, int], set[str]]] = {}

    def __init__(self, parent: QWidget, schema: dict[str, Any], current: str = '',
                 allowed_types: set[str] | None = None) -> None:
        super().__init__(parent)
        load_ui_into(self, 'data_path_picker.ui')
        self.setWindowTitle('データ構造から選択')
        self.result_path: str | None = None
        self.tree = require(self, QTreeWidget, 'pathTree')
        self._display_state_key = json.dumps(
            normalize_template_schema(schema), ensure_ascii=False, sort_keys=True,
            separators=(',', ':'),
        )
        self.toggle_all_button = require(self, QPushButton, 'toggleAllButton')
        configure_table_view(self.tree)
        for column, width in enumerate((210, 100, 300)):
            self.tree.setColumnWidth(column, width)
        selected_item: QTreeWidgetItem | None = None

        def add(parent: QTreeWidgetItem | QTreeWidget, node: dict[str, Any], prefix: str = '') -> None:
            nonlocal selected_item
            path = f'{prefix}.{node["name"]}' if prefix else node['name']
            item = QTreeWidgetItem([node['name'], node['type'], path])
            selectable = (
                node.get('type') in allowed_types if allowed_types is not None
                else node.get('type') not in {'object', 'list'}
            )
            item.setData(0, Qt.ItemDataRole.UserRole, path if selectable else None)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, path)
            parent.addChild(item) if isinstance(parent, QTreeWidgetItem) else parent.addTopLevelItem(item)
            for child in node.get('children', []):
                add(item, child, path)
            if selectable and path == current:
                selected_item = item

        for child in schema.get('children', []):
            add(self.tree, child)
        for template in schema_templates(normalize_template_schema(schema)):
            path = f'@template.{str(template.get("name", "")).strip()}'
            item = QTreeWidgetItem([str(template.get('name', '')), tr('テンプレート'), path])
            item.setData(
                0, Qt.ItemDataRole.UserRole,
                path if allowed_types is not None and 'object' in allowed_types else None,
            )
            item.setData(0, Qt.ItemDataRole.UserRole + 1, path)
            self.tree.addTopLevelItem(item)
            for child in template.get('children', []):
                add(item, child, path)
            if path == current and item.data(0, Qt.ItemDataRole.UserRole):
                selected_item = item
        self.toggle_all_button.clicked.connect(self._toggle_all)
        self.tree.expanded.connect(self._sync_toggle_all_button)
        self.tree.collapsed.connect(self._sync_toggle_all_button)
        self._sync_toggle_all_button()
        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
        self._restore_display_state()
        self.tree.itemDoubleClicked.connect(lambda *_: self._choose())
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._choose)
        buttons.rejected.connect(self.reject)

    def _restore_display_state(self) -> None:
        """初回は全折りたたみ、2回目以降は前回閉じた時の状態へ戻す。"""
        state = self._display_states.get(self._display_state_key)
        expanded_paths = state[1] if state is not None else set()
        for item in self._container_items():
            path = str(item.data(0, Qt.ItemDataRole.UserRole + 1) or '')
            item.setExpanded(path in expanded_paths)
        if state is not None:
            restore_scroll_position(self.tree, state[0])
            # レイアウト確定後にスクロール範囲が変わる場合も同じ位置へ戻す。
            QTimer.singleShot(0, lambda: restore_scroll_position(self.tree, state[0]))
        self._sync_toggle_all_button()

    def _save_display_state(self) -> None:
        """閉じる直前の展開項目を、表示名ではなく安定したデータパスで保存する。"""
        expanded_paths = {
            str(item.data(0, Qt.ItemDataRole.UserRole + 1) or '')
            for item in self._container_items() if item.isExpanded()
        }
        self._display_states[self._display_state_key] = (
            capture_scroll_position(self.tree), expanded_paths,
        )

    def done(self, result: int) -> None:
        self._save_display_state()
        super().done(result)

    def _container_items(self) -> list[QTreeWidgetItem]:
        """展開・折りたたみの対象となる親項目だけを返す。"""
        items: list[QTreeWidgetItem] = []
        stack = [self.tree.topLevelItem(index) for index in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.childCount():
                items.append(item)
                stack.extend(item.child(index) for index in range(item.childCount()))
        return items

    def _sync_toggle_all_button(self, *_args) -> None:
        """現在のツリー状態に合わせてボタンの図案と説明を切り替える。"""
        has_expanded = any(item.isExpanded() for item in self._container_items())
        set_tree_toggle_icon(self.toggle_all_button, not has_expanded)

    def _toggle_all(self) -> None:
        """一つのボタンで全項目の展開と折りたたみを反転する。"""
        if any(item.isExpanded() for item in self._container_items()):
            set_tree_expanded(self.tree, False)
        else:
            set_tree_expanded(self.tree, True)
        self._sync_toggle_all_button()

    def _choose(self) -> None:
        item = self.tree.currentItem()
        path = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not path:
            show_information(self, 'データ構造から選択', '値を持つデータ項目を選択してください。')
            return
        self.result_path = str(path)
        self.accept()


class GuardRuleEditorDialog(QDialog):
    def __init__(self, parent: QWidget, schema: dict[str, Any], rule: dict[str, str] | None = None) -> None:
        super().__init__(parent)
        load_ui_into(self, 'guard_rule.ui')
        self.setWindowTitle('条件編集')
        rule = rule or {'path': '', 'operator': 'eq', 'value': ''}
        self.path = require(self, QComboBox, 'pathCombo')
        self.operator = require(self, QComboBox, 'operatorCombo')
        self.expected = require(self, QLineEdit, 'valueEdit')
        self.path.addItems(_schema_condition_paths(schema))
        self.path.setCurrentText(rule['path'])
        for operator in OPERATORS:
            self.operator.addItem(GUARD_OPERATOR_LABELS[operator], operator)
        self.operator.setCurrentIndex(max(0, self.operator.findData(rule['operator'])))
        self.expected.setText(rule['value'])
        self.schema = schema
        self.operator.currentIndexChanged.connect(self._update_expected_state)
        require(self, QPushButton, 'choosePathButton').clicked.connect(self.choose_path)
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self._update_expected_state()

    def choose_path(self) -> None:
        dialog = DataPathPickerDialog(self, self.schema, self.path.currentText())
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_path:
            self.path.setCurrentText(dialog.result_path)

    def _update_expected_state(self) -> None:
        self.expected.setEnabled(self.operator.currentData() not in UNARY_GUARD_OPERATORS)

    def _save(self) -> None:
        if not self.path.currentText().strip():
            show_warning(self, '条件編集', 'データ項目を選択してください。')
            return
        self.accept()

    def result_data(self) -> dict[str, str]:
        return {
            'path': self.path.currentText().strip(),
            'operator': str(self.operator.currentData()),
            'value': '' if self.operator.currentData() in UNARY_GUARD_OPERATORS else self.expected.text(),
        }


class GuardConditionEditorDialog(QDialog):
    def __init__(self, parent: QWidget, guard: object, schema: dict[str, Any]) -> None:
        super().__init__(parent)
        load_ui_into(self, 'guard_condition.ui')
        self.setWindowTitle('実行条件設定')
        normalized = decode_guard(guard)
        self.rules = [dict(rule) for rule in normalized['rules']]
        self.schema = schema
        self.logic = require(self, QComboBox, 'logicCombo')
        self.logic.addItem('全ての条件に一致', 'all')
        self.logic.addItem('いずれかの条件に一致', 'any')
        self.logic.setCurrentIndex(max(0, self.logic.findData(normalized['logic'])))
        self.tree = require(self, QTreeWidget, 'ruleTree')
        configure_table_view(self.tree)
        for column, width in enumerate((320, 140, 260)):
            self.tree.setColumnWidth(column, width)
        self.tree.itemDoubleClicked.connect(lambda *_: self.edit_rule())
        bind_delete_key(self.tree, self.delete_rule)
        add = require(self, QPushButton, 'addRuleButton')
        add.clicked.connect(self.add_rule)
        require(self, QPushButton, 'editRuleButton').clicked.connect(self.edit_rule)
        delete = require(self, QPushButton, 'deleteRuleButton')
        delete.clicked.connect(self.delete_rule)
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        footer_buttons = [
            add,
            require(self, QPushButton, 'editRuleButton'),
            delete,
            buttons.button(QDialogButtonBox.StandardButton.Save),
            buttons.button(QDialogButtonBox.StandardButton.Cancel),
        ]
        common_width = max(button.sizeHint().width() for button in footer_buttons)
        for button in footer_buttons:
            button.setFixedWidth(common_width)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.refresh()

    def refresh(self, selected: int | None = None) -> None:
        scroll = capture_scroll_position(self.tree)
        self.tree.clear()
        for index, rule in enumerate(self.rules):
            item = QTreeWidgetItem([rule['path'], GUARD_OPERATOR_LABELS[rule['operator']], rule['value']])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            self.tree.addTopLevelItem(item)
            if index == selected:
                self.tree.setCurrentItem(item)
        restore_scroll_position(self.tree, scroll)

    def add_rule(self) -> None:
        current = self.tree.currentItem() if self.tree.selectedItems() else None
        insert_at = (
            int(current.data(0, Qt.ItemDataRole.UserRole)) + 1
            if current is not None else len(self.rules)
        )
        dialog = GuardRuleEditorDialog(self, self.schema)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.rules.insert(insert_at, dialog.result_data())
            self.refresh(insert_at)

    def edit_rule(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        index = int(item.data(0, Qt.ItemDataRole.UserRole))
        dialog = GuardRuleEditorDialog(self, self.schema, self.rules[index])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.rules[index] = dialog.result_data()
            self.refresh(index)

    def delete_rule(self) -> None:
        indexes = sorted({
            int(item.data(0, Qt.ItemDataRole.UserRole)) for item in self.tree.selectedItems()
        }, reverse=True)
        if indexes:
            for index in indexes:
                del self.rules[index]
            self.refresh()

    def result_data(self) -> dict[str, Any]:
        return {'logic': str(self.logic.currentData()), 'rules': self.rules}


class FlowEditorDialog(QDialog):
    def __init__(self, parent: QWidget, workflow: dict[str, Any] | None = None,
                 schema: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        load_ui_into(self, 'flow_editor.ui')
        self.setWindowTitle('業務フロー編集')
        workflow = workflow or {}
        self.schema = schema or {'type': 'object', 'children': []}
        self.guard_value = decode_guard(workflow.get('guard_json', workflow.get('guard', '')))
        self.name = require(self, QLineEdit, 'nameEdit')
        self.description = require(self, QPlainTextEdit, 'descriptionEdit')
        self.enabled = require(self, QCheckBox, 'enabledCheck')
        self.data_start = require(self, QCheckBox, 'dataStartCheck')
        self.guard_summary = require(self, QLineEdit, 'guardSummaryEdit')
        self.name.setText(str(workflow.get('name', '')))
        self.description.setPlainText(str(workflow.get('description', '')))
        self.enabled.setChecked(bool(workflow.get('enabled', True)))
        self.data_start.setChecked(bool(workflow.get('pcl_loop_start', False)))
        require(self, QPushButton, 'editGuardButton').clicked.connect(self.edit_guard)
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        self.update_guard_summary()

    def update_guard_summary(self) -> None:
        self.guard_summary.setText(
            _localized_guard_summary(self.guard_value) or tr('常に実行')
        )

    def edit_guard(self) -> None:
        dialog = GuardConditionEditorDialog(self, self.guard_value, self.schema)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.guard_value = dialog.result_data()
            self.update_guard_summary()

    def _accept_if_valid(self) -> None:
        if not self.name.text().strip():
            show_warning(self, tr('error.create_title'), '業務フロー名を入力してください。')
            return
        self.accept()

    def result_data(self) -> dict[str, Any]:
        return {
            'name': self.name.text().strip(),
            'description': self.description.toPlainText().strip(),
            'enabled': self.enabled.isChecked(),
            'data_start': self.data_start.isChecked(),
            'guard': self.guard_value,
        }


class WorkflowGroupDialog(QDialog):
    """Flow 一覧の管理用グループ名だけを編集する軽量ダイアログ。"""

    def __init__(
        self, parent: QWidget, name: str='', guard: Any=None,
        schema: dict[str, Any] | None=None,
    ) -> None:
        super().__init__(parent)
        load_ui_into(self, 'workflow_group.ui')
        self.schema = schema or {'type': 'object', 'children': []}
        self.guard_value = decode_guard(guard)
        self.name_edit = require(self, QLineEdit, 'nameEdit')
        self.guard_summary = require(self, QLineEdit, 'guardSummaryEdit')
        self.name_edit.setText(name)
        require(self, QPushButton, 'editGuardButton').clicked.connect(self._edit_guard)
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        self._update_guard_summary()

    def _edit_guard(self) -> None:
        dialog = GuardConditionEditorDialog(self, self.guard_value, self.schema)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.guard_value = dialog.result_data()
            self._update_guard_summary()

    def _update_guard_summary(self) -> None:
        self.guard_summary.setText(
            _localized_guard_summary(self.guard_value) or tr('常に実行')
        )

    def _accept_if_valid(self) -> None:
        if not self.name_edit.text().strip():
            show_warning(self, 'グループ', 'グループ名を入力してください。')
            return
        self.accept()

    def result_name(self) -> str:
        return self.name_edit.text().strip()

    def result_guard(self) -> dict[str, Any]:
        return self.guard_value


class FlowDesignPage(QWidget):
    EVENT_END_ID_ROLE = Qt.ItemDataRole.UserRole + 2
    WORKFLOW_NODE_ROLE = Qt.ItemDataRole.UserRole
    WORKFLOW_DATA_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, project_dir: Path, db: Database) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.db = db
        self.current_workflow_id: int | None = None
        self._event_tree_workflow_id: int | None = None
        self._event_display_states: dict[int, tuple[Any, ...]] = {}
        self.debug_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='qt-event-debug')
        self.file_log = DailyLogWriter(project_dir)
        self.debug_browser = DebugBrowserSession(
            project_dir, self.db.get_start_url() or 'https://github.com/?locale=ja',
            lambda _message, _source='DebugBrowserSession': None,
            lambda: profile_path(project_dir, self.db.get_auth_profile()),
            self.db.get_action_stable_ms,
        )
        self._load_designer_form()
        self.reload()
        return
    def _load_designer_form(self) -> None:
        load_ui_into(self, 'flow_design.ui')
        splitter = require(self, QSplitter, 'mainSplitter')
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 38)
        splitter.setStretchFactor(1, 62)
        splitter.setSizes([380, 620])
        designer_table = require(self, QTreeWidget, 'workflowTable')
        table_layout = designer_table.parentWidget().layout()
        self.workflow_table = HierarchicalReorderTreeWidget(designer_table.parentWidget())
        self.workflow_table.setObjectName('workflowTable')
        table_layout.replaceWidget(designer_table, self.workflow_table)
        designer_table.setObjectName('workflowTableDesignerPlaceholder')
        designer_table.setParent(None)
        designer_table.deleteLater()
        self.workflow_table.setColumnCount(5)
        self.workflow_table.setHeaderLabels([tr('flow.name'), tr('common.order'), tr('common.enabled'), tr('condition.execution_condition'), tr('flow.data_start')])
        configure_table_view(self.workflow_table, reorder=True)
        # CopyAction を受け付け、Qt 標準の移動後削除による行欠落を防ぐ。
        self.workflow_table.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        workflow_header = self.workflow_table.header()
        workflow_header.setMinimumSectionSize(36)
        # 業務フロー名を先頭、順番を第二列にして階層と順序を同時に読み取れるようにする。
        set_column_layout(
            self.workflow_table,
            logical_order=(0, 1, 2, 3, 4),
            widths=(190, 54, 58, 120, 82),
        )
        self.workflow_table.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.workflow_table.setExpandsOnDoubleClick(False)
        self.workflow_table.setContainerTest(
            lambda item: str((item.data(0, self.WORKFLOW_DATA_ROLE) or {}).get('kind')) == 'group'
        )
        self._workflow_reorder_pending = False
        self.workflow_table.orderChanged.connect(self._queue_workflow_tree_reorder)
        self.workflow_table.itemSelectionChanged.connect(self._workflow_selected)
        self.workflow_table.itemDoubleClicked.connect(self._workflow_double_clicked)
        self.workflow_table.viewport().installEventFilter(self)
        bind_structured_copy_paste(
            self.workflow_table, 'workflow', self._copy_workflow_payload,
            self._paste_workflow_payload,
        )
        bind_delete_key(self.workflow_table, self.delete_workflow)
        add_workflow = require(self, QPushButton, 'newWorkflowButton')
        add_workflow_menu = QMenu(add_workflow)
        add_workflow_menu.addAction('フロー追加', self.add_workflow)
        add_workflow_menu.addAction('グループ追加', self.add_workflow_group)
        add_workflow.setMenu(add_workflow_menu)
        require(self, QPushButton, 'editWorkflowButton').clicked.connect(self.edit_workflow)
        require(self, QPushButton, 'deleteWorkflowButton').clicked.connect(self.delete_workflow)
        workflow_up = require(self, QPushButton, 'moveWorkflowUpButton')
        workflow_down = require(self, QPushButton, 'moveWorkflowDownButton')
        configure_row_move_tooltips(workflow_up, workflow_down, '業務フロー')
        workflow_up.clicked.connect(lambda: self.workflow_table.moveSelected(-1))
        workflow_down.clicked.connect(lambda: self.workflow_table.moveSelected(1))
        self.workflow_group_toggle = require(self, QPushButton, 'workflowGroupToggleButton')
        self.workflow_group_toggle.clicked.connect(self._toggle_workflow_groups)
        self.workflow_table.itemExpanded.connect(self._update_workflow_group_toggle)
        self.workflow_table.itemCollapsed.connect(self._update_workflow_group_toggle)
        json_button = require(self, QPushButton, 'workflowJsonButton')
        json_menu = QMenu(json_button)
        json_menu.addAction(tr('flow.import_all_json'), self.import_json)
        json_menu.addAction(tr('flow.export_all_json'), self.export_json)
        json_button.setMenu(json_menu)
        self.event_title = require(self, QLabel, 'eventTitle')
        designer_event_tree = require(self, QTreeWidget, 'eventTree')
        event_tree_layout = designer_event_tree.parentWidget().layout()
        self.event_tree = HierarchicalReorderTreeWidget(designer_event_tree.parentWidget())
        self.event_tree.setObjectName('eventTree')
        event_tree_layout.replaceWidget(designer_event_tree, self.event_tree)
        designer_event_tree.setObjectName('eventTreeDesignerPlaceholder')
        designer_event_tree.setParent(None)
        designer_event_tree.deleteLater()
        self.event_tree.setColumnCount(6)
        self.event_tree.setHeaderLabels([tr('event.name'), tr('common.action'), tr('event.fixed_value'), tr('condition.execution_condition'), tr('common.enabled'), tr('common.order')])
        configure_table_view(self.event_tree, reorder=True)
        self.event_tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        event_header = self.event_tree.header()
        event_header.setMinimumSectionSize(36)
        # 表示順: イベント名、順番、有効、実行条件、操作、固定値。
        set_column_layout(
            self.event_tree,
            logical_order=(0, 5, 4, 3, 1, 2),
            widths=(220, 110, 220, 120, 70, 60),
        )
        self.event_tree.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.event_tree.setContainerTest(
            lambda item: str((item.data(0, Qt.ItemDataRole.UserRole + 1) or {}).get('action', '')).endswith('_start')
        )
        self._event_reorder_pending = False
        self.event_tree.orderChanged.connect(self._queue_event_tree_reorder)
        self.event_tree.setExpandsOnDoubleClick(False)
        self.event_tree.viewport().installEventFilter(self)
        self.event_tree.itemDoubleClicked.connect(self._event_double_clicked)
        bind_structured_copy_paste(
            self.event_tree, 'event', self._copy_event_payload, self._paste_event_payload,
        )
        bind_delete_key(self.event_tree, self.delete_event)
        add = require(self, QPushButton, 'addEventButton')
        add_menu = QMenu(add)
        add_menu.addAction(tr('event.add'), self.add_event)
        add_menu.addAction(tr('event.group_add'), self.add_group)
        add.setMenu(add_menu)
        require(self, QPushButton, 'editEventButton').clicked.connect(self.edit_event)
        require(self, QPushButton, 'deleteEventButton').clicked.connect(self.delete_event)
        move_up = require(self, QPushButton, 'moveUpButton')
        move_down = require(self, QPushButton, 'moveDownButton')
        configure_row_move_tooltips(move_up, move_down, 'イベント')
        move_up.clicked.connect(lambda: self.move_event(-1))
        move_down.clicked.connect(lambda: self.move_event(1))
        self.group_toggle_button = require(self, QPushButton, 'groupToggleButton')
        self.group_toggle_button.clicked.connect(self._toggle_all_groups)
        self.event_tree.itemExpanded.connect(self._update_group_toggle_button)
        self.event_tree.itemCollapsed.connect(self._update_group_toggle_button)
        self._update_group_toggle_button()

    def reload(
        self, select_id: int | None=None, *, select_node_id: int | None=None,
        select_first: bool=True, selected_node_ids: set[int] | None=None,
    ) -> None:
        previous = self.workflow_table.currentItem()
        previous_node_id = (
            int(previous.data(0, self.WORKFLOW_NODE_ROLE)) if previous is not None else None
        )
        if select_id is None and select_node_id is None:
            # グループ選択も含め、再読込前の行を安定した node ID で復元する。
            select_node_id = previous_node_id
        display_state = capture_tree_display_state(
            self.workflow_table, lambda item: item.data(0, self.WORKFLOW_NODE_ROLE),
        )
        workflows = {int(row['id']): dict(row) for row in self.db.list_workflows()}
        nodes = [dict(row) for row in self.db.list_workflow_outline()]
        current = select_id if select_id is not None else self.current_workflow_id
        items: dict[int, QTreeWidgetItem] = {}
        selected: QTreeWidgetItem | None = None
        for node in nodes:
            workflow = workflows.get(int(node['workflow_id'])) if node['workflow_id'] is not None else None
            if node['kind'] == 'group':
                data = {
                    'kind': 'group', 'node_id': int(node['id']), 'name': str(node['name']),
                    'guard_json': str(node.get('guard_json', '')),
                }
                values = [
                    node['name'], '', '',
                    _localized_guard_summary(decode_guard(node.get('guard_json', ''))) or tr('常に実行'),
                    '',
                ]
            elif workflow is not None:
                data = workflow | {'kind': 'flow', 'node_id': int(node['id'])}
                values = [
                    workflow['name'], workflow['position'],
                    tr('common.yes') if workflow['enabled'] else tr('common.no'),
                    _localized_guard_summary(decode_guard(workflow['guard_json'])) or tr('常に実行'),
                    '★' if workflow['pcl_loop_start'] else '',
                ]
            else:
                continue
            item = QTreeWidgetItem([str(value) for value in values])
            item.setData(0, self.WORKFLOW_NODE_ROLE, int(node['id']))
            item.setData(0, self.WORKFLOW_DATA_ROLE, data)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
            if workflow is not None:
                set_row_enabled_appearance(item, bool(workflow['enabled']))
            items[int(node['id'])] = item
            if (
                int(node['id']) == select_node_id
                or (select_node_id is None and workflow is not None and int(workflow['id']) == current)
            ):
                selected = item
        roots: list[QTreeWidgetItem] = []
        for node in nodes:
            item = items.get(int(node['id']))
            if item is None:
                continue
            parent = items.get(int(node['parent_id'])) if node['parent_id'] is not None else None
            if parent is not None:
                parent.addChild(item)
            else:
                roots.append(item)
        with bulk_view_update(self.workflow_table):
            self.workflow_table.clear()
            self.workflow_table.addTopLevelItems(roots)
            restore_tree_display_state(
                self.workflow_table, display_state,
                lambda item: item.data(0, self.WORKFLOW_NODE_ROLE),
            )
            if selected is not None:
                self.workflow_table.setCurrentItem(selected)
            elif roots and select_first:
                self.workflow_table.setCurrentItem(roots[0])
            else:
                self.workflow_table.setCurrentItem(None)
                self.workflow_table.clearSelection()
                self.current_workflow_id = None
                self.event_title.setText(tr('flow.selection_required'))
                self.event_tree.clear()
            if selected_node_ids:
                # 並べ替え前の複数選択を、安定したノード ID でまとめて復元する。
                for node_id in selected_node_ids:
                    item = items.get(node_id)
                    if item is not None:
                        item.setSelected(True)
        # 一括更新では選択通知を抑止するため、左側の選択結果を右側へ明示的に反映する。
        if self.workflow_table.currentItem() is not None:
            self._workflow_selected()
        self._update_workflow_group_toggle()

    def _workflow_selected(self) -> None:
        if self._event_tree_workflow_id is not None:
            self._event_display_states[self._event_tree_workflow_id] = (
                capture_tree_display_state(
                    self.event_tree,
                    lambda event_item: event_item.data(0, Qt.ItemDataRole.UserRole),
                )
            )
        item = self.workflow_table.currentItem()
        if item is None:
            return
        data = item.data(0, self.WORKFLOW_DATA_ROLE) or {}
        if data.get('kind') != 'flow':
            self.current_workflow_id = None
            self.event_title.setText(str(data.get('name', '')))
            self.event_tree.clear()
            self._event_tree_workflow_id = None
            return
        self.current_workflow_id = int(data['id'])
        self.event_title.setText(str(data['name']))
        self.load_events()

    def _reorder_workflow_rows(self, source: int, target: int) -> None:
        """旧画面 API からの呼出しも、ルート行の移動として互換維持する。"""
        if not 0 <= source < self.workflow_table.topLevelItemCount():
            return
        item = self.workflow_table.takeTopLevelItem(source)
        self.workflow_table.insertTopLevelItem(
            max(0, min(target, self.workflow_table.topLevelItemCount())), item,
        )
        self.workflow_table.setCurrentItem(item)
        self._persist_workflow_tree_order()

    def _workflow_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        data = item.data(0, self.WORKFLOW_DATA_ROLE) or {}
        if data.get('kind') == 'group':
            self.edit_workflow()
            return
        if column == 2:
            self.toggle_workflow()
        elif column == 4:
            self.toggle_data_start()
        elif column == 3:
            self.edit_guard_summary()
        else:
            self.edit_workflow()

    def _workflow_tree_nodes(self) -> list[tuple[int, int | None, int]]:
        nodes: list[tuple[int, int | None, int]] = []
        def append_children(parent: QTreeWidgetItem | None, parent_id: int | None) -> None:
            count = self.workflow_table.topLevelItemCount() if parent is None else parent.childCount()
            for index in range(count):
                item = self.workflow_table.topLevelItem(index) if parent is None else parent.child(index)
                node_id = int(item.data(0, self.WORKFLOW_NODE_ROLE))
                nodes.append((node_id, parent_id, index + 1))
                append_children(item, node_id)
        append_children(None, None)
        return nodes

    def _queue_workflow_tree_reorder(self, *_args) -> None:
        if self._workflow_reorder_pending:
            return
        self._workflow_reorder_pending = True
        QTimer.singleShot(0, self._persist_workflow_tree_order)

    def _persist_workflow_tree_order(self) -> None:
        self._workflow_reorder_pending = False
        selected = self.workflow_table.currentItem()
        selected_data = selected.data(0, self.WORKFLOW_DATA_ROLE) if selected is not None else {}
        selected_workflow_id = (
            selected_data.get('id') if selected_data.get('kind') == 'flow' else None
        )
        try:
            self.db.reorder_workflow_outline(self._workflow_tree_nodes())
        except ValueError as error:
            if str(error) != 'flow.name_duplicate':
                raise
            # 移動先の同一階層に同名フローがある場合は、DB上の配置へ戻す。
            show_warning(self, tr('error.create_title'), tr('flow.name_duplicate'))
        selected_node_id = (
            int(selected.data(0, self.WORKFLOW_NODE_ROLE)) if selected is not None else None
        )
        selected_node_ids = {
            int(item.data(0, self.WORKFLOW_NODE_ROLE))
            for item in self.workflow_table.selectedItems()
        }
        self.reload(
            selected_workflow_id, select_node_id=selected_node_id,
            selected_node_ids=selected_node_ids,
        )

    def _workflow_groups(self) -> list[QTreeWidgetItem]:
        groups: list[QTreeWidgetItem] = []
        def collect(item: QTreeWidgetItem) -> None:
            if (item.data(0, self.WORKFLOW_DATA_ROLE) or {}).get('kind') == 'group':
                groups.append(item)
            for index in range(item.childCount()):
                collect(item.child(index))
        for index in range(self.workflow_table.topLevelItemCount()):
            collect(self.workflow_table.topLevelItem(index))
        return groups

    def _update_workflow_group_toggle(self, *_args) -> None:
        groups = self._workflow_groups()
        all_expanded = bool(groups) and all(item.isExpanded() for item in groups)
        self.workflow_group_toggle.setEnabled(bool(groups))
        set_tree_toggle_icon(self.workflow_group_toggle, not all_expanded)

    def _toggle_workflow_groups(self) -> None:
        groups = self._workflow_groups()
        if not groups:
            return
        set_tree_expanded(self.workflow_table, not all(item.isExpanded() for item in groups))
        self._update_workflow_group_toggle()

    def eventFilter(self, watched, event) -> bool:
        """展開アイコンのダブルクリックを編集操作として扱わない。"""
        trees = [
            tree for tree in (
                getattr(self, 'workflow_table', None), getattr(self, 'event_tree', None),
            ) if tree is not None
        ]
        tree = next((candidate for candidate in trees if watched is candidate.viewport()), None)
        if tree is not None and event.type() == QEvent.Type.MouseButtonDblClick:
            position = event.position().toPoint()
            index = tree.indexAt(position)
            item = tree.itemAt(position)
            is_group = bool(item and (
                item.childCount() or
                (item.data(0, self.WORKFLOW_DATA_ROLE) or {}).get('kind') == 'group'
            ))
            if index.isValid() and index.column() == 0 and is_group:
                depth = 0
                parent = item.parent()
                while parent is not None:
                    depth += 1
                    parent = parent.parent()
                branch_right = (
                    tree.header().sectionViewportPosition(0)
                    + tree.indentation() * (depth + 1)
                )
                if position.x() <= branch_right:
                    return True
        return super().eventFilter(watched, event)

    def _event_double_clicked(self, _item: QTreeWidgetItem, column: int) -> None:
        """イベント表の列ごとにダブルクリック操作を振り分ける。"""
        if column == 4:
            self.toggle_event_enabled()
        elif column == 3:
            self.edit_event_guard()
        else:
            self.edit_event()

    def load_events(
        self, select_id: int | None=None, *, selected_ids: set[int] | None=None,
    ) -> None:
        current_display_state = capture_tree_display_state(
            self.event_tree,
            lambda item: item.data(0, Qt.ItemDataRole.UserRole),
        )
        if self._event_tree_workflow_id is not None:
            self._event_display_states[self._event_tree_workflow_id] = current_display_state
        if self.current_workflow_id is None:
            self.event_tree.clear()
            self._event_tree_workflow_id = None
            return
        display_state = self._event_display_states.get(
            self.current_workflow_id,
            current_display_state
            if self._event_tree_workflow_id == self.current_workflow_id
            else ((0, 0), False, set()),
        )
        rows = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        parent_stack: list[QTreeWidgetItem] = []
        roots: list[QTreeWidgetItem] = []
        items: dict[int, QTreeWidgetItem] = {}
        selected: QTreeWidgetItem | None = None
        for row in rows:
            action = str(row['action'])
            if action.endswith('_end'):
                if parent_stack:
                    parent_stack[-1].setData(0, self.EVENT_END_ID_ROLE, row['id'])
                    parent_stack.pop()
                continue
            # DB の内部 action 名をそのまま見せず、利用者向けの操作名で表示する。
            shown_action = tr(ACTION_LABELS.get(action, action))
            # クリック成功確認は内部設定であり、固定値列には表示しない。
            shown_value = '' if action == 'click' else row['value']
            values = [row['name'], shown_action, shown_value, _localized_guard_summary(decode_guard(row['guard_json'])) or tr('常に実行'), tr('common.yes') if row['enabled'] else tr('common.no'), row['position']]
            item = QTreeWidgetItem([str(value) for value in values])
            set_row_enabled_appearance(item, bool(row['enabled']))
            item.setData(0, Qt.ItemDataRole.UserRole, row['id'])
            item.setData(0, Qt.ItemDataRole.UserRole + 1, row)
            items[int(row['id'])] = item
            # ドロップ位置を全行で受け付け、実際の移動先は専用ツリー側で同階層に限定する。
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
            if parent_stack:
                parent_stack[-1].addChild(item)
            else:
                roots.append(item)
            if action.endswith('_start'):
                parent_stack.append(item)
                item.setExpanded(True)
            if row['id'] == select_id:
                selected = item
        # 全階層を画面外で組み立て、追加・展開状態復元・選択を一括更新する。
        with bulk_view_update(self.event_tree):
            self.event_tree.clear()
            self.event_tree.addTopLevelItems(roots)
            if selected is not None:
                self.event_tree.setCurrentItem(selected)
            if selected_ids:
                # 再読込で currentItem 以外の選択が消えないよう ID から復元する。
                for event_id in selected_ids:
                    item = items.get(event_id)
                    if item is not None:
                        item.setSelected(True)
            restore_tree_display_state(
                self.event_tree, display_state,
                lambda item: item.data(0, Qt.ItemDataRole.UserRole),
            )
        self._event_tree_workflow_id = self.current_workflow_id
        self._event_display_states[self.current_workflow_id] = capture_tree_display_state(
            self.event_tree, lambda item: item.data(0, Qt.ItemDataRole.UserRole),
        )
        self._update_group_toggle_button()

    def _group_items(self) -> list[QTreeWidgetItem]:
        """展開または折りたたみの対象になるグループ項目を取得する。"""
        groups: list[QTreeWidgetItem] = []

        def collect(item: QTreeWidgetItem) -> None:
            if item.childCount():
                groups.append(item)
            for child_index in range(item.childCount()):
                collect(item.child(child_index))

        for root_index in range(self.event_tree.topLevelItemCount()):
            collect(self.event_tree.topLevelItem(root_index))
        return groups

    def _update_group_toggle_button(self, *_args) -> None:
        """ツリー状態に合わせて共通の展開・折りたたみボタンを更新する。"""
        groups = self._group_items()
        all_expanded = bool(groups) and all(item.isExpanded() for item in groups)
        self.group_toggle_button.setEnabled(bool(groups))
        set_tree_toggle_icon(self.group_toggle_button, not all_expanded)

    def _toggle_all_groups(self) -> None:
        """現在のグループ状態と反対の一括操作を実行する。"""
        groups = self._group_items()
        if not groups:
            return
        if all(item.isExpanded() for item in groups):
            set_tree_expanded(self.event_tree, False)
        else:
            set_tree_expanded(self.event_tree, True)
        self._update_group_toggle_button()

    def _queue_event_tree_reorder(self, *_args) -> None:
        if self._event_reorder_pending:
            return
        self._event_reorder_pending = True
        QTimer.singleShot(0, self._persist_event_tree_order)

    def _persist_event_tree_order(self) -> None:
        self._event_reorder_pending = False
        if self.current_workflow_id is None:
            return
        selected = self.event_tree.currentItem()
        selected_id = selected.data(0, Qt.ItemDataRole.UserRole) if selected is not None else None
        selected_ids = {
            int(item.data(0, Qt.ItemDataRole.UserRole))
            for item in self.event_tree.selectedItems()
        }
        ordered: list[int] = []

        def append_item(item: QTreeWidgetItem) -> None:
            ordered.append(int(item.data(0, Qt.ItemDataRole.UserRole)))
            for child_index in range(item.childCount()):
                append_item(item.child(child_index))
            end_id = item.data(0, self.EVENT_END_ID_ROLE)
            if end_id is not None:
                ordered.append(int(end_id))

        for root_index in range(self.event_tree.topLevelItemCount()):
            append_item(self.event_tree.topLevelItem(root_index))
        existing = [row['id'] for row in self.db.list_events(self.current_workflow_id)]
        ordered.extend(event_id for event_id in existing if event_id not in ordered)
        self.db.reorder_events(self.current_workflow_id, ordered)
        self.load_events(selected_id, selected_ids=selected_ids)

    def _selected_workflow(self) -> dict[str, Any] | None:
        item = self.workflow_table.currentItem()
        if item is None:
            return None
        data = item.data(0, self.WORKFLOW_DATA_ROLE) or {}
        return dict(data) if data.get('kind') == 'flow' else None

    def _selected_event(self) -> dict[str, Any] | None:
        item = self.event_tree.currentItem()
        return dict(item.data(0, Qt.ItemDataRole.UserRole + 1)) if item is not None else None

    def _copy_workflow_payload(self) -> list[dict[str, Any]] | None:
        """選択した業務フローを表示順で、配下イベントごとコピーする。"""
        def serialize(item: QTreeWidgetItem) -> dict[str, Any]:
            data = dict(item.data(0, self.WORKFLOW_DATA_ROLE) or {})
            if data.get('kind') == 'group':
                return {
                    'group': str(data.get('name', '')),
                    'guard': decode_guard(data.get('guard_json', '')),
                    'children': [serialize(item.child(index)) for index in range(item.childCount())],
                }
            return {
                'workflow': data,
                'events': [dict(row) for row in self.db.list_events(int(data['id']))],
            }
        selected = selected_outer_items(self.workflow_table)
        return [serialize(item) for item in selected] or None

    def _paste_workflow_payload(self, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        sources = payload if isinstance(payload, list) else [payload]
        if not sources:
            return
        selected_item = self.workflow_table.currentItem()
        anchor_node_id = (
            int(selected_item.data(0, self.WORKFLOW_NODE_ROLE)) if selected_item is not None else None
        )

        def paste_group(source: dict[str, Any], anchor_id: int | None, *, sibling: bool) -> int:
            group_id = self.db.add_workflow_group(
                str(source.get('group', 'グループ')), decode_guard(source.get('guard')),
            )
            self._place_new_workflow_node(group_id, anchor_id, group_as_parent=not sibling)
            for child in source.get('children', []):
                if 'group' in child:
                    paste_group(child, group_id, sibling=False)
                else:
                    paste_flow(child, group_id, parent_id=group_id)
            return group_id

        def paste_flow(
            source_payload: dict[str, Any], anchor_id: int | None, *, parent_id: int | None=None,
        ) -> tuple[int, int]:
            source = dict(source_payload['workflow'])
            destination_parent = parent_id
            if destination_parent is None:
                destination_parent = self._workflow_insertion_parent_id(
                    anchor_id, group_as_parent=False,
                )
            name = unique_copy_name(
                str(source['name']), self.db.workflow_names_at_level(destination_parent),
            )
            workflow_id = self.db.add_workflow(
                name, str(source.get('description', '')), destination_parent,
            )
            self.db.set_workflow_enabled(workflow_id, bool(source.get('enabled', True)))
            self.db.set_workflow_guard(workflow_id, decode_guard(source.get('guard_json', '')))
            # データ開始位置は一意のため、元フロー側を維持して複製側には移動しない。
            for event in source_payload.get('events', []):
                self.db.add_event(workflow_id, copy.deepcopy(event))
            node = next(
                row for row in self.db.list_workflow_outline() if row['workflow_id'] == workflow_id
            )
            node_id = int(node['id'])
            self._place_new_workflow_node(
                node_id, anchor_id, group_as_parent=parent_id is not None,
            )
            return node_id, workflow_id

        last_workflow_id: int | None = None
        for source in sources:
            if 'group' in source:
                anchor_node_id = paste_group(source, anchor_node_id, sibling=True)
                last_workflow_id = None
            else:
                anchor_node_id, last_workflow_id = paste_flow(source, anchor_node_id)
        self.reload(last_workflow_id, select_node_id=anchor_node_id)

    def _copy_event_payload(self) -> list[dict[str, Any]] | None:
        """選択イベントを、グループの場合は終了境界までまとめてコピーする。"""
        selected = selected_outer_items(self.event_tree)
        if not selected or self.current_workflow_id is None:
            return None
        event_ids: list[int] = []

        def append_item(item: QTreeWidgetItem) -> None:
            event_ids.append(int(item.data(0, Qt.ItemDataRole.UserRole)))
            for child_index in range(item.childCount()):
                append_item(item.child(child_index))
            end_id = item.data(0, self.EVENT_END_ID_ROLE)
            if end_id is not None:
                event_ids.append(int(end_id))

        for item in selected:
            append_item(item)
        rows = {int(row['id']): dict(row) for row in self.db.list_events(self.current_workflow_id)}
        return [rows[event_id] for event_id in event_ids if event_id in rows]

    def _paste_event_payload(self, events: list[dict[str, Any]]) -> None:
        if self.current_workflow_id is None or not events:
            return
        insert_at = self._event_insertion_index(group_as_parent=False)
        event_ids = [
            self.db.add_event(self.current_workflow_id, copy.deepcopy(event))
            for event in events
        ]
        self._place_new_events(event_ids, insert_at)

    def debug_jobs(
        self, target_event_id: int | None,
        *, current_event_limit: int | None=None,
    ) -> list[dict[str, Any]]:
        if self.current_workflow_id is None:
            return []
        workflows = [dict(row) for row in self.db.list_workflows()]
        marker = next((row for row in workflows if row['pcl_loop_start']), None)
        marker_position = marker['position'] if marker else None
        records = [row for row in self.db.list_data_records() if row['enabled']]
        debug_record = records[0] if records else None
        jobs: list[dict[str, Any]] = []
        for workflow in workflows:
            if workflow['id'] != self.current_workflow_id and not workflow['enabled']:
                continue
            events = [dict(row) for row in self.db.list_events(workflow['id'])]
            if workflow['id'] == self.current_workflow_id:
                if current_event_limit is not None:
                    events = events[:current_event_limit]
                for event in events:
                    if event['id'] == target_event_id:
                        event['enabled'] = 1
            data = (
                debug_record['data'] if debug_record and marker_position is not None
                and workflow['position'] >= marker_position else None
            )
            jobs.append({
                'id': workflow['id'], 'position': workflow['position'], 'name': workflow['name'],
                'phase': 'pcl' if data is not None else 'once',
                'session': 1,
                'group': str(debug_record.get('execution_group', '1')) if data is not None else '1',
                'pcl_index': 1,
                'pcl_total': len(records) or 1,
                # イベント編集の実行ログで、使用したテストデータを明示する。
                'pcl_name': str(debug_record.get('name', '')) if data is not None else '',
                'events': events, 'guard': decode_guard(workflow['guard_json']),
                'guards': self.db.get_workflow_guards(int(workflow['id'])), 'data': data,
            })
            if workflow['id'] == self.current_workflow_id:
                break
        return jobs

    def shutdown(self) -> None:
        self.debug_browser.shutdown()
        self.debug_pool.shutdown(wait=False, cancel_futures=True)

    def add_workflow(self) -> None:
        selected_item = self.workflow_table.currentItem() if self.workflow_table.selectedItems() else None
        selected_node_id = (
            int(selected_item.data(0, self.WORKFLOW_NODE_ROLE)) if selected_item is not None else None
        )
        dialog = FlowEditorDialog(self, schema=self.db.get_data_schema())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.result_data()
        parent_id = self._workflow_insertion_parent_id(selected_node_id)
        if data['name'] in self.db.workflow_names_at_level(parent_id):
            show_warning(self, tr('error.create_title'), tr('flow.name_duplicate'))
            return
        workflow_id = self.db.add_workflow(data['name'], data['description'], parent_id)
        self.db.set_workflow_enabled(workflow_id, data['enabled'])
        self.db.set_workflow_guard(workflow_id, data['guard'])
        if data['data_start']:
            self.db.set_pcl_loop_start(workflow_id)
        node = next(
            row for row in self.db.list_workflow_outline()
            if row['workflow_id'] == workflow_id
        )
        self._place_new_workflow_node(int(node['id']), selected_node_id)
        self.reload(workflow_id)

    def _place_new_workflow_node(
        self, node_id: int, selected_node_id: int | None, *, group_as_parent: bool=True,
    ) -> None:
        """通常追加は Group 内、貼付けは選択 Group と同階層へ配置できる。"""
        rows = [dict(row) for row in self.db.list_workflow_outline()]
        by_id = {int(row['id']): row for row in rows}
        selected = by_id.get(selected_node_id) if selected_node_id is not None else None
        parent_id = self._workflow_insertion_parent_id(
            selected_node_id, group_as_parent=group_as_parent, rows=rows,
        )
        siblings = sorted(
            (row for row in rows if row['parent_id'] == parent_id and int(row['id']) != node_id),
            key=lambda row: (int(row['position']), int(row['id'])),
        )
        insert_at = len(siblings)
        if selected and (selected['kind'] != 'group' or not group_as_parent):
            insert_at = next(
                (index + 1 for index, row in enumerate(siblings) if int(row['id']) == int(selected['id'])),
                len(siblings),
            )
        siblings.insert(insert_at, by_id[node_id])
        children: dict[int | None, list[dict[str, Any]]] = {}
        for row in rows:
            if int(row['id']) == node_id:
                continue
            children.setdefault(row['parent_id'], []).append(row)
        for child_rows in children.values():
            child_rows.sort(key=lambda value: (int(value['position']), int(value['id'])))
        children[parent_id] = siblings
        ordered: list[tuple[int, int | None, int]] = []
        def append(parent: int | None) -> None:
            for position, row in enumerate(children.get(parent, []), 1):
                current_id = int(row['id'])
                ordered.append((current_id, parent, position))
                append(current_id)
        append(None)
        self.db.reorder_workflow_outline(ordered)

    def _workflow_insertion_parent_id(
        self, selected_node_id: int | None, *, group_as_parent: bool=True,
        rows: list[dict[str, Any]] | None=None,
    ) -> int | None:
        """選択行と追加方法から、新しいFlowを配置する親階層を返す。"""
        outline = rows or [dict(row) for row in self.db.list_workflow_outline()]
        selected = next(
            (row for row in outline if int(row['id']) == selected_node_id), None,
        ) if selected_node_id is not None else None
        if selected and selected['kind'] == 'group' and group_as_parent:
            return int(selected['id'])
        return selected['parent_id'] if selected else None

    def _place_new_workflow(self, workflow_id: int, selected_id: int | None) -> None:
        """従来の Flow ID 指定を管理ツリーのノード ID へ変換する互換窓口。"""
        rows = [dict(row) for row in self.db.list_workflow_outline()]
        node_id = next(int(row['id']) for row in rows if row['workflow_id'] == workflow_id)
        selected_node_id = next(
            (int(row['id']) for row in rows if row['workflow_id'] == selected_id), None,
        )
        self._place_new_workflow_node(node_id, selected_node_id)

    def add_workflow_group(self) -> None:
        selected = self.workflow_table.currentItem()
        selected_node_id = (
            int(selected.data(0, self.WORKFLOW_NODE_ROLE)) if selected is not None else None
        )
        dialog = WorkflowGroupDialog(self, schema=self.db.get_data_schema())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        group_id = self.db.add_workflow_group(dialog.result_name(), dialog.result_guard())
        self._place_new_workflow_node(group_id, selected_node_id)
        self.reload()
        for item in self.workflow_table.findItems(dialog.result_name(), Qt.MatchFlag.MatchExactly, 0):
            if (item.data(0, self.WORKFLOW_DATA_ROLE) or {}).get('kind') == 'group':
                self.workflow_table.setCurrentItem(item)
                break

    def edit_workflow(self) -> None:
        selected_item = self.workflow_table.currentItem()
        selected_data = selected_item.data(0, self.WORKFLOW_DATA_ROLE) if selected_item is not None else {}
        if selected_data.get('kind') == 'group':
            dialog = WorkflowGroupDialog(
                self, str(selected_data.get('name', '')),
                selected_data.get('guard_json', ''), self.db.get_data_schema(),
            )
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.db.update_workflow_group(
                    int(selected_data['node_id']), dialog.result_name(), dialog.result_guard(),
                )
                self.reload(select_node_id=int(selected_data['node_id']))
            return
        row = self._selected_workflow()
        if row is None:
            return
        dialog = FlowEditorDialog(self, row, self.db.get_data_schema())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.result_data()
        parent_id = self.db.workflow_parent_id(int(row['id']))
        if data['name'] in self.db.workflow_names_at_level(
            parent_id, exclude_workflow_id=int(row['id']),
        ):
            show_warning(self, tr('error.create_title'), tr('flow.name_duplicate'))
            return
        self.db.update_workflow(row['id'], data['name'], data['description'])
        self.db.set_workflow_enabled(row['id'], data['enabled'])
        self.db.set_workflow_guard(row['id'], data['guard'])
        if data['data_start']:
            self.db.set_pcl_loop_start(row['id'])
        elif row['pcl_loop_start']:
            self.db.set_pcl_loop_start(None)
        self.reload(row['id'])

    def delete_workflow(self) -> None:
        selected_items = set(self.workflow_table.selectedItems())
        # 親グループとその子が同時選択された場合、親グループだけを削除対象にする。
        items = []
        for item in selected_items:
            parent = item.parent()
            ancestor_selected = False
            while parent is not None:
                if parent in selected_items:
                    ancestor_selected = True
                    break
                parent = parent.parent()
            if not ancestor_selected:
                items.append(item)
        rows = [item.data(0, self.WORKFLOW_DATA_ROLE) or {} for item in items]
        if not rows:
            return
        has_group = any(row.get('kind') == 'group' for row in rows)
        message = (
            'グループ内の業務フローも削除されます。よろしいですか？'
            if len(rows) == 1 and has_group else
            tr('flow.delete_confirmation') if len(rows) == 1 else
            f'選択した {len(rows)} 件を削除しますか？'
        )
        if not confirm_deletion(self, message):
            return
        for row in rows:
            if row.get('kind') == 'group':
                self.db.delete_workflow_group(int(row['node_id']))
            elif row.get('id') is not None:
                self.db.delete_workflow(int(row['id']))
        self.current_workflow_id = None
        self.reload(select_first=False)

    def toggle_workflow(self) -> None:
        row = self._selected_workflow()
        if row:
            self.db.set_workflow_enabled(row['id'], not bool(row['enabled']))
            update_preserving_scroll(
                (self.workflow_table, self.event_tree), lambda: self.reload(row['id']),
            )

    def toggle_data_start(self) -> None:
        row = self._selected_workflow()
        if row is None:
            return
        self.db.set_pcl_loop_start(None if row['pcl_loop_start'] else row['id'])
        update_preserving_scroll(
            (self.workflow_table, self.event_tree), lambda: self.reload(row['id']),
        )

    def edit_guard_summary(self) -> None:
        row = self._selected_workflow()
        if row is None:
            return
        dialog = GuardConditionEditorDialog(self, row['guard_json'], self.db.get_data_schema())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.db.set_workflow_guard(row['id'], dialog.result_data())
            self.reload(row['id'])

    def import_json(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, tr('flow.import_all_json'), str(self.project_dir), 'JSON (*.json)')
        if not path:
            return
        if self.db.list_workflows() and not confirm_action(
            self, 'JSON 読込',
            '現在の業務フローを置き換えて JSON を読み込みますか？',
            confirm_text='読み込む',
        ):
            return
        try:
            self.db.import_workflow_collection(Path(path), SUPPORTED_ACTIONS, SUPPORTED_SELECTOR_TYPES)
        except (OSError, ValueError) as error:
            show_error(self, tr('error.import_title'), tr(str(error)))
            return
        self.current_workflow_id = None
        self.reload()
        show_file_imported(self, path)

    def export_json(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(self, tr('flow.export_all_json'), str(self.project_dir / 'workflows.json'), 'JSON (*.json)')
        if not path:
            return
        try:
            self.db.export_workflow_collection(Path(path))
        except (OSError, ValueError) as error:
            show_error(self, tr('error.import_title'), tr(str(error)))
            return
        show_file_exported(self, path)

    def _event_insertion_index(self, *, group_as_parent: bool=True) -> int:
        """選択行の種類から、新規イベントを挿入する平坦順序位置を取得する。"""
        ordered: list[int] = []

        def append_item(item: QTreeWidgetItem) -> None:
            ordered.append(int(item.data(0, Qt.ItemDataRole.UserRole)))
            for child_index in range(item.childCount()):
                append_item(item.child(child_index))
            end_id = item.data(0, self.EVENT_END_ID_ROLE)
            if end_id is not None:
                ordered.append(int(end_id))

        for root_index in range(self.event_tree.topLevelItemCount()):
            append_item(self.event_tree.topLevelItem(root_index))
        selected = self.event_tree.currentItem() if self.event_tree.selectedItems() else None
        if selected is None:
            return len(ordered)
        end_id = selected.data(0, self.EVENT_END_ID_ROLE)
        anchor_id = int(end_id if end_id is not None else selected.data(0, Qt.ItemDataRole.UserRole))
        # 通常追加は Group 内、貼付けは選択 Group の終了境界直後へ配置する。
        return ordered.index(anchor_id) + (end_id is None or not group_as_parent)

    def _place_new_events(self, event_ids: list[int], insert_at: int) -> None:
        """追加したイベント一式を計算済み位置へまとめて配置する。"""
        if self.current_workflow_id is None:
            return
        ordered = [
            row['id'] for row in self.db.list_events(self.current_workflow_id)
            if row['id'] not in event_ids
        ]
        ordered[insert_at:insert_at] = event_ids
        self.db.reorder_events(self.current_workflow_id, ordered)
        self.load_events(event_ids[0])

    def add_event(self) -> None:
        if self.current_workflow_id is None:
            show_information(self, tr('common.notice'), tr('flow.create_or_select_first'))
            return
        insert_at = self._event_insertion_index()
        dialog = EventEditorDialog(self, insert_at=insert_at)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            event_id = self.db.add_event(self.current_workflow_id, dialog.result_data())
            self._place_new_events([event_id], insert_at)

    def add_group(self) -> None:
        if self.current_workflow_id is None:
            return
        insert_at = self._event_insertion_index()
        dialog = EventGroupEditorDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        start = dialog.result_data()
        start_id = self.db.add_event(self.current_workflow_id, start)
        end = dict(start)
        end.update(action='group_end', value='', data_path='', retry_count=0,
                   retry_interval_ms=0, guard={'logic': 'all', 'rules': []})
        end_id = self.db.add_event(self.current_workflow_id, end)
        self._place_new_events([start_id, end_id], insert_at)

    def edit_event(self) -> None:
        row = self._selected_event()
        if row is None:
            return
        rows = [dict(item) for item in self.db.list_events(self.current_workflow_id)]
        is_group = str(row['action']) in {'group_start', 'loop_start', 'retry_start'}
        dialog = EventGroupEditorDialog(self, row) if is_group else EventEditorDialog(self, row)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            data = dialog.result_data()
            self.db.update_event(row['id'], data)
            pair_id = self._paired_boundary_event_id(rows, row['id'])
            if is_group and pair_id is not None:
                end = dict(data)
                end.update(action='group_end', value='', data_path='', retry_count=0,
                           retry_interval_ms=0, guard={'logic': 'all', 'rules': []})
                self.db.update_event(pair_id, end)
            self.load_events(row['id'])

    def toggle_event_enabled(self) -> None:
        """選択イベントと対応するグループ終端の有効状態を反転する。"""
        row = self._selected_event()
        if row is None or self.current_workflow_id is None:
            return
        enabled = not bool(row['enabled'])
        self.db.set_event_enabled(row['id'], enabled)
        rows = [dict(item) for item in self.db.list_events(self.current_workflow_id)]
        pair_id = self._paired_boundary_event_id(rows, row['id'])
        if pair_id is not None:
            self.db.set_event_enabled(pair_id, enabled)
        update_preserving_scroll(
            (self.event_tree,), lambda: self.load_events(row['id']),
        )

    def edit_event_guard(self) -> None:
        """選択イベントの実行条件だけを編集する。"""
        row = self._selected_event()
        if row is None:
            return
        dialog = GuardConditionEditorDialog(self, row['guard_json'], self.db.get_data_schema())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dict(row)
        updated['guard'] = dialog.result_data()
        self.db.update_event(row['id'], updated)
        self.load_events(row['id'])

    def delete_event(self) -> None:
        if self.current_workflow_id is None:
            return
        selected_ids = {
            int((item.data(0, Qt.ItemDataRole.UserRole + 1) or {})['id'])
            for item in self.event_tree.selectedItems()
            if (item.data(0, Qt.ItemDataRole.UserRole + 1) or {}).get('id') is not None
        }
        if not selected_ids:
            return
        if not confirm_deletion(self, tr('event.delete_confirmation')):
            return
        rows = [dict(item) for item in self.db.list_events(self.current_workflow_id)]
        delete_ids = set(selected_ids)
        for event_id in selected_ids:
            pair_id = self._paired_boundary_event_id(rows, event_id)
            if pair_id is None:
                continue
            start = next(index for index, item in enumerate(rows) if item['id'] == event_id)
            end = next(index for index, item in enumerate(rows) if item['id'] == pair_id)
            delete_ids.update(item['id'] for item in rows[min(start, end):max(start, end) + 1])
        self.db.delete_events(sorted(delete_ids), self.current_workflow_id)
        self.load_events()

    @staticmethod
    def _paired_boundary_event_id(rows: list[dict[str, Any]], event_id: int) -> int | None:
        selected = next((index for index, row in enumerate(rows) if row['id'] == event_id), None)
        if selected is None:
            return None
        action = str(rows[selected]['action'])
        pairs = {
            'loop_start': ('loop_start', 'loop_end', 1), 'loop_end': ('loop_start', 'loop_end', -1),
            'retry_start': ('retry_start', 'retry_end', 1), 'retry_end': ('retry_start', 'retry_end', -1),
            'group_start': ('group_start', 'group_end', 1), 'group_end': ('group_start', 'group_end', -1),
        }
        if action not in pairs:
            return None
        start_action, end_action, direction = pairs[action]
        depth = 0
        indexes = range(selected + 1, len(rows)) if direction > 0 else range(selected - 1, -1, -1)
        for index in indexes:
            candidate = str(rows[index]['action'])
            if candidate == (start_action if direction > 0 else end_action):
                depth += 1
            elif candidate == (end_action if direction > 0 else start_action):
                if depth == 0:
                    return int(rows[index]['id'])
                depth -= 1
        return None

    def move_event(self, direction: int) -> None:
        if self.current_workflow_id is not None:
            self.event_tree.moveSelected(direction)
