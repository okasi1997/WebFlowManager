import json
import tempfile
import unittest
from pathlib import Path

from core.database import Database
from core.settings import SUPPORTED_ACTIONS, SUPPORTED_SELECTOR_TYPES


class WaitMigrationTests(unittest.TestCase):
    def test_action_stable_time_defaults_to_250_and_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / 'test.db')
            self.assertEqual(database.get_action_stable_ms(), 250)
            database.set_action_stable_ms(0)
            self.assertEqual(database.get_action_stable_ms(), 0)
            database.close()

    def test_new_database_persists_visible_browser_as_first_run_default(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / 'test.db')
            stored = database.connection.execute(
                "SELECT value FROM app_meta WHERE key='browser_visible'"
            ).fetchone()['value']
            database.close()

        self.assertEqual(stored, '1')

    def test_new_database_starts_with_an_empty_data_schema(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / 'test.db')
            schema = database.get_data_schema()
            database.close()

        self.assertEqual(schema, {'name': 'Data', 'type': 'object', 'children': []})

    def test_existing_wait_hidden_event_is_migrated_when_database_opens(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.db'
            database = Database(path)
            workflow_id = database.add_workflow('test')
            database.add_event(workflow_id, {
                'name': 'hidden', 'action': 'wait_hidden', 'selector_type': 'css',
                'selector': '.spinner', 'value': '', 'timeout_ms': 1000,
                'enabled': 1, 'continue_on_error': 0,
            })
            database.close()

            database = Database(path)
            event = dict(database.list_events(workflow_id)[0])
            database.close()

        self.assertEqual(event['action'], 'wait')
        self.assertEqual(event['value'], 'hidden')

    def test_import_converts_legacy_wait_hidden_before_validation(self) -> None:
        payload = {
            'version': 2,
            'type': 'web-flow-collection',
            'workflows': [{
                'name': 'test',
                'events': [{
                    'name': 'hidden', 'action': 'wait_hidden',
                    'selector_type': 'css', 'selector': '.spinner', 'value': '',
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as folder:
            folder_path = Path(folder)
            source = folder_path / 'legacy.json'
            source.write_text(json.dumps(payload), encoding='utf-8')
            database = Database(folder_path / 'test.db')
            database.import_workflow_collection(source, SUPPORTED_ACTIONS, SUPPORTED_SELECTOR_TYPES)
            workflow = database.list_workflows()[0]
            event = dict(database.list_events(workflow['id'])[0])
            database.close()

        self.assertNotIn('wait_hidden', SUPPORTED_ACTIONS)
        self.assertEqual(event['action'], 'wait')
        self.assertEqual(event['value'], 'hidden')

    def test_existing_detailed_wait_condition_is_combined_as_operable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.db'
            database = Database(path)
            workflow_id = database.add_workflow('test')
            database.add_event(workflow_id, {
                'name': 'editable', 'action': 'wait', 'selector_type': 'css',
                'selector': 'input', 'value': 'editable', 'timeout_ms': 1000,
                'enabled': 1, 'continue_on_error': 0,
            })
            database.close()

            database = Database(path)
            event = dict(database.list_events(workflow_id)[0])
            database.close()

        self.assertEqual(event['action'], 'wait')
        self.assertEqual(event['value'], 'operable')


if __name__ == '__main__':
    unittest.main()
