import argparse
import json
import tempfile
import unittest
from dataclasses import replace
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
            main.InstitutionalDay("2330", f"2026-09-{day}", 6, 0, 0, 0, 0, 6, 0)
            for day in (17, 18, 21, 22, 23, 24, 25, 28, 29, 30)
        ]

    def test_institution_bonus_requires_ten_day_average_above_three_percent(self):
        self.assertFalse(main.score_conditions(self.price, self.days[1:])["institution"])
        for net in (-1, 0, 2.99, 3):
            with self.subTest(net=net):
                days = [replace(day, total_buy=net) for day in self.days]
                self.assertFalse(main.score_conditions(self.price, days)["institution"])
        days = [replace(day, total_buy=3.01) for day in self.days]
        self.assertTrue(main.score_conditions(self.price, days)["institution"])
        self.assertFalse(main.score_conditions(replace(self.price, recent_average_volume=200), days)["institution"])

    def test_institution_bonus_allows_net_selling_days(self):
        days = [replace(self.days[0], total_buy=0, total_sell=20), *self.days[1:]]
        self.assertTrue(main.score_conditions(self.price, days)["institution"])
        self.assertEqual(main.calculate_score(self.price, days), 85)
        days[0] = replace(days[0], total_sell=24)  # Average is exactly 3%.
        self.assertFalse(main.score_conditions(self.price, days)["institution"])
        days[0] = replace(days[0], total_sell=54)  # Average is zero.
        self.assertFalse(main.score_conditions(self.price, days)["institution"])

    def test_institution_window_ignores_old_future_and_other_stock_days(self):
        extra = [replace(self.days[0], trading_date="2026-09-16", total_buy=0),
                 replace(self.days[-1], trading_date="2026-10-05", total_buy=0),
                 replace(self.days[-1], stock_id="2454", total_buy=0)]
        self.assertTrue(main.score_conditions(self.price, list(reversed(self.days + extra)))["institution"])

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
                 patch.object(main, "CachedFinMindClient") as cached, \
                 patch.object(main, "run_analysis", return_value=(
                     [self.price], self.days, [], [], {"2330": "台積電"}, {}
                 )) as fetch:
                cached.return_value.warnings = []
                self.assertEqual(main.main(), 0)
            fetch.assert_called_once()
            self.assertNotIn('id="score-form"', output.read_text(encoding="utf-8"))
            self.assertIn('id="score-form"', (output.parent / "analysis_param.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
