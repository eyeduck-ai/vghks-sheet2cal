import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required for browser rendering checks")
class PartialSyncUiTests(unittest.TestCase):
    def test_partial_rows_render_as_text_and_clear_after_recovery(self):
        static = Path(__file__).resolve().parents[1] / "src/vghks_sheet2cal/static"
        # Exercise the shipped browser script against the IDs in the shipped HTML.
        # Reject HTML insertion so sheet names/messages cannot become active markup.
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const staticDir = process.argv[1];
function element() {
  return {textContent: '', value: '', checked: false, disabled: false, hidden: false,
    classList: {toggle() {}}, listeners: {},
    addEventListener(type, listener) { this.listeners[type] = listener; },
    replaceChildren(...children) { this.children = children; },
    set innerHTML(value) { throw new Error('Unsafe HTML insertion'); }};
}
const ids = new Map([...fs.readFileSync(staticDir + '/calendar-monitor.html', 'utf8')
  .matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], element()]));
const context = vm.createContext({
  document: {getElementById(id) { assert(ids.has(id), 'Missing HTML element ' + id); return ids.get(id); },
    createElement: element},
  location: {hash: ''}, sessionStorage: {getItem() { return ''; }},
  fetch() { return new Promise(() => {}); }, setInterval() {}
});
vm.runInContext(fs.readFileSync(staticDir + '/calendar-monitor.js', 'utf8'), context);
const issue = {sheet: '<script>202611</script>', row: 6, message: '<img onerror=alert(1)>需核對排程'};
const status = {enabled: true, message: '部分完成；已處理其他正常資料列',
  last_attempt: '2026-11-01T09:00:00+08:00', updated: 3, deleted: 0,
  row_issues: [issue], partial: true, scope_from: '2026-11-01',
  host_state: 'registered', history: [{ok: false, partial: true, updated: 3,
    row_issues: [issue], message: '部分完成'}]};
context.status = status;
vm.runInContext('paint(status)', context);
assert.equal(ids.get('counts').textContent, '更新 3 筆、刪除 0 筆、需核對 1 列');
assert.equal(ids.get('row-issues-panel').hidden, false);
assert.equal(ids.get('row-issues').children[0].textContent,
  '<script>202611</script> 第 6 列：<img onerror=alert(1)>需核對排程');
assert.match(ids.get('history').children[0].textContent, /^部分完成 · /);
status.partial = false;
status.row_issues = [];
status.updated = 1;
status.message = '同步完成';
status.history = [{ok: true, updated: 1, message: '同步完成'}];
vm.runInContext('paint(status)', context);
assert.equal(ids.get('row-issues-panel').hidden, true);
assert.equal(ids.get('row-issues').children.length, 0);
assert.equal(ids.get('counts').textContent, '更新 1 筆、刪除 0 筆、需核對 0 列');
assert.match(ids.get('history').children[0].textContent, /^完成 · /);
(async () => {
  context.fetch = async path => ({ok: true, json: async () => path === '/api/check'
    ? {ok: false, row_issues: [issue], issues: []} : status});
  await ids.get('check').listeners.click();
  assert.match(ids.get('notice').textContent, /其他正常資料列仍可同步/);
  assert.equal(ids.get('row-issues-panel').hidden, false);
  await vm.runInContext('refresh()', context);
  assert.equal(ids.get('row-issues-panel').hidden, false,
    'Preflight row details must remain visible across periodic refreshes');
  status.last_attempt = '2026-11-01T10:00:00+08:00';
  await vm.runInContext('refresh()', context);
  assert.equal(ids.get('row-issues-panel').hidden, true,
    'A new sync replaces the older preflight result');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(static)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
