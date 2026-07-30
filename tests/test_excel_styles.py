from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from ui.structured_data import write_records_excel


class ExcelStyleTests(unittest.TestCase):
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
