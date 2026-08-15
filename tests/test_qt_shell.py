from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
from pathlib import Path
import json
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QStyle, QTableWidget,
    QTabWidget, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QEvent, QModelIndex, QPoint, QSize, QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtTest import QTest

from core.database import Database
from browser.element_picker import ElementPicker
from i18n import set_language, tr
from qt_ui.main_window import MainWindow
from qt_ui.application import _ComboBoxWheelBlocker
from qt_ui.pages.flow_design import (
    DataPathPickerDialog, EventEditorDialog, EventGroupEditorDialog, FlowEditorDialog,
    GuardConditionEditorDialog, GuardRuleEditorDialog, WorkflowGroupDialog,
)
from qt_ui.pages.structured import FieldDialog
from qt_ui.pages.data import RecordMetadataDialog
from qt_ui.pages.execution import (
    ExecutionActionDelegate, ExecutionOrderDialog, STATUS_LABELS, natural_sort_key,
)
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
        self.assertEqual(self.window.stack.count(), 1)
        for page_name in self.window.pages:
            self.window.show_page(page_name)
        self.assertEqual(self.window.stack.count(), 6)
        self.assertEqual(self.window.size(), QSize(1480, 860))
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

    def test_imports_confirm_before_replacing_existing_content(self) -> None:
        data_page = self.window.pages['data']
        schema_page = self.window.pages['schema']
        design_page = self.window.pages['design']
        import_path = self.project_dir / 'import-source.json'
        import_path.write_text('[]', encoding='utf-8')
        record_id = self.db.add_data_record(0, 'existing', {'value': 'before'})

        for method_name, reader_name in (
            ('import_json', None),
            ('import_excel', 'read_records_excel'),
            ('import_excel_with_schema', 'read_records_excel_with_schema'),
        ):
            patches = [
                patch('qt_ui.pages.data.QFileDialog.getOpenFileName', return_value=(str(import_path), '')),
                patch('qt_ui.pages.data.confirm_import_overwrite', return_value=False),
            ]
            if reader_name is not None:
                patches.append(patch(f'qt_ui.pages.data.{reader_name}'))
            started = [item.start() for item in patches]
            try:
                getattr(data_page, method_name)()
                if reader_name is not None:
                    started[-1].assert_not_called()
            finally:
                for item in reversed(patches):
                    item.stop()
            self.assertEqual(self.db.list_data_records()[0]['id'], record_id)

        schema_page.schema = {
            'name': 'Data', 'type': 'object',
            'children': [{'name': 'existing', 'type': 'text'}],
        }
        with (
            patch('qt_ui.pages.structured.QFileDialog.getOpenFileName', return_value=(str(import_path), '')),
            patch('qt_ui.pages.structured.confirm_import_overwrite', return_value=False),
        ):
            schema_page.import_json()
        self.assertEqual(schema_page.schema['children'][0]['name'], 'existing')

        workflow_id = self.db.add_workflow('existing flow')
        with (
            patch('qt_ui.pages.flow_design.QFileDialog.getOpenFileName', return_value=(str(import_path), '')),
            patch('qt_ui.pages.flow_design.confirm_action', return_value=False),
            patch.object(self.db, 'import_workflow_collection') as importer,
        ):
            design_page.import_json()
            importer.assert_not_called()
        self.assertEqual(self.db.list_workflows()[0]['id'], workflow_id)

    def test_excel_import_with_schema_replaces_both_after_confirmation(self) -> None:
        page = self.window.pages['data']
        self.db.add_data_record(0, 'existing', {'old': 'value'})
        schema = {
            'name': 'Data', 'type': 'object',
            'children': [{'name': 'amount', 'type': 'number'}],
        }
        records = [{
            'name': 'PCL_001', 'summary': 'summary', 'enabled': True,
            'execution_group': '1', 'data': {'amount': 12},
        }]
        path = self.project_dir / 'with-schema.xlsx'
        with (
            patch('qt_ui.pages.data.QFileDialog.getOpenFileName', return_value=(str(path), '')),
            patch('qt_ui.pages.data.confirm_import_overwrite', return_value=True),
            patch('qt_ui.pages.data.read_records_excel_with_schema', return_value=(schema, records)),
            patch('qt_ui.pages.data.show_file_imported') as notified,
        ):
            page.import_excel_with_schema()
        self.assertEqual(self.db.get_data_schema(), schema)
        self.assertEqual(self.db.list_data_records()[0]['data'], {'amount': 12})
        notified.assert_called_once_with(page, str(path))

    def test_all_dialogs_are_locked_without_locking_main_window(self) -> None:
        blocker = _ComboBoxWheelBlocker()
        dialog = QDialog(self.window)
        dialog.resize(640, 420)
        blocker.eventFilter(dialog, QEvent(QEvent.Type.Show))
        self.assertEqual(dialog.minimumSize(), dialog.maximumSize())
        self.assertEqual((dialog.width(), dialog.height()), (640, 420))
        self.assertNotEqual(self.window.minimumSize(), self.window.maximumSize())
        self.assertEqual(self.window.size().height(), 860)
        self.assertEqual(self.window.minimumSize(), self.window.size())
        self.window.show_page('settings')
        self.assertIs(self.window.stack.currentWidget(), self.window.pages['settings'])

    def test_execution_page_is_selected_at_startup(self) -> None:
        self.assertIs(
            self.window.stack.currentWidget(), self.window.pages['execution'],
        )
        self.assertTrue(self.window.nav_buttons['execution'].property('navSelected'))
        self.assertEqual(
            set(self.window.pages._instances), {'execution'},
        )

        # 画面遷移なしで Data 管理だけを事前生成できる。
        self.window.preload_page('data')
        self.assertEqual(
            set(self.window.pages._instances), {'execution', 'data'},
        )
        self.assertIs(
            self.window.stack.currentWidget(), self.window.pages['execution'],
        )

        # 初回の画面切替時にだけ対象画面を生成する。
        self.window.show_page('auth')
        self.assertEqual(
            set(self.window.pages._instances), {'execution', 'data', 'auth'},
        )
        self.assertIsNone(self.window.pages['auth'].session._thread)
        self.window.show_page('design')
        self.assertIsNone(self.window.pages['design'].debug_browser._thread)

    def test_flow_page_loads_database_rows(self) -> None:
        workflow_id = self.db.add_workflow('Qt flow')
        self.window.pages['design'].reload(workflow_id)
        self.assertEqual(self.window.pages['design'].workflow_table.topLevelItemCount(), 1)
        self.assertEqual(self.window.pages['design'].workflow_table.topLevelItem(0).text(0), 'Qt flow')

    def test_all_designer_forms_are_present_and_valid(self) -> None:
        forms = Path(__file__).parents[1] / 'qt_ui' / 'forms'
        expected = {
            'main_window.ui', 'flow_design.ui', 'flow_editor.ui', 'event_editor.ui', 'guard_dialog.ui',
            'guard_condition.ui', 'guard_rule.ui', 'data_path_picker.ui',
            'event_group.ui',
            'auth.ui', 'settings.ui', 'schema.ui', 'field_dialog.ui', 'data.ui',
            'record_dialog.ui', 'execution.ui', 'execution_order.ui', 'confirmation.ui',
            'workflow_group.ui',
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

    def test_execution_log_is_appended_to_daily_file(self) -> None:
        """画面ログと同じ内容が旧版互換の日次ログへ保存される。"""
        page = self.window.pages['execution']
        page.append_log('ログ保存テスト')

        log_path = self.project_dir / 'log' / f'{datetime.now():%Y-%m-%d}.log'
        text = log_path.read_text(encoding='utf-8')
        self.assertIn('ログ保存テスト', text)
        self.assertRegex(text, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[MainThread\] ')

    def test_data_update_keeps_completed_execution_status(self) -> None:
        """実行結果データの保存後も、完了状態を明示的なクリアまで維持する。"""
        record_id = self.db.add_data_record(0, '完了データ', {'value': 'before'})
        self.db.set_data_record_status(record_id, 'success')

        self.db.update_data_record(record_id, '完了データ', {'value': 'after'})

        record = next(row for row in self.db.list_data_records() if row['id'] == record_id)
        self.assertEqual(record['execution_status'], 'success')

        self.db.set_data_record_enabled(record_id, False)
        self.db.prepare_data_record_statuses()

        skipped = next(row for row in self.db.list_data_records() if row['id'] == record_id)
        self.assertFalse(skipped['enabled'])
        self.assertEqual(skipped['execution_status'], 'success')

    def test_execution_bulk_settings_and_group_order_use_single_saved_state(self) -> None:
        """大量データ向けの一括設定とグループ内順序を DB に保持する。"""
        ids = [
            self.db.add_data_record(0, name, {})
            for name in ('bulk_A1', 'bulk_B1', 'bulk_A2')
        ]
        self.db.set_data_records_group([ids[0], ids[2]], 'bulk_A')
        self.db.set_data_records_group([ids[1]], 'bulk_B')
        self.db.set_data_records_enabled([ids[0], ids[2]], False)
        rows = {record['id']: record for record in self.db.list_data_records()}
        self.assertFalse(rows[ids[0]]['enabled'])
        self.assertTrue(rows[ids[1]]['enabled'])
        self.assertFalse(rows[ids[2]]['enabled'])

        self.db.set_data_records_enabled([ids[0], ids[2]], None)
        self.db.reorder_group_data_records('bulk_A', [ids[2], ids[0]])
        ordered = [
            record['id'] for record in self.db.list_data_records()
            if record['execution_group'] == 'bulk_A'
        ]
        self.assertEqual(ordered, [ids[2], ids[0]])
        self.assertTrue(all(
            record['enabled'] for record in self.db.list_data_records()
            if record['id'] in {ids[0], ids[2]}
        ))
        self.db.set_data_record_status(ids[0], 'error_waiting')
        self.db.recover_interrupted_data_record_statuses()
        recovered = next(
            record for record in self.db.list_data_records() if record['id'] == ids[0]
        )
        self.assertEqual(recovered['execution_status'], 'failed')

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

    def test_auth_cards_keep_layout_ratio_when_none_is_selected(self) -> None:
        page = self.window.pages['auth']
        self.window.resize(1600, 900)
        self.window.show_page('auth')
        self.window.show()
        self.app.processEvents()
        profile_card = page.findChild(QFrame, 'profileCard')
        browser_card = page.findChild(QFrame, 'browserCard')
        initial_widths = profile_card.width(), browser_card.width()

        page.select_profile('none')
        self.app.processEvents()

        self.assertEqual((profile_card.width(), browser_card.width()), initial_widths)
        self.assertGreater(profile_card.width(), 250)

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
        self.assertEqual(page.workflow_table.dragDropMode(), QAbstractItemView.DragDropMode.DragDrop)
        self.assertEqual(page.workflow_table.defaultDropAction(), Qt.DropAction.CopyAction)
        self.assertEqual(page.event_tree.dragDropMode(), QAbstractItemView.DragDropMode.DragDrop)
        self.assertEqual(page.event_tree.defaultDropAction(), Qt.DropAction.CopyAction)
        header = page.event_tree.header()
        self.assertTrue(page.workflow_table.header().sectionsMovable())
        self.assertTrue(header.sectionsMovable())
        workflow_header = page.workflow_table.header()
        self.assertEqual(
            [workflow_header.logicalIndex(visual) for visual in range(5)],
            [0, 1, 2, 3, 4],
        )
        self.assertEqual(
            [header.logicalIndex(visual) for visual in range(6)],
            [0, 5, 4, 3, 1, 2],
        )
        self.assertGreater(page.workflow_table.columnWidth(0), max(
            page.workflow_table.columnWidth(column) for column in (1, 2, 3, 4)
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
        first_item = page.workflow_table.topLevelItem(0)
        page._workflow_double_clicked(first_item, 2)
        self.assertNotEqual(bool(next(row for row in self.db.list_workflows() if row['id'] == first)['enabled']), enabled_before)
        page._workflow_double_clicked(page.workflow_table.topLevelItem(0), 4)
        self.assertTrue(bool(next(row for row in self.db.list_workflows() if row['id'] == first)['pcl_loop_start']))
        calls: list[str] = []
        page.edit_guard_summary = lambda: calls.append('guard')
        page.edit_workflow = lambda: calls.append('edit')
        page._workflow_double_clicked(page.workflow_table.topLevelItem(0), 3)
        page._workflow_double_clicked(page.workflow_table.topLevelItem(0), 0)
        self.assertEqual(calls, ['guard', 'edit'])
        page._reorder_workflow_rows(0, 1)
        self.assertEqual([row['id'] for row in self.db.list_workflows()], [second, first])
        self.assertEqual(page.workflow_table.topLevelItem(0).text(0), 'second')
        self.assertEqual(page.workflow_table.topLevelItem(1).text(0), 'first')

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

    def test_workflow_copy_paste_inserts_after_selected_with_events(self) -> None:
        page = self.window.pages['design']
        workflow_id = self.db.add_workflow('source flow', 'description')
        event = {
            'name': 'click event', 'action': 'click', 'selector_type': 'css',
            'selector': '#submit', 'value': '', 'timeout_ms': 10000,
            'enabled': True, 'continue_on_error': False,
            'guard': {'logic': 'all', 'rules': []},
        }
        self.db.add_event(workflow_id, event)
        page.reload(workflow_id)
        copy_shortcut, paste_shortcut = page.workflow_table._structured_copy_paste_shortcuts
        copy_shortcut.activated.emit()
        paste_shortcut.activated.emit()

        workflows = [dict(row) for row in self.db.list_workflows()]
        self.assertEqual([row['name'] for row in workflows], ['source flow', 'source flow - Copy'])
        copied_events = [dict(row) for row in self.db.list_events(workflows[1]['id'])]
        self.assertEqual([(row['name'], row['action']) for row in copied_events], [('click event', 'click')])

    def test_workflow_groups_preserve_hierarchy_and_flat_execution_order(self) -> None:
        page = self.window.pages['design']
        first_id = self.db.add_workflow('group flow 1')
        second_id = self.db.add_workflow('group flow 2')
        group_id = self.db.add_workflow_group('Flow group')
        nodes = [dict(row) for row in self.db.list_workflow_outline()]
        first_node = next(int(row['id']) for row in nodes if row['workflow_id'] == first_id)
        second_node = next(int(row['id']) for row in nodes if row['workflow_id'] == second_id)
        other_nodes = [
            (int(row['id']), row['parent_id'], int(row['position'])) for row in nodes
            if int(row['id']) not in {first_node, second_node, group_id}
        ]
        self.db.reorder_workflow_outline(
            other_nodes + [(group_id, None, len(other_nodes) + 1), (first_node, group_id, 1), (second_node, group_id, 2)]
        )
        group_guard = {
            'logic': 'all', 'rules': [{'path': 'kind', 'operator': 'eq', 'value': 'A'}],
        }
        flow_guard = {
            'logic': 'any', 'rules': [{'path': 'status', 'operator': 'eq', 'value': 'ready'}],
        }
        self.db.update_workflow_group(group_id, 'Flow group', group_guard)
        self.db.set_workflow_guard(first_id, flow_guard)

        page.reload(first_id)
        group_item = page.workflow_table.topLevelItem(page.workflow_table.topLevelItemCount() - 1)
        self.assertEqual(group_item.text(0), 'Flow group')
        self.assertEqual(group_item.childCount(), 2)
        self.assertIn('kind', group_item.text(3))
        self.assertEqual(self.db.get_workflow_guards(first_id), [group_guard, flow_guard])
        self.assertEqual(page.workflow_table.headerItem().text(1), '順番')
        self.assertEqual(page.findChild(QPushButton, 'newWorkflowButton').text(), '追加')
        self.assertEqual(
            [action.text() for action in page.findChild(QPushButton, 'newWorkflowButton').menu().actions()],
            ['フロー追加', 'グループ追加'],
        )

        page.workflow_table.setCurrentItem(group_item.child(1))
        page.workflow_table.moveCurrent(-1)
        self.app.processEvents()
        ordered = [int(row['id']) for row in self.db.list_workflows()]
        self.assertLess(ordered.index(second_id), ordered.index(first_id))

        # Group 選択中の貼付けは、その Group の子ではなく同じ階層の直後へ置く。
        group_item = next(
            page.workflow_table.topLevelItem(index)
            for index in range(page.workflow_table.topLevelItemCount())
            if page.workflow_table.topLevelItem(index).text(0) == 'Flow group'
        )
        page.workflow_table.setCurrentItem(group_item)
        copy_shortcut, paste_shortcut = page.workflow_table._structured_copy_paste_shortcuts
        copy_shortcut.activated.emit()
        paste_shortcut.activated.emit()
        groups = [
            page.workflow_table.topLevelItem(index)
            for index in range(page.workflow_table.topLevelItemCount())
            if page.workflow_table.topLevelItem(index).text(0) == 'Flow group'
        ]
        self.assertEqual(len(groups), 2)
        self.assertIs(page.workflow_table.currentItem(), groups[1])
        self.assertTrue(all(item.parent() is None for item in groups))
        self.assertFalse(any(
            (groups[0].child(index).data(0, page.WORKFLOW_DATA_ROLE) or {}).get('kind') == 'group'
            for index in range(groups[0].childCount())
        ))

    def test_workflow_delete_leaves_tree_and_event_list_unselected(self) -> None:
        page = self.window.pages['design']
        workflow_id = self.db.add_workflow('delete selection')
        self.db.add_event(workflow_id, {
            'name': 'event', 'action': 'click', 'selector_type': 'none', 'selector': '',
            'value': '', 'timeout_ms': 10000, 'enabled': True,
            'continue_on_error': False, 'guard': {'logic': 'all', 'rules': []},
        })
        page.reload(workflow_id)
        self.assertIsNotNone(page.workflow_table.currentItem())
        with patch('qt_ui.pages.flow_design.confirm_deletion', return_value=True):
            page.delete_workflow()
        self.assertIsNone(page.workflow_table.currentItem())
        self.assertEqual(page.workflow_table.selectedItems(), [])
        self.assertIsNone(page.current_workflow_id)
        self.assertEqual(page.event_tree.topLevelItemCount(), 0)

    def test_event_group_copy_paste_keeps_boundaries_and_existing_insert_position(self) -> None:
        page = self.window.pages['design']
        workflow_id = self.db.add_workflow('event copy')
        base = {
            'selector_type': 'none', 'selector': '', 'value': '',
            'timeout_ms': 10000, 'enabled': True, 'continue_on_error': False,
            'guard': {'logic': 'all', 'rules': []},
        }
        self.db.add_event(workflow_id, base | {'name': 'group', 'action': 'group_start'})
        self.db.add_event(workflow_id, base | {'name': 'child', 'action': 'click'})
        self.db.add_event(workflow_id, base | {'name': 'group', 'action': 'group_end'})
        self.db.add_event(workflow_id, base | {'name': 'after', 'action': 'click'})
        page.current_workflow_id = workflow_id
        page.load_events()
        page.event_tree.setCurrentItem(page.event_tree.topLevelItem(0))
        copy_shortcut, paste_shortcut = page.event_tree._structured_copy_paste_shortcuts
        copy_shortcut.activated.emit()
        paste_shortcut.activated.emit()

        rows = [dict(row) for row in self.db.list_events(workflow_id)]
        self.assertEqual(
            [(row['name'], row['action']) for row in rows],
            [
                ('group', 'group_start'), ('child', 'click'), ('group', 'group_end'),
                ('group', 'group_start'), ('child', 'click'), ('group', 'group_end'),
                ('after', 'click'),
            ],
        )
        self.assertEqual(page.event_tree.topLevelItemCount(), 3)
        self.assertEqual(page.event_tree.topLevelItem(0).childCount(), 1)
        self.assertEqual(page.event_tree.topLevelItem(1).childCount(), 1)

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
        group = page.event_tree.topLevelItem(0)
        group.setExpanded(False)
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
        self.assertTrue(editor.findChild(QFrame, 'executionCard').property('card'))
        self.assertTrue(editor.findChild(QLabel, 'executionTitle').property('cardTitle'))
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

    def test_workflow_group_dialog_matches_flow_editor_card_layout(self) -> None:
        dialog = WorkflowGroupDialog(self.window)
        self.assertEqual(dialog.minimumSize(), dialog.maximumSize())
        self.assertEqual((dialog.width(), dialog.height()), (600, 300))
        self.assertTrue(dialog.findChild(QFrame, 'basicCard').property('card'))
        self.assertTrue(dialog.findChild(QFrame, 'executionCard').property('card'))
        self.assertTrue(dialog.findChild(QLabel, 'basicTitle').property('cardTitle'))
        self.assertTrue(dialog.findChild(QLabel, 'executionTitle').property('cardTitle'))
        self.assertEqual(
            dialog.findChild(QLineEdit, 'nameEdit').placeholderText(),
            '管理するフローのまとまりを入力します',
        )
        dialog.close()

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
        editor.action.setCurrentIndex(editor.action.findData('screenshot'))
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
        for failure_choice, expected_continue in (
            ('stop', 0), ('continue', 1), ('refresh', 0), ('goto', 0),
        ):
            editor.failure_action.setCurrentIndex(editor.failure_action.findData(failure_choice))
            self.assertEqual(
                editor.result_data()['continue_on_error'], expected_continue, failure_choice,
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

    def test_event_form_labels_use_common_optical_vertical_alignment(self) -> None:
        editor = EventEditorDialog(self.window.pages['design'], {
            'name': 'click', 'action': 'click', 'selector_type': 'css',
            'selector': '#target', 'fallback_selector_type': 'none',
            'fallback_selector': '', 'value': '',
        })
        editor.show()
        self.app.processEvents()
        for form_name in ('eventFormBasic', 'locatorForm', 'executionForm'):
            form = editor.findChild(QFormLayout, form_name)
            for row in range(form.rowCount()):
                item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                label = item.widget() if item is not None else None
                if isinstance(label, QLabel):
                    self.assertEqual(label.contentsMargins().top(), 2)
        editor.close()

    def test_event_editor_prepares_both_tab_layouts_before_first_switch(self) -> None:
        editor = EventEditorDialog(self.window.pages['design'], {
            'name': 'click', 'action': 'click', 'selector_type': 'css',
            'selector': '#target', 'value': '',
        })
        changes: list[int] = []
        editor.left_tabs.currentChanged.connect(changes.append)
        original = editor.left_tabs.currentIndex()

        editor.show()
        self.app.processEvents()

        self.assertTrue(editor._tab_layouts_prepared)
        self.assertEqual(editor.left_tabs.currentIndex(), original)
        self.assertEqual(changes, [])
        for index in range(editor.left_tabs.count()):
            page = editor.left_tabs.widget(index)
            self.assertEqual(page.size(), editor.left_tabs.currentWidget().size())
        editor.close()

    def test_click_event_round_trips_its_success_condition(self) -> None:
        config = {
            'condition': 'operable', 'selector_type': 'css',
            'target': '.save-complete', 'iframe_path': '["iframe.result"]',
        }
        editor = EventEditorDialog(self.window.pages['design'], {
            'name': 'click', 'action': 'click', 'selector_type': 'css',
            'selector': '#save', 'value': json.dumps(config),
        })
        self.assertEqual(editor.click_success_condition.currentData(), 'operable')
        self.assertEqual(editor.click_success_selector_type.currentText(), 'css')
        self.assertEqual(editor.click_success_target.text(), '.save-complete')
        self.assertEqual(editor.click_success_iframe_path.text(), '["iframe.result"]')
        result = editor.result_data()
        self.assertEqual(result['value'], '')
        self.assertEqual(json.loads(result['success_json']), config)
        editor.click_success_condition.setCurrentIndex(
            editor.click_success_condition.findData('none')
        )
        self.assertEqual(editor.result_data()['success_json'], '')
        editor.close()

    def test_success_iframe_input_follows_element_condition_and_stays_aligned(self) -> None:
        editor = EventEditorDialog(self.window.pages['design'], {
            'name': 'click', 'action': 'click', 'selector_type': 'css',
            'selector': '#save', 'value': '',
        })
        editor.left_tabs.setCurrentIndex(1)
        self.app.processEvents()

        self.assertEqual(editor.click_success_condition.currentData(), 'none')
        self.assertEqual(editor.click_success_iframe_path.text(), '')
        self.assertFalse(editor.click_success_iframe_path.isVisibleTo(editor))
        self.assertFalse(
            editor.findChild(QLabel, 'clickSuccessIframeLabel').isVisibleTo(editor)
        )
        editor.click_success_condition.setCurrentIndex(
            editor.click_success_condition.findData('visible')
        )
        self.app.processEvents()
        self.assertTrue(editor.click_success_iframe_path.isVisibleTo(editor))
        self.assertTrue(
            editor.findChild(QLabel, 'clickSuccessIframeLabel').isVisibleTo(editor)
        )
        self.assertEqual(
            editor.click_success_iframe_path.geometry().width(),
            editor.click_success_target.geometry().width(),
        )
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
        self.assertEqual(editor.click_success_iframe_path.text(), 'css=iframe.result')
        editor.click_success_iframe_path.setText('["iframe.manual"]')
        self.assertEqual(
            json.loads(editor.result_data()['success_json'])['iframe_path'], '["iframe.manual"]',
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

    def test_select_first_internal_value_is_not_shown_in_the_editor(self) -> None:
        editor = EventEditorDialog(self.window.pages['design'], {
            'name': 'first', 'action': 'select', 'selector_type': 'css',
            'selector': '#target', 'value': '__WEBFLOW_SELECT_FIRST__',
        })
        self.assertEqual(editor.value.text(), '')
        self.assertTrue(editor.value_action_button.isHidden())
        trailing = editor.findChild(QWidget, 'valueTrailing')
        self.assertIsNotNone(trailing)
        self.assertFalse(trailing.isHidden())
        editor.show()
        QApplication.processEvents()
        value_right = editor.value.mapTo(editor, editor.value.rect().topRight()).x()
        action_right = editor.action.mapTo(editor, editor.action.rect().topRight()).x()
        self.assertEqual(value_right, action_right)
        self.assertTrue(editor.value_action_button.property('selected'))
        self.assertEqual(editor.result_data()['value'], '__WEBFLOW_SELECT_FIRST__')
        editor.close()

    def test_execute_until_event_appends_to_daily_file(self) -> None:
        """「直前まで実行」の詳細ログも実行管理と同じ日次ファイルへ保存する。"""
        editor = EventEditorDialog(self.window.pages['design'], insert_at=0)
        editor._run_debug = lambda _working, operation, _success, log_queue=None: operation()

        def execute_until(*_args, logger, **_kwargs):
            logger('直前実行ログテスト')

        with (
            patch.object(editor._service_host, 'debug_jobs', return_value=[]),
            patch.object(editor._service_host.debug_browser, 'execute_until', side_effect=execute_until),
        ):
            editor.execute_until_event()

        log_path = self.project_dir / 'log' / f'{datetime.now():%Y-%m-%d}.log'
        self.assertIn('直前実行ログテスト', log_path.read_text(encoding='utf-8'))
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

    def test_screenshot_picker_accepts_visible_container_without_hit_test(self) -> None:
        """子要素に覆われたキャプチャーコンテナーも、一意かつ可視なら選択できる。"""
        picker = ElementPicker()
        match = Mock()
        match.is_visible.return_value = True
        info = {'tag': 'div', 'css': '#capture', 'xpath': '//*[@id="capture"]'}
        with (
            patch.object(picker, '_matches_in_context', return_value=[match]),
            patch.object(picker, '_actionable_matches_in_context') as actionable,
            patch.object(picker, '_iframe_path', return_value=''),
        ):
            result = picker._choose_unique_locator(Mock(), info, Mock(), 'screenshot')

        self.assertEqual(result['selector'], '#capture')
        actionable.assert_not_called()

    def test_screenshot_picker_saves_explicit_scroll_target(self) -> None:
        """スクリーンショット選択ではキャプチャー範囲とスクロール要素を別々に保存する。"""
        editor = EventEditorDialog(self.window.pages['design'])
        editor.action.setCurrentIndex(editor.action.findData('screenshot'))
        editor._run_debug = lambda _message, operation, done: done(operation())
        target = {
            'selector_type': 'css', 'selector': '#capture',
            'fallback_selector_type': 'none', 'fallback_selector': '',
            'iframe_path': '["iframe"]', 'display': 'Capture',
        }
        scroll = {
            'selector_type': 'css', 'selector': '.scroller',
            'fallback_selector_type': 'none', 'fallback_selector': '',
            'iframe_path': '["iframe"]', 'display': 'Scroller',
        }
        with patch.object(
            editor._service_host.debug_browser, 'pick',
            side_effect=[target, scroll],
        ) as pick:
            editor.pick_element()

        self.assertEqual(pick.call_count, 2)
        self.assertTrue(pick.call_args_list[1].kwargs['require_scroll'])
        saved = json.loads(editor.result_data()['scroll_json'])
        self.assertEqual(saved['selector'], '.scroller')
        editor.close()

    def test_screenshot_picker_allows_skipping_scroll_target(self) -> None:
        """第二段階を Esc で省略した場合はキャプチャー対象自身を使用する。"""
        editor = EventEditorDialog(self.window.pages['design'])
        editor.action.setCurrentIndex(editor.action.findData('screenshot'))
        editor._run_debug = lambda _message, operation, done: done(operation())
        target = {
            'selector_type': 'css', 'selector': '#capture',
            'fallback_selector_type': 'none', 'fallback_selector': '',
            'iframe_path': '', 'display': 'Capture',
        }
        with patch.object(
            editor._service_host.debug_browser, 'pick',
            side_effect=[target, RuntimeError('event.element_selection_cancelled')],
        ):
            editor.pick_element()

        self.assertEqual(editor.result_data()['scroll_json'], '')
        self.assertEqual(editor.selector.text(), '#capture')
        editor.close()

    def test_closing_event_editor_cancels_pending_element_selection(self) -> None:
        """閉じた編集画面の選択待機が、次回選択で画面を復活させない。"""
        editor = EventEditorDialog(self.window.pages['design'])
        editor.show()
        with patch.object(editor._service_host.debug_browser, 'cancel_selection') as cancel:
            editor.close()
        cancel.assert_called_once_with()
        self.assertTrue(editor._closing)

        with patch.object(editor, 'showNormal') as show:
            editor._restore_editor_focus()
        show.assert_not_called()

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
        delete_button = data.findChild(QPushButton, 'deleteRecordButton')
        self.assertFalse(delete_button.isHidden())
        self.assertTrue(delete_button.property('danger'))
        self.assertEqual(delete_button.text(), '削除')
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
            'Excel を読み込む（データ構造を含む）',
        ])
        self.assertFalse(data.findChild(QPushButton, 'importDataJsonButton').isVisibleTo(data))
        self.assertEqual(data.values.columnCount(), 4)
        self.assertEqual(data.values.headerItem().text(2), '値  ✎')
        value_toggle = data.findChild(QPushButton, 'valueToggleButton')
        self.assertTrue(value_toggle.isHidden())
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
        self.assertFalse(value_toggle.isHidden())
        self.assertEqual(value_toggle.text(), '')
        self.assertFalse(value_toggle.icon().isNull())
        self.assertFalse(
            value_toggle.icon().pixmap(QSize(18, 18), QIcon.Mode.Disabled).isNull()
        )
        self.assertEqual(value_toggle.toolTip(), 'すべて折りたたむ')
        value_toggle.click()
        self.assertEqual(value_toggle.toolTip(), 'すべて展開')
        data.render_values()
        self.assertFalse(data.values.topLevelItem(0).isExpanded())
        self.assertEqual(value_toggle.toolTip(), 'すべて展開')
        self.window.show_page('schema')
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

    def test_schema_copy_paste_uses_selected_same_level(self) -> None:
        page = self.window.pages['schema']
        page.schema = {
            'name': 'Data', 'type': 'object', 'children': [
                {'name': 'container', 'type': 'object', 'children': [
                    {'name': 'child', 'type': 'text'},
                ]},
                {'name': 'tail', 'type': 'text'},
            ],
        }
        page.render()
        page.tree.setCurrentItem(page.tree.topLevelItem(0))
        copy_shortcut, paste_shortcut = page.tree._structured_copy_paste_shortcuts
        copy_shortcut.activated.emit()
        paste_shortcut.activated.emit()

        children = page.schema['children']
        self.assertEqual([node['name'] for node in children], [
            'container', 'container - Copy', 'tail',
        ])
        self.assertEqual(children[1]['children'], [{'name': 'child', 'type': 'text'}])

    def test_data_record_copy_paste_preserves_settings_and_inserts_after_selected(self) -> None:
        page = self.window.pages['data']
        first_id = self.db.add_data_record(0, 'PCL_001', {'value': 'first'}, 'summary')
        second_id = self.db.add_data_record(0, 'PCL_002', {'value': 'second'})
        self.db.set_data_record_group(first_id, '7')
        self.db.set_data_record_enabled(first_id, False)
        page.reload(first_id)
        copy_shortcut, paste_shortcut = page.tree._structured_copy_paste_shortcuts
        copy_shortcut.activated.emit()
        paste_shortcut.activated.emit()

        records = self.db.list_data_records()
        self.assertEqual([record['id'] for record in records][::2], [first_id, second_id])
        copied = records[1]
        self.assertEqual(copied['name'], 'PCL_001 - Copy')
        self.assertEqual(copied['summary'], 'summary')
        self.assertEqual(copied['execution_group'], '7')
        self.assertFalse(copied['enabled'])
        self.assertEqual(copied['data'], {'value': 'first'})

    def test_execution_page_restores_status_log_and_record_controls(self) -> None:
        page = self.window.pages['execution']
        page.append_log(f'{tr("error.wait_condition_not_met_prefix")}visible')
        self.assertEqual(page._log_lines.pop(), f'{tr("error.wait_condition_not_met_prefix")}visible')
        self.assertEqual(page.stack.count(), 2)
        self.assertEqual(page.records.columnCount(), 8)
        self.assertEqual(
            [page.records.headerItem().text(column) for column in range(8)],
            ['操作', 'グループ', '今回実行', '実行データ', '概要', '実行状況', '業務フロー', '現在のイベント'],
        )
        self.assertEqual(
            [page.records.columnWidth(column) for column in range(8)],
            [84, 68, 90, 190, 210, 155, 200, 215],
        )
        page._sort_by_column(1, Qt.KeyboardModifier.NoModifier)
        page._sort_by_column(5, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(page.records.headerItem().text(1), 'グループ')
        self.assertEqual(page.records.headerItem().text(5), '実行状況')
        self.assertEqual(page.records.header().sort_indicator(1), (1, Qt.SortOrder.AscendingOrder))
        self.assertEqual(page.records.header().sort_indicator(5), (2, Qt.SortOrder.AscendingOrder))
        page._sort_by_column(5, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(page.records.header().sort_indicator(5), (2, Qt.SortOrder.DescendingOrder))
        page._sort_by_column(5, Qt.KeyboardModifier.ShiftModifier)
        page._sort_by_column(1, Qt.KeyboardModifier.NoModifier)
        page._sort_by_column(1, Qt.KeyboardModifier.NoModifier)
        self.assertEqual(page._sort_criteria, [])
        self.assertEqual(page.records.headerItem().text(0), '操作')
        self.assertEqual(page.records.toolTip(), '')
        self.assertEqual(page.records.header().minimumSectionSize(), 54)
        enabled_records = [record for record in self.db.list_data_records() if record['enabled']]
        completed = sum(
            record['execution_status'] in {'stopped', 'success', 'failed'}
            for record in enabled_records
        )
        self.assertEqual(page.progress.maximum(), max(1, len(enabled_records)))
        self.assertEqual(page.progress_count.text(), f'{completed} / {len(enabled_records)}')
        self.assertTrue(page.findChild(QPushButton, 'clearResultsButton').property('danger'))
        self.assertEqual(STATUS_LABELS['not_run'], '未実行')
        self.assertEqual(STATUS_LABELS['running'], '実行中')
        self.assertEqual(STATUS_LABELS['error_waiting'], 'エラー確認中')
        self.assertEqual(STATUS_LABELS['stopped'], '中止')
        self.assertEqual(STATUS_LABELS['skipped'], 'スキップ')
        self.assertIsNotNone(page.findChild(QPushButton, 'toggleExecutionButton'))
        self.assertIsNotNone(page.findChild(QPushButton, 'setGroupButton'))
        self.assertIsNotNone(page.findChild(QPushButton, 'setOrderButton'))
        self.assertEqual(
            page.records.selectionMode(), QAbstractItemView.SelectionMode.ExtendedSelection,
        )
        self.assertEqual(
            [action.text() for action in page.toggle_button.menu().actions()],
            ['実行に設定', 'スキップに設定', '選択を反転'],
        )
        self.assertIsInstance(page.records.itemDelegateForColumn(0), ExecutionActionDelegate)
        self.assertTrue(all(
            page.records.itemWidget(page.records.topLevelItem(index), 0) is None
            for index in range(page.records.topLevelItemCount())
        ))
        order_dialog = ExecutionOrderDialog(self.window, self.db)
        groups = [
            order_dialog.group_combo.itemText(index)
            for index in range(order_dialog.group_combo.count())
        ]
        self.assertEqual(groups, sorted(groups, key=natural_sort_key))
        order_dialog.close()
        self.assertIsNotNone(page.findChild(QPushButton, 'clearResultsButton'))
        called = []
        original_set_group = page.set_group
        original_toggle_record = page.toggle_record
        page.set_group = lambda: called.append('group')
        page.toggle_record = lambda: called.append('toggle')
        page._record_double_clicked(None, 7)
        self.assertEqual(called, [])
        page._record_double_clicked(None, 1)
        self.assertEqual(called, ['group'])
        page._record_double_clicked(None, 2)
        self.assertEqual(called, ['group', 'toggle'])
        page.set_group = original_set_group
        page.toggle_record = original_toggle_record
        page.select_tab(1)
        self.assertEqual(page.stack.currentIndex(), 1)
        page.select_tab(0)
        self.assertEqual(page.stack.currentIndex(), 0)

    def test_execution_order_defaults_to_selected_group(self) -> None:
        page = self.window.pages['execution']
        group_10_id = self.db.add_data_record(0, 'order group 10', {})
        group_2_id = self.db.add_data_record(0, 'order group 2', {})
        self.db.set_data_records_group([group_10_id], '10')
        self.db.set_data_records_group([group_2_id], '2')
        try:
            page._table_signature = None
            page.reload()

            def select_records(*record_ids: int) -> None:
                page.records.clearSelection()
                selected = set(record_ids)
                for index in range(page.records.topLevelItemCount()):
                    item = page.records.topLevelItem(index)
                    item.setSelected(
                        int(item.data(0, Qt.ItemDataRole.UserRole)) in selected
                    )

            # 単一選択時は、その行のグループを初期表示する。
            select_records(group_10_id)
            with patch('qt_ui.pages.execution.ExecutionOrderDialog') as dialog_class:
                dialog_class.return_value.exec.return_value = QDialog.DialogCode.Rejected
                page.set_order()
                dialog_class.assert_called_once_with(page, self.db, '10')

            # 複数グループ選択時は、自然順で最小のグループを初期表示する。
            select_records(group_10_id, group_2_id)
            with patch('qt_ui.pages.execution.ExecutionOrderDialog') as dialog_class:
                dialog_class.return_value.exec.return_value = QDialog.DialogCode.Rejected
                page.set_order()
                dialog_class.assert_called_once_with(page, self.db, '2')
        finally:
            self.db.delete_data_record(0, group_10_id)
            self.db.delete_data_record(0, group_2_id)
            page._table_signature = None
            page.reload()

    def test_execution_order_hides_skipped_rows_and_preserves_their_slots(self) -> None:
        group = 'order-visible-only'
        first_id = self.db.add_data_record(0, 'enabled first', {})
        skipped_id = self.db.add_data_record(0, 'skipped middle', {})
        last_id = self.db.add_data_record(0, 'enabled last', {})
        record_ids = [first_id, skipped_id, last_id]
        self.db.set_data_records_group(record_ids, group)
        self.db.set_data_record_enabled(skipped_id, False)
        dialog = None
        try:
            dialog = ExecutionOrderDialog(self.window, self.db, group)
            visible_ids = [
                int(dialog.tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole))
                for index in range(dialog.tree.topLevelItemCount())
            ]
            self.assertEqual(visible_ids, [first_id, last_id])

            move_up = dialog.findChild(QPushButton, 'moveUpButton')
            move_down = dialog.findChild(QPushButton, 'moveDownButton')
            self.assertIsNotNone(move_up)
            self.assertIsNotNone(move_down)
            self.assertIn('実行データ', move_up.toolTip())

            # 上下ボタンは選択を維持し、画面上の連番も直ちに更新する。
            dialog.tree.setCurrentItem(dialog.tree.topLevelItem(1))
            move_up.click()
            self.assertEqual(
                int(dialog.tree.currentItem().data(0, Qt.ItemDataRole.UserRole)), last_id,
            )
            self.assertTrue(dialog.tree.topLevelItem(0).text(0).startswith('(1/2) '))
            self.assertTrue(dialog.tree.topLevelItem(1).text(0).startswith('(2/2) '))
            move_down.click()
            self.assertEqual(
                int(dialog.tree.topLevelItem(1).data(0, Qt.ItemDataRole.UserRole)), last_id,
            )
            move_up.click()

            # 実行対象だけを入れ替え、スキップ行の位置は維持する。
            dialog._save()
            ordered_group_ids = [
                int(record['id']) for record in self.db.list_data_records()
                if str(record['execution_group']) == group
            ]
            self.assertEqual(ordered_group_ids, [last_id, skipped_id, first_id])
        finally:
            if dialog is not None:
                dialog.close()
            for record_id in record_ids:
                self.db.delete_data_record(0, record_id)

    def test_settings_page_uses_compact_balanced_cards(self) -> None:
        page = self.window.pages['settings']
        display_card = page.findChild(QFrame, 'displayCard')
        runtime_card = page.findChild(QFrame, 'runtimeCard')
        self.assertEqual((display_card.height(), runtime_card.height()), (380, 380))
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
        self.assertEqual(page.action_stable.value(), 250)
        self.assertEqual(page.action_stable.suffix(), ' ms')

    def test_settings_edits_are_pending_until_single_save(self) -> None:
        page = self.window.pages['settings']
        stored_url = self.db.get_start_url()
        page.start_url.setText('https://example.com/pending')
        page.font_size.setCurrentText('8')
        page.action_stable.setValue(100)
        self.assertTrue(page.has_pending_changes())
        self.assertEqual(self.db.get_start_url(), stored_url)
        self.assertTrue(page.save_all(show_message=False))
        self.assertEqual(self.db.get_start_url(), 'https://example.com/pending')
        self.assertEqual(self.db.get_action_stable_ms(), 100)
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

    def test_escape_on_embedded_ui_dialog_closes_outer_dialog(self) -> None:
        """内側の .ui だけを閉じて、空白の外側ダイアログを残さない。"""
        dialog = ConfirmationDialog(self.window, '確認', '閉じますか？')
        embedded = dialog.findChildren(QDialog)
        self.assertEqual(len(embedded), 1)
        dialog.show()
        QApplication.processEvents()

        QTest.keyClick(embedded[0], Qt.Key.Key_Escape)
        QApplication.processEvents()

        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertFalse(dialog.isVisible())


if __name__ == '__main__':
    unittest.main()
