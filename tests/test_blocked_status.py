"""Operational regressions: a live worker must not hide a failed sync round."""
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vghks_sheet2cal.calendar_monitor_server import MonitorHandler, health
from vghks_sheet2cal.settings import TAIPEI

NOW = datetime(2026, 11, 10, 18, 0, tzinfo=TAIPEI)


class BlockedHealthTests(unittest.TestCase):
    def application(self, *, age=timedelta(hours=1), code="", failures=0, partial=False):
        config = {"enabled": True, "setup_done": True, "hours": 1, "consecutive_failures": failures,
                  "last_success": (NOW - age).isoformat()}
        app = SimpleNamespace(
            store=SimpleNamespace(get=lambda *args: config, configured=lambda: True),
            thread=SimpleNamespace(is_alive=lambda: True),
            status={"partial": partial, "failure_code": code, "failed_rows": int(partial)},
        )
        return app, config

    def snapshot(self, app):
        with patch("vghks_sheet2cal.calendar_monitor_server.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            return health(app)

    def test_recent_success_does_not_hide_latest_global_failure(self):
        for code, failures in (("conflict", 1), ("transport", 0), ("", 1)):
            with self.subTest(code=code, failures=failures):
                app, _ = self.application(code=code, failures=failures)
                result = self.snapshot(app)
                self.assertTrue(result["alive"])
                self.assertFalse(result["ready"])
                self.assertEqual(result["state"], "blocked")
                self.assertFalse(result["stale"])
                self.assertEqual(result["failure_code"], code)

    def test_successful_recovery_clears_blocked_state(self):
        app, config = self.application(code="conflict", failures=97, age=timedelta(days=4))
        self.assertFalse(self.snapshot(app)["ready"])
        config.update(consecutive_failures=0, last_success=NOW.isoformat())
        app.status.update(failure_code="", partial=False)
        result = self.snapshot(app)
        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["failure_code"], "")
        self.assertEqual(result["last_success_age_seconds"], 0)

    def test_multiday_stall_reports_age_threshold_and_safe_error_code(self):
        app, _ = self.application(age=timedelta(days=4), code="conflict", failures=97)
        result = self.snapshot(app)
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(result["stale"])
        self.assertEqual(result["last_success_age_seconds"], 4 * 86400)
        self.assertEqual(result["success_max_age_seconds"], 2 * 3600 + 300)
        self.assertEqual(result["consecutive_failures"], 97)
        app.status["failure_code"] = "untrusted detail <script>not a code</script>"
        result = self.snapshot(app)
        self.assertEqual(result["failure_code"], "unknown")
        self.assertNotIn("untrusted detail", str(result))
        self.assertNotIn("message", result)
        self.assertNotIn("row_issues", result)

    def test_stale_success_without_recorded_error_is_not_ready(self):
        app, _ = self.application(age=timedelta(days=4))
        result = self.snapshot(app)
        self.assertFalse(result["ready"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["state"], "awaiting_success")

    def test_partial_and_paused_states_remain_distinct_from_global_block(self):
        app, config = self.application(partial=True, code="row_issues", failures=1)
        self.assertEqual(self.snapshot(app)["state"], "partial")
        self.assertFalse(self.snapshot(app)["ready"])
        config["enabled"] = False
        self.assertEqual(self.snapshot(app)["state"], "paused")

    def test_http_routes_report_failed_sync_separately_from_liveness(self):
        app, _ = self.application(code="conflict", failures=1)
        app.public = lambda: {"message": "需核對識別"}
        for path, expected_status in (("/readyz", 503), ("/healthz", 200), ("/api/status", 200)):
            with self.subTest(path=path):
                responses = []
                handler = SimpleNamespace(
                    path=path, server=SimpleNamespace(application=app), local_request=lambda: True,
                    authorized=lambda: True,
                    reply=lambda status, payload, responses=responses: responses.append((status, payload)),
                )
                with patch("vghks_sheet2cal.calendar_monitor_server.datetime", wraps=datetime) as clock:
                    clock.now.return_value = NOW
                    MonitorHandler.do_GET(handler)
                code, payload = responses[0]
                self.assertEqual(code, expected_status)
                result = payload["health"] if path == "/api/status" else payload
                self.assertFalse(result["ready"])
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["failure_code"], "conflict")


@unittest.skipUnless(shutil.which("node"), "Node.js is required for browser rendering checks")
class BlockedStatusUiTests(unittest.TestCase):
    def test_blocked_stale_and_recovered_status_use_safe_visible_state(self):
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
const ids = new Map([...fs.readFileSync(staticDir + '/calendar-monitor.html', 'utf8')
  .matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], element()]));
const context = vm.createContext({
  document: {getElementById(id) { assert(ids.has(id), 'Missing HTML element ' + id); return ids.get(id); },
    createElement: element},
  location: {hash: ''}, sessionStorage: {getItem() { return ''; }},
  fetch() { return new Promise(() => {}); }, setInterval() {}
});
vm.runInContext(fs.readFileSync(staticDir + '/calendar-monitor.js', 'utf8'), context);
const status = {enabled: true, setup_done: true,
  message: '<img onerror=alert(1)>需核對識別', failure_code: 'conflict',
  consecutive_failures: 97, last_attempt: '2026-11-10T17:00:00+08:00',
  last_success: '2026-11-06T18:00:00+08:00', updated: 0, deleted: 0,
  row_issues: [], partial: false, scope_from: '2026-11-10', history: [],
  health: {state: 'blocked', last_success_age_seconds: 4 * 86400,
    stale: true, success_max_age_seconds: 7500}};
status.history = [{ok: false, failure_code: 'conflict', message: status.message}];
context.status = status;
vm.runInContext('paint(status)', context);
assert.match(ids.get('state').textContent, /^同步受阻 · 定時監控已啟用/);
assert(ids.get('state').classList.contains('blocked'));
assert(!ids.get('state').classList.contains('warning'));
assert(ids.get('state').textContent.includes('<img onerror=alert(1)>'));
assert.match(ids.get('success').textContent, /已 4 天未成功同步/);
assert.match(ids.get('success').textContent, /125 分鐘/);
assert.match(ids.get('counts').textContent, /^本輪未完成/);
assert(!ids.get('counts').textContent.includes('需核對 0 列'));
assert.match(ids.get('history').children[0].textContent, /^未完成 · /);
(async () => {
  context.fetch = async path => ({ok: true, json: async () => path === '/api/check'
    ? {ok: true, monthly_rows: 2, fu_rows: 1, planned_technical_requests: 0} : status});
  await ids.get('check').listeners.click();
  assert.match(ids.get('notice').textContent, /唯讀檢查完成/);
  assert.match(ids.get('state').textContent, /^同步受阻/,
    'A successful connection check cannot erase the latest failed sync result');
  status.partial = true;
  status.failure_code = 'row_issues';
  status.health.state = 'partial';
  status.row_issues = [{sheet: '202611', row: 3, message: '日期需核對'}];
  status.last_attempt = '2026-11-10T18:00:00+08:00';
  vm.runInContext('paint(status)', context);
  assert(ids.get('state').classList.contains('warning'));
  assert(!ids.get('state').classList.contains('blocked'));
  assert.equal(ids.get('row-issues-panel').hidden, false);
  status.partial = false;
  status.failure_code = '';
  status.consecutive_failures = 0;
  status.row_issues = [];
  status.message = '同步完成';
  status.last_attempt = '2026-11-10T19:00:00+08:00';
  status.last_success = status.last_attempt;
  status.health = {state: 'ready', stale: false, last_success_age_seconds: 30,
    success_max_age_seconds: 7500};
  await vm.runInContext('refresh()', context);
  assert.match(ids.get('state').textContent, /^同步正常/);
  assert(!ids.get('state').classList.contains('blocked'));
  assert(!ids.get('state').classList.contains('warning'));
  assert(!ids.get('success').textContent.includes('未成功同步'));
  assert(!ids.get('counts').textContent.includes('本輪未完成'));
  assert.equal(ids.get('row-issues-panel').hidden, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(static)], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
