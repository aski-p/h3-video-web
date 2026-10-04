import unittest
from studio_native_verify import matches_dialogue


class NativeDialogueTests(unittest.TestCase):
    def test_punctuation_and_spacing_only(self):
        self.assertTrue(matches_dialogue('나 지금 좀 떨리나봐', '나 지금 좀 떨리나 봐.'))

    def test_foreign_opening_is_not_discarded(self):
        for opening in ['Hello ', 'こんにちは ', 'Bonjour ', '123 ']:
            self.assertFalse(matches_dialogue(opening + '나 지금 좀 떨리나 봐.', '나 지금 좀 떨리나 봐.'))

    def test_reported_extra_opening_and_empty_fail(self):
        self.assertFalse(matches_dialogue('아 왜 저나 깃털 올라자 이스타벌 아살렘 나 지금 좀 떨리나 봐', '나 지금 좀 떨리나 봐.'))
        self.assertFalse(matches_dialogue('', ''))


if __name__ == '__main__':
    unittest.main()
