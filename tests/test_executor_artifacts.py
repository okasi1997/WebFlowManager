import json
import unittest
from datetime import datetime

from core.executor import WorkflowExecutor


class ExecutorArtifactNameTests(unittest.TestCase):
    def test_multi_path_event_log_detail_does_not_repeat_selector_json(self) -> None:
        selector = {
            'version': 1,
            'steps': [
                {'kind': 'scope', 'value': 'service'},
                {'kind': 'target', 'value': 'quantity'},
            ],
            'resolved': {'selector_type': 'xpath', 'selector': '//input'},
            'parameters': {'1': {'resolved_value': 'very long value'}},
        }

        detail = WorkflowExecutor._event_log_detail({
            'action': 'fill', 'selector_type': 'path',
            'selector': json.dumps(selector), 'value': '2',
        }, {})

        self.assertEqual(detail, 'fill path=multi-path(2 steps) value="2"')
        self.assertNotIn('resolved', detail)

    def test_single_event_context_uses_first_item_of_each_parent_group(self) -> None:
        first_service = {
            'instance_id': 'service-1', 'template_id': 'microsoft365',
            'data': {'plans': [{'name': 'Basic'}, {'name': 'Premium'}]},
        }
        data = {
            'services': [first_service, {
                'instance_id': 'service-2', 'template_id': 'microsoft365',
                'data': {'plans': [{'name': 'Enterprise'}]},
            }],
        }

        context = WorkflowExecutor.first_item_loop_context(
            data, ['services', '@template.microsoft365.plans'],
        )

        self.assertIs(context['services'], first_service)
        self.assertEqual(
            context['@template.microsoft365.plans'], {'name': 'Basic'},
        )
        self.assertEqual(
            WorkflowExecutor._resolve_data(
                data, '@template.microsoft365.plans.name', context,
            ),
            'Basic',
        )

    def test_single_event_context_rejects_empty_parent_group(self) -> None:
        with self.assertRaisesRegex(ValueError, '空'):
            WorkflowExecutor.first_item_loop_context(
                {'services': []}, ['services'],
            )

    def test_top_level_template_is_resolved_as_an_object(self) -> None:
        data = {'_template_instances': [
            {'template_id': 'product', 'data': {'name': 'A'}},
            {'template_id': 'other', 'data': {'name': 'C'}},
        ]}

        product = WorkflowExecutor._resolve_data(data, '@template.product', {})

        self.assertEqual(product, {'name': 'A'})
        self.assertEqual(
            WorkflowExecutor._resolve_data(data, '@template.product.name', {}),
            'A',
        )

    def test_template_instance_inside_list_is_resolved_by_virtual_path(self) -> None:
        data = {'services': [{
            'instance_id': 'nested-1', 'template_id': 'product',
            'name': 'Product 1', 'data': {'name': 'Nested'},
        }]}

        self.assertEqual(
            WorkflowExecutor._resolve_data(data, '@template.product', {}),
            [{'name': 'Nested'}],
        )

    def test_template_inside_current_outer_list_item_is_resolved_as_object(self) -> None:
        """外側リストの現在要素にあるテンプレートは単一 object として扱う。"""
        first_service = {
            'instance_id': 'service-1', 'template_id': 'microsoft365',
            'data': {'plans': [{'name': 'Basic'}, {'name': 'Premium'}]},
        }
        second_service = {
            'instance_id': 'service-2', 'template_id': 'microsoft365',
            'data': {'plans': [{'name': 'Enterprise'}]},
        }
        data = {'services': [first_service, second_service]}
        context = {'services': first_service}

        self.assertEqual(
            WorkflowExecutor._resolve_data(
                data, '@template.microsoft365.plans', context,
            ),
            [{'name': 'Basic'}, {'name': 'Premium'}],
        )

    def test_nested_template_list_uses_current_plan_item(self) -> None:
        """テンプレート内リストの子項目は内側ループの現在値から取得する。"""
        service = {
            'instance_id': 'service-1', 'template_id': 'microsoft365',
            'data': {'plans': [{'name': 'Basic'}, {'name': 'Premium'}]},
        }
        data = {'services': [service]}
        context = {
            'services': service,
            '@template.microsoft365.plans': service['data']['plans'][1],
        }

        self.assertEqual(
            WorkflowExecutor._resolve_data(
                data, '@template.microsoft365.plans.name', context,
            ),
            'Premium',
        )

    def test_assign_data_uses_template_in_current_outer_list_item(self) -> None:
        """入力結果も現在の外側要素に属するテンプレートへ書き戻す。"""
        service = {
            'instance_id': 'service-1', 'template_id': 'microsoft365',
            'data': {'plans': [{'name': 'Basic'}]},
        }
        data = {'services': [service]}
        plan = service['data']['plans'][0]
        context = {
            'services': service,
            '@template.microsoft365.plans': plan,
        }

        WorkflowExecutor._assign_data(
            data, '@template.microsoft365.plans.name', context, 'Updated',
        )

        self.assertEqual(plan['name'], 'Updated')

    def test_template_name_can_be_used_as_virtual_path(self) -> None:
        data = {'_template_instances': [{
            'template_id': 'generated-id', 'template_name': '仮想商材',
            'data': {'name': 'A'},
        }]}

        self.assertEqual(
            WorkflowExecutor._resolve_data(data, '@template.仮想商材', {}),
            {'name': 'A'},
        )

    def test_unsaved_screenshot_event_uses_timestamp_filename(self) -> None:
        """DB ID のない新規イベントも既定名で試行できる。"""
        filename = WorkflowExecutor._screenshot_filename(
            {'action': 'screenshot'}, '', datetime(2026, 8, 13, 19, 5, 15, 123456),
        )

        self.assertEqual(filename, 'screenshot_20260813_190515_123456.png')

    def test_saved_or_named_screenshot_keeps_existing_filename_rules(self) -> None:
        self.assertEqual(
            WorkflowExecutor._screenshot_filename({'id': 12}, ''), 'screenshot_12.png',
        )
        self.assertEqual(
            WorkflowExecutor._screenshot_filename({}, 'manual.png'), 'manual.png',
        )

    def test_parallel_log_prefix_keeps_short_execution_context(self) -> None:
        step = {
            'phase': 'pcl', 'session': 2, 'group': 7,
            'pcl_index': 3, 'pcl_total': 5, 'position': 4,
        }

        step_prefix = WorkflowExecutor._step_log_prefix(step)
        event_prefix = WorkflowExecutor._event_log_prefix(
            step_prefix, {'position': 12}, ['2/3'],
        )

        self.assertEqual(step_prefix, '[S2 G7 D3/5] F4 | ')
        self.assertEqual(event_prefix, '[S2 G7 D3/5] F4 E12 L2/3 | ')

    def test_retry_suffix_does_not_increase_failure_screenshot_name(self) -> None:
        base = WorkflowExecutor._failure_screenshot_name('run_group_1', 12)
        retried = WorkflowExecutor._failure_screenshot_name(
            'run_group_1_retry_1_retry_2_retry_3', 12,
        )

        self.assertEqual(retried, base)

    def test_long_failure_screenshot_name_is_bounded_and_stays_unique(self) -> None:
        first = WorkflowExecutor._failure_screenshot_name('a' * 300, 12)
        second = WorkflowExecutor._failure_screenshot_name('a' * 299 + 'b', 12)

        self.assertLessEqual(len(first), 100)
        self.assertTrue(first.endswith('.png'))
        self.assertNotEqual(first, second)


if __name__ == '__main__':
    unittest.main()
