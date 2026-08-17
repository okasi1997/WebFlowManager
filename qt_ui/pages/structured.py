from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QHeaderView, QHBoxLayout, QInputDialog, QLineEdit, QMenu, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QWidget,
)

from core.database import Database
from core.data_templates import (
    migrate_legacy_template_data, normalize_template_schema, schema_templates,
    sync_template_instance_names, validate_unique_template_names,
)
from i18n import tr
from ..table_view import (
    HierarchicalReorderTreeWidget, bind_delete_key, bind_structured_copy_paste, configure_row_move_tooltips,
    configure_table_view, set_tree_expanded, unique_copy_name,
)
from ..ui_loader import (
    confirm_deletion, confirm_import_overwrite, confirm_pending_changes, load_ui_into, localize_dialog_buttons, require,
    set_tree_toggle_icon, show_file_exported, show_file_imported, show_information, show_warning,
)


TYPES = ('text', 'number', 'boolean', 'object', 'list')
SCHEMA_INDEX_PATH_ROLE = int(Qt.ItemDataRole.UserRole) + 1
SCHEMA_NODE_KEY_ROLE = int(Qt.ItemDataRole.UserRole) + 2
STRUCTURE_KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 3


def _walk_schema_nodes(node: dict[str, Any]):
    """構造配下の全ノードを一度ずつ列挙する。"""
    for child in node.get('children', []):
        yield child
        yield from _walk_schema_nodes(child)


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
    """新規 PCL には、任意追加のテンプレートを含めない。"""
    return {
        child['name']: record_default_value(child)
        for child in schema.get('children', [])
        if not child.get('data_template', False)
    }


def record_default_value(node: dict[str, Any]) -> Any:
    """通常構造だけを補い、任意テンプレートは未追加のまま維持する。"""
    if node['type'] == 'object':
        return {
            child['name']: record_default_value(child)
            for child in node.get('children', [])
            if not child.get('data_template', False)
        }
    return default_value(node)


def validate_schema(node: Any, location: str = 'Data', *, split_ancestor: bool = False) -> None:
    if not isinstance(node, dict) or not str(node.get('name', '')).strip():
        raise ValueError(f'{location}: フィールド名を入力してください')
    if node.get('type') not in TYPES:
        raise ValueError(f'{location}: 未対応の型です: {node.get("type")}')
    split_here = bool(node.get('excel_sheet', False))
    if split_here and node.get('type') != 'object':
        raise ValueError(f'{location}{tr(": Excel の別シート出力は object だけに設定できます。")}')
    if split_here and split_ancestor:
        raise ValueError(f'{location}{tr(": 別シート出力の配下では、別のシートを設定できません。")}')
    children = node.get('children', [])
    if node['type'] in ('object', 'list'):
        if not isinstance(children, list):
            raise ValueError(f'{location}: children は配列である必要があります')
        names: set[str] = set()
        for child in children:
            name = str(child.get('name', '')) if isinstance(child, dict) else ''
            if name in names:
                raise ValueError(f'{location}: フィールド名が重複しています: {name}')
            names.add(name)
            validate_schema(
                child, f'{location}.{name}',
                split_ancestor=split_ancestor or split_here,
            )
    elif children:
        raise ValueError(f'{location}: 基本型には子フィールドを設定できません')


class FieldDialog(QDialog):
    def __init__(self, parent: QWidget, node: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        load_ui_into(self, 'field_dialog.ui')
        self.setWindowTitle(tr('フィールド編集' if node else 'フィールド追加'))
        self.name = require(self, QLineEdit, 'nameEdit')
        self.name.setText(str((node or {}).get('name', '')))
        self.kind = require(self, QComboBox, 'typeCombo')
        self.kind.addItems(TYPES)
        self.kind.setCurrentText(str((node or {}).get('type', 'text')))
        self.excel_sheet = require(self, QCheckBox, 'excelSheetCheck')
        self.excel_sheet.setChecked(bool((node or {}).get('excel_sheet', False)))
        self.excel_skip_empty = require(self, QCheckBox, 'excelSkipEmptyCheck')
        self.excel_skip_empty.setChecked(bool((node or {}).get('excel_skip_empty', True)))
        self.kind.currentTextChanged.connect(self._sync_excel_sheet_enabled)
        self.excel_sheet.toggled.connect(self._sync_excel_sheet_enabled)
        self._sync_excel_sheet_enabled()
        buttons = require(self, QDialogButtonBox, 'buttonBox')
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

    def value(self) -> dict[str, Any]:
        node: dict[str, Any] = {'name': self.name.text().strip(), 'type': self.kind.currentText()}
        if node['type'] in ('object', 'list'):
            node['children'] = []
        if node['type'] == 'object' and self.excel_sheet.isChecked():
            node['excel_sheet'] = True
            node['excel_skip_empty'] = self.excel_skip_empty.isChecked()
        return node

    def _sync_excel_sheet_enabled(self) -> None:
        """Excel の別シート出力は、値をまとめられる object だけで設定可能にする。"""
        enabled = self.kind.currentText() == 'object'
        self.excel_sheet.setEnabled(enabled)
        self.excel_skip_empty.setEnabled(enabled and self.excel_sheet.isChecked())
        if not enabled:
            self.excel_sheet.setChecked(False)


class SchemaPage(QWidget):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.schema: dict[str, Any] = {}
        self._schema_expanded_by_structure: dict[str, set[int]] = {}
        load_ui_into(self, 'schema.ui')
        manager_placeholder = require(self, QTreeWidget, 'structureManagerTree')
        manager_layout = manager_placeholder.parentWidget().layout()
        self.structure_manager = HierarchicalReorderTreeWidget(manager_placeholder.parentWidget())
        self.structure_manager.setObjectName('structureManagerTree')
        manager_layout.replaceWidget(manager_placeholder, self.structure_manager)
        manager_placeholder.setParent(None)
        manager_placeholder.deleteLater()
        self.structure_manager.setColumnCount(1)
        self.structure_manager.setHeaderHidden(True)
        configure_table_view(self.structure_manager, reorder=True)
        # 固定行だけが縞色に見えないよう、左側は文字の太さと階層だけで区別する。
        self.structure_manager.setAlternatingRowColors(False)
        self.structure_manager.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.structure_manager.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.structure_manager.setIndentation(18)
        self.structure_manager.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.structure_manager.setContainerTest(
            lambda item: item.data(0, STRUCTURE_KIND_ROLE) == 'templates'
        )
        self.structure_manager.setMoveTest(
            lambda source, parent: (
                source.data(0, STRUCTURE_KIND_ROLE) == 'template'
                and parent.data(0, STRUCTURE_KIND_ROLE) == 'templates'
            )
        )
        self.structure_manager.orderChanged.connect(self._persist_template_order)
        self.structure_manager.currentItemChanged.connect(self._select_structure)
        self.structure_manager.itemDoubleClicked.connect(self._structure_double_clicked)
        bind_structured_copy_paste(
            self.structure_manager, 'data-template-definition',
            self._copy_template_definition_payload,
            self._paste_template_definition_payload,
        )
        bind_delete_key(self.structure_manager, self.delete_template_definition)
        template_button = require(self, QPushButton, 'addTemplateDefinitionButton')
        template_menu = QMenu(template_button)
        template_menu.addAction(tr('新規'), self.add_template_definition)
        self.edit_template_definition_action = template_menu.addAction(
            tr('common.edit'), self.rename_template_definition,
        )
        self.delete_template_definition_action = template_menu.addAction(
            tr('削除'), self.delete_template_definition,
        )
        template_button.setMenu(template_menu)
        splitter = require(self, QSplitter, 'schemaSplitter')
        splitter.setHandleWidth(8)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
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
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([
            tr('フィールド名'), tr('種別'), tr('イベントリンクパス'), 'Excel 出力',
        ])
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
        for column, width in enumerate((220, 110, 340, 110)):
            self.tree.setColumnWidth(column, width)
        self._schema_reorder_pending = False
        self._reorder_selected_key: int | None = None
        self.tree.orderChanged.connect(self._queue_schema_reorder)
        self.tree.itemDoubleClicked.connect(lambda *_: self.edit_field())
        bind_structured_copy_paste(
            self.tree, 'data-schema-field', self._copy_field_payload, self._paste_field_payload,
        )
        bind_delete_key(self.tree, self.delete_field)
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
        export_button = require(self, QPushButton, 'exportSchemaButton')
        save = require(self, QPushButton, 'saveSchemaButton')
        io_menu = QMenu(export_button)
        io_menu.addAction('JSON を出力', self.export_json)
        io_menu.addAction('JSON を読み込む', self.import_json)
        export_button.setMenu(io_menu)
        save.clicked.connect(self.save)
        # 左側は操作ボタンが収まる最小幅に止め、残りをフィールド領域へ配分する。
        for card_name, toolbar_name in (
            ('structureManagerHost', 'templateManagerToolbar'),
            ('schemaTreeHost', 'fieldToolbar'),
        ):
            card = require(self, QFrame, card_name)
            toolbar = require(self, QHBoxLayout, toolbar_name)
            margins = card.layout().contentsMargins()
            card.setMinimumWidth(max(
                card.minimumWidth(), toolbar.sizeHint().width()
                + margins.left() + margins.right(),
            ))
        splitter.setSizes([
            require(self, QFrame, 'structureManagerHost').minimumWidth(),
            require(self, QFrame, 'schemaTreeHost').minimumWidth(),
        ])
        self.add_field_button = add_button
        self.edit_field_button = require(self, QPushButton, 'editFieldButton')
        self.delete_field_button = require(self, QPushButton, 'deleteFieldButton')
        self.move_field_up_button = require(self, QPushButton, 'moveFieldUpButton')
        self.move_field_down_button = require(self, QPushButton, 'moveFieldDownButton')
        self.reload()

    def reload(self) -> None:
        self.schema = normalize_template_schema(self.db.get_data_schema())
        self._saved_schema = copy.deepcopy(self.schema)
        self._render_structure_manager()
        self.render()

    def _render_structure_manager(self, selected_template_id: str | None = None) -> None:
        """共通構造とテンプレート定義を左側の管理ツリーへ表示する。"""
        current = self.structure_manager.currentItem()
        if current is not None and hasattr(self, 'tree'):
            self._capture_schema_expansion(self._structure_key(current))
        if selected_template_id is None and current is not None:
            selected_template_id = current.data(0, Qt.ItemDataRole.UserRole)
        previous_blocked = self.structure_manager.blockSignals(True)
        self.structure_manager.clear()
        common = QTreeWidgetItem([tr('共通')])
        common.setData(0, Qt.ItemDataRole.UserRole, '')
        common.setData(0, STRUCTURE_KIND_ROLE, 'common')
        templates_root = QTreeWidgetItem([tr('テンプレート')])
        templates_root.setData(0, Qt.ItemDataRole.UserRole, None)
        templates_root.setData(0, STRUCTURE_KIND_ROLE, 'templates')
        for fixed_item in (common, templates_root):
            font = fixed_item.font(0)
            font.setBold(True)
            fixed_item.setFont(0, font)
            fixed_item.setFlags(
                (fixed_item.flags() | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                & ~Qt.ItemFlag.ItemIsDragEnabled
            )
        templates_root.setFlags(templates_root.flags() | Qt.ItemFlag.ItemIsDropEnabled)
        self.structure_manager.addTopLevelItems([common, templates_root])
        selected = common
        for template in schema_templates(self.schema):
            item = QTreeWidgetItem([str(template.get('name', ''))])
            template_id = str(template.get('template_id', ''))
            item.setData(0, Qt.ItemDataRole.UserRole, template_id)
            item.setData(0, STRUCTURE_KIND_ROLE, 'template')
            item.setFlags(
                (item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
                & ~Qt.ItemFlag.ItemIsDropEnabled
            )
            templates_root.addChild(item)
            if template_id == selected_template_id:
                selected = item
        templates_root.setExpanded(True)
        self.structure_manager.setCurrentItem(selected)
        self.structure_manager.blockSignals(previous_blocked)
        self._select_structure(selected, None)
        self._sync_template_definition_buttons()

    def _persist_template_order(self) -> None:
        """左側のドラッグ結果をテンプレート定義の保存順へ反映する。"""
        root = self.structure_manager.topLevelItem(1)
        if root is None:
            return
        by_id = {
            str(template.get('template_id', '')): template
            for template in schema_templates(self.schema)
        }
        ordered = [
            by_id[str(root.child(index).data(0, Qt.ItemDataRole.UserRole))]
            for index in range(root.childCount())
            if str(root.child(index).data(0, Qt.ItemDataRole.UserRole)) in by_id
        ]
        if len(ordered) == len(by_id):
            self.schema['templates'] = ordered

    def _active_template(self) -> dict[str, Any] | None:
        """左側で選択中のテンプレート定義を返す。"""
        item = self.structure_manager.currentItem()
        template_id = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else ''
        if not template_id:
            return None
        return next(
            (template for template in schema_templates(self.schema)
             if str(template.get('template_id', '')) == str(template_id)),
            None,
        )

    def _active_root(self) -> dict[str, Any] | None:
        """右側フィールドツリーが現在編集している構造を返す。"""
        item = self.structure_manager.currentItem()
        kind = item.data(0, STRUCTURE_KIND_ROLE) if item is not None else None
        if kind == 'common':
            return self.schema
        if kind == 'template':
            return self._active_template()
        return None

    @staticmethod
    def _structure_key(item: QTreeWidgetItem | None) -> str:
        """右側の表示状態を構造ごとに保持するためのキーを返す。"""
        if item is None:
            return 'none'
        kind = str(item.data(0, STRUCTURE_KIND_ROLE) or 'none')
        return f'{kind}:{item.data(0, Qt.ItemDataRole.UserRole) or ""}'

    def _capture_schema_expansion(self, key: str) -> None:
        self._schema_expanded_by_structure[key] = {
            int(item.data(0, SCHEMA_NODE_KEY_ROLE))
            for item in self._all_items()
            if item.isExpanded() and item.data(0, SCHEMA_NODE_KEY_ROLE) is not None
        }

    def _select_structure(
            self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None,
    ) -> None:
        self._capture_schema_expansion(self._structure_key(previous))
        self.render(capture_current=False)
        self._sync_template_definition_buttons()

    def _sync_template_definition_buttons(self) -> None:
        selected = self._active_template() is not None
        self.edit_template_definition_action.setEnabled(selected)
        self.delete_template_definition_action.setEnabled(selected)
        has_structure = self._active_root() is not None
        self.add_field_button.setEnabled(has_structure)
        self.edit_field_button.setEnabled(has_structure)
        self.delete_field_button.setEnabled(has_structure)
        self.move_field_up_button.setEnabled(has_structure)
        self.move_field_down_button.setEnabled(has_structure)
        self.toggle_all_button.setEnabled(has_structure and bool(self._expandable_items()))

    def _structure_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """テンプレート定義行だけ、ダブルクリックで名称変更を開始する。"""
        if item.data(0, STRUCTURE_KIND_ROLE) != 'template':
            return
        self.structure_manager.setCurrentItem(item)
        self.rename_template_definition()

    def _copy_template_definition_payload(self) -> dict[str, Any] | None:
        """選択中のテンプレート定義を Ctrl+C の対象として返す。"""
        template = self._active_template()
        return copy.deepcopy(template) if template is not None else None

    def _paste_template_definition_payload(self, source: dict[str, Any]) -> None:
        """Ctrl+V でテンプレート定義を選択行の直後へ追加する。"""
        templates = self.schema.setdefault('templates', [])
        copied = copy.deepcopy(source)
        copied['template_id'] = uuid.uuid4().hex
        copied['name'] = unique_copy_name(
            str(copied.get('name', '')),
            (str(item.get('name', '')) for item in templates),
        )
        selected = self._active_template()
        insert_at = templates.index(selected) + 1 if selected in templates else len(templates)
        templates.insert(insert_at, copied)
        self._render_structure_manager(str(copied['template_id']))
        self.render()

    def add_template_definition(self) -> None:
        """空のテンプレート定義を追加し、右側でフィールド編集を開始できるようにする。"""
        name, ok = QInputDialog.getText(self, tr('テンプレート追加'), tr('テンプレート名'))
        name = name.strip()
        if not ok or not name:
            return
        if any(str(item.get('name', '')).strip() == name for item in schema_templates(self.schema)):
            show_warning(self, tr('テンプレート追加'), tr('同じ名前のテンプレートが存在します。'))
            return
        template = {'template_id': uuid.uuid4().hex, 'name': name, 'type': 'object', 'children': []}
        self.schema.setdefault('templates', []).append(template)
        self._render_structure_manager(template['template_id'])
        self.render()

    def rename_template_definition(self) -> None:
        template = self._active_template()
        if template is None:
            return
        name, ok = QInputDialog.getText(
            self, tr('テンプレート名変更'), tr('テンプレート名'),
            text=str(template.get('name', '')),
        )
        name = name.strip()
        if not ok or not name or name == template.get('name'):
            return
        if any(item is not template and str(item.get('name', '')).strip() == name
               for item in schema_templates(self.schema)):
            show_warning(self, tr('テンプレート名変更'), tr('同じ名前のテンプレートが存在します。'))
            return
        template['name'] = name
        self._render_structure_manager(str(template['template_id']))

    def delete_template_definition(self) -> None:
        selected_ids = {
            str(item.data(0, Qt.ItemDataRole.UserRole))
            for item in self.structure_manager.selectedItems()
            if item.data(0, STRUCTURE_KIND_ROLE) == 'template'
        }
        templates = [
            template for template in schema_templates(self.schema)
            if str(template.get('template_id', '')) in selected_ids
        ]
        if not templates:
            return
        used_ids = {str(template.get('template_id', '')) for template in templates}
        used_count = sum(
            1 for record in self.db.list_data_records()
            for instance in record.get('data', {}).get('_template_instances', [])
            if isinstance(instance, dict) and str(instance.get('template_id', '')) in used_ids
        )
        if used_count:
            show_warning(
                self, tr('テンプレート削除'),
                f'{tr("このテンプレートは PCL で使用されています: ")}{used_count}',
            )
            return
        if not confirm_deletion(
            self,
            f'{templates[0]["name"]}{tr(" を削除しますか？")}' if len(templates) == 1
            else f'選択した {len(templates)} 件のテンプレートを削除しますか？',
        ):
            return
        self.schema['templates'] = [
            template for template in schema_templates(self.schema) if template not in templates
        ]
        self._render_structure_manager()
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

    def render(
            self, selected_index_path: tuple[int, ...] | None = None,
            *, capture_current: bool = True,
    ) -> None:
        # Qt の UserRole に格納した dict は QVariant 変換時に複製される場合があるため、
        # 選択位置の特定には schema 内の安定したインデックス経路だけを使用する。
        previous_item = self.tree.currentItem()
        if capture_current and selected_index_path is None and previous_item is not None:
            selected_index_path = self._item_index_path(previous_item)
        vertical_value = self.tree.verticalScrollBar().value()
        horizontal_value = self.tree.horizontalScrollBar().value()
        structure_key = self._structure_key(self.structure_manager.currentItem())
        if capture_current and self.tree.topLevelItemCount():
            self._capture_schema_expansion(structure_key)
        expanded_keys = self._schema_expanded_by_structure.get(structure_key)
        self.tree.clear()
        selected_item: QTreeWidgetItem | None = None

        def add(
            parent: QTreeWidgetItem | QTreeWidget,
            node: dict[str, Any],
            path: str,
            index_path: tuple[int, ...],
        ) -> None:
            nonlocal selected_item
            item = QTreeWidgetItem([
                node['name'], node['type'], path,
                (
                    tr('別シート（空時省略）')
                    if node.get('excel_sheet', False) and node.get('excel_skip_empty', True)
                    else tr('別シート') if node.get('excel_sheet', False) else '-'
                ),
            ])
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
        active_root = self._active_root()
        if active_root is not None:
            for child_index, child in enumerate(active_root.get('children', [])):
                add(self.tree, child, child['name'], (child_index,))

        if expanded_keys is not None:
            for item in self._all_items():
                item.setExpanded(item.data(0, SCHEMA_NODE_KEY_ROLE) in expanded_keys)
        elif active_root is not None:
            self.tree.expandAll()
        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
        # setCurrentItem() による自動スクロールを打ち消し、操作前の表示位置を維持する。
        self.tree.verticalScrollBar().setValue(vertical_value)
        self.tree.horizontalScrollBar().setValue(horizontal_value)
        self._sync_toggle_all_button()
        self._sync_template_definition_buttons()

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
        active_root = self._active_root()
        if not index_path or active_root is None:
            return None
        siblings = active_root.setdefault('children', [])
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
        set_tree_toggle_icon(self.toggle_all_button, should_expand)

    def _toggle_all(self) -> None:
        """展開と折りたたみを一つのボタンで切り替える。"""
        items = self._expandable_items()
        if items and all(not item.isExpanded() for item in items):
            set_tree_expanded(self.tree, True)
        else:
            set_tree_expanded(self.tree, False)
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

        active_root = self._active_root()
        remember_nodes(active_root.get('children', []))

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

        active_root['children'] = collect(self.tree)
        self.render(selected_index_path)

    def selected(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        item = self.tree.currentItem() if self.tree.selectedItems() else None
        if item is None:
            return None
        location = self._schema_location(self._item_index_path(item))
        return (location[0], location[1]) if location else None

    def _copy_field_payload(self) -> dict[str, Any] | None:
        """選択フィールドと配下構造をまとめてコピーする。"""
        if self._schema_reorder_pending:
            self._apply_schema_tree_order()
        selected = self.selected()
        return selected[0] if selected else None

    def _paste_field_payload(self, source: dict[str, Any]) -> None:
        """選択行と同じ階層の直後へ貼り付ける。"""
        active_root = self._active_root()
        if active_root is None:
            return
        if self._schema_reorder_pending:
            self._apply_schema_tree_order()
        item = self.tree.currentItem() if self.tree.selectedItems() else None
        index_path = self._item_index_path(item) if item is not None else None
        location = self._schema_location(index_path)
        if location is None or index_path is None:
            siblings = active_root.setdefault('children', [])
            insert_at = len(siblings)
            selected_path = (insert_at,)
        else:
            _selected_node, siblings, selected_index = location
            insert_at = selected_index + 1
            selected_path = (*index_path[:-1], insert_at)
        node = copy.deepcopy(source)
        node['name'] = unique_copy_name(
            str(node['name']), (sibling['name'] for sibling in siblings),
        )
        siblings.insert(insert_at, node)
        self.render(selected_path)

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
        active_root = self._active_root()
        if active_root is None:
            return
        node = self._ask_field()
        if not node:
            return
        item = self.tree.currentItem() if self.tree.selectedItems() else None
        index_path = self._item_index_path(item) if item is not None else None
        location = self._schema_location(index_path)
        if location is None or index_path is None:
            siblings = active_root.setdefault('children', [])
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
        paths = sorted({
            self._item_index_path(item) for item in self.tree.selectedItems()
            if self._item_index_path(item) is not None
        })
        # 親が選択済みの場合は、その配下を重複して削除しない。
        paths = [path for path in paths if not any(
            path[:len(other)] == other for other in paths if len(other) < len(path)
        )]
        if paths and confirm_deletion(
            self, '選択したフィールドと子フィールドを削除しますか？',
        ):
            for path in sorted(paths, reverse=True):
                location = self._schema_location(path)
                if location is not None:
                    location[1].pop(location[2])
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
            validate_unique_template_names(self.schema)
            for template in schema_templates(self.schema):
                name = str(template.get('name', '')).strip()
                validate_schema(template, f'{tr("テンプレート")}.{name}')
            old_schema = self.db.get_data_schema()
            self.db.save_data_schema(0, self.schema)
            # 名称パスで実行できるよう、既存の全実体にも定義名を同期する。
            for record in self.db.list_data_records():
                data = record.get('data', {})
                if sync_template_instance_names(data, self.schema):
                    self.db.update_data_record(record['id'], record['name'], data)
            # 旧追加テンプレートを初めて保存する場合だけ、既存 PCL の値も同時に移行する。
            if any(node.get('data_template', False) for node in _walk_schema_nodes(old_schema)):
                for record in self.db.list_data_records():
                    migrated = migrate_legacy_template_data(old_schema, record.get('data', {}))
                    if migrated != record.get('data', {}):
                        self.db.update_data_record(record['id'], record['name'], migrated)
        except ValueError as error:
            show_warning(self, '保存', str(error))
            return False
        self._saved_schema = copy.deepcopy(self.schema)
        if show_message:
            show_information(self, '保存', 'データ構造を保存しました。')
        return True

    def export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr('JSON 出力'), 'data_schema.json', 'JSON (*.json)')
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self.schema, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as error:
            show_warning(self, 'JSON 出力', str(error))
            return
        show_file_exported(self, path)

    def import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr('JSON 読込'), '', 'JSON (*.json)')
        if not path:
            return
        if not confirm_import_overwrite(self, bool(self.schema.get('children')), 'データ構造'):
            return
        try:
            schema = normalize_template_schema(json.loads(Path(path).read_text(encoding='utf-8')))
            validate_schema(schema)
            validate_unique_template_names(schema)
            for template in schema_templates(schema):
                validate_schema(template, f'Template.{template.get("name", "")}')
            self.schema = schema
            self._render_structure_manager()
            self.render()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            show_warning(self, 'JSON 読込', str(error))
            return
        show_file_imported(self, path)

