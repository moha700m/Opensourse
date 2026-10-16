import unittest

from openshorts.pipeline import _wrap_caption


class CaptionTests(unittest.TestCase):
    def test_wrapping_keeps_every_word_with_at_most_two_lines(self):
        # A character-count budget alone can fit three long words into a
        # group that actually needs three lines, losing the last word.
        texts = [
            "a" * 22 + " " + "b" * 22 + " " + "c" * 18,
            "هذه تجربة لترجمة الفيديو إلى مقاطع قصيرة مع الحفاظ على كل الكلمات عند تقسيم النص إلى سطرين مناسبين للجوال",
        ]
        for text in texts:
            with self.subTest(text=text):
                captions = _wrap_caption(text)
                self.assertTrue(all(len(caption) <= 2 for caption in captions))
                actual = " ".join(line for caption in captions for line in caption)
                self.assertEqual(actual.split(), text.split())


if __name__ == "__main__":
    unittest.main()
