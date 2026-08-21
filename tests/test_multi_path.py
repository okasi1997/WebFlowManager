from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from browser.element_picker import ElementPicker
from browser.locators import (
    build_locator, multi_path_steps, selector_console_preview, selector_preview,
)
from browser.picker_scripts import picker_script
from core.settings import SUPPORTED_SELECTOR_TYPES
from core.database import Database
from core.executor import (
    find_variables, substitute_event_parameter_value, substitute_event_selector,
    substitute_event_selector_value,
    substitute_path_step_values,
)
from core.executor import WorkflowExecutor


class MultiPathLocatorTests(unittest.TestCase):
    def test_build_locator_reuses_resolved_xpath(self) -> None:
        context = MagicMock()
        selector = json.dumps({
            'version': 1,
            'steps': [{'kind': 'scope', 'display': 'サービス'}],
            'resolved': {'selector_type': 'xpath', 'selector': '//tr//input'},
        })

        result = build_locator(context, 'path', selector)

        context.locator.assert_called_once_with('xpath=//tr//input')
        self.assertIs(result, context.locator.return_value)

    def test_occurrence_rule_selects_first_or_last_case_insensitively(self) -> None:
        context = MagicMock()
        locator = context.locator.return_value
        locator.count.return_value = 3
        selector = {
            'steps': [{'value': 'target'}],
            'occurrence': {'position': 'last', 'index': 3, 'count': 3},
            'resolved': {
                'selector_type': 'xpath',
                'selector': '(//button)[last()]',
            },
        }

        selector['occurrence_rule'] = 'FIRST'
        result = build_locator(context, 'path', json.dumps(selector))
        context.locator.assert_called_with('xpath=//button')
        locator.nth.assert_called_with(0)
        self.assertIs(result, locator.nth.return_value)

        locator.nth.reset_mock()
        selector['occurrence_rule'] = 'Last'
        build_locator(context, 'path', json.dumps(selector))
        locator.nth.assert_called_with(2)

    def test_occurrence_preview_uses_the_edited_one_based_rule(self) -> None:
        selector = {
            'steps': [{'value': 'target'}],
            'occurrence': {'position': 'first', 'index': 1, 'count': 3},
            'resolved': {
                'selector_type': 'xpath',
                'selector': '(//input[@name="price"])[1]',
            },
        }

        selector['occurrence_rule'] = 'first'
        self.assertEqual(
            selector_preview('path', json.dumps(selector)),
            '(//input[@name="price"])[1]',
        )
        selector['occurrence_rule'] = '3'
        self.assertEqual(
            selector_preview('path', json.dumps(selector)),
            '(//input[@name="price"])[3]',
        )
        selector['occurrence_rule'] = 'LAST'
        self.assertEqual(
            selector_preview('path', json.dumps(selector)),
            '(//input[@name="price"])[last()]',
        )

    def test_edited_occurrence_replaces_a_stale_picker_suffix(self) -> None:
        context = MagicMock()
        locator = context.locator.return_value
        locator.count.return_value = 3
        selector = {
            'steps': [{'value': 'price'}],
            # Legacy metadata may disagree with the suffix after an older edit.
            'occurrence': {'position': 'last', 'index': 3, 'count': 3},
            'occurrence_rule': '2',
            'resolved': {
                'selector_type': 'xpath',
                'selector': '(//input[@name="price"])[1]',
            },
        }

        build_locator(context, 'path', json.dumps(selector))

        context.locator.assert_called_once_with('xpath=//input[@name="price"]')
        locator.nth.assert_called_once_with(1)
        self.assertEqual(
            selector_preview('path', json.dumps(selector)),
            '(//input[@name="price"])[2]',
        )

    def test_edited_occurrence_removes_repeated_legacy_picker_suffixes(self) -> None:
        context = MagicMock()
        locator = context.locator.return_value
        locator.count.return_value = 3
        selector = {
            'steps': [{'value': 'price'}],
            'occurrence': {'position': 'first', 'index': 1, 'count': 3},
            'occurrence_rule': '2',
            'resolved': {
                'selector_type': 'xpath',
                'selector': '((//input[@name="price"])[1])[1]',
            },
        }

        build_locator(context, 'path', json.dumps(selector))

        context.locator.assert_called_once_with('xpath=//input[@name="price"]')
        locator.nth.assert_called_once_with(1)
        self.assertEqual(
            selector_preview('path', json.dumps(selector)),
            '(//input[@name="price"])[2]',
        )

    def test_xpath_diagnostic_is_directly_pasteable_in_chrome_console(self) -> None:
        xpath = "//div[contains(., '\u00a0\u3000') and @title=\"price\"]"

        preview = selector_console_preview('xpath', xpath)

        self.assertTrue(preview.startswith('$x("'))
        self.assertIn(r'\u00a0\u3000', preview)
        self.assertIn(r'\"price\"', preview)

    def test_build_locator_renders_edited_step_values(self) -> None:
        context = MagicMock()
        selector = json.dumps({
            'version': 1,
            'steps': [
                {'kind': 'scope', 'value': "O'Reilly"},
                {'kind': 'target', 'value': 'amount'},
            ],
            'resolved': {
                'selector_type': 'xpath',
                'selector': '//tr[contains(.,__WFM_STEP_0__)]//input[@name=__WFM_STEP_1__]',
            },
        })

        build_locator(context, 'path', selector)

        context.locator.assert_called_once_with(
            'xpath=//tr[contains(.,"O\'Reilly")]//input[@name=\'amount\']'
        )

    def test_multi_path_steps_rejects_broken_data(self) -> None:
        self.assertEqual(multi_path_steps('{broken'), [])
        self.assertEqual(multi_path_steps('[]'), [])

    def test_build_locator_maps_source_row_index_to_another_table(self) -> None:
        context = MagicMock()
        source = MagicMock()
        source.count.return_value = 1
        source.nth.return_value.evaluate.return_value = 2
        table = MagicMock()
        table.count.return_value = 1
        rows = MagicMock()
        rows.count.return_value = 4
        target_row = MagicMock()
        rows.nth.return_value = target_row
        table.nth.return_value.locator.return_value = rows
        context.locator.side_effect = [source, table]
        expected = target_row.locator.return_value
        selector = json.dumps({
            'version': 1,
            'steps': [],
            'row_mapping': {
                'operation': 'same_row_index',
                'source': {'selector_type': 'xpath', 'selector': '//table[1]//tr[3]'},
                'target_table': {'selector_type': 'xpath', 'selector': '//table[2]'},
                'row_selector': ':scope > tbody > tr, :scope > tr',
                'target': {'selector_type': 'xpath', 'selector': './/input'},
            },
        })

        result = build_locator(context, 'path', selector)

        self.assertIs(result, expected)
        rows.nth.assert_called_once_with(2)
        target_row.locator.assert_called_once_with('xpath=.//input')

    def test_build_locator_uses_the_only_target_row_regardless_of_source_index(self) -> None:
        context = MagicMock()
        source = MagicMock()
        source.count.return_value = 1
        source.nth.return_value.evaluate.return_value = 4
        table = MagicMock()
        table.count.return_value = 1
        rows = MagicMock()
        rows.count.return_value = 1
        target_row = MagicMock()
        rows.nth.return_value = target_row
        table.nth.return_value.locator.return_value = rows
        context.locator.side_effect = [source, table]
        selector = json.dumps({
            'row_mapping': {
                'operation': 'same_row_index', 'target_row_mode': 'only_row',
                'source': {'selector_type': 'xpath', 'selector': '//table[1]//tr[5]'},
                'target_table': {'selector_type': 'xpath', 'selector': '//table[2]'},
                'target': {'selector_type': 'xpath', 'selector': ".//button[contains(.,'OP')]"},
            },
        })

        result = build_locator(context, 'path', selector)

        self.assertIs(result, target_row.locator.return_value)
        rows.nth.assert_called_once_with(0)

    def test_picker_converts_f1_steps_and_f2_target(self) -> None:
        picker = ElementPicker()
        locator = MagicMock()
        locator.count.return_value = 1
        info = {
            'path_xpath': "//tr[contains(.,'サービス')]//input",
            'path_occurrence': {'position': 'last', 'index': 2, 'count': 2},
            'path_steps': [
                {'tag': 'tr', 'text': 'サービス'},
                {'tag': 'tr', 'text': '料金行'},
                {'tag': 'input', 'name': '適用終了月', 'input_type': 'text'},
            ],
        }
        frame = MagicMock()
        with (
            patch.object(picker, '_locator', return_value=locator),
            patch.object(picker, '_iframe_path', return_value=''),
        ):
            result = picker._choose_multi_path(MagicMock(), info, frame, 'fill')

        self.assertEqual(result['selector_type'], 'path')
        self.assertEqual(result['fallback_selector_type'], 'none')
        self.assertEqual(result['fallback_selector'], '')
        saved = json.loads(result['selector'])
        self.assertEqual([step['kind'] for step in saved['steps']], [
            'scope', 'scope', 'target',
        ])
        self.assertEqual(saved['resolved']['selector_type'], 'xpath')
        self.assertEqual(saved['occurrence']['position'], 'last')
        self.assertEqual(saved['occurrence_rule'], 'last')
        self.assertIn('同一条件の最後', saved['steps'][-1]['display'])
        self.assertIn('Path diagnostics:', result['path_diagnostics'])
        self.assertIn('resolved:', result['path_diagnostics'])
        self.assertIn('match_count: 1', result['path_diagnostics'])

    def test_picker_saves_last_f1_as_cross_table_source_row(self) -> None:
        picker = ElementPicker()
        locator = MagicMock()
        locator.count.return_value = 1
        mapping = {
            'operation': 'same_row_index',
            'source': {'selector_type': 'xpath', 'selector': '//tr[1]'},
            'target_table': {'selector_type': 'xpath', 'selector': '//table[2]'},
            'row_selector': ':scope > tbody > tr, :scope > tr',
            'target': {'selector_type': 'xpath', 'selector': './/input'},
        }
        info = {
            'path_xpath': '', 'row_mapping': mapping,
            'path_steps': [
                {'tag': 'section', 'text': '契約'},
                {'tag': 'tr', 'text': '基準料金'},
                {'tag': 'input', 'name': '対象料金', 'input_type': 'text'},
            ],
        }
        with (
            patch.object(picker, '_locator', return_value=locator),
            patch.object(picker, '_iframe_path', return_value=''),
        ):
            result = picker._choose_multi_path(MagicMock(), info, MagicMock(), 'fill')

        saved = json.loads(result['selector'])
        self.assertEqual([step['kind'] for step in saved['steps']], [
            'scope', 'source_row', 'target',
        ])
        self.assertEqual(saved['row_mapping']['operation'], 'same_row_index')
        self.assertNotIn('resolved', saved)

    def test_picker_saves_multiple_and_conditions_as_source_row_steps(self) -> None:
        picker = ElementPicker()
        locator = MagicMock()
        locator.count.return_value = 1
        mapping = {
            'operation': 'same_row_index', 'source_step_count': 3,
            'source': {
                'selector_type': 'xpath',
                'selector': "//tr[contains(.,'plan') and contains(.,'annual') and contains(.,'base')]",
            },
            'target_table': {'selector_type': 'xpath', 'selector': '//table[2]'},
            'target': {'selector_type': 'xpath', 'selector': ".//button[contains(.,'OP')]"},
        }
        info = {
            'path_xpath': '', 'row_mapping': mapping,
            'path_steps': [
                {'tag': 'td', 'text': 'plan'},
                {'tag': 'td', 'text': 'annual'},
                {'tag': 'td', 'text': 'base'},
                {'tag': 'button', 'text': 'OP'},
            ],
        }
        with (
            patch.object(picker, '_locator', return_value=locator),
            patch.object(picker, '_iframe_path', return_value=''),
        ):
            result = picker._choose_multi_path(MagicMock(), info, MagicMock(), 'click')

        saved = json.loads(result['selector'])
        self.assertEqual(
            [step['kind'] for step in saved['steps']],
            ['source_row', 'source_row', 'source_row', 'target'],
        )
        self.assertEqual(saved['row_mapping']['source_step_count'], 3)

    def test_picker_script_contains_shortcut_controls(self) -> None:
        script = picker_script('run', 'wait', 'active')
        self.assertIn("event.key === 'F1'", script)
        self.assertIn("event.key === 'F2'", script)
        self.assertIn("event.key === 'Backspace'", script)
        self.assertIn('const enterSelectionMode = (keyName) =>', script)
        self.assertIn('const leaveSelectionMode = () =>', script)
        self.assertIn("enterSelectionMode('F1')", script)
        self.assertIn('const allowF1 = true', script)
        self.assertIn("enterSelectionMode('F2')", script)
        self.assertIn("selectionKey === 'F1'", script)
        self.assertIn("document.addEventListener('click', choose, true)", script)
        self.assertIn('leaveSelectionMode();', script)
        self.assertIn("window.__sfFlowPicked = {cancelled: true}", script)
        self.assertIn("event.key === 'Backspace' && pathAnchors.length", script)
        self.assertIn('const updateWaitingBanner = () =>', script)
        self.assertIn("operation: 'same_row_index'", script)
        self.assertIn("conditions.join(' and ')", script)
        self.assertIn('const siblingScopeXPath = (anchors, target) =>', script)
        self.assertIn('const commonAnchorXPath = (anchors, target) =>', script)
        self.assertIn('sourceRows.sort((left, right)', script)

        screenshot_script = picker_script(
            'run', 'wait', 'active', allow_f1=False,
        )
        self.assertIn('const allowF1 = false', screenshot_script)
        self.assertIn("if (allowF1 || screenshotMode) enterSelectionMode('F1')", screenshot_script)

        screenshot_session_script = picker_script(
            'run', 'wait', 'active', allow_f1=False, screenshot_mode=True,
        )
        self.assertIn('const screenshotMode = true', screenshot_session_script)
        self.assertIn('finishScreenshot()', screenshot_session_script)
        self.assertIn('recordScreenshotElement(hovered)', screenshot_session_script)
        self.assertIn("screenshotHistory.pop()", screenshot_session_script)
        self.assertIn('__sf-flow-capture-area', screenshot_session_script)
        self.assertIn('__sf-flow-scroll-area', screenshot_session_script)
        self.assertIn('commonAnchorXPath(sourceAnchors, sourceRow)', script)
        self.assertIn("target_row_mode: rowCount(targetRow) === 1", script)
        self.assertIn("['button', 'a'].includes(tag)", script)
        self.assertIn("const type = element.getAttribute('type')", script)
        self.assertIn("uniqueConditions.join(' and ')", script)
        self.assertIn('sharedOuterRow?.contains(target)', script)
        self.assertIn('matches.snapshotLength === 1', script)
        self.assertIn('const sourceAnchors = anchors.slice(sourceStart)', script)
        self.assertIn('sourceRow.contains(targetTable)', script)
        self.assertIn('const qualifyXPathOccurrence = (xpath, target)', script)
        self.assertIn("? `(${xpath})[last()]`", script)
        self.assertIn('qualifiedSource.xpath', script)
        self.assertIn('normalize-space(translate(', script)
        self.assertIn("xpathLiteral('\\u00a0\\u3000')", script)
        self.assertIn('const mutableValueControl = (element)', script)
        self.assertIn("mutableValueControl(element) ? '' : (element.value || '')", script)
        self.assertIn("'aria-label', 'placeholder', 'title', 'role'", script)
        self.assertNotIn('const siblingStep', script)

    def test_path_is_supported_for_json_import(self) -> None:
        self.assertIn('path', SUPPORTED_SELECTOR_TYPES)

    def test_step_references_are_substituted_without_breaking_json(self) -> None:
        selector = json.dumps({
            'steps': [{'value': '${customer}'}, {'value': '${data:price_name}'}],
            'resolved': {'selector_type': 'xpath', 'selector': '__WFM_STEP_0__'},
        })
        variable_result = substitute_event_selector(
            {'selector_type': 'path', 'selector': selector},
            {'customer': 'A "quoted" customer'},
        )
        variable_data = json.loads(variable_result)
        self.assertEqual(variable_data['steps'][0]['value'], 'A "quoted" customer')
        data_result = substitute_path_step_values(
            variable_result,
            lambda value: value.replace('${data:price_name}', "O'Reilly price"),
        )
        self.assertEqual(json.loads(data_result)['steps'][1]['value'], "O'Reilly price")

    def test_short_parameters_expand_and_can_be_reused(self) -> None:
        context = MagicMock()
        selector = json.dumps({
            'steps': [{'value': '$1-$2-$1'}],
            'parameters': {
                '1': {'resolved_value': "O'Reilly"},
                '2': {'resolved_value': '2026'},
            },
            'resolved': {'selector_type': 'xpath', 'selector': '//tr[contains(.,__WFM_STEP_0__)]'},
        })

        build_locator(context, 'path', selector)

        context.locator.assert_called_once_with(
            'xpath=//tr[contains(.,"O\'Reilly-2026-O\'Reilly")]'
        )

    def test_double_dollar_keeps_a_literal_parameter_text(self) -> None:
        context = MagicMock()
        selector = json.dumps({
            'steps': [{'value': '$$1/$1'}],
            'parameters': {'1': {'resolved_value': 'value'}},
            'resolved': {'selector_type': 'xpath', 'selector': '__WFM_STEP_0__'},
        })

        build_locator(context, 'path', selector)

        context.locator.assert_called_once_with("xpath='$1/value'")

    def test_undefined_short_parameter_is_rejected(self) -> None:
        selector = json.dumps({
            'steps': [{'value': '$9'}],
            'resolved': {'selector_type': 'xpath', 'selector': '__WFM_STEP_0__'},
        })
        with self.assertRaisesRegex(ValueError, r'\$9'):
            build_locator(MagicMock(), 'path', selector)

    def test_fixed_and_runtime_variable_parameters_are_resolved(self) -> None:
        selector = json.dumps({
            'steps': [{'value': '$1-$2'}],
            'parameters': {
                '1': {'source': 'fixed', 'value': 'fixed', 'max_length': 20},
                '2': {'source': 'variable', 'value': 'customer', 'max_length': 20},
            },
        })
        result = json.loads(substitute_event_selector(
            {'selector_type': 'path', 'selector': selector}, {'customer': 'dynamic'},
        ))
        self.assertEqual(result['parameters']['1']['resolved_value'], 'fixed')
        self.assertEqual(result['parameters']['2']['resolved_value'], 'dynamic')
        self.assertEqual(find_variables([{
            'selector_type': 'path', 'selector': selector,
        }]), ['customer'])

    def test_runtime_datetime_parameter_is_formatted_and_can_be_concatenated(self) -> None:
        event = {
            'selector_type': 'none',
            'selector_parameters_json': json.dumps({
                '3': {
                    'source': 'variable', 'value': 'yyyymmdd_hhmm',
                    'empty_action': 'error', 'max_length': 20,
                },
                '4': {
                    'source': 'data', 'value': '案件名',
                    'resolved_value': '案件A', 'empty_action': 'error', 'max_length': 20,
                },
            }),
        }
        with patch('core.executor.datetime') as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 8, 20, 21, 54, 30)
            self.assertEqual(
                substitute_event_parameter_value(event, '$4_$3', {}),
                '案件A_20260820_2154',
            )
        self.assertEqual(find_variables([event | {'value': '$4_$3'}]), [])

    def test_named_runtime_variable_takes_priority_over_datetime_format(self) -> None:
        event = {
            'selector_type': 'none',
            'selector_parameters_json': json.dumps({
                '1': {
                    'source': 'variable', 'value': 'yyyymmdd',
                    'empty_action': 'error', 'max_length': 20,
                },
            }),
        }
        self.assertEqual(
            substitute_event_parameter_value(event, '$1', {'yyyymmdd': 'manual'}),
            'manual',
        )

    def test_data_parameter_requires_scalar_and_honors_length(self) -> None:
        selector = json.dumps({
            'steps': [{'value': '$1'}],
            'parameters': {'1': {'source': 'data', 'value': 'customer.name', 'max_length': 3}},
        })
        resolved = WorkflowExecutor._resolve_path_data_parameters(
            selector, {'customer': {'name': 'long'}}, {},
        )
        with self.assertRaisesRegex(ValueError, 'too long'):
            substitute_event_selector({'selector_type': 'path', 'selector': resolved}, {})
        with self.assertRaisesRegex(ValueError, 'scalar'):
            WorkflowExecutor._resolve_path_data_parameters(
                selector, {'customer': {'name': ['invalid']}}, {},
            )

    def test_occurrence_rule_accepts_a_data_parameter(self) -> None:
        selector = json.dumps({
            'steps': [{'value': 'target'}],
            'occurrence_rule': '$1',
            'parameters': {
                '1': {'source': 'data', 'value': 'selection', 'max_length': 10},
            },
            'resolved': {'selector_type': 'xpath', 'selector': '//button'},
        })
        resolved = WorkflowExecutor._resolve_path_data_parameters(
            selector, {'selection': 'LAST'}, {},
        )
        context = MagicMock()
        context.locator.return_value.count.return_value = 2

        build_locator(context, 'path', resolved)

        context.locator.return_value.nth.assert_called_once_with(1)

    def test_normal_and_fallback_selectors_share_short_parameters(self) -> None:
        event = {
            'selector_type': 'css', 'selector': '#order-$1[data-name="$2"]',
            'fallback_selector': "//button[contains(., '$2')]",
            'selector_parameters_json': json.dumps({
                '1': {'source': 'fixed', 'value': '42', 'max_length': 10},
                '2': {'source': 'variable', 'value': 'customer', 'max_length': 30},
            }),
        }
        variables = {'customer': 'Alice'}

        self.assertEqual(
            substitute_event_selector(event, variables), '#order-42[data-name="Alice"]',
        )
        self.assertEqual(
            substitute_event_selector_value(event, 'fallback_selector', variables),
            "//button[contains(., 'Alice')]",
        )
        self.assertEqual(find_variables([event]), ['customer'])
        self.assertEqual(
            substitute_event_parameter_value(event, 'Hello $2', variables), 'Hello Alice',
        )

    def test_normal_selector_supports_literal_dollar_and_data_parameter(self) -> None:
        parameters = {
            '1': {'source': 'data', 'value': 'order.number', 'max_length': 20},
        }
        resolved = WorkflowExecutor._resolve_selector_data_parameters(
            json.dumps(parameters), {'order': {'number': 'A-01'}}, {},
        )
        event = {
            'selector_type': 'css', 'selector': '[data-price="$$1"][data-id="$1"]',
            'selector_parameters_json': resolved,
        }
        self.assertEqual(
            substitute_event_selector(event, {}), '[data-price="$1"][data-id="A-01"]',
        )

    def test_database_persists_normal_selector_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / 'test.db')
            workflow_id = database.add_workflow('parameters')
            parameters = json.dumps({'1': {'source': 'fixed', 'value': 'A'}})
            event_id = database.add_event(workflow_id, {
                'name': 'click', 'action': 'click', 'selector_type': 'css',
                'selector': '#$1', 'fallback_selector_type': 'css',
                'fallback_selector': '.$1', 'selector_parameters_json': parameters,
                'value': '', 'timeout_ms': 1000, 'enabled': 1, 'continue_on_error': 0,
            })
            row = next(row for row in database.list_events(workflow_id) if row['id'] == event_id)
            self.assertEqual(row['selector_parameters_json'], parameters)
            database.close()

    def test_resolved_path_preview_contains_composed_xpath(self) -> None:
        selector = json.dumps({
            'steps': [{'value': 'Customer A'}],
            'parameters': {},
            'resolved': {
                'selector_type': 'xpath',
                'selector': '//tr[contains(.,__WFM_STEP_0__)]//input',
            },
        })
        self.assertEqual(
            selector_preview('path', selector),
            "//tr[contains(.,'Customer A')]//input",
        )

    def test_event_preparation_ignores_unused_data_parameter(self) -> None:
        event = {
            'action': 'click', 'selector_type': 'css', 'selector': '#fixed-$1',
            'selector_parameters_json': json.dumps({
                '1': {'source': 'fixed', 'value': 'A'},
                '2': {'source': 'data', 'value': 'missing.path'},
            }),
        }
        prepared = WorkflowExecutor.prepare_event_data(event, None)
        self.assertEqual(substitute_event_selector(prepared, {}), '#fixed-A')


if __name__ == '__main__':
    unittest.main()
