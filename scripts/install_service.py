"""Render or install the user systemd service; installation does not configure Google."""
import argparse
import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def quoted(path):
    return '"' + str(path).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit():
    return f'''[Unit]
Description=VGHKS hourly surgery and FU calendar monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={ROOT}
ExecStart={quoted(ROOT / '.runtime/service-venv/bin/python')} -m vghks_sheet2cal --data-dir %S/vghks-sheet2cal serve --port 8765
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=VGHKS_SHEET_LOCK_DIR=%C/vghks-sheet-locks
StateDirectory=vghks-sheet2cal
StateDirectoryMode=0700
CacheDirectory=vghks-sheet-locks
CacheDirectoryMode=0700
UMask=0077
NoNewPrivileges=true
Restart=on-failure
RestartSec=10
TimeoutStopSec=90

[Install]
WantedBy=default.target
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    if not args.install:
        print(unit(), end="")
        return
    directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "vghks-sheet2cal.service"
    if target.exists() and target.read_text() != unit():
        backups = ROOT / ".local/unit-backups"
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = target.read_text()
        backup = backups / ("vghks-sheet2cal-" + hashlib.sha256(content.encode()).hexdigest()[:12] + ".service")
        if not backup.exists():
            backup.write_text(content)
    target.write_text(unit())
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", target.name], check=True)
    print("Installed and started " + str(target))


if __name__ == "__main__":
    main()
