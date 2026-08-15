from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QPushButton,
    QSplitter, QTreeWidget, QTreeWidgetItem, QWidget,
)

from core.database import Database
from core.excel_io import read_records_excel, read_records_excel_with_schema, write_records_excel
from i18n import tr
from .structured import default_value, empty_record
from ..table_view import (
    HierarchicalReorderTreeWidget, bind_structured_copy_paste, bulk_view_update, capture_scroll_position,
    capture_tree_display_state, configure_table_view, order_with_inserted_after,
    restore_scroll_position, restore_tree_display_state, set_tree_expanded,
    unique_copy_name,
)
from ..ui_loader import (
    confirm_deletion, confirm_import_overwrite, confirm_pending_changes, load_ui_into, localize_dialog_buttons, require,
    set_tree_toggle_icon, show_file_exported, show_file_imported, show_information, show_warning,
)


class RecordMetadataDialog(QDialog):
    def __init__(self, parent: QWidget, record: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        load_ui_into(self, 'record_dialog.ui')
        self.name = require(self, QLineEdit, 'nameEdit')
        self.summary = require(self, QLineEdit, 'summaryEdit')
        self.name.setText(str((record or {}).get('name', '')))
        self.summary.setText(str((record or {}).get('summary', '')))
        # 実行グループは「実行管理」でのみ編集するため、この画面では行ごと非表示にする。
        require(self, QLabel, 'groupLabel').hide()
        require(self, QLineEdit, 'groupEdit').hide()
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

    def _save(self) -> None:
        if not self.name.text().strip():
            show_warning(self, '実行データ', '名称を入力してください。')
            return
        self.accept()

    def value(self) -> tuple[str, str]:
        return self.name.text().strip(), self.summary.text().strip()


class DataPage(QWidget):
    PATH_ROLE = Qt.ItemDataRole.UserRole + 1
    SCHEMA_ROLE = Qt.ItemDataRole.UserRole + 2
    LIST_INDEX_ROLE = Qt.ItemDataRole.UserRole + 3

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.current_record: dict[str, Any] | None = None
        self.current_data: dict[str, Any] = {}
        load_ui_into(self, 'data.ui')
        record_card = require(self, QFrame, 'recordCard')
        value_card = require(self, QFrame, 'valueCard')
        splitter = require(self, QSplitter, 'dataSplitter')
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setHandleWidth(6)
        splitter.setSizes([430, 700])
        designer_tree = require(self, QTreeWidget, 'recordTree')
        tree_layout = designer_tree.parentWidget().layout()
        self.tree = HierarchicalReorderTreeWidget(designer_tree.parentWidget())
        self.tree.setObjectName('recordTree')
        tree_layout.replaceWidget(designer_tree, self.tree)
        designer_tree.setObjectName('recordTreeDesignerPlaceholder')
        designer_tree.setParent(None)
        designer_tree.deleteLater()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([tr('名称'), tr('概要')])
        configure_table_view(self.tree, reorder=True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.tree.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.tree.setContainerTest(lambda _item: False)
        self.tree.orderChanged.connect(self._persist_record_order)
        for column, width in enumerate((150, 200)):
            self.tree.setColumnWidth(column, width)
        self.tree.currentItemChanged.connect(lambda *_: self._select_record())
        self.tree.itemDoubleClicked.connect(self._record_double_clicked)
        bind_structured_copy_paste(
            self.tree, 'data-record', self._copy_record_payload, self._paste_record_payload,
        )
        self.values = require(self, QTreeWidget, 'valueTree')
        configure_table_view(self.values)
        self.values.headerItem().setText(2, tr('値  ✎'))
        self.values.headerItem().setToolTip(2, tr('鉛筆マークの値はダブルクリックで編集できます'))
        for column, width in enumerate((190, 100, 260, 260)):
            self.values.setColumnWidth(column, width)
        self.values.itemDoubleClicked.connect(lambda *_: self.edit_value())
        callbacks = {
            'addRecordButton': self.add_record, 'editRecordButton': self.edit_record,
            'copyRecordButton': self.copy_record, 'deleteRecordButton': self.delete_record,
            'toggleRecordButton': self.toggle_enabled, 'editValueButton': self.edit_value,
            'addListItemButton': self.add_list_item, 'deleteListItemButton': self.delete_list_item,
            'saveRecordButton': self.save_record,
        }
        for name, callback in callbacks.items():
            require(self, QPushButton, name).clicked.connect(callback)

        # 今回実行と実行グループは実行管理へ集約し、左側にはデータ自体を
        # 管理する新規・編集・複製・削除を表示する。
        require(self, QPushButton, 'toggleRecordButton').hide()

        list_button = require(self, QPushButton, 'addListItemButton')
        list_menu = QMenu(list_button)
        list_menu.addAction('リスト項目を追加', self.add_list_item)
        list_menu.addAction('リスト項目を削除', self.delete_list_item)
        list_button.setMenu(list_menu)
        require(self, QPushButton, 'deleteListItemButton').hide()
        self.value_toggle_button = QPushButton()
        self.value_toggle_button.setObjectName('valueToggleButton')
        self.value_toggle_button.setFixedWidth(42)
        self.value_toggle_button.clicked.connect(self._toggle_values)
        value_toolbar = require(self, QHBoxLayout, 'valueToolbar')
        value_toolbar.insertWidget(3, self.value_toggle_button)
        self.values.itemExpanded.connect(lambda *_: self._sync_value_toggle_button())
        self.values.itemCollapsed.connect(lambda *_: self._sync_value_toggle_button())
        io_button = require(self, QPushButton, 'exportDataJsonButton')
        io_menu = QMenu(io_button)
        io_menu.addAction('JSON を出力', self.export_json)
        io_menu.addAction('JSON を読み込む', self.import_json)
        io_menu.addSeparator()
        io_menu.addAction('Excel を出力', self.export_excel)
        io_menu.addAction('Excel を読み込む', self.import_excel)
        io_menu.addAction('Excel を読み込む（データ構造を含む）', self.import_excel_with_schema)
        io_button.setMenu(io_menu)
        require(self, QPushButton, 'importDataJsonButton').hide()
        require(self, QPushButton, 'excelButton').hide()
        self.reload()

    def reload(self, select_id: int | None = None) -> None:
        scroll = capture_scroll_position(self.tree)
        selected = None
        items: list[QTreeWidgetItem] = []
        for record in self.db.list_data_records():
            item = QTreeWidgetItem([record['name'], record['summary']])
            item.setData(0, Qt.ItemDataRole.UserRole, record)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
            items.append(item)
            if record['id'] == select_id:
                selected = item
        # 行を画面外で作成し、モデル通知と再描画を一回にまとめる。
        with bulk_view_update(self.tree):
            self.tree.clear()
            self.tree.addTopLevelItems(items)
        if selected is not None:
            self.tree.setCurrentItem(selected)
        elif self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self.current_record, self.current_data = None, {}
            self.values.clear()
            self._sync_value_toggle_button()
        restore_scroll_position(self.tree, scroll)

    def selected(self) -> dict[str, Any] | None:
        item = self.tree.currentItem()
        return dict(item.data(0, Qt.ItemDataRole.UserRole)) if item else None

    def _persist_record_order(self) -> None:
        """画面上の行順をデータベースへ保存し、移動行の選択を維持する。"""
        current = self.tree.currentItem()
        selected_id = (
            current.data(0, Qt.ItemDataRole.UserRole)['id'] if current is not None else None
        )
        record_ids = [
            int(self.tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole)['id'])
            for index in range(self.tree.topLevelItemCount())
        ]
        self.db.reorder_data_records(record_ids)
        self.reload(selected_id)

    def _select_record(self) -> None:
        self.current_record = self.selected()
        self.current_data = copy.deepcopy((self.current_record or {}).get('data', {}))
        self.render_values()

    def has_pending_changes(self) -> bool:
        return bool(
            self.current_record
            and self.current_data != self.current_record.get('data', {})
        )

    def confirm_pending_changes(self) -> bool:
        if not self.has_pending_changes():
            return True
        choice = confirm_pending_changes(self, '実行データに未保存の変更があります。保存しますか？')
        if choice == 'save':
            return self.save_record(show_message=False)
        if choice == 'discard':
            self.current_data = copy.deepcopy(self.current_record.get('data', {}))
            self.render_values()
            return True
        return False

    def render_values(self) -> None:
        display_state = capture_tree_display_state(
            self.values,
            lambda item: tuple(item.data(0, self.PATH_ROLE) or ()),
        )
        schema = self.db.get_data_schema()
        roots: list[QTreeWidgetItem] = []

        def add(parent, node: dict[str, Any], value: Any, path: list[Any], label: str | None = None, list_index: int | None = None) -> None:
            kind = node['type']
            shown = f'{len(value or [])} {tr("件")}' if kind == 'list' else ('' if kind == 'object' else (tr('はい') if value is True else tr('いいえ') if value is False else str(value or '')))
            item = QTreeWidgetItem([label or node['name'], kind, shown, '.'.join(map(str, path))])
            item.setData(0, self.PATH_ROLE, path)
            item.setData(0, self.SCHEMA_ROLE, node)
            item.setData(0, self.LIST_INDEX_ROLE, list_index)
            self._show_value_editability(item)
            # 子階層を画面外で完成させ、トップ階層だけを最後に一括追加する。
            parent.addChild(item) if isinstance(parent, QTreeWidgetItem) else roots.append(item)
            if kind == 'object':
                mapping = value if isinstance(value, dict) else {}
                for child in node.get('children', []):
                    add(item, child, mapping.get(child['name'], default_value(child)), path + [child['name']])
            elif kind == 'list':
                for index, entry in enumerate(value if isinstance(value, list) else []):
                    entry_item = QTreeWidgetItem([f'[{index}]', 'item', '' if node.get('children') else str(entry), '.'.join(map(str, path + [index]))])
                    entry_item.setData(0, self.PATH_ROLE, path + [index])
                    entry_item.setData(0, self.SCHEMA_ROLE, node)
                    entry_item.setData(0, self.LIST_INDEX_ROLE, index)
                    self._show_value_editability(entry_item)
                    item.addChild(entry_item)
                    mapping = entry if isinstance(entry, dict) else {}
                    for child in node.get('children', []):
                        add(entry_item, child, mapping.get(child['name'], default_value(child)), path + [index, child['name']])

        for child in schema.get('children', []):
            add(roots, child, self.current_data.get(child['name'], default_value(child)), [child['name']])
        with bulk_view_update(self.values):
            self.values.clear()
            self.values.addTopLevelItems(roots)
            restore_tree_display_state(
                self.values, display_state,
                lambda item: tuple(item.data(0, self.PATH_ROLE) or ()),
            )
        self._sync_value_toggle_button()

    @staticmethod
    def _editable_value_kind(item: QTreeWidgetItem) -> str | None:
        """値セルを編集できる場合、その入力種別を返す。"""
        node = item.data(0, DataPage.SCHEMA_ROLE)
        if not node:
            return None
        kind = str(node.get('type', ''))
        if kind not in ('object', 'list'):
            return kind
        # 子定義のない list の各 item は文字列値として直接編集できる。
        if kind == 'list' and item.data(0, DataPage.LIST_INDEX_ROLE) is not None and not node.get('children'):
            return 'text'
        return None

    def _show_value_editability(self, item: QTreeWidgetItem) -> None:
        """編集可能な値だけを鉛筆マークと文字色で控えめに示す。"""
        if self._editable_value_kind(item) is None:
            return
        item.setText(2, f'✎  {item.text(2)}'.rstrip())
        item.setForeground(2, QBrush(QColor('#0b6fae')))
        item.setToolTip(2, tr('ダブルクリックで値を編集'))

    def _expandable_value_items(self) -> list[QTreeWidgetItem]:
        """データ内容ツリーの展開可能な項目を表示順で取得する。"""
        items: list[QTreeWidgetItem] = []
        stack = [
            self.values.topLevelItem(index)
            for index in reversed(range(self.values.topLevelItemCount()))
        ]
        while stack:
            item = stack.pop()
            if item.childCount():
                items.append(item)
            stack.extend(item.child(index) for index in reversed(range(item.childCount())))
        return items

    def _sync_value_toggle_button(self) -> None:
        """展開状態に合わせて、次に行う一括操作をボタンへ表示する。"""
        items = self._expandable_value_items()
        all_expanded = bool(items) and all(item.isExpanded() for item in items)
        # 展開対象がない場合は、機能しない空のボタンをツールバーに残さない。
        self.value_toggle_button.setVisible(bool(items))
        self.value_toggle_button.setEnabled(bool(items))
        set_tree_toggle_icon(self.value_toggle_button, not all_expanded)

    def _toggle_values(self) -> None:
        """データ内容の全項目を現在と反対の状態へ切り替える。"""
        items = self._expandable_value_items()
        if not items:
            return
        if all(item.isExpanded() for item in items):
            set_tree_expanded(self.values, False)
        else:
            set_tree_expanded(self.values, True)
        self._sync_value_toggle_button()

    def _resolve(self, path: list[Any]) -> tuple[Any, Any]:
        target: Any = self.current_data
        for key in path[:-1]:
            target = target[key]
        return target, path[-1]

    def edit_value(self) -> None:
        item = self.values.currentItem()
        if item is None:
            return
        node, path = item.data(0, self.SCHEMA_ROLE), item.data(0, self.PATH_ROLE)
        kind = self._editable_value_kind(item)
        if not node or kind is None:
            return
        parent, key = self._resolve(path)
        current = parent[key]
        if kind == 'boolean':
            text, ok = QInputDialog.getItem(self, tr('値を編集'), item.text(0), [tr('はい'), tr('いいえ')], 0 if current else 1, False)
            value = text == 'はい'
        elif kind == 'number':
            value, ok = QInputDialog.getDouble(self, tr('値を編集'), item.text(0), float(current or 0), decimals=6)
        else:
            value, ok = QInputDialog.getText(self, tr('値を編集'), item.text(0), text=str(current or ''))
        if ok:
            parent[key] = value
            self.render_values()

    def _selected_list(self):
        item = self.values.currentItem() if self.values.selectedItems() else None
        selected_index = None
        while item:
            if selected_index is None and item.data(0, self.LIST_INDEX_ROLE) is not None:
                selected_index = int(item.data(0, self.LIST_INDEX_ROLE))
            node = item.data(0, self.SCHEMA_ROLE)
            if node and node['type'] == 'list' and item.text(1) == 'list':
                parent, key = self._resolve(item.data(0, self.PATH_ROLE))
                return parent[key], node, selected_index
            item = item.parent()
        return None

    def add_list_item(self) -> None:
        selected = self._selected_list()
        if selected:
            values, node, selected_index = selected
            value = ({child['name']: default_value(child) for child in node.get('children', [])}
                     if node.get('children') else '')
            insert_at = len(values) if selected_index is None else selected_index + 1
            values.insert(insert_at, value)
            self.render_values()

    def delete_list_item(self) -> None:
        item = self.values.currentItem()
        while item and item.data(0, self.LIST_INDEX_ROLE) is None:
            item = item.parent()
        if item:
            path = item.data(0, self.PATH_ROLE)
            if path and isinstance(path[-1], int):
                parent, index = self._resolve(path)
                parent.pop(index)
                self.render_values()

    def _metadata(self, record):
        dialog = RecordMetadataDialog(self, record)
        return dialog.value() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def add_record(self) -> None:
        metadata = self._metadata(None)
        if metadata:
            selected = self.selected() if self.tree.selectedItems() else None
            name, summary = metadata
            record_id = self.db.add_data_record(0, name, empty_record(self.db.get_data_schema()), summary)
            self._place_new_record(record_id, selected['id'] if selected else None)

    def edit_record(self) -> None:
        record = self.selected()
        metadata = self._metadata(record) if record else None
        if metadata:
            name, summary = metadata
            self.db.update_data_record(record['id'], name, self.current_data)
            self.db.set_data_record_summary(record['id'], summary)
            self.reload(record['id'])

    def copy_record(self) -> None:
        record = self.selected()
        if record:
            self._duplicate_record(record, record['id'])

    def _copy_record_payload(self) -> dict[str, Any] | None:
        """選択中の実行データを、実行結果を除いてコピーする。"""
        return self.selected()

    def _paste_record_payload(self, record: dict[str, Any]) -> None:
        selected = self.selected() if self.tree.selectedItems() else None
        self._duplicate_record(record, selected['id'] if selected else None)

    def _duplicate_record(self, record: dict[str, Any], selected_id: int | None) -> None:
        name = unique_copy_name(
            str(record['name']), (row['name'] for row in self.db.list_data_records()),
        )
        record_id = self.db.add_data_record(
            0, name, copy.deepcopy(record['data']), str(record.get('summary', '')),
        )
        self.db.set_data_record_group(record_id, str(record.get('execution_group', '1')))
        self.db.set_data_record_enabled(record_id, bool(record.get('enabled', True)))
        self._place_new_record(record_id, selected_id)

    def _place_new_record(self, record_id: int, selected_id: int | None) -> None:
        """新規・複製データを選択行の直後、未選択時は末尾へ配置する。"""
        record_ids = order_with_inserted_after(
            (record['id'] for record in self.db.list_data_records()),
            [record_id], selected_id,
        )
        self.db.reorder_data_records(record_ids)
        self.reload(record_id)

    def delete_record(self) -> None:
        record = self.selected()
        if record and confirm_deletion(self, f'{record["name"]} を削除しますか？'):
            self.db.delete_data_record(0, record['id'])
            self.reload()

    def toggle_enabled(self) -> None:
        record = self.selected()
        if record:
            self.db.set_data_record_enabled(record['id'], not record['enabled'])
            self.reload(record['id'])

    def _record_double_clicked(self, _item, column: int) -> None:
        self.edit_record()

    def save_record(self, _checked: bool = False, *, show_message: bool = True) -> bool:
        record = self.selected()
        if record:
            self.db.update_data_record(record['id'], record['name'], self.current_data)
            self.reload(record['id'])
            if show_message:
                show_information(self, '保存', '実行データを保存しました。')
            return True
        return False

    def export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr('JSON 出力'), 'data_records.json', 'JSON (*.json)')
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self.db.list_data_records(), ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as error:
            show_warning(self, 'JSON 出力', str(error))
            return
        show_file_exported(self, path)

    def import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr('JSON 読込'), '', 'JSON (*.json)')
        if not path:
            return
        if not confirm_import_overwrite(self, bool(self.db.list_data_records()), 'データ'):
            return
        try:
            records = json.loads(Path(path).read_text(encoding='utf-8'))
            if not isinstance(records, list):
                raise ValueError('JSON の最上位は配列である必要があります。')
            self.db.replace_data_records(records)
            self.reload()
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as error:
            show_warning(self, 'JSON 読込', str(error))
            return
        show_file_imported(self, path)

    def export_excel(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr('Excel を出力'), 'data_records.xlsx', 'Excel (*.xlsx)')
        if not path:
            return
        try:
            write_records_excel(path, self.db.get_data_schema(), self.db.list_data_records())
        except Exception as error:
            show_warning(self, 'Excel 出力', str(error))
            return
        show_file_exported(self, path)

    def import_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr('Excel を読み込む'), '', 'Excel (*.xlsx)')
        if not path:
            return
        if not confirm_import_overwrite(self, bool(self.db.list_data_records()), 'データ'):
            return
        try:
            self.db.replace_data_records(read_records_excel(path, self.db.get_data_schema()))
            self.reload()
        except Exception as error:
            show_warning(self, 'Excel 読込', str(error))
            return
        show_file_imported(self, path)

    def import_excel_with_schema(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr('Excel を読み込む（データ構造を含む）'), '', 'Excel (*.xlsx)',
        )
        if not path:
            return
        has_existing = bool(
            self.db.list_data_records() or self.db.get_data_schema().get('children')
        )
        if not confirm_import_overwrite(self, has_existing, 'データとデータ構造'):
            return
        try:
            schema, records = read_records_excel_with_schema(path)
            self.db.save_data_schema(0, schema)
            self.db.replace_data_records(records)
            self.reload()
        except Exception as error:
            show_warning(self, 'Excel 読込', str(error))
            return
        show_file_imported(self, path)
