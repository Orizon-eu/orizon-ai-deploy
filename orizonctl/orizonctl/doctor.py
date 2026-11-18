"""Post-deployment health report: what is running, what answers, what is
about to run out."""

from __future__ import annotations

import socket
import ssl
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import hostinfo as h
from .assets import drifted, is_bundle_available
from .compose import Stack, http_ok
from .preflight import Check, _fail, _info, _ok, _warn, backend_cpu_check
from .state import State

DB_AUTH_FAILED = '"error_code":"P1000"'


def report(stack: Stack, state: State, values: dict[str, str]) -> list[Check]:
    checks: list[Check] = []

    # containers
    services = state.all_services()
    statuses = {s: stack.service_status(s) for s in services}
    active = set(stack.config_services()) if "absent" in statuses.values() else set()
    unplaced = stack.explain_missing([s for s, st in statuses.items() if st == "absent" and s not in active])
    for s, st in statuses.items():
        if st == "ready":
            checks.append(_ok(f"container {s}", "running" + (" (healthy)" if _has_health(stack, s) else "")))
        elif s in unplaced:
            why, fix = unplaced[s]
            checks.append(_fail(f"container {s}", why, fix))
        elif st == "absent":
            checks.append(_fail(f"container {s}", "not created", "orizonctl up"))
        elif st == "starting":
            checks.append(_warn(f"container {s}", "starting"))
        else:
            checks.append(_fail(f"container {s}", st[7:], f"orizonctl logs {s}"))
    if state.mcp and stack.has_file(mcp=True):
        for s in stack.config_services(mcp=True):
            st = stack.service_status(s, mcp=True)
            checks.append(_ok(f"container {s}", "running") if st == "ready" else _warn(f"container {s}", st, f"orizonctl logs --mcp {s}"))

    # http
    bind = values.get("SERVER_BIND_ADDRESS") or "127.0.0.1"
    backend = f"http://{bind}:{values.get('SERVER_PORT', '8005')}/health"
    backend_ok = http_ok(backend)
    checks.append(_ok("backend /health", backend) if backend_ok else _fail("backend /health", f"no answer at {backend}", "orizonctl logs backend"))
    backend_unwell = not backend_ok or statuses.get("backend", "ready") != "ready"
    if backend_unwell and statuses.get("backend") not in (None, "absent") and DB_AUTH_FAILED in stack.tail("backend", 300):
        checks.append(_fail("database login", "the database rejects the backend's password (P1000)", "`orizonctl up` sets the database password from .env; then `orizonctl restart backend`."))
    frontend = f"http://127.0.0.1:{values.get('FRONTEND_PORT', '3000')}/"
    checks.append(_ok("frontend", frontend) if http_ok(frontend) else _fail("frontend", f"no answer at {frontend}", "orizonctl logs frontend"))
    if state.uses_nginx:
        np = values.get("NGINX_HTTP_PORT") or "80"
        checks.append(_ok("nginx", "/nginx-health answers") if http_ok(f"http://127.0.0.1:{np}/nginx-health") else _fail("nginx", f"no answer on :{np}", "orizonctl logs nginx"))
    if state.public_url:
        checks.append(_ok("public URL", state.public_url) if http_ok(state.public_url, timeout=8) else _warn("public URL", f"{state.public_url} not reachable from this host", "DNS, firewall, or the proxy in front; not necessarily a stack problem."))

    # model endpoints
    for s, port_key in (("vllm-server", "VLLM_PORT"), ("embedding-vllm-server", "EMBEDDING_VLLM_PORT"), ("rerank-vllm-server", "RERANK_VLLM_PORT"), ("transcription-vllm-server", "TRANSCRIPTION_VLLM_PORT"), ("diarization-server", "DIARIZATION_SERVER_PORT")):
        if s in services:
            url = f"http://127.0.0.1:{values.get(port_key) or _default_port(port_key)}/health"
            checks.append(_ok(f"{s} /health", url) if http_ok(url) else _warn(f"{s} /health", f"not ready at {url}", "Weights may still be loading; see orizonctl logs " + s))

    # GPU as seen by the text server
    if state.text_mode == "local" and stack.service_status("vllm-server") == "ready":
        r = stack.exec("vllm-server", ["nvidia-smi", "-L"], capture=True, timeout=30)
        if r.returncode == 0:
            first = (r.stdout.decode(errors="replace").strip().splitlines() or ["?"])[0]
            checks.append(_ok("GPU in vllm-server", first))
        else:
            checks.append(_fail("GPU in vllm-server", "nvidia-smi failed inside the container", "nvidia container toolkit / docker runtime"))

    # disk
    root = h.docker_root_dir()
    if root:
        free = h.disk_free_gb(root)
        if free is not None:
            checks.append((_ok if free > 30 else _warn if free > 10 else _fail)("disk: docker root", f"{free:.0f} GB free at {root}", "" if free > 30 else "Prune old images: docker image prune -a"))
    mp = values.get("MODELS_PATH")
    if mp and state.keeps_models:
        free = h.disk_free_gb(mp)
        if free is not None:
            checks.append((_ok if free > 20 else _warn)("disk: models", f"{free:.0f} GB free at {mp}"))
    checks.append(backend_cpu_check(values))
    if is_bundle_available():
        changed = drifted(stack.repo)
        if changed:
            checks.append(_warn("deployment files", f"differ from this orizonctl's version: {', '.join(changed)}", "Local edits, or older files kept over an update. `orizonctl init --force` replaces them and saves yours under .orizon/local-edits/."))
    logs = stack.repo / "backend-orizon-ai" / "logs"
    if logs.exists():
        size = sum(f.stat().st_size for f in logs.rglob("*") if f.is_file()) / 1024**3
        checks.append((_ok if size < 5 else _warn)("backend logs", f"{size:.1f} GB in {logs}", "" if size < 5 else "orizonctl systemd install  (daily cleanup timer)"))

    # certificate
    if state.https and state.domain:
        exp = cert_expiry(state.domain)
        if exp is None:
            checks.append(_warn("TLS certificate", f"could not read the certificate served for {state.domain}"))
        else:
            days = (exp - datetime.now(timezone.utc)).days
            if days < 0:
                checks.append(_fail("TLS certificate", f"expired {-days} days ago"))
            elif days < 14:
                checks.append(_warn("TLS certificate", f"expires in {days} days", "orizonctl logs certbot"))
            else:
                checks.append(_ok("TLS certificate", f"valid for {days} more days"))

    checks.append(_info("version", f"ORIZON_VERSION={values.get('ORIZON_VERSION', '?')}  configured {state.configured_at or 'never'}"))
    return checks


def _has_health(stack: Stack, service: str) -> bool:
    ids = stack.container_ids(service)
    if not ids:
        return False
    _, health, _ = stack.inspect_state(ids[0])
    return health == "healthy"


def _default_port(key: str) -> int:
    from .schema import DEFAULT_PORTS

    return DEFAULT_PORTS.get(key, 0)


def cert_expiry(domain: str, port: int = 443) -> datetime | None:
    """notAfter of whatever certificate the domain serves right now (staging
    certs included: verification is off, this is a lifetime check)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((domain, port), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None
    if not der:
        return None
    try:
        r = subprocess.run(["openssl", "x509", "-noout", "-enddate", "-inform", "DER"], input=der, capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    line = r.stdout.decode(errors="replace").strip()  # notAfter=Sep 15 12:00:00 2026 GMT
    _, _, when = line.partition("=")
    try:
        return datetime.strptime(when.strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
