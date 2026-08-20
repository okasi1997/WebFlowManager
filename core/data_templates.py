"""データ構造テンプレートと PCL ごとのテンプレート実体を扱う。"""
from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from typing import Any


TEMPLATE_INSTANCES_KEY = '_template_instances'


def schema_templates(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """データ構造に定義されたテンプレートを返す。"""
    templates = schema.get('templates', [])
    return templates if isinstance(templates, list) else []


def validate_unique_template_names(schema: dict[str, Any]) -> None:
    """表示パスに使用するテンプレート名が一意であることを検証する。"""
    seen: set[str] = set()
    for template in schema_templates(schema):
        name = str(template.get('name', '')).strip()
        if not name:
            raise ValueError('テンプレート名を入力してください。')
        if name in seen:
            raise ValueError(f'テンプレート名が重複しています: {name}')
        seen.add(name)


def template_by_id(schema: dict[str, Any], template_id: str) -> dict[str, Any] | None:
    """安定 ID からテンプレート定義を検索する。"""
    return next(
        (template for template in schema_templates(schema)
         if str(template.get('template_id', '')) == template_id),
        None,
    )


def template_instances(data: dict[str, Any]) -> list[dict[str, Any]]:
    """PCL 内の有効なテンプレート実体だけを返す。"""
    instances = data.get(TEMPLATE_INSTANCES_KEY, [])
    # 呼出側で並び替え・削除するため、正常な配列は同じオブジェクトを返す。
    if not isinstance(instances, list):
        return []
    if any(not isinstance(item, dict) for item in instances):
        instances[:] = [item for item in instances if isinstance(item, dict)]
    return instances


def iter_template_instances(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], dict[str, Any]]]:
    """データ内の格納場所にかかわらず、テンプレート実体とそのパスを列挙する。"""
    if isinstance(value, dict):
        is_instance = (
            isinstance(value.get('data'), dict)
            and bool(str(value.get('template_id', '')).strip())
        )
        if is_instance:
            yield path, value
        for key, child in value.items():
            # 実体の管理情報は走査せず、内容側にネストした実体だけを対象にする。
            if is_instance and key != 'data':
                continue
            yield from iter_template_instances(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_template_instances(child, (*path, index))


def sync_template_instance_names(data: dict[str, Any], schema: dict[str, Any]) -> bool:
    """名称パス解決用の定義名を、既存・取込済み実体へ同期する。"""
    names_by_id = {
        str(template.get('template_id', '')): str(template.get('name', '')).strip()
        for template in schema_templates(schema)
    }
    changed = False
    for _path, instance in iter_template_instances(data):
        template_name = names_by_id.get(str(instance.get('template_id', '')))
        if template_name and instance.get('template_name') != template_name:
            instance['template_name'] = template_name
            changed = True
    return changed


def sync_template_instances_to_schema(data: dict[str, Any], schema: dict[str, Any]) -> bool:
    """既存 PCL のテンプレート実体を最新定義へ同期し、同名項目の値は保持する。"""
    projected = project_data_to_schema(data, schema)
    projected_by_id = {
        str(instance.get('instance_id', '')): instance
        for _path, instance in iter_template_instances(projected)
        if str(instance.get('instance_id', ''))
    }
    changed = False
    # 先に一覧化し、親実体の data 更新中に走査先を変更しない。
    for _path, instance in list(iter_template_instances(data)):
        projected_instance = projected_by_id.get(str(instance.get('instance_id', '')))
        if projected_instance is not None and instance != projected_instance:
            instance.clear()
            instance.update(copy.deepcopy(projected_instance))
            changed = True
    return changed


def project_data_to_schema(data: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """現在の構造に存在する値だけを、JSON 出力用に抽出する。"""
    normalized_schema = normalize_template_schema(schema)

    def project_node(value: Any, node: dict[str, Any]) -> Any:
        kind = node.get('type')
        if kind == 'object':
            source = value if isinstance(value, dict) else {}
            return {
                child['name']: project_node(source.get(child['name']), child)
                for child in node.get('children', [])
            }
        if kind == 'list':
            if not isinstance(value, list):
                return []
            projected: list[Any] = []
            for item in value:
                if isinstance(item, dict) and item.get('template_id'):
                    template = template_by_id(normalized_schema, str(item.get('template_id', '')))
                    if template is not None:
                        projected.append(project_instance(item, template))
                    continue
                projected.append(
                    project_node(item, {'type': 'object', 'children': node.get('children', [])})
                    if node.get('children') else copy.deepcopy(item)
                )
            return projected
        if value is None:
            return 0 if kind == 'number' else False if kind == 'boolean' else ''
        return copy.deepcopy(value)

    def project_instance(instance: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
        """内部識別情報を維持しつつ、実体内容から旧フィールドを除外する。"""
        return {
            'instance_id': str(instance.get('instance_id', '')),
            'template_id': str(template.get('template_id', '')),
            'template_name': str(template.get('name', '')).strip(),
            'name': str(instance.get('name', '')).strip() or str(template.get('name', '')),
            'data': project_node(instance.get('data', {}), template),
        }

    result = project_node(data, normalized_schema)
    instances = []
    for instance in template_instances(data):
        template = template_by_id(normalized_schema, str(instance.get('template_id', '')))
        if template is not None:
            instances.append(project_instance(instance, template))
    if instances:
        result[TEMPLATE_INSTANCES_KEY] = instances
    return result


def data_to_public_json(data: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """内部 ID を含まない、利用者向け JSON データへ変換する。"""
    projected = project_data_to_schema(data, schema)

    def convert(value: Any) -> Any:
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        if value.get('template_id') and isinstance(value.get('data'), dict):
            return {
                'template': str(value.get('template_name', '')).strip(),
                'name': str(value.get('name', '')).strip(),
                'data': convert(value['data']),
            }
        return {key: convert(child) for key, child in value.items()}

    instances = projected.pop(TEMPLATE_INSTANCES_KEY, [])
    result = convert(projected)
    if instances:
        result['templates'] = convert(instances)
    return result


def data_from_public_json(data: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """利用者向け JSON から内部テンプレート実体を復元する。"""
    normalized_schema = normalize_template_schema(schema)
    templates_by_name = {
        str(template.get('name', '')).strip(): template
        for template in schema_templates(normalized_schema)
    }

    def convert(value: Any) -> Any:
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        if set(value).issubset({'template', 'name', 'data'}) and isinstance(value.get('data'), dict):
            template_name = str(value.get('template', '')).strip()
            template = templates_by_name.get(template_name)
            if template is None:
                raise ValueError(f'テンプレートが見つかりません: {template_name}')
            instance = new_template_instance(template, [])
            instance['name'] = str(value.get('name', '')).strip() or instance['name']
            instance['data'] = convert(value['data'])
            return instance
        return {key: convert(child) for key, child in value.items()}

    source = copy.deepcopy(data)
    public_instances = source.pop('templates', None)
    restored = convert(source)
    if isinstance(public_instances, list):
        restored[TEMPLATE_INSTANCES_KEY] = convert(public_instances)
    # JSON 側に残る旧項目も、現在の構造へ合わせて除外する。
    return project_data_to_schema(restored, normalized_schema)


def new_template_instance(template: dict[str, Any], existing: list[dict[str, Any]]) -> dict[str, Any]:
    """同一テンプレートの連番名を付けた新しい実体を作成する。"""
    template_id = str(template.get('template_id', ''))
    count = sum(
        isinstance(item, dict) and str(item.get('template_id', '')) == template_id
        for item in existing
    )
    name = str(template.get('name', '')).strip()
    return {
        'instance_id': uuid.uuid4().hex,
        'template_id': template_id,
        'template_name': name,
        'name': f'{name} {count + 1}',
        'data': _default_object_data(template),
    }


def copy_template_instance(instance: dict[str, Any], existing: list[dict[str, Any]]) -> dict[str, Any]:
    """値を維持したまま、識別子と表示名を更新して実体を複製する。"""
    copied = copy.deepcopy(instance)
    copied['instance_id'] = uuid.uuid4().hex
    base = str(instance.get('name', '')).strip() or 'テンプレート'
    used = {str(item.get('name', '')).strip() for item in existing}
    candidate = f'{base} - Copy'
    suffix = 2
    while candidate in used:
        candidate = f'{base} - Copy {suffix}'
        suffix += 1
    copied['name'] = candidate
    return copied


def normalize_template_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """旧 data_template ノードを独立したテンプレート定義へ移行する。"""
    result = copy.deepcopy(schema)
    templates = [copy.deepcopy(item) for item in schema_templates(result) if isinstance(item, dict)]

    def extract(nodes: list[dict[str, Any]], path: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        common: list[dict[str, Any]] = []
        for source in nodes:
            node = copy.deepcopy(source)
            current_path = (*path, str(node.get('name', '')))
            if node.get('data_template', False) and node.get('type') == 'object':
                node.pop('data_template', None)
                node['template_id'] = str(node.get('template_id', '')) or uuid.uuid5(
                    uuid.NAMESPACE_URL, f'webflowmanager-template:{".".join(current_path)}',
                ).hex
                templates.append(node)
                continue
            original_children = node.get('children', [])
            if node.get('type') in ('object', 'list'):
                node['children'] = extract(original_children, current_path)
                # テンプレートだけを束ねていた旧 object は共通データへ残さない。
                if original_children and not node['children'] and node.get('type') == 'object':
                    continue
            common.append(node)
        return common

    result['children'] = extract(result.get('children', []))
    seen: set[str] = set()
    normalized_templates: list[dict[str, Any]] = []
    for template in templates:
        template['type'] = 'object'
        template['children'] = template.get('children', [])
        template['template_id'] = str(template.get('template_id', '')) or uuid.uuid4().hex
        if template['template_id'] in seen:
            template['template_id'] = uuid.uuid4().hex
        seen.add(template['template_id'])
        normalized_templates.append(template)
    if normalized_templates or 'templates' in schema:
        result['templates'] = normalized_templates
    else:
        result.pop('templates', None)
    return result


def migrate_legacy_template_data(schema: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """旧 data_template の入れ子値を、新しい複数実体配列へ移行する。"""
    result = copy.deepcopy(data)
    instances = result.get(TEMPLATE_INSTANCES_KEY)
    if not isinstance(instances, list):
        instances = []

    def pop_path(path: list[str]) -> Any:
        current: Any = result
        parents: list[tuple[dict[str, Any], str]] = []
        for key in path[:-1]:
            if not isinstance(current, dict) or not isinstance(current.get(key), dict):
                return None
            parents.append((current, key))
            current = current[key]
        if not isinstance(current, dict):
            return None
        value = current.pop(path[-1], None)
        # テンプレート専用だった空の旧コンテナーもデータから取り除く。
        for parent, key in reversed(parents):
            child = parent.get(key)
            if isinstance(child, dict) and not child:
                parent.pop(key, None)
            else:
                break
        return value

    def walk(nodes: list[dict[str, Any]], path: list[str]) -> None:
        for node in nodes:
            current_path = [*path, str(node.get('name', ''))]
            if node.get('data_template', False) and node.get('type') == 'object':
                value = pop_path(current_path)
                if isinstance(value, dict):
                    template_id = str(node.get('template_id', '')) or uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f'webflowmanager-template:{".".join(current_path)}',
                    ).hex
                    if not any(str(item.get('template_id', '')) == template_id for item in instances):
                        instances.append({
                            'instance_id': uuid.uuid4().hex,
                            'template_id': template_id,
                            'name': f'{node.get("name", "")} 1',
                            'data': value,
                        })
                continue
            if node.get('type') in ('object', 'list'):
                walk(node.get('children', []), current_path)

    walk(schema.get('children', []), [])
    if instances:
        result[TEMPLATE_INSTANCES_KEY] = instances
    return result


def _default_object_data(node: dict[str, Any]) -> dict[str, Any]:
    """循環 import を避け、テンプレート用の既定値を局所的に生成する。"""
    def value(child: dict[str, Any]) -> Any:
        kind = child.get('type')
        if kind == 'object':
            return {item['name']: value(item) for item in child.get('children', [])}
        if kind == 'list':
            return []
        if kind == 'number':
            return 0
        if kind == 'boolean':
            return False
        return ''

    return {child['name']: value(child) for child in node.get('children', [])}
