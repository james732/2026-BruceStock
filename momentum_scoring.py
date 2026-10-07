"""Bruce momentum v1: Decimal features, independent rules and auditable scoring."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json

D = Decimal
RULES = {
    'P1': ('籌碼集中', 20), 'P2': ('法人共鳴', 20),
    'P3': ('短線攻擊', 20), 'P4': ('長線保護', 20),
    'P5': ('量價結構', 20), 'B1': ('強勢加權', 15),
    'N1': ('土洋雙殺', -20), 'N2': ('大戶倒貨', -25),
    'N3': ('放量轉弱', -30), 'N4': ('量縮', -15), 'N5': ('跌破三線', -30),
}


def number(value, *, minimum=None, maximum=None):
    try:
        if isinstance(value, bool):
            return None
        n = D(str(value))
        if not n.is_finite() or (minimum is not None and n < minimum) or (maximum is not None and n > maximum):
            return None
        return n
    except (InvalidOperation, ValueError, TypeError):
        return None


@dataclass
class Config:
    version: str = 'bruce-momentum-v1.0'
    weights: dict = field(default_factory=lambda: {k: v[1] for k, v in RULES.items()})
    enabled: dict = field(default_factory=lambda: {k: True for k in RULES})
    short_ma: tuple = (5, 10)
    long_ma: tuple = (20, 60)
    weak_ma: tuple = (5, 10)
    breakdown_ma: tuple = (5, 10, 20)
    price_ma: int = 20
    volume_days: int = 10
    institutional_days: int = 10
    sell_days: int = 5
    return_days: int = 5
    consecutive_changes: int = 2
    buy_ratio: str = '0.03'
    growth_ratio: str = '1.0'
    shrink_ratio: str = '0.75'
    return_min: str = '0.05'
    whale_min: str = '0.60'
    delta_up: str = '0.001'
    delta_down: str = '0.001'
    delta_mid: str = '0.001'
    weekly_max_age_days: int = 14
    shrink_inclusive: bool = False
    conflict_policy: str = 'max_per_group'
    display_bounds: tuple | None = None

    def validate(self):
        if set(self.weights) != set(RULES) or set(self.enabled) != set(RULES):
            raise ValueError('weights/enabled must contain the 11 rule IDs')
        for k, v in self.weights.items():
            if type(v) is not int or (v > 0 if k.startswith('N') else v < 0):
                raise ValueError('Invalid rule weight: ' + k)
        if any(type(v) is not bool for v in self.enabled.values()):
            raise ValueError('enabled values must be boolean')
        windows = [self.price_ma, self.volume_days, self.institutional_days,
                   self.sell_days, self.return_days, self.consecutive_changes]
        for key, count in [('short_ma', 2), ('long_ma', 2), ('weak_ma', 2), ('breakdown_ma', 3)]:
            values = getattr(self, key)
            if len(values) != count:
                raise ValueError('Invalid MA window count')
            windows.extend(values)
        if any(type(n) is not int or n <= 0 for n in windows):
            raise ValueError('Windows must be positive integers')
        if self.volume_days != self.institutional_days:
            raise ValueError('Institutional and volume periods must match')
        for key in ('buy_ratio', 'whale_min', 'delta_up', 'delta_down', 'delta_mid'):
            if number(getattr(self, key), minimum=0, maximum=1) is None:
                raise ValueError('Invalid threshold: ' + key)
        g, s, r = (number(getattr(self, key)) for key in ('growth_ratio', 'shrink_ratio', 'return_min'))
        if g is None or s is None or r is None or not 0 < s < g or r < 0:
            raise ValueError('Invalid growth/shrink/return threshold')
        if type(self.weekly_max_age_days) is not int or self.weekly_max_age_days < 0:
            raise ValueError('Invalid weekly age')
        if type(self.shrink_inclusive) is not bool or self.conflict_policy not in ('max_per_group', 'additive'):
            raise ValueError('Invalid conflict/boundary policy')
        if self.display_bounds is not None:
            if len(self.display_bounds) != 2 or any(type(v) is not int for v in self.display_bounds) or self.display_bounds[0] > self.display_bounds[1]:
                raise ValueError('Invalid display bounds')
        return self

    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def evaluate(features, config=None):
    """None/nonfinite values are unknown even when another AND term is false."""
    c = (config or Config()).validate()
    rules = []

    def rule(key, fields, predicate):
        vals = {f: number(features.get(f)) for f in fields}
        for f, v in list(vals.items()):
            price_field = f in ('close', 'price_ma') or f.startswith(('short_', 'long_', 'weak_', 'break_'))
            if v is not None and ((f == 'whale' and not 0 <= v <= 1) or (price_field and v <= 0)):
                vals[f] = None
        missing = [f for f, v in vals.items() if v is None]
        state = 'disabled' if not c.enabled[key] else 'unknown' if missing else 'true' if predicate(vals) else 'false'
        rules.append(dict(id=key, label=RULES[key][0], state=state, weight=c.weights[key],
                          contribution=c.weights[key] if state == 'true' else 0,
                          suppressed_by=None, missing_fields=missing if state == 'unknown' else [],
                          reason=features.get('reasons', {}).get(key, 'missing_or_invalid') if state == 'unknown' else None,
                          observed={f: str(v) if v is not None else None for f, v in vals.items()}))

    rule('P1', ['whale_delta_' + str(i) for i in range(c.consecutive_changes)],
         lambda x: all(v > D(c.delta_up) for v in x.values()))
    rule('P2', ['institution_mean', 'volume_recent'], lambda x: x['institution_mean'] > D(c.buy_ratio) * x['volume_recent'])
    rule('P3', ['close', 'short_0', 'short_1'], lambda x: x['close'] > x['short_0'] and x['close'] > x['short_1'])
    rule('P4', ['long_0', 'long_1'], lambda x: x['long_0'] > x['long_1'])
    rule('P5', ['close', 'price_ma', 'volume_recent', 'volume_previous'],
         lambda x: x['volume_recent'] > D(c.growth_ratio) * x['volume_previous'] and x['close'] > x['price_ma'])
    rule('B1', ['return', 'whale'], lambda x: x['return'] > D(c.return_min) and x['whale'] > D(c.whale_min))
    rule('N1', ['foreign_sum', 'trust_sum'], lambda x: x['foreign_sum'] < 0 and x['trust_sum'] < 0)
    rule('N2', ['whale_delta_0', 'mid_delta'], lambda x: x['whale_delta_0'] < -D(c.delta_down) and x['mid_delta'] > D(c.delta_mid))
    rule('N3', ['close', 'weak_0', 'weak_1', 'volume_recent', 'volume_previous'],
         lambda x: x['volume_recent'] > D(c.growth_ratio) * x['volume_previous'] and x['close'] < x['weak_0'] and x['close'] < x['weak_1'])
    rule('N4', ['volume_recent', 'volume_previous'],
         lambda x: x['volume_recent'] <= D(c.shrink_ratio) * x['volume_previous'] if c.shrink_inclusive else x['volume_recent'] < D(c.shrink_ratio) * x['volume_previous'])
    rule('N5', ['close', 'break_0', 'break_1', 'break_2'], lambda x: all(x['close'] < x[k] for k in ('break_0', 'break_1', 'break_2')))
    by_id = {r['id']: r for r in rules}
    # Volume features must have a meaningful positive denominator.
    for key in ('P2', 'P5', 'N3', 'N4'):
        r = by_id[key]
        if r['state'] != 'disabled' and any(number(features.get(f)) is not None and number(features[f]) <= 0 for f in (['volume_recent'] if key == 'P2' else ['volume_recent', 'volume_previous'])):
            r.update(state='unknown', contribution=0, reason='zero_volume')
    if c.short_ma == c.weak_ma and by_id['P3']['state'] == by_id['N3']['state'] == 'true':
        raise ValueError('Inconsistent bullish/bearish features')
    if by_id['P1']['state'] == by_id['N2']['state'] == 'true':
        raise ValueError('Inconsistent weekly direction')
    if c.conflict_policy == 'max_per_group':
        hit = [by_id[k] for k in ('N3', 'N5') if by_id[k]['state'] == 'true']
        if len(hit) == 2:
            winner = min(hit, key=lambda r: (r['weight'], r['id']))
            for r in hit:
                if r is not winner:
                    r.update(contribution=0, suppressed_by=winner['id'])
    active = [r for r in rules if r['state'] != 'disabled']
    known = sum(r['state'] in ('true', 'false') for r in active)
    raw = sum(r['contribution'] for r in rules) if known else None
    score = raw
    if raw is not None and c.display_bounds is not None:
        score = max(c.display_bounds[0], min(c.display_bounds[1], raw))
    flags = []
    if by_id['P5']['state'] == by_id['N3']['state'] == 'true':
        flags.append('mixed_signal')
    if any(by_id[k]['state'] == 'unknown' for k in ('N3', 'N5')):
        flags.append('price_weakness_group_incomplete')
    return dict(status='complete' if known == len(active) and active else 'partial',
                positive_score=sum(r['contribution'] for r in rules if r['id'].startswith('P')),
                bonus_score=by_id['B1']['contribution'], risk_score=sum(r['contribution'] for r in rules if r['id'].startswith('N')),
                known_score=raw or 0, raw_score=raw, score=score, coverage=f'{known}/{len(active)}',
                config_version=c.version, config_hash=c.digest(), rules=rules, flags=flags)


def normalize_shares(value, unit):
    n = number(value)
    if n is None or unit not in ('shares', 'lots'):
        return None
    n *= 1000 if unit == 'lots' else 1
    return n if n == n.to_integral_value() else None


def build_features(daily, weekly, trading_dates, as_of, cutoff_at, config=None):
    """Canonical rows use shares and ratios. Date gaps never shorten a window."""
    c = (config or Config()).validate()
    features = {}
    expected = sorted(set(d for d in trading_dates if d <= as_of))
    dates = [r['date'] for r in daily]
    by_date = {r['date']: r for r in daily if r['date'] <= as_of}
    duplicate = len(dates) != len(set(dates))

    def values(n, field, minimum=None):
        if duplicate or n > len(expected) or not expected or expected[-1] != as_of:
            return None
        result = []
        for d in expected[-n:]:
            row = by_date.get(d, {})
            if field.endswith('_shares'):
                v = normalize_shares(row.get(field), row.get('unit'))
            else:
                v = number(row.get(field))
            if v is None or (minimum is not None and v < minimum):
                return None
            result.append(v)
        return result

    def mean(n):
        v = values(n, 'close', D('0.0000000001'))
        return sum(v) / n if v is not None else None

    features['close'] = mean(1)
    for prefix, windows in [('short', c.short_ma), ('long', c.long_ma), ('weak', c.weak_ma), ('break', c.breakdown_ma)]:
        for i, n in enumerate(windows):
            features[f'{prefix}_{i}'] = mean(n)
    features['price_ma'] = mean(c.price_ma)
    vs = values(c.volume_days * 2, 'volume_shares', 0)
    recent = values(c.volume_days, 'volume_shares', 0)
    features['volume_recent'] = sum(recent) / c.volume_days if recent is not None else None
    features['volume_previous'] = sum(vs[:c.volume_days]) / c.volume_days if vs is not None else None
    nets = [values(c.institutional_days, key + '_net_shares') for key in ('foreign', 'trust', 'dealer')]
    features['institution_mean'] = sum(sum(x) for x in nets) / c.institutional_days if all(x is not None for x in nets) else None
    for key in ('foreign', 'trust'):
        v = values(c.sell_days, key + '_net_shares')
        features[key + '_sum'] = sum(v) if v is not None else None
    closes = values(c.return_days + 1, 'close', D('0.0000000001'))
    features['return'] = closes[-1] / closes[0] - 1 if closes else None
    weeks = sorted([r for r in weekly if r['snapshot_date'] <= as_of and r.get('published_at') is not None and r['published_at'] <= cutoff_at], key=lambda r: r['snapshot_date'], reverse=True)
    duplicate_week = len({r['snapshot_date'] for r in weeks}) != len(weeks)

    def valid_week(r):
        return not duplicate_week and 0 <= (as_of - r['snapshot_date']).days <= c.weekly_max_age_days

    def ratio(r, key):
        return number(r.get(key), minimum=0, maximum=1) if r.get('ratio_unit') == 'ratio' else None

    def comparable(a, b):
        return (a.get('period_index') is not None and b.get('period_index') == a['period_index'] - 1
                and a.get('denominator_id') and a.get('denominator_id') == b.get('denominator_id')
                and a.get('comparable', True) and b.get('comparable', True))

    if weeks and valid_week(weeks[0]):
        features['whale'] = ratio(weeks[0], 'whale_ratio')
        chain_ok = True
        for i in range(c.consecutive_changes):
            if len(weeks) <= i + 1 or not comparable(weeks[i], weeks[i+1]):
                chain_ok = False
            if chain_ok:
                a, b = ratio(weeks[i], 'whale_ratio'), ratio(weeks[i+1], 'whale_ratio')
                features['whale_delta_' + str(i)] = a - b if a is not None and b is not None else None
        if len(weeks) > 1 and comparable(weeks[0], weeks[1]):
            a, b = ratio(weeks[0], 'mid_ratio'), ratio(weeks[1], 'mid_ratio')
            features['mid_delta'] = a - b if a is not None and b is not None else None
    features['reasons'] = {}
    if not weeks or not valid_week(weeks[0]):
        features['reasons'].update({k: 'weekly_unavailable_or_stale' for k in ('P1', 'N2', 'B1')})
    else:
        if any(features.get('whale_delta_' + str(i)) is None for i in range(c.consecutive_changes)):
            features['reasons']['P1'] = 'weekly_gap_or_noncomparable'
        if features.get('mid_delta') is None or features.get('whale_delta_0') is None:
            features['reasons']['N2'] = 'weekly_gap_or_noncomparable'
    return features
