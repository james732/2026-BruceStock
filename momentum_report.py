"""Generate a separate Bruce momentum page without sending email."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import html
import json
import re
from pathlib import Path
import sys

import requests
from main import FinMindClient, TdccClient, TAIPEI_TIMEZONE, API_TOKEN, load_stock_ids, analyze_news_rows, safe_news_link, analyze_price_rows, stock_history_issue, render_report, ScoreDay
from main import analyze_institutional_rows, analyze_weekly_holdings
from score_history import render_history
from finmind_cache import CachedFinMindClient
from momentum_scoring import Config, RULES, build_features, evaluate, number, normalize_shares

REMOTE_ERRORS = (OSError, ValueError, RuntimeError, requests.RequestException)


def adapt_daily(symbol, prices, institutions):
    """Require each three-institution category; never invent a missing zero."""
    grouped = {}
    for r in institutions:
        if str(r.get('stock_id', symbol)) != symbol:
            continue
        grouped.setdefault(str(r.get('date')), []).append(r)
    result = []
    for p in prices:
        if str(p.get('stock_id', symbol)) != symbol:
            continue
        row = dict(date=date.fromisoformat(str(p['date'])), close=p.get('close'),
                   volume_shares=p.get('Trading_Volume'), unit='shares')
        rows = grouped.get(str(p['date']), [])
        categories = {}
        for r in rows:
            categories.setdefault(r.get('name'), []).append(r)
        for key, names in [('foreign', ['Foreign_Investor']), ('trust', ['Investment_Trust']),
                           ('dealer', ['Dealer_self', 'Dealer_Hedging'])]:
            total = Decimal(0)
            valid = True
            for name in names:
                entries = categories.get(name, [])
                if len(entries) != 1:
                    valid = False
                    break
                buy = normalize_shares(entries[0].get('buy'), 'shares')
                sell = normalize_shares(entries[0].get('sell'), 'shares')
                if buy is None or sell is None or min(buy, sell) < 0:
                    valid = False
                    break
                total += buy - sell
            row[key + '_net_shares'] = total if valid else None
        result.append(row)
    return result


def adapt_weekly(symbol, snapshots, observed_at):
    """Live availability is observed, never backdated to snapshot_date."""
    result = []
    for d, stocks in snapshots.items():
        levels = stocks.get(symbol, {})
        total = number(levels.get(17), minimum=1)
        whale = number(levels.get(15), minimum=0)
        mids = [number(levels.get(k), minimum=0) for k in (12, 13)]
        result.append(dict(snapshot_date=d, published_at=observed_at,
                           period_index=(d.toordinal() - d.weekday()) // 7,
                           denominator_id='tdcc-level17-total-shares-v1',
                           total_shares=total,
                           ratio_unit='ratio',
                           whale_ratio=whale / total if total and whale is not None else None,
                           mid_ratio=sum(mids) / total if total and all(x is not None for x in mids) else None))
    return result


def analyze_momentum_history(symbol, daily, weeks, dates, as_of, config, current_result, rule_results=None):
    """Snapshot-date reconstruction; not a publication-time backtest."""
    reconstructed = [dict(w, published_at=datetime.combine(w['snapshot_date'], time.min, TAIPEI_TIMEZONE))
                     for w in weeks]
    days = []
    for day in sorted(set(d for d in dates if d <= as_of))[-10:]:
        if day == as_of:
            result = current_result
        else:
            features = build_features(daily, reconstructed, dates, day,
                                      datetime.combine(day, time.max, TAIPEI_TIMEZONE), config)
            by_date = {row['date']: row for row in daily}
            for rule, window, fields in (
                ('P2', config.institutional_days, ('foreign', 'trust', 'dealer')),
                ('N1', config.sell_days, ('foreign', 'trust')),
            ):
                missing = [d.isoformat() for d in sorted(set(d for d in dates if d <= day))[-window:]
                           if any(by_date.get(d, {}).get(key + '_net_shares') is None for key in fields)]
                if missing:
                    features['reasons'][rule] = '法人資料缺漏日期：' + '、'.join(missing) + '；需同期完整資料，不以零補值'
            result = evaluate(features, config)
        if rule_results is not None:
            rule_results.append(dict({key: value for key, value in result.items()
                                      if key not in ('history_rule_results', 'score_history', 'price_analysis')},
                                     as_of=day.isoformat(), provenance={
                'weekly_snapshot_dates': sorted(w['snapshot_date'].isoformat() for w in weeks if w['snapshot_date'] <= day),
                'history_basis': '本次取得時間' if day == as_of else '快照日期回算（發布時間未驗證）',
            }))
        # A known subtotal must never masquerade as a complete historical score.
        score = result['score'] if result['status'] == 'complete' else None
        days.append(ScoreDay(symbol, day.isoformat(), None, score))
    return days


CONDITIONS = {
 'P1': '千張大戶持股連兩週增加，每週超過 0.10 個百分點',
 'P2': '同期十日三大法人均淨買超 > 十日均量 × 3%',
 'P3': '收盤 > MA5 且收盤 > MA10', 'P4': 'MA20 > MA60',
 'P5': '最近十日均量 > 前十日均量，且收盤 > MA20',
 'B1': '滾動五交易日漲幅 > 5%，且千張大戶持股 > 60%',
 'N1': '近五交易日外資與投信累計皆淨賣超',
 'N2': '千張大戶下降、400～800 張上升，兩者皆超過 0.10 個百分點',
 'N3': '均量增加，且收盤 < MA5、MA10', 'N4': '最近十日均量 < 前十日均量 × 75%',
 'N5': '收盤 < MA5、MA10、MA20',
}
REASONS = {'missing_or_invalid': '必要資料缺漏、日期不連續、單位不明或無法驗證',
           'weekly_unavailable_or_stale': '集保快照無法證明已取得、過期或重複',
           'weekly_gap_or_noncomparable': '集保缺少相鄰週、比例失效或統計口徑變更',
           'zero_volume': '均量為零，不能進行相對量判斷'}


def render_rule_details(result, label):
    esc = lambda value: html.escape(str(value), quote=True)
    details = []
    for x in result['rules']:
        state = {'true': '成立', 'false': '未成立', 'unknown': '缺資料', 'disabled': '停用'}[x['state']]
        if x['suppressed_by']:
            state += '；重疊扣分由 ' + x['suppressed_by'] + ' 計入'
        if x['reason']:
            state += '；' + REASONS.get(x['reason'], x['reason'])
        observed = '；'.join(f'{k}={v if v is not None else "缺資料"}' for k, v in x['observed'].items())
        details.append(f'<tr><td>{x["id"]} {esc(x["label"])}</td><td>{esc(CONDITIONS[x["id"]])}</td><td>{x["weight"]:+d}</td><td>{esc(state)}</td><td>{x["contribution"]:+d}</td><td>{esc(observed)}</td></tr>')
    title = '總分' if result['status'] == 'complete' else '已知訊號小計'
    score = '無法計分' if result['score'] is None else str(result['score']) + ' 分'
    provenance = result.get('provenance', {})
    return f'''<details class="daily-rules" data-score-date="{esc(result.get('as_of', ''))}"><summary>{esc(label)}</summary>
<div class="scroll"><table><thead><tr><th>規則</th><th>預設條件</th><th>設定權重</th><th>判定</th><th>實際分數</th><th>輸入（股數、比例）</th></tr></thead><tbody>{''.join(details)}</tbody></table></div>
<p>{title}：{esc(score)} · 行情截止：{esc(result.get('as_of', '未知'))} · 核心 {result['positive_score']:+d}／加權 {result['bonus_score']:+d}／風險 {result['risk_score']:+d} · 原始分數 {esc(result['raw_score'])}</p>
<p>集保快照：{esc(', '.join(provenance.get('weekly_snapshot_dates', [])) or '缺資料')}；{esc(provenance.get('history_basis', '本次取得時間'))}。各週以該週總股數計算持股比例；統計口徑一致且週次相鄰才比較。</p></details>'''


def render(results, names, as_of, generated_at, config, warnings, news=None):
    esc = lambda s: html.escape(str(s), quote=True)
    original = render_report([], [], [], [], {}, as_of)
    original_style = re.search(r'<style>(.*?)</style>', original, re.S).group(1)
    original_script = re.search(r'<script>(.*?)</script>', original, re.S).group(1)
    original_header = re.search(r'<table class="overview-table".*?(<thead>.*?</thead>)', original, re.S).group(1)
    cards = ['<h2>均線與成交量總覽</h2><p>收盤價相較前一個交易日：上漲為紅色，下跌為綠色，平盤為灰色。</p><div class="table-wrap"><table class="overview-table" id="overview-table">' + original_header + '<tbody id="overview-body">']
    news = news or {}
    news_end = date.fromisoformat(as_of)
    news_start = news_end - timedelta(days=2)
    complete = sorted([r for r in results if r['status'] == 'complete'], key=lambda r: (-r['raw_score'], r['symbol']))
    partial = sorted([r for r in results if r['status'] != 'complete'], key=lambda r: r['symbol'])
    for section, rows in [('完整資料排名', complete), ('資料不完整（不納入排名）', partial)]:
        for i, r in enumerate(rows, 1):
            title = '總分' if r['status'] == 'complete' else '已知訊號小計'
            score = '無法計分' if r['score'] is None else str(r['score']) + ' 分'
            missing_ids = {x['id'] for x in r['rules'] if x['state'] == 'unknown'}
            missing_labels = []
            if missing_ids & {'P2', 'N1'}:
                missing_labels.append('法人資料未齊')
            if missing_ids & {'P1', 'N2', 'B1'}:
                missing_labels.append('集保資料待確認')
            if missing_ids - {'P2', 'N1', 'P1', 'N2', 'B1'}:
                missing_labels.append('行情資料未齊')
            missing_label = '、'.join(missing_labels) or '必要資料未齊'
            daily_results = r.get('history_rule_results', [])
            if daily_results:
                rule_details = ''.join(render_rule_details(day_result,
                    f"查看 {date.fromisoformat(day_result['as_of']).month}/{date.fromisoformat(day_result['as_of']).day} 的11條評分規則與輸入值")
                    for day_result in reversed(daily_results))
            else:
                rule_details = render_rule_details(r, '查看 11 條評分規則與輸入值')
            prefix = str(i) + '. ' if r['status'] == 'complete' else ''
            color = 'positive' if (r['score'] or 0) > 0 else 'negative' if (r['score'] or 0) < 0 else ''
            provenance = r.get('provenance', {})
            row_id = 'stock-' + r['symbol']
            analysis = r.get('price_analysis')
            market_details = '<h3>最近五個交易日法人買賣</h3><p>此期間沒有取得法人資料。</p><h3>最近四週股價與持股分級</h3><p>此期間沒有取得持股分級資料。</p>'
            if analysis is not None:
                history = {r['symbol']: [ScoreDay(r['symbol'], analysis.trading_date, {}, r['score'] if r['score'] is not None else 0)]}
                original_row = render_report([analysis], r.get('institutional_days', []), r.get('weekly_holdings', []), [], names, as_of, score_histories=history)
                market_details = re.search(r'(<h3>[^<]*最近五個交易日法人買賣</h3>.*?)<h3 class="news-heading">', original_row, re.S).group(1)
                summary = re.search(r'<tr class="stock-row".*?</tr>', original_row, re.S).group(0)
                summary = summary.replace('institution-' + esc(r['symbol']), esc(row_id))
                if r['status'] != 'complete':
                    summary = re.sub(r'(<td class="score-cell[^>]*>).*?(</td>)', lambda m: m[1] + esc(score) + '（' + esc(missing_label) + '）' + m[2], summary, count=1)
            else:
                stock_label = esc(r['symbol'] + ' ' + names.get(r['symbol'], ''))
                missing = ''.join('<td data-sort-value="">缺資料</td>' for _ in range(9))
                summary = f'<tr class="stock-row" role="button" tabindex="0" aria-expanded="false" aria-controls="{esc(row_id)}" data-details-id="{esc(row_id)}"><td class="score-cell" data-sort-value="{r["score"] or 0}">{esc(score)}（{esc(missing_label)}）</td><td class="stock-cell" data-sort-value="{stock_label}"><span class="toggle-indicator" aria-hidden="true">▶</span>{stock_label}</td>{missing}</tr>'
            news_rows = []
            for item in news.get(r['symbol'], []):
                link = safe_news_link(item.link)
                headline = f'<a href="{esc(link)}" target="_blank" rel="noopener noreferrer">{esc(item.title)}</a>' if link else esc(item.title)
                news_rows.append(f'<tr><td>{esc(item.published_at)}</td><td>{esc(item.source)}</td><td>{headline}</td></tr>')
            news_html = '<div class="scroll"><table><thead><tr><th>時間</th><th>來源</th><th>新聞標題</th></tr></thead><tbody>' + ''.join(news_rows) + '</tbody></table></div>' if news_rows else '<p>此期間沒有取得相關新聞。</p>'
            history_html = render_history(r.get('score_history', []), r['symbol'] + ' ' + names.get(r['symbol'], ''),
                note='依各日行情、法人與截至該日的集保快照日期回算評分v2；集保發布時間未驗證，可能包含當日尚未公開資料，不代表當時可取得的分數或投資回測。任一啟用規則缺資料即標示資料不足並中斷折線。')
            cards.append(f'''{summary}
<tr id="{esc(row_id)}" class="institution-details stock-detail" hidden><td colspan="11"><section class="institution-panel">
{rule_details}
{history_html}
{market_details}
<h3>近三日個股新聞（{news_start.isoformat()} 至 {news_end.isoformat()}）</h3>{news_html}</section></td></tr>''')
    cards.append('</tbody></table></div>')
    config_json = esc(json.dumps(asdict(config), ensure_ascii=False, indent=2))
    warning_html = ''.join(f'<li>{esc(w)}</li>' for w in warnings)
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>評分v2</title>
<style>{original_style}.scroll{{overflow-x:auto}}details table{{white-space:normal}}details th{{position:static}}summary{{cursor:pointer;color:#1255a1}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.notice{{padding:16px;background:#fff8dd;border-left:4px solid #aa7d00}}[hidden]{{display:none!important}}</style></head><body><main>

<h1>評分v2</h1><p>查詢截止：{esc(as_of)} · 報告產生：{esc(generated_at)} · {esc(config.version)}</p>
<div class="notice">從 0 分累加，五個核心模組各 +20、強勢加權 +15；總分可超過 100 或低於 0。預設放量轉弱與跌破三線只計較重的一筆，完整資料按原始分數排序。資料不完整（不納入排名），只顯示已知小計。</div>
<p>法人與成交量使用相同交易日及原始股數；數值判斷不先四捨五入。價格採 FinMind 未還原收盤價，均線與五日報酬使用相同基準，除權息可能影響訊號。歷史總分採集保快照日期回算，發布時間未驗證；不提供投資回測或勝率。</p>
<ul>{warning_html}</ul>{''.join(cards)}
<details><summary>查看實際參數設定</summary><p>上方條件欄說明預設值；自訂值以此設定與規則輸入為準。修改 momentum_config.json 後重新產生頁面。</p><pre>{config_json}</pre><p>Config SHA-256：{config.digest()}</p></details>
</main><script>{original_script}</script>
</body></html>'''


def generate(stock_ids, end_date, remote, cache, tdcc, config, now, mode='auto', metadata=None):
    warnings = []
    # The API documents TaiwanStockTradingDate as a dataset-only query.
    calendar_rows = remote._fetch_rows({'dataset': 'TaiwanStockTradingDate'})
    latest_closed = now.date() if now.time() >= time(13, 30) else now.date() - timedelta(days=1)
    dates = sorted({date.fromisoformat(str(r['date'])) for r in calendar_rows if date.fromisoformat(str(r['date'])) <= min(end_date, latest_closed)})
    if not dates:
        raise ValueError('No closed trading date available from market calendar')
    as_of = dates[-1]
    if mode not in ('auto', 'daily', 'historical'):
        raise ValueError('Unknown report mode')
    historical = mode == 'historical' or (mode == 'auto' and end_date < now.date())
    cutoff = datetime.combine(end_date, time.max, TAIPEI_TIMEZONE) if historical else now
    try:
        snapshots = tdcc.fetch_recent_snapshots(stock_ids, as_of, count=max(5, config.consecutive_changes + 1))
    except REMOTE_ERRORS as e:
        snapshots = {}
        warnings.append('集保資料取得失敗；相關規則標記缺資料：' + type(e).__name__)
    observed = datetime.now(TAIPEI_TIMEZONE)
    if metadata is not None:
        metadata.update(report_date=end_date.isoformat(), market_as_of=as_of.isoformat(),
                        generated_at=now.isoformat(timespec='seconds'),
                        data_observed_at=observed.isoformat(timespec='seconds'),
                        mode='historical' if historical else 'daily')
    if not historical and end_date < now.date():
        warnings.append('每日批次延遲執行：資料於本次執行時取得，並非原定發布時間的歷史快照。')
    if historical:
        warnings.append('歷史查詢無可驗證的集保發布時間；本次取得的週資料不視為歷史當時已知，相關規則不計分。')
    max_window = max(*config.short_ma, *config.long_ma, *config.weak_ma, *config.breakdown_ma,
                     config.price_ma, config.volume_days * 2, config.institutional_days,
                     config.sell_days, config.return_days + 1)
    start = (as_of - timedelta(days=max(180, max_window * 3))).isoformat()
    try:
        names = remote.fetch_stock_names(stock_ids)
    except REMOTE_ERRORS:
        names = {}
        warnings.append('股票名稱取得失敗，使用代碼顯示。')
    results = []
    news = {}
    for symbol in stock_ids:
        try:
            prices = cache.fetch('TaiwanStockPrice', symbol, start, as_of.isoformat())
        except REMOTE_ERRORS as e:
            prices = []
            warnings.append(symbol + ' 行情資料取得失敗：' + type(e).__name__)
        issue = stock_history_issue(symbol, prices, as_of)
        if issue:
            warnings.append(issue)
            print(f"::warning title=Stock skipped::{issue}", file=sys.stderr)
            continue
        try:
            institutions = cache.fetch('TaiwanStockInstitutionalInvestorsBuySell', symbol, start, as_of.isoformat())
        except REMOTE_ERRORS as e:
            institutions = []
            warnings.append(symbol + ' 法人資料取得失敗：' + type(e).__name__)
        weeks = adapt_weekly(symbol, snapshots, observed)
        daily = adapt_daily(symbol, prices, institutions)
        features = build_features(daily, weeks, dates, as_of,
                                  observed if not historical else cutoff, config)
        by_date = {row['date']: row for row in daily}
        for rule, window, fields in (
            ('P2', config.institutional_days, ('foreign', 'trust', 'dealer')),
            ('N1', config.sell_days, ('foreign', 'trust')),
        ):
            missing_days = [d.isoformat() for d in dates[-window:]
                            if any(by_date.get(d, {}).get(key + '_net_shares') is None for key in fields)]
            if missing_days:
                features['reasons'][rule] = '法人資料缺漏日期：' + '、'.join(missing_days) + '；需同期完整資料，不以零補值'
        result = evaluate(features, config)
        result.update(symbol=symbol, as_of=as_of.isoformat(), provenance={
            'weekly_snapshot_dates': sorted(d.isoformat() for d in snapshots),
            'weekly_observed_at': observed.isoformat(), 'cutoff_at': (observed if not historical else cutoff).isoformat(),
            'units': 'shares', 'price_basis': 'unadjusted-close'})
        try:
            result['price_analysis'] = analyze_price_rows(symbol, prices)
        except (ValueError, KeyError, TypeError):
            result['price_analysis'] = None
        try:
            result['institutional_days'] = analyze_institutional_rows(symbol, institutions, trading_days=5)
        except (ValueError, KeyError, TypeError):
            result['institutional_days'] = []
            warnings.append(symbol + ' 法人明細無法產生；保留其他資料。')
        try:
            result['weekly_holdings'] = analyze_weekly_holdings([symbol], snapshots, {symbol: prices})
        except (ValueError, KeyError, TypeError, ZeroDivisionError):
            result['weekly_holdings'] = []
            warnings.append(symbol + ' 最近四週持股明細資料不足；保留其他資料。')
        result['history_rule_results'] = []
        result['score_history'] = analyze_momentum_history(symbol, daily, weeks, dates, as_of, config, result, result['history_rule_results'])
        results.append(result)
        try:
            news[symbol] = analyze_news_rows(symbol, cache.fetch_stock_news(symbol, end_date, days=3))
        except REMOTE_ERRORS as e:
            news[symbol] = []
            warnings.append(symbol + ' 新聞資料取得失敗：' + type(e).__name__)
    if not results:
        raise ValueError("No stocks with enough recent price history to generate a report")
    warnings.extend(cache.warnings)
    return render(results, names, end_date.isoformat(), now.isoformat(timespec='seconds'), config, warnings, news)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=Path('target.txt'))
    parser.add_argument('--end-date', type=date.fromisoformat, default=datetime.now(TAIPEI_TIMEZONE).date())
    parser.add_argument('--output', type=Path, default=Path('analysis_momentum.html'))
    parser.add_argument('--config', type=Path, default=Path('momentum_config.json'))
    parser.add_argument('--mode', choices=('auto', 'daily', 'historical'), default='auto')
    parser.add_argument('--metadata-output', type=Path)
    args = parser.parse_args()
    cache = None
    try:
        if not API_TOKEN:
            raise ValueError('FINMIND_TOKEN is required')
        if args.output.name in ('analysis.html', 'analysis_param.html', 'index.html'):
            raise ValueError('Momentum output must not overwrite an existing report')
        config = Config(**json.loads(args.config.read_text(encoding='utf-8'))).validate()
        remote = FinMindClient(API_TOKEN)
        cache = CachedFinMindClient(remote, Path(__file__).resolve().parent / 'data' / 'finmind.sqlite3')
        metadata = {}
        content = generate(load_stock_ids(args.target), args.end_date, remote, cache,
                           TdccClient(Path('tdcc_cache')), config, datetime.now(TAIPEI_TIMEZONE),
                           mode=args.mode, metadata=metadata)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding='utf-8')
        if args.metadata_output:
            args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
            args.metadata_output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Momentum report written to', args.output)
        return 0
    except REMOTE_ERRORS + (TypeError, KeyError) as e:
        print('Momentum report failed:', e, file=sys.stderr)
        return 1
    finally:
        if cache is not None:
            cache.close()


if __name__ == '__main__':
    raise SystemExit(main())

