from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from main import (
    MA_WINDOWS,
    StockNews,
    WeeklyHolding,
    analyze_institutional_rows,
    analyze_news_rows,
    analyze_price_rows,
    analyze_weekly_holdings,
    compare_price,
    extract_stock_names,
    format_lots,
    format_volume,
    load_stock_ids,
    news_query_dates,
    parse_tdcc_rows,
    render_report,
    safe_news_link,
)


class AnalysisTests(unittest.TestCase):
    def test_load_stock_ids_ignores_conditions_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.txt"
            target.write_text("2330\n2454\n2330\nconditions:\n1. text\n", encoding="utf-8")
            self.assertEqual(load_stock_ids(target), ["2330", "2454"])

    def test_compare_price_returns_all_relations(self) -> None:
        self.assertEqual(compare_price(11, 10), "above")
        self.assertEqual(compare_price(9, 10), "below")
        self.assertEqual(compare_price(10, 10), "equal")

    def test_extract_stock_names_deduplicates_categories(self) -> None:
        rows = [
            {"stock_id": "2330", "stock_name": "台積電", "industry_category": "半導體業"},
            {"stock_id": "2330", "stock_name": "台積電", "industry_category": "電子工業"},
            {"stock_id": "2454", "stock_name": "聯發科", "industry_category": "半導體業"},
        ]

        result = extract_stock_names(rows, ["2330", "2454", "9999"])

        self.assertEqual(result["2330"], "台積電")
        self.assertEqual(result["2454"], "聯發科")
        self.assertEqual(result["9999"], "名稱未提供")

    def test_share_and_lot_formats_use_half_up_rounding(self) -> None:
        self.assertEqual(format_lots(1_499), "1")
        self.assertEqual(format_lots(1_500), "2")
        self.assertEqual(format_lots(-1_500), "-2")
        self.assertEqual(format_volume(12_345.4), "12,345")
        self.assertEqual(format_volume(12_345.5), "12,346")

    def test_news_query_dates_include_seven_calendar_days(self) -> None:
        dates = news_query_dates(date(2026, 10, 2))
        self.assertEqual(len(dates), 7)
        self.assertEqual(dates[0], date(2026, 9, 26))
        self.assertEqual(dates[-1], date(2026, 10, 2))

    def test_news_analysis_deduplicates_links_and_sorts_newest_first(self) -> None:
        rows = [
            {
                "date": "2026-10-01 09:00:00",
                "title": "Older",
                "source": "Source A",
                "link": "https://example.com/older",
            },
            {
                "date": "2026-10-02 10:00:00",
                "title": "Newest",
                "source": "Source B",
                "link": "https://example.com/newest",
            },
            {
                "date": "2026-10-02 10:00:00",
                "title": "Duplicate",
                "source": "Source B",
                "link": "https://example.com/newest",
            },
        ]

        result = analyze_news_rows("2330", rows)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].title, "Duplicate")
        self.assertEqual(result[1].title, "Older")
        self.assertEqual(safe_news_link(result[0].link), result[0].link)
        self.assertIsNone(safe_news_link("javascript:alert(1)"))

    def test_tdcc_rows_handle_bom_and_weekly_holding_changes(self) -> None:
        snapshot_dates = [
            "20260828",
            "20260904",
            "20260911",
            "20260918",
            "20260924",
        ]
        rows = []
        for snapshot_date in snapshot_dates:
            for level, shares in {
                12: 40,
                13: 20,
                14: 10,
                15: 600,
                17: 1_000,
            }.items():
                rows.append(
                    {
                        "\ufeff資料日期": snapshot_date,
                        "證券代號": "2357  ",
                        "持股分級": str(level),
                        "股數": str(shares),
                    }
                )
        snapshots = parse_tdcc_rows(rows, ["2357"])
        closes = [965, 1025, 924, 960, 953]
        price_rows = {
            "2357": [
                {"date": snapshot_date, "close": close}
                for snapshot_date, close in zip(
                    ["2026-08-28", "2026-09-04", "2026-09-11", "2026-09-18", "2026-09-24"],
                    closes,
                    strict=True,
                )
            ]
        }

        result = analyze_weekly_holdings(["2357"], snapshots, price_rows)

        self.assertEqual(len(result), 4)
        self.assertEqual(result[0].week_label, "26W39")
        self.assertEqual(result[0].close, 953)
        self.assertEqual(result[0].change, -7)
        self.assertAlmostEqual(result[0].change_percent, -0.7291666667)
        self.assertEqual(result[0].percent_400_to_800, 6)
        self.assertEqual(result[0].percent_800_to_1000, 1)
        self.assertEqual(result[0].percent_over_1000, 60)

    def test_price_analysis_uses_non_overlapping_volume_windows(self) -> None:
        rows = []
        for day in range(1, 61):
            rows.append(
                {
                    "date": f"2026-01-{day:02d}",
                    "close": day,
                    "Trading_Volume": 100 if day <= 50 else 300,
                }
            )

        result = analyze_price_rows("2330", rows)

        self.assertEqual(result.close, 60)
        self.assertEqual(result.trading_date, "2026-01-60")
        self.assertEqual(result.moving_averages[5], 58)
        self.assertEqual(result.moving_averages[10], 55.5)
        self.assertEqual(result.moving_averages[20], 50.5)
        self.assertEqual(result.moving_averages[60], 30.5)
        self.assertTrue(all(result.relations[window] == "above" for window in MA_WINDOWS))
        self.assertEqual(result.recent_average_volume, 300)
        self.assertEqual(result.previous_average_volume, 100)
        self.assertTrue(result.volume_increased)

    def test_price_analysis_requires_60_rows(self) -> None:
        rows = [
            {"date": "2026-01-01", "close": 10, "Trading_Volume": 100}
        ]
        with self.assertRaisesRegex(ValueError, "at least 60"):
            analyze_price_rows("2330", rows)

    def test_institutional_analysis_aggregates_last_three_dates(self) -> None:
        rows = []
        for day in range(1, 5):
            trading_date = f"2026-10-0{day}"
            rows.extend(
                [
                    {
                        "date": trading_date,
                        "name": "Foreign_Investor",
                        "buy": 2_000 * day,
                        "sell": 1_000 * day,
                    },
                    {
                        "date": trading_date,
                        "name": "Investment_Trust",
                        "buy": 1_000,
                        "sell": 2_000,
                    },
                    {
                        "date": trading_date,
                        "name": "Dealer_self",
                        "buy": 500,
                        "sell": 100,
                    },
                    {
                        "date": trading_date,
                        "name": "Dealer_Hedging",
                        "buy": 700,
                        "sell": 200,
                    },
                    {
                        "date": trading_date,
                        "name": "Foreign_Dealer_Self",
                        "buy": 300,
                        "sell": 100,
                    },
                ]
            )

        result = analyze_institutional_rows("2330", rows)

        self.assertEqual([day.trading_date for day in result], ["2026-10-02", "2026-10-03", "2026-10-04"])
        self.assertEqual(result[-1].foreign_net, 4_000)
        self.assertEqual(result[-1].investment_trust_net, -1_000)
        self.assertEqual(result[-1].dealer_net, 900)
        self.assertEqual(result[-1].foreign_dealer_net, 200)
        self.assertEqual(result[-1].total_net, 4_100)

    def test_report_is_traditional_chinese_html(self) -> None:
        price_rows = [
            {
                "date": f"2026-01-{day:02d}",
                "close": day,
                "Trading_Volume": day * 100,
            }
            for day in range(1, 61)
        ]
        institution_rows = [
            {
                "date": f"2026-10-0{day}",
                "name": "Foreign_Investor",
                "buy": 2_000,
                "sell": 1_000,
            }
            for day in range(1, 4)
        ]
        report = render_report(
            [analyze_price_rows("2330", price_rows)],
            analyze_institutional_rows("2330", institution_rows),
            [
                WeeklyHolding(
                    stock_id="2330",
                    week_label="26W39",
                    data_date="2026-09-24",
                    price_date="2026-09-24",
                    close=953,
                    change=-7,
                    change_percent=-0.73,
                    percent_400_to_800=6.63,
                    percent_800_to_1000=2.88,
                    percent_over_1000=65.14,
                )
            ],
            [
                StockNews(
                    stock_id="2330",
                    published_at="2026-10-02 10:00:00",
                    title="Sample news",
                    source="Sample source",
                    link="https://example.com/news",
                )
            ],
            {"2330": "台積電"},
            "2026-10-02",
        )

        self.assertTrue(report.startswith("<!doctype html>"))
        self.assertIn('<html lang="zh-Hant">', report)
        self.assertIn("<h1>台股法人、均線與成交量分析</h1>", report)
        self.assertIn('<span class="stock-code">2330</span>', report)
        self.assertIn('<span class="stock-name">台積電</span>', report)
        self.assertIn('<td class="relation-above" data-sort-value="58.0">58(&gt;)</td>', report)
        self.assertNotIn("<th>關係</th>", report)
        self.assertIn("最近三個交易日法人買賣", report)
        self.assertIn('aria-controls="institution-2330"', report)
        self.assertIn('<tr class="institution-details" id="institution-2330" hidden>', report)
        self.assertIn("最近四週股價與持股分級", report)
        self.assertIn("＞400張～≤800張", report)
        self.assertIn("26W39", report)
        self.assertIn("近七日個股新聞（2026-09-26 至 2026-10-02）", report)
        self.assertIn('href="https://example.com/news"', report)
        self.assertLess(report.index("最近四週股價與持股分級"), report.index("近七日個股新聞"))
        self.assertNotIn("<h2>最近三個交易日法人買賣</h2>", report)
        self.assertEqual(report.count("<table"), 4)
        self.assertEqual(report.count("<tbody"), 4)


if __name__ == "__main__":
    unittest.main()
