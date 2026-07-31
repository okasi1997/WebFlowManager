import unittest

from core.executor import WorkflowExecutor


class DataReferenceTests(unittest.TestCase):
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
