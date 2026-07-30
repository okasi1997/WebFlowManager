from __future__ import annotations

import unittest

from ui.structured_data import SchemaDesignerDialog


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


if __name__ == '__main__':
    unittest.main()
