import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from finmind_cache import CachedFinMindClient, TAIPEI, dates_between


PRICE = "TaiwanStockPrice"


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "cache.sqlite3"
        self.now = datetime(2026, 10, 5, 20, tzinfo=TAIPEI)
        self.remote = Mock()
        self.remote.fetch.side_effect = self.price_rows
        self.client = CachedFinMindClient(self.remote, self.path, now=lambda: self.now)
        self.addCleanup(self.client.close)

    @staticmethod
    def price_rows(dataset, stock, start, end):
        return [{"date": day.isoformat(), "stock_id": stock, "close": 100, "Trading_Volume": 1000}
                for day in dates_between(date.fromisoformat(start), date.fromisoformat(end))
                if day.weekday() < 5]

    def fetch(self, start="2026-09-01", end="2026-10-05"):
        return self.client.fetch(PRICE, "2330", start, end)

    def test_initial_fetch_and_refresh_only_recent_week(self):
        expected = self.fetch()
        self.remote.fetch.assert_called_once_with(PRICE, "2330", "2026-09-01", "2026-10-05")
        self.remote.fetch.reset_mock()
        self.assertEqual(expected, self.fetch())
        self.remote.fetch.assert_called_once_with(PRICE, "2330", "2026-09-29", "2026-10-05")

    def test_missing_hole_is_merged_and_empty_weekends_are_cached(self):
        self.fetch()
        self.client.connection.execute("DELETE FROM daily_cache WHERE day BETWEEN '2026-09-14' AND '2026-09-15'")
        self.client.connection.commit()
        self.remote.fetch.reset_mock()
        self.fetch()
        self.assertEqual([call.args[2:] for call in self.remote.fetch.call_args_list],
                         [("2026-09-14", "2026-09-15"), ("2026-09-29", "2026-10-05")])

    def test_cache_survives_reopening_and_refresh_replaces_corrections(self):
        self.fetch()
        self.client.close()
        self.client = CachedFinMindClient(self.remote, self.path, now=lambda: self.now)
        self.addCleanup(self.client.close)
        self.remote.fetch.side_effect = lambda *args: [dict(row, close=200) for row in self.price_rows(*args)]
        rows = self.fetch()
        self.assertEqual(rows[0]["close"], 100)
        self.assertEqual(rows[-1]["close"], 200)
        self.assertEqual(len({row["date"] for row in rows}), len(rows))

    def test_forced_refresh_only_replaces_requested_range(self):
        self.fetch()
        self.client.refresh = True
        self.remote.fetch.reset_mock()
        self.fetch("2026-09-10", "2026-09-20")
        self.remote.fetch.assert_called_once_with(PRICE, "2330", "2026-09-10", "2026-09-20")
        self.assertIsNotNone(self.client.connection.execute("SELECT 1 FROM daily_cache WHERE day='2026-09-01'").fetchone())

    def test_failed_refresh_uses_complete_cache_and_preserves_timestamp(self):
        expected = self.fetch()
        before = self.client.connection.execute("SELECT * FROM daily_cache ORDER BY day").fetchall()
        self.remote.fetch.side_effect = RuntimeError("offline")
        self.assertEqual(self.fetch(), expected)
        self.assertIn("最後成功更新", self.client.warnings[-1])
        self.assertEqual(before, self.client.connection.execute("SELECT * FROM daily_cache ORDER BY day").fetchall())

    def test_missing_required_data_fails_without_marking_coverage(self):
        self.remote.fetch.side_effect = RuntimeError("offline")
        with self.assertRaisesRegex(RuntimeError, "cache is incomplete"):
            self.fetch()
        self.assertEqual(self.client.connection.execute("SELECT count(*) FROM daily_cache").fetchone()[0], 0)

    def test_invalid_payload_does_not_overwrite_good_cache(self):
        expected = self.fetch()
        self.remote.fetch.side_effect = None
        self.remote.fetch.return_value = [{"date": "2026-10-05", "close": None, "Trading_Volume": 1}]
        self.assertEqual(self.fetch(), expected)
        self.assertTrue(self.client.warnings)

    def test_invalid_cached_json_is_refetched(self):
        self.fetch()
        self.client.connection.execute("UPDATE daily_cache SET payload='broken' WHERE day='2026-09-02'")
        self.client.connection.commit()
        self.remote.fetch.reset_mock()
        self.fetch()
        self.assertEqual(self.remote.fetch.call_args_list[0].args[2:], ("2026-09-02", "2026-09-02"))

    def test_news_today_refreshes_and_historical_ttl(self):
        self.remote.fetch_stock_news.return_value = []
        self.client.fetch_stock_news("2330", self.now.date())
        self.assertEqual(self.remote.fetch_stock_news.call_count, 3)
        self.remote.fetch_stock_news.reset_mock()
        self.client.fetch_stock_news("2330", self.now.date())
        self.remote.fetch_stock_news.assert_called_once_with("2330", self.now.date(), days=1)
        self.now += timedelta(days=1)
        self.remote.fetch_stock_news.reset_mock()
        self.client.fetch_stock_news("2330", date(2026, 10, 5))
        self.assertEqual(self.remote.fetch_stock_news.call_count, 3)

    def test_news_failure_is_reported_without_failing_report(self):
        self.remote.fetch_stock_news.side_effect = RuntimeError("offline")
        self.assertEqual(self.client.fetch_stock_news("2330", self.now.date(), days=1), [])
        self.assertIn("新聞資料取得失敗", self.client.warnings[-1])

    def test_names_cache_ttl_missing_stock_and_failure(self):
        self.remote.fetch_stock_info.return_value = [{"stock_id": "2330", "stock_name": "台積電"}]
        self.assertEqual(self.client.fetch_stock_names(["2330"]), {"2330": "台積電"})
        self.client.fetch_stock_names(["2330"])
        self.remote.fetch_stock_info.assert_called_once()
        self.client.fetch_stock_names(["2330", "9999"])
        self.assertEqual(self.remote.fetch_stock_info.call_count, 2)
        self.now += timedelta(days=7)
        self.remote.fetch_stock_info.side_effect = RuntimeError("offline")
        self.assertEqual(self.client.fetch_stock_names(["2330", "9999"]), {"2330": "台積電", "9999": "9999"})
        self.assertIn("最後成功更新", self.client.warnings[-1])

    def test_sqlite_unavailable_uses_direct_download_without_deleting_file(self):
        self.client.close()
        self.path.write_text("not a database", encoding="utf-8")
        client = CachedFinMindClient(self.remote, self.path, now=lambda: self.now)
        self.addCleanup(client.close)
        self.assertTrue(client.fetch(PRICE, "2330", "2026-10-01", "2026-10-05"))
        self.assertIn("SQLite", client.warnings[0])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "not a database")

    def test_write_failure_returns_downloaded_data(self):
        self.client.connection.execute("PRAGMA query_only=ON")
        self.assertTrue(self.fetch())
        self.assertIsNone(self.client.connection)
        self.assertIn("SQLite", self.client.warnings[0])

    def test_distinct_stock_and_dataset_keys(self):
        self.fetch()
        self.remote.fetch.reset_mock()
        self.client.fetch(PRICE, "2454", "2026-09-01", "2026-10-05")
        self.remote.fetch.assert_called_once_with(PRICE, "2454", "2026-09-01", "2026-10-05")
        self.remote.fetch.side_effect = None
        self.remote.fetch.return_value = []
        self.client.fetch("TaiwanStockInstitutionalInvestorsBuySell", "2330", "2026-09-01", "2026-10-05")
        self.assertEqual(self.remote.fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
