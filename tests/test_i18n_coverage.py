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
    def test_msgid_sets_remain_shared_and_do_not_grow_for_source_texts(self) -> None:
        ja = json.loads((PROJECT_DIR / 'locales' / 'ja.json').read_text(encoding='utf-8'))
        zh = json.loads((PROJECT_DIR / 'locales' / 'zh.json').read_text(encoding='utf-8'))
        self.assertEqual(set(ja), set(zh))
        self.assertEqual(len(ja), 478)

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
