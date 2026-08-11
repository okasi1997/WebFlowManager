import unittest

from core.executor import WorkflowExecutor


class ExecutorArtifactNameTests(unittest.TestCase):
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
