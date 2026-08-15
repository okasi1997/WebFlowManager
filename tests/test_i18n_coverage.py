from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from i18n import tr_language


PROJECT_DIR = Path(__file__).resolve().parents[1]
SAME_IN_BOTH_LANGUAGES = {
    '中文', '保存', '使用中', '名称', '概要', '成功', '空', '真', '操作',
}


def _contains_japanese(value: str) -> bool:
    return any(
        '\u3040' <= character <= '\u30ff' or '\u4e00' <= character <= '\u9fff'
        for character in value
    )


class I18nCoverageTests(unittest.TestCase):
    def test_each_language_uses_one_complete_catalog(self) -> None:
        locale_dir = PROJECT_DIR / 'locales'
        ja = json.loads((PROJECT_DIR / 'locales' / 'ja.json').read_text(encoding='utf-8'))
        zh = json.loads((PROJECT_DIR / 'locales' / 'zh.json').read_text(encoding='utf-8'))
        # 言語ごとの補助辞書を作らず、同一キーを持つ主辞書だけを管理する。
        self.assertEqual({path.name for path in locale_dir.glob('*.json')}, {'ja.json', 'zh.json'})
        self.assertEqual(set(ja), set(zh))
        self.assertFalse(any(key.startswith('msg.') for key in ja))
        common_keys = {key for key in ja if key.startswith('common.')}
        self.assertEqual(len(common_keys), 23)
        error_keys = {key for key in ja if key.startswith('error.')}
        self.assertEqual(len(error_keys), 29)
        event_keys = {key for key in ja if key.startswith('event.')}
        self.assertEqual(len(event_keys), 48)
        flow_keys = {key for key in ja if key.startswith('flow.')}
        self.assertEqual(len(flow_keys), 36)
        schema_keys = {key for key in ja if key.startswith('schema.')}
        self.assertEqual(len(schema_keys), 5)
        data_excel_keys = {key for key in ja if key.startswith('data_excel.')}
        self.assertEqual(len(data_excel_keys), 9)
        login_keys = {key for key in ja if key.startswith('login.')}
        self.assertEqual(len(login_keys), 25)
        settings_keys = {key for key in ja if key.startswith('settings.')}
        self.assertEqual(len(settings_keys), 21)
        selector_keys = {key for key in ja if key.startswith('selector.')}
        self.assertEqual(len(selector_keys), 8)
        execution_keys = {key for key in ja if key.startswith('execution.')}
        self.assertEqual(len(execution_keys), 27)
        condition_keys = {key for key in ja if key.startswith('condition.')}
        self.assertEqual(len(condition_keys), 2)
        browser_keys = {key for key in ja if key.startswith('browser.')}
        self.assertEqual(len(browser_keys), 1)
        app_keys = {key for key in ja if key.startswith('app.')}
        self.assertEqual(len(app_keys), 1)
        validation_keys = {key for key in ja if key.startswith('validation.')}
        self.assertEqual(len(validation_keys), 2)
        source_keys = [
            key for key in ja
            if not key.startswith(
                (
                    'common.', 'error.', 'event.', 'flow.', 'schema.',
                    'data_excel.', 'login.', 'settings.', 'selector.', 'execution.',
                    'condition.', 'browser.', 'app.', 'validation.',
                )
            )
        ]
        self.assertGreater(len(source_keys), 0)
        self.assertTrue(all(ja[key] == key for key in source_keys))

    def test_all_designer_texts_have_a_chinese_translation(self) -> None:
        missing: list[str] = []
        for path in (PROJECT_DIR / 'qt_ui' / 'forms').glob('*.ui'):
            for prop in ET.parse(path).getroot().findall('.//property'):
                if prop.get('name') not in {
                    'text', 'title', 'windowTitle', 'placeholderText', 'toolTip',
                }:
                    continue
                source = ''.join(prop.itertext()).strip()
                if (
                    _contains_japanese(source)
                    and source not in SAME_IN_BOTH_LANGUAGES
                    and tr_language(source, 'zh') == source
                ):
                    missing.append(f'{path.name}: {source}')
        self.assertEqual(missing, [])

    def test_python_display_texts_have_a_chinese_translation(self) -> None:
        missing: list[str] = []
        for folder in ('browser', 'core', 'qt_ui'):
            for path in (PROJECT_DIR / folder).rglob('*.py'):
                tree = ast.parse(path.read_text(encoding='utf-8'))
                parents = {
                    child: node for node in ast.walk(tree)
                    for child in ast.iter_child_nodes(node)
                }
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                        continue
                    source = node.value
                    if not _contains_japanese(source) or len(source) > 1000:
                        continue
                    parent = parents.get(node)
                    # 文書文字列とブラウザーへ注入する長いスクリプトは表示翻訳の対象外。
                    if (
                        isinstance(parent, ast.Expr)
                        and parent.value is node
                        and isinstance(
                            parents.get(parent),
                            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                        )
                    ):
                        continue
                    if source not in SAME_IN_BOTH_LANGUAGES and tr_language(source, 'zh') == source:
                        missing.append(f'{path.relative_to(PROJECT_DIR)}:{node.lineno}: {source}')
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
