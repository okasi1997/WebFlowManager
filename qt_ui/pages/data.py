from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSignalBlocker, QTimer, Qt
from PySide6.QtGui import QBrush, QColor, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QPushButton,
    QSplitter, QTreeWidget, QTreeWidgetItem, QWidget,
)

from core.database import Database
from core.data_templates import (
    TEMPLATE_INSTANCES_KEY, copy_template_instance, new_template_instance,
    data_from_public_json, data_to_public_json, migrate_legacy_template_data,
    normalize_template_schema, schema_templates, sync_template_instance_names, template_by_id,
    template_instances,
)
from core.excel_io import read_records_excel, write_records_excel
from i18n import tr
from .structured import default_value, empty_record, record_default_value
from ..table_view import (
    HierarchicalReorderTreeWidget, bind_delete_key, bind_structured_copy_paste, bulk_view_update, capture_scroll_position,
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
    TEMPLATE_INSTANCE_ROLE = Qt.ItemDataRole.UserRole + 4

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.current_record: dict[str, Any] | None = None
        self.current_data: dict[str, Any] = {}
        self._value_tree_record_id: int | None = None
        self._value_display_states: dict[int, tuple[Any, ...]] = {}
        self._pending_record_data: dict[int, dict[str, Any]] = {}
        self._record_switch_in_progress = False
        load_ui_into(self, 'data.ui')
        record_card = require(self, QFrame, 'recordCard')
        value_card = require(self, QFrame, 'valueCard')
        splitter = require(self, QSplitter, 'dataSplitter')
        self._data_splitter = splitter
        self._splitter_user_adjusted = False
        self._splitter_resize_pending = False
        splitter.setChildrenCollapsible(False)
        # 左側は操作ボタンが収まる最小幅を維持し、余剰幅はデータ内容へ割り当てる。
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setHandleWidth(6)
        splitter.splitterMoved.connect(self._mark_splitter_adjusted)
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
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.tree.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.tree.setContainerTest(lambda _item: False)
        self.tree.orderChanged.connect(self._persist_record_order)
        for column, width in enumerate((150, 200)):
            self.tree.setColumnWidth(column, width)
        self.tree.currentItemChanged.connect(self._select_record)
        self.tree.itemSelectionChanged.connect(self._sync_record_buttons)
        self.tree.itemDoubleClicked.connect(self._record_double_clicked)
        bind_structured_copy_paste(
            self.tree, 'data-record', self._copy_record_payload, self._paste_record_payload,
        )
        bind_delete_key(self.tree, self.delete_record)
        self.values = require(self, QTreeWidget, 'valueTree')
        configure_table_view(self.values)
        self.values.headerItem().setText(2, tr('値  ✎'))
        self.values.headerItem().setToolTip(2, tr('鉛筆マークの値はダブルクリックで編集できます'))
        for column, width in enumerate((190, 100, 260, 260)):
            self.values.setColumnWidth(column, width)
        self.values.itemDoubleClicked.connect(lambda *_: self.edit_value())
        bind_delete_key(self.values, self._delete_selected_value)
        callbacks = {
            'addRecordButton': self.add_record, 'editRecordButton': self.edit_record,
            'copyRecordButton': self.copy_record, 'deleteRecordButton': self.delete_record,
            'editValueButton': self.edit_value, 'addListItemButton': self.add_list_item,
            'saveRecordButton': self.save_record,
        }
        for name, callback in callbacks.items():
            require(self, QPushButton, name).clicked.connect(callback)
        self.edit_record_button = require(self, QPushButton, 'editRecordButton')
        self.copy_record_button = require(self, QPushButton, 'copyRecordButton')
        self.delete_record_button = require(self, QPushButton, 'deleteRecordButton')

        self.template_button = require(self, QPushButton, 'addTemplateButton')
        template_operation_menu = QMenu(self.template_button)
        self.template_menu = template_operation_menu.addMenu(tr('テンプレート追加'))
        self.template_menu.aboutToShow.connect(self._rebuild_template_menu)
        template_operation_menu.addSeparator()
        self.rename_template_action = template_operation_menu.addAction(
            tr('名前変更'), self.rename_data_template,
        )
        self.copy_template_action = template_operation_menu.addAction(
            tr('複製'), self.copy_data_template,
        )
        self.move_template_up_action = template_operation_menu.addAction(
            tr('上へ移動'), lambda: self.move_data_template(-1),
        )
        self.move_template_down_action = template_operation_menu.addAction(
            tr('下へ移動'), lambda: self.move_data_template(1),
        )
        self.delete_template_action = template_operation_menu.addAction(
            tr('削除'), self.delete_data_template,
        )
        self.template_button.setMenu(template_operation_menu)
        self.values.currentItemChanged.connect(lambda *_: self._sync_template_buttons())

        list_button = require(self, QPushButton, 'addListItemButton')
        list_menu = QMenu(list_button)
        list_menu.addAction('リスト項目を追加', self.add_list_item)
        list_menu.addAction('リスト項目を削除', self.delete_list_item)
        list_button.setMenu(list_menu)
        io_button = require(self, QPushButton, 'exportDataJsonButton')
        io_menu = QMenu(io_button)
        io_menu.addAction('JSON を出力', self.export_json)
        io_menu.addAction('JSON を読み込む', self.import_json)
        io_menu.addSeparator()
        io_menu.addAction('Excel を出力', self.export_excel)
        io_menu.addAction('Excel を読み込む', self.import_excel)
        io_button.setMenu(io_menu)
        # メニュー設定後の最終 sizeHint を使い、左右どちらの操作ボタンも潰れない幅を確保する。
        for card, toolbar_name in (
            (record_card, 'recordToolbar'), (value_card, 'valueToolbar'),
        ):
            toolbar = require(self, QHBoxLayout, toolbar_name)
            margins = card.layout().contentsMargins()
            buttons = [
                toolbar.itemAt(index).widget()
                for index in range(toolbar.count())
                if toolbar.itemAt(index).widget() is not None
            ]
            button_width = sum(button.sizeHint().width() for button in buttons)
            # spacer 自体は伸縮させるが、各 layout item 間の余白は最小幅へ含める。
            button_width += toolbar.spacing() * max(0, toolbar.count() - 1)
            card.setMinimumWidth(max(
                card.minimumWidth(), button_width + margins.left() + margins.right(),
            ))
        self.value_toggle_all_button = require(self, QPushButton, 'valueToggleAllButton')
        self.value_toggle_all_button.clicked.connect(self._toggle_all_values)
        self.values.itemExpanded.connect(lambda *_: self._sync_value_toggle_button())
        self.values.itemCollapsed.connect(lambda *_: self._sync_value_toggle_button())
        self.reload()

    def resizeEvent(self, event: QResizeEvent) -> None:
        """ユーザーが分割線を動かすまでは、余剰幅を右カードへ配分する。"""
        super().resizeEvent(event)
        if self._splitter_user_adjusted or self._splitter_resize_pending:
            return
        self._splitter_resize_pending = True
        # 親画面のリサイズ後に一度だけ実行し、連続した resizeEvent をまとめる。
        QTimer.singleShot(0, self._apply_compact_split)

    def _mark_splitter_adjusted(self, _position: int, _index: int) -> None:
        """手動調整後は自動配置を止め、利用者が決めた幅を維持する。"""
        self._splitter_user_adjusted = True

    def _apply_compact_split(self) -> None:
        """左カードを操作ボタンが欠けない最小幅へ寄せる。"""
        self._splitter_resize_pending = False
        if self._splitter_user_adjusted:
            return
        left_minimum = self._data_splitter.widget(0).minimumWidth()
        right_minimum = self._data_splitter.widget(1).minimumWidth()
        available = max(0, self._data_splitter.width() - self._data_splitter.handleWidth())
        # setSizes 自体を手動ドラッグとして扱わないよう、通知だけを一時停止する。
        blocker = QSignalBlocker(self._data_splitter)
        self._data_splitter.setSizes([
            left_minimum,
            max(right_minimum, available - left_minimum),
        ])
        del blocker

    def reload(self, select_id: int | None = None) -> None:
        if select_id is None:
            current = self.tree.currentItem()
            current_record = (
                current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
            )
            if isinstance(current_record, dict):
                # 並べ替えや編集後も、左一覧の現在行を維持する。
                select_id = int(current_record['id'])
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
        self._record_switch_in_progress = True
        try:
            with bulk_view_update(self.tree):
                self.tree.clear()
                self.tree.addTopLevelItems(items)
            if selected is not None:
                self.tree.setCurrentItem(selected)
            elif self.tree.topLevelItemCount():
                self.tree.setCurrentItem(self.tree.topLevelItem(0))
        finally:
            self._record_switch_in_progress = False
        self._load_selected_record()
        restore_scroll_position(self.tree, scroll)
        self._sync_record_buttons()

    def selected(self) -> dict[str, Any] | None:
        item = self.tree.currentItem()
        return dict(item.data(0, Qt.ItemDataRole.UserRole)) if item else None

    def selected_records(self) -> list[dict[str, Any]]:
        """Ctrl 選択された実行データを、画面上の並び順で返す。"""
        selected_items = set(self.tree.selectedItems())
        return [
            dict(item.data(0, Qt.ItemDataRole.UserRole))
            for index in range(self.tree.topLevelItemCount())
            if (item := self.tree.topLevelItem(index)) in selected_items
        ]

    def _sync_record_buttons(self) -> None:
        """複数選択は一括削除だけに使用し、単一行操作を無効化する。"""
        count = len(self.tree.selectedItems())
        self.edit_record_button.setEnabled(count == 1)
        self.copy_record_button.setEnabled(count == 1)
        self.delete_record_button.setEnabled(count >= 1)

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

    def _load_selected_record(self) -> None:
        self.current_record = self.selected()
        self.current_data = self._record_data(self.current_record)
        self.render_values()

    def _record_data(self, record: dict[str, Any] | None) -> dict[str, Any]:
        """旧テンプレート値も画面を開いた時点で新しい実体形式へ変換する。"""
        record_id = int(record['id']) if record is not None else None
        if record_id is not None and record_id in self._pending_record_data:
            return copy.deepcopy(self._pending_record_data[record_id])
        data = copy.deepcopy((record or {}).get('data', {}))
        return migrate_legacy_template_data(self.db.get_data_schema(), data)

    def _select_record(
            self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None,
    ) -> None:
        """PCL ごとの変更を一時保持し、確認を出さずに表示を切り替える。"""
        if self._record_switch_in_progress:
            return
        next_record = (
            dict(current.data(0, Qt.ItemDataRole.UserRole)) if current is not None else None
        )
        self._cache_current_record()
        self.current_record = next_record
        self.current_data = self._record_data(next_record)
        self.render_values()

    def has_pending_changes(self) -> bool:
        self._cache_current_record()
        return bool(self._pending_record_data)

    def confirm_pending_changes(self) -> bool:
        if not self.has_pending_changes():
            return True
        choice = confirm_pending_changes(self, '実行データに未保存の変更があります。保存しますか？')
        if choice == 'save':
            self._save_pending_records_to_db()
            return True
        if choice == 'discard':
            selected_id = (self.current_record or {}).get('id')
            self._pending_record_data.clear()
            self.reload(selected_id)
            return True
        return False

    def _cache_current_record(self) -> None:
        """現在の編集値を PCL 単位で保持し、元に戻った値はキャッシュから除外する。"""
        if self.current_record is None:
            return
        record_id = int(self.current_record['id'])
        if self.current_data != self.current_record.get('data', {}):
            self._pending_record_data[record_id] = copy.deepcopy(self.current_data)
        else:
            self._pending_record_data.pop(record_id, None)

    def _save_pending_records_to_db(self) -> None:
        """一時保持した全 PCL の変更を一回の保存操作で確定する。"""
        self._cache_current_record()
        records = {int(record['id']): record for record in self.db.list_data_records()}
        for record_id, data in self._pending_record_data.items():
            record = records.get(record_id)
            if record is not None:
                self.db.update_data_record(record_id, record['name'], data)
        self._pending_record_data.clear()

    def render_values(self) -> None:
        current_display_state = capture_tree_display_state(
            self.values,
            lambda item: tuple(item.data(0, self.PATH_ROLE) or ()),
        )
        if self._value_tree_record_id is not None:
            self._value_display_states[self._value_tree_record_id] = current_display_state
        record_id = (
            int(self.current_record['id']) if self.current_record is not None else None
        )
        display_state = (
            self._value_display_states.get(
                record_id,
                current_display_state
                if self._value_tree_record_id == record_id
                else ((0, 0), False, set()),
            )
            if record_id is not None else current_display_state
        )
        schema = normalize_template_schema(self.db.get_data_schema())
        roots: list[QTreeWidgetItem] = []

        def populate_template(
                parent: QTreeWidgetItem, template: dict[str, Any], instance: dict[str, Any],
                data_path: list[Any], display_path: list[Any],
        ) -> None:
            """保存場所に依存せず、テンプレート実体の子フィールドを表示する。"""
            values = instance.get('data', {}) if isinstance(instance.get('data'), dict) else {}
            for child in template.get('children', []):
                add(
                    parent, child, values.get(child['name'], default_value(child)),
                    [*data_path, child['name']], present=child['name'] in values,
                    display_path=[*display_path, child['name']],
                )

        def add(
                parent, node: dict[str, Any], value: Any, path: list[Any],
                label: str | None = None, list_index: int | None = None,
                *, present: bool = True, display_path: list[Any] | None = None,
        ) -> None:
            kind = node['type']
            # 任意テンプレートは、現在の PCL に追加されるまでデータ内容へ表示しない。
            if node.get('data_template', False) and not present:
                return
            shown = (
                f'{len(value or [])} {tr("件")}' if kind == 'list'
                else ('' if kind == 'object' else (tr('はい') if value is True else tr('いいえ') if value is False else str(value or '')))
            )
            visible_path = display_path if display_path is not None else path
            item = QTreeWidgetItem([label or node['name'], kind, shown, '.'.join(map(str, visible_path))])
            item.setData(0, self.PATH_ROLE, path)
            item.setData(0, self.SCHEMA_ROLE, node)
            item.setData(0, self.LIST_INDEX_ROLE, list_index)
            self._show_value_editability(item)
            # 子階層を画面外で完成させ、トップ階層だけを最後に一括追加する。
            parent.addChild(item) if isinstance(parent, QTreeWidgetItem) else roots.append(item)
            if kind == 'object':
                mapping = value if isinstance(value, dict) else {}
                for child in node.get('children', []):
                    child_present = child['name'] in mapping
                    add(
                        item, child, mapping.get(child['name'], default_value(child)),
                        path + [child['name']], present=child_present,
                        display_path=[*visible_path, child['name']],
                    )
            elif kind == 'list':
                for index, entry in enumerate(value if isinstance(value, list) else []):
                    template = (
                        template_by_id(schema, str(entry.get('template_id', '')))
                        if isinstance(entry, dict) else None
                    )
                    if template is not None:
                        instance_name = str(entry.get('name', '')).strip() or str(template.get('name', ''))
                        entry_item = QTreeWidgetItem([
                            instance_name, tr('テンプレート'), '',
                            '.'.join(map(str, [*visible_path, instance_name])),
                        ])
                        entry_item.setData(0, self.PATH_ROLE, path + [index])
                        entry_item.setData(0, self.SCHEMA_ROLE, template)
                        entry_item.setData(0, self.LIST_INDEX_ROLE, index)
                        entry_item.setData(
                            0, self.TEMPLATE_INSTANCE_ROLE,
                            str(entry.get('instance_id', '')),
                        )
                        item.addChild(entry_item)
                        populate_template(
                            entry_item, template, entry, path + [index, 'data'],
                            [*visible_path, instance_name],
                        )
                        continue
                    entry_item = QTreeWidgetItem([f'[{index}]', 'item', '' if node.get('children') else str(entry), '.'.join(map(str, [*visible_path, index]))])
                    entry_item.setData(0, self.PATH_ROLE, path + [index])
                    entry_item.setData(0, self.SCHEMA_ROLE, node)
                    entry_item.setData(0, self.LIST_INDEX_ROLE, index)
                    self._show_value_editability(entry_item)
                    item.addChild(entry_item)
                    mapping = entry if isinstance(entry, dict) else {}
                    for child in node.get('children', []):
                        add(
                            entry_item, child, mapping.get(child['name'], default_value(child)),
                            path + [index, child['name']],
                            display_path=[*visible_path, index, child['name']],
                        )

        for child in schema.get('children', []):
            child_present = child['name'] in self.current_data
            add(
                roots, child, self.current_data.get(child['name'], default_value(child)),
                [child['name']], present=child_present,
            )
        for instance_index, instance in enumerate(template_instances(self.current_data)):
            template = template_by_id(schema, str(instance.get('template_id', '')))
            if template is None:
                continue
            instance_path = [TEMPLATE_INSTANCES_KEY, instance_index, 'data']
            instance_item = QTreeWidgetItem([
                str(instance.get('name', '')).strip() or str(template.get('name', '')),
                tr('テンプレート'), '', '',
            ])
            instance_item.setData(0, self.PATH_ROLE, [TEMPLATE_INSTANCES_KEY, instance_index])
            instance_item.setData(0, self.SCHEMA_ROLE, template)
            instance_item.setData(0, self.TEMPLATE_INSTANCE_ROLE, str(instance.get('instance_id', '')))
            roots.append(instance_item)
            populate_template(
                instance_item, template, instance, instance_path,
                [str(instance.get('name', ''))],
            )
        with bulk_view_update(self.values):
            self.values.clear()
            self.values.addTopLevelItems(roots)
            restore_tree_display_state(
                self.values, display_state,
                lambda item: tuple(item.data(0, self.PATH_ROLE) or ()),
            )
        self._value_tree_record_id = record_id
        if record_id is not None:
            self._value_display_states[record_id] = capture_tree_display_state(
                self.values,
                lambda item: tuple(item.data(0, self.PATH_ROLE) or ()),
            )
        self._sync_template_buttons()
        self._sync_value_toggle_button()

    def _expandable_value_items(self) -> list[QTreeWidgetItem]:
        """データ内容ツリーで展開可能な行だけを返す。"""
        result: list[QTreeWidgetItem] = []
        stack = [self.values.topLevelItem(i) for i in range(self.values.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.childCount():
                result.append(item)
                stack.extend(item.child(i) for i in range(item.childCount()))
        return result

    def _sync_value_toggle_button(self) -> None:
        items = self._expandable_value_items()
        should_expand = bool(items) and all(not item.isExpanded() for item in items)
        self.value_toggle_all_button.setEnabled(bool(items))
        set_tree_toggle_icon(self.value_toggle_all_button, should_expand)

    def _toggle_all_values(self) -> None:
        items = self._expandable_value_items()
        set_tree_expanded(self.values, bool(items) and all(not item.isExpanded() for item in items))
        self._sync_value_toggle_button()

    def _delete_selected_value(self) -> None:
        """選択行の種類に応じ、既存のテンプレートまたはリスト削除を呼び出す。"""
        if self._selected_template() is not None:
            self.delete_data_template()
        else:
            self.delete_list_item()

    def _rebuild_template_menu(self) -> None:
        """同じ定義を複数回追加できるテンプレートメニューを再構築する。"""
        self.template_menu.clear()
        templates = schema_templates(normalize_template_schema(self.db.get_data_schema()))
        for template in templates:
            action = self.template_menu.addAction(str(template.get('name', '')))
            action.triggered.connect(
                lambda _checked=False, item=template: self.add_data_template(item)
            )
        if not templates:
            action = self.template_menu.addAction(tr('追加できる構造がありません'))
            action.setEnabled(False)

    def add_data_template(self, template: dict[str, Any]) -> None:
        """選択した定義から、現在の PCL に新しいテンプレート実体を追加する。"""
        if self.current_record is None:
            show_information(self, tr('テンプレート追加'), tr('実行データを選択してください。'))
            return
        selected_list = self._selected_list()
        if selected_list is not None:
            instances, _node, selected_index = selected_list
            insert_at = len(instances) if selected_index is None else selected_index + 1
        else:
            instances = self.current_data.setdefault(TEMPLATE_INSTANCES_KEY, [])
            if not isinstance(instances, list):
                instances = []
                self.current_data[TEMPLATE_INSTANCES_KEY] = instances
            template_id = str(template.get('template_id', ''))
            if any(
                isinstance(instance, dict)
                and str(instance.get('template_id', '')) == template_id
                for instance in instances
            ):
                show_information(
                    self, tr('テンプレート追加'),
                    tr('最上位のテンプレートは1つの構造体として使用します。'),
                )
                return
            insert_at = len(instances)
        created = new_template_instance(template, instances)
        instances.insert(insert_at, created)
        self.render_values()
        self._select_template_instance(str(created['instance_id']))

    def _selected_template(self) -> tuple[list[Any], int, dict[str, Any]] | None:
        """選択行を包含するテンプレート実体と、その格納配列を返す。"""
        item = self.values.currentItem()
        while item is not None and not item.data(0, self.TEMPLATE_INSTANCE_ROLE):
            item = item.parent()
        if item is None:
            return None
        path = item.data(0, self.PATH_ROLE)
        if not isinstance(path, list) or not path or not isinstance(path[-1], int):
            return None
        container, index = self._resolve(path)
        if isinstance(container, list) and 0 <= index < len(container):
            instance = container[index]
            if isinstance(instance, dict):
                return container, index, instance
        return None

    def _sync_template_buttons(self) -> None:
        self.template_button.setEnabled(self.current_record is not None)
        selected = self._selected_template()
        enabled = selected is not None
        self.delete_template_action.setEnabled(enabled)
        self.rename_template_action.setEnabled(enabled)
        is_top_level = bool(
            selected and selected[0] is template_instances(self.current_data)
        )
        self.copy_template_action.setEnabled(enabled and not is_top_level)
        index = selected[1] if selected else -1
        count = len(selected[0]) if selected else 0
        self.move_template_up_action.setEnabled(enabled and index > 0)
        self.move_template_down_action.setEnabled(enabled and index + 1 < count)

    def delete_data_template(self) -> None:
        """現在の PCL だけから、選択した任意構造を取り除く。"""
        selected = self._selected_template()
        if selected is None:
            return
        instances, index, instance = selected
        if not confirm_deletion(self, f'{instance.get("name", "")}{tr(" を削除しますか？")}'):
            return
        instances.pop(index)
        self.render_values()

    def rename_data_template(self) -> None:
        selected = self._selected_template()
        if selected is None:
            return
        _instances, _index, instance = selected
        name, ok = QInputDialog.getText(
            self, tr('テンプレート名変更'), tr('テンプレート名'),
            text=str(instance.get('name', '')),
        )
        name = name.strip()
        if ok and name:
            instance['name'] = name
            self.render_values()
            self._select_template_instance(str(instance.get('instance_id', '')))

    def copy_data_template(self) -> None:
        selected = self._selected_template()
        if selected is None:
            return
        instances, index, instance = selected
        if instances is template_instances(self.current_data):
            return
        copied = copy_template_instance(instance, instances)
        instances.insert(index + 1, copied)
        self.render_values()
        self._select_template_instance(str(copied['instance_id']))

    def move_data_template(self, direction: int) -> None:
        selected = self._selected_template()
        if selected is None:
            return
        instances, index, instance = selected
        destination = index + direction
        if not 0 <= destination < len(instances):
            return
        instances[index], instances[destination] = instances[destination], instances[index]
        self.render_values()
        self._select_template_instance(str(instance.get('instance_id', '')))

    def _select_template_instance(self, instance_id: str) -> None:
        """再描画後も操作対象のテンプレート実体を選択する。"""
        stack = [
            self.values.topLevelItem(index)
            for index in reversed(range(self.values.topLevelItemCount()))
        ]
        while stack:
            item = stack.pop()
            if str(item.data(0, self.TEMPLATE_INSTANCE_ROLE) or '') == instance_id:
                self.values.setCurrentItem(item)
                return
            stack.extend(item.child(index) for index in reversed(range(item.childCount())))

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

    _MISSING_VALUE = object()

    def _resolve(
            self, path: list[Any], leaf_default: Any=_MISSING_VALUE,
    ) -> tuple[Any, Any]:
        """表示上だけ補完されている構造も、操作時に実データへ安全に作成する。"""
        if not path:
            raise ValueError('data path is empty')
        target: Any = self.current_data
        for index, key in enumerate(path[:-1]):
            next_key = path[index + 1]
            expected_type = list if isinstance(next_key, int) else dict
            if isinstance(target, dict):
                child = target.get(key)
                if not isinstance(child, expected_type):
                    child = expected_type()
                    target[key] = child
                target = child
            elif isinstance(target, list) and isinstance(key, int):
                while len(target) <= key:
                    target.append(expected_type())
                child = target[key]
                if not isinstance(child, expected_type):
                    child = expected_type()
                    target[key] = child
                target = child
            else:
                raise TypeError(f'invalid data path: {path!r}')

        leaf_key = path[-1]
        if leaf_default is not self._MISSING_VALUE:
            if isinstance(target, dict):
                current = target.get(leaf_key, self._MISSING_VALUE)
                wrong_container = (
                    isinstance(leaf_default, (dict, list))
                    and not isinstance(current, type(leaf_default))
                )
                if current is self._MISSING_VALUE or wrong_container:
                    target[leaf_key] = copy.deepcopy(leaf_default)
            elif isinstance(target, list) and isinstance(leaf_key, int):
                while len(target) <= leaf_key:
                    target.append(copy.deepcopy(leaf_default))
        return target, leaf_key

    def edit_value(self) -> None:
        item = self.values.currentItem()
        if item is None:
            return
        node, path = item.data(0, self.SCHEMA_ROLE), item.data(0, self.PATH_ROLE)
        kind = self._editable_value_kind(item)
        if not node or kind is None:
            return
        parent, key = self._resolve(path, default_value(node))
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
                parent, key = self._resolve(item.data(0, self.PATH_ROLE), [])
                return parent[key], node, selected_index
            item = item.parent()
        return None

    def add_list_item(self) -> None:
        selected = self._selected_list()
        if selected:
            values, node, selected_index = selected
            value = ({
                child['name']: record_default_value(child)
                for child in node.get('children', [])
                if not child.get('data_template', False)
            }
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
        records = self._copy_record_payload()
        if records:
            selected = self.selected()
            anchor_id = selected['id'] if selected else None
            for record in records:
                anchor_id = self._duplicate_record(record, anchor_id)

    def _copy_record_payload(self) -> list[dict[str, Any]] | None:
        """選択中の実行データを表示順で、実行結果を除いてコピーする。"""
        selected = set(self.tree.selectedItems())
        payload = [
            dict(item.data(0, Qt.ItemDataRole.UserRole) or {})
            for index in range(self.tree.topLevelItemCount())
            for item in [self.tree.topLevelItem(index)]
            if item in selected
        ]
        return payload or None

    def _paste_record_payload(self, record: dict[str, Any] | list[dict[str, Any]]) -> None:
        selected = self.selected() if self.tree.selectedItems() else None
        anchor_id = selected['id'] if selected else None
        records = record if isinstance(record, list) else [record]
        for source in records:
            anchor_id = self._duplicate_record(source, anchor_id)

    def _duplicate_record(self, record: dict[str, Any], selected_id: int | None) -> int:
        name = unique_copy_name(
            str(record['name']), (row['name'] for row in self.db.list_data_records()),
        )
        record_id = self.db.add_data_record(
            0, name, copy.deepcopy(record['data']), str(record.get('summary', '')),
        )
        self.db.set_data_record_group(record_id, str(record.get('execution_group', '1')))
        self.db.set_data_record_enabled(record_id, bool(record.get('enabled', True)))
        self._place_new_record(record_id, selected_id)
        return record_id

    def _place_new_record(self, record_id: int, selected_id: int | None) -> None:
        """新規・複製データを選択行の直後、未選択時は末尾へ配置する。"""
        record_ids = order_with_inserted_after(
            (record['id'] for record in self.db.list_data_records()),
            [record_id], selected_id,
        )
        self.db.reorder_data_records(record_ids)
        self.reload(record_id)

    def delete_record(self) -> None:
        records = self.selected_records()
        if not records:
            return
        message = (
            f'{records[0]["name"]} を削除しますか？'
            if len(records) == 1
            else f'選択した {len(records)} 件の実行データを削除しますか？'
        )
        if confirm_deletion(self, message):
            for record in records:
                self._pending_record_data.pop(int(record['id']), None)
            self.db.delete_data_records(0, [record['id'] for record in records])
            self.reload()
            self.tree.clearSelection()
            self.tree.setCurrentItem(None)

    def toggle_enabled(self) -> None:
        record = self.selected()
        if record:
            self.db.set_data_record_enabled(record['id'], not record['enabled'])
            self.reload(record['id'])

    def _record_double_clicked(self, _item, column: int) -> None:
        self.edit_record()

    def save_record(self, _checked: bool = False, *, show_message: bool = True) -> bool:
        record = self.current_record
        if record or self._pending_record_data:
            selected_id = record['id'] if record else None
            self._save_pending_records_to_db()
            self.reload(selected_id)
            if show_message:
                show_information(self, '保存', '実行データを保存しました。')
            return True
        return False

    def export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr('JSON 出力'), 'data_records.json', 'JSON (*.json)')
        if not path:
            return
        try:
            schema = self.db.get_data_schema()
            records = [
                {
                    'name': record['name'],
                    'summary': str(record.get('summary', '')),
                    'enabled': bool(record.get('enabled', True)),
                    'execution_group': str(record.get('execution_group', '1')),
                    'data': data_to_public_json(record.get('data', {}), schema),
                }
                for record in self.db.list_data_records()
            ]
            Path(path).write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
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
            schema = self.db.get_data_schema()
            for record in records:
                if isinstance(record, dict) and isinstance(record.get('data'), dict):
                    record['data'] = data_from_public_json(record['data'], schema)
                    sync_template_instance_names(record['data'], schema)
            self.db.replace_data_records(records)
            self._pending_record_data.clear()
            self.current_record = None
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
            self._pending_record_data.clear()
            self.current_record = None
            self.reload()
        except Exception as error:
            show_warning(self, 'Excel 読込', str(error))
            return
        show_file_imported(self, path)
