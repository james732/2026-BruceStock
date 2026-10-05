// Exercise the generated report script against a minimal DOM, without a browser.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {script, histories, defaults} = JSON.parse(fs.readFileSync(0, 'utf8'));
function element(text = '') {
  return {textContent: String(text), dataset: {}, style: {}, attrs: {}, events: {},
    classList: {remove() {}, add() {}},
    setAttribute(key, value) { this.attrs[key] = String(value); },
    getAttribute(key) { return this.attrs[key] ?? null; },
    hasAttribute(key) { return key in this.attrs; },
    removeAttribute(key) { delete this.attrs[key]; },
    addEventListener(key, handler) { this.events[key] = handler; }};
}
const panels = new Map();
const rows = Object.entries(histories).map(([stock, days]) => {
  const section = element();
  section.dataset.history = JSON.stringify(days);
  section.line = element();
  section.ticks = Array.from({length: 5}, () => element());
  section.values = days.map(day => element(day.score ?? '資料不足'));
  section.points = days.flatMap((day, index) => {
    if (day.score === null) return [];
    const point = element(); point.dataset.index = String(index);
    const circle = element(), title = element(), label = element();
    circle.querySelector = () => title;
    point.querySelector = selector => selector === 'circle' ? circle : label;
    return [point];
  });
  section.querySelector = () => section.line;
  section.querySelectorAll = selector => ({'.history-tick': section.ticks,
    '.history-value': section.values, '.history-point': section.points}[selector]);
  const panel = element(); panel.hidden = true;
  panel.querySelectorAll = () => [section]; panel.section = section;
  panels.set(`institution-${stock}`, panel);
  const row = element();
  row.dataset.detailsId = `institution-${stock}`;
  row.dataset.scoreConditions = JSON.stringify(days.at(-1).conditions);
  row.cells = [element(days.at(-1).score), element(stock)];
  row.cells[0].dataset.sortValue = String(days.at(-1).score);
  row.cells[1].dataset.sortValue = stock;
  return row;
});
const inputs = Object.entries(defaults).map(([name, value]) => ({name, value: String(value), defaultValue: String(value),
  get valueAsNumber() { return this.value === '' ? NaN : Number(this.value); }}));
const form = element();
form.querySelectorAll = () => inputs;
form.checkValidity = () => inputs.every(input => Number.isInteger(input.valueAsNumber) && Math.abs(input.valueAsNumber) <= 100);
const status = element(), overview = element(), header = element(), button = element();
header.setAttribute('aria-sort', 'descending');
button.dataset = {column: '0', type: 'number'};
header.querySelector = () => button;
overview.querySelectorAll = () => rows;
const appended = [];
overview.append = (row, panel) => { assert.equal(panels.get(row.dataset.detailsId), panel); appended.push(row); };
const document = {
  querySelectorAll: selector => selector === '.stock-row' ? rows : [header],
  getElementById: id => ({'overview-body': overview, 'score-form': form, 'score-status': status}[id] ?? panels.get(id)),
};
vm.runInNewContext(script, {document});
const calc = (conditions, values) => Math.min(100, 100 + Object.entries(values).reduce((sum, [key, value]) => sum + (conditions[key] ? value : 0), 0));
function verify(values) {
  for (const row of rows) {
    const section = panels.get(row.dataset.detailsId).section;
    const days = JSON.parse(section.dataset.history);
    const scores = days.map(day => day.conditions === null ? null : calc(day.conditions, values));
    assert.equal(row.cells[0].textContent, String(scores.at(-1)));
    assert.deepEqual(section.values.map(cell => cell.textContent), scores.map(score => score === null ? '資料不足' : String(score)));
    const low = Math.floor(Math.min(0, ...scores.filter(score => score !== null)) / 20) * 20;
    assert.equal(section.ticks[0].textContent, String(low));
    for (const point of section.points) {
      const index = Number(point.dataset.index), score = scores[index];
      assert.equal(point.querySelector('text').textContent, String(score));
      assert.equal(Number(point.querySelector('circle').getAttribute('cy')), 210 - (score - low) / (100 - low) * 180);
    }
    assert(!section.line.getAttribute('d').includes('NaN'));
  }
}
inputs.forEach(input => { input.value = '-100'; });
form.events.input();
verify(Object.fromEntries(inputs.map(input => [input.name, -100])));
const unchanged = rows.map(row => row.cells[0].textContent);
inputs[0].value = '101'; form.events.input();
assert.deepEqual(rows.map(row => row.cells[0].textContent), unchanged);
assert(status.textContent.includes('保留上次有效結果'));
form.events.reset(); verify(defaults);
rows[0].events.click(); assert.equal(panels.get(rows[0].dataset.detailsId).hidden, false);
appended.length = 0;
button.events.click(); assert.equal(header.getAttribute('aria-sort'), 'ascending');
assert.equal(appended.length, rows.length);
assert(Number(appended[0].cells[0].textContent) <= Number(appended.at(-1).cells[0].textContent));
verify(defaults);
console.log('Report JS: recalculation, negative axis, gaps, invalid input, reset, toggle and sorting passed.');
