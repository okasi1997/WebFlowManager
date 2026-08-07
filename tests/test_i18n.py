import unittest

from core.conditions import OPERATORS
from i18n import get_language, set_language
from ui.dialogs import guard_operator_labels


class GuardOperatorTranslationTests(unittest.TestCase):
    def test_all_guard_operators_are_translated_in_every_language(self) -> None:
        original_language = get_language()
        try:
            for language in ('ja', 'zh'):
                set_language(language)
                labels = guard_operator_labels()
                self.assertEqual(set(labels), set(OPERATORS))
                self.assertTrue(all(not label.startswith('msg.') for label in labels.values()))
        finally:
            set_language(original_language)


if __name__ == '__main__':
    unittest.main()
