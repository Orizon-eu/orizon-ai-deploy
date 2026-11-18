"""systemd units this deployment installs on the host.

Only the log cleanup timer: container restart policies already bring the
stack back after a reboot, and certificate renewal runs inside the certbot
container. The units in `systemd/` in the repo hard-code a home directory, so
they are rendered here with the real paths instead of copied.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import ui

UNIT_DIR = Path("/etc/systemd/system")

SERVICE = """[Unit]
Description=OrizonAI log cleanup (container stdout + application logs)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={repo}
ExecStart=/usr/bin/env bash {repo}/cleanup-logs.sh
Environment=MAX_CONTAINER_LOG_MB={max_mb}
Environment=APP_LOG_RETENTION_DAYS={days}
"""

TIMER = """[Unit]
Description=OrizonAI log cleanup (daily)

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
"""

NAME = "orizon-log-cleanup"


def install(repo: Path, max_mb: int = 200, days: int = 14) -> bool:
    if not (repo / "cleanup-logs.sh").exists():
        ui.fail(f"{repo}/cleanup-logs.sh not found in this checkout")
        return False
    try:
        (UNIT_DIR / f"{NAME}.service").write_text(SERVICE.format(repo=repo, max_mb=max_mb, days=days))
        (UNIT_DIR / f"{NAME}.timer").write_text(TIMER)
    except OSError as e:
        ui.fail(f"cannot write units under {UNIT_DIR}: {e}")
        return False
    for cmd in (["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", f"{NAME}.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            ui.fail(f"{' '.join(cmd)}: {r.stderr.strip()}")
            return False
    ui.ok(f"{NAME}.timer enabled (daily at 04:00, ±15 min)")
    return True


def remove() -> None:
    subprocess.run(["systemctl", "disable", "--now", f"{NAME}.timer"], capture_output=True)
    for suffix in ("service", "timer"):
        try:
            (UNIT_DIR / f"{NAME}.{suffix}").unlink()
        except FileNotFoundError:
            pass
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    ui.ok(f"{NAME} units removed")


def status() -> str:
    r = subprocess.run(["systemctl", "list-timers", f"{NAME}.timer", "--no-pager"], capture_output=True, text=True)
    return r.stdout
