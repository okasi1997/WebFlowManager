"""固定機能と settings.json のユーザー設定を実行時形式へまとめる。"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

SUPPORTED_ACTIONS = (
    'goto',
    'click',
    'fill',
    'select',
    'wait',
    'press',
    'get_text',
    'screenshot',
    'pause',
    'upload_file',
    'loop_start',
    'loop_end',
    'retry_start',
    'retry_end',
    'group_start',
    'group_end',
)

SUPPORTED_SELECTOR_TYPES = (
    'none',
    'role',
    'label',
    'placeholder',
    'text',
    'css',
    'xpath',
)

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
    picker = raw.get('picker', {})
    if not isinstance(picker, dict):
        raise SettingsError('msg.0234')
    start_url = picker.get('start_url', 'https://example.com/')
    if not isinstance(start_url, str) or not start_url.startswith(('http://', 'https://')):
        raise SettingsError('msg.0235')
    return {
        'actions': SUPPORTED_ACTIONS,
        'selector_types': SUPPORTED_SELECTOR_TYPES,
        'picker': {'start_url': start_url},
    }
