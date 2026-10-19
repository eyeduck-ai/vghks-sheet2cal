"""Service lifecycle and local configuration without logging credentials."""
import argparse
import http.client
import json
import os
import signal
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from .calendar_monitor import CalendarMonitor
from .calendar_monitor_server import MonitorServer


def control(directory, path, values=None):
    runtime = json.loads((directory / ".runtime.json").read_text(encoding="utf-8"))
    address = urlsplit(runtime["url"])
    if address.scheme != "http" or address.hostname != "127.0.0.1" or address.path != "/" or not address.port:
        raise ValueError("本機監控入口格式錯誤。")
    connection = http.client.HTTPConnection("127.0.0.1", address.port, timeout=120)
    try:
        connection.request("POST", "/api/session", json.dumps({"token": address.fragment}), {"Content-Type": "application/json"})
        response = connection.getresponse()
        session = json.loads(response.read())
        if response.status != 200:
            raise ValueError("監控入口已失效，請確認服務狀態。")
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        headers = {"Cookie": cookie, "Content-Type": "application/json", "X-CSRF-Token": session["csrf"]}
        connection.request("GET" if values is None else "POST", path,
                           None if values is None else json.dumps(values), headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        if response.status >= 400:
            raise ValueError(result.get("error", "操作失敗。"))
        return result
    finally:
        connection.close()


def serve(directory, port):
    os.umask(0o077)
    monitor = CalendarMonitor(directory)
    runtime = directory / ".runtime.json"
    try:
        with MonitorServer(port, monitor) as server:
            url = server.origin + "/#" + monitor.launch_token
            temporary = directory / ".runtime.tmp"
            temporary.write_text(json.dumps({"url": url, "instance": monitor.store.get("config", "settings")["worker_id"]}), encoding="utf-8")
            temporary.replace(runtime)

            def stop(_number, _frame):
                monitor.stop.set()
                threading.Thread(target=server.shutdown, daemon=True).start()

            previous = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGINT)}
            try:
                print(f"VGHKS sheet2cal listening at {server.origin}; credentials and launch token are omitted.", flush=True)
                server.serve_forever(poll_interval=0.3)
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
    finally:
        monitor.close()
        runtime.unlink(missing_ok=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="獨立刀表與 FU 日曆監控")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "vghks-sheet2cal")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="常駐服務；由 systemd 管理")
    server.add_argument("--port", type=int, default=8765)
    configure = commands.add_parser("configure", help="從本機 JSON 匯入連線設定，保持停用")
    configure.add_argument("--file", type=Path, required=True)
    commands.add_parser("prepare", help="檢查並準備系統欄與主機登記")
    for name in ("enable", "pause", "run-once", "status", "open-ui", "check"):
        commands.add_parser(name)
    test = commands.add_parser("self-test", help="僅使用合成資料，無 Google 連線")
    test.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    directory = args.data_dir.expanduser().resolve()
    try:
        if args.command == "serve":
            return serve(directory, args.port)
        if args.command == "self-test":
            from .selftest_calendar import self_test

            return self_test(args.report)
        if args.command == "open-ui":
            runtime = json.loads((directory / ".runtime.json").read_text(encoding="utf-8"))
            address = urlsplit(runtime["url"])
            if address.hostname != "127.0.0.1" or address.scheme != "http":
                raise ValueError("監控入口格式錯誤。")
            webbrowser.open(runtime["url"])
            return 0
        if args.command == "configure":
            values = json.loads(args.file.read_text(encoding="utf-8"))
            keyfile = Path(values.pop("service_account_file")).expanduser()
            if not keyfile.is_absolute():
                keyfile = args.file.resolve().parent / keyfile
            values.update(key=json.loads(keyfile.read_text(encoding="utf-8")), enabled=False)
            result = control(directory, "/api/config", values)
        elif args.command == "prepare":
            result = control(directory, "/api/prepare", {})
        elif args.command == "enable":
            result = control(directory, "/api/config", {"enabled": True})
        elif args.command == "pause":
            result = control(directory, "/api/pause", {})
        elif args.command == "run-once":
            result = control(directory, "/api/run", {})
        elif args.command == "check":
            result = control(directory, "/api/check", {})
            fields = ("ok", "planned_technical_requests", "monthly_rows", "fu_rows", "owner_registered", "owner_matches", "calendar_readable")
            print(json.dumps({key: result[key] for key in fields}, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 2
        else:
            result = control(directory, "/api/status")
        # CLI summaries never include patient details or the credential envelope.
        fields = ("ok", "configured", "enabled", "setup_done", "worker_alive", "hours", "include_fu",
                  "last_success", "next_check", "failure_code", "consecutive_failures")
        print(json.dumps({key: result[key] for key in fields if key in result}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        print("操作未完成：請確認服務、本機設定檔及權限；可開啟狀態頁查看需核對原因。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
