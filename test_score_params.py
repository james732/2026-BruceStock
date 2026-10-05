import argparse
import json
import tempfile
import unittest
from datetime import date
from html import unescape
from pathlib import Path
from unittest.mock import patch
import re

import main


class ScoreParameterTests(unittest.TestCase):
    def setUp(self):
        self.price = main.PriceAnalysis(
            "2330", "2026-10-02", 10,
            {5: 10, 10: 10, 20: 10, 60: 10},
            {5: "equal", 10: "equal", 20: "equal", 60: "equal"},
            100, 100, False,
        )
        self.days = [
            main.InstitutionalDay("2330", f"2026-09-{day}", 1, 0, 0, 0, 0, 1, 0)
            for day in (28, 29, 30)
        ]

    def test_equal_ma_only_penalizes_short_windows_and_volume(self):
        self.assertEqual(main.calculate_score(self.price, []), 75)
        self.assertEqual(main.calculate_score(self.price, self.days[:2]), 75)
        self.assertEqual(main.calculate_score(self.price, self.days), 85)
        self.price.relations.update({window: "below" for window in main.MA_WINDOWS})
        self.assertEqual(main.calculate_score(self.price, self.days), 35)
        self.price.relations.update({window: "above" for window in main.MA_WINDOWS})
        self.assertEqual(main.calculate_score(self.price, self.days), 100)

    def test_report_embeds_exact_conditions_and_keeps_static_report(self):
        args = ([self.price], self.days, [], [], {"2330": "台積電"}, "2026-10-02")
        report = main.render_report(*args, adjustable_scores=True)
        conditions = json.loads(unescape(re.search(r'data-score-conditions="([^"]+)"', report)[1]))
        self.assertEqual(conditions, {
            "ma5": True, "ma10": True, "ma20": False, "ma60": False,
            "volume": True, "institution": True,
        })
        self.assertEqual(len(re.findall(r'<input [^>]*type="number"', report)), 6)
        self.assertIn('data-sort-value="85">85</td>', report)
        self.assertNotIn('id="score-form"', main.render_report(*args))

    def test_main_writes_both_reports_from_one_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "custom.html"
            args = argparse.Namespace(output=output, target=Path("unused"), end_date=date(2026, 10, 2))
            with patch.object(main, "parse_arguments", return_value=args), \
                 patch.object(main, "load_stock_ids", return_value=["2330"]), \
                 patch.object(main, "run_analysis", return_value=(
                     [self.price], self.days, [], [], {"2330": "台積電"}
                 )) as fetch:
                self.assertEqual(main.main(), 0)
            fetch.assert_called_once()
            self.assertNotIn('id="score-form"', output.read_text(encoding="utf-8"))
            self.assertIn('id="score-form"', (output.parent / "analysis_param.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
