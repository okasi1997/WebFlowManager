from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QLineEdit, QMenu, QPushButton, QTreeWidget, QTreeWidgetItem, QWidget,
)

from core.database import Database
from ..table_view import (
    HierarchicalReorderTreeWidget, configure_row_move_tooltips, configure_table_view,
)
from ..ui_loader import (
    confirm_deletion, confirm_pending_changes, load_ui_into, localize_dialog_buttons, require,
    show_information, show_warning,
)


TYPES = ('text', 'number', 'boolean', 'object', 'list')
SCHEMA_INDEX_PATH_ROLE = int(Qt.ItemDataRole.UserRole) + 1
SCHEMA_NODE_KEY_ROLE = int(Qt.ItemDataRole.UserRole) + 2


def default_value(node: dict[str, Any]) -> Any:
    kind = node['type']
    if kind == 'object':
        return {child['name']: default_value(child) for child in node.get('children', [])}
    if kind == 'list':
        return []
    if kind == 'number':
        return 0
    if kind == 'boolean':
        return False
    return ''


def empty_record(schema: dict[str, Any]) -> dict[str, Any]:
    return {child['name']: default_value(child) for child in schema.get('children', [])}


def validate_schema(node: Any, location: str = 'Data') -> None:
    if not isinstance(node, dict) or not str(node.get('name', '')).strip():
        raise ValueError(f'{location}: 字段名不能为空')
    if node.get('type') not in TYPES:
        raise ValueError(f'{location}: 不支持的类型 {node.get("type")}')
    children = node.get('children', [])
    if node['type'] in ('object', 'list'):
        if not isinstance(children, list):
            raise ValueError(f'{location}: children 必须是数组')
        names: set[str] = set()
        for child in children:
            name = str(child.get('name', '')) if isinstance(child, dict) else ''
            if name in names:
                raise ValueError(f'{location}: 字段名重复：{name}')
            names.add(name)
            validate_schema(child, f'{location}.{name}')
    elif children:
        raise ValueError(f'{location}: 基本类型不能包含子字段')


class FieldDialog(QDialog):
    def __init__(self, parent: QWidget, node: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        load_ui_into(self, 'field_dialog.ui')
        self.setWindowTitle('フィールド編集' if node else 'フィールド追加')
        self.name = require(self, QLineEdit, 'nameEdit')
        self.name.setText(str((node or {}).get('name', '')))
        self.kind = require(self, QComboBox, 'typeCombo')
        self.kind.addItems(TYPES)
        self.kind.setCurrentText(str((node or {}).get('type', 'text')))
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

    def value(self) -> dict[str, Any]:
        node: dict[str, Any] = {'name': self.name.text().strip(), 'type': self.kind.currentText()}
        if node['type'] in ('object', 'list'):
            node['children'] = []
        return node


class SchemaPage(QWidget):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.schema: dict[str, Any] = {}
        load_ui_into(self, 'schema.ui')
        designer_tree = require(self, QTreeWidget, 'schemaTree')
        tree_layout = designer_tree.parentWidget().layout()
        self.tree = HierarchicalReorderTreeWidget(designer_tree.parentWidget())
        self.tree.setObjectName('schemaTree')
        tree_layout.replaceWidget(designer_tree, self.tree)
        designer_tree.setObjectName('schemaTreeDesignerPlaceholder')
        designer_tree.setParent(None)
        designer_tree.deleteLater()
        configure_table_view(self.tree, reorder=True)
        # 実データの移動は共通ツリーが CopyAction を受けて安全に行うため、
        # Qt 標準の MoveAction 限定モードを解除し、ドラッグ表示も有効にする。
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(['フィールド名', '種別', 'イベントリンクパス'])
        self.tree.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.tree.setContainerTest(
            lambda item: str((item.data(0, Qt.ItemDataRole.UserRole) or {}).get('type', '')) in ('object', 'list')
        )
        self.tree.setMoveTest(
            lambda source, parent: all(
                parent.child(index) is source or parent.child(index).text(0) != source.text(0)
                for index in range(parent.childCount())
            )
        )
        for column, width in enumerate((240, 120, 360)):
            self.tree.setColumnWidth(column, width)
        self._schema_reorder_pending = False
        self._reorder_selected_key: int | None = None
        self.tree.orderChanged.connect(self._queue_schema_reorder)
        self.tree.itemDoubleClicked.connect(lambda *_: self.edit_field())
        actions = {
            'editFieldButton': self.edit_field, 'deleteFieldButton': self.delete_field,
            'moveFieldUpButton': lambda: self.move(-1), 'moveFieldDownButton': lambda: self.move(1),
        }
        for name, callback in actions.items():
            require(self, QPushButton, name).clicked.connect(callback)
        configure_row_move_tooltips(
            require(self, QPushButton, 'moveFieldUpButton'),
            require(self, QPushButton, 'moveFieldDownButton'),
            'フィールド',
        )
        self.toggle_all_button = require(self, QPushButton, 'toggleAllButton')
        self.toggle_all_button.clicked.connect(self._toggle_all)
        self.tree.itemExpanded.connect(lambda *_: self._sync_toggle_all_button())
        self.tree.itemCollapsed.connect(lambda *_: self._sync_toggle_all_button())
        add_button = require(self, QPushButton, 'addFieldButton')
        add_menu = QMenu(add_button)
        add_menu.addAction('フィールド追加', self.add_field)
        add_menu.addAction('子フィールド追加', self.add_child)
        add_button.setMenu(add_menu)
        require(self, QPushButton, 'addChildButton').hide()
        export_button = require(self, QPushButton, 'exportSchemaButton')
        import_button = require(self, QPushButton, 'importSchemaButton')
        save = require(self, QPushButton, 'saveSchemaButton')
        io_menu = QMenu(export_button)
        io_menu.addAction('JSON を出力', self.export_json)
        io_menu.addAction('JSON を読み込む', self.import_json)
        export_button.setMenu(io_menu)
        import_button.hide()
        save.clicked.connect(self.save)
        self.reload()

    def reload(self) -> None:
        self.schema = copy.deepcopy(self.db.get_data_schema())
        self._saved_schema = copy.deepcopy(self.schema)
        self.render()

    def has_pending_changes(self) -> bool:
        if self._schema_reorder_pending:
            self._apply_schema_tree_order()
        return self.schema != getattr(self, '_saved_schema', self.schema)

    def confirm_pending_changes(self) -> bool:
        if not self.has_pending_changes():
            return True
        choice = confirm_pending_changes(self, 'データ構造に未保存の変更があります。保存しますか？')
        if choice == 'save':
            return self.save(show_message=False)
        if choice == 'discard':
            self.reload()
            return True
        return False

    def render(self, selected_index_path: tuple[int, ...] | None = None) -> None:
        # Qt の UserRole に格納した dict は QVariant 変換時に複製される場合があるため、
        # 選択位置の特定には schema 内の安定したインデックス経路だけを使用する。
        previous_item = self.tree.currentItem()
        if selected_index_path is None and previous_item is not None:
            selected_index_path = self._item_index_path(previous_item)
        vertical_value = self.tree.verticalScrollBar().value()
        horizontal_value = self.tree.horizontalScrollBar().value()
        had_items = self.tree.topLevelItemCount() > 0
        expanded_keys = {
            item.data(0, SCHEMA_NODE_KEY_ROLE)
            for item in self._all_items() if item.isExpanded()
        }
        self.tree.clear()
        selected_item: QTreeWidgetItem | None = None

        def add(
            parent: QTreeWidgetItem | QTreeWidget,
            node: dict[str, Any],
            path: str,
            index_path: tuple[int, ...],
        ) -> None:
            nonlocal selected_item
            item = QTreeWidgetItem([node['name'], node['type'], path])
            item.setData(0, Qt.ItemDataRole.UserRole, node)
            item.setData(0, SCHEMA_INDEX_PATH_ROLE, index_path)
            # 表示順が変わっても同じノードを選び直せるよう、画面内だけで使う識別値を保持する。
            item.setData(0, SCHEMA_NODE_KEY_ROLE, id(node))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
            if node['type'] in ('object', 'list'):
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDropEnabled)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            parent.addChild(item) if isinstance(parent, QTreeWidgetItem) else parent.addTopLevelItem(item)
            if index_path == selected_index_path:
                selected_item = item
            for child_index, child in enumerate(node.get('children', [])):
                child_path = f'{path}.{child["name"]}' if path else child['name']
                add(item, child, child_path, (*index_path, child_index))
        for child_index, child in enumerate(self.schema.get('children', [])):
            add(self.tree, child, child['name'], (child_index,))

        if had_items:
            for item in self._all_items():
                item.setExpanded(item.data(0, SCHEMA_NODE_KEY_ROLE) in expanded_keys)
        else:
            self.tree.expandAll()
        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
        # setCurrentItem() による自動スクロールを打ち消し、操作前の表示位置を維持する。
        self.tree.verticalScrollBar().setValue(vertical_value)
        self.tree.horizontalScrollBar().setValue(horizontal_value)
        self._sync_toggle_all_button()

    def _all_items(self) -> list[QTreeWidgetItem]:
        """ツリー内の全項目を表示順で返す。"""
        result: list[QTreeWidgetItem] = []
        stack = [
            self.tree.topLevelItem(index)
            for index in reversed(range(self.tree.topLevelItemCount()))
        ]
        while stack:
            item = stack.pop()
            result.append(item)
            stack.extend(item.child(index) for index in reversed(range(item.childCount())))
        return result

    @staticmethod
    def _item_index_path(item: QTreeWidgetItem) -> tuple[int, ...] | None:
        """ツリー項目に保存した schema のインデックス経路を取得する。"""
        value = item.data(0, SCHEMA_INDEX_PATH_ROLE)
        return tuple(value) if value is not None else None

    def _remember_reorder_selection(self, *move_args) -> None:
        """Qt が行を移す前に選択ノードを記録し、移動先行への選択変更を防ぐ。"""
        item = None
        if len(move_args) >= 2:
            source_parent, source_row = move_args[:2]
            source_index = self.tree.model().index(source_row, 0, source_parent)
            item = self.tree.itemFromIndex(source_index)

        # 直接呼び出された場合やモデル索引を取得できない場合だけ現在行を使用する。
        if item is None:
            item = self.tree.currentItem()
        self._reorder_selected_key = (
            item.data(0, SCHEMA_NODE_KEY_ROLE) if item is not None else None
        )

    def _schema_location(
        self, index_path: tuple[int, ...] | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int] | None:
        """インデックス経路から現在の schema 内の項目と兄弟一覧を取得する。"""
        if not index_path:
            return None
        siblings = self.schema.setdefault('children', [])
        for depth, index in enumerate(index_path):
            if not 0 <= index < len(siblings):
                return None
            node = siblings[index]
            if depth == len(index_path) - 1:
                return node, siblings, index
            siblings = node.setdefault('children', [])
        return None

    def _expandable_items(self) -> list[QTreeWidgetItem]:
        """子要素を持つ項目だけを列挙し、ボタン状態判定を共通化する。"""
        result: list[QTreeWidgetItem] = []
        stack = [self.tree.topLevelItem(index) for index in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.childCount():
                result.append(item)
                stack.extend(item.child(index) for index in range(item.childCount()))
        return result

    def _sync_toggle_all_button(self) -> None:
        """現在の展開状態に合わせて、次に実行する操作を表示する。"""
        items = self._expandable_items()
        should_expand = bool(items) and all(not item.isExpanded() for item in items)
        self.toggle_all_button.setEnabled(bool(items))
        self.toggle_all_button.setText('⏷' if should_expand else '⏵')
        self.toggle_all_button.setToolTip('すべて展開' if should_expand else 'すべて折りたたむ')

    def _toggle_all(self) -> None:
        """展開と折りたたみを一つのボタンで切り替える。"""
        items = self._expandable_items()
        if items and all(not item.isExpanded() for item in items):
            self.tree.expandAll()
        else:
            self.tree.collapseAll()
        self._sync_toggle_all_button()

    def _queue_schema_reorder(self, *_args) -> None:
        if self._schema_reorder_pending:
            return
        self._schema_reorder_pending = True
        QTimer.singleShot(0, self._apply_schema_tree_order)

    def _apply_schema_tree_order(self) -> None:
        # 手動で先に同期した場合、予約済みのタイマーから二重適用しない。
        if not self._schema_reorder_pending:
            return
        self._schema_reorder_pending = False
        current_item = self.tree.currentItem()
        selected_key = self._reorder_selected_key
        self._reorder_selected_key = None
        selected_index_path: tuple[int, ...] | None = None

        # UserRole の辞書は Qt 側で複製され得るため、安定キーから schema 本体を引き直す。
        nodes_by_key: dict[int, dict[str, Any]] = {}

        def remember_nodes(nodes: list[dict[str, Any]]) -> None:
            for schema_node in nodes:
                nodes_by_key[id(schema_node)] = schema_node
                remember_nodes(schema_node.get('children', []))

        remember_nodes(self.schema.get('children', []))

        def collect(
            parent: QTreeWidgetItem | QTreeWidget,
            parent_path: tuple[int, ...] = (),
        ) -> list[dict[str, Any]]:
            nonlocal selected_index_path
            count = parent.childCount() if isinstance(parent, QTreeWidgetItem) else parent.topLevelItemCount()
            result: list[dict[str, Any]] = []
            for index in range(count):
                item = parent.child(index) if isinstance(parent, QTreeWidgetItem) else parent.topLevelItem(index)
                index_path = (*parent_path, index)
                item_key = item.data(0, SCHEMA_NODE_KEY_ROLE)
                if item_key == selected_key or (selected_key is None and item is current_item):
                    selected_index_path = index_path
                node = nodes_by_key.get(item_key)
                if node is None:
                    # 外部から追加された項目だけは表示データをフォールバックとして使用する。
                    node = item.data(0, Qt.ItemDataRole.UserRole)
                if node.get('type') in ('object', 'list'):
                    node['children'] = collect(item, index_path)
                else:
                    node.pop('children', None)
                result.append(node)
            return result

        self.schema['children'] = collect(self.tree)
        self.render(selected_index_path)

    def selected(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        item = self.tree.currentItem() if self.tree.selectedItems() else None
        if item is None:
            return None
        location = self._schema_location(self._item_index_path(item))
        return (location[0], location[1]) if location else None

    def _ask_field(self, node: dict[str, Any] | None = None) -> dict[str, Any] | None:
        dialog = FieldDialog(self, node)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        result = dialog.value()
        if not result['name']:
            show_warning(self, 'フィールド', 'フィールド名を入力してください。')
            return None
        return result

    def add_field(self) -> None:
        node = self._ask_field()
        if not node:
            return
        item = self.tree.currentItem() if self.tree.selectedItems() else None
        index_path = self._item_index_path(item) if item is not None else None
        location = self._schema_location(index_path)
        if location is None or index_path is None:
            siblings = self.schema.setdefault('children', [])
            insert_at = len(siblings)
            selected_path = (insert_at,)
        else:
            selected_node, siblings, selected_index = location
            if selected_node['type'] in ('object', 'list'):
                siblings = selected_node.setdefault('children', [])
                insert_at = len(siblings)
                selected_path = (*index_path, insert_at)
            else:
                insert_at = selected_index + 1
                selected_path = (*index_path[:-1], insert_at)
        siblings.insert(insert_at, node)
        self.render(selected_path)

    def add_child(self) -> None:
        selected = self.selected()
        if not selected:
            return
        parent = selected[0]
        if parent['type'] not in ('object', 'list'):
            show_information(self, 'フィールド', '子フィールドを追加できるのは object と list だけです。')
            return
        node = self._ask_field()
        if node:
            parent.setdefault('children', []).append(node)
            self.render()

    def edit_field(self) -> None:
        selected = self.selected()
        if not selected:
            return
        old = selected[0]
        new = self._ask_field(old)
        if not new:
            return
        if new['type'] in ('object', 'list'):
            new['children'] = old.get('children', [])
        old.clear()
        old.update(new)
        self.render()

    def delete_field(self) -> None:
        selected = self.selected()
        if selected and confirm_deletion(
            self, '選択したフィールドと子フィールドを削除しますか？',
        ):
            selected[1].remove(selected[0])
            self.render()

    def move(self, direction: int) -> None:
        # ドロップ直後でも、古い schema 順序を使わないよう保留中の同期を先に完了する。
        if self._schema_reorder_pending:
            self._apply_schema_tree_order()
        item = self.tree.currentItem()
        if item is None:
            return
        self._reorder_selected_key = item.data(0, SCHEMA_NODE_KEY_ROLE)
        if self.tree.moveCurrent(direction) and self._schema_reorder_pending:
            # ボタン操作は直後の処理からも新順序を参照できるよう、その場で同期を完了する。
            self._apply_schema_tree_order()

    def save(self, _checked: bool = False, *, show_message: bool = True) -> bool:
        try:
            validate_schema(self.schema)
            self.db.save_data_schema(0, self.schema)
        except ValueError as error:
            show_warning(self, '保存', str(error))
            return False
        self._saved_schema = copy.deepcopy(self.schema)
        if show_message:
            show_information(self, '保存', 'データ構造を保存しました。')
        return True

    def export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, 'JSON 导出', 'data_schema.json', 'JSON (*.json)')
        if path:
            Path(path).write_text(json.dumps(self.schema, ensure_ascii=False, indent=2), encoding='utf-8')

    def import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, 'JSON 导入', '', 'JSON (*.json)')
        if not path:
            return
        try:
            schema = json.loads(Path(path).read_text(encoding='utf-8'))
            validate_schema(schema)
            self.schema = schema
            self.render()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            show_warning(self, 'JSON 読込', str(error))

