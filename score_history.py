"""Self-contained SVG score history, with optional browser-side recalculation."""
from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ScoreDay:
    stock_id: str
    trading_date: str
    conditions: dict[str, bool] | None
    score: int | None


def score_color(score):
    return "#b42318" if score >= 80 else "#067647" if score < 70 else "#17202a"


def render_history(days: list[ScoreDay], label: str) -> str:
    if not days:
        return '<h3>最近 10 個交易日總分</h3><p>尚無歷史分數資料。</p>'
    minimum = min([0] + [day.score for day in days if day.score is not None])
    low = (minimum // 20) * 20
    y = lambda score: 210 - (score - low) / (100 - low) * 180
    x = lambda index: 55 + index * 720 / max(1, len(days) - 1)
    parts = []
    for index in range(5):
        value = low + (100 - low) * index / 4
        position = y(value)
        parts.append(f'<line x1="55" x2="775" y1="{position:g}" y2="{position:g}" stroke="#e4e7ec"/>')
        parts.append(f'<text class="history-tick" x="45" y="{position + 4:g}" text-anchor="end">{value:g}</text>')
    path = []
    connected = False
    for index, day in enumerate(days):
        if day.score is None:
            connected = False
            continue
        path.append(f'{"L" if connected else "M"}{x(index):g},{y(day.score):g}')
        connected = True
    parts.append(f'<path class="history-line" d="{" ".join(path)}" fill="none" stroke="#175cd3" stroke-width="2.5"/>')
    for index, day in enumerate(days):
        parts.append(f'<text x="{x(index):g}" y="235" text-anchor="middle">{html.escape(day.trading_date[5:])}</text>')
        if day.score is not None:
            color = score_color(day.score)
            parts.append(f'<g class="history-point" data-index="{index}"><circle cx="{x(index):g}" cy="{y(day.score):g}" r="4" fill="{color}"><title>{day.trading_date}：{day.score} 分</title></circle><text x="{x(index):g}" y="{y(day.score)-10:g}" text-anchor="middle" fill="{color}">{day.score}</text></g>')
    payload = html.escape(json.dumps([asdict(day) for day in days]), quote=True)
    headers = ''.join(f'<th scope="col">{html.escape(day.trading_date)}</th>' for day in days)
    cells = ''.join(f'<td class="history-value" style="color:{score_color(day.score) if day.score is not None else "#667085"}">{day.score if day.score is not None else "資料不足"}</td>' for day in days)
    return f'''<section class="score-history" data-history="{payload}">
      <h3>最近 10 個交易日總分</h3>
      <p>依各交易日當時資料回算；行情不足 60 筆時標示資料不足。實際顯示 {len(days)} 個交易日。</p>
      <div class="nested-table-wrap"><svg class="history-chart" viewBox="0 0 820 260" role="img" aria-label="{html.escape(label, quote=True)} 最近 10 個交易日總分折線圖">
      <title>{html.escape(label)} 歷史總分；每日數值列於下方表格</title>
      <text x="12" y="16">總分</text>{''.join(parts)}<text x="790" y="253" text-anchor="end">交易日期</text></svg></div>
      <div class="nested-table-wrap"><table class="holding-table"><caption>每日總分</caption><thead><tr>{headers}</tr></thead><tbody><tr>{cells}</tr></tbody></table></div>
    </section>'''


HISTORY_CSS = """
    .score-history { margin-bottom: 24px; }
    .score-history p { font-size: .9rem; color: #475467; }
    .history-chart { display: block; width: 100%; min-width: 760px; max-height: 320px; background: #fff; }
    .history-chart text { font: 12px sans-serif; }
    .history-value { font-weight: 700 !important; }
"""


HISTORY_SCRIPT = """
  function updateHistory(panel, values) {
    panel.querySelectorAll('.score-history').forEach(section => {
      const days = JSON.parse(section.dataset.history);
      const scores = days.map(day => day.conditions === null ? null : Math.min(100,
        100 + Object.entries(values).reduce((sum, [key, value]) => sum + (day.conditions[key] ? value : 0), 0)));
      const low = Math.floor(Math.min(0, ...scores.filter(score => score !== null)) / 20) * 20;
      const y = score => 210 - (score - low) / (100 - low) * 180;
      const x = index => 55 + index * 720 / Math.max(1, days.length - 1);
      const color = score => score >= 80 ? '#b42318' : score < 70 ? '#067647' : '#17202a';
      section.querySelectorAll('.history-tick').forEach((tick, index) => {
        tick.textContent = String(low + (100 - low) * index / 4);
      });
      let connected = false;
      const path = [];
      scores.forEach((score, index) => {
        if (score === null) { connected = false; return; }
        path.push(`${connected ? 'L' : 'M'}${x(index)},${y(score)}`);
        connected = true;
      });
      section.querySelector('.history-line').setAttribute('d', path.join(' '));
      section.querySelectorAll('.history-point').forEach(point => {
        const index = Number(point.dataset.index), score = scores[index];
        const circle = point.querySelector('circle'), label = point.querySelector('text');
        circle.setAttribute('cy', y(score));
        circle.setAttribute('fill', color(score));
        circle.querySelector('title').textContent = `${days[index].trading_date}：${score} 分`;
        label.setAttribute('y', y(score) - 10);
        label.setAttribute('fill', color(score));
        label.textContent = String(score);
      });
      section.querySelectorAll('.history-value').forEach((cell, index) => {
        cell.textContent = scores[index] === null ? '資料不足' : String(scores[index]);
        cell.style.color = scores[index] === null ? '#667085' : color(scores[index]);
      });
    });
  }
"""
