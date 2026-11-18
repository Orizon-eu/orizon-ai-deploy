"""Read-only facts about the host: OS, hardware, GPUs, Docker, ports.

Nothing here changes the machine. Every function degrades to a "don't know"
value instead of raising, so preflight can report what it could not check.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def run(cmd: list[str], timeout: int = 30, **kw) -> subprocess.CompletedProcess:
    """subprocess.run that never raises on a missing binary or a timeout."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]}: timed out")


def which(name: str) -> Optional[str]:
    return shutil.which(name)


# --------------------------------------------------------------------------- OS


def os_release() -> dict[str, str]:
    out: dict[str, str] = {}
    p = Path("/etc/os-release")
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"')
    return out


def is_linux() -> bool:
    return platform.system() == "Linux"


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def arch() -> str:
    return platform.machine()


def kernel() -> str:
    return platform.release()


def sysctl(name: str) -> Optional[str]:
    p = Path("/proc/sys") / name.replace(".", "/")
    try:
        return p.read_text().strip()
    except OSError:
        return None


def has_apparmor_parser() -> bool:
    return which("apparmor_parser") is not None


def fuse_device() -> bool:
    return Path("/dev/fuse").exists()


# --------------------------------------------------------------------- hardware


def mem_total_gb() -> Optional[float]:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return None


def cpu_count() -> int:
    return os.cpu_count() or 0


def disk_free_gb(path: str | Path) -> Optional[float]:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        st = shutil.disk_usage(p)
        return st.free / 1024**3
    except OSError:
        return None


def primary_ip() -> str:
    """The address a LAN client would use for this host, without sending anything."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# -------------------------------------------------------------------------- GPU


@dataclass
class Gpu:
    index: int
    name: str
    memory_mib: int
    driver: str


def nvidia_gpus() -> list[Gpu]:
    r = run(["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    if r.returncode != 0:
        return []
    gpus = []
    for line in r.stdout.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            gpus.append(Gpu(int(parts[0]), parts[1], int(float(parts[2])), parts[3]))
        except ValueError:
            continue
    return gpus


def nvidia_hardware_present() -> bool:
    """An NVIDIA device on the PCI bus, whether or not a driver is loaded."""
    r = run(["lspci"])
    if r.returncode == 0 and re.search(r"NVIDIA", r.stdout, re.I):
        return True
    for dev in Path("/sys/bus/pci/devices").glob("*/vendor") if Path("/sys/bus/pci/devices").exists() else []:
        try:
            if dev.read_text().strip().lower() == "0x10de":
                return True
        except OSError:
            pass
    return False


def nvidia_ctk_version() -> Optional[str]:
    r = run(["nvidia-ctk", "--version"])
    if r.returncode != 0:
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", r.stdout + r.stderr)
    return m.group(1) if m else (r.stdout.strip() or "present")


# ----------------------------------------------------------------------- Docker


def docker_version() -> Optional[str]:
    r = run(["docker", "version", "--format", "{{.Server.Version}}"])
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def docker_client_only() -> bool:
    """docker binary present but the daemon is not answering."""
    return which("docker") is not None and docker_version() is None


def compose_version() -> Optional[str]:
    r = run(["docker", "compose", "version", "--short"])
    if r.returncode != 0:
        return None
    return r.stdout.strip().lstrip("v") or None


def version_at_least(version: Optional[str], minimum: str) -> bool:
    if not version:
        return False

    def parse(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

    try:
        return parse(version) >= parse(minimum)
    except ValueError:
        return False


def docker_runtimes() -> list[str]:
    r = run(["docker", "info", "--format", "{{json .Runtimes}}"])
    if r.returncode != 0:
        return []
    try:
        return sorted(json.loads(r.stdout).keys())
    except (ValueError, AttributeError):
        return []


def docker_gpu_smoke_test(timeout: int = 120) -> tuple[bool, str]:
    """Run nvidia-smi inside a throwaway container: proves the toolkit is wired
    into Docker, which `nvidia-ctk --version` alone does not."""
    r = run(["docker", "run", "--rm", "--gpus", "all", "debian:bookworm-slim", "nvidia-smi", "-L"], timeout=timeout)
    if r.returncode == 0:
        return True, r.stdout.strip()
    return False, (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "unknown error"


def docker_root_dir() -> Optional[str]:
    r = run(["docker", "info", "--format", "{{.DockerRootDir}}"])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


# ------------------------------------------------------------------------ ports


@dataclass
class PortUse:
    port: int
    address: str
    process: str


def listening_ports() -> list[PortUse]:
    """Every TCP listener, from `ss`; process names need root."""
    r = run(["ss", "-ltnpH"])
    if r.returncode != 0:
        r = run(["ss", "-ltnH"])
    out: list[PortUse] = []
    if r.returncode != 0:
        return out
    for line in r.stdout.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        local = cols[3]
        addr, _, port = local.rpartition(":")
        try:
            portnum = int(port)
        except ValueError:
            continue
        proc = ""
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        if m:
            proc = f"{m.group(1)} (pid {m.group(2)})"
        out.append(PortUse(portnum, addr, proc))
    return out


def port_conflicts(ports: dict[int, str], bind: dict[int, str] | None = None) -> list[tuple[int, str, PortUse]]:
    """ports: port -> what we need it for. Returns the ones something already
    listens on. A wildcard listener conflicts with any bind address; a listener
    on a specific address conflicts only with the same address or our wildcard."""
    bind = bind or {}
    listeners = listening_ports()
    conflicts = []
    for port, purpose in ports.items():
        want = bind.get(port, "0.0.0.0")
        for l in listeners:
            if l.port != port:
                continue
            l_any = l.address in ("*", "0.0.0.0", "[::]", "::", "")
            w_any = want in ("0.0.0.0", "::", "*")
            if l_any or w_any or l.address.strip("[]") == want:
                conflicts.append((port, purpose, l))
                break
    return conflicts


def port_is_free(port: int, address: str = "0.0.0.0") -> bool:
    """Bind test; used as the fallback when `ss` is missing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((address, port))
            return True
        except OSError:
            return False
