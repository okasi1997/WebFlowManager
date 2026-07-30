from __future__ import annotations

import unittest

from app import FlowManagerApp


class EventInsertionPositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            {'id': 1, 'action': 'click'},
            {'id': 2, 'action': 'group_start'},
            {'id': 3, 'action': 'fill'},
            {'id': 4, 'action': 'group_end'},
            {'id': 5, 'action': 'click'},
        ]

    def test_regular_selection_inserts_after_selected_row(self) -> None:
        self.assertEqual(FlowManagerApp._event_insert_before_id(self.rows, 1), 2)

    def test_container_selection_inserts_before_its_end_boundary(self) -> None:
        self.assertEqual(FlowManagerApp._event_insert_before_id(self.rows, 2), 4)

    def test_no_selection_appends_to_end(self) -> None:
        self.assertIsNone(FlowManagerApp._event_insert_before_id(self.rows, None))


if __name__ == '__main__':
    unittest.main()
