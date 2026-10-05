import json
import re
import shutil
import subprocess
import unittest
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from unittest.mock import Mock

import main
from mail_report import extract_report_scores
from score_history import ScoreDay, render_history


def sample_rows(count=75):
    start = date(2026, 6, 1)
    dates = []
    while len(dates) < count:
        if start.weekday() < 5:
            dates.append(start.isoformat())
        start += timedelta(days=1)
    prices = [{"date": day, "close": 100 + index % 7, "Trading_Volume": 1000 + index * 10}
              for index, day in enumerate(dates)]
    institutions = [{"date": day, "name": "Foreign_Investor", "buy": 10 if index % 6 < 4 else 0, "sell": 1}
                    for index, day in enumerate(dates)]
    return prices, institutions


class ScoreHistoryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the report JavaScript test")
    def test_generated_javascript_updates_chart_and_scores(self):
        prices, institutions = sample_rows(64)
        history = main.analyze_score_history("2330", prices, institutions)
        analysis = main.analyze_price_rows("2330", prices)
        days = main.analyze_institutional_rows("2330", institutions)
        report = main.render_report([analysis], days, [], [], {"2330": "台積電"}, prices[-1]["date"],
                                    adjustable_scores=True, score_histories={"2330": history})
        from dataclasses import asdict, replace
        second_history = [replace(day, stock_id="2454", conditions=None if day.conditions is None else
                                  dict(day.conditions, ma5=False, ma10=False)) for day in history]
        payload = {"script": re.search(r"<script>(.*?)</script>", report, re.S)[1],
                   "histories": {"2330": [asdict(day) for day in history], "2454": [asdict(day) for day in second_history]},
                   "defaults": {key: value for key, _, value in main.SCORE_RULES}}
        result = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("test_report_ui.cjs"))],
                                input=json.dumps(payload), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ten_trading_days_match_independent_daily_calculation(self):
        prices, institutions = sample_rows()
        history = main.analyze_score_history("2330", prices, institutions)
        self.assertEqual([day.trading_date for day in history], [row["date"] for row in prices[-10:]])
        for day in history:
            analysis = main.analyze_price_rows("2330", [row for row in prices if row["date"] <= day.trading_date])
            daily = main.analyze_institutional_rows("2330", [row for row in institutions if row["date"] <= day.trading_date])
            self.assertEqual(day.score, main.calculate_score(analysis, daily))
            self.assertEqual(day.conditions, main.score_conditions(analysis, daily))

    def test_future_price_and_institution_data_cannot_change_earlier_scores(self):
        prices, institutions = sample_rows()
        before = main.analyze_score_history("2330", prices[:-1], institutions[:-1])
        prices[-1]["close"] = 9999
        institutions[-1]["buy"] = 99999
        after = main.analyze_score_history("2330", prices, institutions)
        self.assertEqual(before[1:], after[:-1])

    def test_insufficient_price_history_creates_gaps_and_institutions_no_bonus(self):
        prices, _ = sample_rows(64)
        history = main.analyze_score_history("2330", prices, [])
        self.assertEqual(sum(day.score is None for day in history), 5)
        self.assertTrue(all(not day.conditions["institution"] for day in history if day.conditions))
        rendered = render_history(history, "test")
        self.assertEqual(rendered.count('class="history-point"'), 5)
        self.assertIn("資料不足", rendered)
        self.assertIn("資料不足", render_history([ScoreDay("2330", "2026-10-01", None, None)], "test"))

    def test_chart_gap_breaks_path_and_negative_values_expand_axis(self):
        days = [ScoreDay("2330", f"2026-10-0{index+1}", {} if value is not None else None, value)
                for index, value in enumerate([80, None, -150])]
        rendered = render_history(days, '<script>')
        path = re.search(r'class="history-line" d="([^"]*)"', rendered)[1]
        self.assertEqual(path.count("M"), 2)
        self.assertNotIn("L", path)
        self.assertIn(">-160</text>", rendered)
        self.assertNotIn("<script>", rendered)

    def test_two_reports_latest_matches_overview_and_email_parser(self):
        prices, institutions = sample_rows()
        analysis = main.analyze_price_rows("2330", prices)
        days = main.analyze_institutional_rows("2330", institutions)
        history = main.analyze_score_history("2330", prices, institutions)
        for adjustable in (False, True):
            report = main.render_report([analysis], days, [], [], {"2330": "台積電"}, prices[-1]["date"],
                                        adjustable_scores=adjustable, score_histories={"2330": history},
                                        data_warnings=["新聞資料取得失敗 <test>"])
            self.assertEqual(extract_report_scores(report), [(history[-1].score, "2330", "台積電")])
            embedded = json.loads(unescape(re.search(r'data-history="([^"]*)"', report)[1]))
            self.assertEqual(len(embedded), 10)
            self.assertEqual(embedded[-1]["score"], history[-1].score)
            self.assertIn("<svg", report)
            self.assertNotIn('<script src=', report)
            self.assertIn("新聞資料取得失敗 &lt;test&gt;", report)
            self.assertEqual("function updateHistory" in report, adjustable)

    def test_analysis_passes_history_and_honors_cutoff(self):
        prices, institutions = sample_rows()
        remote, tdcc = Mock(), Mock()
        remote.fetch.side_effect = [prices, institutions]
        remote.fetch_stock_names.return_value = {"2330": "台積電"}
        remote.fetch_stock_news.return_value = []
        tdcc.fetch_recent_snapshots.return_value = {}
        from unittest.mock import patch
        with patch.object(main, "analyze_weekly_holdings", return_value=[]):
            result = main.run_analysis(["2330"], date.fromisoformat(prices[-2]["date"]), remote, tdcc)
        self.assertEqual(result[0][0].trading_date, prices[-2]["date"])
        self.assertEqual(result[-1]["2330"][-1].trading_date, prices[-2]["date"])


if __name__ == "__main__":
    unittest.main()
