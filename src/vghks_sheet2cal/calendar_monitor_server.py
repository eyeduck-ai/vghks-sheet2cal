"""Loopback-only authenticated control surface, independent of the hospital app."""
import hmac
import json
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from vghks_sheet_core.contract import CONTRACT_VERSION

from .calendar_client import CalendarError
from .google_sheets import SheetError
from .settings import TAIPEI

STATIC = Path(__file__).with_name("static")


def health(application):
    config = application.store.get("config", "settings")
    last = config.get("last_success", "")
    try:
        age = (datetime.now(TAIPEI) - datetime.fromisoformat(last)).total_seconds() if last else None
    except ValueError:
        age = None
    configured = application.store.configured() and config.get("setup_done", False)
    enabled = config.get("enabled", False)
    alive = application.thread.is_alive()
    ready = bool(configured and enabled and alive and age is not None and age <= config["hours"] * 3600 * 2 + 300)
    return {"alive": alive, "configured": configured, "enabled": enabled, "ready": ready,
            "state": "setup_required" if not configured else "paused" if not enabled else "ready" if ready else "awaiting_success",
            "last_success": last, "last_success_age_seconds": round(age) if age is not None else None,
            "consecutive_failures": config.get("consecutive_failures", 0), "contract_version": CONTRACT_VERSION}


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, application):
        self.application = application
        super().__init__(("127.0.0.1", port), MonitorHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def handle_error(self, *_):
        # Request bodies and URLs can contain credentials or clinical content.
        pass


class MonitorHandler(BaseHTTPRequestHandler):
    server_version = "VGHKSSheet2Cal"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def reply(self, status, value, mime="application/json; charset=utf-8", cookie=None):
        payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def local_request(self):
        if (self.headers.get("Host") != self.server.origin.removeprefix("http://")
                or self.headers.get("Sec-Fetch-Site") == "cross-site"
                or self.headers.get("Origin", self.server.origin) != self.server.origin):
            self.close_connection = True
            self.reply(403, {"error": "不允許跨網站或外部主機存取。"})
            return False
        return True

    def authorized(self, *, mutation=False):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            item = cookie.get("sheet2cal_session")
            valid = item and hmac.compare_digest(item.value, self.server.application.session_token)
        except Exception:
            valid = False
        if not valid:
            self.close_connection = True
            self.reply(401, {"error": "請由本機監控入口開啟頁面。"})
            return False
        if mutation and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.application.csrf_token):
            self.close_connection = True
            self.reply(403, {"error": "操作驗證失敗，請重新開啟監控頁面。"})
            return False
        return True

    def do_GET(self):
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        files = {"/": ("calendar-monitor.html", "text/html; charset=utf-8"),
                 "/calendar-monitor.js": ("calendar-monitor.js", "text/javascript; charset=utf-8"),
                 "/calendar-monitor.css": ("calendar-monitor.css", "text/css; charset=utf-8")}
        if path in files:
            name, mime = files[path]
            self.reply(200, (STATIC / name).read_bytes(), mime)
        elif path in {"/healthz", "/readyz"}:
            state = health(self.server.application)
            self.reply(200 if path == "/healthz" or state["ready"] else 503, state)
        elif path == "/api/instance":
            self.reply(200, {"instance": self.server.application.store.get("config", "settings")["worker_id"]})
        elif path == "/api/status":
            if self.authorized():
                self.reply(200, self.server.application.public())
        else:
            self.reply(404, {"error": "找不到此頁面。"})

    def do_POST(self):
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        if path != "/api/session" and not self.authorized(mutation=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= 1048576 or self.headers.get_content_type() != "application/json":
                raise ValueError("請使用大小不超過 1 MiB 的 JSON 物件。")
            values = json.loads(self.rfile.read(length))
            if not isinstance(values, dict):
                raise ValueError("請使用 JSON 物件。")
            self.dispatch_post(self.server.application, path, values)
        except (ValueError, CalendarError, SheetError) as exc:
            self.reply(400, {"error": str(exc) if not isinstance(exc, json.JSONDecodeError) else "JSON 格式錯誤。"})
        except Exception:
            self.reply(500, {"error": "操作未完成；請檢查本機狀態，原紀錄已保留。"})

    def dispatch_post(self, app, path, values):
        if path == "/api/session":
            token = values.get("token", "")
            if not isinstance(token, str) or not hmac.compare_digest(token, app.launch_token):
                self.reply(401, {"error": "請由本機監控入口開啟頁面。"})
                return
            self.reply(200, {"csrf": app.csrf_token}, cookie=f"sheet2cal_session={app.session_token}; HttpOnly; SameSite=Strict; Path=/")
        elif path == "/api/config":
            self.reply(200, app.configure(values))
        elif path == "/api/prepare":
            if values:
                raise ValueError("主機登記不接受額外選項。")
            self.reply(200, app.prepare())
        elif path == "/api/check":
            self.reply(200, app.inspect())
        elif path == "/api/run":
            self.reply(202, app.run_background())
        elif path == "/api/pause":
            self.reply(200, app.pause())
        elif path == "/api/remove-key":
            self.reply(200, app.remove_key())
        else:
            self.reply(404, {"error": "找不到此操作。"})
