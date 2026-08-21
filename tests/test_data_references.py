import unittest
from pathlib import Path

from core.conditions import evaluate_guard
from core.executor import WorkflowExecutor


class DataReferenceTests(unittest.TestCase):
    def test_template_presence_guard_distinguishes_empty_instance_from_missing(self) -> None:
        root_data = {
            '_template_instances': [{
                'template_id': 'product',
                'template_name': '商品',
                'data': {},
            }],
        }
        resolver = lambda path: WorkflowExecutor._resolve_guard_data(root_data, path, {})

        self.assertTrue(evaluate_guard({
            'logic': 'all',
            'rules': [{'path': '@template.商品', 'operator': 'exists', 'value': ''}],
        }, resolver))
        self.assertTrue(evaluate_guard({
            'logic': 'all',
            'rules': [{'path': '@template.未設定', 'operator': 'not_exists', 'value': ''}],
        }, resolver))

    def test_workflow_guard_check_includes_inherited_group_guards(self) -> None:
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        requires_template = {
            'logic': 'all',
            'rules': [{
                'path': '@template.試算基本情報',
                'operator': 'exists',
                'value': '',
            }],
        }
        step = {
            # The legacy direct guard is empty, while the inherited group guard
            # is carried in guards. Debug execution must not ignore it.
            'guard': None,
            'guards': [requires_template, None],
        }
        configured = {
            '_template_instances': [{
                'template_id': 'estimate-basic',
                'template_name': '試算基本情報',
                'data': {},
            }],
        }

        self.assertTrue(executor._workflow_guards_pass(step, configured))
        self.assertFalse(executor._workflow_guards_pass(step, {}))

    def test_replaces_multiple_scalar_references(self) -> None:
        data = {
            'PCL_NO': 'PCL_001',
            'output': {'contract_id': 42},
        }

        actual = WorkflowExecutor._substitute_data_references(
            '/pcl/${data:PCL_NO}/contract/${data:output.contract_id}',
            data,
            {},
        )

        self.assertEqual(actual, '/pcl/PCL_001/contract/42')

    def test_uses_current_loop_item(self) -> None:
        data = {'plans': [{'name': 'standard'}, {'name': 'premium'}]}

        actual = WorkflowExecutor._substitute_data_references(
            "//a[@data-plan='${data:plans.name}']",
            data,
            {'plans': data['plans'][1]},
        )

        self.assertEqual(actual, "//a[@data-plan='premium']")

    def test_rejects_non_scalar_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, 'must point to a scalar'):
            WorkflowExecutor._substitute_data_references(
                '${data:output}',
                {'output': {'contract_id': 42}},
                {},
            )

    def test_leaves_runtime_variable_tokens_untouched(self) -> None:
        actual = WorkflowExecutor._substitute_data_references(
            '${runtime_value}/${data:PCL_NO}',
            {'PCL_NO': 'PCL_001'},
            {},
        )

        self.assertEqual(actual, '${runtime_value}/PCL_001')


if __name__ == '__main__':
    unittest.main()
