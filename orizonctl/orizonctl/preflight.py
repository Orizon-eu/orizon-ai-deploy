"""Host readiness checks. Read-only; `up` applies the fixes it can."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import click

from . import hostinfo as h
from . import ui
from .state import State

MIN_COMPOSE = "2.24.0"  # `env_file: {path, required: false}` needs it
MIN_RAM_GB = 32
MIN_CPUS = 8
MIN_DISK_APP_GB = 50
MIN_DISK_MODELS_GB = 100
BACKEND_CPUS = 16.0


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    detail: str = ""
    hint: str = ""


def _ok(name, detail="", hint=""):
    return Check(name, "ok", detail, hint)


def _warn(name, detail="", hint=""):
    return Check(name, "warn", detail, hint)


def _fail(name, detail="", hint=""):
    return Check(name, "fail", detail, hint)


def _info(name, detail="", hint=""):
    return Check(name, "info", detail, hint)


def orizon_owned_port(port: int) -> str | None:
    """Name of an orizon-* container already publishing this port, if any: a
    re-run over a live stack is not a conflict."""
    r = h.run(["docker", "ps", "--filter", f"publish={port}", "--format", "{{.Names}}"])
    if r.returncode != 0:
        return None
    names = [n for n in r.stdout.split() if n.startswith("orizon-")]
    return names[0] if names else None


HARD_CHECK_NAMES = ("Operating system", "Docker", "Docker Compose", "App directory")
_QUICK: dict[str, bool] = {"on": False}  # skips the registry round-trip in quick mode


def run_checks(
    repo: Path,
    state: State | None,
    values: dict[str, str],
    gpu_smoke: bool = False,
    quick: bool = False,
) -> list[Check]:
    """All host checks; with `quick`, only the ones that decide whether the
    wizard can run at all (OS, Docker, Compose, app directory). `setup` uses
    quick before asking its questions and the full set afterwards, so the
    registry and port checks see the answers rather than a previous state."""
    _QUICK["on"] = quick
    try:
        checks = _run_checks(repo, state, values, gpu_smoke and not quick)
    finally:
        _QUICK["on"] = False
    if quick:
        return [c for c in checks if c.name in HARD_CHECK_NAMES]
    return checks


def _run_checks(repo: Path, state: State | None, values: dict[str, str], gpu_smoke: bool) -> list[Check]:
    checks: list[Check] = []
    wants_gpu = state.uses_gpu if state else bool(h.nvidia_gpus())

    # ------------------------------------------------------------------ OS
    if not h.is_linux():
        checks.append(_fail("Operating system", f"{h.arch()} {h.kernel()} is not Linux", "The stack is deployed on Ubuntu Server."))
    else:
        osr = h.os_release()
        pretty = osr.get("PRETTY_NAME", "unknown Linux")
        if osr.get("ID") == "ubuntu":
            ver = osr.get("VERSION_ID", "0")
            if h.version_at_least(ver, "22.04"):
                checks.append(_ok("Operating system", pretty))
            else:
                checks.append(_warn("Operating system", pretty, "Ubuntu 22.04 or newer is what this installer is tested on."))
        else:
            checks.append(_warn("Operating system", pretty, "Not Ubuntu: install.sh's package steps assume apt and the Docker/NVIDIA Ubuntu repositories."))

    if h.arch() in ("x86_64", "amd64"):
        checks.append(_ok("Architecture", h.arch()))
    elif wants_gpu:
        checks.append(_fail("Architecture", h.arch(), "The vLLM and transcription images pin CUDA x86 wheels and are published for linux/amd64 only. Use external model endpoints on this host."))
    else:
        checks.append(_warn("Architecture", h.arch(), "backend/frontend/MCP images are multi-arch; the bundled model servers are amd64 only."))

    if not h.is_root():
        checks.append(_warn("Privileges", "not running as root", "install.sh and orizonctl expect sudo: they write under /opt, /etc and the Docker socket."))
    else:
        checks.append(_ok("Privileges", "root"))

    # -------------------------------------------------------------- hardware
    mem = h.mem_total_gb()
    if mem is None:
        checks.append(_info("Memory", "could not read /proc/meminfo"))
    elif mem < 16:
        checks.append(_fail("Memory", f"{mem:.0f} GB", "docker-compose.yml reserves 32 GB for the backend alone; under 16 GB the stack will not stay up."))
    elif mem < MIN_RAM_GB:
        checks.append(_warn("Memory", f"{mem:.0f} GB", f"{MIN_RAM_GB} GB or more is recommended; indexing large workspaces is memory-bound."))
    else:
        checks.append(_ok("Memory", f"{mem:.0f} GB"))

    cpus = h.cpu_count()
    checks.append(_ok("CPUs", str(cpus)) if cpus >= MIN_CPUS else _warn("CPUs", str(cpus), f"{MIN_CPUS}+ recommended."))
    checks.append(backend_cpu_check(values, cpus))

    free_app = h.disk_free_gb(repo)
    if free_app is not None:
        c = _ok if free_app >= MIN_DISK_APP_GB else _warn
        checks.append(c("Disk (app + images)", f"{free_app:.0f} GB free at {repo}", "" if free_app >= MIN_DISK_APP_GB else f"Images, logs and the database live here; {MIN_DISK_APP_GB} GB+ recommended."))
    models_path = values.get("MODELS_PATH")
    if models_path and (wants_gpu or (state is not None and state.download_models)):
        free_models = h.disk_free_gb(models_path)
        if free_models is not None:
            existing = Path(models_path).exists() and any(Path(models_path).iterdir()) if Path(models_path).exists() else False
            if free_models >= MIN_DISK_MODELS_GB or existing:
                checks.append(_ok("Disk (models)", f"{free_models:.0f} GB free at {models_path}"))
            else:
                checks.append(_warn("Disk (models)", f"{free_models:.0f} GB free at {models_path}", "The default text model alone is ~36 GB; embeddings, reranker and ASR add ~6 GB."))

    # ---------------------------------------------------------------- Docker
    dv = h.docker_version()
    if dv is None:
        if h.which("docker"):
            checks.append(_fail("Docker", "binary present, daemon not answering", "systemctl start docker; or add yourself to the docker group / use sudo."))
        else:
            checks.append(_fail("Docker", "not installed", "install.sh installs Docker Engine from download.docker.com."))
    else:
        checks.append(_ok("Docker", f"engine {dv}"))
        cv = h.compose_version()
        if cv is None:
            checks.append(_fail("Docker Compose", "plugin missing", "apt install docker-compose-plugin"))
        elif not h.version_at_least(cv, MIN_COMPOSE):
            checks.append(_warn("Docker Compose", f"v{cv}", f"v{MIN_COMPOSE}+ is needed for the optional .env.secrets mount (`required: false`)."))
        else:
            checks.append(_ok("Docker Compose", f"v{cv}"))

    # ------------------------------------------------------------------ GPU
    gpus = h.nvidia_gpus()
    if gpus:
        names = ", ".join(f"[{g.index}] {g.name} {g.memory_label()}" for g in gpus)
        checks.append(_ok("NVIDIA driver", f"{gpus[0].driver}; {names}"))
    elif wants_gpu:
        if h.nvidia_hardware_present():
            checks.append(_fail("NVIDIA driver", "GPU on the bus but nvidia-smi is missing or failing", "sudo ubuntu-drivers install --gpgpu && sudo reboot   (or install the vendor .run driver)"))
        else:
            checks.append(_fail("NVIDIA driver", "no NVIDIA GPU detected", "Local model servers need a CUDA GPU. Re-run `orizonctl configure` and choose external endpoints."))
    else:
        checks.append(_info("NVIDIA driver", "no GPU; external model endpoints only"))

    if wants_gpu and dv is not None:
        ctk = h.nvidia_ctk_version()
        if ctk is None:
            checks.append(_fail("NVIDIA container toolkit", "nvidia-ctk not found", "install.sh installs it; or follow https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"))
        else:
            checks.append(_ok("NVIDIA container toolkit", ctk))
        runtimes = h.docker_runtimes()
        if runtimes and "nvidia" not in runtimes:
            checks.append(_fail("Docker nvidia runtime", f"runtimes: {', '.join(runtimes)}", "sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"))
        elif runtimes:
            checks.append(_ok("Docker nvidia runtime", "configured"))
        if gpu_smoke and gpus and ctk:
            ok, detail = h.docker_gpu_smoke_test()
            checks.append(_ok("GPU inside a container", detail.splitlines()[0] if detail else "nvidia-smi ran") if ok else _fail("GPU inside a container", detail, "The toolkit is installed but Docker cannot hand out the GPU. Restart docker after `nvidia-ctk runtime configure`."))

    # ------------------------------------------------------------------ misc
    if h.fuse_device():
        checks.append(_ok("/dev/fuse", "present (backend SMB mounts)"))
    else:
        checks.append(_fail("/dev/fuse", "missing", "sudo modprobe fuse && echo fuse | sudo tee /etc/modules-load.d/fuse.conf   (docker-compose.yml passes /dev/fuse to the backend)"))

    for tool in ("git", "curl"):
        checks.append(_ok(tool, h.which(tool) or "") if h.which(tool) else _warn(tool, "not found", f"apt install {tool}; the helper scripts use it."))

    if state and state.mcp:
        restrict = h.sysctl("kernel.apparmor_restrict_unprivileged_userns")
        if h.has_apparmor_parser() and restrict == "1":
            if Path("/etc/apparmor.d/rootlesskit").exists():
                checks.append(_ok("AppArmor userns (sandbox)", "rootlesskit profile installed"))
            else:
                checks.append(_warn("AppArmor userns (sandbox)", "restriction active, profile missing", "`orizonctl up` runs ./setup-sandbox-host.sh, which installs apparmor/rootlesskit."))
        else:
            checks.append(_ok("AppArmor userns (sandbox)", "no restriction on this kernel"))

    # ----------------------------------------------------------- app files
    if (repo / "docker-compose.yml").exists() and (repo / "env.example").exists():
        checks.append(_ok("App directory", str(repo)))
        checks.extend(_checkout_hazards(repo))
    else:
        checks.append(_fail("App directory", f"{repo} has no docker-compose.yml / env.example", "`orizonctl init` writes the bundled deployment files there."))

    # -------------------------------------------------------------- registry
    if state is not None and dv is not None and values.get("ORIZON_VERSION") and not _QUICK.get("on"):
        from .compose import image_accessible
        from .schema import PROBE_IMAGE

        ref = f"{values.get('ORIZON_IMAGE_PREFIX') or state.image_prefix}/{PROBE_IMAGE}:{values['ORIZON_VERSION']}"
        ok, msg = image_accessible(ref)
        if ok:
            checks.append(_ok("Registry access", ref))
        else:
            login = f"docker login {state.registry_host}" if state.registry_host != "docker.io" else "docker login"
            checks.append(_fail("Registry access", f"{ref}: {msg}", f"Wrong prefix/tag, or not logged in: {login} (a GitLab deploy token with read_registry works). Re-run `orizonctl configure` to redo the login."))

    # ----------------------------------------------------------------- ports
    if state is not None:
        wanted = state.required_ports(values)
        binds = state.bind_addresses(values)
        conflicts = h.port_conflicts(wanted, binds)
        real = []
        for port, purpose, use in conflicts:
            owner = orizon_owned_port(port)
            if owner:
                continue  # our own running stack
            real.append((port, purpose, use))
        if real:
            for port, purpose, use in real:
                who = use.process or "unknown process (run as root to see it)"
                checks.append(_fail(f"Port {port}", f"needed for {purpose}; in use by {who} on {use.address or '*'}", "Stop that service or pick another port in `orizonctl configure`."))
        else:
            checks.append(_ok("Ports", ", ".join(str(p) for p in sorted(wanted)) + " free (or held by this stack)"))

    return checks


def backend_cpu_check(values: dict[str, str], cpus: int | None = None) -> Check:
    """docker-compose.yml caps the backend at BACKEND_CPU_LIMIT (16 by default),
    and Docker refuses to create a container whose limit exceeds the host's CPUs."""
    cpus = h.cpu_count() if cpus is None else cpus
    raw = (values.get("BACKEND_CPU_LIMIT") or "").strip()
    try:
        limit = float(raw) if raw else BACKEND_CPUS
    except ValueError:
        return _fail("Backend CPU limit", f"BACKEND_CPU_LIMIT={raw} is not a number", "Set it in .env to a number such as 8.0.")
    if cpus < limit:
        return _fail(
            "Backend CPU limit",
            f"this host has {cpus} CPUs; the backend is limited to {limit:g}",
            f"Docker refuses to start the backend with a CPU limit above the host's CPU count. "
            f"Set BACKEND_CPU_LIMIT={cpus}.0 and BACKEND_CPU_RESERVATION at or below it in .env.",
        )
    if cpus < BACKEND_CPUS:
        return _warn("Backend CPU limit", f"this host has {cpus} CPUs; the backend is sized for {BACKEND_CPUS:g}", "It runs, but indexing and parallel chats are slower than on a 16 CPU host.")
    return _ok("Backend CPU limit", f"{limit:g} of {cpus} CPUs")


def _checkout_hazards(repo: Path) -> list[Check]:
    """A source-tree checkout (a vX.Y.Z branch) bind-mounts build products such
    as node_modules/.prisma into the backend. Pulled images do not need them,
    and an empty directory mounted over /app/node_modules/.prisma breaks the
    backend's Prisma client. The deploy branch has no such mounts."""
    out: list[Check] = []
    try:
        text = (repo / "docker-compose.yml").read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- ./") and "node_modules" in s and ":" in s:
            src = s[2:].split(":", 1)[0]
            if not (repo / src).exists():
                out.append(_warn("Compose bind mount", f"{src} is mounted but absent", "This checkout is a source tree without built assets. Deploy from the `deploy` branch (pulled images) or init submodules and run `orizonctl up --build`."))
    return out


def print_checks(checks: list[Check]) -> None:
    for c in checks:
        badge = {"ok": ui.OK, "warn": ui.WARN, "fail": ui.FAIL, "info": ui.INFO}[c.status]
        click.echo(f"[{badge}] {c.name:<28} {c.detail}")
        if c.hint and c.status in ("warn", "fail"):
            ui.note(c.hint)


def has_failures(checks: list[Check]) -> bool:
    return any(c.status == "fail" for c in checks)


def apply_host_fixes(repo: Path, state: State) -> None:
    """The fixes `up` is allowed to make on its own: they are idempotent and
    local to this stack's needs."""
    if not h.fuse_device():
        ui.step("Loading the fuse kernel module (backend SMB mounts)")
        h.run(["modprobe", "fuse"])
        try:
            Path("/etc/modules-load.d").mkdir(parents=True, exist_ok=True)
            Path("/etc/modules-load.d/fuse.conf").write_text("fuse\n")
        except OSError:
            pass
    if state.mcp:
        script = repo / "setup-sandbox-host.sh"
        if script.exists():
            ui.step("Preparing the host for the code sandbox (setup-sandbox-host.sh)")
            r = subprocess.run(["bash", str(script)], cwd=str(repo))
            if r.returncode != 0:
                ui.warn("setup-sandbox-host.sh exited non-zero; sandbox-dind may restart-loop. See its output above.")
