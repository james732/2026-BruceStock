"""Generate a separate Bruce momentum page without sending email."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import html
import json
from pathlib import Path
import sys

import requests
from main import FinMindClient, TdccClient, TAIPEI_TIMEZONE, API_TOKEN, load_stock_ids, analyze_news_rows, safe_news_link
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
                           denominator_id='tdcc-level17:' + str(total),
                           ratio_unit='ratio',
                           whale_ratio=whale / total if total and whale is not None else None,
                           mid_ratio=sum(mids) / total if total and all(x is not None for x in mids) else None))
    return result


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
           'weekly_gap_or_noncomparable': '集保缺少相鄰週、比例失效或分母變更',
           'zero_volume': '均量為零，不能進行相對量判斷'}


def render(results, names, as_of, generated_at, config, warnings, news=None):
    esc = lambda s: html.escape(str(s), quote=True)
    cards = []
    news = news or {}
    news_end = date.fromisoformat(as_of)
    news_start = news_end - timedelta(days=2)
    complete = sorted([r for r in results if r['status'] == 'complete'], key=lambda r: (-r['raw_score'], r['symbol']))
    partial = sorted([r for r in results if r['status'] != 'complete'], key=lambda r: r['symbol'])
    for section, rows in [('完整資料排名', complete), ('資料不完整（不納入排名）', partial)]:
        headers = ''.join(f'<th>{esc(label)}</th>' for label, _ in RULES.values())
        cards.append(f'<h2>{section}</h2><div class="scroll"><table class="overview"><thead><tr><th>排名</th><th>個股</th><th>總分／小計</th>{headers}<th>資料完整度</th></tr></thead><tbody>')
        if not rows:
            cards.append('<tr><td colspan="15">目前沒有符合此類別的股票。</td></tr>')
        for i, r in enumerate(rows, 1):
            title = '總分' if r['status'] == 'complete' else '已知訊號小計'
            score = '無法計分' if r['score'] is None else str(r['score']) + ' 分'
            details = []
            for x in r['rules']:
                state = {'true': '成立', 'false': '未成立', 'unknown': '缺資料', 'disabled': '停用'}[x['state']]
                if x['suppressed_by']:
                    state += '；重疊扣分由 ' + x['suppressed_by'] + ' 計入'
                if x['reason']:
                    state += '；' + REASONS.get(x['reason'], x['reason'])
                observed = '；'.join(f'{k}={v if v is not None else "缺資料"}' for k, v in x['observed'].items())
                details.append(f'<tr><td>{x["id"]} {esc(x["label"])}</td><td>{esc(CONDITIONS[x["id"]])}</td><td>{x["weight"]:+d}</td><td>{esc(state)}</td><td>{x["contribution"]:+d}</td><td>{esc(observed)}</td></tr>')
            prefix = str(i) + '. ' if r['status'] == 'complete' else ''
            color = 'positive' if (r['score'] or 0) > 0 else 'negative' if (r['score'] or 0) < 0 else ''
            provenance = r.get('provenance', {})
            row_id = 'stock-' + r['symbol']
            cells = []
            for x in r['rules']:
                text = '缺資料' if x['state'] == 'unknown' else '停用' if x['state'] == 'disabled' else f"{x['contribution']:+d}"
                if x['suppressed_by']:
                    text += '（重疊）'
                cells.append(f'<td>{esc(text)}</td>')
            news_rows = []
            for item in news.get(r['symbol'], []):
                link = safe_news_link(item.link)
                headline = f'<a href="{esc(link)}" target="_blank" rel="noopener noreferrer">{esc(item.title)}</a>' if link else esc(item.title)
                news_rows.append(f'<tr><td>{esc(item.published_at)}</td><td>{esc(item.source)}</td><td>{headline}</td></tr>')
            news_html = '<div class="scroll"><table><thead><tr><th>時間</th><th>來源</th><th>新聞標題</th></tr></thead><tbody>' + ''.join(news_rows) + '</tbody></table></div>' if news_rows else '<p>此期間沒有取得相關新聞。</p>'
            cards.append(f'''<tr><td>{i if r['status'] == 'complete' else '—'}</td><td><button type="button" class="stock-toggle" aria-expanded="false" aria-controls="{esc(row_id)}">{esc(r['symbol'])} {esc(names.get(r['symbol'], ''))}</button></td><td class="{color}">{esc(score)}</td>{''.join(cells)}<td>{esc(r['coverage'])}</td></tr>
<tr id="{esc(row_id)}" class="stock-detail" hidden><td colspan="15">
<details><summary>查看 11 條評分規則與輸入值</summary><div class="scroll"><table><thead><tr><th>規則</th><th>預設條件</th><th>設定權重</th><th>判定</th><th>實際分數</th><th>輸入（股數、比例）</th></tr></thead><tbody>{''.join(details)}</tbody></table></div>
<p>{title}：{esc(score)} · 行情截止：{esc(r.get('as_of', '未知'))} · 核心 {r['positive_score']:+d}／加權 {r['bonus_score']:+d}／風險 {r['risk_score']:+d} · 原始分數 {esc(r['raw_score'])}</p>
<p>集保快照：{esc(', '.join(provenance.get('weekly_snapshot_dates', [])) or '缺資料')}；發布時序採本次取得時間，分母變更停止跨週比較。</p></details>
<h3>近三日個股新聞（{news_start.isoformat()} 至 {news_end.isoformat()}）</h3>{news_html}</td></tr>''')
        cards.append('</tbody></table></div>')
    config_json = esc(json.dumps(asdict(config), ensure_ascii=False, indent=2))
    warning_html = ''.join(f'<li>{esc(w)}</li>' for w in warnings)
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>評分v2</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1600px;margin:24px auto;padding:0 20px;background:#f5f7fa;color:#17263c}}nav{{display:flex;gap:20px;flex-wrap:wrap}}a{{color:#1255a1}}h1{{margin-bottom:8px}}article{{background:white;border:1px solid #cbd5e1;border-radius:8px;padding:18px;margin:16px 0}}.headline{{display:flex;gap:20px;justify-content:space-between;align-items:center;flex-wrap:wrap}}strong{{font-size:1.3rem}}.positive{{color:#a12628}}.negative{{color:#167047}}p,li{{line-height:1.7}}summary{{cursor:pointer;color:#1255a1}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:.9rem;margin:16px 0}}td,th{{border:1px solid #cbd5e1;padding:10px;text-align:left;min-width:75px}}th{{background:#edf2f8}}.overview{{white-space:nowrap}}.stock-toggle{{border:0;background:none;color:#1255a1;font:inherit;cursor:pointer;text-align:left;text-decoration:underline}}.stock-detail>td{{background:#f8fafc;padding:20px;white-space:normal}}[hidden]{{display:none!important}}.overview>thead>tr>th{{position:sticky;top:0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.notice{{padding:16px;background:#fff8dd;border-left:4px solid #aa7d00}}@media(max-width:600px){{body{{padding:0 12px}}}}</style></head><body>
<nav><a href="analysis.html">原版報告</a><a href="analysis_param.html">原版參數頁</a><a href="analysis_momentum.html" aria-current="page">評分v2</a></nav>
<h1>評分v2</h1><p>查詢截止：{esc(as_of)} · 報告產生：{esc(generated_at)} · {esc(config.version)}</p>
<div class="notice">從 0 分累加，五個核心模組各 +20、強勢加權 +15；總分可超過 100 或低於 0。預設放量轉弱與跌破三線只計較重的一筆，完整資料按原始分數排序。缺資料只顯示已知小計，不納入排名。</div>
<p>法人與成交量使用相同交易日及原始股數；數值判斷不先四捨五入。價格採 FinMind 未還原收盤價，均線與五日報酬使用相同基準，除權息可能影響訊號。本頁為本次快照，未使用週資料製作歷史回測或勝率。</p>
<ul>{warning_html}</ul>{''.join(cards)}
<details><summary>查看實際參數設定</summary><p>上方條件欄說明預設值；自訂值以此設定與規則輸入為準。修改 momentum_config.json 後重新產生頁面。</p><pre>{config_json}</pre><p>Config SHA-256：{config.digest()}</p></details>
<script>document.querySelectorAll('.stock-toggle').forEach(button=>button.addEventListener('click',()=>{{const row=document.getElementById(button.getAttribute('aria-controls'));const expanded=button.getAttribute('aria-expanded')==='true';button.setAttribute('aria-expanded',String(!expanded));row.hidden=expanded;}}));</script>
</body></html>'''


def generate(stock_ids, end_date, remote, cache, tdcc, config, now):
    warnings = []
    # The API documents TaiwanStockTradingDate as a dataset-only query.
    calendar_rows = remote._fetch_rows({'dataset': 'TaiwanStockTradingDate'})
    latest_closed = now.date() if now.time() >= time(13, 30) else now.date() - timedelta(days=1)
    dates = sorted({date.fromisoformat(str(r['date'])) for r in calendar_rows if date.fromisoformat(str(r['date'])) <= min(end_date, latest_closed)})
    if not dates:
        raise ValueError('No closed trading date available from market calendar')
    as_of = dates[-1]
    historical = end_date < now.date()
    cutoff = datetime.combine(end_date, time.max, TAIPEI_TIMEZONE) if historical else now
    try:
        snapshots = tdcc.fetch_recent_snapshots(stock_ids, as_of, count=max(5, config.consecutive_changes + 1))
    except REMOTE_ERRORS as e:
        snapshots = {}
        warnings.append('集保資料取得失敗；相關規則標記缺資料：' + type(e).__name__)
    observed = datetime.now(TAIPEI_TIMEZONE)
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
        try:
            institutions = cache.fetch('TaiwanStockInstitutionalInvestorsBuySell', symbol, start, as_of.isoformat())
        except REMOTE_ERRORS as e:
            institutions = []
            warnings.append(symbol + ' 法人資料取得失敗：' + type(e).__name__)
        weeks = adapt_weekly(symbol, snapshots, observed)
        features = build_features(adapt_daily(symbol, prices, institutions), weeks, dates, as_of,
                                  observed if not historical else cutoff, config)
        result = evaluate(features, config)
        result.update(symbol=symbol, as_of=as_of.isoformat(), provenance={
            'weekly_snapshot_dates': sorted(d.isoformat() for d in snapshots),
            'weekly_observed_at': observed.isoformat(), 'cutoff_at': (observed if not historical else cutoff).isoformat(),
            'units': 'shares', 'price_basis': 'unadjusted-close'})
        results.append(result)
        try:
            news[symbol] = analyze_news_rows(symbol, cache.fetch_stock_news(symbol, end_date, days=3))
        except REMOTE_ERRORS as e:
            news[symbol] = []
            warnings.append(symbol + ' 新聞資料取得失敗：' + type(e).__name__)
    warnings.extend(cache.warnings)
    return render(results, names, end_date.isoformat(), now.isoformat(timespec='seconds'), config, warnings, news)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=Path('target.txt'))
    parser.add_argument('--end-date', type=date.fromisoformat, default=datetime.now(TAIPEI_TIMEZONE).date())
    parser.add_argument('--output', type=Path, default=Path('analysis_momentum.html'))
    parser.add_argument('--config', type=Path, default=Path('momentum_config.json'))
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
        content = generate(load_stock_ids(args.target), args.end_date, remote, cache,
                           TdccClient(Path('tdcc_cache')), config, datetime.now(TAIPEI_TIMEZONE))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding='utf-8')
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
