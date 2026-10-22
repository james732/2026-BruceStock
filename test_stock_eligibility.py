"""Keep a data-poor watchlist entry from blocking every daily report."""
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import Mock

from main import TAIPEI_TIMEZONE, run_analysis, stock_history_issue
from momentum_report import generate
from momentum_scoring import Config


def prices(count=60, end=date(2026, 10, 2)):
    return [
        {'date': (end - timedelta(days=count - i - 1)).isoformat(),
         'close': 100 + i, 'Trading_Volume': 100000 + i}
        for i in range(count)
    ]


class StockEligibilityTests(unittest.TestCase):
    def test_eligibility_insufficient_stale_and_valid(self):
        cutoff = date(2026, 10, 2)
        self.assertIn('僅 39 筆', stock_history_issue('6806', prices(39), cutoff))
        self.assertIn('超過 14 天', stock_history_issue(
            '6806', prices(60, date(2026, 6, 23)), cutoff))
        self.assertIsNone(stock_history_issue('2330', prices(60), cutoff))

    def test_original_report_skips_6806_and_excludes_it_from_tdcc(self):
        cutoff = date(2026, 10, 2)
        valid = prices(60)
        invalid = prices(39)
        client = Mock()
        client.fetch_stock_names.return_value = {'6806': 'Test', '2330': 'Test'}
        client.fetch.side_effect = lambda dataset, symbol, *args: (
            invalid if symbol == '6806' else valid
        ) if dataset == 'TaiwanStockPrice' else []
        client.fetch_stock_news.return_value = []
        client.warnings = []
        weekly = {
            date(2026, 9, 4) + timedelta(days=7 * i):
            {'2330': {12: 100, 13: 100, 14: 100, 15: 600, 17: 1000}}
            for i in range(5)
        }
        tdcc = Mock()
        tdcc.fetch_recent_snapshots.return_value = weekly
        result = run_analysis(['6806', '2330'], cutoff, client, tdcc)
        self.assertEqual([row.stock_id for row in result[0]], ['2330'])
        self.assertEqual(len(result[2]), 4)
        self.assertEqual(list(result[5]), ['2330'])
        self.assertTrue(any('6806' in warning for warning in client.warnings))
        tdcc.fetch_recent_snapshots.assert_called_once_with(['2330'], cutoff, count=5)

    def test_momentum_skips_6806_without_failing_other_stock(self):
        cutoff = date(2026, 10, 2)
        remote = Mock()
        remote._fetch_rows.return_value = [{'date': cutoff.isoformat()}]
        remote.fetch_stock_names.return_value = {'6806': 'Test', '2330': 'Test'}
        cache = Mock()
        cache.fetch.side_effect = lambda dataset, symbol, *args: (
            prices(39) if symbol == '6806' else prices(60)
        ) if dataset == 'TaiwanStockPrice' else []
        cache.fetch_stock_news.return_value = []
        cache.warnings = []
        tdcc = Mock()
        tdcc.fetch_recent_snapshots.side_effect = RuntimeError('no weekly')
        html = generate(['6806', '2330'], cutoff, remote, cache, tdcc, Config(),
                        datetime(2026, 10, 2, 16, tzinfo=TAIPEI_TIMEZONE))
        self.assertIn('2330', html)
        self.assertIn('6806 歷史股價僅 39 筆', html)
        self.assertNotIn('institution-6806', html)
        cache.fetch_stock_news.assert_called_once_with('2330', cutoff, days=3)


if __name__ == '__main__':
    unittest.main()
