"""データ構造の正規化と Excel 入出力を扱う。"""
from __future__ import annotations
import copy
from pathlib import Path
from typing import Any
from core.data_values import normalize_data_record, strip_unicode_whitespace
from i18n import SUPPORTED_LANGUAGES, tr, tr_language
TYPES = ('text', 'number', 'boolean', 'object', 'list')

def strip_data_whitespace(value: Any) -> Any:
    """Data 管理で扱う文字列から先頭・末尾の Unicode 空白を除去する。"""
    return strip_unicode_whitespace(value)

def strip_data_record_whitespace(record: dict[str, Any]) -> dict[str, Any]:
    """Data の入出力用レコードに同じ空白除去規則を適用する。"""
    return normalize_data_record(record)

def strip_schema_name_whitespace(schema: dict[str, Any]) -> dict[str, Any]:
    """構造内の全フィールド名から先頭・末尾の Unicode 空白を除去する。"""
    cleaned = copy.deepcopy(schema)
    if not isinstance(cleaned, dict):
        validate_schema(cleaned)

    def walk(node: dict[str, Any]) -> None:
        node['name'] = str(node.get('name', '')).strip()
        for child in node.get('children', []):
            if isinstance(child, dict):
                walk(child)
    walk(cleaned)
    validate_schema(cleaned)
    return cleaned

def remap_data_for_schema_names(
        old_schema: dict[str, Any], new_schema: dict[str, Any], data: Any) -> Any:
    """空白除去で変更された構造名に合わせ、既存データのキーを移行する。"""
    if old_schema.get('type') == 'list':
        if not isinstance(data, list):
            return data
        return [remap_data_for_schema_names(
            {'type': 'object', 'children': old_schema.get('children', [])},
            {'type': 'object', 'children': new_schema.get('children', [])},
            item,
        ) for item in data]
    if old_schema.get('type') != 'object' or not isinstance(data, dict):
        return data
    result = dict(data)
    for old_child, new_child in zip(old_schema.get('children', []), new_schema.get('children', [])):
        old_name, new_name = old_child['name'], new_child['name']
        if old_name not in result:
            continue
        value = result.pop(old_name)
        result[new_name] = remap_data_for_schema_names(old_child, new_child, value)
    return result

def schema_name_path_map(
        old_schema: dict[str, Any], new_schema: dict[str, Any]) -> dict[str, str]:
    """空白除去前後で変更された全構造パスの対応表を作成する。"""
    result: dict[str, str] = {}

    def walk(old_node: dict[str, Any], new_node: dict[str, Any], old_prefix: str, new_prefix: str) -> None:
        for old_child, new_child in zip(old_node.get('children', []), new_node.get('children', [])):
            old_path = f"{old_prefix}.{old_child['name']}" if old_prefix else old_child['name']
            new_path = f"{new_prefix}.{new_child['name']}" if new_prefix else new_child['name']
            if old_path != new_path:
                result[old_path] = new_path
            walk(old_child, new_child, old_path, new_path)
    walk(old_schema, new_schema, '', '')
    return result

def validate_schema(node: Any, location: str='Data') -> None:
    if not isinstance(node, dict) or not isinstance(node.get('name'), str) or (not node['name'].strip()):
        raise ValueError(f'{location}msg.0236')
    if node.get('type') not in TYPES:
        raise ValueError(f"{location}msg.0237{node.get('type')}")
    children = node.get('children', [])
    if node['type'] in ('object', 'list'):
        if not isinstance(children, list):
            raise ValueError(f'{location}msg.0238')
        names: set[str] = set()
        for child in children:
            child_name = child.get('name') if isinstance(child, dict) else '?'
            if child_name in names:
                raise ValueError(f'{location}msg.0239{child_name}')
            names.add(child_name)
            validate_schema(child, f'{location}.{child_name}')
    elif 'children' in node and children:
        raise ValueError(f'{location}msg.0240')

def scalar_paths(schema: dict[str, Any]) -> list[str]:
    return [path for path, node in schema_paths(schema) if node['type'] not in ('object', 'list')]

def _flatten_record_with_groups(schema: dict[str, Any], data: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, int]]]:
    # list の添字も返し、同じ親値を Excel 上で何度も出力しないようにする。

    def merge(left: list[tuple[dict[str, Any], dict[str, int]]], right: list[tuple[dict[str, Any], dict[str, int]]]) -> list[tuple[dict[str, Any], dict[str, int]]]:
        return [({**a_values, **b_values}, {**a_groups, **b_groups}) for a_values, a_groups in left for b_values, b_groups in right]

    def walk_fields(nodes: list[dict[str, Any]], value: dict[str, Any], prefix: str) -> list[tuple[dict[str, Any], dict[str, int]]]:
        rows: list[tuple[dict[str, Any], dict[str, int]]] = [({}, {})]
        for node in nodes:
            path = f"{prefix}.{node['name']}" if prefix else node['name']
            current = value.get(node['name'])
            if node['type'] == 'list':
                items = current if isinstance(current, list) else []
                child_rows: list[tuple[dict[str, Any], dict[str, int]]] = []
                for item_index, item in enumerate(items):
                    for child_values, child_groups in walk_fields(node.get('children', []), item if isinstance(item, dict) else {}, path):
                        child_rows.append((child_values, {path: item_index, **child_groups}))
                if not child_rows:
                    child_rows = [({child_path: '' for child_path in _descendant_scalar_paths(node, path)}, {path: -1})]
            elif node['type'] == 'object':
                child_rows = walk_fields(node.get('children', []), current if isinstance(current, dict) else {}, path)
            else:
                child_rows = [({path: current if current is not None else ''}, {})]
            rows = merge(rows, child_rows)
        return rows
    return walk_fields(schema.get('children', []), data, '')

def scalar_list_owners(schema: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}

    def walk(node: dict[str, Any], prefix: str, list_ancestors: tuple[str, ...]) -> None:
        for child in node.get('children', []):
            path = f"{prefix}.{child['name']}" if prefix else child['name']
            ancestors = (*list_ancestors, path) if child['type'] == 'list' else list_ancestors
            if child['type'] in ('object', 'list'):
                walk(child, path, ancestors)
            else:
                result[path] = list_ancestors
    walk(schema, '', ())
    return result

def _descendant_scalar_paths(node: dict[str, Any], prefix: str) -> list[str]:
    result: list[str] = []
    for child in node.get('children', []):
        path = f"{prefix}.{child['name']}"
        if child['type'] in ('object', 'list'):
            result.extend(_descendant_scalar_paths(child, path))
        else:
            result.append(path)
    return result

def write_records_excel(path: str | Path, schema: dict[str, Any], records: list[dict[str, Any]]) -> int:
    """PCL と実行設定を、再読込可能な Excel ブックへ保存する。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    original_schema = schema
    schema = strip_schema_name_whitespace(schema)
    columns = scalar_paths(schema)
    owners = scalar_list_owners(schema)
    max_depth = max((len(column.split('.')) for column in columns), default=1)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = tr('msg.0241')
    sheet.cell(1, 1, tr('msg.0242'))
    if max_depth > 1:
        sheet.merge_cells(start_row=1, start_column=1, end_row=max_depth, end_column=1)
    path_parts = [column.split('.') for column in columns]
    for column_index, parts in enumerate(path_parts, 2):
        for level, part in enumerate(parts, 1):
            sheet.cell(level, column_index, part)
        if len(parts) < max_depth:
            sheet.merge_cells(start_row=len(parts), start_column=column_index, end_row=max_depth, end_column=column_index)
    for level in range(1, max_depth + 1):
        start = 0
        while start < len(columns):
            prefix = tuple(path_parts[start][:level]) if len(path_parts[start]) >= level else None
            end = start + 1
            while end < len(columns) and prefix is not None and (tuple(path_parts[end][:level]) == prefix):
                end += 1
            if prefix is not None and end - start > 1:
                sheet.merge_cells(start_row=level, start_column=start + 2, end_row=level, end_column=end + 1)
            start = end
    output_row = max_depth + 1
    for record in records:
        migrated = remap_data_for_schema_names(original_schema, schema, record['data'])
        clean_data = strip_data_whitespace(normalize_record(schema, migrated))
        rows = _flatten_record_with_groups(schema, clean_data) or [({}, {})]
        seen_scopes: set[tuple[Any, ...]] = set()
        for row_index, (values, groups) in enumerate(rows):
            sheet.cell(output_row, 1, str(record['name']).strip() if row_index == 0 else '')
            for column_index, column in enumerate(columns, 2):
                ancestors = owners[column]
                scope = (column, *(groups.get(path, -1) for path in ancestors))
                if scope not in seen_scopes:
                    sheet.cell(output_row, column_index, values.get(column, ''))
                    seen_scopes.add(scope)
            output_row += 1
    header_level_colors = ('2F5597', '4472C4', '5B9BD5', '6F9FD5', '7EA6D8')
    for level, row in enumerate(sheet.iter_rows(min_row=1, max_row=max_depth)):
        color = header_level_colors[min(level, len(header_level_colors) - 1)]
        for cell in row:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor=color)
    sheet.freeze_panes = f'A{max_depth + 1}'
    headers = [tr('msg.0242'), *columns]
    for index, header in enumerate(headers, 1):
        values = [str(sheet.cell(row, index).value or '') for row in range(1, sheet.max_row + 1)]
        sheet.column_dimensions[get_column_letter(index)].width = min(max(len(header) + 2, *(len(value) + 2 for value in values)), 45)
    settings_sheet = workbook.create_sheet(tr('msg.0243'))
    settings_sheet.append([tr('msg.0242'), tr('msg.0517'), tr('msg.0244'), tr('msg.0245')])
    for record in records:
        settings_sheet.append([
            str(record['name']).strip(),
            str(record.get('summary', '')).strip(),
            bool(record.get('enabled', True)),
            str(record.get('execution_group', '1')).strip(),
        ])
    for current_sheet in workbook.worksheets:
        for row in current_sheet.iter_rows():
            for cell in row:
                if cell.value is not None:
                    cell.alignment = Alignment(horizontal='left', vertical='center')
    workbook.save(path)
    return sheet.max_row - max_depth

def read_records_excel(path: str | Path, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """write_records_excel が作成した階層ブックを PCL に復元する。"""
    from openpyxl import load_workbook
    schema = strip_schema_name_whitespace(schema)
    workbook = load_workbook(path, data_only=True)
    sheet = workbook.active
    header_depth = 1
    for merged in sheet.merged_cells.ranges:
        # 先頭列の縦結合範囲が、階層ヘッダーの深さを表す。
        if merged.min_col == merged.max_col == 1 and merged.min_row == 1:
            header_depth = max(header_depth, merged.max_row)

    def header_value(row: int, column: int) -> Any:
        value = sheet.cell(row, column).value
        if value is not None:
            return value
        for merged in sheet.merged_cells.ranges:
            if merged.min_row <= row <= merged.max_row and merged.min_col <= column <= merged.max_col:
                # 縦結合された末端項目は複数のヘッダー行を占めるが、パス要素は一つだけである。
                # 一方、横結合では親項目を配下の各列へ引き継ぐ。
                if merged.min_row < row and merged.max_row > merged.min_row:
                    return None
                return sheet.cell(merged.min_row, merged.min_col).value
        return None
    columns: list[str] = []
    for column in range(2, sheet.max_column + 1):
        parts = [str(header_value(row, column)).strip() for row in range(1, header_depth + 1) if header_value(row, column) not in (None, '')]
        # 料金.オプション.オプション のような階層間の同名項目を保持する。
        # 縦結合による重複部分は header_value() ですでに除外している。
        path_name = '.'.join(parts)
        columns.append(path_name)
    expected = set(scalar_paths(schema))
    actual = set(columns)
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if not columns or unexpected:
        details = ['msg.0246']
        if unexpected:
            details.append(f"Excel only: {', '.join(unexpected)}")
        if missing:
            details.append(f"Current structure only: {', '.join(missing)}")
        details.append(f"Excel headers: {', '.join(columns) if columns else '(none)'}")
        raise ValueError('\n'.join(details))
    grouped: list[tuple[str, list[dict[str, Any]]]] = []
    current_name = ''
    current_rows: list[dict[str, Any]] = []
    for row_number in range(header_depth + 1, sheet.max_row + 1):
        name_value = sheet.cell(row_number, 1).value
        values = {
            path_name: strip_data_whitespace(sheet.cell(row_number, index + 2).value)
            for index, path_name in enumerate(columns)
        }
        if name_value not in (None, ''):
            if current_name:
                grouped.append((current_name, current_rows))
            current_name = str(name_value).strip()
            current_rows = []
        if current_name and any((value is not None for value in values.values())):
            current_rows.append(values)
    if current_name:
        grouped.append((current_name, current_rows))
    if not grouped:
        raise ValueError('msg.0247')
    execution_settings: list[tuple[str, bool, str]] = []
    settings_names = {tr_language('msg.0243', language) for language in SUPPORTED_LANGUAGES}
    # 出力時と現在の UI 言語が異なっても設定シートを認識する。
    settings_name = next((name for name in workbook.sheetnames if name in settings_names), None)
    if settings_name is not None:
        settings_sheet = workbook[settings_name]
        has_summary_column = settings_sheet.max_column >= 4
        for row in settings_sheet.iter_rows(min_row=2, values_only=True):
            summary = str(row[1] if has_summary_column and len(row) > 1 and row[1] is not None else '').strip()
            enabled_index = 2 if has_summary_column else 1
            group_index = 3 if has_summary_column else 2
            enabled_value = row[enabled_index] if len(row) > enabled_index else True
            enabled_words = {tr_language('msg.0037', language).lower() for language in SUPPORTED_LANGUAGES}
            enabled_words.add(tr('msg.0248').lower())
            enabled = enabled_value if isinstance(enabled_value, bool) else str(enabled_value).lower() in {'true', '1', 'yes', *enabled_words}
            group = str(row[group_index] if len(row) > group_index and row[group_index] not in (None, '') else '1').strip()
            execution_settings.append((summary, enabled, group))
    owners = scalar_list_owners(schema)
    list_paths = sorted({owner for value in owners.values() for owner in value}, key=lambda value: (value.count('.'), value))
    anchors = {list_path: [column for column in columns if owners[column] and owners[column][-1] == list_path] for list_path in list_paths}
    node_by_path = {path_name: node for path_name, node in schema_paths(schema)}

    def assign(data: dict[str, Any], path_name: str, value: Any, indexes: dict[str, int]) -> None:
        # パスと行ごとの list 添字を使って、平坦なセルを階層辞書へ戻す。
        container: Any = data
        prefix = ''
        parts = path_name.split('.')
        for index, part in enumerate(parts):
            prefix = f'{prefix}.{part}' if prefix else part
            node = node_by_path[prefix]
            last = index == len(parts) - 1
            if node['type'] == 'list':
                target = container.setdefault(part, [])
                item_index = indexes[prefix]
                while len(target) <= item_index:
                    target.append(new_list_item(node))
                container = target[item_index]
            elif last:
                if node['type'] == 'number' and value not in (None, ''):
                    value = float(value) if isinstance(value, float) and (not value.is_integer()) else int(value)
                elif node['type'] == 'boolean':
                    yes_words = {tr_language('msg.0037', language).lower() for language in SUPPORTED_LANGUAGES}
                    value = value if isinstance(value, bool) else str(value).lower() in {'true', '1', 'yes', *yes_words}
                container[part] = value
            elif node['type'] == 'object':
                container = container.setdefault(part, {})
    result: list[dict[str, Any]] = []
    for name, rows in grouped:
        data = {child['name']: default_value(child) for child in schema.get('children', [])}
        counters = {path_name: 0 for path_name in list_paths}
        previous = dict(counters)
        for row_index, values in enumerate(rows):
            previous = dict(counters)
            for list_path in list_paths:
                parents = [owner for owner in list_paths if list_path.startswith(owner + '.')]
                parent = max(parents, key=len) if parents else None
                if row_index == 0 or (parent and counters[parent] != previous[parent]):
                    counters[list_path] = 0
                elif any((values.get(column) is not None for column in anchors[list_path])):
                    counters[list_path] += 1
            for path_name, value in values.items():
                if value is not None:
                    assign(data, path_name, value, counters)
        setting = execution_settings[len(result)] if len(result) < len(execution_settings) else ('', True, '1')
        result.append({'name': name, 'summary': setting[0], 'enabled': setting[1], 'execution_group': setting[2], 'data': strip_data_whitespace(normalize_record(schema, data))})
    return result

def default_value(node: dict[str, Any]) -> Any:
    kind = node['type']
    if kind in ('object', 'list'):
        value = {child['name']: default_value(child) for child in node.get('children', [])}
        return [] if kind == 'list' else value
    if kind == 'number':
        return 0
    if kind == 'boolean':
        return False
    return ''

def new_list_item(node: dict[str, Any], populate_nested_lists: bool=False) -> dict[str, Any]:

    def build(child: dict[str, Any]) -> Any:
        if child['type'] == 'list':
            return [new_list_item(child, True)] if populate_nested_lists else []
        if child['type'] == 'object':
            return {grandchild['name']: build(grandchild) for grandchild in child.get('children', [])}
        return default_value(child)
    return {child['name']: build(child) for child in node.get('children', [])}

def normalize_record(schema: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    for child in schema.get('children', []):
        name = child['name']
        if name not in result:
            result[name] = default_value(child)
        elif child['type'] == 'object':
            result[name] = normalize_record(child, result[name]) if isinstance(result[name], dict) else default_value(child)
        elif child['type'] == 'list':
            result[name] = [normalize_record(child, item) if isinstance(item, dict) else new_list_item(child) for item in result[name]] if isinstance(result[name], list) else []
    return result

def schema_paths(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    result: list[tuple[str, dict[str, Any]]] = []

    def walk(node: dict[str, Any], prefix: str) -> None:
        for child in node.get('children', []):
            path = f"{prefix}.{child['name']}" if prefix else child['name']
            result.append((path, child))
            if child['type'] in ('object', 'list'):
                walk(child, path)
    walk(schema, '')
    return result

