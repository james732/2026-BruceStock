import copy
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from main import TAIPEI_TIMEZONE
from momentum_scoring import Config, RULES, evaluate, build_features, normalize_shares
from momentum_report import adapt_daily, adapt_weekly, render, generate, main


def bullish():
    return dict(close='110', short_0='105', short_1='102', long_0='100', long_1='90',
                weak_0='105', weak_1='102', break_0='105', break_1='102', break_2='100',
                price_ma='100', volume_recent='120', volume_previous='100',
                institution_mean='5', foreign_sum='10', trust_sum='10',
                whale_delta_0='.0011', whale_delta_1='.0011', mid_delta='0', whale='.61',
                **{'return': '.06'})


def fixture():
    days = []
    d = date(2026, 6, 1)
    while len(days) < 70:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    daily = [dict(date=d, close=str(100+i), volume_shares=1000000,
                  foreign_net_shares=31000, trust_net_shares=0, dealer_net_shares=0, unit='shares')
             for i, d in enumerate(days)]
    end = days[-1]
    stamp = datetime.combine(end, datetime.min.time(), TAIPEI_TIMEZONE)
    weeks = [dict(snapshot_date=end-timedelta(days=7*i), published_at=stamp,
                  period_index=100-i, denominator_id='same', whale_ratio=str(D('.61')-D('.0011')*i),
                  mid_ratio='.10', ratio_unit='ratio') for i in range(3)]
    return daily, weeks, days, end, stamp


class MomentumRulesTests(unittest.TestCase):
    def result(self, f=None, config=None):
        return evaluate(f or bullish(), config)

    def rule(self, key, f=None, config=None):
        return next(r for r in self.result(f, config)['rules'] if r['id'] == key)

    def test_115_and_display_clipping(self):
        self.assertEqual(self.result()['raw_score'], 115)
        r = self.result(config=Config(display_bounds=(0, 100)))
        self.assertEqual((r['raw_score'], r['score']), (115, 100))

    def bearish(self, volume):
        f = bullish()
        f.update(close='80', long_0='80', long_1='90', volume_recent=volume,
                 institution_mean='0', foreign_sum='-1', trust_sum='-1',
                 whale_delta_0='-.002', mid_delta='.002', **{'return': '0'})
        return f

    def test_negative_and_de_duplication(self):
        f = self.bearish('70')
        self.assertEqual(self.result(f)['raw_score'], -90)
        self.assertEqual(self.result(f, Config(display_bounds=(0, 100)))['score'], 0)
        f = self.bearish('120')
        a = self.result(f, Config(conflict_policy='additive'))
        b = self.result(f)
        self.assertEqual(a['raw_score'], -105)
        self.assertEqual(b['raw_score'], -75)
        self.assertEqual(self.rule('N5', f)['suppressed_by'], 'N3')
        self.assertEqual(self.rule('N5', f)['state'], 'true')

    def test_larger_weight_wins(self):
        c = Config()
        c.weights['N3'] = -20
        r = self.rule('N3', self.bearish('120'), c)
        self.assertEqual((r['contribution'], r['suppressed_by']), (0, 'N5'))

    def test_institutional_three_percent_boundary(self):
        for mean, expected in [('30000', 'false'), ('30000.1', 'true'), ('10000', 'false')]:
            with self.subTest(mean=mean):
                f = bullish(); f.update(volume_recent='1000000', institution_mean=mean)
                self.assertEqual(self.rule('P2', f)['state'], expected)

    def test_volume_boundaries(self):
        for volume, p5, n4 in [('100','false','false'), ('75','false','false'), ('74.999','false','true'), ('101','true','false')]:
            f = bullish(); f['volume_recent'] = volume
            self.assertEqual(self.rule('P5', f)['state'], p5)
            self.assertEqual(self.rule('N4', f)['state'], n4)
        f['volume_recent'] = '75'
        self.assertEqual(self.rule('N4', f, Config(shrink_inclusive=True))['state'], 'true')

    def test_zero_volume_is_unknown(self):
        for field in ('volume_recent', 'volume_previous'):
            f = bullish(); f[field] = 0
            for rule in ('P5', 'N3', 'N4'):
                self.assertEqual(self.rule(rule, f)['state'], 'unknown')
        self.assertEqual(self.rule('P2', dict(bullish(), volume_recent=0))['state'], 'unknown')

    def test_equal_ma_and_bonus_boundaries(self):
        f = bullish(); f['short_0'] = '110'
        self.assertEqual(self.rule('P3', f)['state'], 'false')
        f['long_0'] = f['long_1']
        self.assertEqual(self.rule('P4', f)['state'], 'false')
        for ret, whale in [('.05','.61'), ('.051','.60')]:
            f = bullish(); f.update(whale=whale, **{'return':ret})
            self.assertEqual(self.rule('B1', f)['state'], 'false')
        f = bullish(); f['whale'] = '60.22'
        self.assertEqual(self.rule('B1', f)['state'], 'unknown')

    def test_weekly_delta_boundaries(self):
        f = bullish(); f['whale_delta_0'] = '.001'
        self.assertEqual(self.rule('P1', f)['state'], 'false')
        self.assertEqual(self.rule('P1', f, Config(delta_up='0'))['state'], 'true')
        f = self.bearish('100'); f['whale_delta_0'] = '-.001'
        self.assertEqual(self.rule('N2', f)['state'], 'false')

    def test_dual_sell_boundary(self):
        f = bullish(); f.update(foreign_sum=-1, trust_sum=0)
        self.assertEqual(self.rule('N1', f)['state'], 'false')
        f['trust_sum'] = -1
        self.assertEqual(self.rule('N1', f)['state'], 'true')
        self.assertEqual(self.rule('P2', f)['state'], 'true')

    def test_unknowns_and_disabled(self):
        for value in (None, 'NaN', 'Infinity'):
            f = bullish(); f['long_1'] = value
            self.assertEqual(self.rule('P4', f)['state'], 'unknown')
            self.assertEqual(self.result(f)['status'], 'partial')
        r = evaluate({})
        self.assertEqual((r['score'], r['raw_score'], r['known_score'], r['coverage']), (None, None, 0, '0/11'))
        c = Config(); c.enabled['P4'] = False
        f = bullish(); f['long_1'] = None
        self.assertEqual(self.result(f,c)['coverage'], '10/10')
        self.assertEqual(self.result(f,c)['status'], 'complete')

    def test_unknown_group_preserves_known_penalty(self):
        f = self.bearish('120'); f['break_2'] = None
        r = self.result(f)
        self.assertEqual(self.rule('N3', f)['contribution'], -30)
        self.assertIn('price_weakness_group_incomplete', r['flags'])

    def test_mixed_signal_retained(self):
        f = self.bearish('120'); f['price_ma'] = '70'; f['break_2'] = '70'
        self.assertEqual(self.rule('P5', f)['contribution'], 20)
        self.assertEqual(self.rule('N3', f)['contribution'], -30)
        self.assertIn('mixed_signal', self.result(f)['flags'])

    def test_invalid_config_and_repeatable_hash(self):
        for kwargs in [dict(delta_up='-.1'), dict(volume_days=0), dict(institutional_days=5), dict(shrink_ratio='1.1'), dict(conflict_policy='other')]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs).validate()
        c=Config(); c.weights['N3']=30
        with self.assertRaises(ValueError): c.validate()
        self.assertEqual(Config().digest(), Config(**json.loads(json.dumps(asdict(Config())))).digest())

    def test_scale_and_feature_order_invariance(self):
        f = bullish(); before = self.result(f)
        for field in ('volume_recent','volume_previous','institution_mean','foreign_sum','trust_sum'):
            f[field] = D(str(f[field])) * 1000
        after = self.result(dict(reversed(list(f.items()))))
        self.assertEqual(before['raw_score'], after['raw_score'])
        self.assertEqual([r['state'] for r in before['rules']], [r['state'] for r in after['rules']])


class MomentumAdapterTests(unittest.TestCase):
    def build(self, daily=None, weeks=None, **kwargs):
        ds, ws, dates, end, stamp = fixture()
        return build_features(ds if daily is None else daily, ws if weeks is None else weeks, dates, end, stamp, **kwargs)

    def test_full_feature_alignment_and_weekly_precision(self):
        f = self.build()
        self.assertEqual(evaluate(f)['status'], 'complete')
        self.assertEqual(f['institution_mean'], 31000)
        self.assertEqual(f['whale_delta_0'], D('.0011'))

    def test_missing_day_does_not_shorten_window(self):
        ds, ws, days, end, stamp = fixture(); del ds[-2]
        f = self.build(ds)
        self.assertIsNone(f['volume_recent'])
        self.assertIsNone(f['institution_mean'])
        self.assertEqual(evaluate(f)['status'], 'partial')

    def test_missing_latest_does_not_use_old_price(self):
        ds, *_ = fixture(); ds.pop()
        self.assertIsNone(self.build(ds)['close'])

    def test_duplicate_and_unknown_unit(self):
        ds, *_ = fixture(); ds.append(copy.deepcopy(ds[-1]))
        self.assertIsNone(self.build(ds)['close'])
        ds, *_ = fixture(); ds[-1]['unit']='money'
        self.assertIsNone(self.build(ds)['volume_recent'])
        self.assertEqual(normalize_shares('1000','lots'), D('1000000'))
        self.assertIsNone(normalize_shares('1000','money'))

    def test_missing_dealer_only_affects_institutional_bonus(self):
        ds, *_ = fixture(); ds[-8]['dealer_net_shares']=None
        f = self.build(ds)
        self.assertEqual(next(r for r in evaluate(f)['rules'] if r['id']=='P2')['state'],'unknown')
        self.assertIsNotNone(f['foreign_sum'])

    def test_nonoverlapping_volume_and_zero_day(self):
        ds, *_ = fixture()
        for r in ds[-20:-10]: r['volume_shares']=100
        for r in ds[-10:]: r['volume_shares']=200
        f=self.build(ds)
        self.assertEqual((f['volume_previous'],f['volume_recent']),(100,200))
        ds[-1]['volume_shares']=0
        self.assertEqual(self.build(ds)['volume_recent'],180)

    def test_19_days_and_return_rounding(self):
        ds, ws, dates, end, stamp = fixture()
        f = build_features(ds[-19:],ws,dates,end,stamp)
        self.assertIsNone(f['volume_previous'])
        ds[-6]['close']='100'; ds[-1]['close']='105.0001'
        self.assertGreater(self.build(ds)['return'],D('.05'))

    def test_missing_intermediate_week_and_only_two_weeks(self):
        _,ws,*_=fixture()
        f=self.build(weeks=[ws[0],ws[2]])
        self.assertNotIn('whale_delta_0',f)
        self.assertIsNotNone(f['whale'])
        f=self.build(weeks=ws[:2])
        self.assertIsNotNone(f['whale_delta_0'])
        self.assertNotIn('whale_delta_1',f)

    def test_future_publication_and_weekly_age(self):
        _,ws,_,end,stamp=fixture()
        ws[0]['published_at']=stamp+timedelta(days=1)
        f=self.build(weeks=ws)
        self.assertEqual(f['whale'],D(ws[1]['whale_ratio']))
        for age, usable in [(14,True),(15,False)]:
            _,ws,*_=fixture()
            for i,w in enumerate(ws): w['snapshot_date']=end-timedelta(days=age+7*i)
            self.assertEqual('whale' in self.build(weeks=ws), usable)

    def test_bad_weekly_ratio_and_denominator(self):
        _,ws,*_=fixture(); ws[0]['whale_ratio']='60.22'
        self.assertIsNone(self.build(weeks=ws)['whale'])
        _,ws,*_=fixture(); ws[1]['denominator_id']='changed'
        f=self.build(weeks=ws)
        self.assertNotIn('whale_delta_0',f)
        self.assertIsNotNone(f['whale'])

    def test_raw_institutional_category_validation(self):
        p=[dict(date='2026-10-06',close=100,Trading_Volume=1000000)]
        names=['Foreign_Investor','Investment_Trust','Dealer_self','Dealer_Hedging','Foreign_Dealer_Self']
        inst=[dict(date='2026-10-06',name=n,buy=31000,sell=0) for n in names]
        row=adapt_daily('2330',p,inst)[0]
        self.assertEqual(row['dealer_net_shares'],62000)
        self.assertEqual(row['foreign_net_shares'],31000)
        del inst[3]
        self.assertIsNone(adapt_daily('2330',p,inst)[0]['dealer_net_shares'])
        inst.append(copy.deepcopy(inst[0]))
        self.assertIsNone(adapt_daily('2330',p,inst)[0]['foreign_net_shares'])

    def test_weekly_raw_levels_ratio_and_period_year_boundary(self):
        stamp=datetime(2026,1,5,tzinfo=TAIPEI_TIMEZONE)
        snapshots={date(2026,1,2):{'2330':{12:100,13:200,15:610,17:1000}},
                   date(2025,12,26):{'2330':{12:100,13:200,15:600,17:1000}}}
        w=sorted(adapt_weekly('2330',snapshots,stamp), key=lambda w:w['snapshot_date'])
        self.assertEqual(w[-1]['mid_ratio'],D('.3'))
        self.assertEqual(w[-1]['whale_ratio'],D('.61'))
        self.assertEqual(w[-1]['period_index']-w[0]['period_index'],1)


class MomentumPageTests(unittest.TestCase):
    def test_separate_sections_and_html_escape(self):
        a=evaluate(bullish()); a.update(symbol='2330',as_of='2026-10-06')
        b=evaluate({}); b.update(symbol='<script>',as_of='2026-10-06')
        s=render([b,a],{'2330':'台積電'},'2026-10-06','2026-10-07',Config(),['<error>'])
        self.assertIn('115 分',s)
        self.assertIn('資料不完整（不納入排名）',s)
        self.assertIn('&lt;error&gt;',s)
        self.assertNotIn('stock-<script>',s)
        self.assertIn('stock-&lt;script&gt;',s)
        self.assertIn('<th>籌碼集中</th>',s)
        self.assertIn('aria-expanded="false"',s)
        self.assertIn('class="stock-detail" hidden',s)
        self.assertLess(s.index('查看 11 條評分規則與輸入值'), s.index('近三日個股新聞'))
        self.assertIn('analysis_momentum.html',s)
        self.assertIn('Config SHA-256',s)

    def test_generate_with_network_mocks_and_missing_tdcc(self):
        remote=Mock(); remote._fetch_rows.return_value=[{'date':'2026-10-06'}]
        remote.fetch_stock_names.return_value={'2330':'台積電'}
        cache=Mock(); cache.fetch.return_value=[]; cache.fetch_stock_news.return_value=[]; cache.warnings=[]
        tdcc=Mock(); tdcc.fetch_recent_snapshots.side_effect=RuntimeError('missing')
        now=datetime(2026,10,7,11,tzinfo=TAIPEI_TIMEZONE)
        s=generate(['2330'],now.date(),remote,cache,tdcc,Config(),now)
        self.assertIn('集保資料取得失敗',s)
        self.assertIn('無法計分',s)
        remote._fetch_rows.assert_called_once_with({'dataset':'TaiwanStockTradingDate'})
        cache.fetch_stock_news.assert_called_once_with('2330', now.date(), days=3)

    def test_news_window_and_safe_links(self):
        from main import analyze_news_rows
        a = evaluate(bullish()); a.update(symbol='2330')
        news = analyze_news_rows('2330', [dict(date='2026-10-07', title='<headline>', source='news', link='javascript:alert(1)')])
        page = render([a], {}, '2026-10-07', 'now', Config(), [], {'2330': news})
        self.assertIn('2026-10-05 至 2026-10-07', page)
        self.assertIn('&lt;headline&gt;', page)
        self.assertNotIn('javascript:', page)

    def test_configuration_file_valid(self):
        Config(**json.loads(Path(__file__).with_name('momentum_config.json').read_text())).validate()

    def test_cli_never_overwrites_legacy_output(self):
        with patch('momentum_report.API_TOKEN','test'), patch('sys.argv',['momentum_report.py','--output','analysis.html']):
            self.assertEqual(main(),1)


if __name__ == '__main__': unittest.main()
