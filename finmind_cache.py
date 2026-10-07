"""SQLite read-through cache for FinMind's raw responses."""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests


TAIPEI = timezone(timedelta(hours=8))
REMOTE_ERRORS = (OSError, ValueError, RuntimeError, requests.RequestException)


def dates_between(start: date, end: date):
    while start <= end:
        yield start
        start += timedelta(days=1)


class CachedFinMindClient:
    def __init__(self, remote, path: Path, *, refresh=False, now=None):
        self.remote = remote
        self.refresh = refresh
        self.now = now or (lambda: datetime.now(TAIPEI))
        self.warnings: list[str] = []
        self.connection = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, timeout=10)
            self.connection.execute("""CREATE TABLE IF NOT EXISTS daily_cache (
                dataset TEXT NOT NULL, stock_id TEXT NOT NULL, day TEXT NOT NULL,
                payload TEXT NOT NULL, fetched_at TEXT NOT NULL,
                PRIMARY KEY (dataset, stock_id, day)
            )""")
            self.connection.commit()
        except (OSError, sqlite3.Error) as error:
            self._disable(error)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _warn(self, message):
        self.warnings.append(message)
        print(f"Warning: {message}", file=sys.stderr)

    def _disable(self, error):
        self.close()
        self._warn(f"SQLite 無法讀寫，改用直接下載（{type(error).__name__}）。")

    @staticmethod
    def _validate(dataset, stock_id, rows, start=None, end=None):
        if not isinstance(rows, list):
            raise ValueError("Expected a list of data rows")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid data row")
            if dataset == "TaiwanStockInfo":
                if not row.get("stock_id") or not row.get("stock_name"):
                    raise ValueError("Invalid stock information")
                continue
            day = date.fromisoformat(str(row.get("date", ""))[:10])
            if start is not None and not start <= day <= end:
                raise ValueError("Data date outside requested range")
            if "stock_id" in row and str(row["stock_id"]) != stock_id:
                raise ValueError("Unexpected stock identifier")
            fields = {"TaiwanStockPrice": ("close", "Trading_Volume"),
                      "TaiwanStockInstitutionalInvestorsBuySell": ("buy", "sell")}.get(dataset, ())
            for field in fields:
                try:
                    valid = field in row and math.isfinite(float(row[field]))
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    raise ValueError(f"Invalid {field}")
            if dataset == "TaiwanStockInstitutionalInvestorsBuySell" and not row.get("name"):
                raise ValueError("Missing institution name")
            if dataset == "TaiwanStockNews" and not all(row.get(key) for key in ("title", "link")):
                raise ValueError("Invalid news row")

    def _read(self, dataset, stock_id, start, end):
        if self.connection is None:
            return {}
        try:
            records = self.connection.execute(
                "SELECT day, payload, fetched_at FROM daily_cache "
                "WHERE dataset=? AND stock_id=? AND day BETWEEN ? AND ?",
                (dataset, stock_id, start, end),
            ).fetchall()
            result = {}
            for day, payload, fetched in records:
                try:
                    rows = json.loads(payload)
                    stamp = datetime.fromisoformat(fetched)
                    if stamp.tzinfo is None:
                        raise ValueError("Missing timestamp timezone")
                    boundary = date.fromisoformat(day)
                    self._validate(dataset, stock_id, rows, boundary, boundary)
                    result[day] = (rows, stamp)
                except (ValueError, TypeError, KeyError):
                    print(f"Cache invalid: {dataset}/{stock_id}/{day}; fetching again", file=sys.stderr)
            return result
        except sqlite3.Error as error:
            self._disable(error)
            return {}

    def _write(self, dataset, stock_id, grouped, timestamp):
        if self.connection is None:
            return
        try:
            with self.connection:
                self.connection.executemany(
                    "INSERT OR REPLACE INTO daily_cache VALUES (?, ?, ?, ?, ?)",
                    [(dataset, stock_id, day, json.dumps(rows, ensure_ascii=False), timestamp.isoformat())
                     for day, rows in grouped.items()],
                )
        except sqlite3.Error as error:
            self._disable(error)

    def _query_days(self, dataset, stock_id, start, end, *, news=False):
        days = list(dates_between(start, end))
        cached = self._read(dataset, stock_id, start.isoformat(), end.isoformat())
        now = self.now()
        needed = []
        for day in days:
            item = cached.get(day.isoformat())
            stale = (day == now.date() or item is None or now - item[1] >= timedelta(hours=24)) if news else day >= end - timedelta(days=6)
            if self.refresh or item is None or stale:
                needed.append(day)
        print(f"Cache {dataset}/{stock_id}: {len(days) - len(needed)} hit days, {len(needed)} fetch days", file=sys.stderr)
        ranges = []
        for day in needed:
            if not news and ranges and day == ranges[-1][1] + timedelta(days=1):
                ranges[-1] = (ranges[-1][0], day)
            else:
                ranges.append((day, day))
        for first, last in ranges:
            print(f"Fetching {dataset}/{stock_id}: {first}..{last}", file=sys.stderr)
            try:
                rows = (self.remote.fetch_stock_news(stock_id, first, days=1) if news else
                        self.remote.fetch(dataset, stock_id, first.isoformat(), last.isoformat()))
                self._validate(dataset, stock_id, rows, first, last)
            except REMOTE_ERRORS:
                requested = [day.isoformat() for day in dates_between(first, last)]
                if all(day in cached for day in requested):
                    oldest = min(cached[day][1] for day in requested)
                    self._warn(f"{stock_id} {dataset} {first}～{last} 刷新失敗，使用快取；最後成功更新（最早）：{oldest.isoformat()}。")
                    continue
                if news:
                    self._warn(f"{stock_id} {first} 新聞資料取得失敗，該日資料不完整。")
                    continue
                raise RuntimeError(f"{stock_id} {dataset} {first}..{last}: download failed and cache is incomplete") from None
            grouped = {day.isoformat(): [] for day in dates_between(first, last)}
            for row in rows:
                grouped[str(row["date"])[:10]].append(row)
            self._write(dataset, stock_id, grouped, now)
            cached.update({day: (values, now) for day, values in grouped.items()})
        return [row for day in days for row in cached.get(day.isoformat(), ([], now))[0]]

    def fetch(self, dataset, stock_id, start_date, end_date):
        return self._query_days(dataset, stock_id, date.fromisoformat(start_date), date.fromisoformat(end_date))

    def fetch_stock_news(self, stock_id, end_date, days=3):
        return self._query_days("TaiwanStockNews", stock_id, end_date - timedelta(days=days - 1), end_date, news=True)

    def fetch_stock_names(self, stock_ids):
        stock_ids = list(stock_ids)
        dataset = "TaiwanStockInfo"
        key = "1970-01-01"
        item = self._read(dataset, "", key, key).get(key)
        rows, stamp = item or ([], None)
        names = {str(row["stock_id"]): str(row["stock_name"]) for row in rows}
        now = self.now()
        if self.refresh or stamp is None or now - stamp >= timedelta(days=7) or any(stock not in names for stock in stock_ids):
            print("Fetching TaiwanStockInfo", file=sys.stderr)
            try:
                rows = self.remote.fetch_stock_info()
                self._validate(dataset, "", rows)
                if not rows:
                    raise ValueError("Empty stock information")
                self._write(dataset, "", {key: rows}, now)
                names = {str(row["stock_id"]): str(row["stock_name"]) for row in rows}
            except REMOTE_ERRORS:
                self._warn(f"股票名稱更新失敗，使用既有名稱或股票代碼；最後成功更新：{stamp.isoformat() if stamp else '無'}。")
        else:
            print("Cache TaiwanStockInfo: hit", file=sys.stderr)
        return {stock: names.get(stock, stock) for stock in stock_ids}
