from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from pathlib import Path
import json
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFrame, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QStyle, QTableWidget,
    QTabWidget, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QEvent, QModelIndex, QPoint, QTimer, Qt
from PySide6.QtTest import QTest

from core.database import Database
from browser.element_picker import ElementPicker
from i18n import set_language, tr
from qt_ui.main_window import MainWindow
from qt_ui.application import _ComboBoxWheelBlocker
from qt_ui.pages.flow_design import (
    DataPathPickerDialog, EventEditorDialog, EventGroupEditorDialog, FlowEditorDialog,
    GuardConditionEditorDialog, GuardRuleEditorDialog,
)
from qt_ui.pages.structured import FieldDialog
from qt_ui.pages.data import RecordMetadataDialog
from qt_ui.pages.execution import STATUS_LABELS
from qt_ui.ui_loader import ConfirmationDialog, DeletionConfirmDialog
from qt_ui.table_view import TREE_LEVEL_INDENT, configure_table_view


class QtShellTests(unittest.TestCase):
    def test_tree_forms_do_not_expand_rows_on_double_click(self) -> None:
        """ダブルクリック操作と行の展開を競合させない。"""
        form_dir = Path(__file__).resolve().parents[1] / 'qt_ui' / 'forms'
        tree_count = 0
        for path in form_dir.glob('*.ui'):
            root = ET.parse(path).getroot()
            for widget in root.findall(".//widget[@class='QTreeWidget']"):
                tree_count += 1
                value = widget.find("property[@name='expandsOnDoubleClick']/bool")
                self.assertIsNotNone(value, f'{path.name}:{widget.get("name")}')
                self.assertEqual(value.text, 'false', f'{path.name}:{widget.get("name")}')
        self.assertGreater(tree_count, 0)

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        (self.project_dir / 'assets').mkdir()
        self.db = Database(self.project_dir / 'flows.db')
        self.window = MainWindow(self.project_dir, self.db)

    def tearDown(self) -> None:
        self.window._force_close = True
        self.window.close()
        self.db.close()
        self.temp_dir.cleanup()

    def test_navigation_has_six_qt_pages(self) -> None:
        self.assertEqual(self.window.stack.count(), 6)
        self.assertEqual(self.window.size().width(), 1180)
        sidebar = self.window.findChild(QFrame, 'sidebar')
        self.assertEqual(sidebar.width(), 180)
        self.assertEqual(sidebar.minimumWidth(), sidebar.maximumWidth())
        self.assertTrue(all(button.property('nav') for button in self.window.nav_buttons.values()))

    def test_main_editing_page_toolbars_share_bottom_edge(self) -> None:
        """主要編集ページ間で下部ツールバーの基準位置を共通化する。"""
        button_names = {
            'design': 'newWorkflowButton',
            'data': 'addRecordButton',
            'schema': 'addFieldButton',
        }
        bottom_edges = []
        self.window.show()
        for page_name, button_name in button_names.items():
            self.window.show_page(page_name)
            self.app.processEvents()
            button = self.window.pages[page_name].findChild(QPushButton, button_name)
            bottom_edges.append(button.mapTo(self.window, button.rect().bottomLeft()).y())
        # テスト環境はテーマ未適用のため、Qt 標準スタイルの余白差だけ許容する。
        self.assertLessEqual(max(bottom_edges) - min(bottom_edges), 2, bottom_edges)
        schema_layout = self.window.pages['schema'].findChild(QVBoxLayout, 'schemaCardLayout')
        margins = schema_layout.contentsMargins()
        self.assertEqual((margins.left(), margins.right(), margins.bottom()), (9, 9, 9))

    def test_data_file_operations_show_completion_messages(self) -> None:
        """データ関連の入出力が成功した場合だけ、共通の完了通知を表示する。"""
        data_page = self.window.pages['data']
        schema_page = self.window.pages['schema']
        json_path = self.project_dir / 'records.json'
        excel_path = self.project_dir / 'records.xlsx'
        schema_path = self.project_dir / 'schema.json'

        with (
            patch('qt_ui.pages.data.QFileDialog.getSaveFileName', return_value=(str(json_path), '')),
            patch('qt_ui.pages.data.show_file_exported') as notified,
        ):
            data_page.export_json()
            notified.assert_called_once_with(data_page, str(json_path))

        json_path.write_text('[]', encoding='utf-8')
        with (
            patch('qt_ui.pages.data.QFileDialog.getOpenFileName', return_value=(str(json_path), '')),
            patch('qt_ui.pages.data.show_file_imported') as notified,
        ):
            data_page.import_json()
            notified.assert_called_once_with(data_page, str(json_path))

        with (
            patch('qt_ui.pages.data.QFileDialog.getSaveFileName', return_value=(str(excel_path), '')),
            patch('qt_ui.pages.data.write_records_excel'),
            patch('qt_ui.pages.data.show_file_exported') as notified,
        ):
            data_page.export_excel()
            notified.assert_called_once_with(data_page, str(excel_path))

        with (
            patch('qt_ui.pages.data.QFileDialog.getOpenFileName', return_value=(str(excel_path), '')),
            patch('qt_ui.pages.data.read_records_excel', return_value=[]),
            patch('qt_ui.pages.data.show_file_imported') as notified,
        ):
            data_page.import_excel()
            notified.assert_called_once_with(data_page, str(excel_path))

        with (
            patch('qt_ui.pages.structured.QFileDialog.getSaveFileName', return_value=(str(schema_path), '')),
            patch('qt_ui.pages.structured.show_file_exported') as notified,
        ):
            schema_page.export_json()
            notified.assert_called_once_with(schema_page, str(schema_path))

        schema_path.write_text(json.dumps(schema_page.schema, ensure_ascii=False), encoding='utf-8')
        with (
            patch('qt_ui.pages.structured.QFileDialog.getOpenFileName', return_value=(str(schema_path), '')),
            patch('qt_ui.pages.structured.show_file_imported') as notified,
        ):
            schema_page.import_json()
            notified.assert_called_once_with(schema_page, str(schema_path))

        design_page = self.window.pages['design']
        workflow_path = self.project_dir / 'workflows.json'
        with (
            patch('qt_ui.pages.flow_design.QFileDialog.getSaveFileName', return_value=(str(workflow_path), '')),
            patch.object(self.db, 'export_workflow_collection'),
            patch('qt_ui.pages.flow_design.show_file_exported') as notified,
        ):
            design_page.export_json()
            notified.assert_called_once_with(design_page, str(workflow_path))

        with (
            patch('qt_ui.pages.flow_design.QFileDialog.getOpenFileName', return_value=(str(workflow_path), '')),
            patch('qt_ui.pages.flow_design.confirm_action', return_value=True),
            patch.object(self.db, 'import_workflow_collection'),
            patch.object(design_page, 'reload'),
            patch('qt_ui.pages.flow_design.show_file_imported') as notified,
        ):
            design_page.import_json()
            notified.assert_called_once_with(design_page, str(workflow_path))

    def test_all_dialogs_are_locked_without_locking_main_window(self) -> None:
        blocker = _ComboBoxWheelBlocker()
        dialog = QDialog(self.window)
        dialog.resize(640, 420)
        blocker.eventFilter(dialog, QEvent(QEvent.Type.Show))
        self.assertEqual(dialog.minimumSize(), dialog.maximumSize())
        self.assertEqual((dialog.width(), dialog.height()), (640, 420))
        self.assertNotEqual(self.window.minimumSize(), self.window.maximumSize())
        self.assertEqual(self.window.size().height(), 720)
        self.assertEqual(self.window.minimumSize(), self.window.size())
        self.window.show_page('settings')
        self.assertIs(self.window.stack.currentWidget(), self.window.pages['settings'])

    def test_flow_page_loads_database_rows(self) -> None:
        workflow_id = self.db.add_workflow('Qt flow')
        self.window.pages['design'].reload(workflow_id)
        self.assertEqual(self.window.pages['design'].workflow_table.rowCount(), 1)
        self.assertEqual(self.window.pages['design'].workflow_table.item(0, 1).text(), 'Qt flow')

    def test_all_designer_forms_are_present_and_valid(self) -> None:
        forms = Path(__file__).parents[1] / 'qt_ui' / 'forms'
        expected = {
            'main_window.ui', 'flow_design.ui', 'flow_editor.ui', 'event_editor.ui', 'guard_dialog.ui',
            'guard_condition.ui', 'guard_rule.ui', 'data_path_picker.ui',
            'event_group.ui',
            'auth.ui', 'settings.ui', 'schema.ui', 'field_dialog.ui', 'data.ui',
            'record_dialog.ui', 'execution.ui', 'confirmation.ui',
        }
        self.assertEqual({path.name for path in forms.glob('*.ui')}, expected)
        for path in forms.glob('*.ui'):
            root = ET.parse(path).getroot()
            self.assertEqual(root.tag, 'ui')
            dialog = root.find('.//widget[@class="QDialog"]')
            if dialog is not None:
                repeated_names = {
                    widget.get('name') for widget in dialog.findall('.//widget')
                    if widget.get('name') in {'titleLabel', 'hintLabel'}
                }
                self.assertEqual(repeated_names, set(), f'{path.name} repeats its window title or hint')

    def test_designer_object_names_are_bound_to_controllers(self) -> None:
        self.assertEqual(self.window.stack.objectName(), 'contentStack')
        self.assertEqual(self.window.pages['design'].workflow_table.objectName(), 'workflowTable')
        self.assertEqual(self.window.pages['schema'].tree.objectName(), 'schemaTree')
        self.assertFalse(self.window.pages['schema'].tree.expandsOnDoubleClick())
        self.assertEqual(self.window.pages['data'].tree.objectName(), 'recordTree')
        self.assertEqual(self.window.pages['execution'].records.objectName(), 'executionTree')

    def test_auth_page_keeps_active_profile_status_column(self) -> None:
        page = self.window.pages['auth']
        self.window.show()
        self.app.processEvents()
        page._sync_save_button_width()
        self.assertEqual(page.save_state_button.width(), page.create_profile_button.width())
        self.assertEqual(page.profiles.columnCount(), 2)
        self.assertEqual(page.profiles.headerItem().text(1), '状態')
        self.assertEqual(page.profiles.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertEqual(page.profiles.header().sectionResizeMode(0), QHeaderView.ResizeMode.Stretch)
        self.assertEqual(page.profiles.header().sectionResizeMode(1), QHeaderView.ResizeMode.Fixed)
        self.assertEqual(page.profiles.columnWidth(1), 82)
        self.assertLessEqual(
            page.profiles.columnWidth(0) + page.profiles.columnWidth(1),
            page.profiles.viewport().width() + 1,
        )
        page.profiles.setFixedWidth(220)
        self.app.processEvents()
        self.assertEqual(page.profiles.columnWidth(1), 82)
        self.assertLessEqual(
            page.profiles.columnWidth(0) + page.profiles.columnWidth(1),
            page.profiles.viewport().width() + 1,
        )
        current = page.profiles.currentItem()
        self.assertIsNotNone(current)
        self.assertEqual(current.text(1), '使用中')
        self.assertEqual(page.selected.text(), current.text(0))

    def test_flow_tables_share_row_and_column_interaction_rules(self) -> None:
        page = self.window.pages['design']
        self.window.show_page('design')
        self.window.show()
        self.app.processEvents()
        self.assertEqual(page.workflow_table.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.assertEqual(page.event_tree.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.assertEqual(page.findChild(QPushButton, 'workflowJsonButton').text(), 'Json 入出力')
        self.assertEqual(page.findChild(QPushButton, 'addEventButton').text(), '追加')
        move_up = page.findChild(QPushButton, 'moveUpButton')
        move_down = page.findChild(QPushButton, 'moveDownButton')
        group_toggle = page.findChild(QPushButton, 'groupToggleButton')
        self.assertIsNone(page.findChild(QPushButton, 'collapseAllButton'))
        self.assertEqual(move_up.toolTip(), '選択したイベントを上へ移動')
        self.assertEqual(move_down.toolTip(), '選択したイベントを下へ移動')
        self.assertEqual(group_toggle.text(), '')
        self.assertFalse(group_toggle.icon().isNull())
        self.assertIn(group_toggle.toolTip(), ('すべて展開', 'すべて折りたたむ'))
        self.assertEqual(page.workflow_table.editTriggers(), QAbstractItemView.EditTrigger.NoEditTriggers)
        self.assertEqual(page.event_tree.editTriggers(), QAbstractItemView.EditTrigger.NoEditTriggers)
        self.assertIsNotNone(page.findChild(QPushButton, 'editWorkflowButton'))
        self.assertIsNone(page.findChild(QPushButton, 'workflowMoreButton'))
        self.assertEqual(page.workflow_table.selectionBehavior(), QAbstractItemView.SelectionBehavior.SelectRows)
        self.assertFalse(page.workflow_table.showGrid())
        self.assertEqual(page.workflow_table.dragDropMode(), QAbstractItemView.DragDropMode.DragDrop)
        self.assertFalse(page.workflow_table.dragDropOverwriteMode())
        self.assertEqual(page.workflow_table.defaultDropAction(), Qt.DropAction.CopyAction)
        self.assertEqual(page.event_tree.dragDropMode(), QAbstractItemView.DragDropMode.DragDrop)
        self.assertEqual(page.event_tree.defaultDropAction(), Qt.DropAction.CopyAction)
        header = page.event_tree.header()
        self.assertTrue(page.workflow_table.horizontalHeader().sectionsMovable())
        self.assertTrue(header.sectionsMovable())
        workflow_header = page.workflow_table.horizontalHeader()
        self.assertEqual(
            [workflow_header.logicalIndex(visual) for visual in range(5)],
            [0, 1, 2, 4, 3],
        )
        self.assertEqual(
            [header.logicalIndex(visual) for visual in range(6)],
            [0, 5, 4, 3, 1, 2],
        )
        self.assertGreater(page.workflow_table.columnWidth(1), max(
            page.workflow_table.columnWidth(column) for column in (0, 2, 3, 4)
        ))
        self.assertGreater(page.event_tree.columnWidth(0), max(
            page.event_tree.columnWidth(column) for column in (1, 2, 3, 4, 5)
        ))
        self.assertTrue(all(
            header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
            for column in range(page.event_tree.columnCount())
        ))
        self.assertGreater(page.event_tree.columnWidth(0), page.event_tree.columnWidth(1))
        self.assertEqual(page.event_tree.indentation(), TREE_LEVEL_INDENT)
        self.assertEqual(self.window.pages['schema'].tree.indentation(), TREE_LEVEL_INDENT)
        self.assertEqual(self.window.pages['data'].values.indentation(), TREE_LEVEL_INDENT)
        self.assertEqual(
            self.window.pages['schema'].tree.dragDropMode(),
            QAbstractItemView.DragDropMode.DragDrop,
        )
        self.assertEqual(
            self.window.pages['schema'].tree.defaultDropAction(),
            Qt.DropAction.CopyAction,
        )
        schema_page = self.window.pages['schema']
        self.assertEqual(
            schema_page.findChild(QPushButton, 'moveFieldUpButton').toolTip(),
            '選択したフィールドを上へ移動',
        )
        self.assertEqual(
            schema_page.findChild(QPushButton, 'moveFieldDownButton').toolTip(),
            '選択したフィールドを下へ移動',
        )

    def test_workflow_double_click_dispatches_by_column(self) -> None:
        first = self.db.add_workflow('first')
        second = self.db.add_workflow('second')
        page = self.window.pages['design']
        page.reload(first)
        enabled_before = bool(next(row for row in self.db.list_workflows() if row['id'] == first)['enabled'])
        page._workflow_double_clicked(page.workflow_table.item(0, 2))
        self.assertNotEqual(bool(next(row for row in self.db.list_workflows() if row['id'] == first)['enabled']), enabled_before)
        page._workflow_double_clicked(page.workflow_table.item(0, 3))
        self.assertTrue(bool(next(row for row in self.db.list_workflows() if row['id'] == first)['pcl_loop_start']))
        calls: list[str] = []
        page.edit_guard_summary = lambda: calls.append('guard')
        page.edit_workflow = lambda: calls.append('edit')
        page._workflow_double_clicked(page.workflow_table.item(0, 4))
        page._workflow_double_clicked(page.workflow_table.item(0, 0))
        self.assertEqual(calls, ['guard', 'edit'])
        page._reorder_workflow_rows(0, 1)
        self.assertEqual([row['id'] for row in self.db.list_workflows()], [second, first])
        self.assertEqual(page.workflow_table.item(0, 1).text(), 'second')
        self.assertEqual(page.workflow_table.item(1, 1).text(), 'first')

    def test_event_double_click_dispatches_by_column_without_expanding_groups(self) -> None:
        page = self.window.pages['design']
        calls: list[str] = []
        page.toggle_event_enabled = lambda: calls.append('enabled')
        page.edit_event_guard = lambda: calls.append('guard')
        page.edit_event = lambda: calls.append('edit')
        page._event_double_clicked(None, 4)
        page._event_double_clicked(None, 3)
        page._event_double_clicked(None, 0)
        page._event_double_clicked(None, 2)
        self.assertEqual(calls, ['enabled', 'guard', 'edit', 'edit'])
        self.assertFalse(page.event_tree.expandsOnDoubleClick())

    def test_event_reload_preserves_collapse_and_scroll_state(self) -> None:
        page = self.window.pages['design']
        workflow_id = self.db.add_workflow('state preservation')
        base = {
            'selector_type': 'none', 'selector': '', 'value': '',
            'timeout_ms': 10000, 'enabled': True, 'continue_on_error': False,
            'guard': {'logic': 'all', 'rules': []},
        }
        start_id = self.db.add_event(workflow_id, base | {'name': 'group', 'action': 'group_start'})
        child_id = self.db.add_event(workflow_id, base | {'name': 'child', 'action': 'click'})
        self.db.add_event(workflow_id, base | {'name': 'group', 'action': 'group_end'})
        for index in range(24):
            self.db.add_event(workflow_id, base | {'name': f'event {index}', 'action': 'click'})
        page.current_workflow_id = workflow_id
        page.load_events(child_id)
        group = page.event_tree.topLevelItem(0)
        self.assertEqual(group.data(0, Qt.ItemDataRole.UserRole), start_id)
        self.assertEqual(group.text(1), 'グループ')
        self.assertNotEqual(group.text(1), 'group_start')
        group.setExpanded(False)
        self.window.show_page('design')
        self.window.show()
        self.app.processEvents()
        scrollbar = page.event_tree.verticalScrollBar()
        scrollbar.setValue(min(80, scrollbar.maximum()))
        scroll_before = scrollbar.value()

        page.load_events(child_id)

        self.assertFalse(page.event_tree.topLevelItem(0).isExpanded())
        self.assertEqual(scrollbar.value(), scroll_before)

    def test_flow_editor_uses_localized_structured_guard_dialog(self) -> None:
        schema = {'type': 'object', 'children': [{'name': 'case_no', 'type': 'text'}]}
        guard = {'logic': 'all', 'rules': [{'path': 'case_no', 'operator': 'eq', 'value': 'A001'}]}
        editor = FlowEditorDialog(self.window, {'name': '登録', 'guard': guard}, schema)
        self.assertEqual(editor.windowTitle(), '業務フロー編集')
        self.assertEqual(editor.minimumSize(), editor.maximumSize())
        self.assertIsNone(editor.findChild(QLabel, 'titleLabel'))
        self.assertIsNone(editor.findChild(QLabel, 'hintLabel'))
        self.assertNotIn('{', editor.guard_summary.text())
        condition = GuardConditionEditorDialog(editor, guard, schema)
        self.assertEqual(condition.windowTitle(), '実行条件設定')
        self.assertIsNone(condition.findChild(QLabel, 'titleLabel'))
        self.assertEqual(condition.tree.topLevelItemCount(), 1)
        self.assertEqual(condition.result_data(), guard)
        footer_buttons = [
            condition.findChild(QPushButton, name)
            for name in ('addRuleButton', 'editRuleButton', 'deleteRuleButton')
        ]
        button_box = condition.findChild(QDialogButtonBox, 'buttonBox')
        footer_buttons.extend([
            button_box.button(QDialogButtonBox.StandardButton.Save),
            button_box.button(QDialogButtonBox.StandardButton.Cancel),
        ])
        self.assertEqual(len({button.width() for button in footer_buttons}), 1)
        condition.close()
        editor.close()

    def test_event_editor_restores_structured_condition_and_data_picker(self) -> None:
        design_page = self.window.pages['design']
        editor = EventEditorDialog(design_page)
        self.assertIsNone(editor.parent())
        self.assertIs(editor._service_host, design_page)
        self.assertTrue(editor.windowFlags() & Qt.WindowType.Window)
        self.assertEqual(editor.windowTitle(), 'イベント追加')
        self.assertEqual(editor.action.currentData(), '')
        self.assertTrue(editor.pick_button.isEnabled())
        editor.action.setCurrentIndex(editor.action.findData('goto'))
        self.assertFalse(editor.pick_button.isEnabled())
        editor.action.setCurrentIndex(editor.action.findData('click'))
        self.assertTrue(editor.pick_button.isEnabled())
        self.assertEqual(editor.timeout.suffix(), ' ms')
        self.assertEqual(editor.retry_interval.suffix(), ' ms')
        self.assertTrue(editor.guard_summary.isReadOnly())
        self.assertIsNone(editor.findChild(QLineEdit, 'guardEdit'))
        self.assertIsInstance(editor.data_path, QComboBox)
        left_tabs = editor.findChild(QTabWidget, 'eventEditorTabs')
        self.assertIsNotNone(left_tabs)
        self.assertFalse(left_tabs.tabBar().expanding())
        self.assertEqual(left_tabs.width(), 473)
        self.assertEqual([left_tabs.tabText(index) for index in range(left_tabs.count())], ['イベント・検出', '実行制御'])
        buttons = [button.text() for button in editor.findChildren(QPushButton)]
        self.assertIn('条件を設定', buttons)
        picker_tooltips = {
            'valueReferenceButton': 'データ参照',
            'pathButton': 'データ構造から選択',
            'selectorReferenceButton': 'データ参照',
            'fallbackReferenceButton': 'データ参照',
            'failureReferenceButton': 'データ参照',
        }
        for name, tooltip in picker_tooltips.items():
            picker_button = editor.findChild(QPushButton, name)
            self.assertEqual(picker_button.text(), '')
            self.assertTrue(picker_button.property('dataReferenceButton'))
            self.assertFalse(picker_button.icon().isNull())
            self.assertEqual(picker_button.toolTip(), tooltip)
            self.assertEqual(picker_button.width(), 42)
        self.assertIsNone(editor.findChild(QLabel, 'titleLabel'))
        self.assertEqual((editor.width(), editor.height()), (1000, 620))
        self.assertEqual(editor.minimumSize(), editor.maximumSize())
        for name in ('pageCard', 'resultCard'):
            self.assertTrue(editor.findChild(QFrame, name).property('card'))
        for name in ('eventCard', 'locatorCard', 'executionCard'):
            card = editor.findChild(QFrame, name)
            self.assertFalse(card.property('card'))
            self.assertTrue(card.property('embeddedCard'))
        for name in ('pickButton', 'closeDebugButton', 'tryEventButton', 'executeUntilButton'):
            self.assertIsNotNone(editor.findChild(QPushButton, name))
        self.assertIsNone(editor.findChild(QPushButton, 'testButton'))
        self.assertIsNone(editor.findChild(QLabel, 'statusLabel'))
        self.assertEqual(
            [editor.failure_action.itemData(index) for index in range(editor.failure_action.count())],
            ['stop', 'continue', 'refresh', 'goto'],
        )
        editor.action.setCurrentIndex(editor.action.findData('wait'))
        self.assertTrue(editor.wait_condition.isVisibleTo(editor))
        self.assertTrue(editor.selector.isVisibleTo(editor))
        self.assertLess(editor.selector_type.mapTo(editor, QPoint()).y(), editor.selector.mapTo(editor, QPoint()).y())
        self.assertLess(
            editor.fallback_selector_type.mapTo(editor, QPoint()).y(),
            editor.fallback_selector.mapTo(editor, QPoint()).y(),
        )
        self.assertFalse(editor.fallback_selector.isEnabled())
        editor.show()
        self.app.processEvents()
        for action_key in ('wait', 'get_text', 'pause'):
            editor.action.setCurrentIndex(editor.action.findData(action_key))
            self.app.processEvents()
            value_widget = editor.wait_condition if action_key == 'wait' else editor.value
            action_right = editor.action.mapTo(editor, editor.action.rect().topRight()).x()
            value_right = value_widget.mapTo(editor, value_widget.rect().topRight()).x()
            self.assertLessEqual(abs(action_right - value_right), 2, action_key)
        editor.action.setCurrentIndex(editor.action.findData('wait'))
        self.app.processEvents()
        footer = editor.findChild(QWidget, 'footerHost')
        button_box = editor.findChild(QDialogButtonBox, 'buttonBox')
        footer_geometry = footer.geometry()
        button_box_geometry = button_box.geometry()
        self.assertEqual(footer.height(), 42)
        left_tabs.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(footer.geometry(), footer_geometry)
        self.assertEqual(button_box.geometry(), button_box_geometry)
        left_tabs.setCurrentIndex(0)
        self.app.processEvents()
        page_card = editor.findChild(QFrame, 'pageCard')
        self.assertEqual(left_tabs.width(), page_card.width())
        self.assertEqual(
            left_tabs.currentWidget().mapTo(editor, QPoint()).y(),
            page_card.mapTo(editor, QPoint()).y(),
        )
        self.assertEqual(
            editor.selector_reference_button.width(),
            editor.fallback_reference_button.width(),
        )
        self.assertEqual(editor.selector_reference_button.width(), 42)
        self.assertEqual(editor.value_data_reference_button.width(), 42)
        self.assertEqual(editor.value_action_button.width(), 42)
        self.assertTrue(editor.value_action_button.property('dataReferenceButton'))
        editor.iframe_path.setText('[]')
        self.assertEqual(editor.result_data()['iframe_path'], '')
        self.assertGreater(editor.selector.width(), editor.selector_reference_button.width())
        self.assertGreaterEqual(editor.selector.width(), 190)
        self.assertEqual(
            editor.selector_type.mapTo(editor, QPoint()).x(),
            editor.selector.mapTo(editor, QPoint()).x(),
        )
        self.assertEqual(
            editor.selector_type.mapTo(editor, QPoint()).x(),
            editor.iframe_path.mapTo(editor, QPoint()).x(),
        )
        aligned_right_edges = [
            widget.mapTo(editor, widget.rect().topRight()).x()
            for widget in (
                editor.action, editor.selector_type,
                editor.fallback_selector_type, editor.iframe_path,
            )
        ]
        self.assertLessEqual(max(aligned_right_edges) - min(aligned_right_edges), 2)
        self.assertGreaterEqual(editor.enabled.width(), editor.enabled.sizeHint().width())
        left_tabs.setCurrentIndex(1)
        self.app.processEvents()
        execution_card = editor.findChild(QFrame, 'executionCard')
        failure_action = editor.failure_action
        self.assertLessEqual(
            failure_action.mapTo(execution_card, failure_action.rect().bottomLeft()).y(),
            execution_card.contentsRect().bottom(),
        )
        left_tabs.setCurrentIndex(0)
        editor.action.setCurrentIndex(editor.action.findData('goto'))
        self.app.processEvents()
        event_card = editor.findChild(QFrame, 'eventCard')
        event_title = editor.findChild(QLabel, 'eventCardTitle')
        self.assertFalse(event_title.isVisibleTo(editor))
        editor.close()

    def test_click_event_round_trips_its_success_condition(self) -> None:
        config = {
            'condition': 'operable', 'selector_type': 'css',
            'target': '.save-complete',
        }
        editor = EventEditorDialog(self.window.pages['design'], {
            'name': 'click', 'action': 'click', 'selector_type': 'css',
            'selector': '#save', 'value': json.dumps(config),
        })
        self.assertEqual(editor.click_success_condition.currentData(), 'operable')
        self.assertEqual(editor.click_success_selector_type.currentText(), 'css')
        self.assertEqual(editor.click_success_target.text(), '.save-complete')
        result = editor.result_data()
        self.assertEqual(result['value'], '')
        self.assertEqual(json.loads(result['success_json']), config)
        editor.click_success_condition.setCurrentIndex(
            editor.click_success_condition.findData('none')
        )
        self.assertEqual(editor.result_data()['success_json'], '')
        editor.close()

    def test_page_picker_can_fill_click_success_target_from_execution_tab(self) -> None:
        editor = EventEditorDialog(self.window.pages['design'], {
            'name': 'click', 'action': 'click', 'selector_type': 'css',
            'selector': '#submit', 'value': '',
        })
        editor.left_tabs.setCurrentIndex(1)
        editor.click_success_condition.setCurrentIndex(
            editor.click_success_condition.findData('visible')
        )
        self.assertEqual(editor.pick_button.property('pickDestination'), 'click_success')
        self.assertEqual(editor.pick_button.text(), '成功確認対象を選択')
        editor._run_debug = lambda _message, operation, done: done(operation())
        picked = {
            'selector_type': 'role', 'selector': 'status|Complete',
            'fallback_selector_type': 'none', 'fallback_selector': '',
            'iframe_path': 'css=iframe.result', 'display': 'Complete',
        }
        with patch.object(editor._service_host.debug_browser, 'pick', return_value=picked):
            editor.pick_element()
        self.assertEqual(editor.selector.text(), '#submit')
        self.assertEqual(editor.click_success_selector_type.currentText(), 'role')
        self.assertEqual(editor.click_success_target.text(), 'status|Complete')
        self.assertEqual(
            json.loads(editor.result_data()['success_json'])['iframe_path'], 'css=iframe.result',
        )
        editor.close()

    def test_new_event_debug_buttons_follow_input_state(self) -> None:
        editor = EventEditorDialog(self.window.pages['design'], insert_at=0)
        try_button = editor.findChild(QPushButton, 'tryEventButton')
        self.assertTrue(editor.pick_button.isEnabled())
        self.assertFalse(try_button.isEnabled())
        self.assertTrue(editor.execute_until_button.isEnabled())

        editor.action.setCurrentIndex(editor.action.findData('click'))
        self.assertTrue(try_button.isEnabled())
        editor.close()

    def test_event_editor_translates_designer_and_dynamic_texts_to_chinese(self) -> None:
        set_language('zh')
        try:
            editor = EventEditorDialog(self.window.pages['design'])
            QApplication.processEvents()
            self.assertEqual(
                editor.findChild(QLabel, 'successConfirmationTitle').text(), '成功确认设置',
            )
            self.assertEqual(editor.pick_button.text(), '选择操作对象')
            editor.action.setCurrentIndex(editor.action.findData('click'))
            editor.left_tabs.setCurrentIndex(1)
            editor.click_success_condition.setCurrentIndex(
                editor.click_success_condition.findData('visible')
            )
            self.assertEqual(editor.pick_button.text(), '选择成功确认对象')
            editor.close()
        finally:
            set_language('ja')

    def test_goto_select_and_press_keep_value_separate_from_success_confirmation(self) -> None:
        for action, value in (('goto', 'https://example.com'), ('select', 'A'), ('press', 'Enter')):
            editor = EventEditorDialog(self.window.pages['design'], {
                'name': action, 'action': action, 'selector_type': 'css',
                'selector': '#target', 'value': value,
            })
            self.assertEqual(editor.click_success_condition.currentData(), 'none')
            if action == 'goto':
                # ページ移動は通常の操作対象を持たない。
                self.assertFalse(editor.pick_button.isEnabled())
                editor.left_tabs.setCurrentIndex(1)
            editor.click_success_condition.setCurrentIndex(
                editor.click_success_condition.findData('visible')
            )
            if action == 'goto':
                # 要素による成功確認を選んだ場合だけ、確認対象を選択できる。
                self.assertTrue(editor.pick_button.isEnabled())
            editor.click_success_target.setText('.completed')
            result = editor.result_data()
            self.assertEqual(result['value'], value)
            self.assertEqual(json.loads(result['success_json'])['target'], '.completed')
            editor.close()

    def test_element_picker_suggests_action_only_when_action_is_empty(self) -> None:
        self.assertEqual(ElementPicker._suggest_action({'tag': 'input', 'input_type': 'text'}), 'fill')
        self.assertEqual(ElementPicker._suggest_action({'tag': 'input', 'input_type': 'file'}), 'upload_file')
        self.assertEqual(ElementPicker._suggest_action({'tag': 'a', 'role': 'link'}), 'click')
        editor = EventEditorDialog(self.window.pages['design'])
        editor._run_debug = lambda _message, operation, done: done(operation())
        picked = {
            'selector_type': 'css', 'selector': '#name',
            'fallback_selector_type': 'none', 'fallback_selector': '',
            'iframe_path': '', 'display': 'Name', 'suggested_action': 'fill',
        }
        with patch.object(editor._service_host.debug_browser, 'pick', return_value=picked):
            editor.pick_element()
        self.assertEqual(editor.action.currentData(), 'fill')
        editor.action.setCurrentIndex(editor.action.findData('click'))
        picked['suggested_action'] = 'select'
        with patch.object(editor._service_host.debug_browser, 'pick', return_value=picked):
            editor.pick_element()
        self.assertEqual(editor.action.currentData(), 'click')
        editor.close()

    def test_try_event_on_execution_tab_highlights_success_element(self) -> None:
        editor = EventEditorDialog(self.window.pages['design'], {
            'name': 'click', 'action': 'click', 'selector_type': 'css',
            'selector': '#submit', 'value': json.dumps({
                'condition': 'visible', 'selector_type': 'css', 'target': '.done',
            }),
        })
        editor.left_tabs.setCurrentIndex(1)
        editor._run_debug = lambda _message, operation, done: (operation(), done)
        with patch.object(editor._service_host.debug_browser, 'highlight_element') as highlight:
            with patch.object(editor._service_host.debug_browser, 'execute_event') as execute:
                editor.try_event()
        highlight.assert_called_once()
        execute.assert_not_called()
        highlighted_event = highlight.call_args.args[0]
        self.assertEqual(highlighted_event['selector'], '.done')
        editor.close()

    def test_group_editor_restores_loop_retry_and_boundary_pair_data(self) -> None:
        page = self.window.pages['design']
        editor = EventGroupEditorDialog(page, {
            'name': '繰り返し登録', 'action': 'group_start', 'enabled': 1,
            'data_path': 'plans', 'value': '3', 'retry_interval_ms': 500,
            'timeout_ms': 600000, 'guard': {'logic': 'all', 'rules': []},
        })
        self.assertEqual(editor.windowTitle(), 'イベントグループ編集')
        self.assertTrue(editor.loop_enabled.isChecked())
        self.assertTrue(editor.retry_enabled.isChecked())
        self.assertIsInstance(editor.data_path, QComboBox)
        self.assertEqual(editor.data_path.currentText(), 'plans')
        result = editor.result_data()
        self.assertEqual(result['name'], '繰り返し登録')
        self.assertEqual(result['action'], 'group_start')
        self.assertEqual(result['data_path'], 'plans')
        self.assertEqual(result['value'], '3')
        self.assertEqual(result['retry_count'], 3)
        self.assertEqual(editor.timeout.suffix(), ' ms')
        self.assertEqual(editor.retry_interval.suffix(), ' ms')
        editor.show()
        self.app.processEvents()
        self.assertEqual(editor.path_button.width(), 42)
        self.assertEqual(editor.path_button.text(), '')
        self.assertTrue(editor.path_button.property('dataReferenceButton'))
        self.assertFalse(editor.path_button.icon().isNull())
        self.assertEqual(editor.path_button.toolTip(), 'データ構造から選択')
        self.assertEqual(editor.name.geometry().left(), editor.data_path.geometry().left())
        self.assertEqual(
            editor.name.mapTo(editor, QPoint()).x(),
            editor.timeout.mapTo(editor, QPoint()).x(),
        )
        aligned_widths = [widget.width() for widget in (
            editor.timeout, editor.retry_count, editor.retry_interval, editor.guard_summary,
        )]
        # ボタンと固定スペーサーのサイズヒント差を許容し、視覚上の列幅を確認する。
        self.assertLessEqual(max(aligned_widths) - min(aligned_widths), 6)
        data_path_right = editor.data_path.mapTo(editor, editor.data_path.rect().topRight()).x()
        timeout_right = editor.timeout.mapTo(editor, editor.timeout.rect().topRight()).x()
        self.assertLessEqual(abs(data_path_right - timeout_right), 6)
        path_button_left = editor.path_button.mapTo(editor, QPoint()).x()
        self.assertLessEqual(path_button_left - data_path_right, 12)
        self.assertTrue(editor.name.parentWidget().property('formHost'))
        self.assertTrue(editor.loop_enabled.parentWidget().property('formHost'))
        self.assertTrue(editor.data_path.parentWidget().property('formHost'))
        editor.close()

        workflow_id = self.db.add_workflow('group pair')
        start_id = self.db.add_event(workflow_id, result)
        end = dict(result)
        end.update(action='group_end', value='', data_path='', guard={})
        end_id = self.db.add_event(workflow_id, end)
        rows = [dict(row) for row in self.db.list_events(workflow_id)]
        self.assertEqual(page._paired_boundary_event_id(rows, start_id), end_id)

    def test_data_and_schema_restore_grouped_io_and_structured_values(self) -> None:
        data = self.window.pages['data']
        self.assertEqual(data.tree.columnCount(), 2)
        self.assertEqual(
            [data.tree.headerItem().text(column) for column in range(2)],
            ['名称', '概要'],
        )
        self.assertEqual(data.tree.dragDropMode(), QAbstractItemView.DragDropMode.DragDrop)
        self.assertEqual(data.tree.defaultDropAction(), Qt.DropAction.CopyAction)
        splitter = data.findChild(QSplitter, 'dataSplitter')
        self.assertFalse(splitter.childrenCollapsible())
        self.assertEqual(splitter.widget(0).minimumWidth(), 280)
        self.assertEqual(splitter.widget(1).minimumWidth(), 360)
        self.assertIsNone(data.findChild(QPushButton, 'moveRecordUpButton'))
        self.assertIsNone(data.findChild(QPushButton, 'moveRecordDownButton'))
        self.assertIsNone(data.findChild(QPushButton, 'recordMoreButton'))
        self.assertTrue(data.findChild(QPushButton, 'deleteRecordButton').isHidden())
        self.assertTrue(data.findChild(QPushButton, 'toggleRecordButton').isHidden())
        self.assertFalse(data.findChild(QPushButton, 'copyRecordButton').isHidden())
        list_button = data.findChild(QPushButton, 'addListItemButton')
        self.assertEqual(list_button.text(), 'リスト操作')
        self.assertEqual(
            [action.text() for action in list_button.menu().actions()],
            ['リスト項目を追加', 'リスト項目を削除'],
        )
        io_button = data.findChild(QPushButton, 'exportDataJsonButton')
        self.assertEqual(io_button.text(), 'データ入出力')
        self.assertEqual([action.text() for action in io_button.menu().actions() if not action.isSeparator()], [
            'JSON を出力', 'JSON を読み込む', 'Excel を出力', 'Excel を読み込む',
        ])
        self.assertFalse(data.findChild(QPushButton, 'importDataJsonButton').isVisibleTo(data))
        self.assertEqual(data.values.columnCount(), 4)
        self.assertEqual(data.values.headerItem().text(2), '値  ✎')
        self.db.save_data_schema(0, {
            'name': 'Data', 'type': 'object', 'children': [
                {'name': 'output', 'type': 'object', 'children': [
                    {'name': 'value', 'type': 'text'},
                ]},
            ],
        })
        data.current_data = {'output': {'value': 'x'}}
        data.render_values()
        editable_value = data.values.topLevelItem(0).child(0)
        self.assertEqual(editable_value.toolTip(2), 'ダブルクリックで値を編集')
        self.assertTrue(editable_value.text(2).startswith('✎'))
        self.assertEqual(editable_value.foreground(2).color().name(), '#0b6fae')
        value_toggle = data.findChild(QPushButton, 'valueToggleButton')
        self.assertEqual(value_toggle.text(), '')
        self.assertFalse(value_toggle.icon().isNull())
        self.assertEqual(value_toggle.toolTip(), 'すべて折りたたむ')
        value_toggle.click()
        self.assertEqual(value_toggle.toolTip(), 'すべて展開')
        data.render_values()
        self.assertFalse(data.values.topLevelItem(0).isExpanded())
        self.assertEqual(value_toggle.toolTip(), 'すべて展開')
        schema = self.window.pages['schema']
        add = schema.findChild(QPushButton, 'addFieldButton')
        io = schema.findChild(QPushButton, 'exportSchemaButton')
        self.assertEqual(add.text(), '追加')
        self.assertEqual([action.text() for action in add.menu().actions()], ['フィールド追加', '子フィールド追加'])
        self.assertEqual(io.text(), 'JSON 入出力')
        toggle = schema.findChild(QPushButton, 'toggleAllButton')
        self.assertEqual(toggle.text(), '')
        self.assertFalse(toggle.icon().isNull())
        self.assertEqual(toggle.toolTip(), 'すべて折りたたむ')
        toggle.click()
        self.assertEqual(toggle.toolTip(), 'すべて展開')

        metadata = RecordMetadataDialog(
            self.window, {'name': 'PCL_001', 'summary': 'ケース', 'execution_group': '9'},
        )
        self.assertEqual(metadata.value(), ('PCL_001', 'ケース'))
        self.assertTrue(metadata.findChild(QLabel, 'groupLabel').isHidden())
        self.assertTrue(metadata.findChild(QLineEdit, 'groupEdit').isHidden())
        self.assertEqual(metadata.height(), 208)
        metadata.close()

    def test_data_record_rows_can_be_reordered(self) -> None:
        first_id = self.db.add_data_record(0, 'first', {}, 'first summary')
        second_id = self.db.add_data_record(0, 'second', {}, 'second summary')
        page = self.window.pages['data']
        page.reload(first_id)

        self.assertTrue(page.tree.moveCurrent(1))

        records = self.db.list_data_records()
        self.assertEqual([record['id'] for record in records], [second_id, first_id])
        self.assertEqual(page.selected()['id'], first_id)

    def test_new_rows_follow_the_shared_insertion_rule(self) -> None:
        # 平坦な Flow / 実行データは選択行の直後へ挿入する。
        flow_page = self.window.pages['design']
        first_flow = self.db.add_workflow('first flow')
        second_flow = self.db.add_workflow('second flow')
        new_flow = self.db.add_workflow('new flow')
        flow_page._place_new_workflow(new_flow, first_flow)
        self.assertEqual(
            [row['id'] for row in self.db.list_workflows()],
            [first_flow, new_flow, second_flow],
        )

        data_page = self.window.pages['data']
        first_record = self.db.add_data_record(0, 'first record', {})
        second_record = self.db.add_data_record(0, 'second record', {})
        new_record = self.db.add_data_record(0, 'new record', {})
        data_page._place_new_record(new_record, first_record)
        self.assertEqual(
            [row['id'] for row in self.db.list_data_records()],
            [first_record, new_record, second_record],
        )

        # 構造体選択時はその子の末尾、通常行選択時は同階層の直後へ追加する。
        schema_page = self.window.pages['schema']
        schema_page.schema = {
            'name': 'Data', 'type': 'object', 'children': [
                {'name': 'field', 'type': 'text'},
                {'name': 'group', 'type': 'object', 'children': []},
            ],
        }
        schema_page.render((0,))
        additions = iter([
            {'name': 'after_field', 'type': 'text'},
            {'name': 'inside_group', 'type': 'text'},
            {'name': 'at_end', 'type': 'text'},
        ])
        schema_page._ask_field = lambda: next(additions)
        schema_page.add_field()
        schema_page.tree.setCurrentItem(schema_page.tree.topLevelItem(2))
        schema_page.add_field()
        schema_page.tree.clearSelection()
        schema_page.add_field()
        self.assertEqual(
            [node['name'] for node in schema_page.schema['children']],
            ['field', 'after_field', 'group', 'at_end'],
        )
        self.assertEqual(
            [node['name'] for node in schema_page.schema['children'][2]['children']],
            ['inside_group'],
        )
        group_item = schema_page.tree.topLevelItem(2)
        group_item.setExpanded(True)
        schema_page.tree.setCurrentItem(group_item)
        schema_page._ask_field = lambda _old: {
            'name': 'renamed_group', 'type': 'object', 'children': [],
        }
        schema_page.edit_field()
        self.assertTrue(schema_page.tree.topLevelItem(2).isExpanded())

    def test_schema_move_uses_index_path_and_keeps_selection(self) -> None:
        page = self.window.pages['schema']
        page.schema = {
            'name': 'Data',
            'type': 'object',
            'children': [
                {'name': 'first', 'type': 'object', 'children': [{'name': 'child', 'type': 'text'}]},
                {'name': 'second', 'type': 'text'},
            ],
        }
        page.render()

        # UserRole の辞書が複製・変化しても、schema のインデックス経路で移動できる。
        first_item = page.tree.topLevelItem(0)
        first_item.setData(0, Qt.ItemDataRole.UserRole, {'name': 'stale', 'type': 'text'})
        page.tree.setCurrentItem(first_item)
        page.move(1)
        self.assertEqual([node['name'] for node in page.schema['children']], ['second', 'first'])
        self.assertEqual(page.tree.currentItem().text(0), 'first')
        self.assertTrue(page.tree.currentItem().isSelected())

        # 子項目でも同じ経路解決を使い、移動後の行選択を維持する。
        page.schema['children'][1]['children'].append({'name': 'child2', 'type': 'text'})
        page.render((1, 0))
        page.move(1)
        self.assertEqual(
            [node['name'] for node in page.schema['children'][1]['children']],
            ['child2', 'child'],
        )
        self.assertEqual(page.tree.currentItem().text(0), 'child')
        self.assertTrue(page.tree.currentItem().isSelected())

    def test_schema_drag_then_button_move_keeps_same_node_selected(self) -> None:
        page = self.window.pages['schema']
        page.schema = {
            'name': 'Data',
            'type': 'object',
            'children': [
                {'name': 'first', 'type': 'text'},
                {'name': 'second', 'type': 'text'},
                {'name': 'third', 'type': 'text'},
            ],
        }
        page.render()

        # ドラッグ後の表示順を再現し、直後のボタン移動でも同じノードを基準にする。
        first_item = page.tree.topLevelItem(0)
        page.tree.setCurrentItem(first_item)
        page._remember_reorder_selection(QModelIndex(), 2, 2, QModelIndex(), 0)
        page.tree.insertTopLevelItem(0, page.tree.takeTopLevelItem(2))
        page._queue_schema_reorder()

        page.move(1)

        self.assertEqual(
            [node['name'] for node in page.schema['children']],
            ['first', 'third', 'second'],
        )
        self.assertEqual(page.tree.currentItem().text(0), 'third')
        self.assertTrue(page.tree.currentItem().isSelected())

    def test_page_margins_and_small_field_dialog_are_consistent(self) -> None:
        expected = (28, 22, 28, 22)
        for name in ('auth', 'design', 'schema', 'data', 'execution', 'settings'):
            page = self.window.pages[name]
            form = page.layout().itemAt(0).widget()
            margins = form.findChild(QVBoxLayout, 'rootLayout').contentsMargins()
            self.assertEqual((margins.left(), margins.top(), margins.right(), margins.bottom()), expected)
        dialog = FieldDialog(self.window, {'name': '案件名', 'type': 'text'})
        self.assertEqual(dialog.minimumSize(), dialog.maximumSize())
        self.assertTrue(dialog.findChild(QFrame, 'fieldCard').property('card'))
        self.assertEqual(dialog.windowTitle(), 'フィールド編集')
        dialog.close()

    def test_execution_page_restores_status_log_and_record_controls(self) -> None:
        page = self.window.pages['execution']
        page.append_log('msg.0588visible')
        self.assertEqual(page._log_lines.pop(), f'{tr("msg.0588")}visible')
        self.assertEqual(page.stack.count(), 2)
        self.assertEqual(page.records.columnCount(), 7)
        self.assertEqual(
            [page.records.headerItem().text(column) for column in range(7)],
            ['実行グループ', '今回実行', '実行データ', '概要', '業務フロー', '現在のイベント', '実行状況'],
        )
        self.assertEqual(
            [page.records.columnWidth(column) for column in range(7)],
            [90, 90, 190, 210, 200, 215, 155],
        )
        self.assertEqual(page.records.toolTip(), '')
        self.assertEqual(page.records.header().minimumSectionSize(), 54)
        self.assertTrue(page.findChild(QPushButton, 'clearResultsButton').property('danger'))
        self.assertEqual(STATUS_LABELS['not_run'], '未実行')
        self.assertEqual(STATUS_LABELS['running'], '実行中')
        self.assertEqual(STATUS_LABELS['skipped'], 'スキップ')
        self.assertIsNotNone(page.findChild(QPushButton, 'toggleExecutionButton'))
        self.assertIsNotNone(page.findChild(QPushButton, 'setGroupButton'))
        self.assertIsNotNone(page.findChild(QPushButton, 'clearResultsButton'))
        called = []
        original_set_group = page.set_group
        original_toggle_record = page.toggle_record
        page.set_group = lambda: called.append('group')
        page.toggle_record = lambda: called.append('toggle')
        page._record_double_clicked(None, 6)
        self.assertEqual(called, [])
        page._record_double_clicked(None, 0)
        self.assertEqual(called, ['group'])
        page._record_double_clicked(None, 1)
        self.assertEqual(called, ['group', 'toggle'])
        page.set_group = original_set_group
        page.toggle_record = original_toggle_record
        page.select_tab(1)
        self.assertEqual(page.stack.currentIndex(), 1)
        page.select_tab(0)
        self.assertEqual(page.stack.currentIndex(), 0)

    def test_settings_page_uses_compact_balanced_cards(self) -> None:
        page = self.window.pages['settings']
        display_card = page.findChild(QFrame, 'displayCard')
        runtime_card = page.findChild(QFrame, 'runtimeCard')
        self.assertEqual((display_card.height(), runtime_card.height()), (330, 330))
        save_buttons = [
            button for button in page.findChildren(QPushButton)
            if '保存' in button.text()
        ]
        self.assertEqual(len(save_buttons), 1)
        self.assertEqual(save_buttons[0].objectName(), 'saveSettingsButton')
        self.assertEqual(save_buttons[0].width(), 148)
        self.assertEqual(page.language.currentText(), '日本語')
        self.assertEqual(page.language.currentData(), 'ja')
        self.assertEqual(page.timeout.suffix(), ' ms')

    def test_settings_edits_are_pending_until_single_save(self) -> None:
        page = self.window.pages['settings']
        stored_url = self.db.get_start_url()
        page.start_url.setText('https://example.com/pending')
        page.font_size.setCurrentText('8')
        self.assertTrue(page.has_pending_changes())
        self.assertEqual(self.db.get_start_url(), stored_url)
        self.assertTrue(page.save_all(show_message=False))
        self.assertEqual(self.db.get_start_url(), 'https://example.com/pending')
        data_tree = self.window.pages['data'].tree
        self.assertEqual(data_tree.font().pointSize(), 8)
        self.assertEqual(data_tree.viewport().font().pointSize(), 8)
        self.assertEqual(data_tree.header().font().pointSize(), 8)
        self.assertFalse(page.has_pending_changes())

    def test_navigation_stops_when_current_page_rejects_pending_changes(self) -> None:
        settings = self.window.pages['settings']
        self.window.show_page('settings')
        settings.confirm_pending_changes = lambda: False
        self.window.show_page('design')
        self.assertIs(self.window.stack.currentWidget(), settings)

    def test_schema_and_data_pages_detect_unsaved_edits(self) -> None:
        schema_page = self.window.pages['schema']
        schema_page.schema.setdefault('children', []).append({'name': 'pending', 'type': 'text'})
        self.assertTrue(schema_page.has_pending_changes())

        self.db.save_data_schema(0, {'type': 'object', 'children': [{'name': 'value', 'type': 'text'}]})
        record_id = self.db.add_data_record(0, 'record', {'value': 'before'})
        data_page = self.window.pages['data']
        data_page.reload(record_id)
        data_page.current_data['value'] = 'after'
        self.assertTrue(data_page.has_pending_changes())

    def test_condition_rule_restores_schema_picker_and_primary_save(self) -> None:
        schema = {'type': 'object', 'children': [{'name': 'case_no', 'type': 'text'}]}
        editor = GuardRuleEditorDialog(self.window, schema)
        choose_path_button = editor.findChild(QPushButton, 'choosePathButton')
        self.assertEqual(choose_path_button.text(), '')
        self.assertTrue(choose_path_button.property('dataReferenceButton'))
        self.assertFalse(choose_path_button.icon().isNull())
        self.assertEqual(choose_path_button.toolTip(), 'データ構造から選択')
        self.assertEqual(choose_path_button.width(), 42)
        editor.show()
        self.app.processEvents()
        aligned_right_edges = [
            widget.mapTo(editor, widget.rect().topRight()).x()
            for widget in (editor.path, editor.operator, editor.expected)
        ]
        self.assertLessEqual(max(aligned_right_edges) - min(aligned_right_edges), 2)
        self.assertIsNone(editor.findChild(QLabel, 'titleLabel'))
        self.assertEqual(editor.minimumSize(), editor.maximumSize())
        buttons = editor.findChild(QDialogButtonBox, 'buttonBox')
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.assertEqual(save.text(), '保存')
        self.assertTrue(save.property('primary'))
        self.assertEqual(save.minimumWidth(), cancel.minimumWidth())
        picker = DataPathPickerDialog(editor, schema)
        toggle = picker.findChild(QPushButton, 'toggleAllButton')
        self.assertEqual(toggle.text(), '')
        self.assertFalse(toggle.icon().isNull())
        self.assertEqual(toggle.toolTip(), 'すべて展開')
        picker.tree.setCurrentItem(picker.tree.topLevelItem(0))
        picker._choose()
        self.assertEqual(picker.result_path, 'case_no')
        list_schema = {'type': 'object', 'children': [
            {'name': 'plans', 'type': 'list', 'children': [{'name': 'name', 'type': 'text'}]},
            {'name': 'case_no', 'type': 'text'},
        ]}
        list_picker = DataPathPickerDialog(editor, list_schema, allowed_types={'list'})
        list_toggle = list_picker.findChild(QPushButton, 'toggleAllButton')
        self.assertEqual(list_toggle.toolTip(), 'すべて折りたたむ')
        list_toggle.click()
        self.assertEqual(list_toggle.toolTip(), 'すべて展開')
        list_toggle.click()
        self.assertEqual(list_toggle.toolTip(), 'すべて折りたたむ')
        plans = list_picker.tree.topLevelItem(0)
        case_no = list_picker.tree.topLevelItem(1)
        self.assertEqual(plans.data(0, Qt.ItemDataRole.UserRole), 'plans')
        self.assertIsNone(case_no.data(0, Qt.ItemDataRole.UserRole))
        list_picker.close()
        editor.close()

    def test_combo_box_wheel_selection_is_globally_blocked(self) -> None:
        combo = QComboBox()
        spin = QSpinBox()
        blocker = _ComboBoxWheelBlocker()
        self.assertTrue(blocker.eventFilter(combo, QEvent(QEvent.Type.Wheel)))
        self.assertTrue(blocker.eventFilter(spin, QEvent(QEvent.Type.Wheel)))
        blocker.eventFilter(spin, QEvent(QEvent.Type.Show))
        self.assertTrue(spin.isAccelerated())
        input_dialog = QInputDialog()
        blocker.eventFilter(input_dialog, QEvent(QEvent.Type.Show))
        self.assertTrue(input_dialog.property('compactDialog'))
        input_dialog.close()
        self.assertFalse(blocker.eventFilter(combo, QEvent(QEvent.Type.MouseButtonPress)))

    def test_pointer_selection_does_not_move_table_scrollbars(self) -> None:
        table = QTableWidget(30, 12)
        configure_table_view(table)
        table.resize(320, 220)
        for column in range(table.columnCount()):
            table.setColumnWidth(column, 120)
        table.show()
        self.app.processEvents()
        horizontal = table.horizontalScrollBar()
        vertical = table.verticalScrollBar()
        horizontal.setValue(horizontal.maximum() // 3)
        vertical.setValue(vertical.maximum() // 3)
        expected = (horizontal.value(), vertical.value())

        table._pointer_scroll_filter.eventFilter(
            table.viewport(), QEvent(QEvent.Type.MouseButtonDblClick),
        )
        horizontal.setValue(horizontal.maximum())
        vertical.setValue(vertical.maximum())
        table._pointer_scroll_filter.eventFilter(
            table.viewport(), QEvent(QEvent.Type.MouseButtonRelease),
        )
        QTimer.singleShot(600, lambda: horizontal.setValue(horizontal.maximum()))
        QTimer.singleShot(600, lambda: vertical.setValue(vertical.maximum()))
        QTest.qWait(1100)

        self.assertEqual((horizontal.value(), vertical.value()), expected)
        table._pointer_scroll_filter.eventFilter(
            table.viewport(), QEvent(QEvent.Type.Wheel),
        )
        horizontal.setValue(horizontal.maximum())
        self.assertEqual(horizontal.value(), horizontal.maximum())
        table.close()

    def test_small_message_boxes_are_compact_localized_and_primary(self) -> None:
        box = QMessageBox(QMessageBox.Icon.Information, 'お知らせ', '保存しました。', QMessageBox.StandardButton.Ok)
        blocker = _ComboBoxWheelBlocker()
        blocker.eventFilter(box, QEvent(QEvent.Type.Show))
        ok = box.button(QMessageBox.StandardButton.Ok)
        self.assertEqual(ok.text(), '確認')
        self.assertTrue(ok.property('primary'))
        self.assertEqual(ok.width(), 88)
        self.assertTrue(box.property('compactDialog'))
        box.close()

        question = QMessageBox(
            QMessageBox.Icon.Question, '削除', '選択した項目を削除しますか？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        blocker.eventFilter(question, QEvent(QEvent.Type.Show))
        yes = question.button(QMessageBox.StandardButton.Yes)
        no = question.button(QMessageBox.StandardButton.No)
        self.assertEqual((yes.text(), no.text()), ('削除', 'キャンセル'))
        self.assertEqual(yes.width(), no.width())
        self.assertGreaterEqual(yes.width(), 96)
        self.assertFalse(yes.property('primary'))
        self.assertTrue(yes.property('danger'))
        self.assertTrue(question.findChild(QLabel, 'qt_msgboxex_icon_label').isHidden())
        question.close()

        delete_dialog = DeletionConfirmDialog(
            self.window, '選択したフィールドと子フィールドを削除しますか？',
        )
        self.assertEqual(delete_dialog.size().width(), 380)
        self.assertEqual(delete_dialog.size().height(), 156)
        self.assertIsNotNone(delete_dialog.findChild(QLabel, 'confirmIcon').pixmap())
        self.assertEqual(delete_dialog.findChild(QLabel, 'confirmMessage').text(),
                         '選択したフィールドと子フィールドを削除しますか？')
        self.assertTrue(delete_dialog.delete_button.property('danger'))
        self.assertTrue(delete_dialog.cancel_button.isDefault())
        self.assertEqual(delete_dialog.delete_button.width(), delete_dialog.cancel_button.width())
        delete_dialog.close()

        normal_dialog = ConfirmationDialog(
            self.window, '確認', 'この操作を続行しますか？', confirm_text='続行',
        )
        self.assertTrue(normal_dialog.confirm_button.property('primary'))
        self.assertFalse(bool(normal_dialog.confirm_button.property('danger')))
        self.assertTrue(normal_dialog.cancel_button.isDefault())
        normal_dialog.close()

        long_message = '\n'.join(f'検証エラー {index}' for index in range(12))
        details_dialog = ConfirmationDialog(
            self.window, '読込エラー', long_message, cancel_text=None,
            icon=QStyle.StandardPixmap.SP_MessageBoxCritical,
        )
        details = details_dialog.findChild(QPlainTextEdit, 'messageDetails')
        self.assertTrue(details_dialog.findChild(QLabel, 'confirmMessage').isHidden())
        self.assertFalse(details.isHidden())
        self.assertTrue(details.isReadOnly())
        self.assertEqual(details.toPlainText(), long_message)
        self.assertGreater(details_dialog.height(), 156)
        details_dialog.close()

        notice_dialog = ConfirmationDialog(
            self.window, 'お知らせ', '保存しました。', cancel_text=None,
            icon=QStyle.StandardPixmap.SP_MessageBoxInformation,
        )
        self.assertIsNone(notice_dialog.cancel_button)
        self.assertTrue(notice_dialog.confirm_button.isDefault())
        self.assertEqual(notice_dialog.confirm_button.text(), '確認')
        notice_dialog.close()


if __name__ == '__main__':
    unittest.main()
