from __future__ import annotations

import unittest
from types import SimpleNamespace
import tempfile
from pathlib import Path

from core.database import Database
from ui.structured_data import HierarchicalDataDialog, SchemaDesignerDialog


class DataEditorReloadTests(unittest.TestCase):
    def test_reload_discards_stale_editor_copy_before_syncing(self) -> None:
        dialog = object.__new__(HierarchicalDataDialog)
        dialog.workflow_id = 0
        dialog.db = SimpleNamespace(get_data_schema=lambda _workflow_id: {'type': 'object', 'children': []})
        dialog.current_id = 17
        dialog.current_name = 'old'
        dialog.current_summary = 'old summary'
        dialog.current_data = {'stale': 'value'}
        calls = []
        dialog._sync_all_records = lambda show_message=True: calls.append(
            ('sync', dialog.current_id, show_message)
        )
        dialog._refresh_records = lambda selected_id=None: calls.append(('refresh', selected_id))

        dialog.reload_from_database()

        self.assertEqual(dialog.current_data, {})
        self.assertEqual(calls, [('sync', None, False), ('refresh', 17)])


class SchemaDesignerOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first = {'name': 'first', 'type': 'text'}
        self.second = {'name': 'second', 'type': 'text'}
        self.third = {'name': 'third', 'type': 'text'}
        self.children = [self.first, self.second, self.third]

    def test_drag_before_target(self) -> None:
        changed = SchemaDesignerDialog._reorder(self.children, self.third, self.first, after=False)
        self.assertTrue(changed)
        self.assertEqual(self.children, [self.third, self.first, self.second])

    def test_drag_after_target(self) -> None:
        changed = SchemaDesignerDialog._reorder(self.children, self.first, self.third, after=True)
        self.assertTrue(changed)
        self.assertEqual(self.children, [self.second, self.third, self.first])

    def test_dragging_onto_itself_does_nothing(self) -> None:
        changed = SchemaDesignerDialog._reorder(self.children, self.second, self.second, after=False)
        self.assertFalse(changed)
        self.assertEqual(self.children, [self.first, self.second, self.third])

    def test_drop_inside_object_moves_node_to_children(self) -> None:
        container = {'name': 'output', 'type': 'object', 'children': []}
        root = {'name': 'Data', 'type': 'list', 'children': [self.first, container]}
        changed = SchemaDesignerDialog._drop_node(root, self.first, root, container, 'inside')
        self.assertTrue(changed)
        self.assertEqual(root['children'], [container])
        self.assertEqual(container['children'], [self.first])

    def test_drop_can_move_child_back_to_root(self) -> None:
        container = {'name': 'output', 'type': 'object', 'children': [self.first]}
        root = {'name': 'Data', 'type': 'list', 'children': [container, self.second]}
        changed = SchemaDesignerDialog._drop_node(container, self.first, root, self.second, 'before')
        self.assertTrue(changed)
        self.assertEqual(root['children'], [container, self.first, self.second])
        self.assertEqual(container['children'], [])

    def test_parent_cannot_be_dropped_into_its_descendant(self) -> None:
        nested = {'name': 'nested', 'type': 'object', 'children': []}
        container = {'name': 'output', 'type': 'object', 'children': [nested]}
        root = {'name': 'Data', 'type': 'list', 'children': [container]}
        changed = SchemaDesignerDialog._drop_node(root, container, container, nested, 'inside')
        self.assertFalse(changed)
        self.assertEqual(root['children'], [container])

    def test_duplicate_name_prevents_cross_parent_drop(self) -> None:
        duplicate = {'name': 'first', 'type': 'number'}
        container = {'name': 'output', 'type': 'object', 'children': [duplicate]}
        root = {'name': 'Data', 'type': 'list', 'children': [self.first, container]}
        changed = SchemaDesignerDialog._drop_node(root, self.first, root, container, 'inside')
        self.assertFalse(changed)
        self.assertEqual(root['children'], [self.first, container])

    def test_add_after_selected_regular_field(self) -> None:
        root = {'name': 'Data', 'type': 'list', 'children': self.children}
        parent, index = SchemaDesignerDialog._add_location(root, (self.second, root))
        self.assertIs(parent, root)
        self.assertEqual(index, 2)

    def test_add_inside_selected_container(self) -> None:
        container = {'name': 'output', 'type': 'object', 'children': [self.first]}
        root = {'name': 'Data', 'type': 'list', 'children': [container]}
        parent, index = SchemaDesignerDialog._add_location(root, (container, root))
        self.assertIs(parent, container)
        self.assertEqual(index, 1)

    def test_add_at_end_without_selection(self) -> None:
        root = {'name': 'Data', 'type': 'list', 'children': self.children}
        parent, index = SchemaDesignerDialog._add_location(root, None)
        self.assertIs(parent, root)
        self.assertEqual(index, 3)

    def test_move_down_leaves_container_at_lower_edge(self) -> None:
        container = {'name': 'output', 'type': 'object', 'children': [self.first]}
        root = {'name': 'Data', 'type': 'object', 'children': [container, self.second]}
        result = SchemaDesignerDialog._move_node(root, self.first, container, 1)
        self.assertEqual(result, 'moved')
        self.assertEqual(container['children'], [])
        self.assertEqual(root['children'], [container, self.first, self.second])

    def test_move_up_leaves_container_at_upper_edge(self) -> None:
        container = {'name': 'output', 'type': 'object', 'children': [self.first]}
        root = {'name': 'Data', 'type': 'object', 'children': [self.second, container]}
        result = SchemaDesignerDialog._move_node(root, self.first, container, -1)
        self.assertEqual(result, 'moved')
        self.assertEqual(root['children'], [self.second, self.first, container])

    def test_move_at_root_edge_is_blocked(self) -> None:
        root = {'name': 'Data', 'type': 'object', 'children': [self.first]}
        result = SchemaDesignerDialog._move_node(root, self.first, root, 1)
        self.assertEqual(result, 'blocked')
        self.assertEqual(root['children'], [self.first])

    def test_move_out_rejects_duplicate_name(self) -> None:
        duplicate = {'name': 'first', 'type': 'number'}
        container = {'name': 'output', 'type': 'object', 'children': [self.first]}
        root = {'name': 'Data', 'type': 'object', 'children': [container, duplicate]}
        result = SchemaDesignerDialog._move_node(root, self.first, container, 1)
        self.assertEqual(result, 'duplicate')
        self.assertEqual(container['children'], [self.first])

    def test_legacy_list_root_is_loaded_as_object(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / 'test.db')
            legacy = {'name': 'Data', 'type': 'list', 'children': [self.first]}
            database.save_data_schema(0, legacy)
            loaded = database.get_data_schema()
            database.close()
        self.assertEqual(loaded['type'], 'object')
        self.assertEqual(loaded['children'], [self.first])


if __name__ == '__main__':
    unittest.main()
