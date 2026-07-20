"""settings.json を検証し、実行時に扱いやすい形式へ正規化する。"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

class SettingsError(ValueError):
    pass

def load_settings(path: Path) -> dict[str, Any]:
    # 設定不備は起動直後に具体的な理由付きで通知する。
    try:
        raw: Any = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as error:
        raise SettingsError(f'msg.0228{path}') from error
    except json.JSONDecodeError as error:
        raise SettingsError(f'msg.0229{error}') from error
    if not isinstance(raw, dict):
        raise SettingsError('msg.0230')
    result: dict[str, Any] = {}
    for key in ('actions', 'selector_types'):
        values = raw.get(key)
        if not isinstance(values, list) or not values:
            raise SettingsError(f'{key}msg.0231')
        if any((not isinstance(value, str) or not value.strip() for value in values)):
            raise SettingsError(f'{key}msg.0232')
        normalized = tuple((value.strip() for value in values))
        if len(set(normalized)) != len(normalized):
            raise SettingsError(f'{key}msg.0233')
        result[key] = normalized
    picker = raw.get('picker', {})
    if not isinstance(picker, dict):
        raise SettingsError('msg.0234')
    start_url = picker.get('start_url', 'https://example.com/')
    if not isinstance(start_url, str) or not start_url.startswith(('http://', 'https://')):
        raise SettingsError('msg.0235')
    result['picker'] = {'start_url': start_url}
    return result
