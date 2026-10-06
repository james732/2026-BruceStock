from __future__ import annotations

import argparse
import csv
import html
import io
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

from finmind_cache import CachedFinMindClient
from score_history import HISTORY_CSS, HISTORY_SCRIPT, ScoreDay, render_history


API_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiamFtZXM3MzJAZ21haWwuY29tIiwiZW1haWwiOiJqYW1lczczMkBnbWFpbC5jb20iLCJ0b2tlbl92ZXJzaW9uIjowfQ.cQ1k-Dfqs4Ggb8CdEwHErdmSLJdRFbo8WxtScv_MjWc"
API_URL = "https://api.finmindtrade.com/api/v4/data"
TDCC_OFFICIAL_URL = "https://openapi.tdcc.com.tw/v1/opendata/1-5"
TDCC_ARCHIVE_CONTENTS_URL = (
    "https://api.github.com/repos/wirelessr/tdcc-opendata-archive/contents/snapshots/{year}"
)
TAIPEI_TIMEZONE = timezone(timedelta(hours=8), name="UTC+08:00")
LOOKBACK_CALENDAR_DAYS = 180
MA_WINDOWS = (5, 10, 20, 60)
SCORE_RULES = (
    ("ma5", "收盤未高於 5 日均線", -5),
    ("ma10", "收盤未高於 10 日均線", -10),
    ("ma20", "收盤低於月線", -20),
    ("ma60", "收盤低於季線", -30),
    ("volume", "最近均量未增加", -10),
    ("institution", "法人近十個交易日平均淨買超為正值，且大於近十日平均成交量的 3%", 10),
)


@dataclass(frozen=True)
class PriceAnalysis:
    stock_id: str
    trading_date: str
    close: float
    moving_averages: dict[int, float]
    relations: dict[int, str]
    recent_average_volume: float
    previous_average_volume: float
    volume_increased: bool
    previous_close: float | None = None


@dataclass(frozen=True)
class InstitutionalDay:
    stock_id: str
    trading_date: str
    foreign_net: float
    investment_trust_net: float
    dealer_net: float
    foreign_dealer_net: float
    other_net: float
    total_buy: float
    total_sell: float

    @property
    def total_net(self) -> float:
        return self.total_buy - self.total_sell


@dataclass(frozen=True)
class StockNews:
    stock_id: str
    published_at: str
    title: str
    source: str
    link: str


@dataclass(frozen=True)
class WeeklyHolding:
    stock_id: str
    week_label: str
    data_date: str
    price_date: str
    close: float
    change: float
    change_percent: float
    percent_400_to_800: float
    percent_800_to_1000: float
    percent_over_1000: float


class FinMindClient:
    def __init__(self, token: str, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def fetch(
        self,
        dataset: str,
        stock_id: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        return self._fetch_rows(
            {
                "dataset": dataset,
                "data_id": stock_id,
                "start_date": start_date,
                "end_date": end_date,
            }
        )

    def fetch_stock_names(self, stock_ids: Iterable[str]) -> dict[str, str]:
        rows = self.fetch_stock_info()
        return extract_stock_names(rows, stock_ids)

    def fetch_stock_info(self) -> list[dict[str, Any]]:
        return self._fetch_rows({"dataset": "TaiwanStockInfo"})

    def fetch_stock_news(
        self, stock_id: str, end_date: date, days: int = 7
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for query_date in news_query_dates(end_date, days):
            rows.extend(
                self._fetch_rows(
                    {
                        "dataset": "TaiwanStockNews",
                        "data_id": stock_id,
                        "start_date": query_date.isoformat(),
                    }
                )
            )
        return rows

    def _fetch_rows(self, params: dict[str, str]) -> list[dict[str, Any]]:
        response = self.session.get(API_URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != 200:
            message = payload.get("msg", "Unknown FinMind API error")
            dataset = params.get("dataset", "unknown dataset")
            stock_id = params.get("data_id", "all stocks")
            raise RuntimeError(f"FinMind API error for {stock_id}/{dataset}: {message}")
        data = payload.get("data")
        if not isinstance(data, list):
            dataset = params.get("dataset", "unknown dataset")
            raise RuntimeError(f"Unexpected FinMind response for {dataset}")
        return data


class TdccClient:
    def __init__(self, cache_dir: Path, timeout: float = 45.0) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "finmindtrade/0.1 TDCC weekly analysis",
            }
        )

    def fetch_recent_snapshots(
        self,
        stock_ids: Iterable[str],
        end_date: date,
        count: int = 5,
    ) -> dict[date, dict[str, dict[int, int]]]:
        if count < 1:
            raise ValueError("TDCC snapshot count must be at least 1")

        wanted = set(stock_ids)
        official_response = self.session.get(TDCC_OFFICIAL_URL, timeout=self.timeout)
        official_response.raise_for_status()
        official_payload = official_response.json()
        if not isinstance(official_payload, list):
            raise RuntimeError("Unexpected TDCC official API response")
        official_snapshots = parse_tdcc_rows(official_payload, wanted)
        official_snapshots = {
            snapshot_date: holdings
            for snapshot_date, holdings in official_snapshots.items()
            if snapshot_date <= end_date
        }

        archive_files = self._discover_archive_files(end_date)
        candidate_dates = sorted(archive_files)[-count:]
        snapshots: dict[date, dict[str, dict[int, int]]] = {}
        for snapshot_date in candidate_dates:
            cache_path = self.cache_dir / f"{snapshot_date.isoformat()}.csv"
            if cache_path.exists():
                content = cache_path.read_bytes()
            else:
                download_url = archive_files[snapshot_date]
                if download_url is None:
                    raise RuntimeError(f"Missing TDCC archive URL for {snapshot_date}")
                self._validate_archive_url(download_url)
                response = self.session.get(download_url, timeout=self.timeout)
                response.raise_for_status()
                content = response.content
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                temporary_path = cache_path.with_suffix(".tmp")
                temporary_path.write_bytes(content)
                temporary_path.replace(cache_path)
            rows = list(
                csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
            )
            snapshots.update(parse_tdcc_rows(rows, wanted))

        for snapshot_date, official_holdings in official_snapshots.items():
            archived_holdings = snapshots.get(snapshot_date)
            if archived_holdings is not None and archived_holdings != official_holdings:
                raise RuntimeError(
                    f"TDCC archive mismatch against official data for {snapshot_date}"
                )
            snapshots[snapshot_date] = official_holdings

        eligible_dates = sorted(
            snapshot_date for snapshot_date in snapshots if snapshot_date <= end_date
        )
        if len(eligible_dates) < count:
            raise RuntimeError(
                f"Only {len(eligible_dates)} TDCC snapshots are available; {count} are required"
            )
        selected_dates = eligible_dates[-count:]
        selected = {snapshot_date: snapshots[snapshot_date] for snapshot_date in selected_dates}
        validate_tdcc_coverage(selected, wanted)
        return selected

    def _discover_archive_files(self, end_date: date) -> dict[date, str | None]:
        files: dict[date, str | None] = {}
        if self.cache_dir.exists():
            for cache_path in self.cache_dir.glob("????-??-??.csv"):
                try:
                    snapshot_date = date.fromisoformat(cache_path.stem)
                except ValueError:
                    continue
                if snapshot_date <= end_date:
                    files[snapshot_date] = None

        discovery_error: requests.RequestException | None = None
        for year in (end_date.year - 1, end_date.year):
            url = TDCC_ARCHIVE_CONTENTS_URL.format(year=year)
            try:
                response = self.session.get(url, timeout=self.timeout)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                items = response.json()
            except requests.RequestException as error:
                discovery_error = error
                continue
            if not isinstance(items, list):
                raise RuntimeError(f"Unexpected TDCC archive listing for {year}")
            for item in items:
                if not isinstance(item, dict):
                    continue
                match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.csv", str(item.get("name", "")))
                if not match:
                    continue
                snapshot_date = date.fromisoformat(match.group(1))
                download_url = item.get("download_url")
                if snapshot_date <= end_date and isinstance(download_url, str):
                    files[snapshot_date] = download_url

        if not files and discovery_error is not None:
            raise RuntimeError("Unable to discover TDCC archive snapshots") from discovery_error
        return files

    @staticmethod
    def _validate_archive_url(url: str) -> None:
        parsed = urlparse(url)
        expected_prefix = "/wirelessr/tdcc-opendata-archive/"
        if (
            parsed.scheme != "https"
            or parsed.netloc != "raw.githubusercontent.com"
            or not parsed.path.startswith(expected_prefix)
        ):
            raise RuntimeError("Unexpected TDCC archive download URL")


def load_stock_ids(path: Path) -> list[str]:
    stock_ids: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        candidate = raw_line.strip()
        if re.fullmatch(r"\d{4,6}", candidate) and candidate not in seen:
            stock_ids.append(candidate)
            seen.add(candidate)
    if not stock_ids:
        raise ValueError(f"No stock IDs found in {path}")
    return stock_ids


def extract_stock_names(
    rows: Iterable[dict[str, Any]], stock_ids: Iterable[str]
) -> dict[str, str]:
    wanted = set(stock_ids)
    names: dict[str, str] = {}
    for row in rows:
        stock_id = str(row.get("stock_id", ""))
        stock_name = str(row.get("stock_name", "")).strip()
        if stock_id in wanted and stock_name:
            names[stock_id] = stock_name
    return {stock_id: names.get(stock_id, "名稱未提供") for stock_id in wanted}


def parse_tdcc_rows(
    rows: Iterable[dict[str, Any]], stock_ids: Iterable[str]
) -> dict[date, dict[str, dict[int, int]]]:
    wanted = set(stock_ids)
    snapshots: dict[date, dict[str, dict[int, int]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for original_row in rows:
        row = {str(key).lstrip("\ufeff"): value for key, value in original_row.items()}
        stock_id = str(row.get("證券代號", "")).strip()
        if stock_id not in wanted:
            continue
        date_text = str(row.get("資料日期", "")).strip()
        level_text = str(row.get("持股分級", "")).strip()
        shares_text = str(row.get("股數", "")).strip().replace(",", "")
        try:
            snapshot_date = datetime.strptime(date_text, "%Y%m%d").date()
            level = int(level_text)
            shares = int(shares_text)
        except ValueError as error:
            raise ValueError(f"Invalid TDCC row for {stock_id}: {row}") from error
        snapshots[snapshot_date][stock_id][level] = shares
    return {
        snapshot_date: {
            stock_id: dict(levels) for stock_id, levels in holdings.items()
        }
        for snapshot_date, holdings in snapshots.items()
    }


def validate_tdcc_coverage(
    snapshots: dict[date, dict[str, dict[int, int]]], stock_ids: Iterable[str]
) -> None:
    required_levels = {12, 13, 14, 15, 17}
    wanted = set(stock_ids)
    for snapshot_date, holdings in snapshots.items():
        missing_stocks = wanted - set(holdings)
        if missing_stocks:
            raise RuntimeError(
                f"TDCC snapshot {snapshot_date} is missing stocks: "
                + ", ".join(sorted(missing_stocks))
            )
        for stock_id in wanted:
            levels = holdings[stock_id]
            missing_levels = required_levels - set(levels)
            if missing_levels:
                raise RuntimeError(
                    f"TDCC snapshot {snapshot_date}/{stock_id} is missing levels: "
                    + ", ".join(str(level) for level in sorted(missing_levels))
                )
            if levels[17] <= 0:
                raise RuntimeError(
                    f"TDCC snapshot {snapshot_date}/{stock_id} has an invalid total"
                )


def analyze_weekly_holdings(
    stock_ids: Iterable[str],
    snapshots: dict[date, dict[str, dict[int, int]]],
    price_rows_by_stock: dict[str, list[dict[str, Any]]],
    display_weeks: int = 4,
) -> list[WeeklyHolding]:
    snapshot_dates = sorted(snapshots)
    if len(snapshot_dates) < display_weeks + 1:
        raise ValueError(
            f"At least {display_weeks + 1} TDCC snapshots are required for "
            f"{display_weeks} weekly changes"
        )

    results: list[WeeklyHolding] = []
    selected_dates = snapshot_dates[-(display_weeks + 1) :]
    for stock_id in stock_ids:
        prices = sorted(
            (
                date.fromisoformat(str(row["date"])),
                float(row["close"]),
            )
            for row in price_rows_by_stock[stock_id]
        )
        weekly_values: list[tuple[date, date, float, dict[int, int]]] = []
        for snapshot_date in selected_dates:
            eligible_prices = [item for item in prices if item[0] <= snapshot_date]
            if not eligible_prices:
                raise ValueError(
                    f"No price is available on or before {snapshot_date} for {stock_id}"
                )
            price_date, close = eligible_prices[-1]
            weekly_values.append(
                (snapshot_date, price_date, close, snapshots[snapshot_date][stock_id])
            )

        stock_results: list[WeeklyHolding] = []
        for index in range(1, len(weekly_values)):
            snapshot_date, price_date, close, levels = weekly_values[index]
            previous_close = weekly_values[index - 1][2]
            change = close - previous_close
            total_shares = levels[17]
            iso_week = snapshot_date.isocalendar()
            stock_results.append(
                WeeklyHolding(
                    stock_id=stock_id,
                    week_label=f"{iso_week.year % 100:02d}W{iso_week.week:02d}",
                    data_date=snapshot_date.isoformat(),
                    price_date=price_date.isoformat(),
                    close=close,
                    change=change,
                    change_percent=change / previous_close * 100,
                    percent_400_to_800=(levels[12] + levels[13]) / total_shares * 100,
                    percent_800_to_1000=levels[14] / total_shares * 100,
                    percent_over_1000=levels[15] / total_shares * 100,
                )
            )
        results.extend(reversed(stock_results[-display_weeks:]))
    return results


def news_query_dates(end_date: date, days: int = 7) -> list[date]:
    if days < 1:
        raise ValueError("News query days must be at least 1")
    start_date = end_date - timedelta(days=days - 1)
    return [start_date + timedelta(days=offset) for offset in range(days)]


def compare_price(price: float, average: float) -> str:
    if math.isclose(price, average, rel_tol=1e-9, abs_tol=1e-9):
        return "equal"
    return "above" if price > average else "below"


def analyze_price_rows(stock_id: str, rows: Iterable[dict[str, Any]]) -> PriceAnalysis:
    ordered_rows = sorted(rows, key=lambda row: str(row["date"]))
    required_rows = max(MA_WINDOWS)
    if len(ordered_rows) < required_rows:
        raise ValueError(
            f"{stock_id} has only {len(ordered_rows)} price rows; "
            f"at least {required_rows} are required"
        )

    closes = [float(row["close"]) for row in ordered_rows]
    volumes = [float(row["Trading_Volume"]) for row in ordered_rows]
    latest_close = closes[-1]
    moving_averages = {
        window: fmean(closes[-window:]) for window in MA_WINDOWS
    }
    relations = {
        window: compare_price(latest_close, average)
        for window, average in moving_averages.items()
    }
    recent_average_volume = fmean(volumes[-10:])
    previous_average_volume = fmean(volumes[-20:-10])

    return PriceAnalysis(
        stock_id=stock_id,
        trading_date=str(ordered_rows[-1]["date"]),
        close=latest_close,
        moving_averages=moving_averages,
        relations=relations,
        recent_average_volume=recent_average_volume,
        previous_average_volume=previous_average_volume,
        volume_increased=recent_average_volume > previous_average_volume,
        previous_close=closes[-2],
    )


def institutional_bucket(name: str) -> str:
    mapping = {
        "Foreign_Investor": "foreign",
        "Investment_Trust": "investment_trust",
        "Dealer_self": "dealer",
        "Dealer_Hedging": "dealer",
        "Foreign_Dealer_Self": "foreign_dealer",
    }
    return mapping.get(name, "other")


def analyze_institutional_rows(
    stock_id: str,
    rows: Iterable[dict[str, Any]],
    trading_days: int = 10,
) -> list[InstitutionalDay]:
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row["date"])].append(row)

    selected_dates = sorted(rows_by_date)[-trading_days:]
    if len(selected_dates) < trading_days:
        raise ValueError(
            f"{stock_id} has only {len(selected_dates)} institutional trading days; "
            f"{trading_days} are required"
        )

    results: list[InstitutionalDay] = []
    for trading_date in selected_dates:
        net_by_bucket: dict[str, float] = defaultdict(float)
        total_buy = 0.0
        total_sell = 0.0
        for row in rows_by_date[trading_date]:
            buy = float(row.get("buy", 0))
            sell = float(row.get("sell", 0))
            total_buy += buy
            total_sell += sell
            net_by_bucket[institutional_bucket(str(row.get("name", "")))] += buy - sell

        results.append(
            InstitutionalDay(
                stock_id=stock_id,
                trading_date=trading_date,
                foreign_net=net_by_bucket["foreign"],
                investment_trust_net=net_by_bucket["investment_trust"],
                dealer_net=net_by_bucket["dealer"],
                foreign_dealer_net=net_by_bucket["foreign_dealer"],
                other_net=net_by_bucket["other"],
                total_buy=total_buy,
                total_sell=total_sell,
            )
        )
    return results


def analyze_news_rows(
    stock_id: str, rows: Iterable[dict[str, Any]]
) -> list[StockNews]:
    deduplicated: dict[str, StockNews] = {}
    for row in rows:
        published_at = str(row.get("date", "")).strip()
        title = str(row.get("title", "")).strip()
        source = str(row.get("source", "")).strip()
        link = str(row.get("link", "")).strip()
        if not title:
            continue
        key = link or f"{published_at}\0{source}\0{title}"
        deduplicated[key] = StockNews(
            stock_id=stock_id,
            published_at=published_at,
            title=title,
            source=source,
            link=link,
        )
    return sorted(
        deduplicated.values(),
        key=lambda item: (item.published_at, item.title),
        reverse=True,
    )


def format_price(value: float) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def format_signed_price(value: float) -> str:
    prefix = "+" if value > 0 else ""
    return prefix + format_price(value)


def format_percent(value: float, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return prefix + f"{value:.2f}".rstrip("0").rstrip(".") + "%"


def round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_lots(shares: float) -> str:
    lots = Decimal(str(shares)) / Decimal("1000")
    return f"{round_half_up(lots):,}"


def format_volume(shares: float) -> str:
    return f"{round_half_up(Decimal(str(shares))):,}"


def safe_news_link(link: str) -> str | None:
    parsed = urlparse(link)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return link
    return None


def score_conditions(
    analysis: PriceAnalysis,
    institutional_days: Iterable[InstitutionalDay],
) -> dict[str, bool]:
    recent_days = sorted(
        (
            day
            for day in institutional_days
            if day.stock_id == analysis.stock_id and day.trading_date <= analysis.trading_date
        ),
        key=lambda day: day.trading_date,
    )[-10:]
    average_institution_net = sum(day.total_net for day in recent_days) / 10
    return {
        "ma5": analysis.relations[5] != "above",
        "ma10": analysis.relations[10] != "above",
        "ma20": analysis.relations[20] == "below",
        "ma60": analysis.relations[60] == "below",
        "volume": not analysis.volume_increased,
        "institution": (
            len(recent_days) == 10
            and average_institution_net > 0
            and average_institution_net > analysis.recent_average_volume * 0.03
        ),
    }


def calculate_score(
    analysis: PriceAnalysis,
    institutional_days: Iterable[InstitutionalDay],
) -> int:
    """Calculate the overview score using SCORE_RULES and current conditions."""
    conditions = score_conditions(analysis, institutional_days)
    return min(100, 100 + sum(value for key, _, value in SCORE_RULES if conditions[key]))


def analyze_score_history(
    stock_id: str, price_rows: list[dict[str, Any]], institution_rows: list[dict[str, Any]],
) -> list[ScoreDay]:
    ordered = sorted(price_rows, key=lambda row: str(row["date"]))
    institution_count = len({str(row["date"]) for row in institution_rows})
    institutions = analyze_institutional_rows(stock_id, institution_rows, institution_count) if institution_count else []
    result = []
    for index in range(max(0, len(ordered) - 10), len(ordered)):
        trading_date = str(ordered[index]["date"])
        if index + 1 < max(MA_WINDOWS):
            result.append(ScoreDay(stock_id, trading_date, None, None))
            continue
        analysis = analyze_price_rows(stock_id, ordered[:index + 1])
        conditions = score_conditions(analysis, institutions)
        result.append(ScoreDay(stock_id, trading_date, conditions, calculate_score(analysis, institutions)))
    return result


def render_report(
    analyses: list[PriceAnalysis],
    institutional_days: list[InstitutionalDay],
    weekly_holdings: list[WeeklyHolding],
    stock_news: list[StockNews],
    stock_names: dict[str, str],
    requested_end_date: str,
    *,
    adjustable_scores: bool = False,
    score_histories: dict[str, list[ScoreDay]] | None = None,
    data_warnings: list[str] | None = None,
) -> str:
    score_histories = score_histories or {}
    warning_content = ('<section class="note" role="status"><h2>資料更新提醒</h2><ul>'
                       + ''.join(f'<li>{html.escape(message)}</li>' for message in data_warnings)
                       + '</ul></section>') if data_warnings else ''
    relation_symbols = {"above": ">", "below": "<", "equal": "="}
    generated_at = datetime.now(TAIPEI_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
    institutions_by_stock: dict[str, list[InstitutionalDay]] = defaultdict(list)
    for day in institutional_days:
        institutions_by_stock[day.stock_id].append(day)
    weekly_by_stock: dict[str, list[WeeklyHolding]] = defaultdict(list)
    for weekly_holding in weekly_holdings:
        weekly_by_stock[weekly_holding.stock_id].append(weekly_holding)
    news_by_stock: dict[str, list[StockNews]] = defaultdict(list)
    for item in stock_news:
        news_by_stock[item.stock_id].append(item)
    news_end_date = date.fromisoformat(requested_end_date)
    news_start_date = news_end_date - timedelta(days=6)

    scores = {
        analysis.stock_id: (score_histories[analysis.stock_id][-1].score
        if score_histories.get(analysis.stock_id) else calculate_score(
            analysis, institutions_by_stock.get(analysis.stock_id, [])
        ))
        for analysis in analyses
    }
    sorted_analyses = sorted(
        analyses,
        key=lambda analysis: (-scores[analysis.stock_id], analysis.stock_id),
    )

    conditions_by_stock = {
        analysis.stock_id: (
            score_histories[analysis.stock_id][-1].conditions
            if score_histories.get(analysis.stock_id)
            else score_conditions(analysis, institutions_by_stock[analysis.stock_id])
        )
        for analysis in analyses
    }
    qualifying_stocks = [
        html.escape(f'{analysis.stock_id} {stock_names.get(analysis.stock_id, "名稱未提供")}')
        for analysis in sorted_analyses
        if (conditions_by_stock[analysis.stock_id] or {}).get("institution", False)
    ]
    institution_summary = "、".join(qualifying_stocks) or "無符合個股"

    price_rows: list[str] = []
    for analysis in sorted_analyses:
        stock_name = stock_names.get(analysis.stock_id, "名稱未提供")
        stock_label = f"{analysis.stock_id} {stock_name}"
        details_id = f"institution-{analysis.stock_id}"
        score = scores[analysis.stock_id]
        close_class = (
            "positive" if analysis.close > analysis.previous_close else
            "negative" if analysis.close < analysis.previous_close else "neutral"
        ) if analysis.previous_close is not None else "neutral"
        score_class = (
            "score-high" if score >= 80 else "score-low" if score < 70 else "score-mid"
        )
        cells = [
            f'<td class="score-cell {score_class}" data-sort-value="{score}">{score}</td>',
            (
                f'<td class="stock-cell" data-sort-value="{html.escape(stock_label, quote=True)}">'
                '<span class="toggle-indicator" aria-hidden="true">▶</span>'
                f'<span class="stock-code">{html.escape(analysis.stock_id)}</span> '
                f'<span class="stock-name">{html.escape(stock_name)}</span>'
                "</td>"
            ),
            (
                f'<td data-sort-value="{html.escape(analysis.trading_date, quote=True)}">'
                f"{html.escape(analysis.trading_date)}</td>"
            ),
            f'<td class="{close_class}" data-sort-value="{analysis.close}">{format_price(analysis.close)}</td>',
        ]
        for window in MA_WINDOWS:
            relation = analysis.relations[window]
            moving_average = format_price(analysis.moving_averages[window])
            relation_symbol = html.escape(relation_symbols[relation])
            cells.append(
                f'<td class="relation-{relation}" '
                f'data-sort-value="{analysis.moving_averages[window]}">'
                f"{moving_average}({relation_symbol})</td>"
            )
        cells.extend(
            [
                (
                    f'<td data-sort-value="{analysis.recent_average_volume}">'
                    f"{format_lots(analysis.recent_average_volume)}</td>"
                ),
                (
                    f'<td data-sort-value="{analysis.previous_average_volume}">'
                    f"{format_lots(analysis.previous_average_volume)}</td>"
                ),
                (
                    '<td class="volume-up" data-sort-value="1">是</td>'
                    if analysis.volume_increased
                    else '<td class="volume-down" data-sort-value="0">否</td>'
                ),
            ]
        )
        conditions_attribute = ""
        if adjustable_scores:
            conditions_attribute = 'data-score-conditions="' + html.escape(
                json.dumps(conditions_by_stock[analysis.stock_id]),
                quote=True,
            ) + '" '
        summary_row = (
            f'<tr class="stock-row" role="button" tabindex="0" aria-expanded="false" '
            f'{conditions_attribute}'
            f'aria-controls="{details_id}" data-details-id="{details_id}" '
            f'title="展開 {html.escape(stock_label, quote=True)} 詳細資訊">'
            + "".join(cells)
            + "</tr>"
        )

        detail_rows: list[str] = []
        displayed_days = sorted(
            (day for day in institutions_by_stock.get(analysis.stock_id, [])
             if day.trading_date <= analysis.trading_date),
            key=lambda day: day.trading_date,
        )[-5:]
        for day in displayed_days:
            net_values = [
                day.foreign_net,
                day.investment_trust_net,
                day.dealer_net,
                day.foreign_dealer_net,
                day.other_net,
            ]
            detail_cells = [f"<td>{html.escape(day.trading_date)}</td>"]
            for value in net_values:
                value_class = (
                    "positive" if value > 0 else "negative" if value < 0 else "neutral"
                )
                detail_cells.append(
                    f'<td class="{value_class}">{format_lots(value)}</td>'
                )
            total_class = (
                "positive"
                if day.total_net > 0
                else "negative"
                if day.total_net < 0
                else "neutral"
            )
            detail_cells.extend(
                [
                    f"<td>{format_lots(day.total_buy)}</td>",
                    f"<td>{format_lots(day.total_sell)}</td>",
                    f'<td class="{total_class}">{format_lots(day.total_net)}</td>',
                ]
            )
            detail_rows.append("<tr>" + "".join(detail_cells) + "</tr>")

        news_rows: list[str] = []
        for item in news_by_stock.get(analysis.stock_id, []):
            safe_link = safe_news_link(item.link)
            escaped_title = html.escape(item.title)
            if safe_link:
                title_markup = (
                    f'<a href="{html.escape(safe_link, quote=True)}" '
                    f'target="_blank" rel="noopener noreferrer">{escaped_title}</a>'
                )
            else:
                title_markup = f"<span>{escaped_title}</span>"
            news_rows.append(
                "<tr>"
                f"<td>{html.escape(item.published_at)}</td>"
                f"<td>{html.escape(item.source or '未提供')}</td>"
                f'<td class="news-title">{title_markup}</td>'
                "</tr>"
            )
        if news_rows:
            news_content = f"""<div class="nested-table-wrap">
        <table class="news-table">
          <thead><tr><th>時間</th><th>來源</th><th>新聞標題</th></tr></thead>
          <tbody>{''.join(news_rows)}</tbody>
        </table>
      </div>"""
        else:
            news_content = '<p class="no-news">此期間沒有取得相關新聞。</p>'

        holding_rows: list[str] = []
        for weekly_holding in weekly_by_stock.get(analysis.stock_id, []):
            movement_class = (
                "positive"
                if weekly_holding.change > 0
                else "negative"
                if weekly_holding.change < 0
                else "neutral"
            )
            price_date_note = (
                ""
                if weekly_holding.price_date == weekly_holding.data_date
                else f' title="股價資料日：{html.escape(weekly_holding.price_date, quote=True)}"'
            )
            holding_rows.append(
                "<tr>"
                f"<td>{html.escape(weekly_holding.week_label)}</td>"
                f"<td>{html.escape(weekly_holding.data_date)}</td>"
                f"<td{price_date_note}>{format_price(weekly_holding.close)}</td>"
                f'<td class="{movement_class}">{format_signed_price(weekly_holding.change)}</td>'
                f'<td class="{movement_class}">{format_percent(weekly_holding.change_percent, signed=True)}</td>'
                f"<td>{format_percent(weekly_holding.percent_400_to_800)}</td>"
                f"<td>{format_percent(weekly_holding.percent_800_to_1000)}</td>"
                f"<td>{format_percent(weekly_holding.percent_over_1000)}</td>"
                "</tr>"
            )
        if holding_rows:
            holding_content = f"""<div class="nested-table-wrap">
        <table class="holding-table">
          <thead><tr><th>週別</th><th>統計日期</th><th>收盤</th><th>漲跌（元）</th><th>漲跌（%）</th><th>＞400張～≤800張</th><th>＞800張～≤1千張</th><th>＞1千張</th></tr></thead>
          <tbody>{''.join(holding_rows)}</tbody>
        </table>
      </div>"""
        else:
            holding_content = '<p class="no-holding">此期間沒有取得持股分級資料。</p>'

        detail_row = f"""<tr class="institution-details" id="{details_id}" hidden>
  <td colspan="11">
    <section class="institution-panel" aria-label="{html.escape(stock_label, quote=True)} 最近五個交易日法人買賣">
      {render_history(score_histories.get(analysis.stock_id, []), stock_label)}
      <h3>{html.escape(stock_label)}：最近五個交易日法人買賣</h3>
      <div class="nested-table-wrap">
        <table class="institution-table">
          <thead><tr><th>日期</th><th>外資淨額（張）</th><th>投信淨額（張）</th><th>自營商淨額（張）</th><th>外資自營商淨額（張）</th><th>其他淨額（張）</th><th>法人買進（張）</th><th>法人賣出（張）</th><th>法人合計淨額（張）</th></tr></thead>
          <tbody>{''.join(detail_rows)}</tbody>
        </table>
      </div>
      <h3 class="holding-heading">最近四週股價與持股分級</h3>
      {holding_content}
      <h3 class="news-heading">近七日個股新聞（{news_start_date.isoformat()} 至 {news_end_date.isoformat()}）</h3>
      {news_content}
    </section>
  </td>
</tr>"""
        price_rows.extend([summary_row, detail_row])

    score_description = (
        "總分以 100 分為上限：收盤未高於 5 日、10 日均線分別扣 5、10 分；"
        "低於月線、季線分別扣 20、30 分；最近均量未增加扣 10 分；"
        "法人近十個交易日平均淨買超為正值，且大於近十日平均成交量的 3% 才加 10 分。"
    )
    score_controls = ""
    score_script = ""
    if adjustable_scores:
        score_description = "總分從 100 分起算，上限為 100 分；依下方評分參數即時計算。"
        inputs = "".join(
            f'<label for="score-{key}">{label}'
            f'<input id="score-{key}" name="{key}" type="number" '
            f'min="-100" max="100" step="1" value="{value}" required></label>'
            for key, label, value in SCORE_RULES
        )
        score_controls = f"""
  <section class="note" aria-labelledby="score-heading">
    <h2 id="score-heading">評分參數</h2>
    <p>正數加分、負數扣分，0 表示不計分；每項可輸入 -100 至 100 的整數。
    修改後即時重算並依目前欄位排序。設定僅適用本頁，重新開啟或重新整理將恢復預設。</p>
    <form id="score-form">
      <div class="score-inputs">{inputs}</div>
      <button type="reset">恢復預設</button>
      <p id="score-status" role="status" aria-live="polite"></p>
    </form>
    <noscript>請啟用 JavaScript 才能調整評分；目前顯示預設分數。</noscript>
  </section>"""
        score_script = """
  const scoreForm = document.getElementById("score-form");
  const scoreInputs = Array.from(scoreForm.querySelectorAll("input"));
  const scoreStatus = document.getElementById("score-status");
  function updateScores() {
    if (!scoreForm.checkValidity()) {
      scoreStatus.textContent = "請將所有加減分填為 -100 至 100 的整數；目前保留上次有效結果。";
      return;
    }
    const values = Object.fromEntries(scoreInputs.map(input => [input.name, input.valueAsNumber]));
    overviewBody.querySelectorAll(".stock-row").forEach(row => {
      const conditions = JSON.parse(row.dataset.scoreConditions);
      const score = Math.min(100, 100 + Object.entries(values).reduce(
        (total, [key, value]) => total + (conditions[key] ? value : 0), 0));
      const cell = row.cells[0];
      cell.textContent = String(score);
      cell.dataset.sortValue = String(score);
      cell.classList.remove("score-high", "score-mid", "score-low");
      cell.classList.add(score >= 80 ? "score-high" : score < 70 ? "score-low" : "score-mid");
      updateHistory(document.getElementById(row.dataset.detailsId), values);
    });
    const activeHeader = Array.from(sortableHeaders).find(header => header.hasAttribute("aria-sort"));
    sortRows(activeHeader, activeHeader.getAttribute("aria-sort"));
    scoreStatus.textContent = "已依目前參數更新分數、10 日折線圖與排序。";
  }
  scoreForm.addEventListener("submit", event => event.preventDefault());
  scoreForm.addEventListener("input", updateScores);
  scoreForm.addEventListener("reset", () => {
    scoreInputs.forEach(input => { input.value = input.defaultValue; });
    updateScores();
  });
"""

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>台股法人、均線與成交量分析</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f4f6f8; color: #17202a; }}
    main {{ max-width: 1600px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 16px; }}
    h2 {{ margin-top: 32px; }}
    .meta, .note {{ background: #fff; border: 1px solid #dce1e6; border-radius: 8px; padding: 12px 18px; }}
    .meta {{ line-height: 1.7; }}
    .table-wrap {{ overflow-x: auto; background: #fff; border: 1px solid #dce1e6; border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; white-space: nowrap; font-variant-numeric: tabular-nums; }}
    th, td {{ padding: 9px 11px; border-bottom: 1px solid #e5e9ed; text-align: right; }}
    th {{ position: sticky; top: 0; background: #223a5e; color: #fff; }}
    th:first-child, td:first-child {{ text-align: left; font-weight: 600; }}
    .overview-table .sortable {{ padding: 0; }}
    .sort-button {{ display: flex; width: 100%; align-items: center; justify-content: flex-end; gap: 5px; padding: 9px 11px; border: 0; background: transparent; color: inherit; font: inherit; font-weight: inherit; white-space: nowrap; cursor: pointer; }}
    .sort-button:hover, .sort-button:focus-visible {{ background: #34527d; outline: 2px solid #fff; outline-offset: -3px; }}
    .sort-indicator {{ width: 0.8em; color: #cbd5e1; }}
    .sortable[aria-sort="ascending"] .sort-indicator::before {{ content: "▲"; color: #fff; }}
    .sortable[aria-sort="descending"] .sort-indicator::before {{ content: "▼"; color: #fff; }}
    .sortable:not([aria-sort]) .sort-indicator::before {{ content: "↕"; }}
    .stock-header .sort-button {{ justify-content: flex-start; }}
    .stock-row {{ cursor: pointer; }}
    .stock-row:nth-of-type(4n + 3) {{ background: #f8fafc; }}
    .stock-row:hover, .stock-row:focus-visible {{ background: #e9f2fc; outline: 2px solid #2e6da4; outline-offset: -2px; }}
    .stock-cell {{ min-width: 150px; }}
    .score-cell {{ text-align: right !important; font-size: 1.05rem; font-weight: 800 !important; }}
    .score-high {{ color: #b42318; }}
    .score-low {{ color: #067647; }}
    .score-mid {{ color: #17202a; }}
    .score-inputs {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; margin-bottom: 16px; }}
    .score-inputs label {{ display: flex; flex-direction: column; gap: 6px; }}
    .score-inputs input {{ padding: 8px; font: inherit; border: 1px solid #98a2b3; border-radius: 4px; }}
    .score-inputs input:invalid {{ border-color: #b42318; }}
    #score-form button {{ padding: 8px 16px; font: inherit; cursor: pointer; }}
    .stock-code {{ font-weight: 750; }}
    .stock-name {{ color: #475467; font-weight: 500; }}
    .toggle-indicator {{ display: inline-block; width: 1.2em; transition: transform 0.15s ease; }}
    .stock-row[aria-expanded="true"] .toggle-indicator {{ transform: rotate(90deg); }}
    .institution-details > td {{ padding: 14px 18px 18px; background: #eef4fb; text-align: left; }}
    .institution-panel h3 {{ margin: 0 0 10px; font-size: 1rem; }}
    .nested-table-wrap {{ overflow-x: auto; border: 1px solid #cbd8e6; border-radius: 6px; }}
    .institution-table th {{ position: static; background: #405b78; }}
    .institution-table td {{ background: #fff; font-weight: 400; }}
    .institution-table tbody tr:nth-child(even) td {{ background: #f8fafc; }}
    .holding-heading {{ margin-top: 20px !important; }}
    .holding-table th {{ position: static; background: #405b78; }}
    .holding-table td {{ background: #fff; font-weight: 400; }}
    .holding-table tbody tr:nth-child(even) td {{ background: #f8fafc; }}
    .news-heading {{ margin-top: 20px !important; }}
    .news-table {{ white-space: normal; }}
    .news-table th {{ position: static; background: #405b78; }}
    .news-table th:nth-child(1) {{ min-width: 145px; }}
    .news-table th:nth-child(2) {{ min-width: 110px; }}
    .news-table td {{ background: #fff; font-weight: 400; vertical-align: top; }}
    .news-table tbody tr:nth-child(even) td {{ background: #f8fafc; }}
    .news-title {{ min-width: 360px; text-align: left; }}
    .news-title a {{ color: #175cd3; text-decoration: none; }}
    .news-title a:hover {{ text-decoration: underline; }}
    .no-holding, .no-news {{ margin: 0; padding: 12px; background: #fff; border-radius: 6px; }}
    .positive, .relation-above, .volume-up {{ color: #b42318; font-weight: 700; }}
    .negative, .relation-below, .volume-down {{ color: #067647; font-weight: 700; }}
    .neutral, .relation-equal {{ color: #475467; font-weight: 700; }}
    code {{ background: #edf1f5; padding: 2px 5px; border-radius: 4px; }}
    @media (max-width: 720px) {{ main {{ padding: 14px; }} h1 {{ font-size: 1.55rem; }} }}
    {HISTORY_CSS}
  </style>
</head>
<body>
<main>
  <h1>台股法人、均線與成交量分析</h1>
  <ul class="meta">
    <li>查詢截止日：{html.escape(requested_end_date)}</li>
    <li>報告產生時間：{html.escape(generated_at)}</li>
    <li>月線採 20 個交易日，季線採 60 個交易日；均線均包含資料最新日，括號內符號表示收盤價相對於均線的關係。</li>
    <li>法人數值為買進減賣出的淨額，單位為張；正數代表淨買超，負數代表淨賣超。</li>
    <li>量能比較使用兩段不重疊區間：最近 10 個交易日與再前 10 個交易日。</li>
    <li>{score_description}</li>
    <li>點選個股資料列，可展開或收合最近三個交易日的法人買賣資訊。</li>
    <li>展開區列出最近 4 個已完成的 TDCC 週次；週漲跌以相鄰兩個集保資料日收盤價計算。</li>
    <li>展開區同時列出查詢截止日往前 7 個日曆日的相關新聞；新聞標題會開啟原始來源。</li>
  </ul>

  {warning_content}
  {score_controls}
  <h2>均線與成交量總覽</h2>
  <p class="note" id="institution-qualified"><strong>符合法人加分條件個股：</strong>{institution_summary}<br>條件：法人近十個交易日平均淨買超為正值，且大於近十日平均成交量的 3%（預設加 10 分）。</p>
  <p>收盤價相較前一個交易日：上漲為紅色，下跌為綠色，平盤為灰色。</p>
  <div class="table-wrap">
    <table class="overview-table" id="overview-table">
      <thead><tr>
        <th class="sortable" aria-sort="descending"><button type="button" class="sort-button" data-column="0" data-type="number">總分<span class="sort-indicator" aria-hidden="true"></span></button></th>
        <th class="sortable stock-header"><button type="button" class="sort-button" data-column="1" data-type="text">股票代碼／名稱<span class="sort-indicator" aria-hidden="true"></span></button></th>
        <th class="sortable"><button type="button" class="sort-button" data-column="2" data-type="text">資料日<span class="sort-indicator" aria-hidden="true"></span></button></th>
        <th class="sortable"><button type="button" class="sort-button" data-column="3" data-type="number">收盤價<span class="sort-indicator" aria-hidden="true"></span></button></th>
        <th class="sortable"><button type="button" class="sort-button" data-column="4" data-type="number">5 日均線<span class="sort-indicator" aria-hidden="true"></span></button></th>
        <th class="sortable"><button type="button" class="sort-button" data-column="5" data-type="number">10 日均線<span class="sort-indicator" aria-hidden="true"></span></button></th>
        <th class="sortable"><button type="button" class="sort-button" data-column="6" data-type="number">月線<span class="sort-indicator" aria-hidden="true"></span></button></th>
        <th class="sortable"><button type="button" class="sort-button" data-column="7" data-type="number">季線<span class="sort-indicator" aria-hidden="true"></span></button></th>
        <th class="sortable"><button type="button" class="sort-button" data-column="8" data-type="number">最近 10 日均量（張）<span class="sort-indicator" aria-hidden="true"></span></button></th>
        <th class="sortable"><button type="button" class="sort-button" data-column="9" data-type="number">前 10 日均量（張）<span class="sort-indicator" aria-hidden="true"></span></button></th>
        <th class="sortable"><button type="button" class="sort-button" data-column="10" data-type="number">最近均量較大<span class="sort-indicator" aria-hidden="true"></span></button></th>
      </tr></thead>
      <tbody id="overview-body">{''.join(price_rows)}</tbody>
    </table>
  </div>

  <h2>資料來源與限制</h2>
  <p class="note">股價、法人與新聞資料取自 FinMind；持股分級取自 TDCC 官方 OpenAPI，歷史週次使用 <a href="https://github.com/wirelessr/tdcc-opendata-archive" target="_blank" rel="noopener noreferrer">TDCC 開放資料歷史快照</a>，且最新週會與官方資料交叉核對。新聞僅包含時間、來源、標題與原始連結，不包含新聞全文。報告反映資料源在產生當下回傳的內容；盤中或資料尚未定案時，最新數值之後可能調整。</p>
</main>
<script>
  document.querySelectorAll(".stock-row").forEach((row) => {{
    const toggleDetails = () => {{
      const details = document.getElementById(row.dataset.detailsId);
      const willExpand = row.getAttribute("aria-expanded") !== "true";
      row.setAttribute("aria-expanded", String(willExpand));
      details.hidden = !willExpand;
    }};
    row.addEventListener("click", toggleDetails);
    row.addEventListener("keydown", (event) => {{
      if (event.key === "Enter" || event.key === " ") {{
        event.preventDefault();
        toggleDetails();
      }}
    }});
  }});

  const overviewBody = document.getElementById("overview-body");
  const sortableHeaders = document.querySelectorAll(".overview-table th.sortable");
  function sortRows(header, direction) {{
      const button = header.querySelector(".sort-button");
      const column = Number(button.dataset.column);
      const type = button.dataset.type;
      const rows = Array.from(overviewBody.querySelectorAll(".stock-row"));

      rows.sort((leftRow, rightRow) => {{
        const left = leftRow.cells[column].dataset.sortValue ?? leftRow.cells[column].textContent.trim();
        const right = rightRow.cells[column].dataset.sortValue ?? rightRow.cells[column].textContent.trim();
        let comparison;
        if (type === "number") {{
          comparison = Number(left) - Number(right);
        }} else {{
          comparison = left.localeCompare(right, "zh-Hant", {{ numeric: true }});
        }}
        if (comparison === 0) {{
          comparison = leftRow.cells[1].dataset.sortValue.localeCompare(
            rightRow.cells[1].dataset.sortValue,
            "zh-Hant",
            {{ numeric: true }},
          );
        }}
        return direction === "ascending" ? comparison : -comparison;
      }});

      sortableHeaders.forEach((otherHeader) => otherHeader.removeAttribute("aria-sort"));
      header.setAttribute("aria-sort", direction);
      rows.forEach((row) => {{
        const details = document.getElementById(row.dataset.detailsId);
        overviewBody.append(row, details);
      }});
  }}
  sortableHeaders.forEach((header) => {{
    header.querySelector(".sort-button").addEventListener("click", () => {{
      const direction = header.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
      sortRows(header, direction);
    }});
  }});
  {HISTORY_SCRIPT if adjustable_scores else ''}
  {score_script}
</script>
</body>
</html>
"""


def run_analysis(
    stock_ids: list[str],
    end_date: date,
    client: FinMindClient,
    tdcc_client: TdccClient,
) -> tuple[
    list[PriceAnalysis],
    list[InstitutionalDay],
    list[WeeklyHolding],
    list[StockNews],
    dict[str, str],
    dict[str, list[ScoreDay]],
]:
    start_date = end_date - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    start_date_text = start_date.isoformat()
    end_date_text = end_date.isoformat()
    price_analyses: list[PriceAnalysis] = []
    institutional_days: list[InstitutionalDay] = []
    stock_news: list[StockNews] = []
    price_rows_by_stock: dict[str, list[dict[str, Any]]] = {}
    score_histories: dict[str, list[ScoreDay]] = {}
    print("Fetching stock names...", file=sys.stderr)
    stock_names = client.fetch_stock_names(stock_ids)

    for index, stock_id in enumerate(stock_ids, start=1):
        print(f"[{index}/{len(stock_ids)}] Fetching {stock_id}...", file=sys.stderr)
        price_rows = client.fetch(
            "TaiwanStockPrice", stock_id, start_date_text, end_date_text
        )
        institution_rows = client.fetch(
            "TaiwanStockInstitutionalInvestorsBuySell",
            stock_id,
            start_date_text,
            end_date_text,
        )
        news_rows = client.fetch_stock_news(stock_id, end_date, days=7)
        price_rows = [row for row in price_rows if str(row["date"]) <= end_date_text]
        institution_rows = [row for row in institution_rows if str(row["date"]) <= end_date_text]
        price_rows_by_stock[stock_id] = price_rows
        price_analyses.append(analyze_price_rows(stock_id, price_rows))
        score_histories[stock_id] = analyze_score_history(stock_id, price_rows, institution_rows)
        institutional_days.extend(
            analyze_institutional_rows(stock_id, institution_rows,
                                       min(10, len({str(row["date"]) for row in institution_rows})))
            if institution_rows else []
        )
        stock_news.extend(analyze_news_rows(stock_id, news_rows))

    print("Fetching TDCC weekly holdings...", file=sys.stderr)
    snapshots = tdcc_client.fetch_recent_snapshots(stock_ids, end_date, count=5)
    weekly_holdings = analyze_weekly_holdings(
        stock_ids, snapshots, price_rows_by_stock, display_weeks=4
    )

    return price_analyses, institutional_days, weekly_holdings, stock_news, stock_names, score_histories


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze target Taiwan stocks with FinMind data."
    )
    parser.add_argument("--refresh-cache", action="store_true", help="Refresh all FinMind data required by this report.")
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("target.txt"),
        help="Path to the target stock list.",
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=datetime.now(TAIPEI_TIMEZONE).date(),
        help="Inclusive query end date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("analysis.html"),
        help="HTML report output path; analysis_param.html is also generated in the same directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    client = None
    try:
        param_output = args.output.with_name("analysis_param.html")
        if args.output.resolve() == param_output.resolve():
            raise ValueError("--output must differ from analysis_param.html")
        stock_ids = load_stock_ids(args.target)
        client = CachedFinMindClient(FinMindClient(API_TOKEN), Path(__file__).resolve().parent / "data" / "finmind.sqlite3",
                                     refresh=getattr(args, "refresh_cache", False))
        analyses, institutional_days, weekly_holdings, stock_news, stock_names, score_histories = run_analysis(
            stock_ids,
            args.end_date,
            client,
            TdccClient(Path("tdcc_cache")),
        )
        report = render_report(
            analyses,
            institutional_days,
            weekly_holdings,
            stock_news,
            stock_names,
            args.end_date.isoformat(),
            score_histories=score_histories,
            data_warnings=client.warnings,
        )
        args.output.write_text(report, encoding="utf-8")
        param_report = render_report(
            analyses,
            institutional_days,
            weekly_holdings,
            stock_news,
            stock_names,
            args.end_date.isoformat(),
            adjustable_scores=True,
            score_histories=score_histories,
            data_warnings=client.warnings,
        )
        param_output.write_text(param_report, encoding="utf-8")
    except (OSError, ValueError, RuntimeError, requests.RequestException) as error:
        print(f"Analysis failed: {error}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()

    print(f"Report written to {args.output}")
    print(f"Adjustable report written to {param_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
