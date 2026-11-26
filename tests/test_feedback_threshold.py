import unittest

from checker import grading, summary
from checker.reporter import generate_html_report
from checker.text_plagiarism import check_text_plagiarism


def work(path, words):
    return {'path': path, 'full_text': ' '.join(words)}


def outputs(reports, threshold):
    text = check_text_plagiarism(reports, threshold)
    digest = summary.build(reports, [], text, {}, threshold)
    html = generate_html_report(reports, [], text, {}, threshold)
    return digest, html


class FeedbackThresholdTests(unittest.TestCase):
    def test_feedback_agrees_with_detector_at_fractional_boundaries(self):
        # 149 shared five-word shingles out of 250 gives exactly 59.6%.
        words = [f'слово{i}' for i in range(254)]
        works = [work('a', words),
                 work('b', words[:153] + [f'иной{i}' for i in range(101)])]
        for threshold, percent, flagged in ((0.6, 60, False),
                                            (0.5961, 59.61, False),
                                            (0.596, 59.6, True),
                                            (0.5959, 59.59, True)):
            with self.subTest(threshold=threshold):
                digest, html = outputs(works, threshold)
                self.assertEqual(digest['threshold'], percent)
                self.assertEqual(digest['plag'], 59.6)
                self.assertEqual(digest['plag_flagged'], flagged)
                self.assertEqual(bool(digest['matches']), flagged)
                self.assertIn(f'Порог заимствования: {percent}%', html)
                self.assertEqual('Совпадение с другой работой' in html, flagged)
                for student in digest['students']:
                    self.assertEqual(student['plag_flagged'], flagged)
                    feedback = grading.feedback_text(student, digest['threshold'])
                    self.assertEqual('Совпадение с другой работой' in feedback, flagged)
                    if flagged:
                        line = f'Совпадение с другой работой – 59.6% (допустимый порог {percent}%)'
                        self.assertIn(line, feedback)
                        self.assertIn(line, html)

    def test_zero_threshold_requires_a_measured_score(self):
        reports = [work(tag, [f'{tag}{i}' for i in range(50)]) for tag in ('a', 'b')]
        for batch, flagged in ((reports[:1], False), (reports, True)):
            with self.subTest(size=len(batch)):
                digest, html = outputs(batch, 0)
                self.assertEqual(bool(digest['matches']), flagged)
                self.assertEqual(digest['plag_flagged'], flagged)
                self.assertEqual('class="badge badge-red">Заимствование' in html, flagged)
                self.assertEqual('Совпадение с другой работой – 0%' in html, flagged)
                for student in digest['students']:
                    self.assertEqual(student['plag_flagged'], flagged)
                    self.assertEqual(bool(grading.feedback_lines(student, 0)), flagged)

    def test_display_precision_preserves_the_side_of_the_threshold(self):
        self.assertEqual(grading.similarity_percent(0.59994, 0.6), 59.994)
        self.assertEqual(grading.similarity_percent(0.60023, 0.6002), 60.023)
        self.assertEqual(grading.similarity_percent(1 / 4000, 0.6), 0.025)
