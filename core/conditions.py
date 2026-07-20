"""業務フローとイベントに付与するガード条件の検証、評価、表示を行う。"""
from __future__ import annotations

import json
from typing import Any, Callable


OPERATORS = ('eq', 'ne', 'contains', 'not_contains', 'gt', 'ge', 'lt', 'le', 'empty', 'not_empty', 'true', 'false')


def decode_guard(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        if not value.strip():
            return {'logic': 'all', 'rules': []}
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError('msg.0380') from error
    return normalize_guard(value)


def normalize_guard(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {'logic': 'all', 'rules': []}
    logic = value.get('logic', 'all')
    rules = value.get('rules', [])
    if logic not in {'all', 'any'} or not isinstance(rules, list):
        raise ValueError('msg.0380')
    normalized: list[dict[str, str]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError('msg.0380')
        path = str(rule.get('path', '')).strip()
        operator = str(rule.get('operator', 'eq'))
        expected = str(rule.get('value', ''))
        if not path or operator not in OPERATORS:
            raise ValueError('msg.0380')
        normalized.append({'path': path, 'operator': operator, 'value': expected})
    return {'logic': logic, 'rules': normalized}


def evaluate_guard(guard: dict[str, Any] | None, resolver: Callable[[str], Any]) -> bool:
    normalized = normalize_guard(guard)
    if not normalized['rules']:
        return True
    results = [_evaluate_rule(resolver(rule['path']), rule['operator'], rule['value']) for rule in normalized['rules']]
    return all(results) if normalized['logic'] == 'all' else any(results)


def summarize_guard(guard: dict[str, Any] | None, operator_labels: dict[str, str] | None=None) -> str:
    normalized = normalize_guard(guard)
    if not normalized['rules']:
        return ''
    labels = operator_labels or {}
    joiner = ' AND ' if normalized['logic'] == 'all' else ' OR '
    parts = []
    for rule in normalized['rules']:
        operator = labels.get(rule['operator'], rule['operator'])
        suffix = '' if rule['operator'] in {'empty', 'not_empty', 'true', 'false'} else f' {rule['value']}'
        parts.append(f'{rule['path']} {operator}{suffix}')
    return joiner.join(parts)


def _evaluate_rule(actual: Any, operator: str, expected: str) -> bool:
    if operator == 'empty':
        return actual is None or actual == '' or actual == [] or actual == {}
    if operator == 'not_empty':
        return not _evaluate_rule(actual, 'empty', expected)
    if operator == 'true':
        return actual is True or str(actual).strip().casefold() in {'true', '1', 'yes', 'on'}
    if operator == 'false':
        return actual is False or str(actual).strip().casefold() in {'false', '0', 'no', 'off', ''}
    if operator in {'contains', 'not_contains'}:
        contained = expected in actual if isinstance(actual, (list, tuple, set, dict)) else expected in str(actual)
        return contained if operator == 'contains' else not contained
    if operator in {'gt', 'ge', 'lt', 'le'}:
        try:
            left, right = float(actual), float(expected)
        except (TypeError, ValueError):
            left, right = str(actual), expected
        return {'gt': left > right, 'ge': left >= right, 'lt': left < right, 'le': left <= right}[operator]
    equal = str(actual) == expected
    return equal if operator == 'eq' else not equal
