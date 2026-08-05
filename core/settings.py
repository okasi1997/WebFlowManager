"""ブラウザー操作で使用できる固定機能を定義する。"""
from __future__ import annotations

DEFAULT_START_URL = 'https://github.com/?locale=ja'
SELECT_FIRST_VALUE = '__WEBFLOW_SELECT_FIRST__'

SUPPORTED_ACTIONS = (
    'goto',
    'click',
    'fill',
    'select',
    'wait',
    'wait_hidden',
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

def runtime_settings(start_url: str) -> dict[str, object]:
    """永続化済みの開始 URL と固定機能から実行時設定を作成する。"""
    return {
        'actions': SUPPORTED_ACTIONS,
        'selector_types': SUPPORTED_SELECTOR_TYPES,
        'picker': {'start_url': start_url},
    }
