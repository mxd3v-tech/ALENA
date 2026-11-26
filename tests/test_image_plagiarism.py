import unittest

from checker import summary
from checker.image_plagiarism import check_image_plagiarism


def picture(bits, label, is_ui=False):
    # The reference image has an all-zero pHash.
    return {'page': 2, 'hashes': [f'{(1 << bits) - 1:036x}'],
            'thumb': label, 'is_ui': is_ui}


class PageEvidenceTests(unittest.TestCase):
    def assert_page_evidence(self, candidates, expected_thumb, expected_review):
        source = {'path': 'a.pdf', 'student': {'name': 'Иванов'},
                  'images': [picture(0, 'original')]}
        for historical in (False, True):
            for images in (candidates, list(reversed(candidates))):
                with self.subTest(historical=historical, images=[i['thumb'] for i in images]):
                    other = {'path': 'b.pdf', 'student': {'name': 'Петров'},
                             'is_historical': historical,
                             'precomputed_images' if historical else 'images': images}
                    result = check_image_plagiarism([source, other])
                    pair, = result['pairs']
                    self.assertEqual(pair['img2'], expected_thumb)
                    self.assertEqual(pair['ui_review'], expected_review)
                    current, history = ([source], [other]) if historical else ([source, other], [])
                    digest = summary.build(current, history, {}, result, 0.6)
                    self.assertEqual(digest['matches_total'], 0 if expected_review else 1)

    def test_exact_copy_wins_over_similar_screenshot(self):
        self.assert_page_evidence([picture(8, 'review', True), picture(0, 'exact')],
                                  'exact', False)

    def test_confirmed_copy_wins_over_closer_review(self):
        self.assert_page_evidence([picture(8, 'review', True), picture(15, 'copy')],
                                  'copy', False)

    def test_closest_screenshot_is_kept_for_review(self):
        self.assert_page_evidence([picture(12, 'far', True), picture(8, 'near', True)],
                                  'near', True)
