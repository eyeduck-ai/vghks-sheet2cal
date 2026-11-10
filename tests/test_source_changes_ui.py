"""Render source observations without claiming that Calendar has confirmed them."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required for browser rendering checks")
class SourceChangesUiTests(unittest.TestCase):
    def test_source_baseline_changes_and_calendar_failure_remain_distinct(self):
        static = Path(__file__).resolve().parents[1] / "src/vghks_sheet2cal/static"
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const staticDir = process.argv[1];
function element() {
  const classes = new Set();
  return {textContent: '', value: '', checked: false, disabled: false, hidden: false,
    classList: {toggle(name, present) { present ? classes.add(name) : classes.delete(name); },
      contains(name) { return classes.has(name); }}, listeners: {},
    addEventListener(type, listener) { this.listeners[type] = listener; },
    replaceChildren(...children) { this.children = children; },
    set innerHTML(value) { throw new Error('Unsafe HTML insertion'); }};
}
const html = fs.readFileSync(staticDir + '/calendar-monitor.html', 'utf8');
const ids = new Map([...html.matchAll(/\bid="([^"]+)"/g)]
  .map(match => [match[1], element()]));
const timers = [];
const requests = [];
let resolveInitialFetch;
const context = vm.createContext({
  document: {getElementById(id) { assert(ids.has(id), 'Missing HTML element ' + id); return ids.get(id); },
    createElement: element},
  location: {hash: ''}, sessionStorage: {getItem() { return ''; }},
  fetch(path, options) {
    requests.push({path, options});
    return new Promise(resolve => { resolveInitialFetch = resolve; });
  },
  setInterval(callback, milliseconds) { timers.push({callback, milliseconds}); }
});
vm.runInContext(fs.readFileSync(staticDir + '/calendar-monitor.js', 'utf8'), context);
const status = {enabled: true, setup_done: true, message: '仍有列待核對', partial: false,
  failure_code: 'conflict', consecutive_failures: 1, last_attempt: '2026-11-10T18:00:00+08:00',
  last_success: '2026-11-10T17:00:00+08:00', updated: 0, deleted: 0, row_issues: [], history: [],
  health: {state: 'blocked', stale: false, last_success_age_seconds: 3600,
    success_max_age_seconds: 7500}};
context.status = status;
status.source_observation = {};
vm.runInContext('paint(status)', context);
assert.equal(ids.get('source-observation').textContent, '尚未建立刀表觀察基準。');
assert.equal(ids.get('source-changes').children.length, 0);
assert.equal(ids.get('source-observation-error').hidden, true);
assert.equal(ids.get('sheets-usage').textContent, '尚未使用');
assert.equal(ids.get('calendar-usage').textContent, '尚未使用');
assert.match(ids.get('source-frequency').textContent, /5 分鐘（300 秒）/);
assert.match(ids.get('source-frequency').textContent, /日曆每 60 分鐘同步/);
assert.match(ids.get('source-frequency').textContent, /不是即時或每次編輯觸發/);
status.source_observation_error = '<img onerror=alert(1)>刀表觀察暫未完成；稍後重試。';
vm.runInContext('paint(status)', context);
assert.equal(ids.get('source-observation-error').hidden, false);
assert.equal(ids.get('source-observation-error').textContent, status.source_observation_error);
assert.match(ids.get('state').textContent, /^同步受阻/,
  'A source observation error is displayed separately from Calendar failure');
delete status.source_observation_error;
status.source_observation = {at: '2026-11-10T18:00:00+08:00', phase: 'source', revision: 1,
  baseline: true, changed: 0, added: 0, moved: 0, modified: 0, removed: 0};
status.source_changes = [];
vm.runInContext('paint(status)', context);
assert.match(ids.get('source-observation').textContent, /已建立刀表觀察基準/);
assert.equal(ids.get('source-observation-error').hidden, true);
assert.match(ids.get('source-observation').textContent, /新增 0、搬移 0、修改 0、移除 0/);
assert.equal(ids.get('source-empty').hidden, false);
assert.match(ids.get('state').textContent, /^同步受阻/);
assert.equal(ids.get('success').textContent,
  '2026-11-10T17:00:00+08:00（距今 1 小時）');
assert(html.includes('不是每次編輯的歷史，也不代表日曆已更新'));
status.source_observation = {at: '2026-11-10T19:00:00+08:00', phase: 'source', revision: 2,
  baseline: false, changed: 4, added: 1, moved: 1, modified: 1, removed: 1,
  diagnostic_counts: {orphan_sync_id: 1}};
const common = {at: '2026-11-10T19:00:00+08:00', revision: 2,
  name: 'PRIVATE-SYNTHETIC-NAME', mrn: 'PRIVATE-SYNTHETIC-MRN', uid: 'PRIVATE-SYNTHETIC-UID'};
status.source_changes = [
  {...common, kind: 'moved', sheet: '<img onerror=alert(1)>202611', row: 8,
    previous_sheet: '202611', previous_row: 3, date: '2026-11-23', previous_date: '2026-11-10',
    hospital: '聯醫', previous_hospital: '高榮', fields: ['date', 'hospital', 'mrn', 'name']},
  {...common, kind: 'modified', sheet: 'FU', row: 5, date: '', previous_date: '2026-11-23',
    fields: ['uid', 'event_id', 'custom', 'PRIVATE-SYNTHETIC-FIELD-VALUE', '__proto__']},
  {...common, kind: 'added', sheet: '202612', row: 4, date: '2026-12-01', hospital: '高榮'},
  {...common, kind: 'removed', sheet: '202611', row: 9, previous_date: '2026-11-11'}
];
vm.runInContext('paint(status)', context);
assert.match(ids.get('source-observation').textContent, /新增 1、搬移 1、修改 1、移除 1/);
assert.match(ids.get('source-observation').textContent, /辨識提示 1 項/);
assert.equal(ids.get('source-empty').hidden, true);
assert.equal(ids.get('source-changes').children.length, 4);
const rendered = ids.get('source-changes').children.map(li => li.textContent);
assert(rendered[0].includes('202611 第 3 列 → <img onerror=alert(1)>202611 第 8 列'));
assert(rendered[0].includes('日期 2026-11-10 → 2026-11-23'));
assert(rendered[0].includes('院區 高榮 → 聯醫'));
assert(rendered[0].includes('變更欄位：日期、院區、病歷號、姓名'));
assert(rendered[1].includes('日期 2026-11-23 → 未填'));
assert(rendered[1].includes('日曆事件綁定'));
assert(rendered[1].includes('其他欄位'));
assert(rendered[2].includes('新增 · 202612 第 4 列'));
assert(rendered[3].includes('移除 · 202611 第 9 列'));
assert(rendered[3].includes('日期 2026-11-11'));
assert(!rendered[3].includes('→ 未填'), 'Removed rows are not described as a cleared date');
assert(!rendered.join(' ').includes('PRIVATE-SYNTHETIC'));
assert(!rendered.join(' ').includes('[object Object]'));
assert.match(ids.get('state').textContent, /^同步受阻/,
  'A recorded source change is not a confirmed Calendar update');
(async () => {
  context.fetch = async (path, options) => {
    requests.push({path, options});
    return {ok: true, json: async () => status};
  };
  resolveInitialFetch({ok: true, json: async () => status});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.length, 1);
  assert.equal(timers[0].milliseconds, 5000);
  assert.equal(ids.get('source-changes').children.length, 4);

  // A quota rejection is a global failure even with a recent successful sync.
  status.failure_code = 'usage_budget';
  status.message = '本機 Google API 用量已達最近 24 小時上限，暫停送出要求；稍後會重新核對。';
  status.consecutive_failures = 2;
  status.last_success = '2026-11-10T19:00:00+08:00';
  status.health.last_success_age_seconds = 60;
  status.api_usage = {
    sheets: {day_calls: 10000, day_units: 10000, day_limit: 10000,
      minute_calls: 45, minute_units: 45, minute_limit: 45},
    calendar: {day_calls: 3, day_units: 9, day_limit: 10000,
      minute_calls: 3, minute_units: 9, minute_limit: 120}
  };
  status.history = [{ok: false, failure_code: 'usage_budget', partial: false,
    message: status.message, updated: 0, deleted: 0}];
  await timers[0].callback();
  assert.match(ids.get('state').textContent, /^同步受阻/);
  assert(ids.get('state').classList.contains('blocked'));
  assert(ids.get('state').textContent.includes(status.message));
  assert.match(ids.get('success').textContent, /19:00:00.*距今 1 分鐘/);
  assert.match(ids.get('counts').textContent, /本輪未完成/);
  assert(!ids.get('counts').textContent.includes('需核對 0'));
  assert.equal(ids.get('failures').textContent, '2');
  assert.equal(ids.get('sheets-usage').textContent,
    '10000 次請求 · 10000 / 10000 單位（最近一分鐘 45 / 45）');
  assert.equal(ids.get('calendar-usage').textContent,
    '3 次請求 · 9 / 10000 單位（最近一分鐘 9 / 120）',
    'Calendar PATCH cost is shown as units separately from HTTP attempt count');
  assert.match(ids.get('history').children[0].textContent, /^未完成/);
  assert.equal(ids.get('source-changes').children.length, 4);

  // After the rolling window frees capacity, a confirmed sync clears the banner.
  status.health.state = 'ready';
  status.failure_code = '';
  status.consecutive_failures = 0;
  status.message = '同步完成：更新 1 筆，刪除 0 筆。';
  status.updated = 1;
  status.last_success = '2026-11-10T19:01:00+08:00';
  status.health.last_success_age_seconds = 0;
  status.api_usage.sheets = {...status.api_usage.sheets,
    day_calls: 10, day_units: 10, minute_calls: 5, minute_units: 5};
  status.api_usage.calendar = {...status.api_usage.calendar,
    day_calls: 4, day_units: 6, minute_calls: 1, minute_units: 3};
  await timers[0].callback();
  assert.match(ids.get('state').textContent, /^同步正常/);
  assert(!ids.get('state').classList.contains('blocked'));
  assert.equal(ids.get('failures').textContent, '0');
  assert.equal(ids.get('calendar-usage').textContent,
    '4 次請求 · 6 / 10000 單位（最近一分鐘 3 / 120）');
  assert.match(ids.get('counts').textContent, /更新 1/);
  assert(!ids.get('counts').textContent.includes('本輪未完成'));
  assert.equal(ids.get('source-changes').children.length, 4,
    'Source observation history survives Calendar recovery');
  assert(requests.length >= 3);
  assert(requests.every(({path, options}) => path === '/api/status'
    && (!options.method || options.method === 'GET') && options.body === undefined),
    'Startup and periodic refresh read local status without invoking check/run/cloud actions');
  status.source_observation_error = '刀表觀察暫未完成；稍後重試。';
  status.hours = 2;
  vm.runInContext('paint(status)', context);
  assert.match(ids.get('state').textContent, /^同步正常/,
    'An observation-only error must not replace the last Calendar result');
  assert.equal(ids.get('source-observation-error').hidden, false);
  assert.match(ids.get('source-frequency').textContent, /日曆每 120 分鐘同步/);
  status.source_changes = Array.from({length: 25}, () => status.source_changes[0]);
  vm.runInContext('paint(status)', context);
  assert.equal(ids.get('source-changes').children.length, 20, 'The source history view is bounded');
  delete status.source_observation;
  delete status.source_changes;
  vm.runInContext('paint(status)', context);
  assert.equal(ids.get('source-changes').children.length, 0);
  assert.equal(ids.get('source-empty').hidden, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(static)], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
