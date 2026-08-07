from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from ui.structured_data import read_records_excel, write_records_excel


class ExcelStyleTests(unittest.TestCase):
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
