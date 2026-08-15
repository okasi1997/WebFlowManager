from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from core.excel_io import (
    read_records_excel, read_records_excel_with_schema, remap_data_for_schema_names, schema_name_path_map,
    strip_data_record_whitespace, strip_schema_name_whitespace, write_records_excel,
)


class ExcelStyleTests(unittest.TestCase):
    def test_excel_headers_can_restore_schema_and_records(self) -> None:
        schema = {
            'name': 'Data', 'type': 'object', 'children': [
                {'name': 'profile', 'type': 'object', 'children': [
                    {'name': 'name', 'type': 'text'},
                    {'name': 'active', 'type': 'boolean'},
                    {'name': 'count', 'type': 'number'},
                ]},
                {'name': 'items', 'type': 'list', 'children': [
                    {'name': 'code', 'type': 'text'},
                ]},
            ],
        }
        records = [{
            'name': 'PCL_001', 'summary': 'summary', 'enabled': True,
            'execution_group': '2', 'data': {
                'profile': {'name': 'Alice', 'active': True, 'count': 3},
                'items': [{'code': 'A'}, {'code': 'B'}],
            },
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'schema-import.xlsx'
            write_records_excel(path, schema, records)
            restored_schema, restored_records = read_records_excel_with_schema(path)

        self.assertEqual(restored_schema, schema)
        self.assertEqual(restored_records, records)

    def test_schema_names_and_existing_data_keys_are_trimmed_together(self) -> None:
        schema = {
            'name': '\u3000Data\n', 'type': 'object',
            'children': [{
                'name': '\t料金 \u3000', 'type': 'object',
                'children': [{'name': '\nオプション\r', 'type': 'text'}],
            }],
        }
        cleaned = strip_schema_name_whitespace(schema)
        migrated = remap_data_for_schema_names(
            schema, cleaned, {'\t料金 \u3000': {'\nオプション\r': '値'}},
        )

        self.assertEqual(cleaned['name'], 'Data')
        self.assertEqual(cleaned['children'][0]['name'], '料金')
        self.assertEqual(cleaned['children'][0]['children'][0]['name'], 'オプション')
        self.assertEqual(migrated, {'料金': {'オプション': '値'}})
        self.assertEqual(schema_name_path_map(schema, cleaned), {
            '\t料金 \u3000': '料金',
            '\t料金 \u3000.\nオプション\r': '料金.オプション',
        })

    def test_schema_name_trim_rejects_resulting_sibling_duplicate(self) -> None:
        schema = {
            'name': 'Data', 'type': 'object',
            'children': [
                {'name': '料金', 'type': 'text'},
                {'name': '料金\u3000', 'type': 'text'},
            ],
        }
        with self.assertRaises(ValueError):
            strip_schema_name_whitespace(schema)

    def test_data_json_record_strips_unicode_whitespace(self) -> None:
        cleaned = strip_data_record_whitespace({
            'name': '\u3000PCL_001\n',
            'summary': '\t概要\r',
            'execution_group': '\n 1 \u3000',
            'enabled': True,
            'data': {'outer': {'text': '\r\n 値 \t'}, 'items': ['\u3000A ', '\nB\r']},
        })

        self.assertEqual(cleaned['name'], 'PCL_001')
        self.assertEqual(cleaned['summary'], '概要')
        self.assertEqual(cleaned['execution_group'], '1')
        self.assertEqual(cleaned['data'], {'outer': {'text': '値'}, 'items': ['A', 'B']})

    def test_data_management_excel_strips_unicode_whitespace(self) -> None:
        schema = {
            'name': 'Data', 'type': 'object',
            'children': [{
                'name': 'items', 'type': 'list',
                'children': [{'name': 'value', 'type': 'text'}],
            }],
        }
        records = [{
            'name': '\u3000PCL_001\n',
            'summary': '\t概要\r\n',
            'enabled': True,
            'execution_group': '\u30001\t',
            'data': {'items': [{'value': '\n 値 \u3000'}, {'value': '\t次\r'}]},
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trimmed.xlsx'
            write_records_excel(path, schema, records)
            workbook = load_workbook(path, data_only=True)
            data_sheet = workbook.worksheets[0]
            settings_sheet = workbook.worksheets[1]
            self.assertEqual(data_sheet.cell(3, 1).value, 'PCL_001')
            self.assertEqual(data_sheet.cell(3, 2).value, '値')
            self.assertEqual(data_sheet.cell(4, 2).value, '次')
            self.assertEqual(settings_sheet.cell(2, 2).value, '概要')
            self.assertEqual(settings_sheet.cell(2, 4).value, '1')
            restored = read_records_excel(path, schema)

        self.assertEqual(restored[0]['name'], 'PCL_001')
        self.assertEqual(restored[0]['summary'], '概要')
        self.assertEqual(restored[0]['data']['items'], [{'value': '値'}, {'value': '次'}])

    def test_import_uses_named_data_sheet_when_settings_sheet_is_active(self) -> None:
        """実行設定を表示したまま保存しても、データ一覧を正しく読み込む。"""
        schema = {
            'name': 'Data', 'type': 'object',
            'children': [{'name': 'value', 'type': 'text'}],
        }
        records = [{
            'name': 'PCL_001', 'summary': '概要', 'enabled': True,
            'execution_group': '1', 'data': {'value': '値'},
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'active-settings.xlsx'
            write_records_excel(path, schema, records)
            workbook = load_workbook(path)
            workbook.active = 1
            workbook.save(path)

            restored = read_records_excel(path, schema)

        self.assertEqual(restored, records)

    def test_repeated_names_in_multilevel_header_round_trip(self) -> None:
        schema = {
            'name': 'Data',
            'type': 'object',
            'children': [{
                'name': '料金',
                'type': 'object',
                'children': [{
                    'name': 'オプション',
                    'type': 'list',
                    'children': [{'name': 'オプション', 'type': 'text'}],
                }],
            }],
        }
        records = [{
            'name': 'PCL_001',
            'enabled': True,
            'execution_group': '1',
            'data': {'料金': {'オプション': [
                {'オプション': 'A'}, {'オプション': 'B'},
            ]}},
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'repeated-header.xlsx'
            write_records_excel(path, schema, records)
            restored = read_records_excel(path, schema)

        self.assertEqual(restored[0]['data'], records[0]['data'])

    def test_header_mismatch_reports_actual_differences(self) -> None:
        schema = {
            'name': 'Data', 'type': 'object',
            'children': [{'name': '料金', 'type': 'text'}],
        }
        records = [{
            'name': 'PCL_001', 'enabled': True, 'execution_group': '1',
            'data': {'料金': '100'},
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad-header.xlsx'
            write_records_excel(path, schema, records)
            workbook = load_workbook(path)
            workbook.worksheets[0].cell(1, 2, '金額')
            workbook.save(path)
            with self.assertRaises(ValueError) as raised:
                read_records_excel(path, schema)

        message = str(raised.exception)
        self.assertIn('Excel only: 金額', message)
        self.assertIn('Current structure only: 料金', message)
        self.assertIn('Excel headers: 金額', message)

    def test_all_values_are_left_aligned_and_header_levels_have_distinct_colors(self) -> None:
        schema = {
            'name': 'Data',
            'type': 'list',
            'children': [
                {'name': 'PCL_NO', 'type': 'text'},
                {
                    'name': 'output',
                    'type': 'object',
                    'children': [
                        {'name': 'contract_id', 'type': 'text'},
                        {'name': 'quantity', 'type': 'number'},
                    ],
                },
            ],
        }
        records = [{
            'name': 'PCL_001',
            'enabled': True,
            'execution_group': '1',
            'data': {'PCL_NO': 'PCL_001', 'output': {'contract_id': 'C-001', 'quantity': 12}},
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.xlsx'
            write_records_excel(path, schema, records)
            workbook = load_workbook(path)

        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is not None:
                        self.assertEqual(cell.alignment.horizontal, 'left', f'{sheet.title}!{cell.coordinate}')

        data_sheet = workbook.worksheets[0]
        top_color = data_sheet.cell(1, 3).fill.fgColor.rgb
        child_color = data_sheet.cell(2, 3).fill.fgColor.rgb
        self.assertNotEqual(top_color, child_color)
        self.assertEqual(top_color, '002F5597')
        self.assertEqual(child_color, '004472C4')


if __name__ == '__main__':
    unittest.main()
